"""Tiny in-process async pub/sub used to PUSH run progress to WebSocket clients
instead of having the frontend poll. The runner publishes events; each connected
WebSocket subscribes to its own queue. Same event loop, so `publish` is sync."""
from __future__ import annotations

import asyncio


_CASE_SNAPSHOT_KEYS = (
    "idx", "case_id", "difficulty", "question", "level", "matched",
    "human_level", "error", "reason", "elapsed_s", "attempts",
    "case_status", "case_status_label",
)


def _compact_case(case: dict) -> dict:
    """Keep WebSocket progress state small.

    Full case rows can contain large result tables and raw API responses. Those
    live durably in the store; the bus only needs enough data to update status
    rows without retaining megabytes in subscriber queues.
    """
    case = case or {}
    return {key: case.get(key) for key in _CASE_SNAPSHOT_KEYS if case.get(key) is not None}


def _compact_message(msg: dict) -> dict:
    if (msg or {}).get("type") != "case":
        return msg
    return {**msg, "case": _compact_case(msg.get("case") or {})}


class _Bus:
    def __init__(self):
        self._subs: set[asyncio.Queue] = set()
        self._cases: dict[str, dict] = {}

    def case_snapshot(self) -> list[dict]:
        return [{"run_id": run_id, "case": case} for run_id, cases in self._cases.items()
                for case in cases.values()]

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subs.discard(q)

    def clear_run(self, run_id: str):
        self._cases.pop(run_id, None)

    def publish(self, msg: dict):
        msg = _compact_message(msg)
        self._remember(msg)
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def _remember(self, msg: dict):
        if msg.get("type") == "case":
            run_id = msg.get("run_id")
            case = msg.get("case") or {}
            if not run_id or case.get("idx") is None:
                return
            status = case.get("case_status")
            key = str(case.get("idx"))
            if status in {"api_waiting", "llm_queued", "awaiting_judge", "sent_to_judge", "judging"}:
                self._cases.setdefault(run_id, {})[key] = case
            else:
                existing = self._cases.get(run_id)
                if existing is not None:
                    existing.pop(key, None)
                    if not existing:
                        self._cases.pop(run_id, None)
            return
        if msg.get("type") == "run":
            run = msg.get("run") or {}
            if run.get("status") not in {"queued", "running", "paused", "judging"}:
                self._cases.pop(run.get("id"), None)


bus = _Bus()
