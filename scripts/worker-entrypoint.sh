#!/bin/sh
# ============================================================
# scripts/worker-entrypoint.sh
#
# WHY THIS EXISTS:
#   Docker named volumes are created owned by root.
#   When docker-compose mounts tts_audio_data → /tmp/tts_audio,
#   it replaces the directory the Dockerfile created, and the
#   new mount is root:root with mode 755.
#
#   The worker user (uid 1000) then cannot write WAV files
#   → every synthesis task fails with "Permission denied".
#
# HOW IT WORKS:
#   This script runs as root (no USER directive in Dockerfile).
#   It fixes directory ownership, then uses `su-exec` to drop
#   privileges permanently to the worker user before exec'ing
#   the Celery CMD. The process never runs as root after that.
#
# SECURITY:
#   `exec su-exec worker "$@"` replaces this shell process —
#   there is no root process left running after the switch.
#   This is the same pattern used by official Redis, Postgres,
#   and Nginx Docker images.
# ============================================================
set -e

# Fix ownership of volume-mounted directories
mkdir -p /tmp/tts_audio /app/piper_models
chown -R worker:worker /tmp/tts_audio /app/piper_models

echo "[entrypoint] Volume ownership fixed. Starting as worker user..."

# Drop from root → worker and exec the CMD
# su-exec is a minimal setuid helper (like gosu but smaller)
exec su-exec worker "$@"