"""Limits for the public demo page. Anonymous visitors cannot be trusted with the free-tier quota, so three guards:
a per-visitor rate limit, one daily cap for everybody, and a cap on answers generated at the same time.
All three are in memory: one process only, and they reset on restart."""
import threading
from datetime import datetime, timezone

from fastapi import Request

from app.security import RateLimiter


def client_ip(request: Request, trust_proxy: bool) -> str:
    """The caller's address. Behind a proxy we control, the last X-Forwarded-For entry is the one the proxy added;
    earlier entries are whatever the client claimed, so they are never used."""
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded.strip():
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


class DemoGuard:
    def __init__(self, daily_limit: int, per_minute: int, max_concurrent: int, today=lambda: datetime.now(timezone.utc).date()):
        self.daily_limit, self.today = daily_limit, today
        self.limiter = RateLimiter(per_minute)
        self.slots = threading.BoundedSemaphore(max_concurrent)
        self._lock, self._day, self._used = threading.Lock(), today(), 0

    def admit(self, visitor: str) -> tuple[str, float | None] | None:
        """None if the request may proceed (a generation slot is then held: call release() when it ends);
        otherwise (reason, retry_after_seconds_or_None) with reason rate_limited, daily_cap or busy."""
        wait = self.limiter.check(visitor)
        if wait is not None:
            return "rate_limited", wait
        with self._lock:
            if self.today() != self._day:
                self._day, self._used = self.today(), 0
            if self._used >= self.daily_limit:
                return "daily_cap", None
        if not self.slots.acquire(blocking=False):
            return "busy", 10.0
        with self._lock:
            self._used += 1
        return None

    def release(self) -> None:
        self.slots.release()

    @property
    def used_today(self) -> int:
        with self._lock:
            return self._used if self.today() == self._day else 0
