"""
tests/test_api.py
-----------------
Async test suite for all FastAPI endpoints.
Run with: pytest tests/ -v --asyncio-mode=auto

Requirements: pip install pytest pytest-asyncio httpx
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from main import app


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sample_request() -> dict:
    return {
        "text":        "Hello, this is a test.",
        "voice":       "en_US_female_high",
        "speed":       1.0,
        "pitch":       0.0,
        "chunk_words": 30,
    }


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ── /health ────────────────────────────────────────────────────────────────────

class TestHealth:
    async def test_health_returns_200(self, client):
        with patch("api.routes.tts.redis_ping", new_callable=AsyncMock, return_value=True), \
             patch("api.routes.tts.get_queue_length", new_callable=AsyncMock, return_value=0):
            r = await client.get("/health")
        assert r.status_code == 200

    async def test_health_response_shape(self, client):
        with patch("api.routes.tts.redis_ping", new_callable=AsyncMock, return_value=True), \
             patch("api.routes.tts.get_queue_length", new_callable=AsyncMock, return_value=0):
            data = (await client.get("/health")).json()
        assert "status" in data
        assert "redis_connected" in data


# ── /voices ────────────────────────────────────────────────────────────────────

class TestVoices:
    async def test_voices_returns_list(self, client):
        r = await client.get("/voices")
        assert r.status_code == 200
        voices = r.json()
        assert isinstance(voices, list)
        assert len(voices) == 6

    async def test_voice_has_required_fields(self, client):
        voices = (await client.get("/voices")).json()
        for v in voices:
            for field in ("key", "name", "gender", "locale", "quality"):
                assert field in v, f"Missing field '{field}' in voice {v}"


# ── POST /generate-audio ───────────────────────────────────────────────────────

class TestGenerateAudio:
    async def test_returns_202_with_job_id(self, client, sample_request):
        with patch("api.routes.tts.async_cache_get", new_callable=AsyncMock, return_value=None), \
             patch("api.routes.tts.async_job_set",   new_callable=AsyncMock), \
             patch("api.routes.tts.celery_synthesize") as mock_task:
            mock_task.apply_async = MagicMock()
            r = await client.post("/generate-audio", json=sample_request)

        assert r.status_code == 202
        data = r.json()
        assert "job_id" in data
        assert data["status"] in ("queued", "cached")

    async def test_empty_text_rejected(self, client):
        r = await client.post("/generate-audio", json={"text": "", "voice": "en_US_female_high"})
        assert r.status_code == 422

    async def test_text_too_long_rejected(self, client):
        r = await client.post("/generate-audio", json={
            "text":  "x" * 10_001,
            "voice": "en_US_female_high",
        })
        assert r.status_code == 422

    async def test_invalid_voice_rejected(self, client):
        r = await client.post("/generate-audio", json={
            "text":  "Hello",
            "voice": "not_a_real_voice",
        })
        assert r.status_code == 422

    async def test_speed_out_of_range_rejected(self, client):
        r = await client.post("/generate-audio", json={
            "text": "Hello", "voice": "en_US_female_high", "speed": 5.0
        })
        assert r.status_code == 422

    async def test_cache_hit_returns_download_url(self, client, sample_request):
        import tempfile, pathlib
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"RIFF\x00\x00\x00\x00WAVEfmt ")
            tmp_path = f.name

        mock_cache = {
            "audio_path": tmp_path,
            "audio_duration": 2.5,
            "rtf": 0.8,
        }
        with patch("api.routes.tts.async_cache_get", new_callable=AsyncMock, return_value=mock_cache), \
             patch("api.routes.tts.async_job_set",   new_callable=AsyncMock):
            r = await client.post("/generate-audio", json=sample_request)

        assert r.status_code == 202
        data = r.json()
        assert data["cache_hit"] is True
        assert data["download_url"] != ""

        pathlib.Path(tmp_path).unlink(missing_ok=True)

    async def test_poll_url_in_response(self, client, sample_request):
        with patch("api.routes.tts.async_cache_get", new_callable=AsyncMock, return_value=None), \
             patch("api.routes.tts.async_job_set",   new_callable=AsyncMock), \
             patch("api.routes.tts.celery_synthesize") as mock_task:
            mock_task.apply_async = MagicMock()
            data = (await client.post("/generate-audio", json=sample_request)).json()
        assert "poll_url" in data
        assert data["poll_url"] != ""


# ── GET /jobs/{job_id} ─────────────────────────────────────────────────────────

class TestJobStatus:
    async def test_unknown_job_returns_404(self, client):
        with patch("api.routes.tts.async_job_get", new_callable=AsyncMock, return_value=None):
            r = await client.get("/jobs/nonexistent-job-id")
        assert r.status_code == 404

    async def test_queued_job_returns_correct_status(self, client):
        state = {"status": "queued", "progress": 0}
        with patch("api.routes.tts.async_job_get", new_callable=AsyncMock, return_value=state):
            data = (await client.get("/jobs/test-job-123")).json()
        assert data["status"] == "queued"
        assert data["progress"] == 0

    async def test_completed_job_has_download_url(self, client, tmp_path):
        wav = tmp_path / "test.wav"
        wav.write_bytes(b"RIFF")
        state = {
            "status":       "completed",
            "progress":     100,
            "audio_path":   str(wav),
            "audio_duration": 3.2,
            "processing_time": 1.1,
            "rtf":          0.9,
            "cache_hit":    False,
            "created_at":   __import__("time").time(),
        }
        with patch("api.routes.tts.async_job_get", new_callable=AsyncMock, return_value=state):
            data = (await client.get("/jobs/test-job-123")).json()
        assert data["status"] == "completed"
        assert "/download/" in data["download_url"]

    async def test_failed_job_has_error(self, client):
        state = {"status": "failed", "error": "OOM", "progress": 0}
        with patch("api.routes.tts.async_job_get", new_callable=AsyncMock, return_value=state):
            data = (await client.get("/jobs/test-job-123")).json()
        assert data["status"] == "failed"
        assert data["error"] == "OOM"


# ── GET /download/{job_id} ─────────────────────────────────────────────────────

class TestDownload:
    async def test_download_streams_wav(self, client, tmp_path):
        wav = tmp_path / "output.wav"
        wav.write_bytes(b"RIFF\x00\x00\x00\x00WAVEfmt ")
        state = {"status": "completed", "audio_path": str(wav)}
        with patch("api.routes.tts.async_job_get", new_callable=AsyncMock, return_value=state):
            r = await client.get("/download/test-job-123")
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        assert "attachment" in r.headers["content-disposition"]

    async def test_download_job_not_found(self, client):
        with patch("api.routes.tts.async_job_get", new_callable=AsyncMock, return_value=None):
            r = await client.get("/download/missing-job")
        assert r.status_code == 404

    async def test_download_not_completed_returns_400(self, client):
        state = {"status": "processing", "progress": 50}
        with patch("api.routes.tts.async_job_get", new_callable=AsyncMock, return_value=state):
            r = await client.get("/download/test-job-123")
        assert r.status_code == 400

    async def test_download_expired_returns_410(self, client, tmp_path):
        state = {"status": "completed", "audio_path": "/tmp/nonexistent_12345.wav"}
        with patch("api.routes.tts.async_job_get", new_callable=AsyncMock, return_value=state):
            r = await client.get("/download/test-job-123")
        assert r.status_code == 410


# ── Cache key ─────────────────────────────────────────────────────────────────

class TestCacheKey:
    def test_same_inputs_same_key(self):
        from core.redis_client import make_cache_key
        k1 = make_cache_key("hello", "en_US_female_high", 1.0, 0.0)
        k2 = make_cache_key("hello", "en_US_female_high", 1.0, 0.0)
        assert k1 == k2

    def test_different_text_different_key(self):
        from core.redis_client import make_cache_key
        k1 = make_cache_key("hello",   "en_US_female_high", 1.0, 0.0)
        k2 = make_cache_key("goodbye", "en_US_female_high", 1.0, 0.0)
        assert k1 != k2

    def test_different_speed_different_key(self):
        from core.redis_client import make_cache_key
        k1 = make_cache_key("hello", "en_US_female_high", 1.0, 0.0)
        k2 = make_cache_key("hello", "en_US_female_high", 1.5, 0.0)
        assert k1 != k2

    def test_key_has_prefix(self):
        from core.redis_client import make_cache_key
        k = make_cache_key("hello", "en_US_female_high", 1.0, 0.0)
        assert k.startswith("tts:cache:")
