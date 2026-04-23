"""
api/routes/tts.py
-----------------
All TTS API endpoints.

React frontend flow
-------------------
1. POST /generate-audio  →  { job_id, poll_url }
2. GET  /jobs/{job_id}   →  poll until status == "completed"
3. GET  /download/{job_id}  →  streaming WAV response (triggers browser download)

Cache hit flow (same request within 1 hour):
1. POST /generate-audio  →  { job_id, status="cached", download_url }
   (download_url is populated immediately — no polling needed)
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from core.config import Settings, get_settings
from core.redis_client import (
    async_cache_get,
    async_job_get,
    async_job_set,
    get_queue_length,
    make_cache_key,
    redis_ping,
)
from models.schemas import (
    ErrorResponse,
    GenerateAudioRequest,
    GenerateAudioResponse,
    HealthResponse,
    JobStatus,
    JobStatusResponse,
    VoiceInfo,
)
from worker.celery_app import synthesize as celery_synthesize

logger     = logging.getLogger(__name__)
router     = APIRouter()
SettingsDep = Annotated[Settings, Depends(get_settings)]

VOICE_METADATA = {
    "en_US_female_high":   {"name": "Lessac", "gender": "female", "locale": "en_US", "quality": "high",   "description": "US · Warm & Clear"},
    "en_US_male_high":     {"name": "Ryan",   "gender": "male",   "locale": "en_US", "quality": "high",   "description": "US · Natural"},
    "en_US_female_medium": {"name": "Amy",    "gender": "female", "locale": "en_US", "quality": "medium", "description": "US · Bright"},
    "en_US_male_medium":   {"name": "Joe",    "gender": "male",   "locale": "en_US", "quality": "medium", "description": "US · Neutral"},
    "en_GB_male_medium":   {"name": "Alan",   "gender": "male",   "locale": "en_GB", "quality": "medium", "description": "British · Deep"},
    "en_GB_female_medium": {"name": "Jenny",  "gender": "female", "locale": "en_GB", "quality": "medium", "description": "British · Soft"},
}


def _build_download_url(request: Request, job_id: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/download/{job_id}"


def _build_poll_url(request: Request, job_id: str) -> str:
    base = str(request.base_url).rstrip("/")
    return f"{base}/jobs/{job_id}"


# ── GET /health ───────────────────────────────────────────────────────────────

@router.get("/health", response_model=HealthResponse, tags=["System"])
async def health(settings: SettingsDep) -> HealthResponse:
    redis_ok    = await redis_ping()
    queue_len   = await get_queue_length()
    return HealthResponse(
        status="ok" if redis_ok else "degraded",
        version=settings.APP_VERSION,
        redis_connected=redis_ok,
        workers_active=-1,      # Celery inspect is expensive; omit here
        queue_length=queue_len,
    )


# ── GET /voices ───────────────────────────────────────────────────────────────

@router.get("/voices", response_model=list[VoiceInfo], tags=["TTS"])
async def list_voices() -> list[VoiceInfo]:
    return [VoiceInfo(key=k, **v) for k, v in VOICE_METADATA.items()]


# ── POST /generate-audio ──────────────────────────────────────────────────────

@router.post(
    "/generate-audio",
    response_model=GenerateAudioResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["TTS"],
    summary="Queue a TTS synthesis job",
    responses={429: {"description": "Too many requests"}, 500: {"model": ErrorResponse}},
)
async def generate_audio(
    body: GenerateAudioRequest,
    request: Request,
    settings: SettingsDep,
) -> GenerateAudioResponse:
    """
    Queue a synthesis job. Returns immediately with a job_id.

    - If the exact same request was made within the last hour, returns
      a cache hit with the download_url populated immediately.
    - Otherwise, queues the job and returns a poll_url.

    React should poll GET /jobs/{job_id} every 2 seconds until
    status == "completed", then call GET /download/{job_id}.
    """
    job_id    = uuid.uuid4().hex
    cache_key = make_cache_key(
        body.text, body.voice.value, body.speed, body.pitch
    )

    # ── Cache check (async) ───────────────────────────────────────
    cached = await async_cache_get(cache_key)
    if cached:
        audio_path = Path(cached["audio_path"])
        if audio_path.exists():
            # Instant response — no worker needed
            await async_job_set(job_id, {
                "status":       "completed",
                "progress":     100,
                "audio_path":   str(audio_path),
                "audio_duration": cached["audio_duration"],
                "rtf":          cached["rtf"],
                "processing_time": 0.0,
                "cache_hit":    True,
                "created_at":   time.time(),
            })
            logger.info(f"Cache HIT: job_id={job_id}")
            return GenerateAudioResponse(
                job_id=job_id,
                status=JobStatus.cached,
                cache_hit=True,
                message="Cache hit — audio ready immediately.",
                download_url=_build_download_url(request, job_id),
                poll_url=_build_poll_url(request, job_id),
            )

    # ── Enqueue Celery task ───────────────────────────────────────
    await async_job_set(job_id, {"status": "queued", "progress": 0})

    try:
        celery_synthesize.apply_async(
    args=[
        job_id,
        body.text,
        body.voice.value,
        body.speed,
        body.pitch,
        body.chunk_words,
        cache_key,
    ],
    task_id=job_id,
    queue="celery",   # explicit safety
)
    except Exception as exc:
        logger.error(f"Failed to enqueue job {job_id}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to queue synthesis job.")

    logger.info(
        f"Job queued: job_id={job_id} voice={body.voice.value} chars={len(body.text)}"
    )
    return GenerateAudioResponse(
        job_id=job_id,
        status=JobStatus.queued,
        cache_hit=False,
        message="Job queued. Poll /jobs/{job_id} for status.",
        poll_url=_build_poll_url(request, job_id),
    )


# ── GET /jobs/{job_id} ────────────────────────────────────────────────────────

@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    tags=["TTS"],
    summary="Poll job status",
)
async def get_job_status(
    job_id: str,
    request: Request,
    settings: SettingsDep,
) -> JobStatusResponse:
    """
    Poll this endpoint every 1–3 seconds after POST /generate-audio.
    When status == "completed", call GET /download/{job_id}.
    """
    state = await async_job_get(job_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found or has expired.",
        )

    job_status = JobStatus(state.get("status", "queued"))
    download_url = ""

    if job_status == JobStatus.completed:
        # Always build download_url — the /download endpoint handles
        # file-not-found with 410. Checking path.exists() here breaks
        # when API and worker run in separate containers (different /tmp).
        download_url = _build_download_url(request, job_id)

    # Calculate TTL remaining
    expires_in = None
    if "created_at" in state:
        age = time.time() - state["created_at"]
        remaining = settings.AUDIO_TTL_SECONDS - age
        expires_in = max(0, int(remaining))

    return JobStatusResponse(
        job_id=job_id,
        status=job_status,
        progress=state.get("progress", 0),
        download_url=download_url,
        error=state.get("error"),
        processing_time=state.get("processing_time"),
        audio_duration=state.get("audio_duration"),
        rtf=state.get("rtf"),
        cache_hit=state.get("cache_hit", False),
        expires_in=expires_in,
    )


# ── GET /download/{job_id} ────────────────────────────────────────────────────

@router.get(
    "/download/{job_id}",
    tags=["TTS"],
    summary="Stream audio file to client",
    responses={
        200: {"content": {"audio/wav": {}}},
        404: {"description": "Job not found or audio expired"},
    },
)
async def download_audio(job_id: str) -> StreamingResponse:
    """
    Stream the generated WAV file directly to the client.

    - The file is streamed in 64 KB chunks — no full-file buffering in memory.
    - Content-Disposition triggers a browser download.
    - File is NOT deleted on download — it auto-expires via the cleanup task.
    - After AUDIO_TTL_SECONDS (default 10 min), the file is gone.
    """
    state = await async_job_get(job_id)
    if state is None:
        raise HTTPException(
            status_code=404,
            detail="Job not found or has expired.",
        )

    if state.get("status") != "completed":
        raise HTTPException(
            status_code=400,
            detail=f"Job status is '{state.get('status')}' — audio not ready yet.",
        )

    audio_path = Path(state.get("audio_path", ""))
    if not audio_path.exists():
        raise HTTPException(
            status_code=410,  # 410 Gone — specifically for expired resources
            detail="Audio file has expired. Please re-generate.",
        )

    def _iter_file(path: Path, chunk_size: int = 65_536):
        """Generator that yields file in chunks — memory efficient."""
        with open(path, "rb") as f:
            while chunk := f.read(chunk_size):
                yield chunk

    file_size = audio_path.stat().st_size
    return StreamingResponse(
        content=_iter_file(audio_path),
        media_type="audio/wav",
        headers={
            "Content-Disposition": f'attachment; filename="{job_id}.wav"',
            "Content-Length": str(file_size),
            "Cache-Control": "private, no-store",  # never cache audio in CDN
            "X-Job-Id": job_id,
        },
    )
