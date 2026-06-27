"""Logging helpers used by Docker/uvicorn.

The app already writes per-run JSONL logs. This module keeps process logs in the
same machine-readable shape without adding a runtime dependency.
"""
from __future__ import annotations

import json
import logging
import time
import traceback
from datetime import datetime

from leaderboard.redaction import redact_obj


def _format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rec = {
            "ts": record.created,
            "time": _format_time(record.created),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "message": record.getMessage(),
        }
        if record.exc_info:
            rec["exc_info"] = "".join(traceback.format_exception(*record.exc_info))[:8000]
        for key, value in record.__dict__.items():
            if key in {
                "args", "asctime", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "module", "msecs",
                "message", "msg", "name", "pathname", "process", "processName",
                "relativeCreated", "stack_info", "thread", "threadName",
            }:
                continue
            if key.startswith("_"):
                continue
            rec[key] = value
        return json.dumps(redact_obj(rec), ensure_ascii=False, default=str)


class UtcJsonFormatter(JsonFormatter):
    """Compatibility alias in case a log config wants a UTC-named formatter."""


def configure_basic_json_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, str(level or "INFO").upper(), logging.INFO))
