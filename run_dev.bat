@echo off
REM ============================================================
REM run_dev.bat — Start all services for local development
REM Run each block in a SEPARATE terminal in VS Code
REM ============================================================

REM Prerequisites:
REM   conda activate tts-api
REM   Redis must be running (either Docker or native Windows)

REM ── Terminal 1: Redis (via Docker) ─────────────────────────
REM docker run -d --name redis -p 6379:6379 redis:7-alpine

REM ── Terminal 2: FastAPI ─────────────────────────────────────
REM cd tts-scalable
REM set PYTHONPATH=%cd%
REM uvicorn main:app --host 0.0.0.0 --port 8000 --reload

REM ── Terminal 3: Celery Worker ───────────────────────────────
REM cd tts-scalable
REM set PYTHONPATH=%cd%
REM celery -A worker.celery_app worker --concurrency=1 --loglevel=info -O fair

REM ── Terminal 4: Celery Beat (cleanup scheduler) ─────────────
REM cd tts-scalable
REM set PYTHONPATH=%cd%
REM celery -A worker.celery_app beat --loglevel=info

echo.
echo Open 4 terminals in VS Code and run commands above.
echo Or run: docker-compose up --build
echo.
