import os
from dataclasses import dataclass
from pathlib import Path

REPO_ENV = Path(__file__).resolve().parents[2] / ".env"


def _load_dotenv(path: Path = REPO_ENV) -> None:
    """Read KEY=VALUE lines from the repo-root .env (gitignored) without overriding the real environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    database_url: str
    embed_model: str
    embed_dim: int
    rerank_model: str
    llm_provider: str  # "anthropic" | "gemini"
    llm_model: str
    llm_effort: str
    judge_provider: str
    judge_model: str
    anthropic_api_key: str
    gemini_api_key: str
    gemini_free_tier: bool
    gemini_min_interval_s: float
    top_k: int = 6
    api_keys: str = ""  # "name:secret,name2:secret2"; the gateway refuses every request when empty
    rate_limit_per_minute: int = 20
    # Public demo page (off unless DEMO_ENABLED=1): anonymous visitors, so it has its own limits and never uses API keys.
    demo_enabled: bool = False
    demo_daily_limit: int = 150  # answers per UTC day across all visitors; protects the free-tier quota
    demo_per_minute: int = 4  # per visitor
    demo_max_concurrent: int = 2  # answers being generated at once; the free tier is slow, so extra visitors are told to retry
    trust_proxy_headers: bool = False  # true only behind a proxy you control: the client IP is then the last X-Forwarded-For entry
    stats_key: str = ""  # secret for GET /stats; empty means /stats is refused
    analytics_salt: str = ""  # keeps visitor hashes stable across restarts; empty means a random salt per process


def load_settings() -> Settings:
    _load_dotenv()
    return Settings(
        database_url=os.getenv("DATABASE_URL", "postgresql://compass:compass@localhost:5433/compass"),
        embed_model=os.getenv("EMBED_MODEL", "BAAI/bge-base-en-v1.5"),
        embed_dim=int(os.getenv("EMBED_DIM", "768")),
        rerank_model=os.getenv("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2"),
        llm_provider=os.getenv("LLM_PROVIDER", "anthropic"),
        llm_model=os.getenv("LLM_MODEL", "claude-haiku-4-5"),
        llm_effort=os.getenv("LLM_EFFORT", ""),
        judge_provider=os.getenv("JUDGE_PROVIDER", os.getenv("LLM_PROVIDER", "anthropic")),
        judge_model=os.getenv("JUDGE_MODEL", ""),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_free_tier=os.getenv("GEMINI_FREE_TIER", "1") == "1",
        gemini_min_interval_s=float(os.getenv("GEMINI_MIN_INTERVAL_S", "6.5")),
        api_keys=os.getenv("API_KEYS", ""),
        rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "20")),
        demo_enabled=os.getenv("DEMO_ENABLED", "0") == "1",
        demo_daily_limit=int(os.getenv("DEMO_DAILY_LIMIT", "150")),
        demo_per_minute=int(os.getenv("DEMO_PER_MINUTE", "4")),
        demo_max_concurrent=int(os.getenv("DEMO_MAX_CONCURRENT", "2")),
        trust_proxy_headers=os.getenv("TRUST_PROXY_HEADERS", "0") == "1",
        stats_key=os.getenv("STATS_KEY", ""),
        analytics_salt=os.getenv("ANALYTICS_SALT", ""),
    )
