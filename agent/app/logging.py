"""Structured logging — one JSON object per line on stdout/stderr.

Mirrors telegram/src/lib/log.mjs's shape (ts/level/msg + metadata) so logs
from both services read the same way in `docker compose logs`. No logging
framework: a handful of call sites and one format do not need one.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_LEVEL_NAMES = {"debug", "info", "warning", "warn", "error", "critical"}
_redacted_secrets: list[str] = []


def set_redacted_secrets(secrets: list[str]) -> None:
    """Register secrets (Cursor API key, etc.) to strip from every log line."""
    global _redacted_secrets
    _redacted_secrets = [s for s in secrets if isinstance(s, str) and len(s) >= 6]


def _redact(text: str) -> str:
    for secret in _redacted_secrets:
        text = text.replace(secret, "«redacted»")
    return text


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "msg": _redact(record.getMessage()),
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["error"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload)


def configure_logging(level: str = "info") -> None:
    root = logging.getLogger("agent")
    root.setLevel(level.upper() if level.lower() in {"debug", "info", "warning", "error"} else "INFO")
    root.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    """Return a child of the `agent` logger — call `configure_logging()` once at startup first."""
    return logging.getLogger(f"agent.{name}")
