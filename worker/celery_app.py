"""
piper
worker/celery_app.py
====================
Fix: _chunk_text() now treats the ENTIRE input as one chunk unless it
exceeds max_words (default 500).

Root cause of the duplication bug
----------------------------------
Every previous version split text on sentence boundaries OR word count.
A typical multi-speaker script line such as:
    "Welcome everyone to today's roundtable. In this discussion we will
     explore how artificial intelligence is shaping the future."
contains TWO sentences → was split into TWO chunks → each chunk was
synthesised separately → the two audio segments were concatenated →
the result sounded like the line was spoken twice.

The correct rule is: one script line = one synthesis call = one WAV.
Splitting should only happen for extremely long passages (500+ words)
that would otherwise time-out or exceed Piper's context.
"""

from __future__ import annotations

import logging
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from celery import Celery

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────

try:
    from core.config import get_settings
    _cfg             = get_settings()
    BROKER_URL       = _cfg.celery.BROKER_URL
    RESULT_BACKEND   = _cfg.celery.RESULT_BACKEND
    SOFT_TIME_LIMIT  = 300
    HARD_TIME_LIMIT  = 360
    MAX_RETRIES      = _cfg.celery.MAX_RETRIES
    MODELS_DIR       = Path(_cfg.MODELS_DIR)
    HF_BASE_URL      = _cfg.HF_BASE_URL
    TMP_DIR          = Path(_cfg.storage.LOCAL_TMP_DIR)
except Exception:
    BROKER_URL       = "redis://redis:6379/0"
    RESULT_BACKEND   = "redis://redis:6379/1"
    SOFT_TIME_LIMIT  = 300
    HARD_TIME_LIMIT  = 360
    MAX_RETRIES      = 3
    MODELS_DIR       = Path("/app/piper_models")
    HF_BASE_URL      = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"
    TMP_DIR          = Path("/tmp/tts_audio")

# ── Celery ─────────────────────────────────────────────────────────

celery_app = Celery("tts_worker")
celery_app.conf.update(
    broker_url=BROKER_URL,
    result_backend=RESULT_BACKEND,
    task_default_queue="celery",
    task_soft_time_limit=SOFT_TIME_LIMIT,
    task_time_limit=HARD_TIME_LIMIT,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    broker_connection_retry_on_startup=True,
    task_track_started=True,
    worker_max_tasks_per_child=20,
)

# ── Voice catalog ───────────────────────────────────────────────────

VOICE_CATALOG: Dict[str, Tuple[str, str]] = {
    "en_US_female_high": (
        "en/en_US/lessac/high/en_US-lessac-high.onnx",
        "en/en_US/lessac/high/en_US-lessac-high.onnx.json",
    ),
    "en_US_male_high": (
        "en/en_US/ryan/high/en_US-ryan-high.onnx",
        "en/en_US/ryan/high/en_US-ryan-high.onnx.json",
    ),
    "en_US_female_medium": (
        "en/en_US/lessac/medium/en_US-lessac-medium.onnx",
        "en/en_US/lessac/medium/en_US-lessac-medium.onnx.json",
    ),
    "en_US_male_medium": (
        "en/en_US/ryan/medium/en_US-ryan-medium.onnx",
        "en/en_US/ryan/medium/en_US-ryan-medium.onnx.json",
    ),
    "en_GB_male_medium": (
        "en/en_GB/alan/medium/en_GB-alan-medium.onnx",
        "en/en_GB/alan/medium/en_GB-alan-medium.onnx.json",
    ),
    "en_GB_female_medium": (
        "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx",
        "en/en_GB/jenny_dioco/medium/en_GB-jenny_dioco-medium.onnx.json",
    ),
}

_LOADED_VOICES: Dict[str, Any] = {}
MIN_MODEL_SIZE = 5_000_000

# ── Model management ────────────────────────────────────────────────

def _download_file(url: str, dest: Path, min_bytes: int = 1) -> None:
    import urllib.request
    for attempt in range(1, 4):
        try:
            urllib.request.urlretrieve(f"{url}?download=true", str(dest))
            if dest.stat().st_size >= min_bytes:
                return
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"File too small after download (min={min_bytes})")
        except Exception as exc:
            logger.warning(f"Download attempt {attempt}/3 failed for {dest.name}: {exc}")
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)


def _ensure_voice_files(voice_key: str) -> Tuple[Path, Path]:
    if voice_key not in VOICE_CATALOG:
        raise ValueError(
            f"Unknown voice key: '{voice_key}'. Valid: {sorted(VOICE_CATALOG)}"
        )
    onnx_rel, json_rel = VOICE_CATALOG[voice_key]
    onnx_path = MODELS_DIR / f"{voice_key}.onnx"
    json_path  = MODELS_DIR / f"{voice_key}.onnx.json"
    MODELS_DIR.mkdir(parents=True, exist_ok=True)

    if not (onnx_path.exists() and onnx_path.stat().st_size >= MIN_MODEL_SIZE):
        if onnx_path.exists():
            onnx_path.unlink()
        logger.info(f"[{voice_key}] Downloading .onnx...")
        _download_file(f"{HF_BASE_URL}/{onnx_rel}", onnx_path, MIN_MODEL_SIZE)

    if not (json_path.exists() and json_path.stat().st_size > 100):
        if json_path.exists():
            json_path.unlink()
        logger.info(f"[{voice_key}] Downloading .json...")
        _download_file(f"{HF_BASE_URL}/{json_rel}", json_path, min_bytes=100)

    return onnx_path, json_path


def _get_voice(voice_key: str) -> Any:
    if voice_key in _LOADED_VOICES:
        return _LOADED_VOICES[voice_key]
    from piper import PiperVoice
    onnx_path, json_path = _ensure_voice_files(voice_key)
    logger.info(f"[{voice_key}] Loading into memory...")
    voice = PiperVoice.load(str(onnx_path), config_path=str(json_path))
    _LOADED_VOICES[voice_key] = voice
    logger.info(f"[{voice_key}] Loaded OK")
    return voice


# ── Text chunking ───────────────────────────────────────────────────

def _chunk_text(text: str, max_words: int = 500) -> List[str]:
    """
    Return the entire text as ONE chunk unless it exceeds max_words.

    Do NOT split on sentence boundaries. A multi-speaker script line
    like "Hello world. How are you?" must stay as one chunk so Piper
    synthesises it in one pass — splitting it produces two audio
    segments that sound like the line is spoken twice.

    Only split when the total word count exceeds max_words (default 500),
    which guards against extremely long single-speaker passages.
    """
    text = text.strip()
    if not text:
        return []

    words = text.split()
    if len(words) <= max_words:
        return [text]           # <-- always one chunk for normal lines

    # Genuine long text: split by word count only
    chunks = []
    for i in range(0, len(words), max_words):
        part = " ".join(words[i: i + max_words])
        if part:
            chunks.append(part)
    return chunks


# ── Synthesis ───────────────────────────────────────────────────────

def _synthesise(
    text: str,
    voice_key: str,
    speed: float,
    pitch: float,
    chunk_words: int,
    tmp_dir: Path,
) -> Tuple[Path, float, float]:
    """
    Synthesise text to a WAV file.
    Returns (final_path, audio_duration_seconds, rtf).
    """
    voice  = _get_voice(voice_key)
    chunks = [c for c in _chunk_text(text, chunk_words) if c.strip()]
    if not chunks:
        raise RuntimeError("Text produced no speakable chunks after splitting.")

    logger.info(
        f"[{voice_key}] synthesising {len(chunks)} chunk(s) "
        f"for: {text[:80]!r}"
    )

    tmp_dir.mkdir(parents=True, exist_ok=True)
    session      = uuid.uuid4().hex
    final_path   = tmp_dir / f"{session}.wav"
    chunk_paths: List[Path] = []

    t0 = time.perf_counter()
    try:
        # Step 1: synthesise each chunk into its own temp WAV
        # (wave.Wave_write.setparams() is called once per file → no error)
        for i, chunk in enumerate(chunks):
            cp = tmp_dir / f"{session}_chunk{i}.wav"
            with wave.open(str(cp), "wb") as wf:
                voice.synthesize_wav(chunk, wf)
            chunk_paths.append(cp)

        elapsed = time.perf_counter() - t0

        # Step 2: merge into final WAV
        if len(chunk_paths) == 1:
            chunk_paths[0].rename(final_path)
            chunk_paths.clear()
        else:
            wav_params  = None
            all_frames: List[bytes] = []
            for cp in chunk_paths:
                with wave.open(str(cp), "rb") as wf:
                    if wav_params is None:
                        wav_params = wf.getparams()
                    all_frames.append(wf.readframes(wf.getnframes()))
            with wave.open(str(final_path), "wb") as out:
                out.setparams(wav_params)
                for frames in all_frames:
                    out.writeframes(frames)

    finally:
        for cp in chunk_paths:
            cp.unlink(missing_ok=True)

    # Step 3: measure real audio duration
    with wave.open(str(final_path), "rb") as wf:
        audio_duration = wf.getnframes() / float(wf.getframerate())

    rtf = elapsed / audio_duration if audio_duration > 0 else 0.0
    return final_path, audio_duration, rtf


# ── Redis job-state helpers ─────────────────────────────────────────

def _update_job(job_id: str, state: dict) -> None:
    try:
        from core.redis_client import job_set
        job_set(job_id, state)
    except Exception as exc:
        logger.warning(f"job_set failed ({exc}), using raw redis fallback")
        try:
            import json
            import redis as _r
            r = _r.from_url(BROKER_URL, decode_responses=True)
            r.setex(f"tts:job:{job_id}", 3600, json.dumps(state))
        except Exception as exc2:
            logger.error(f"Raw redis fallback also failed: {exc2}")


# ── Celery task ─────────────────────────────────────────────────────

@celery_app.task(bind=True, max_retries=MAX_RETRIES)
def synthesize(
    self,
    job_id: str,
    text: str,
    voice_key: str,
    speed: float,
    pitch: float,
    chunk_words: int = 500,
    cache_key: str = "",
) -> dict:
    logger.info(f"[{job_id}] synthesize() voice={voice_key} chars={len(text)}")
    _update_job(job_id, {"status": "processing", "progress": 0, "job_id": job_id})

    try:
        wav_path, audio_duration, rtf = _synthesise(
            text, voice_key, speed, pitch, chunk_words, TMP_DIR
        )

        audio_path_str = str(wav_path)
        try:
            from core.config import get_settings as _gs
            cfg = _gs()
            if cfg.storage.BACKEND in ("minio", "s3"):
                import boto3
                s3 = boto3.client(
                    "s3",
                    endpoint_url=cfg.storage.ENDPOINT_URL,
                    aws_access_key_id=cfg.storage.ACCESS_KEY,
                    aws_secret_access_key=cfg.storage.SECRET_KEY,
                )
                s3_key = f"audio/{job_id}.wav"
                s3.upload_file(str(wav_path), cfg.storage.BUCKET_NAME, s3_key)
                audio_path_str = s3_key
        except Exception as upload_err:
            logger.warning(f"[{job_id}] MinIO upload skipped: {upload_err}")

        result = {
            "status":          "completed",
            "progress":        100,
            "job_id":          job_id,
            "audio_path":      audio_path_str,
            "audio_duration":  round(audio_duration, 3),
            "processing_time": round(rtf * audio_duration, 3),
            "rtf":             round(rtf, 4),
            "cache_hit":       False,
            "voice":           voice_key,
        }
        _update_job(job_id, result)
        logger.info(
            f"[{job_id}] done  duration={audio_duration:.2f}s RTF={rtf:.3f}"
        )
        return result

    except Exception as exc:
        logger.error(f"[{job_id}] failed: {exc}", exc_info=True)
        _update_job(job_id, {"status": "failed", "job_id": job_id, "error": str(exc)})
        raise self.retry(exc=exc, countdown=2)


# ── Startup preload ─────────────────────────────────────────────────

try:
    logger.info("Preloading en_US_female_high on startup...")
    _get_voice("en_US_female_high")
    logger.info("Preload complete — worker ready")
except Exception as _err:
    logger.error(f"Preload failed (will load on first request): {_err}")
    