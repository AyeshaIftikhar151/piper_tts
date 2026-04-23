"""
api/middleware/rate_limit.py — Production Rate Limiter
=======================================================
Features:
- Proxy-aware IP extraction (X-Forwarded-For support)
- Per-tier limits (anonymous / free / pro / enterprise)
- API key rate limiting
- In-memory fallback when Redis is unavailable
- Sliding window algorithm
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Dict, Optional

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core.config import get_settings

logger = logging.getLogger(__name__)
_cfg   = get_settings()
_rlcfg = _cfg.rate_limit

# Endpoints that should NOT be rate-limited
_SKIP_PATHS = {"/health", "/metrics", "/docs", "/redoc", "/openapi.json"}

# ── In-memory fallback limiter ────────────────────────────────────────────────

class InMemoryLimiter:
    """
    Thread-safe sliding-window rate limiter using deques.
    Used as fallback when Redis is unavailable.
    """

    def __init__(self) -> None:
        self._windows: Dict[str, deque] = defaultdict(deque)
        self._lock    = Lock()

    def is_allowed(self, key: str, limit: int, window: int) -> bool:
        now  = time.time()
        cutoff = now - window
        with self._lock:
            dq = self._windows[key]
            while dq and dq[0] < cutoff:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True


_fallback_limiter = InMemoryLimiter()


# ── IP extraction ─────────────────────────────────────────────────────────────

def get_client_ip(request: Request) -> str:
    """
    Extract the real client IP, handling proxies.
    Checks X-Forwarded-For first (set by Nginx), falls back to direct IP.
    """
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        # X-Forwarded-For: client, proxy1, proxy2
        # Take the leftmost (original client) IP
        return xff.split(",")[0].strip()

    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()

    return request.client.host if request.client else "unknown"


# ── Tier detection ────────────────────────────────────────────────────────────

def get_client_tier(request: Request) -> tuple[str, int]:
    """
    Returns (tier_name, requests_per_minute) based on API key or IP.
    Extend this to look up keys from a database in production.
    """
    api_key = request.headers.get(_cfg.security.API_KEY_HEADER, "")

    if not api_key:
        return "anonymous", _rlcfg.ANONYMOUS_RPM

    # In production: look up key in Redis/DB and return tier
    # For now: simple prefix-based tiers
    if api_key.startswith("ent_"):
        return "enterprise", _rlcfg.ENTERPRISE_RPM
    if api_key.startswith("pro_"):
        return "pro", _rlcfg.PRO_RPM
    if api_key.startswith("free_"):
        return "free", _rlcfg.FREE_RPM

    return "anonymous", _rlcfg.ANONYMOUS_RPM


# ── Middleware ────────────────────────────────────────────────────────────────

class RateLimitMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip non-rate-limited paths
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        # Only rate-limit synthesis endpoint
        if request.url.path != "/generate-audio":
            return await call_next(request)

        client_ip       = get_client_ip(request)
        tier, rpm_limit = get_client_tier(request)
        window          = _rlcfg.WINDOW_SECONDS
        burst           = int(rpm_limit * _rlcfg.BURST_MULTIPLIER)

        # Try Redis first, fall back to in-memory
        allowed = await self._check_redis(client_ip, rpm_limit, burst, window)
        if allowed is None:
            # Redis unavailable — use in-memory fallback
            allowed = _fallback_limiter.is_allowed(
                f"fallback:{client_ip}",
                _rlcfg.FALLBACK_RPM,
                window,
            )
            logger.warning(
                "Rate limiter using in-memory fallback",
                extra={"client_ip": client_ip},
            )

        if not allowed:
            logger.warning(
                "Rate limit exceeded",
                extra={"client_ip": client_ip, "tier": tier, "rpm": rpm_limit},
            )
            return JSONResponse(
                status_code=429,
                content={
                    "status":     "error",
                    "detail":     f"Rate limit exceeded ({rpm_limit} req/min for {tier} tier).",
                    "error_code": "RATE_LIMITED",
                    "retry_after": window,
                },
                headers={
                    "Retry-After":       str(window),
                    "X-RateLimit-Limit": str(rpm_limit),
                    "X-RateLimit-Tier":  tier,
                },
            )

        # Add rate limit headers to response
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(rpm_limit)
        response.headers["X-RateLimit-Tier"]  = tier
        return response

    async def _check_redis(
        self,
        client_ip: str,
        limit: int,
        burst: int,
        window: int,
    ) -> Optional[bool]:
        """Sliding window rate check in Redis. Returns None if Redis is down."""
        try:
            from core.redis_client import get_async_redis
            r   = await get_async_redis()
            key = f"rate:{client_ip}"
            now = int(time.time() * 1000)

            pipe = r.pipeline()
            pipe.zadd(key, {str(now): now})
            pipe.zremrangebyscore(key, 0, now - window * 1000)
            pipe.zcard(key)
            pipe.expire(key, window)
            results = await pipe.execute()
            count   = results[2]
            return count <= burst
        except Exception as exc:
            logger.error(f"Redis rate limit check failed: {exc}")
            return None   # Signal fallback needed
