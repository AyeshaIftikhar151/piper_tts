"""
observability/metrics.py — Prometheus Metrics + Request Tracing
================================================================
Metrics tracked:
- api_request_duration_seconds (histogram)
- api_requests_total (counter by status/endpoint)
- tts_job_duration_seconds (histogram by voice/status)
- tts_queue_depth (gauge)
- tts_cache_hits_total / tts_cache_misses_total
- tts_worker_active_tasks (gauge)
- redis_circuit_state (gauge)

Run Prometheus at http://localhost:9090
Grafana dashboard at http://localhost:3000
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import Match

logger = logging.getLogger(__name__)


# ── Prometheus metrics ────────────────────────────────────────────────────────

def setup_metrics(app: FastAPI) -> None:
    """
    Add Prometheus metrics to a FastAPI app.
    Requires: pip install prometheus-client
    """
    try:
        from prometheus_client import (
            Counter, Gauge, Histogram, REGISTRY,
            generate_latest, CONTENT_TYPE_LATEST,
        )

        # ── Metric definitions ────────────────────────────────
        REQUEST_DURATION = Histogram(
            "api_request_duration_seconds",
            "API request latency",
            ["method", "endpoint", "status_code"],
            buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
        )

        REQUEST_COUNT = Counter(
            "api_requests_total",
            "Total API requests",
            ["method", "endpoint", "status_code"],
        )

        TTS_JOB_DURATION = Histogram(
            "tts_job_duration_seconds",
            "TTS synthesis duration",
            ["voice", "status"],
            buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0],
        )

        TTS_RTF = Histogram(
            "tts_rtf",
            "Real-Time Factor per synthesis job",
            ["voice"],
            buckets=[0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0],
        )

        QUEUE_DEPTH = Gauge(
            "tts_queue_depth",
            "Number of jobs waiting in synthesis queue",
        )

        CACHE_HITS = Counter(
            "tts_cache_hits_total",
            "Synthesis cache hits (SHA256)",
        )

        CACHE_MISSES = Counter(
            "tts_cache_misses_total",
            "Synthesis cache misses",
        )

        ACTIVE_WORKERS = Gauge(
            "tts_worker_active_tasks",
            "Number of currently active Celery worker tasks",
        )

        CIRCUIT_STATE = Gauge(
            "redis_circuit_breaker_open",
            "1 if Redis circuit breaker is OPEN, 0 if CLOSED",
        )

        ERROR_COUNT = Counter(
            "api_errors_total",
            "Total API errors",
            ["endpoint", "error_code"],
        )

        # Store references on app state for use in routes
        app.state.metrics = {
            "request_duration": REQUEST_DURATION,
            "request_count":    REQUEST_COUNT,
            "tts_job_duration": TTS_JOB_DURATION,
            "tts_rtf":          TTS_RTF,
            "queue_depth":      QUEUE_DEPTH,
            "cache_hits":       CACHE_HITS,
            "cache_misses":     CACHE_MISSES,
            "active_workers":   ACTIVE_WORKERS,
            "circuit_state":    CIRCUIT_STATE,
            "error_count":      ERROR_COUNT,
        }

        # ── /metrics endpoint ─────────────────────────────────
        from core.config import get_settings
        cfg = get_settings()

        @app.get(cfg.observability.METRICS_PATH, include_in_schema=False)
        async def metrics_endpoint():
            from fastapi.responses import Response as FR
            return FR(
                content=generate_latest(REGISTRY),
                media_type=CONTENT_TYPE_LATEST,
            )

        logger.info("Prometheus metrics enabled at /metrics")

    except ImportError:
        logger.warning(
            "prometheus-client not installed — metrics disabled. "
            "Run: pip install prometheus-client"
        )


# ── Request timing middleware ─────────────────────────────────────────────────

class ObservabilityMiddleware(BaseHTTPMiddleware):
    """
    Injects request_id + trace_id into every request context.
    Records request duration in Prometheus.
    Adds X-Request-ID and X-Trace-ID to all responses.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        from core.logging import set_request_id, set_trace_id

        # Generate or propagate IDs
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
        trace_id   = request.headers.get("X-Trace-ID",   str(uuid.uuid4()))

        set_request_id(request_id)
        set_trace_id(trace_id)

        # Store on request state for route handlers
        request.state.request_id = request_id
        request.state.trace_id   = trace_id

        start = time.perf_counter()

        response = await call_next(request)

        duration = time.perf_counter() - start
        endpoint = _get_route_path(request)
        status   = str(response.status_code)

        # Add IDs to response headers
        response.headers["X-Request-ID"]   = request_id
        response.headers["X-Trace-ID"]     = trace_id
        response.headers["X-Process-Time"] = f"{duration:.4f}"

        # Record in Prometheus if available
        try:
            metrics = getattr(request.app.state, "metrics", None)
            if metrics:
                metrics["request_duration"].labels(
                    method=request.method,
                    endpoint=endpoint,
                    status_code=status,
                ).observe(duration)
                metrics["request_count"].labels(
                    method=request.method,
                    endpoint=endpoint,
                    status_code=status,
                ).inc()
        except Exception:
            pass

        return response


def _get_route_path(request: Request) -> str:
    """Extract the route template path (e.g. /jobs/{job_id})."""
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", request.url.path)
    return request.url.path


# ── Helper functions for route handlers ───────────────────────────────────────

def record_tts_job(
    app: FastAPI,
    voice: str,
    duration: float,
    rtf: float,
    status: str = "completed",
    cache_hit: bool = False,
) -> None:
    """Call from route handler after synthesis completes."""
    try:
        m = getattr(app.state, "metrics", None)
        if not m:
            return
        m["tts_job_duration"].labels(voice=voice, status=status).observe(duration)
        m["tts_rtf"].labels(voice=voice).observe(rtf)
        if cache_hit:
            m["cache_hits"].inc()
        else:
            m["cache_misses"].inc()
    except Exception:
        pass


async def update_queue_depth(app: FastAPI) -> None:
    """Update queue depth gauge. Call periodically from background task."""
    try:
        from core.redis_client import get_queue_length
        depth = await get_queue_length()
        m = getattr(app.state, "metrics", None)
        if m and depth >= 0:
            m["queue_depth"].set(depth)
    except Exception:
        pass


def update_circuit_state(app: FastAPI) -> None:
    """Update Redis circuit breaker gauge."""
    try:
        from core.redis_client import get_circuit_state
        m = getattr(app.state, "metrics", None)
        if m:
            is_open = 1 if get_circuit_state() == "open" else 0
            m["circuit_state"].set(is_open)
    except Exception:
        pass
