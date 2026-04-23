# 🎙️ Piper TTS — Production Scalable Text-to-Speech API

[![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110-green?logo=fastapi)](https://fastapi.tiangolo.com)
[![Celery](https://img.shields.io/badge/Celery-5.x-37b24d?logo=celery)](https://docs.celeryq.dev)
[![Redis](https://img.shields.io/badge/Redis-7-red?logo=redis)](https://redis.io)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker)](https://docker.com)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> Production-grade Text-to-Speech API built on [Piper TTS](https://github.com/rhasspy/piper).  
> Supports **500–1000 concurrent users** on a single machine with async job queuing,  
> SHA-256 result caching, multi-speaker dialogue synthesis, and full observability.

---

## Table of Contents

- [Architecture](#architecture)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Environment Variables](#environment-variables)
- [API Reference](#api-reference)
- [Available Voices](#available-voices)
- [Development Setup](#development-setup)
- [Running Tests](#running-tests)
- [Folder Structure](#folder-structure)
- [Scaling Guide](#scaling-guide)
- [Contributing](#contributing)

---

## Architecture

```
React / Streamlit Frontend
         │
         ▼
    Nginx  (port 80)
    ├─ Rate limiting per IP
    ├─ Load balancing: least_conn → tts-api-1, tts-api-2
    └─ Streaming proxy for /download/
         │
         ▼
   FastAPI  (2 stateless instances)
    ├─ POST /generate-audio  → enqueue job, return job_id instantly
    ├─ GET  /jobs/{job_id}   → poll Redis for job state
    └─ GET  /download/{id}   → stream WAV from shared volume
         │
         ▼
      Redis  (2 databases)
    ├─ DB 0: Celery broker + SHA-256 audio cache  (TTL: 1 hr)
    └─ DB 1: Celery result backend + job state    (TTL: 10 min)
         │
         ▼
  Celery Workers  (N × concurrency=1)
    ├─ Preload all 6 Piper voice models at startup
    ├─ One synthesis task at a time per worker
    └─ Write WAV → shared Docker volume → /tmp/tts_audio/
         │
         ▼
  Celery Beat  (cleanup scheduler)
    └─ Every 2 min: delete WAV files older than 10 min
```

---

## Features

- **Async job queue** — API returns in < 5 ms; synthesis runs in background
- **SHA-256 caching** — identical requests served instantly from Redis (0 ms synthesis)
- **Multi-speaker dialogue** — assign different voices per speaker in a script
- **6 built-in voices** — US/GB, male/female, high/medium quality
- **Shared volume architecture** — WAV files accessible across all API + worker containers
- **Circuit breaker** — Redis failures handled gracefully, no cascade crashes
- **Structured JSON logging** — request_id and trace_id on every log line
- **Prometheus + Grafana** — queue depth, RTF, synthesis latency dashboards
- **Rate limiting** — per-IP Redis sliding window, configurable per tier
- **Security headers** — HSTS, CSP, X-Frame-Options, X-Content-Type-Options

---

## Tech Stack

| Layer | Technology |
|---|---|
| TTS Engine | [Piper TTS](https://github.com/rhasspy/piper) (ONNX) |
| API | FastAPI + uvicorn (uvloop + httptools) |
| Task Queue | Celery 5 |
| Broker / Cache | Redis 7 |
| Object Storage | MinIO (S3-compatible) or local volume |
| Reverse Proxy | Nginx |
| Observability | Prometheus + Grafana |
| UI | Streamlit (demo), React client (drop-in hook) |
| Containerisation | Docker + Docker Compose |

---

## Prerequisites

| Requirement | Version |
|---|---|
| Docker Desktop | 24+ |
| Docker Compose | v2 (included with Docker Desktop) |
| RAM | 8 GB minimum, **32 GB recommended** for 4 workers |
| Python (local dev only) | 3.11 |
| conda (local dev only) | any recent version |

> **Windows users:** Docker Desktop with WSL2 backend is required.

---

## Quick Start

### Option A — Docker (recommended)

```bash
# 1. Clone the repository
git clone https://github.com/AyeshaIftikhar151/piper-tts.git
cd piper-tts

# 2. Copy the environment template and fill in your values
cp .env.example .env

# 3. Build and start all services
docker-compose up --build

# 4. Verify everything is running
curl http://localhost/health
```

Services will be available at:

| Service | URL |
|---|---|
| API | http://localhost |
| API Docs (dev only) | http://localhost/docs |
| Streamlit UI | run `streamlit run streamlit_app.py` |
| Grafana | http://localhost:3001 (admin / admin) |
| Prometheus | http://localhost:9090 |
| MinIO Console | http://localhost:9001 |

### Option B — Scale to more workers

```bash
# Run with 4 workers total (supports ~240 synthesis jobs/hour)
docker-compose up --scale worker-1=1 --scale worker-2=3
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in your values. **Never commit `.env` to Git.**

| Variable | Default | Description |
|---|---|---|
| `ENV` | `dev` | `dev`, `staging`, or `prod` |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis broker URL |
| `STORAGE_BACKEND` | `local` | `local` or `minio` |
| `STORAGE_ENDPOINT_URL` | `http://localhost:9000` | MinIO endpoint |
| `STORAGE_ACCESS_KEY` | `minioadmin` | MinIO access key |
| `STORAGE_SECRET_KEY` | `minioadmin` | MinIO secret key |
| `SECURITY_JWT_SECRET_KEY` | — | **Change in production** |
| `GRAFANA_PASSWORD` | `admin` | **Change in production** |
| `CELERY_WORKER_CONCURRENCY` | `1` | Tasks per worker (keep at 1) |
| `CELERY_WORKER_MAX_RAM_MB` | `4000` | RAM guard per worker |

See `.env.example` for the full list.

---

## API Reference

### `POST /generate-audio`

Enqueues a synthesis job. Returns immediately with a `job_id`.

**Request:**
```json
{
  "text":        "Hello, world.",
  "voice":       "en_US_female_high",
  "speed":       1.0,
  "pitch":       0.0,
  "chunk_words": 30
}
```

**Response `202 Accepted`:**
```json
{
  "job_id":      "a1b2c3d4",
  "status":      "queued",
  "cache_hit":   false,
  "poll_url":    "http://localhost/jobs/a1b2c3d4",
  "download_url": ""
}
```

---

### `GET /jobs/{job_id}`

Poll for job status. Call every 2 seconds until `status == "completed"`.

**Response when complete:**
```json
{
  "job_id":          "a1b2c3d4",
  "status":          "completed",
  "progress":        100,
  "download_url":    "http://localhost/download/a1b2c3d4",
  "processing_time": 4.2,
  "audio_duration":  6.1,
  "rtf":             0.689,
  "cache_hit":       false,
  "expires_in":      540
}
```

---

### `GET /download/{job_id}`

Streams the WAV file. File expires 10 minutes after creation (returns `410 Gone` after that).

---

### `GET /health`

```json
{
  "status": "ok",
  "version": "3.0.0",
  "redis_connected": true,
  "workers_active": 2,
  "queue_length": 0
}
```

---

## Available Voices

| Key | Description |
|---|---|
| `en_US_female_high` | US English, female, high quality (default) |
| `en_US_male_high` | US English, male, high quality |
| `en_US_female_medium` | US English, female, medium quality |
| `en_US_male_medium` | US English, male, medium quality |
| `en_GB_male_medium` | British English, male, medium quality |
| `en_GB_female_medium` | British English, female, medium quality |

Voice models (~580 MB each) are downloaded automatically on first use and cached in `./piper_models/`.

---

## Development Setup

```bash
# Terminal 1 — Redis
docker run -d --name redis-dev -p 6379:6379 redis:7-alpine

# Terminal 2 — FastAPI
conda activate tts-api
set PYTHONPATH=%cd%        # Windows
uvicorn main:app --reload --port 8000

# Terminal 3 — Celery worker
conda activate tts-api
set PYTHONPATH=%cd%
celery -A worker.celery_app worker --concurrency=1 --loglevel=info -O fair

# Terminal 4 — Celery Beat
conda activate tts-api
set PYTHONPATH=%cd%
celery -A worker.celery_app beat --loglevel=info

# Terminal 5 — Streamlit UI (optional)
conda activate tts-api
streamlit run streamlit_app.py
```

API docs available at: http://localhost:8000/docs

---

## Running Tests

```bash
pip install pytest pytest-asyncio httpx
pytest tests/ -v --asyncio-mode=auto
```

---

## Folder Structure

```
piper-tts/
├── main.py                          ← FastAPI app factory + lifespan
├── requirements.txt
├── .env.example                     ← copy to .env, never commit .env
├── docker-compose.yml
├── Dockerfile.api                   ← lightweight API image (~200 MB)
├── Dockerfile.worker                ← heavy worker image (~3.5 GB RAM)
├── streamlit_app.py                 ← demo UI
├── run_dev.bat                      ← Windows local dev guide
│
├── scripts/
│   └── worker-entrypoint.sh        ← fixes volume ownership at runtime
│
├── core/
│   ├── config.py                   ← Pydantic BaseSettings
│   ├── redis_client.py             ← async + sync Redis, circuit breaker
│   └── logging.py                  ← structured JSON logging
│
├── models/
│   └── schemas.py                  ← API contract (shared with frontend)
│
├── api/
│   ├── routes/
│   │   └── tts.py                  ← all endpoints
│   └── middleware/
│       └── rate_limit.py           ← Redis sliding-window rate limiter
│
├── worker/
│   └── celery_app.py               ← Celery app + synthesis task
│
├── storage/
│   └── backend.py                  ← local / MinIO storage abstraction
│
├── nginx/
│   └── nginx.conf                  ← load balancer config
│
├── observability/
│   ├── metrics.py                  ← Prometheus metrics
│   └── prometheus/
│       └── prometheus.yml
│
├── react-client/
│   └── ttsClient.js                ← drop-in React hook (useTTS)
│
└── tests/
    └── test_api.py                 ← pytest suite (mocked Redis/Celery)
```

---

## Scaling Guide

| Workers | RAM used | Concurrent jobs | Jobs/hour (RTF 1.0) |
|---|---|---|---|
| 2 | ~7 GB | 2 | ~120 |
| 4 | ~14 GB | 4 | ~240 |
| 6 | ~21 GB | 6 | ~360 |

Add workers by editing `docker-compose.yml` or using `--scale`.  
Each worker loads all 6 voice models once at startup and holds them in RAM.

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/your-feature-name`
3. Commit your changes: `git commit -m "feat: describe your change"`
4. Push to your branch: `git push origin feature/your-feature-name`
5. Open a Pull Request against `main`

All PRs must pass CI (GitHub Actions) before merging.

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

*Built with [Piper TTS](https://github.com/rhasspy/piper) by rhasspy.*