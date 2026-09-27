"""In-process sliding-window rate limiter. Correct for the single-process deployment Podium
ships with; a multi-worker deployment should back this with the database or Redis."""

import hashlib
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, Request

from podium.config import Settings, get_settings
from podium.errors import RateLimited

_buckets: dict[tuple[str, str], deque[float]] = defaultdict(deque)
_lock = threading.Lock()


def client_ip(request: Request) -> str:
    """The socket peer. Behind a proxy, uvicorn --proxy-headers has already replaced it with the
    forwarded client, but only for proxies listed in FORWARDED_ALLOW_IPS; a client-sent
    X-Forwarded-For is never trusted here, or one rotating header would dodge every limit."""
    return request.client.host if request.client else "unknown"


def ip_hash(request: Request) -> str:
    return hashlib.sha256(client_ip(request).encode()).hexdigest()[:32]


def check(bucket: str, key: str, limit: int, window_seconds: int) -> None:
    now = time.monotonic()
    with _lock:
        hits = _buckets[(bucket, key)]
        while hits and hits[0] <= now - window_seconds:
            hits.popleft()
        if len(hits) >= limit:
            raise RateLimited(retry_after=max(1, int(hits[0] + window_seconds - now) + 1))
        hits.append(now)


def limiter(bucket: str, limit: int, window_seconds: int = 60):
    def dependency(request: Request, settings: Settings = Depends(get_settings)) -> None:
        if settings.rate_limit_enabled:
            check(bucket, ip_hash(request), limit, window_seconds)

    dependency.rate_limit = (bucket, limit, window_seconds)  # printed by the API reference
    return dependency


def reset() -> None:  # for tests
    with _lock:
        _buckets.clear()
