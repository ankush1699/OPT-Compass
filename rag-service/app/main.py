import json
import queue
import threading
import time
from contextlib import asynccontextmanager

import hmac
from datetime import datetime, timezone
from pathlib import Path

import anyio
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import accesslog
from app.analytics import Analytics, is_bot, make_salt, visitor_id
from app.config import load_settings
from app.db import connect
from app.demo import DemoGuard, client_ip
from app.generate import ABSTAIN_PHRASE, DISCLAIMER, Answer, answer, answer_stream
from app.llm import ProviderError
from app.retrieve import search
from app.security import RateLimiter, parse_keys, require_key
from app.tracing import trace

STATIC_DIR = Path(__file__).parent / "static"
KIND = {"eCFR": "REGULATION", "Federal Register": "COMMENTARY"}


class AskRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)
    top_k: int = Field(default=6, ge=1, le=15)
    mode: str = Field(default="rerank", pattern="^(dense|keyword|hybrid|rerank)$")
    stream: bool = Field(default=False, description="true: answer as Server-Sent Events (sources, token..., done)")


class DemoRequest(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class SourceOut(BaseModel):
    n: int
    citation: str
    kind: str  # REGULATION (current law) or COMMENTARY (2016 preamble)
    url: str
    snippet: str
    score: float


class UsageOut(BaseModel):
    model: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None when the price is unknown; 0.0 on the free tier
    generation_s: float
    total_s: float


class AskResponse(BaseModel):
    answer: str | None
    mode: str
    disclaimer: str
    sources: list[SourceOut]
    usage: UsageOut


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    accesslog.configure()
    app.state.api_keys = parse_keys(settings.api_keys)
    app.state.limiter = RateLimiter(settings.rate_limit_per_minute)
    app.state.conn = connect()
    app.state.demo_enabled = settings.demo_enabled
    app.state.demo = DemoGuard(settings.demo_daily_limit, settings.demo_per_minute, settings.demo_max_concurrent)
    app.state.trust_proxy = settings.trust_proxy_headers
    app.state.stats_key = settings.stats_key
    app.state.salt = make_salt(settings.analytics_salt)
    app.state.analytics = Analytics(analytics_connect)
    yield
    app.state.conn.close()


def analytics_connect():
    """A separate autocommit connection, so a slow or failed analytics write never touches the retrieval connection."""
    return psycopg.connect(load_settings().database_url, autocommit=True)


app = FastAPI(title="OPT Compass rag-service", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def basic_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.get("/health")
def health() -> dict:
    count = app.state.conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    return {"status": "ok", "chunks": count}


def _sources(hits) -> list[SourceOut]:
    return [
        SourceOut(n=i, citation=h.citation, kind=KIND.get(h.source, h.source), url=h.source_url, snippet=h.content[:400], score=round(h.score, 4))
        for i, h in enumerate(hits, start=1)
    ]


def _usage(result: Answer, started: float) -> UsageOut:
    return UsageOut(
        model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        generation_s=round(result.latency_s, 3),
        total_s=round(time.perf_counter() - started, 3),
    )


def _log(key: str, req: AskRequest, status: int, started: float, hits=(), result: Answer | None = None, error: str | None = None):
    accesslog.log_request(
        key=key,
        path="/ask",
        status=status,
        stream=getattr(req, "stream", True),
        retrieval_mode=getattr(req, "mode", "rerank"),
        question_chars=len(req.question),
        sources=len(hits),
        answer_mode=result.mode if result else None,
        model=result.model if result else None,
        input_tokens=result.input_tokens if result else 0,
        output_tokens=result.output_tokens if result else 0,
        cost_usd=result.cost_usd if result else None,
        latency_ms=round((time.perf_counter() - started) * 1000),
        error=error,
    )


def _event(name: str, data) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


PUBLIC_ERRORS = {
    "quota": "The free AI model has reached its usage limit for now. Please try again later.",
    "provider_error": "The AI model could not answer right now. Please try again in a minute.",
    "retrieval_error": "The search failed. Please try again.",
}


def _outcome_of(result: Answer) -> str:
    if result.mode == "retrieval-only":
        return "retrieval_only"
    return "abstained" if ABSTAIN_PHRASE.lower() in (result.text or "").lower() else "answered"


def _failure_of(exc: Exception) -> str:
    text = str(exc).lower()
    return "quota" if isinstance(exc, ProviderError) and ("quota" in text or "429" in text) else "provider_error"


def _stream_worker(req, key: str, started: float, out: queue.Queue, cancelled: threading.Event, public: bool = False, on_done=None) -> None:
    """Runs the whole pipeline in ONE thread, so the tracing spans open and close in the thread that made them.
    `public` hides provider error text from the browser (it can quote the provider's response); on_done(outcome, hits, result)
    runs last, even on failure."""
    hits, result, error, outcome = [], None, None, "provider_error"
    top_k, mode = getattr(req, "top_k", 6), getattr(req, "mode", "rerank")

    def fail(kind: str, detail: str) -> None:
        nonlocal outcome
        outcome = kind
        out.put(_event("error", {"detail": PUBLIC_ERRORS[kind] if public else detail}))

    try:
        with trace("ask", metadata={"retrieval_mode": mode, "top_k": str(top_k), "stream": "true"}) as root:
            root.update(input={"question": req.question})
            try:
                hits = search(app.state.conn, req.question, top_k, mode)
            except Exception:  # a failed query must not poison the shared connection
                app.state.conn.rollback()
                error = "retrieval failed"
                root.update(level="ERROR", status_message=error)
                fail("retrieval_error", error)
                return
            out.put(_event("sources", [s.model_dump() for s in _sources(hits)]))
            deltas = answer_stream(req.question, hits)
            try:
                for item in deltas:
                    if isinstance(item, str):
                        if cancelled.is_set():
                            error, outcome = "client disconnected", "disconnected"
                            return
                        out.put(_event("token", {"text": item}))
                    else:
                        result = item
            finally:
                deltas.close()
            outcome = _outcome_of(result)
            root.update(output={"mode": result.mode, "answer": result.text, "cost_usd": result.cost_usd})
            out.put(_event("done", {"mode": result.mode, "disclaimer": DISCLAIMER, "usage": _usage(result, started).model_dump()}))
    except ProviderError as exc:
        error = str(exc)
        fail(_failure_of(exc), error)
    except Exception as exc:
        error = f"{type(exc).__name__}"
        fail("provider_error", "generation failed")
    finally:
        try:
            _log(key, req, 200, started, hits, result, error)
            if on_done:
                on_done(outcome, hits, result)
        finally:
            out.put(None)


def _start_stream(req, key: str, started: float, public: bool = False, on_done=None) -> tuple[queue.Queue, threading.Event]:
    """Starts the worker now, not when the response body is first read, so a visitor who leaves early cannot leak the
    generation slot the worker will release."""
    out: queue.Queue = queue.Queue()
    cancelled = threading.Event()
    threading.Thread(target=_stream_worker, args=(req, key, started, out, cancelled, public, on_done), daemon=True).start()
    return out, cancelled


async def _sse(out: queue.Queue, cancelled: threading.Event):
    try:
        while (item := await anyio.to_thread.run_sync(out.get)) is not None:
            yield item
    finally:
        cancelled.set()


@app.post("/ask", response_model=AskResponse)
def ask(req: AskRequest, key: str = Depends(require_key)):
    started = time.perf_counter()
    if req.stream:
        return StreamingResponse(
            _sse(*_start_stream(req, key, started)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    hits, result = [], None
    with trace("ask", metadata={"retrieval_mode": req.mode, "top_k": str(req.top_k)}) as root:
        root.update(input={"question": req.question})
        try:
            hits = search(app.state.conn, req.question, req.top_k, req.mode)
        except Exception as exc:  # a failed query must not poison the shared connection
            app.state.conn.rollback()
            root.update(level="ERROR", status_message="retrieval failed")
            _log(key, req, 500, started, error="retrieval failed")
            raise HTTPException(status_code=500, detail="retrieval failed") from exc
        try:
            result = answer(req.question, hits)
        except ProviderError as exc:
            root.update(level="ERROR", status_message=str(exc))
            _log(key, req, 502, started, hits, error=str(exc))
            raise HTTPException(status_code=502, detail=f"The model provider failed: {exc}") from exc
        root.update(output={"mode": result.mode, "answer": result.text, "cost_usd": result.cost_usd})
    _log(key, req, 200, started, hits, result)
    return AskResponse(
        answer=result.text,
        mode=result.mode,
        disclaimer=DISCLAIMER,
        sources=_sources(hits),
        usage=_usage(result, started),
    )


# ---- public demo page and its own endpoint: no API key, so every request passes the DemoGuard first ----
def _require_demo(request: Request) -> None:
    if not request.app.state.demo_enabled:
        raise HTTPException(status_code=404, detail="Not found")


def _visitor(request: Request) -> str:
    return visitor_id(
        request.app.state.salt,
        client_ip(request, request.app.state.trust_proxy),
        request.headers.get("user-agent", ""),
        datetime.now(timezone.utc).date(),
    )


PAGE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
TURNED_AWAY = {
    "rate_limited": (429, "You are asking quickly. Please wait a moment and try again."),
    "daily_cap": (429, "Today's free demo limit has been reached. It resets at midnight UTC; please come back tomorrow."),
    "busy": (503, "The demo is busy answering other visitors. Please try again in a few seconds."),
}


@app.get("/", include_in_schema=False)
def home(request: Request):
    _require_demo(request)
    agent = request.headers.get("user-agent", "")
    request.app.state.analytics.record(_visitor(request), "page_view", "bot" if is_bot(agent) else "ok")
    return FileResponse(STATIC_DIR / "index.html", headers={"Content-Security-Policy": PAGE_CSP, "Cache-Control": "no-cache"})


@app.post("/demo/ask", include_in_schema=False)
def demo_ask(req: DemoRequest, request: Request):
    _require_demo(request)
    state, visitor, started = request.app.state, _visitor(request), time.perf_counter()
    refused = state.demo.admit(visitor)
    if refused:
        reason, wait = refused
        state.analytics.record(visitor, "ask", reason, question_chars=len(req.question))
        status, detail = TURNED_AWAY[reason]
        headers = {"Retry-After": str(max(1, int((wait or 60) + 0.999)))} if wait else {}
        return JSONResponse({"detail": detail, "reason": reason}, status_code=status, headers=headers)

    def finished(outcome: str, hits, result) -> None:
        state.demo.release()
        state.analytics.record(
            visitor, "ask", outcome,
            latency_ms=round((time.perf_counter() - started) * 1000),
            input_tokens=result.input_tokens if result else None,
            output_tokens=result.output_tokens if result else None,
            question_chars=len(req.question),
        )

    out, cancelled = _start_stream(req, "demo", started, public=True, on_done=finished)
    return StreamingResponse(
        _sse(out, cancelled), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.get("/stats", include_in_schema=False)
def stats(request: Request, days: int = Query(default=30, ge=1, le=365)):
    """Usage counts for the owner. Needs STATS_KEY, sent as X-Stats-Key or as a Bearer token."""
    state = request.app.state
    if not state.stats_key:
        raise HTTPException(status_code=503, detail="STATS_KEY is not configured.")
    bearer = request.headers.get("authorization", "")
    presented = bearer[7:].strip() if bearer.lower().startswith("bearer ") else request.headers.get("x-stats-key", "").strip()
    if not hmac.compare_digest(presented.encode(), state.stats_key.encode()):
        raise HTTPException(status_code=401, detail="Missing or invalid stats key.")
    try:
        summary = state.analytics.summary(days)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Could not read analytics.") from exc
    return {**summary, "demo_answers_today": state.demo.used_today, "demo_daily_limit": state.demo.daily_limit}
