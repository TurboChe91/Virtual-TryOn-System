"""Structured JSON logging with rotation and automatic secret redaction.

Every log record is one JSON line. Task-scoped context (task_id, batch_id, sku,
output_type, provider, ...) is attached via the `extra={"ctx": {...}}` convention
or the `task_logger` helper.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
from pathlib import Path
from typing import Any

# Redact anything that looks like a bearer key/token in log text.
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8})[A-Za-z0-9_-]{8,}|(Bearer\s+)[A-Za-z0-9._-]{12,}")

_CTX_FIELDS = (
    "task_id",
    "batch_id",
    "style_id",
    "sku",
    "output_type",
    "provider",
    "model",
    "stage",
    "status",
    "duration_ms",
    "error_code",
    "attempt",
    "http_status",
)


def redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: (m.group(1) or m.group(2) or "") + "<redacted>", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            for key in _CTX_FIELDS:
                if key in ctx and ctx[key] is not None:
                    doc[key] = ctx[key]
        if record.exc_info and record.exc_info[0] is not None:
            doc["exc_type"] = record.exc_info[0].__name__
            doc["exc"] = redact(self.formatException(record.exc_info))[:4000]
        return json.dumps(doc, ensure_ascii=False)


def setup_logging(log_dir: Path, level: str = "INFO", console: bool = True) -> None:
    """Configure root logging once: JSON lines to rotating file + console."""
    root = logging.getLogger()
    root.setLevel(level)
    # Drop handlers we previously installed (idempotent across app restarts in tests).
    for h in list(root.handlers):
        if getattr(h, "_lunelle", False):
            root.removeHandler(h)

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "lunelle.log", maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler._lunelle = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(JsonFormatter())
        stream._lunelle = True  # type: ignore[attr-defined]
        root.addHandler(stream)

    # Keep noisy third-party loggers at WARNING.
    for name in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(name).setLevel(logging.WARNING)


class TaskLogger(logging.LoggerAdapter):
    """Logger adapter that stamps task context onto every record."""

    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        ctx = dict(self.extra or {})
        extra_ctx = kwargs.pop("ctx", None)
        if extra_ctx:
            ctx.update(extra_ctx)
        kwargs["extra"] = {"ctx": ctx}
        return msg, kwargs


def task_logger(name: str, **ctx: Any) -> TaskLogger:
    return TaskLogger(logging.getLogger(name), ctx)
