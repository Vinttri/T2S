"""Per-run append-only JSONL logs.

These logs are operational breadcrumbs for a run: state transitions, case stage
updates, attempts and errors. They intentionally avoid connector headers and API
keys; full benchmark artifacts remain in SQLite and data/{answers,judged,runs}.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from leaderboard.redaction import redact_obj


DATA_DIR = os.getenv("BENCH_APP_DATA_DIR", "bench_app/data")
RUN_LOGS_DIR = os.getenv("BENCH_APP_RUN_LOGS_DIR", os.path.join(DATA_DIR, "logs"))
_LOCK = threading.Lock()


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _stdout_max_chars() -> int:
    try:
        return max(0, int(os.getenv("BENCH_APP_STDOUT_RUN_LOG_MAX_CHARS", "20000")))
    except (TypeError, ValueError):
        return 20000


def _emit_stdout_log(rec: dict, line: str):
    if not _env_flag("BENCH_APP_STDOUT_RUN_LOGS", True):
        return
    max_chars = _stdout_max_chars()
    out_line = line
    if max_chars and len(line) > max_chars:
        out_line = json.dumps({
            "ts": rec.get("ts"),
            "time": rec.get("time"),
            "module": rec.get("module"),
            "run_id": rec.get("run_id"),
            "event": rec.get("event"),
            "truncated": True,
            "original_chars": len(line),
            "preview": line[:max_chars],
        }, ensure_ascii=False, default=str)
    try:
        print(f"bench_app.run_log {out_line}", file=sys.stdout, flush=True)
    except Exception:
        pass


def run_log_path(run_id: str, *, logs_dir: str | os.PathLike[str] | None = None) -> str:
    safe = "".join(ch for ch in str(run_id) if ch.isalnum() or ch in ("-", "_")) or "run"
    return str(Path(logs_dir or RUN_LOGS_DIR) / f"{safe}.jsonl")


def _caller_module() -> str:
    frame = inspect.currentframe()
    try:
        caller = frame.f_back.f_back if frame and frame.f_back else None
        module = inspect.getmodule(caller) if caller else None
        return module.__name__ if module and module.__name__ else "unknown"
    finally:
        del frame


def _format_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="milliseconds")


def append_run_log(run_id: str, event: str, *, logs_dir: str | os.PathLike[str] | None = None, **fields):
    path = Path(run_log_path(run_id, logs_dir=logs_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    ts = time.time()
    rec = {
        "ts": ts,
        "time": _format_time(ts),
        "module": _caller_module(),
        "run_id": str(run_id),
        "event": event,
        **redact_obj(fields),
    }
    line = json.dumps(rec, ensure_ascii=False, default=str)
    with _LOCK:
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        _emit_stdout_log(rec, line)


def read_run_log(run_id: str, *, logs_dir: str | os.PathLike[str] | None = None,
                 limit: int | None = None) -> list[dict]:
    path = Path(run_log_path(run_id, logs_dir=logs_dir))
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(redact_obj(json.loads(line)))
        except Exception:
            rows.append(redact_obj({"ts": None, "event": "parse_error", "raw": line}))
    return rows[-limit:] if limit else rows


def compact_run(run: dict | None) -> dict:
    run = run or {}
    return {
        "status": run.get("status"),
        "dataset_id": run.get("dataset_id"),
        "dataset_name": run.get("dataset_name"),
        "connector_id": run.get("connector_id"),
        "connector_name": run.get("connector_name"),
        "total_cases": run.get("total_cases"),
        "done_cases": run.get("done_cases"),
        "summary": run.get("summary"),
        "error": run.get("error"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
    }


def compact_case(case: dict | None) -> dict:
    case = case or {}
    assessment = case.get("assessment") or {}
    return {
        "idx": case.get("idx"),
        "case_id": case.get("case_id"),
        "difficulty": case.get("difficulty"),
        "question": case.get("question"),
        "case_status": case.get("case_status"),
        "case_status_label": case.get("case_status_label"),
        "attempts": case.get("attempts"),
        "elapsed_s": case.get("elapsed_s"),
        "error": case.get("error"),
        "level": case.get("level"),
        "matched": case.get("matched"),
        "predicted_sql_present": bool(case.get("predicted_sql")),
        "reason": case.get("reason"),
        "llm": {
            "attempts": assessment.get("attempts"),
            "error_category": assessment.get("error_category"),
            "confidence": assessment.get("confidence"),
            "raw_level": assessment.get("raw_level"),
            "repair_attempted": assessment.get("repair_attempted"),
            "validation_error": assessment.get("validation_error"),
        } if assessment else None,
    }
