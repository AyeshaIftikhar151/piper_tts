"""
core/logging.py — Structured Production Logging
================================================
- JSON format in production, human-readable in dev
- Injects trace_id and request_id into every log record
- Works across all distributed services (API, workers, beat)
- Filters Python reserved log field conflicts
"""
from __future__ import annotations

import json
import logging
import sys
import time
import traceback
import uuid
from contextvars import ContextVar
from typing import Any, Dict, Optional

# ── Context variables (thread/async safe) ─────────────────────────────────────
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")
_trace_id_ctx:   ContextVar[str] = ContextVar("trace_id",   default="")


def get_request_id() -> str:
    return _request_id_ctx.get() or str(uuid.uuid4())[:8]


def get_trace_id() -> str:
    return _trace_id_ctx.get() or str(uuid.uuid4())


def set_request_id(rid: str) -> None:
    _request_id_ctx.set(rid)


def set_trace_id(tid: str) -> None:
    _trace_id_ctx.set(tid)


def new_request_context() -> tuple[str, str]:
    """Generate and set a new request_id + trace_id. Returns both."""
    rid = str(uuid.uuid4())[:8]
    tid = str(uuid.uuid4())
    set_request_id(rid)
    set_trace_id(tid)
    return rid, tid


# ── JSON Formatter ─────────────────────────────────────────────────────────────

class JSONFormatter(logging.Formatter):
    """Emit each log record as one JSON line. Safe for log aggregators."""

    # Python's logging.LogRecord reserved fields — never put these in extra
    RESERVED = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname",
        "filename", "module", "exc_info", "exc_text", "stack_info",
        "lineno", "funcName", "created", "msecs", "relativeCreated",
        "thread", "threadName", "processName", "process", "message",
        "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        base: Dict[str, Any] = {
            "timestamp":  time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level":      record.levelname,
            "logger":     record.name,
            "message":    record.message,
            "module":     record.module,
            "line":       record.lineno,
            "request_id": get_request_id(),
            "trace_id":   get_trace_id(),
        }
        # Merge safe extra fields
        for k, v in record.__dict__.items():
            if k not in self.RESERVED and not k.startswith("_"):
                base[k] = v

        if record.exc_info:
            base["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            )
        return json.dumps(base, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable format for development."""

    COLORS = {
        "DEBUG":    "\033[36m",
        "INFO":     "\033[32m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color  = self.COLORS.get(record.levelname, "")
        reset  = self.RESET
        rid    = get_request_id()
        ts     = time.strftime("%H:%M:%S", time.gmtime(record.created))
        prefix = f"{color}{ts} {record.levelname:<8}{reset}"
        name   = f"\033[2m{record.name:<30}\033[0m"
        rid_s  = f"\033[2m[{rid}]\033[0m " if rid else ""
        return f"{prefix} {name} {rid_s}{record.getMessage()}"


# ── Setup ──────────────────────────────────────────────────────────────────────

def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for h in root.handlers[:]:
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        JSONFormatter() if fmt.lower() == "json" else TextFormatter()
    )
    root.addHandler(handler)

    # Silence noisy libraries
    for noisy in (
        "urllib3", "httpcore", "httpx", "celery.utils",
        "amqp", "kombu", "asyncio",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
