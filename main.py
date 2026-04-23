"""
main.py — Production FastAPI Application
=========================================
Clean middleware stack:
  1. ObservabilityMiddleware (request_id, trace_id, metrics)
  2. RateLimitMiddleware (per-tier, Redis + fallback)
  3. CORSMiddleware
  4. Security headers
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.middleware.rate_limit import RateLimitMiddleware
from api.routes.tts import router as tts_router
from core.config import get_settings
from core.logging import configure_logging
from core.redis_client import close_async_redis, get_async_redis
from observability.metrics import ObservabilityMiddleware, setup_metrics, update_queue_depth

# Setup logging first
_cfg = get_settings()
configure_logging(
    level=_cfg.observability.LOG_LEVEL,
    fmt=_cfg.observability.LOG_FORMAT,
)
logger = logging.getLogger(__name__)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info(f"Starting {_cfg.APP_NAME} v{_cfg.APP_VERSION} [{_cfg.ENV.value}]")

    # Redis warm-up
    try:
        r = await get_async_redis()
        await r.ping()
        logger.info("Redis connected.")
    except Exception as exc:
        logger.error(f"Redis connection failed at startup: {exc}")

    # Storage warm-up
    try:
        from storage.backend import get_storage
        get_storage()
        logger.info(f"Storage backend ready: {_cfg.storage.BACKEND}")
    except Exception as exc:
        logger.error(f"Storage backend failed: {exc}")

    # Background task: update queue depth metric every 10s
    stop_event = asyncio.Event()

    async def _metrics_updater():
        while not stop_event.is_set():
            await update_queue_depth(app)
            await asyncio.sleep(10)

    if _cfg.observability.METRICS_ENABLED:
        task = asyncio.create_task(_metrics_updater())

    yield

    # Shutdown
    stop_event.set()
    if _cfg.observability.METRICS_ENABLED:
        task.cancel()

    await close_async_redis()
    logger.info("Shutdown complete.")


# ── App factory ───────────────────────────────────────────────────────────────

def create_app() -> FastAPI:
    app = FastAPI(
        title=_cfg.APP_NAME,
        version=_cfg.APP_VERSION,
        description=(
            "Production-grade Piper TTS API\n\n"
            "## Flow\n"
            "1. `POST /generate-audio` — queue job, receive `job_id`\n"
            "2. `GET /jobs/{job_id}` — poll until `status == completed`\n"
            "3. `GET /download/{job_id}` — stream WAV to client\n\n"
            "Audio files auto-expire after TTL (default 10 minutes)."
        ),
        lifespan=lifespan,
        docs_url="/docs"  if not _cfg.is_prod else None,
        redoc_url="/redoc" if not _cfg.is_prod else None,
    )

    # ── Observability (must be first middleware) ───────────────
    app.add_middleware(ObservabilityMiddleware)

    # ── CORS ───────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cfg.security.CORS_ORIGINS,
        allow_credentials=_cfg.security.CORS_ALLOW_CREDENTIALS,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*", _cfg.security.API_KEY_HEADER],
        expose_headers=[
            "Content-Disposition", "X-Job-Id",
            "X-Request-ID", "X-Trace-ID", "X-Process-Time",
            "X-RateLimit-Limit", "X-RateLimit-Tier",
        ],
    )

    # ── Rate limiting ──────────────────────────────────────────
    app.add_middleware(RateLimitMiddleware)

    # ── Prometheus metrics ─────────────────────────────────────
    if _cfg.observability.METRICS_ENABLED:
        setup_metrics(app)

    # ── Security headers middleware ────────────────────────────
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]    = "nosniff"
        response.headers["X-Frame-Options"]           = "DENY"
        response.headers["X-XSS-Protection"]          = "1; mode=block"
        response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
        if _cfg.is_prod:
            response.headers["Strict-Transport-Security"] = (
                f"max-age={_cfg.security.HSTS_MAX_AGE}; includeSubDomains"
            )
        return response

    # ── Routes ─────────────────────────────────────────────────
    @app.get("/", tags=["System"])
    async def root():
        return {
            "service": _cfg.APP_NAME,
            "version": _cfg.APP_VERSION,
            "env":     _cfg.ENV.value,
            "docs":    "/docs",
            "health":  "/health",
            "metrics": "/metrics",
        }

    app.include_router(tts_router)

    # ── Global error handler ───────────────────────────────────
    @app.exception_handler(Exception)
    async def global_handler(request: Request, exc: Exception):
        logger.error(
            f"Unhandled exception: {exc}",
            exc_info=True,
            extra={"path": request.url.path},
        )
        return JSONResponse(
            status_code=500,
            content={"status": "error", "detail": "Internal server error."},
        )

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=_cfg.HOST,
        port=_cfg.PORT,
        reload=_cfg.is_dev,
        workers=1,
        loop="uvloop",
        http="httptools",
        log_config=None,   # We handle logging ourselves
    )
