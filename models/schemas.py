"""
models/schemas.py
-----------------
All Pydantic v2 request / response models.
This file is the API contract — shared understanding between
backend and React frontend.
"""
from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class VoiceKey(str, Enum):
    en_US_female_high   = "en_US_female_high"
    en_US_male_high     = "en_US_male_high"
    en_US_female_medium = "en_US_female_medium"
    en_US_male_medium   = "en_US_male_medium"
    en_GB_male_medium   = "en_GB_male_medium"
    en_GB_female_medium = "en_GB_female_medium"


class JobStatus(str, Enum):
    queued     = "queued"
    processing = "processing"
    completed  = "completed"
    failed     = "failed"
    cached     = "cached"      # result served from SHA256 cache


# ── POST /generate-audio ─────────────────────────────────────────────────────

class GenerateAudioRequest(BaseModel):
    text: str = Field(
        ..., min_length=1, max_length=10_000,
        description="Text to synthesise. Markdown stripped automatically.",
    )
    voice: VoiceKey = Field(default=VoiceKey.en_US_female_high)
    speed: float    = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float    = Field(default=0.0, ge=-12.0, le=12.0)
    chunk_words: int = Field(default=30, ge=10, le=60)

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be blank.")
        return v


class GenerateAudioResponse(BaseModel):
    job_id: str
    status: JobStatus = JobStatus.queued
    cache_hit: bool   = False
    message: str      = "Job queued. Poll GET /jobs/{job_id} for status."
    poll_url: str     = ""
    download_url: str = ""   # populated immediately on cache hit


# ── GET /jobs/{job_id} ───────────────────────────────────────────────────────

class JobStatusResponse(BaseModel):
    job_id: str
    status: JobStatus
    progress: int     = Field(default=0, ge=0, le=100)
    download_url: str = ""
    error: Optional[str] = None
    processing_time: Optional[float] = None
    audio_duration:  Optional[float] = None
    rtf: Optional[float] = None
    cache_hit: bool = False
    expires_in: Optional[int] = None   # seconds until audio deleted


# ── GET /download/{job_id} ───────────────────────────────────────────────────
# Returns a StreamingResponse — no Pydantic model needed.


# ── GET /voices ──────────────────────────────────────────────────────────────

class VoiceInfo(BaseModel):
    key: str
    name: str
    gender: str
    locale: str
    quality: str
    description: str


# ── GET /health ──────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    version: str
    redis_connected: bool
    workers_active: int
    queue_length: int


# ── Error envelope ───────────────────────────────────────────────────────────

class ErrorResponse(BaseModel):
    status: str = "error"
    detail: str
    error_code: Optional[str] = None
