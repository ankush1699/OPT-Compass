import json
import threading

import pytest
from fastapi.testclient import TestClient

from app import main
from app.analytics import Analytics, is_bot, visitor_id
from app.demo import DemoGuard
from app.generate import ABSTAIN_PHRASE, Answer
from app.llm import ProviderError
from app.retrieve import Hit
from tests.test_gateway import FakeConn, HIT, final_answer, sse_events

BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh) Safari/605"}
QUESTION = {"question": "How many days of unemployment?"}


class RecordingConn:
    """Stands in for the analytics database: remembers every INSERT and answers summary queries with nothing."""

    closed = False
    inserts: list = []

    def execute(self, sql, params=()):
        if sql.lstrip().startswith("INSERT"):
            RecordingConn.inserts.append(params)

        class Cursor:
            def fetchall(self):
                return []

        return Cursor()


@pytest.fixture
def demo(monkeypatch):
    RecordingConn.inserts = []
    monkeypatch.setenv("DEMO_ENABLED", "1")
    monkeypatch.setenv("DEMO_DAILY_LIMIT", "5")
    monkeypatch.setenv("DEMO_PER_MINUTE", "3")
    monkeypatch.setenv("DEMO_MAX_CONCURRENT", "2")
    monkeypatch.setenv("STATS_KEY", "stats-secret")
    monkeypatch.setenv("ANALYTICS_SALT", "test-salt")
    monkeypatch.setattr(main, "connect", lambda: FakeConn())
    monkeypatch.setattr(main, "analytics_connect", lambda: RecordingConn())
    monkeypatch.setattr(main, "search", lambda *_a, **_k: [HIT])

    def fake_stream(*_a, **_k):
        yield "Ninety "
        yield "days [1]."
        yield final_answer()

    monkeypatch.setattr(main, "answer_stream", fake_stream)
    with TestClient(main.app) as c:
        yield c


def kinds():
    return [(row[2], row[3]) for row in RecordingConn.inserts]  # (kind, outcome)


# ---- switched off by default ----
def test_demo_is_off_unless_enabled(demo, monkeypatch):
    monkeypatch.setenv("DEMO_ENABLED", "0")
    with TestClient(main.app) as off:
        assert off.get("/").status_code == 404
        assert off.post("/demo/ask", json=QUESTION).status_code == 404
        assert off.get("/health").status_code == 200


# ---- page ----
def test_home_serves_the_page_with_a_strict_csp_and_counts_a_view(demo):
    response = demo.get("/", headers=BROWSER)
    assert response.status_code == 200 and "OPT Compass" in response.text
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp and "frame-ancestors 'none'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"
    assert kinds() == [("page_view", "ok")]
    assert demo.get("/static/app.js").status_code == 200


def test_bots_are_counted_separately(demo):
    demo.get("/", headers={"User-Agent": "Googlebot/2.1"})
    demo.get("/", headers={"User-Agent": ""})  # no user agent at all
    assert kinds() == [("page_view", "bot"), ("page_view", "bot")]


def test_page_never_uses_inner_html():
    assert "innerHTML" not in open("app/static/app.js").read()


# ---- /demo/ask ----
def test_demo_ask_streams_sources_tokens_and_done_without_a_key(demo):
    with demo.stream("POST", "/demo/ask", json=QUESTION, headers=BROWSER) as response:
        assert response.status_code == 200
        events = sse_events(response)
    assert [name for name, _ in events] == ["sources", "token", "token", "done"]
    assert events[0][1][0]["kind"] == "REGULATION"
    assert kinds()[-1] == ("ask", "answered")


def test_demo_ask_records_no_question_text(demo):
    demo.post("/demo/ask", json={"question": "my private immigration situation"}, headers=BROWSER)
    assert "private" not in json.dumps(RecordingConn.inserts, default=str)
    assert RecordingConn.inserts[-1][-1] == len("my private immigration situation")  # only the length


def test_abstention_is_recorded_as_such(demo, monkeypatch):
    def abstain(*_a, **_k):
        yield ABSTAIN_PHRASE
        yield final_answer(text=ABSTAIN_PHRASE)

    monkeypatch.setattr(main, "answer_stream", abstain)
    demo.post("/demo/ask", json=QUESTION, headers=BROWSER)
    assert kinds()[-1] == ("ask", "abstained")


def test_validation_errors_are_422(demo):
    assert demo.post("/demo/ask", json={"question": "hi"}).status_code == 422
    assert demo.post("/demo/ask", json={"question": "x" * 501}).status_code == 422
    assert demo.post("/demo/ask", json={}).status_code == 422


def test_per_visitor_limit_returns_429_with_retry_after_and_a_readable_message(demo):
    for _ in range(3):
        assert demo.post("/demo/ask", json=QUESTION, headers=BROWSER).status_code == 200
    blocked = demo.post("/demo/ask", json=QUESTION, headers=BROWSER)
    assert blocked.status_code == 429 and blocked.json()["reason"] == "rate_limited"
    assert 1 <= int(blocked.headers["retry-after"]) <= 60
    assert ("ask", "rate_limited") in kinds()


def test_daily_cap_applies_to_everybody_and_says_when_it_resets(demo, monkeypatch):
    monkeypatch.setenv("DEMO_PER_MINUTE", "100")
    with TestClient(main.app) as c:
        for i in range(5):
            assert c.post("/demo/ask", json=QUESTION, headers={"User-Agent": f"browser-{i}"}).status_code == 200
        capped = c.post("/demo/ask", json=QUESTION, headers={"User-Agent": "someone-else"})
    assert capped.status_code == 429 and capped.json()["reason"] == "daily_cap"
    assert "midnight UTC" in capped.json()["detail"]


def test_provider_failure_gives_a_friendly_message_not_the_providers_text(demo, monkeypatch):
    def broken(*_a, **_k):
        raise ProviderError("Gemini returned 500: internal details with AIza-SECRET")
        yield  # pragma: no cover

    monkeypatch.setattr(main, "answer_stream", broken)
    with demo.stream("POST", "/demo/ask", json=QUESTION, headers=BROWSER) as response:
        events = sse_events(response)
    assert events[0][0] == "sources" and events[-1][0] == "error"
    assert "SECRET" not in events[-1][1]["detail"] and "try again" in events[-1][1]["detail"].lower()
    assert kinds()[-1] == ("ask", "provider_error")


def test_quota_exhaustion_is_reported_as_quota(demo, monkeypatch):
    def quota(*_a, **_k):
        raise ProviderError("Gemini gemini-x asked to wait 480 min (quota exhausted); not waiting.")
        yield  # pragma: no cover

    monkeypatch.setattr(main, "answer_stream", quota)
    with demo.stream("POST", "/demo/ask", json=QUESTION, headers=BROWSER) as response:
        events = sse_events(response)
    assert "usage limit" in events[-1][1]["detail"] and kinds()[-1] == ("ask", "quota")


def test_retrieval_failure_is_reported_and_releases_the_slot(demo, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(main, "search", boom)
    for _ in range(3):  # more than DEMO_MAX_CONCURRENT=2: a leaked slot would turn the third into "busy"
        with demo.stream("POST", "/demo/ask", json=QUESTION, headers=BROWSER) as response:
            events = sse_events(response)
        assert events[-1][0] == "error" and "search failed" in events[-1][1]["detail"].lower()


def test_busy_when_all_generation_slots_are_taken(demo):
    guard = main.app.state.demo
    assert guard.admit("a") is None and guard.admit("b") is None  # holds both slots
    response = demo.post("/demo/ask", json=QUESTION, headers=BROWSER)
    assert response.status_code == 503 and response.json()["reason"] == "busy"
    guard.release()
    guard.release()


def test_analytics_failure_never_breaks_an_answer(demo, monkeypatch):
    def down():
        raise OSError("analytics db unreachable")

    monkeypatch.setattr(main.app.state.analytics, "_connect", down)
    assert demo.get("/", headers=BROWSER).status_code == 200
    with demo.stream("POST", "/demo/ask", json=QUESTION, headers=BROWSER) as response:
        assert [n for n, _ in sse_events(response)][-1] == "done"


# ---- /stats ----
def test_stats_needs_the_stats_key(demo):
    assert demo.get("/stats").status_code == 401
    assert demo.get("/stats", headers={"X-Stats-Key": "wrong"}).status_code == 401
    ok = demo.get("/stats", headers={"X-Stats-Key": "stats-secret"})
    assert ok.status_code == 200 and ok.json()["demo_daily_limit"] == 5
    assert demo.get("/stats", headers={"Authorization": "Bearer stats-secret"}).status_code == 200


def test_stats_is_refused_when_no_key_is_configured(demo, monkeypatch):
    monkeypatch.setenv("STATS_KEY", "")
    with TestClient(main.app) as c:
        assert c.get("/stats", headers={"X-Stats-Key": ""}).status_code == 503


# ---- units ----
def test_visitor_id_is_stable_within_a_day_and_different_across_days():
    from datetime import date

    a = visitor_id("salt", "1.2.3.4", "ua", date(2026, 11, 1))
    assert a == visitor_id("salt", "1.2.3.4", "ua", date(2026, 11, 1))
    assert a != visitor_id("salt", "1.2.3.4", "ua", date(2026, 11, 2))
    assert a != visitor_id("salt", "1.2.3.5", "ua", date(2026, 11, 1))
    assert "1.2.3.4" not in a


def test_is_bot():
    assert is_bot("") and is_bot("curl/8.1") and is_bot("UptimeRobot/2.0")
    assert not is_bot("Mozilla/5.0 (iPhone; CPU iPhone OS 17) AppleWebKit Safari")


def test_guard_resets_its_count_when_the_utc_day_changes():
    day = ["d1"]
    guard = DemoGuard(daily_limit=1, per_minute=100, max_concurrent=5, today=lambda: day[0])
    assert guard.admit("v") is None
    assert guard.admit("v") == ("daily_cap", None)
    day[0] = "d2"
    assert guard.admit("v") is None


def test_client_ip_uses_the_last_forwarded_entry_only_when_a_proxy_is_trusted():
    from app.demo import client_ip

    class Req:
        client = type("C", (), {"host": "10.0.0.9"})()
        headers = {"x-forwarded-for": "6.6.6.6, 203.0.113.7"}

    assert client_ip(Req, trust_proxy=False) == "10.0.0.9"
    assert client_ip(Req, trust_proxy=True) == "203.0.113.7"


def test_analytics_swallows_errors_and_reconnects():
    calls = []

    class Flaky:
        closed = False

        def execute(self, sql, params=()):
            calls.append(sql.split()[0])
            if len(calls) == 2:
                raise OSError("boom")

    analytics = Analytics(lambda: Flaky())
    analytics.record("v", "page_view", "ok")  # connects, creates the table (call 1), then inserts (call 2 raises)
    analytics.record("v", "page_view", "ok")  # reconnects after the failure
    assert analytics._conn is not None


# ---- the page itself (the CSP forbids inline code, so the markup must not rely on it) ----
def _html():
    return open("app/static/index.html").read()


def test_page_has_no_inline_script_style_or_event_handlers():
    import re

    html = _html()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline <script>"
    assert "<style" not in html and not re.search(r"\sstyle=", html)
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"


def test_every_in_page_link_has_a_target():
    import re

    html = _html()
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    for target in re.findall(r'href="#([^"]+)"', html):
        assert target in ids, target


def test_external_links_are_https_and_do_not_leak_the_opener():
    import re

    for tag in re.findall(r"<a [^>]*>", _html()):
        href = re.search(r'href="([^"]+)"', tag).group(1)
        if href.startswith("http"):
            assert href.startswith("https://") and "noopener" in tag and "noreferrer" in tag, tag


def test_logo_and_assets_are_served(demo):
    logo = demo.get("/static/logo.svg")
    assert logo.status_code == 200 and logo.headers["content-type"].startswith("image/svg+xml")
    assert demo.get("/static/style.css").status_code == 200
