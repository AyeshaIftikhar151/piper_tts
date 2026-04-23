"""
storage/backend.py — Pluggable Storage Layer
=============================================
Supports:
  - local   → /tmp/tts_audio  (dev only)
  - minio   → MinIO S3-compatible (recommended for production)
  - s3      → AWS S3

Usage:
    from storage.backend import get_storage
    store = get_storage()
    url   = await store.save(job_id, wav_bytes)
    data  = await store.load(job_id)
    await store.delete(job_id)
"""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from core.config import get_settings

logger = logging.getLogger(__name__)
_cfg   = get_settings()
_scfg  = _cfg.storage


# ── Base interface ────────────────────────────────────────────────────────────

class StorageBackend(ABC):

    @abstractmethod
    async def save(self, job_id: str, data: bytes) -> str:
        """Save audio bytes. Returns a URL or path for retrieval."""

    @abstractmethod
    async def load(self, job_id: str) -> Optional[bytes]:
        """Load audio bytes. Returns None if not found."""

    @abstractmethod
    async def delete(self, job_id: str) -> None:
        """Delete audio file."""

    @abstractmethod
    async def exists(self, job_id: str) -> bool:
        """Check if audio file exists."""

    @abstractmethod
    async def cleanup_expired(self, ttl_seconds: int) -> int:
        """Delete files older than ttl_seconds. Returns count deleted."""


# ── Local storage (dev only) ──────────────────────────────────────────────────

class LocalStorage(StorageBackend):
    """
    Stores WAV files in LOCAL_TMP_DIR.
    WARNING: Not suitable for production — files don't survive container restarts
    and are not shared across API containers unless using a shared volume.
    Use MinIO in production.
    """

    def __init__(self) -> None:
        self._dir = _scfg.LOCAL_TMP_DIR
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self._dir / f"{job_id}.wav"

    async def save(self, job_id: str, data: bytes) -> str:
        path = self._path(job_id)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, path.write_bytes, data)
        return str(path)

    async def load(self, job_id: str) -> Optional[bytes]:
        path = self._path(job_id)
        if not path.exists():
            return None
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, path.read_bytes)

    async def delete(self, job_id: str) -> None:
        self._path(job_id).unlink(missing_ok=True)

    async def exists(self, job_id: str) -> bool:
        return self._path(job_id).exists()

    async def cleanup_expired(self, ttl_seconds: int) -> int:
        now     = time.time()
        deleted = 0
        for f in self._dir.glob("*.wav"):
            try:
                if now - f.stat().st_mtime > ttl_seconds:
                    f.unlink()
                    deleted += 1
            except Exception as exc:
                logger.warning(f"Cleanup error {f}: {exc}")
        return deleted

    def sync_save(self, job_id: str, data: bytes) -> str:
        """Sync version for Celery workers."""
        path = self._path(job_id)
        path.write_bytes(data)
        return str(path)

    def sync_exists(self, job_id: str) -> bool:
        return self._path(job_id).exists()

    def sync_load(self, job_id: str) -> Optional[bytes]:
        path = self._path(job_id)
        return path.read_bytes() if path.exists() else None

    def sync_cleanup_expired(self, ttl_seconds: int) -> int:
        now     = time.time()
        deleted = 0
        for f in self._dir.glob("*.wav"):
            try:
                if now - f.stat().st_mtime > ttl_seconds:
                    f.unlink()
                    deleted += 1
            except Exception as exc:
                logger.warning(f"Cleanup error {f}: {exc}")
        return deleted


# ── MinIO / S3 storage (production) ──────────────────────────────────────────

class MinIOStorage(StorageBackend):
    """
    Stores WAV files in MinIO (S3-compatible).
    Files are accessible across all API and worker containers.
    Presigned URLs allow direct download without proxying through FastAPI.

    Setup MinIO:
        docker run -p 9000:9000 -p 9001:9001 \\
          -e MINIO_ROOT_USER=minioadmin \\
          -e MINIO_ROOT_PASSWORD=minioadmin \\
          minio/minio server /data --console-address ':9001'
    """

    def __init__(self) -> None:
        try:
            import boto3
            from botocore.config import Config

            self._s3 = boto3.client(
                "s3",
                endpoint_url=_scfg.ENDPOINT_URL,
                aws_access_key_id=_scfg.ACCESS_KEY,
                aws_secret_access_key=_scfg.SECRET_KEY,
                region_name=_scfg.REGION,
                config=Config(
                    retries={"max_attempts": 3, "mode": "adaptive"},
                    max_pool_connections=20,
                ),
            )
            self._bucket = _scfg.BUCKET_NAME
            self._ensure_bucket()
            logger.info(f"MinIO storage ready — bucket: {self._bucket}")
        except ImportError:
            raise RuntimeError(
                "boto3 not installed. Run: pip install boto3"
            )

    def _ensure_bucket(self) -> None:
        try:
            self._s3.head_bucket(Bucket=self._bucket)
        except Exception:
            self._s3.create_bucket(Bucket=self._bucket)
            # Set lifecycle rule to auto-delete objects after TTL
            self._s3.put_bucket_lifecycle_configuration(
                Bucket=self._bucket,
                LifecycleConfiguration={
                    "Rules": [{
                        "ID": "auto-delete-audio",
                        "Status": "Enabled",
                        "Expiration": {
                            "Days": 1   # MinIO minimum is 1 day
                        },
                        "Filter": {"Prefix": ""},
                    }]
                },
            )
            logger.info(f"Created MinIO bucket: {self._bucket}")

    def _key(self, job_id: str) -> str:
        return f"audio/{job_id}.wav"

    async def save(self, job_id: str, data: bytes) -> str:
        loop = asyncio.get_event_loop()
        key  = self._key(job_id)

        def _upload():
            import io
            self._s3.upload_fileobj(
                io.BytesIO(data), self._bucket, key,
                ExtraArgs={"ContentType": "audio/wav"},
            )
            # Return presigned URL
            return self._s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=_scfg.PUBLIC_URL_TTL,
            )

        return await loop.run_in_executor(None, _upload)

    async def load(self, job_id: str) -> Optional[bytes]:
        loop = asyncio.get_event_loop()
        def _download():
            try:
                import io
                buf = io.BytesIO()
                self._s3.download_fileobj(
                    self._bucket, self._key(job_id), buf)
                return buf.getvalue()
            except Exception:
                return None
        return await loop.run_in_executor(None, _download)

    async def delete(self, job_id: str) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self._s3.delete_object(
                Bucket=self._bucket, Key=self._key(job_id))
        )

    async def exists(self, job_id: str) -> bool:
        loop = asyncio.get_event_loop()
        def _check():
            try:
                self._s3.head_object(
                    Bucket=self._bucket, Key=self._key(job_id))
                return True
            except Exception:
                return False
        return await loop.run_in_executor(None, _check)

    async def cleanup_expired(self, ttl_seconds: int) -> int:
        # MinIO lifecycle rules handle this automatically
        return 0

    def sync_save(self, job_id: str, data: bytes) -> str:
        """Sync version for Celery workers."""
        import io
        key = self._key(job_id)
        self._s3.upload_fileobj(
            io.BytesIO(data), self._bucket, key,
            ExtraArgs={"ContentType": "audio/wav"},
        )
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=_scfg.PUBLIC_URL_TTL,
        )

    def sync_exists(self, job_id: str) -> bool:
        try:
            self._s3.head_object(
                Bucket=self._bucket, Key=self._key(job_id))
            return True
        except Exception:
            return False

    def sync_load(self, job_id: str) -> Optional[bytes]:
        try:
            import io
            buf = io.BytesIO()
            self._s3.download_fileobj(
                self._bucket, self._key(job_id), buf)
            return buf.getvalue()
        except Exception:
            return None

    def sync_cleanup_expired(self, ttl_seconds: int) -> int:
        return 0  # Handled by MinIO lifecycle rules


# ── Factory ───────────────────────────────────────────────────────────────────

_storage_instance: Optional[StorageBackend] = None


def get_storage() -> StorageBackend:
    global _storage_instance
    if _storage_instance is None:
        backend = _scfg.BACKEND.lower()
        if backend in ("minio", "s3"):
            _storage_instance = MinIOStorage()
        else:
            if _cfg.is_prod:
                logger.warning(
                    "LOCAL storage used in PRODUCTION. "
                    "Set STORAGE_BACKEND=minio for production."
                )
            _storage_instance = LocalStorage()
    return _storage_instance
