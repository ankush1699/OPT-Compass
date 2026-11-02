"""Own-site usage analytics: page views and demo questions, kept in the same Postgres as the corpus.

No cookies, no third-party scripts, no question or answer text, no raw IP address. A visitor is a salted hash of
IP + browser string + UTC day, so the same person on two days looks like two visitors and nobody can be looked up.
A failure here is logged and swallowed: analytics must never break a request.
"""
import hashlib
import logging
import os
import re
import secrets
import threading
from datetime import date, datetime, timezone

logger = logging.getLogger("opt_compass.analytics")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            BIGSERIAL PRIMARY KEY,
    ts            TIMESTAMPTZ NOT NULL DEFAULT now(),
    day           DATE NOT NULL,
    visitor       TEXT NOT NULL,
    kind          TEXT NOT NULL,   -- page_view | ask
    outcome       TEXT NOT NULL,   -- ok | bot | answered | abstained | retrieval_only | quota | provider_error | retrieval_error | rate_limited | daily_cap | busy | disconnected
    latency_ms    INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    question_chars INTEGER
);
CREATE INDEX IF NOT EXISTS events_day_idx ON events (day);
"""

BOT = re.compile(r"bot|crawl|spider|slurp|curl|wget|python-requests|python-httpx|go-http|uptime|monitor|headless|preview", re.I)


def is_bot(user_agent: str) -> bool:
    return not user_agent or bool(BOT.search(user_agent))


def visitor_id(salt: str, ip: str, user_agent: str, day: date) -> str:
    raw = f"{salt}|{day.isoformat()}|{ip}|{user_agent}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def make_salt(configured: str) -> str:
    return configured or secrets.token_hex(16)


class Analytics:
    """`connect` returns a new autocommit connection. One connection, guarded by a lock, shared by all request threads."""

    def __init__(self, connect):
        self._connect, self._conn, self._lock = connect, None, threading.Lock()

    def _db(self):
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
            self._conn.execute(SCHEMA)
        return self._conn

    def record(self, visitor: str, kind: str, outcome: str, latency_ms=None, input_tokens=None, output_tokens=None, question_chars=None) -> None:
        try:
            with self._lock:
                self._db().execute(
                    "INSERT INTO events (day, visitor, kind, outcome, latency_ms, input_tokens, output_tokens, question_chars) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (datetime.now(timezone.utc).date(), visitor, kind, outcome, latency_ms, input_tokens, output_tokens, question_chars),
                )
        except Exception as exc:  # never let tracking break the page or an answer
            logger.warning("analytics write failed: %s", type(exc).__name__)
            self._conn = None

    def summary(self, days: int = 30) -> dict:
        with self._lock:
            conn = self._db()
            rows = conn.execute(
                "SELECT day, "
                "count(*) FILTER (WHERE kind = 'page_view' AND outcome = 'ok') AS page_views, "
                "count(DISTINCT visitor) FILTER (WHERE kind = 'page_view' AND outcome = 'ok') AS visitors, "
                "count(*) FILTER (WHERE kind = 'page_view' AND outcome = 'bot') AS bot_views, "
                "count(*) FILTER (WHERE kind = 'ask') AS questions, "
                "count(DISTINCT visitor) FILTER (WHERE kind = 'ask') AS askers, "
                "count(*) FILTER (WHERE kind = 'ask' AND outcome = 'answered') AS answered, "
                "count(*) FILTER (WHERE kind = 'ask' AND outcome = 'abstained') AS abstained, "
                "count(*) FILTER (WHERE kind = 'ask' AND outcome IN ('quota', 'provider_error', 'retrieval_error')) AS errors, "
                "count(*) FILTER (WHERE kind = 'ask' AND outcome IN ('rate_limited', 'daily_cap', 'busy')) AS turned_away, "
                "avg(latency_ms) FILTER (WHERE kind = 'ask' AND outcome IN ('answered', 'abstained'))::int AS avg_latency_ms, "
                "coalesce(sum(input_tokens), 0)::int AS input_tokens, coalesce(sum(output_tokens), 0)::int AS output_tokens "
                "FROM events WHERE day > current_date - %s GROUP BY day ORDER BY day DESC",
                (days,),
            ).fetchall()
        cols = ["day", "page_views", "visitors", "bot_views", "questions", "askers", "answered", "abstained", "errors",
                "turned_away", "avg_latency_ms", "input_tokens", "output_tokens"]
        per_day = [dict(zip(cols, r)) | {"day": r[0].isoformat()} for r in rows]
        return {"days": days, "per_day": per_day, "totals": {c: sum(d[c] or 0 for d in per_day) for c in cols[1:] if c not in ("visitors", "askers", "avg_latency_ms")}}
