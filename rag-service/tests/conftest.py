import pytest

# Tests must be hermetic: never read the developer's real .env (it holds live keys and model choices)
# and never make a real model call. Each test sets exactly the env it needs.
LEAKY_VARS = [
    "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GEMINI_FREE_TIER", "GEMINI_MIN_INTERVAL_S",
    "LLM_PROVIDER", "LLM_MODEL", "LLM_EFFORT", "JUDGE_PROVIDER", "JUDGE_MODEL",
    "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "API_KEYS", "RATE_LIMIT_PER_MINUTE",
    "DEMO_ENABLED", "DEMO_DAILY_LIMIT", "DEMO_PER_MINUTE", "DEMO_MAX_CONCURRENT", "TRUST_PROXY_HEADERS", "STATS_KEY", "ANALYTICS_SALT",
]


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch):
    monkeypatch.setattr("app.config._load_dotenv", lambda *args, **kwargs: None)
    for var in LEAKY_VARS:
        monkeypatch.delenv(var, raising=False)
