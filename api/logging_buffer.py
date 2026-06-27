"""In-memory ring buffer of recent backend log records, for the UI debug panel.

The app logs through the root logger (``logging.basicConfig`` in
:mod:`api.app_factory`). We attach a bounded :class:`RingBufferLogHandler` to the
root logger so the most recent log lines can be surfaced to the frontend via
``GET /settings/debug-logs`` and shown in the header debug panel.

Kept dependency-free and exception-safe so logging can never break a request.
LiteLLM's logger is disabled elsewhere (api.config) to avoid leaking prompts, so
those lines intentionally do not appear here.
"""

import collections
import contextvars
import logging
import threading
from typing import Any, Dict, List

_MAX_RECORDS = 1000
_buffer: "collections.deque[Dict[str, Any]]" = collections.deque(maxlen=_MAX_RECORDS)
_lock = threading.Lock()
_seq = 0

# Per-request user id so the debug panel shows ONLY the viewer's own logs, not
# other users'. Set by the auth decorators; contextvars propagate to the
# request's coroutine, the asyncio tasks it spawns, and to_thread workers.
_current_user: "contextvars.ContextVar[str | None]" = contextvars.ContextVar(
    "debug_log_user", default=None
)


def set_current_user(user_id: "str | None") -> None:
    """Tag all subsequent log records in this execution context with *user_id*."""
    try:
        _current_user.set(user_id)
    except Exception:  # pylint: disable=broad-except
        pass


class RingBufferLogHandler(logging.Handler):
    """A logging handler that keeps the last N records in memory."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        global _seq  # pylint: disable=global-statement
        try:
            message = record.getMessage()
        except Exception:  # pylint: disable=broad-except
            message = str(getattr(record, "msg", ""))
        try:
            uid = _current_user.get()
        except Exception:  # pylint: disable=broad-except
            uid = None
        try:
            with _lock:
                _seq += 1
                _buffer.append(
                    {
                        "id": _seq,
                        "ts": record.created,
                        "level": record.levelname,
                        "logger": record.name,
                        "message": message[:4000],
                        "user_id": uid,
                    }
                )
        except Exception:  # pylint: disable=broad-except
            # Never let logging raise into the app.
            pass


def get_recent_logs(limit: int = 300, level: str | None = None, after_id: int = 0,
                    user_id: "str | None" = None) -> List[Dict[str, Any]]:
    """Return recent log records (newest last), optionally filtered.

    When *user_id* is given, return only that user's records plus untagged
    system records (``user_id is None``) — never another user's logs.
    """
    with _lock:
        records = list(_buffer)
    if user_id is not None:
        records = [r for r in records if r.get("user_id") in (user_id, None)]
    if level:
        wanted = level.strip().upper()
        if wanted and wanted != "ALL":
            order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
            threshold = order.get(wanted, 0)
            records = [r for r in records if order.get(r["level"], 0) >= threshold]
    if after_id:
        records = [r for r in records if r["id"] > after_id]
    if limit and limit > 0:
        records = records[-limit:]
    return records


_installed = False


def install(level: int = logging.INFO) -> RingBufferLogHandler:
    """Attach a single ring-buffer handler to the root logger (idempotent)."""
    global _installed  # pylint: disable=global-statement
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, RingBufferLogHandler):
            return handler
    handler = RingBufferLogHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    root.addHandler(handler)
    _installed = True
    return handler
