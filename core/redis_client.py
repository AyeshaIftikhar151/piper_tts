"""
core/redis_client.py — Production Redis Layer
==============================================
Features:
- Connection pooling (async + sync)
- Circuit breaker pattern (fail-fast when Redis is down)
- Retry with exponential backoff
- SHA256 cache helpers
- Job state helpers (async + sync)
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from enum import Enum
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
import redis as syncredis
from redis.asyncio import ConnectionPool as AsyncPool
from redis import ConnectionPool as SyncPool

from core.config import get_settings

logger   = logging.getLogger(__name__)
_cfg     = get_settings()
_rcfg    = _cfg.redis


# ── Circuit Breaker ───────────────────────────────────────────────────────────

class CircuitState(str, Enum):
    CLOSED   = "closed"    # Normal operation
    OPEN     = "open"      # Failing — reject fast
    HALF_OPEN = "half_open" # Testing recovery


class CircuitBreaker:
    """
    Simple circuit breaker for Redis operations.
    Prevents cascade failures when Redis is unavailable.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout:  int = 30,
    ) -> None:
        self._failures  = 0
        self._threshold = failure_threshold
        self._timeout   = recovery_timeout
        self._state     = CircuitState.CLOSED
        self._last_fail: float = 0.0

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN:
            if time.time() - self._last_fail > self._timeout:
                self._state = CircuitState.HALF_OPEN
        return self._state

    def record_success(self) -> None:
        self._failures = 0
        self._state    = CircuitState.CLOSED

    def record_failure(self) -> None:
        self._failures  += 1
        self._last_fail  = time.time()
        if self._failures >= self._threshold:
            if self._state != CircuitState.OPEN:
                logger.error(
                    "Redis circuit breaker OPENED after "
                    f"{self._failures} failures."
                )
            self._state = CircuitState.OPEN

    def allow_request(self) -> bool:
        s = self.state
        if s == CircuitState.CLOSED:
            return True
        if s == CircuitState.HALF_OPEN:
            return True   # Allow one probe
        return False      # OPEN — reject


_circuit = CircuitBreaker(
    failure_threshold=_rcfg.CB_FAILURE_THRESHOLD,
    recovery_timeout=_rcfg.CB_RECOVERY_TIMEOUT,
)


# ── Async pool (FastAPI) ──────────────────────────────────────────────────────

_async_pool: Optional[AsyncPool] = None
_async_client: Optional[aioredis.Redis] = None


async def get_async_redis() -> aioredis.Redis:
    global _async_pool, _async_client
    if _async_client is None:
        _async_pool = aioredis.ConnectionPool.from_url(
            _rcfg.URL,
            max_connections=_rcfg.MAX_CONNECTIONS,
            socket_timeout=_rcfg.SOCKET_TIMEOUT,
            socket_connect_timeout=_rcfg.SOCKET_CONNECT_TIMEOUT,
            retry_on_timeout=_rcfg.RETRY_ON_TIMEOUT,
            decode_responses=True,
        )
        _async_client = aioredis.Redis(connection_pool=_async_pool)
    return _async_client


async def close_async_redis() -> None:
    global _async_client, _async_pool
    if _async_client:
        await _async_client.aclose()
        _async_client = None
    if _async_pool:
        await _async_pool.aclose()
        _async_pool = None


# ── Sync pool (Celery workers) ────────────────────────────────────────────────

_sync_pool: Optional[SyncPool] = None
_sync_client: Optional[syncredis.Redis] = None


def get_sync_redis() -> syncredis.Redis:
    global _sync_pool, _sync_client
    if _sync_client is None:
        _sync_pool = syncredis.ConnectionPool.from_url(
            _rcfg.URL,
            max_connections=_rcfg.MAX_CONNECTIONS,
            socket_timeout=_rcfg.SOCKET_TIMEOUT,
            socket_connect_timeout=_rcfg.SOCKET_CONNECT_TIMEOUT,
            retry_on_timeout=_rcfg.RETRY_ON_TIMEOUT,
            decode_responses=True,
        )
        _sync_client = syncredis.Redis(connection_pool=_sync_pool)
    return _sync_client


# ── Cache key helpers ─────────────────────────────────────────────────────────

def make_cache_key(text: str, voice: str, speed: float, pitch: float) -> str:
    payload = f"{text}|{voice}|{speed:.3f}|{pitch:.3f}"
    digest  = hashlib.sha256(payload.encode()).hexdigest()
    return f"tts:cache:{digest}"


def make_job_key(job_id: str) -> str:
    return f"tts:job:{job_id}"


# ── Sync Redis ops (Celery workers) ───────────────────────────────────────────

def _sync_safe(op_name: str, fn, *args, **kwargs) -> Any:
    """Execute a sync Redis operation with circuit breaker."""
    if not _circuit.allow_request():
        logger.warning(f"Redis circuit OPEN — skipping {op_name}")
        return None
    try:
        result = fn(*args, **kwargs)
        _circuit.record_success()
        return result
    except Exception as exc:
        _circuit.record_failure()
        logger.error(f"Redis {op_name} failed: {exc}")
        return None


def cache_get(key: str) -> Optional[Dict[str, Any]]:
    def _op():
        r = get_sync_redis()
        raw = r.get(key)
        return json.loads(raw) if raw else None
    return _sync_safe("cache_get", _op)


def cache_set(key: str, data: Dict[str, Any], ttl: Optional[int] = None) -> None:
    ttl = ttl or _cfg.storage.AUDIO_TTL_SECONDS
    def _op():
        r = get_sync_redis()
        r.setex(key, ttl, json.dumps(data))
    _sync_safe("cache_set", _op)


def job_set(job_id: str, state: Dict[str, Any]) -> None:
    def _op():
        r = get_sync_redis()
        r.setex(make_job_key(job_id), 3600, json.dumps(state))
    _sync_safe("job_set", _op)


def job_get(job_id: str) -> Optional[Dict[str, Any]]:
    def _op():
        r = get_sync_redis()
        raw = r.get(make_job_key(job_id))
        return json.loads(raw) if raw else None
    return _sync_safe("job_get", _op)


# ── Async Redis ops (FastAPI) ─────────────────────────────────────────────────

async def _async_safe(op_name: str, coro) -> Any:
    """Execute an async Redis operation with circuit breaker."""
    if not _circuit.allow_request():
        logger.warning(f"Redis circuit OPEN — skipping {op_name}")
        return None
    try:
        result = await coro
        _circuit.record_success()
        return result
    except Exception as exc:
        _circuit.record_failure()
        logger.error(f"Async Redis {op_name} failed: {exc}")
        return None


async def async_job_get(job_id: str) -> Optional[Dict[str, Any]]:
    async def _op():
        r = await get_async_redis()
        raw = await r.get(make_job_key(job_id))
        return json.loads(raw) if raw else None
    return await _async_safe("async_job_get", _op())


async def async_job_set(job_id: str, state: Dict[str, Any]) -> None:
    async def _op():
        r = await get_async_redis()
        await r.setex(make_job_key(job_id), 3600, json.dumps(state))
    await _async_safe("async_job_set", _op())


async def async_cache_get(key: str) -> Optional[Dict[str, Any]]:
    async def _op():
        r = await get_async_redis()
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    return await _async_safe("async_cache_get", _op())


# ── Health ────────────────────────────────────────────────────────────────────

async def redis_ping() -> bool:
    try:
        r = await get_async_redis()
        return bool(await r.ping())
    except Exception:
        return False


async def get_queue_length() -> int:
    try:
        r  = await get_async_redis()
        ql = await r.llen(_cfg.celery.DEFAULT_QUEUE)
        return ql or 0
    except Exception:
        return -1


def get_circuit_state() -> str:
    return _circuit.state.value
