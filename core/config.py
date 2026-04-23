"""
core/config.py — Production Configuration  (FIXED)
===================================================
Fix: CelerySettings.DEFAULT_QUEUE changed from "tts.default" → "celery"
     to match task_default_queue in celery_app.py.
     The mismatch caused get_queue_length() to read the wrong Redis key,
     always returning 0 even when jobs were backed up.
"""
from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    DEV     = "dev"
    STAGING = "staging"
    PROD    = "prod"


class RedisSettings(BaseSettings):
    URL:              str = "redis://localhost:6379/0"
    RESULT_BACKEND:   str = "redis://localhost:6379/1"
    MAX_CONNECTIONS:  int = 50
    SOCKET_TIMEOUT:   int = 5
    SOCKET_CONNECT_TIMEOUT: int = 5
    RETRY_ON_TIMEOUT: bool = True
    HEALTH_CHECK_INTERVAL: int = 30
    # Circuit breaker
    CB_FAILURE_THRESHOLD: int = 5
    CB_RECOVERY_TIMEOUT:  int = 30

    model_config = SettingsConfigDict(env_prefix="REDIS_", extra="ignore")


class CelerySettings(BaseSettings):
    BROKER_URL:     str = "redis://redis:6379/0"
    RESULT_BACKEND: str = "redis://redis:6379/1"
    TASK_SOFT_TIME_LIMIT: int = 300   # raised from 120 to match worker
    TASK_HARD_TIME_LIMIT: int = 360   # raised from 150 to match worker
    WORKER_CONCURRENCY:  int = 1
    WORKER_MAX_RAM_MB:   int = 4000
    WORKER_MAX_TASKS_PER_CHILD: int = 20
    PREFETCH_MULTIPLIER: int = 1
    TASK_ACKS_LATE:      bool = True
    # Retry policy
    MAX_RETRIES:         int = 3
    RETRY_BACKOFF:       bool = True
    RETRY_BACKOFF_MAX:   int = 60
    # FIX: was "tts.default" — must match task_default_queue in celery_app.py
    DEFAULT_QUEUE: str = "celery"
    HIGH_QUEUE:    str = "celery"   # simplify to one queue for now
    LOW_QUEUE:     str = "celery"

    model_config = SettingsConfigDict(env_prefix="CELERY_", extra="ignore")


class StorageSettings(BaseSettings):
    BACKEND:          str  = "local"
    LOCAL_TMP_DIR:    Path = Path("/tmp/tts_audio")
    AUDIO_TTL_SECONDS: int = 600
    ENDPOINT_URL:     str  = "http://localhost:9000"
    ACCESS_KEY:       str  = "minioadmin"
    SECRET_KEY:       str  = "minioadmin"
    BUCKET_NAME:      str  = "tts-audio"
    REGION:           str  = "us-east-1"
    PUBLIC_URL_TTL:   int  = 3600

    model_config = SettingsConfigDict(env_prefix="STORAGE_", extra="ignore")

    def model_post_init(self, __context: object) -> None:
        if self.BACKEND == "local":
            self.LOCAL_TMP_DIR.mkdir(parents=True, exist_ok=True)


class RateLimitSettings(BaseSettings):
    ANONYMOUS_RPM:  int   = 10
    FREE_RPM:       int   = 30
    PRO_RPM:        int   = 120
    ENTERPRISE_RPM: int   = 600
    BURST_MULTIPLIER: float = 2.0
    WINDOW_SECONDS:   int = 60
    FALLBACK_RPM:     int = 5

    model_config = SettingsConfigDict(env_prefix="RATE_", extra="ignore")


class SecuritySettings(BaseSettings):
    API_KEY_HEADER:     str  = "X-API-Key"
    API_KEYS_ENABLED:   bool = False
    JWT_ENABLED:        bool = False
    JWT_SECRET_KEY:     str  = "change-me-in-production"
    JWT_ALGORITHM:      str  = "HS256"
    JWT_EXPIRE_MINUTES: int  = 60
    CORS_ORIGINS: List[str] = [
        "http://localhost:3000",
        "http://localhost:8501",
    ]
    CORS_ALLOW_CREDENTIALS: bool = True
    HSTS_MAX_AGE: int  = 31536000
    CSP_ENABLED:  bool = False

    model_config = SettingsConfigDict(env_prefix="SECURITY_", extra="ignore")


class ObservabilitySettings(BaseSettings):
    METRICS_ENABLED: bool = True
    METRICS_PATH:    str  = "/metrics"
    TRACING_ENABLED: bool = False
    OTLP_ENDPOINT:   str  = "http://localhost:4317"
    SERVICE_NAME:    str  = "piper-tts-api"
    LOG_LEVEL:       str  = "INFO"
    LOG_FORMAT:      str  = "json"
    LOG_REQUEST_BODY: bool = False

    model_config = SettingsConfigDict(env_prefix="OBSERVABILITY_", extra="ignore")


class Settings(BaseSettings):
    APP_NAME:    str = "Piper TTS API"
    APP_VERSION: str = "3.0.0"
    ENV:         Environment = Environment.DEV
    DEBUG:       bool = False
    
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    AUDIO_TTL_SECONDS: int = 600  # 10 minutes
    MODELS_DIR:      Path = Path("./piper_models")
    MAX_TEXT_LENGTH: int  = 10_000
    DEVICE:          str  = "cpu"

    HF_BASE_URL: str = (
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
    )

    redis:         RedisSettings         = Field(default_factory=RedisSettings)
    celery:        CelerySettings        = Field(default_factory=CelerySettings)
    storage:       StorageSettings       = Field(default_factory=StorageSettings)
    rate_limit:    RateLimitSettings     = Field(default_factory=RateLimitSettings)
    security:      SecuritySettings      = Field(default_factory=SecuritySettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    def model_post_init(self, __context: object) -> None:
        self.MODELS_DIR.mkdir(parents=True, exist_ok=True)

    @property
    def is_prod(self) -> bool:
        return self.ENV == Environment.PROD

    @property
    def is_dev(self) -> bool:
        return self.ENV == Environment.DEV


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()