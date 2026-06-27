"""Benchmark app backend (host service): connector builder, on-demand benchmark
runs, live progress, results with revisions (latest by default), JSON download.

Run:  uvicorn bench_app.server:app --host 0.0.0.0 --port 8090
Store: BENCH_STORE_URL (sqlite:///… default, or postgresql://…)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from leaderboard.db import PgExecutor
from leaderboard.redaction import redact_obj, redact_text, redact_url, safe_exception
from bench_app.store import make_store
from bench_app.defaults import migrate_dataset_paths_to_jsonl, seed_default_datasets
from bench_app.connectors import TemplatedConnector, preview_request, preview_to_curl, validate_plain_http_connector
from bench_app.datasets import save_uploaded_benchmark
from bench_app.http_client import httpx_verify
from bench_app.runner import (run_task, run_json_path, answers_json_path, build_result, build_answers,
                              apply_judged_levels, rerun, rerun_api_case, judge_existing_case,
                              count_rerun_targets, set_control, _result_to_dict,
                              execute_scoring_select, impala_concurrency_limit)
from bench_app.judge import judge_result, judge_answers, llm_config, check_llm_connection
from bench_app.bus import bus
from bench_app.connectors_yaml import export_connector, delete_yaml, export_all, load_into_store
from bench_app.qw_adapter import query_weaver_sql, queryweaver_native_sql_ctx
from bench_app.run_logs import append_run_log, compact_run, read_run_log, run_log_path
from bench_app.state_graph import CASE_STATUS_LABELS, RUN_ACTIVE_STATES, RUN_RECOVERABLE_STATES
from leaderboard.benchmark import BenchmarkCase, benchmark_case_to_json, benchmark_cases_to_jsonl, parse_benchmark_file

LOG = logging.getLogger("bench_app.server")


def _env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}

STORE = make_store()
migrate_dataset_paths_to_jsonl(STORE)
seed_default_datasets(STORE)
SYNC_CONNECTOR_YAML = _env_flag("BENCH_APP_SYNC_CONNECTOR_YAML", True)
if SYNC_CONNECTOR_YAML:
    load_into_store(STORE)   # import any hand-authored / edited YAML connector files
    export_all(STORE)        # ensure every connector has a YAML mirror on disk
STATIC = Path(__file__).parent / "static"
_TASKS: set = set()
_RUN_TASKS: dict[str, set[asyncio.Task]] = {}
_AUTOCONTINUE_STATUSES = set(RUN_ACTIVE_STATES)
_LEGACY_RESTART_STOP_ERROR = "прервано перезапуском сервера"
_EVENT_LOOP_LAG = {"current_ms": 0.0, "max_ms": 0.0, "last_warn_at": 0.0, "last_checked_at": 0.0}
_EVENT_LOOP_MONITOR_TASK: asyncio.Task | None = None


def _runner_mode() -> str:
    return (os.getenv("BENCH_APP_RUNNER_MODE") or "inline").strip().lower()


def _use_worker_runner() -> bool:
    return _runner_mode() in {"worker", "workers", "queue", "queued"}


def _track_task(task: asyncio.Task, run_id: str | None = None) -> asyncio.Task:
    _TASKS.add(task)
    bucket = None
    if run_id:
        bucket = _RUN_TASKS.setdefault(run_id, set())
        bucket.add(task)

    def _done(t: asyncio.Task):
        _TASKS.discard(t)
        if bucket is not None:
            bucket.discard(t)
            if not bucket:
                _RUN_TASKS.pop(run_id, None)
        try:
            t.exception()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    task.add_done_callback(_done)
    return task


def _enqueue_run_job(run_id: str, job_type: str, payload: dict | None = None) -> dict:
    job = STORE.enqueue_job(run_id, job_type, payload or {})
    append_run_log(run_id, "worker_job_queued", job_id=job.get("id"), job_type=job_type,
                   payload=payload or {}, run=compact_run(STORE.get_run(run_id)))
    bus.publish(_redacted({"type": "run", "run": STORE.get_run(run_id)}))
    return job


def _cancel_run_tasks(run_id: str) -> int:
    tasks = [t for t in _RUN_TASKS.get(run_id, set()) if not t.done()]
    for task in tasks:
        task.cancel()
    return len(tasks)


def _case_collected_for_done_count(case: dict) -> bool:
    if case.get("case_status") == "api_waiting":
        return False
    return (
        case.get("attempts") is not None
        or bool(case.get("predicted_sql"))
        or bool(case.get("error"))
        or case.get("level") is not None
        or case.get("gold_result") is not None
        or case.get("agent_result") is not None
    )


def _mark_run_stopped(run_id: str, *, error: str | None = "остановлено пользователем") -> dict | None:
    run = STORE.get_run(run_id)
    if not run:
        return None
    cases = STORE.list_cases(run_id, include_payload=False)
    done_cases = sum(1 for case in cases if _case_collected_for_done_count(case))
    update = {
        "status": "stopped",
        "finished_at": time.time(),
        "done_cases": done_cases,
        "summary": _summary_from_cases(cases, run.get("total_cases") or len(cases)),
    }
    if error is not None:
        update["error"] = error
    STORE.update_run(run_id, **update)
    run = STORE.get_run(run_id)
    append_run_log(run_id, "run_stopped", run=compact_run(run), cancelled_tasks=len(_RUN_TASKS.get(run_id, set())))
    bus.publish(_redacted({"type": "run", "run": run}))
    return run


def _run_deps_or_error(run_id: str):
    run = STORE.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    dataset = STORE.get_dataset(run.get("dataset_id"))
    if not dataset:
        raise HTTPException(400, "датасет этого прогона удален — создайте новый прогон с существующим датасетом")
    connector = STORE.get_connector(run.get("connector_id"))
    if not connector:
        raise HTTPException(400, "коннектор этого прогона удален — создайте новый прогон с существующим коннектором")
    _validate_connector_dataset(connector, dataset)
    return run, dataset, connector


def _bake_db(d: dict) -> dict:
    """Bake the connector's db_id into its body as a literal — `database` is set
    explicitly in the JSON, not substituted as a {{database}} parameter."""
    if d.get("db_id") and d.get("body_template"):
        d["body_template"] = (d["body_template"]
                              .replace("{{database}}", d["db_id"])
                              .replace("{{ database }}", d["db_id"]))
    return d


def _norm_dialect(s: str) -> str:
    """Normalise a SQL dialect / DB-type string for compatibility checks
    ('postgresql'/'pg' → 'postgres')."""
    s = (s or "").lower().strip()
    return "postgres" if s in ("postgres", "postgresql", "pg") else s


def _validate_connector_dataset(conn: dict, ds: dict):
    try:
        validate_plain_http_connector(conn)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    cd = _norm_dialect(conn.get("default_dialect") or "postgres")
    dt = _norm_dialect(ds.get("db_type") or "postgres")
    conn_name = conn.get("name") or conn.get("id") or "коннектор"
    ds_name = ds.get("name") or ds.get("id") or "датасет"
    if cd != dt:
        raise HTTPException(400, f"Несовместимо: коннектор «{conn_name}» рассчитан на SQL-диалект "
                                 f"'{conn.get('default_dialect') or '?'}', а датасет «{ds_name}» — на БД '{ds.get('db_type') or '?'}'.")
    if conn.get("db_id") and conn["db_id"] != ds.get("db_id"):
        raise HTTPException(400, f"Коннектор «{conn_name}» привязан к БД '{conn['db_id']}', "
                                 f"а датасет «{ds_name}» — это '{ds.get('db_id')}'.")
    if not str(ds.get("dsn") or "").strip():
        candidates = _dataset_dsn_env_candidates(ds.get("db_id") or "", ds.get("db_type") or "")
        raise HTTPException(400, "У датасета не задан DSN scoring-базы. Задайте env: " + " / ".join(candidates[:4]))
    _reject_container_localhost_dsn(ds)


def _format_cases(cases):
    return [dict(c) for c in cases]

app = FastAPI(title="Text-to-SQL Benchmark App")


def _redact_url_secret(url: str) -> str:
    return redact_text(redact_url(url))


def _redacted(value):
    return redact_obj(value)


def _restore_redacted_values(value, previous):
    """Do not persist UI placeholders back over existing secret values."""
    if isinstance(value, str) and "<redacted>" in value and isinstance(previous, str) and previous:
        return previous
    if isinstance(value, dict) and isinstance(previous, dict):
        return {k: _restore_redacted_values(v, previous.get(k)) for k, v in value.items()}
    if isinstance(value, list) and isinstance(previous, list):
        return [
            _restore_redacted_values(item, previous[i] if i < len(previous) else None)
            for i, item in enumerate(value)
        ]
    return value


def _public_dataset(d: dict | None) -> dict | None:
    if not d:
        return None
    return _redacted(d)


def _public_run(r: dict | None) -> dict | None:
    if not r:
        return None
    return _redacted(r)


def _public_case(c: dict | None) -> dict | None:
    if not c:
        return None
    return _redacted(c)


def _public_cases(cases):
    return [_public_case(c) for c in cases]


_PROGRESS_LIVE_STATUSES = set(RUN_ACTIVE_STATES)
_PROGRESS_RECOVERABLE_STATUSES = set(RUN_RECOVERABLE_STATES)
_PROGRESS_CASE_STATUS_LABELS = dict(CASE_STATUS_LABELS)


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(0, value)


def _progress_case_payload(case: dict) -> dict:
    """Compact case row for live progress snapshots.

    Full rows may contain large raw API responses and result tables. Sending
    those through every WebSocket reconnect can block the single uvicorn worker;
    the UI loads full details separately through /api/runs/{id}.
    """
    keys = (
        "idx", "case_id", "difficulty", "question", "level", "matched",
        "human_level", "error", "reason", "elapsed_s", "attempts",
        "case_status", "case_status_label",
    )
    payload = {k: case.get(k) for k in keys if case.get(k) is not None}
    status = payload.get("case_status")
    if not status:
        if case.get("level") is not None:
            status = "judged"
        elif case.get("error"):
            status = "sql_error" if case.get("predicted_sql") else "api_error"
        elif case.get("predicted_sql"):
            status = "llm_queued"
        else:
            status = "llm_queued"
        payload["case_status"] = status
    payload.setdefault("case_status_label", _PROGRESS_CASE_STATUS_LABELS.get(status, status))
    return payload


def _progress_case_snapshot(runs: list[dict]) -> list[dict]:
    """Build a WebSocket snapshot from durable DB state plus in-memory live rows.

    The bus only remembers transient rows in RAM. After a process restart that
    memory is empty, but partially completed runs still have their cases in the
    store. Include those persisted cases so the Progress tab can render already
    completed questions before any new live event arrives.
    """
    items: dict[tuple[str, str], dict] = {}
    run_limit = _env_int("BENCH_APP_PROGRESS_SNAPSHOT_RUN_LIMIT", 20)
    case_limit = _env_int("BENCH_APP_PROGRESS_SNAPSHOT_CASE_LIMIT", 500)
    scanned_runs = 0
    for run in runs:
        run_id = run.get("id")
        if not run_id:
            continue
        status = run.get("status")
        done = int(run.get("done_cases") or 0)
        total = int(run.get("total_cases") or 0)
        recoverable_partial = status in _PROGRESS_RECOVERABLE_STATUSES and done > 0 and (not total or done < total)
        if status not in _PROGRESS_LIVE_STATUSES and not recoverable_partial:
            continue
        if run_limit and scanned_runs >= run_limit:
            continue
        scanned_runs += 1
        try:
            cases = STORE.list_cases(run_id, include_payload=False)
        except Exception:
            continue
        for case in cases:
            if case_limit and len(items) >= case_limit:
                break
            key = str(case.get("idx") if case.get("idx") is not None else case.get("case_id") or "")
            if key:
                items[(run_id, key)] = {"run_id": run_id, "case": _progress_case_payload(case)}

    for item in bus.case_snapshot():
        run_id = item.get("run_id")
        case = item.get("case") or {}
        key = str(case.get("idx") if case.get("idx") is not None else case.get("case_id") or "")
        if run_id and key:
            items[(run_id, key)] = item
    return list(items.values())


def _progress_snapshot_message() -> dict:
    runs = STORE.list_runs()
    return _redacted({"type": "snapshot", "runs": runs, "cases": _progress_case_snapshot(runs)})


def _json_download(doc: dict, filename: str):
    return JSONResponse(
        content=_redacted(doc),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _download_name(value: str | None, suffix: str = ".jsonl") -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", (value or "dataset").strip()).strip("._")
    if not name:
        name = "dataset"
    if suffix and not name.lower().endswith(suffix.lower()):
        name += suffix
    return name[:140]


def _dataset_name_key(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _ensure_dataset_name_unique(name: str, dataset_id: str | None = None) -> str:
    clean_name = re.sub(r"\s+", " ", name or "").strip()
    if not clean_name:
        raise HTTPException(400, "Заполните название датасета.")
    key = _dataset_name_key(clean_name)
    for item in STORE.list_datasets():
        if dataset_id and item.get("id") == dataset_id:
            continue
        if _dataset_name_key(item.get("name") or "") == key:
            raise HTTPException(
                400,
                f"Датасет с названием «{clean_name}» уже существует. Выберите другое название.",
            )
    return clean_name


def _validate_dataset_benchmark_path(path: str) -> None:
    suffix = Path(path or "").suffix.lower()
    if suffix not in {".jsonl", ".ndjson"}:
        raise HTTPException(400, "Датасеты benchmark хранятся только в JSONL (.jsonl/.ndjson).")


def _env_token(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_").upper()


def _infer_dataset_db_id(*values: str | None) -> str:
    for value in values:
        key = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_").lower()
        compact = key.replace("_", "")
        if not compact:
            continue
        if "dmmis" in compact:
            return "dm_mis"
        if "sportevent" in compact or "sportsevent" in compact:
            return "sports_events_large"
        if "cybermarket" in compact or "cyber" in compact:
            return "cybermarket_pattern_large"
    for value in values:
        key = re.sub(r"[^A-Za-z0-9]+", "_", value or "").strip("_").lower()
        if key:
            return key
    return ""


def _db_alias_tokens(db_id: str) -> list[str]:
    token = _env_token(db_id)
    aliases: list[str] = []
    if token:
        aliases.append(token)
    compact = token.replace("_", "")
    if "DMMIS" in compact and "DM_MIS" not in aliases:
        aliases.append("DM_MIS")
    if ("SPORTEVENT" in compact or "SPORTSEVENT" in compact) and "SPORTS_EVENTS_LARGE" not in aliases:
        aliases.append("SPORTS_EVENTS_LARGE")
    if ("CYBERMARKET" in compact or "CYBER" in compact) and "CYBERMARKET_PATTERN_LARGE" not in aliases:
        aliases.append("CYBERMARKET_PATTERN_LARGE")
    return [item for item in aliases if item]


def _dataset_dsn_env_candidates(db_id: str, db_type: str = "") -> list[str]:
    typ = _env_token(db_type)
    auto_type = typ in {"", "AUTO"}
    names: list[str] = []
    for db in _db_alias_tokens(db_id):
        if db == "DM_MIS" and (typ == "IMPALA" or auto_type):
            names.extend(["BENCH_DM_MIS_IMPALA_DSN", "DM_MIS_IMPALA_DSN"])
        if auto_type:
            for candidate_type in ("IMPALA", "POSTGRES"):
                names.extend([f"BENCH_{db}_{candidate_type}_DSN", f"{db}_{candidate_type}_DSN"])
        elif typ:
            names.extend([f"BENCH_{db}_{typ}_DSN", f"{db}_{typ}_DSN"])
        names.extend([f"BENCH_{db}_DSN", f"{db}_DSN"])
    if auto_type:
        names.extend(["BENCH_IMPALA_DSN", "BENCH_POSTGRES_DSN"])
    elif typ:
        names.append(f"BENCH_{typ}_DSN")
    names.extend(["BENCH_SCORING_DSN", "SCORING_DSN"])
    out = []
    for name in names:
        if name not in out:
            out.append(name)
    return out


def _dataset_dsn_from_env(db_id: str, db_type: str = "") -> tuple[str, str | None, list[str]]:
    candidates = _dataset_dsn_env_candidates(db_id, db_type)
    for name in candidates:
        value = os.getenv(name)
        if value and value.strip():
            return value.strip(), name, candidates
    return "", None, candidates


def _db_type_from_dsn(dsn: str, fallback: str = "postgres") -> str:
    scheme = (dsn.split(":", 1)[0] if ":" in (dsn or "") else "").strip().lower()
    if scheme in {"postgresql", "postgres", "pg"}:
        return "postgres"
    if scheme:
        return _norm_dialect(scheme)
    fallback = (fallback or "").strip()
    return "postgres" if fallback.lower() == "auto" or not fallback else _norm_dialect(fallback)


def _running_in_container() -> bool:
    return (
        os.getenv("BENCH_APP_CONTAINERIZED", "").strip().lower() in {"1", "true", "yes", "on"}
        or os.path.exists("/.dockerenv")
        or str(os.getenv("BENCH_STORE_URL") or "").startswith("sqlite:////data/")
        or str(os.getenv("BENCH_APP_DATA_DIR") or "") == "/data"
    )


def _dsn_host(dsn: str) -> str:
    try:
        return (urlparse(dsn).hostname or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def _dsn_points_to_container_localhost(dsn: str) -> bool:
    if not _running_in_container():
        return False
    return _dsn_host(dsn) in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _reject_container_localhost_dsn(dataset: dict) -> None:
    dsn = str(dataset.get("dsn") or "").strip()
    if not dsn or not _dsn_points_to_container_localhost(dsn):
        return
    candidates = _dataset_dsn_env_candidates(dataset.get("db_id") or "", dataset.get("db_type") or "")
    raise HTTPException(
        400,
        "DSN scoring-базы у датасета указывает на localhost/127.0.0.1. "
        "В Docker это сам контейнер backend/worker, а не внешняя БД. "
        "Задайте доступный DSN через env: " + " / ".join(candidates[:6]),
    )


def _dataset_dsn_from_existing(db_id: str, db_type: str = "", exclude_id: str | None = None) -> tuple[str, dict | None]:
    db = str(db_id or "").strip()
    typ = _norm_dialect(db_type or "postgres")
    if not db:
        return "", None
    db_aliases = set(_db_alias_tokens(db))
    for item in STORE.list_datasets():
        if exclude_id and item.get("id") == exclude_id:
            continue
        item_db = str(item.get("db_id") or "").strip()
        if item_db != db and not (set(_db_alias_tokens(item_db)) & db_aliases):
            continue
        if _norm_dialect(item.get("db_type") or "postgres") != typ:
            continue
        dsn = str(item.get("dsn") or "").strip()
        if dsn:
            return dsn, item
    return "", None


def _resolve_dataset_dsn(dataset: dict) -> dict:
    requested_db_type = dataset.get("db_type") or "auto"
    dataset["dsn"] = ""
    dsn, env_name, candidates = _dataset_dsn_from_env(dataset.get("db_id") or "", dataset.get("db_type") or "")
    if not dsn:
        raise HTTPException(
            400,
            "DSN scoring-базы не задан. Укажите его в env, например: "
            + " / ".join(candidates)
            + ". Общий fallback для любого датасета: BENCH_SCORING_DSN или SCORING_DSN.",
        )
    meta = dict(dataset.get("meta") or {})
    meta.pop("dsn_source_dataset", None)
    meta["dsn_source_env"] = env_name
    dataset["meta"] = meta
    dataset["dsn"] = dsn
    dataset["db_type"] = _db_type_from_dsn(dsn, str(requested_db_type or "auto"))
    _reject_container_localhost_dsn(dataset)
    return dataset


def _dataset_db_identity_changed(dataset: dict, previous: dict | None) -> bool:
    if not previous:
        return False
    return (
        str(dataset.get("db_id") or "").strip() != str(previous.get("db_id") or "").strip()
        or _norm_dialect(dataset.get("db_type") or "postgres") != _norm_dialect(previous.get("db_type") or "postgres")
    )


def _datasets_dir() -> Path:
    data_dir = os.getenv("BENCH_APP_DATA_DIR", "bench_app/data")
    return Path(os.getenv("BENCH_APP_DATASETS_DIR", os.path.join(data_dir, "datasets")))


def _path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def _dataset_cases_or_error(dataset: dict) -> list[BenchmarkCase]:
    path = Path(dataset.get("benchmark_path") or "")
    _validate_dataset_benchmark_path(str(path))
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "benchmark file not found on disk")
    try:
        return parse_benchmark_file(path)
    except Exception as exc:
        raise HTTPException(400, f"Не удалось прочитать benchmark JSONL: {safe_exception(exc)}") from exc


def _ensure_editable_dataset_path(dataset: dict, cases: list[BenchmarkCase]) -> Path:
    """Return a writable runtime JSONL path and update dataset row if needed."""
    path = Path(dataset.get("benchmark_path") or "")
    datasets_dir = _datasets_dir()
    meta = dict(dataset.get("meta") or {})
    if _path_within(path, datasets_dir):
        if meta.get("seeded_default") or not meta.get("user_edited_dataset"):
            meta["format"] = "jsonl"
            meta["seeded_default"] = False
            meta["user_edited_dataset"] = True
            dataset = {**dataset, "meta": meta}
            STORE.save_dataset(dataset)
        return path
    datasets_dir.mkdir(parents=True, exist_ok=True)
    target_name = _download_name(f"{dataset.get('name') or dataset.get('id') or path.stem}__{dataset.get('id') or path.stem}", ".jsonl")
    target = datasets_dir / target_name
    target.write_text(benchmark_cases_to_jsonl(cases), encoding="utf-8")
    meta["format"] = "jsonl"
    meta["seeded_default"] = False
    meta["user_edited_dataset"] = True
    meta.setdefault("editable_copy_from", str(path))
    dataset = {**dataset, "benchmark_path": str(target.resolve()), "meta": meta}
    STORE.save_dataset(dataset)
    return target


def _case_from_edit(value: DatasetCaseEdit) -> BenchmarkCase:
    case_id = (value.case_id or "").strip()
    question = (value.question or "").strip()
    gold_sql = (value.gold_sql or "").strip()
    if not case_id:
        raise HTTPException(400, "case_id не может быть пустым.")
    if not question:
        raise HTTPException(400, "question не может быть пустым.")
    if not gold_sql:
        raise HTTPException(400, "gold_sql не может быть пустым.")
    conditions = value.conditions
    if isinstance(conditions, (dict, list)):
        conditions = json.dumps(conditions, ensure_ascii=False, separators=(",", ":"))
    return BenchmarkCase(
        benchmark_id=(value.benchmark_id or "").strip() or case_id,
        case_id=case_id,
        difficulty=(value.difficulty or "Unknown").strip() or "Unknown",
        question=question,
        normal_phrasing=(value.normal_phrasing or "").strip(),
        conditions=str(conditions or "").strip(),
        gold_sql=gold_sql,
    )


_READONLY_SQL_RE = re.compile(r"^\s*(select|with|show|describe|desc|explain)\b", re.IGNORECASE)


async def _execute_dataset_sql(dataset_id: str | None, sql: str, timeout_ms: int = 30000) -> dict | None:
    if not dataset_id:
        return None
    dataset = STORE.get_dataset(dataset_id)
    if not dataset:
        raise HTTPException(404, "dataset not found")
    query = (sql or "").strip()
    if not query:
        return {"ok": False, "columns": [], "rows": [], "row_count": 0, "error": "empty SQL"}
    if not _READONLY_SQL_RE.match(query):
        raise HTTPException(400, "В чате можно выполнять только read-only SQL: SELECT/WITH/SHOW/DESCRIBE/EXPLAIN.")
    timeout_ms = max(1, min(int(timeout_ms or 30000), 300000))
    t0 = time.time()
    executor = PgExecutor(dataset["dsn"], statement_timeout_ms=timeout_ms)
    res = await execute_scoring_select(executor, dataset, query)
    return {
        **(_result_to_dict(res) or {}),
        "elapsed_s": round(time.time() - t0, 3),
        "dataset_id": dataset_id,
        "dataset_name": dataset.get("name"),
        "db_id": dataset.get("db_id"),
        "db_type": dataset.get("db_type") or "postgres",
    }


def _default_scoring_db() -> dict:
    env_names = ("BENCH_DM_MIS_IMPALA_DSN", "DM_MIS_IMPALA_DSN", "BENCH_DM_MIS_DSN", "DM_MIS_DSN")
    for name in env_names:
        value = os.getenv(name)
        if value and value.strip():
            return {
                "source": name,
                "dsn": value.strip(),
                "safe_dsn": redact_text(value.strip()),
                "db_type": "impala" if value.strip().lower().startswith("impala://") else "postgres",
                "dataset_id": None,
                "dataset_name": None,
            }
    for dataset in STORE.list_datasets():
        if dataset.get("db_id") == "dm_mis" and dataset.get("db_type") == "impala" and dataset.get("dsn"):
            return {
                "source": "dataset",
                "dsn": dataset["dsn"],
                "safe_dsn": redact_text(dataset["dsn"]),
                "db_type": dataset.get("db_type") or "impala",
                "dataset_id": dataset.get("id"),
                "dataset_name": dataset.get("name"),
            }
    return {"source": None, "dsn": None, "safe_dsn": "", "db_type": None, "dataset_id": None, "dataset_name": None}


@app.get("/api/health")
def health():
    try:
        job_counts = STORE.job_counts()
    except Exception:
        job_counts = {}
    return {
        "ok": True,
        "runner_mode": _runner_mode(),
        "event_loop_lag_ms": _EVENT_LOOP_LAG.get("current_ms", 0.0),
        "event_loop_max_lag_ms": _EVENT_LOOP_LAG.get("max_ms", 0.0),
        "tracked_tasks": len(_TASKS),
        "active_run_tasks": sum(len(tasks) for tasks in _RUN_TASKS.values()),
        "jobs": job_counts,
    }


@app.get("/api/live")
def live():
    return {"ok": True}


def _ready_payload() -> tuple[bool, dict]:
    checks: dict = {}
    errors: list[str] = []
    try:
        checks["jobs"] = STORE.job_counts()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"store: {safe_exception(exc, limit=300)}")
    data_dir = Path(os.getenv("BENCH_APP_DATA_DIR", "bench_app/data"))
    dirs = {
        "data_dir": data_dir,
        "runs_dir": Path(os.getenv("BENCH_APP_RUNS_DIR", data_dir / "runs")),
        "answers_dir": Path(os.getenv("BENCH_APP_ANSWERS_DIR", data_dir / "answers")),
        "judged_dir": Path(os.getenv("BENCH_APP_JUDGED_DIR", data_dir / "judged")),
        "logs_dir": Path(os.getenv("BENCH_APP_RUN_LOGS_DIR", data_dir / "logs")),
        "datasets_dir": Path(os.getenv("BENCH_APP_DATASETS_DIR", data_dir / "datasets")),
    }
    checks["dirs"] = {}
    for name, path in dirs.items():
        ok = path.exists() and path.is_dir() and os.access(path, os.R_OK | os.W_OK)
        checks["dirs"][name] = {"path": str(path), "ok": ok}
        if not ok:
            errors.append(f"{name} is not readable/writable: {path}")
    return not errors, {"ok": not errors, "checks": checks, "errors": errors}


@app.get("/api/ready")
def ready():
    ok, payload = _ready_payload()
    return JSONResponse(status_code=200 if ok else 503, content=payload)


@app.get("/api/settings")
def runtime_settings():
    store_url = os.getenv("BENCH_STORE_URL", "sqlite:///bench_app/data/app.db")
    data_dir = os.getenv("BENCH_APP_DATA_DIR", "bench_app/data")
    return {
        "judge": _judge_settings(),
        "limits": _runtime_limits(),
        "logging": _runtime_logging(),
        "cache": _runtime_cache(),
        "backup": _runtime_backup(),
        "store": {
            "url": _redact_url_secret(store_url),
            "type": "postgres" if store_url.startswith("postgresql://") else "sqlite",
            "data_dir": data_dir,
            "runs_dir": os.getenv("BENCH_APP_RUNS_DIR", os.path.join(data_dir, "runs")),
            "answers_dir": os.getenv("BENCH_APP_ANSWERS_DIR", os.path.join(data_dir, "answers")),
            "judged_dir": os.getenv("BENCH_APP_JUDGED_DIR", os.path.join(data_dir, "judged")),
            "logs_dir": os.getenv("BENCH_APP_RUN_LOGS_DIR", os.path.join(data_dir, "logs")),
            "datasets_dir": os.getenv("BENCH_APP_DATASETS_DIR", os.path.join(data_dir, "datasets")),
        },
        "runner": {
            "mode": _runner_mode(),
            "worker": _use_worker_runner(),
            "env": "BENCH_APP_RUNNER_MODE",
        },
        "scoring_db": {k: v for k, v in _default_scoring_db().items() if k != "dsn"},
        "ssl": {
            "http_verify": httpx_verify(),
            "env": "BENCH_APP_SSL_VERIFY",
        },
        "connector_yaml_sync": SYNC_CONNECTOR_YAML,
    }


# ---------------- models ----------------
class Connector(BaseModel):
    id: str | None = None
    name: str
    method: str = "POST"
    url: str
    headers: dict = {}
    body_template: str = ""
    sql_extract: dict = {}
    default_dialect: str = "postgres"
    timeout: int = 600   # HTTP timeout per request; T2S on CPU can take 135-220s+/query
    max_attempts: int = 1          # 0 = infinite retries (until SQL executes)
    retry_delay: float = 0         # seconds to wait between retries
    description: str = ""
    db_id: str = ""   # target DB this connector is bound to (one connector per DB)


class Dataset(BaseModel):
    id: str | None = None
    name: str
    benchmark_path: str
    db_id: str = ""
    dsn: str = ""
    db_type: str = "auto"


class DatasetUpload(BaseModel):
    id: str | None = None
    name: str
    file_name: str
    content: str
    db_id: str = ""
    dsn: str = ""
    db_type: str = "auto"


class DatasetCaseEdit(BaseModel):
    benchmark_id: str = ""
    case_id: str
    difficulty: str = "Unknown"
    question: str
    normal_phrasing: str = ""
    conditions: str | dict | list = ""
    gold_sql: str


class TriggerReq(BaseModel):
    dataset_id: str
    connector_id: str
    concurrency: int = 1                  # how many questions to run in parallel (1 = sequential)
    max_attempts: int | None = None       # per-run override of retries (0 = infinite); None = connector default
    retry_delay: float | None = None      # per-run override of delay between retries (sec); None = connector default
    case_timeout: float = 600             # hard wall-clock cap per question (sec, 0 = off) — drop a case that spins longer. 600 fits slow CPU-only T2S (~135-220s/query); raise/0 for very slow hosts


class TestReq(BaseModel):
    connector: Connector
    question: str
    dialect: str = "postgres"
    database: str = ""


class ConnectorChatReq(BaseModel):
    connector_id: str
    question: str
    dialect: str = "postgres"
    database: str = ""
    dataset_id: str | None = None


class SqlExecuteReq(BaseModel):
    dataset_id: str
    sql: str
    timeout_ms: int = 30000


class JudgeLevelsReq(BaseModel):
    pass


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


async def _event_loop_lag_monitor():
    interval = max(0.2, _env_float("BENCH_APP_EVENT_LOOP_LAG_CHECK_S", 1.0))
    warn_after_s = max(0.0, _env_float("BENCH_APP_EVENT_LOOP_LAG_WARN_S", 2.0))
    warn_interval_s = max(1.0, _env_float("BENCH_APP_EVENT_LOOP_LAG_WARN_INTERVAL_S", 30.0))
    expected = time.monotonic() + interval
    while True:
        await asyncio.sleep(interval)
        now = time.monotonic()
        lag_s = max(0.0, now - expected)
        lag_ms = round(lag_s * 1000, 1)
        _EVENT_LOOP_LAG["current_ms"] = lag_ms
        _EVENT_LOOP_LAG["max_ms"] = max(float(_EVENT_LOOP_LAG.get("max_ms") or 0.0), lag_ms)
        _EVENT_LOOP_LAG["last_checked_at"] = time.time()
        last_warn_at = float(_EVENT_LOOP_LAG.get("last_warn_at") or 0.0)
        if warn_after_s and lag_s >= warn_after_s and now - last_warn_at >= warn_interval_s:
            _EVENT_LOOP_LAG["last_warn_at"] = now
            payload = {
                "time": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
                "lag_ms": lag_ms,
                "warn_after_ms": round(warn_after_s * 1000, 1),
                "tracked_tasks": len(_TASKS),
                "active_run_tasks": sum(len(tasks) for tasks in _RUN_TASKS.values()),
            }
            LOG.warning("bench_app.event_loop_lag %s", json.dumps(payload, ensure_ascii=False))
        expected = now + interval


def _ensure_event_loop_monitor():
    global _EVENT_LOOP_MONITOR_TASK
    try:
        current_loop = asyncio.get_running_loop()
        task_loop = _EVENT_LOOP_MONITOR_TASK.get_loop() if _EVENT_LOOP_MONITOR_TASK is not None else None
    except RuntimeError:
        return
    if _EVENT_LOOP_MONITOR_TASK is None or _EVENT_LOOP_MONITOR_TASK.done() or task_loop is not current_loop:
        _EVENT_LOOP_MONITOR_TASK = asyncio.create_task(_event_loop_lag_monitor())


def _judge_enabled() -> bool:
    return _env_flag("BENCH_APP_AUTO_JUDGE", True)


def _judge_timeout() -> float:
    return max(1.0, _env_float("LLM_JUDGE_TIMEOUT", 3600.0))


def _llm_test_timeout() -> float:
    return max(1.0, _env_float("LLM_TEST_TIMEOUT", _judge_timeout()))


def _api_concurrency_limit() -> int:
    return max(1, _env_int("BENCH_APP_MAX_API_CONCURRENCY", 1))


def _requested_api_concurrency(requested) -> int:
    try:
        return max(1, int(requested or 1))
    except (TypeError, ValueError):
        return 1


def _effective_api_concurrency(requested) -> int:
    return min(_requested_api_concurrency(requested), _api_concurrency_limit())


def _judge_concurrency() -> int:
    return max(1, _env_int("LLM_JUDGE_CONCURRENCY", 1))


def _impala_concurrency() -> int:
    return impala_concurrency_limit()


def _judge_max_retries() -> int:
    return max(0, _env_int("LLM_JUDGE_MAX_RETRIES", 2))


def _judge_retry_delay() -> float:
    return max(0.0, _env_float("LLM_JUDGE_RETRY_DELAY", 2.0))


def _judge_cfg_from_env():
    return llm_config()


def _autocontinue_enabled() -> bool:
    return _env_flag("BENCH_APP_AUTOCONTINUE_RUNS", True)


def _should_autocontinue_run(run: dict) -> bool:
    status = run.get("status")
    if status in _AUTOCONTINUE_STATUSES:
        return True
    # Compatibility with older builds that marked in-flight runs as stopped on
    # import. User-stopped runs use a different error and are not resumed.
    return status == "stopped" and run.get("error") == _LEGACY_RESTART_STOP_ERROR


def _run_uses_judge(run: dict) -> bool:
    cfg = run.get("config") or {}
    if "auto_judge" in cfg:
        return bool(cfg.get("auto_judge"))
    return _judge_enabled()


def _case_needs_connector_continue(case: dict | None) -> bool:
    if not case:
        return True
    if case.get("level") is not None:
        return False
    if case.get("case_status") == "api_waiting":
        return True
    return not _case_collected_for_done_count(case)


def _case_needs_judge_continue(case: dict | None, *, uses_judge: bool) -> bool:
    if not uses_judge or not case:
        return False
    if case.get("level") is not None:
        return False
    return _case_collected_for_done_count(case)


def _autocontinue_plan(run: dict, dataset: dict) -> tuple[list[str], list[str], int]:
    cases = parse_benchmark_file(dataset["benchmark_path"])
    existing = {c.get("case_id"): c for c in STORE.list_cases(run["id"], include_payload=False)}
    connector_targets: list[str] = []
    judge_targets: list[str] = []
    uses_judge = _run_uses_judge(run)
    for case in cases:
        rec = existing.get(case.case_id)
        if _case_needs_connector_continue(rec):
            connector_targets.append(case.case_id)
        elif _case_needs_judge_continue(rec, uses_judge=uses_judge):
            judge_targets.append(case.case_id)
    return connector_targets, judge_targets, len(cases)


def _finish_autocontinued_run(run_id: str, total: int) -> None:
    run = STORE.get_run(run_id)
    if not run or (run.get("status") in {"error", "stopped"} and run.get("error")):
        return
    cases = STORE.list_cases(run_id, include_payload=False)
    summary = _summary_from_cases(cases, total)
    done = summary.get("done") or 0
    final_status = "done" if done >= total else "stopped"
    update = {
        "status": final_status,
        "finished_at": time.time() if final_status == "done" else None,
        "done_cases": done,
        "summary": summary,
        "error": None if final_status == "done" else "автопродолжение не нашло недоделанные кейсы",
    }
    STORE.update_run(run_id, **update)
    bus.publish(_redacted({"type": "run", "run": STORE.get_run(run_id)}))


async def _autocontinue_run(run_id: str) -> None:
    run, dataset, _connector = _run_deps_or_error(run_id)
    uses_judge = _run_uses_judge(run)
    judge_cfg = None
    if uses_judge:
        judge_cfg = _judge_cfg_from_env()
        if not judge_cfg:
            raise RuntimeError("LLM judge не настроен для автопродолжения — задайте env LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
    connector_targets, judge_targets, total = _autocontinue_plan(run, dataset)
    append_run_log(
        run_id,
        "run_autocontinue_start",
        run=compact_run(run),
        connector_targets=len(connector_targets),
        judge_targets=len(judge_targets),
        total=total,
    )
    if connector_targets:
        await rerun(
            STORE,
            run_id,
            case_ids=connector_targets,
            api_global_concurrency=_api_concurrency_limit(),
            judge_cfg=judge_cfg,
            judge_timeout=(run.get("config") or {}).get("judge_timeout") or _judge_timeout(),
            judge_max_retries=_judge_max_retries(),
            judge_retry_delay=_judge_retry_delay(),
            judge_global_concurrency=_judge_concurrency(),
        )
    for case_id in judge_targets:
        await judge_existing_case(
            STORE,
            run_id,
            case_id,
            judge_cfg,
            judge_timeout=(run.get("config") or {}).get("judge_timeout") or _judge_timeout(),
            judge_max_retries=_judge_max_retries(),
            judge_retry_delay=_judge_retry_delay(),
            restore_status="running",
            judge_global_concurrency=_judge_concurrency(),
        )
    _finish_autocontinued_run(run_id, total)
    append_run_log(run_id, "run_autocontinue_finish", run=compact_run(STORE.get_run(run_id)))


async def _autocontinue_run_safe(run_id: str) -> None:
    try:
        await _autocontinue_run(run_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        msg = f"не удалось автопродолжить после рестарта: {safe_exception(exc)}"
        STORE.update_run(run_id, status="error", finished_at=time.time(), error=msg)
        run = STORE.get_run(run_id)
        append_run_log(run_id, "run_autocontinue_failed", run=compact_run(run), error=msg)
        bus.publish(_redacted({"type": "run", "run": run}))


async def _autocontinue_unfinished_runs() -> dict:
    if not _autocontinue_enabled():
        return {"enabled": False, "scheduled": 0, "failed": 0}
    if _use_worker_runner():
        return {"enabled": True, "scheduled": 0, "failed": 0, "mode": "worker"}
    scheduled = 0
    failed = 0
    for run in reversed(STORE.list_runs()):
        if not _should_autocontinue_run(run):
            continue
        run_id = run.get("id")
        if not run_id or run_id in _RUN_TASKS:
            continue
        try:
            _run_deps_or_error(run_id)
        except HTTPException as exc:
            failed += 1
            msg = f"не удалось автопродолжить после рестарта: {exc.detail}"
            STORE.update_run(run_id, status="error", finished_at=time.time(), error=msg)
            append_run_log(run_id, "run_autocontinue_failed", run=compact_run(STORE.get_run(run_id)), error=msg)
            bus.publish(_redacted({"type": "run", "run": STORE.get_run(run_id)}))
            continue
        STORE.update_run(run_id, status="queued", finished_at=None, error=None)
        append_run_log(run_id, "run_autocontinue_queued", run=compact_run(STORE.get_run(run_id)))
        bus.publish(_redacted({"type": "run", "run": STORE.get_run(run_id)}))
        _track_task(asyncio.create_task(_autocontinue_run_safe(run_id)), run_id)
        scheduled += 1
    return {"enabled": True, "scheduled": scheduled, "failed": failed}


async def _startup_autocontinue_unfinished_runs():
    _ensure_event_loop_monitor()
    await _autocontinue_unfinished_runs()


async def _shutdown_background_tasks():
    global _EVENT_LOOP_MONITOR_TASK
    if _EVENT_LOOP_MONITOR_TASK is not None and not _EVENT_LOOP_MONITOR_TASK.done():
        _EVENT_LOOP_MONITOR_TASK.cancel()
        await asyncio.gather(_EVENT_LOOP_MONITOR_TASK, return_exceptions=True)
    _EVENT_LOOP_MONITOR_TASK = None


app.router.add_event_handler("startup", _startup_autocontinue_unfinished_runs)
app.router.add_event_handler("shutdown", _shutdown_background_tasks)


def _runtime_limits() -> dict:
    return {
        "api_concurrency": _api_concurrency_limit(),
        "judge_concurrency": _judge_concurrency(),
        "impala_concurrency": _impala_concurrency(),
        "autocontinue_runs": _autocontinue_enabled(),
        "circuit_breaker": {
            "enabled": _env_flag("BENCH_APP_CIRCUIT_BREAKER_ENABLED", True),
            "api_failures": _env_int("BENCH_APP_CIRCUIT_BREAKER_API_FAILURES", _env_int("BENCH_APP_CIRCUIT_BREAKER_FAILURES", 5)),
            "db_failures": _env_int("BENCH_APP_CIRCUIT_BREAKER_DB_FAILURES", _env_int("BENCH_APP_CIRCUIT_BREAKER_FAILURES", 5)),
            "llm_failures": _env_int("BENCH_APP_CIRCUIT_BREAKER_LLM_FAILURES", _env_int("BENCH_APP_CIRCUIT_BREAKER_FAILURES", 5)),
        },
        "env": {
            "api_concurrency": "BENCH_APP_MAX_API_CONCURRENCY",
            "judge_concurrency": "LLM_JUDGE_CONCURRENCY",
            "impala_concurrency": "BENCH_APP_MAX_IMPALA_CONCURRENCY",
            "autocontinue_runs": "BENCH_APP_AUTOCONTINUE_RUNS",
            "circuit_breaker": "BENCH_APP_CIRCUIT_BREAKER_*",
        },
    }


def _runtime_logging() -> dict:
    return {
        "stdout_run_logs": _env_flag("BENCH_APP_STDOUT_RUN_LOGS", True),
        "stdout_max_chars": _env_int("BENCH_APP_STDOUT_RUN_LOG_MAX_CHARS", 20000),
        "process_json_logs": True,
        "env": {
            "stdout_run_logs": "BENCH_APP_STDOUT_RUN_LOGS",
            "stdout_max_chars": "BENCH_APP_STDOUT_RUN_LOG_MAX_CHARS",
            "process_json_logs": "UVICORN_LOG_CONFIG=bench_app/logging.ini",
        },
    }


def _runtime_backup() -> dict:
    data_dir = os.getenv("BENCH_APP_DATA_DIR", "bench_app/data")
    return {
        "enabled": _env_flag("BENCH_BACKUP_ENABLED", True),
        "interval_s": _env_int("BENCH_BACKUP_INTERVAL_S", 1800),
        "keep": _env_int("BENCH_BACKUP_KEEP", 48),
        "dir": os.getenv("BENCH_BACKUP_DIR", os.path.join(data_dir, "backups")),
        "env": {
            "enabled": "BENCH_BACKUP_ENABLED",
            "interval_s": "BENCH_BACKUP_INTERVAL_S",
            "keep": "BENCH_BACKUP_KEEP",
            "dir": "BENCH_BACKUP_DIR",
        },
    }


def _runtime_cache() -> dict:
    data_dir = os.getenv("BENCH_APP_DATA_DIR", "bench_app/data")
    return {
        "gold_cache": _env_flag("BENCH_APP_GOLD_CACHE", True),
        "gold_cache_dir": os.getenv("BENCH_APP_GOLD_CACHE_DIR", os.path.join(data_dir, "gold_cache")),
        "gold_cache_memory_entries": _env_int(
            "BENCH_APP_GOLD_CACHE_MEMORY_ENTRIES",
            _env_int("BENCH_APP_GOLD_CACHE_MAX_ENTRIES", 0),
        ),
        "env": {
            "gold_cache": "BENCH_APP_GOLD_CACHE",
            "gold_cache_dir": "BENCH_APP_GOLD_CACHE_DIR",
            "gold_cache_memory_entries": "BENCH_APP_GOLD_CACHE_MEMORY_ENTRIES",
        },
    }


def _judge_settings() -> dict:
    base_url = os.getenv("LLM_BASE_URL", "")
    api_key = os.getenv("LLM_API_KEY", "")
    model = os.getenv("LLM_MODEL", "")
    auth_header = os.getenv("LLM_AUTH_HEADER", "Authorization")
    auth_scheme = os.getenv("LLM_AUTH_SCHEME", "Bearer")
    enabled = _judge_enabled()
    auth_enabled = str(auth_header or "").strip().lower() not in {"", "none", "off", "disabled", "0", "false", "no"}
    ready = bool(enabled and base_url and model and (api_key or not auth_enabled))
    return {
        "auto_judge": enabled,
        "ready": ready,
        "read_only": True,
        "base_url": redact_text(base_url),
        "api_key_set": bool(api_key),
        "api_key": "<задан>" if api_key else "",
        "auth_header": auth_header if auth_enabled else "none",
        "auth_scheme": auth_scheme if auth_enabled else "none",
        "model": model,
        "timeout": _judge_timeout(),
        "test_timeout": _llm_test_timeout(),
        "concurrency": _judge_concurrency(),
        "max_retries": _judge_max_retries(),
        "retry_delay": _judge_retry_delay(),
        "env": {
            "auto_judge": "BENCH_APP_AUTO_JUDGE",
            "base_url": "LLM_BASE_URL",
            "api_key": "LLM_API_KEY",
            "auth_header": "LLM_AUTH_HEADER",
            "auth_scheme": "LLM_AUTH_SCHEME",
            "model": "LLM_MODEL",
            "timeout": "LLM_JUDGE_TIMEOUT",
            "test_timeout": "LLM_TEST_TIMEOUT",
            "concurrency": "LLM_JUDGE_CONCURRENCY",
            "max_retries": "LLM_JUDGE_MAX_RETRIES",
            "retry_delay": "LLM_JUDGE_RETRY_DELAY",
        },
    }


def _safe_error(exc: Exception, cfg: dict | None = None) -> str:
    return safe_exception(exc, extra_secrets=[(cfg or {}).get("api_key")], limit=1000)


@app.post("/api/settings/llm-test")
async def test_llm_settings():
    cfg = _judge_cfg_from_env()
    if not cfg:
        raise HTTPException(400, "LLM не настроен — задайте env LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.")
    try:
        return await check_llm_connection(cfg, timeout=_llm_test_timeout())
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "base_url": redact_text(cfg.get("base_url")),
            "model": cfg.get("model"),
            "elapsed_s": None,
            "error": _safe_error(exc, cfg),
        }


@app.post("/api/settings/db-test")
async def test_scoring_db_settings():
    resolved = _default_scoring_db()
    dsn = resolved.get("dsn")
    if not dsn:
        raise HTTPException(400, "Scoring DB не настроена — задайте BENCH_DM_MIS_IMPALA_DSN или DSN датасета.")
    t0 = time.time()
    try:
        res = await asyncio.wait_for(
            execute_scoring_select(PgExecutor(dsn, statement_timeout_ms=5000), resolved, "SELECT 1"),
            timeout=20,
        )
        return {
            "ok": bool(res.ok),
            "source": resolved.get("source"),
            "dsn": resolved.get("safe_dsn"),
            "db_type": resolved.get("db_type"),
            "dataset_id": resolved.get("dataset_id"),
            "dataset_name": resolved.get("dataset_name"),
            "elapsed_s": round(time.time() - t0, 2),
            "row_count": res.row_count,
            "error": redact_text(res.error),
        }
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "source": resolved.get("source"),
            "dsn": resolved.get("safe_dsn"),
            "db_type": resolved.get("db_type"),
            "dataset_id": resolved.get("dataset_id"),
            "dataset_name": resolved.get("dataset_name"),
            "elapsed_s": round(time.time() - t0, 2),
            "row_count": 0,
            "error": "DB test timeout after 20s",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "source": resolved.get("source"),
            "dsn": resolved.get("safe_dsn"),
            "db_type": resolved.get("db_type"),
            "dataset_id": resolved.get("dataset_id"),
            "dataset_name": resolved.get("dataset_name"),
            "elapsed_s": round(time.time() - t0, 2),
            "row_count": 0,
            "error": safe_exception(exc, extra_secrets=[dsn], limit=1000),
        }


# ---------------- connectors ----------------
@app.get("/api/connectors")
def list_connectors():
    return _redacted(STORE.list_connectors())

@app.post("/api/connectors")
def save_connector(c: Connector):
    conn = c.model_dump()
    previous = STORE.get_connector(conn.get("id")) if conn.get("id") else None
    if previous:
        conn = _restore_redacted_values(conn, previous)
    conn = _bake_db(conn)
    try:
        validate_plain_http_connector(conn)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    saved = STORE.save_connector(conn)
    if SYNC_CONNECTOR_YAML:
        export_connector(saved)   # mirror to YAML
    for run in STORE.list_runs():
        if run.get("connector_id") == saved.get("id"):
            bus.publish(_redacted({"type": "run", "run": run}))
    return _redacted(saved)

@app.delete("/api/connectors/{cid}")
def del_connector(cid: str):
    STORE.delete_connector(cid)
    if SYNC_CONNECTOR_YAML:
        delete_yaml(cid)
    return {"ok": True}

@app.get("/api/first-question")
def first_question(db_id: str):
    """First question of the benchmark whose dataset matches db_id — used to
    auto-fill a connector's test question with a real question for ITS database."""
    matches = [d for d in STORE.list_datasets() if d.get("db_id") == db_id]
    # prefer the dataset named exactly like the db (the canonical benchmark), not e.g. Training
    ds = next((d for d in matches if d.get("name") == db_id), None) or (matches[0] if matches else None)
    if not ds:
        return {"question": None}
    try:
        cases = parse_benchmark_file(ds["benchmark_path"])
        c = cases[0] if cases else None
        return {"question": c.question if c else None, "case_id": c.case_id if c else None}
    except Exception as exc:  # noqa: BLE001
        return {"question": None, "error": safe_exception(exc, limit=120)}

@app.post("/api/connectors/preview")
def preview(t: TestReq):
    db = t.database or t.connector.db_id
    try:
        return _redacted(preview_request(_bake_db(t.connector.model_dump()), t.question, t.dialect, db))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

@app.post("/api/connectors/curl")
def connector_curl(t: TestReq):
    db = t.database or t.connector.db_id
    try:
        rendered = preview_request(_bake_db(t.connector.model_dump()), t.question, t.dialect, db)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _redacted({"preview": rendered, "curl": preview_to_curl(rendered)})

@app.post("/api/connectors/test")
async def test_connector(t: TestReq):
    db = t.database or t.connector.db_id   # fall back to the connector's bound DB
    c = _bake_db(t.connector.model_dump())
    try:
        validate_plain_http_connector(c)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    conn = TemplatedConnector(c)
    async with httpx.AsyncClient(verify=httpx_verify("CONNECTOR_SSL_VERIFY")) as client:
        sql, payload, err = await conn.generate(client, t.question, t.dialect, float(t.connector.timeout), db)
    return _redacted({"sql": sql, "error": err, "response": payload})


@app.post("/api/connectors/chat")
async def chat_connector(req: ConnectorChatReq):
    conn_doc = STORE.get_connector(req.connector_id)
    if not conn_doc:
        raise HTTPException(404, "connector not found")
    try:
        validate_plain_http_connector(conn_doc)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(400, "Введите вопрос для коннектора.")
    dialect = req.dialect or conn_doc.get("default_dialect") or "postgres"
    database = req.database or conn_doc.get("db_id") or ""
    conn = TemplatedConnector(conn_doc)
    async with httpx.AsyncClient(verify=httpx_verify("CONNECTOR_SSL_VERIFY")) as client:
        sql, payload, err = await conn.generate(client, question, dialect, float(conn_doc.get("timeout") or 200), database)
    sql_result = await _execute_dataset_sql(req.dataset_id, sql) if sql else None
    return _redacted({
        "connector_id": req.connector_id,
        "connector_name": conn_doc.get("name"),
        "question": question,
        "dialect": dialect,
        "database": database,
        "dataset_id": req.dataset_id,
        "sql": sql,
        "sql_result": sql_result,
        "error": err,
        "response": payload,
    })


# ---------------- ad-hoc SQL execution ----------------
@app.post("/api/sql/execute")
async def execute_sql(req: SqlExecuteReq):
    result = await _execute_dataset_sql(req.dataset_id, req.sql, req.timeout_ms)
    return _redacted({"sql": req.sql, "result": result})


# ---------------- datasets ----------------
@app.get("/api/datasets")
def list_datasets():
    return [_public_dataset(d) for d in STORE.list_datasets()]

@app.post("/api/datasets")
def save_dataset(d: Dataset):
    raw_dataset = d.model_dump()
    dataset = dict(raw_dataset)
    dataset["name"] = _ensure_dataset_name_unique(dataset.get("name") or "", dataset.get("id"))
    _validate_dataset_benchmark_path(dataset.get("benchmark_path") or "")
    previous = STORE.get_dataset(dataset.get("id")) if dataset.get("id") else None
    if previous:
        dataset = _restore_redacted_values(dataset, previous)
    dataset["db_id"] = (
        str(raw_dataset.get("db_id") or "").strip()
        or str((previous or {}).get("db_id") or "").strip()
        or _infer_dataset_db_id(dataset.get("name"), dataset.get("benchmark_path"))
    )
    if not dataset["db_id"]:
        raise HTTPException(400, "Не удалось определить db_id по имени датасета или файла.")
    dataset["dsn"] = ""
    dataset["db_type"] = raw_dataset.get("db_type") or "auto"
    dataset = _resolve_dataset_dsn(dataset)
    return _public_dataset(STORE.save_dataset(dataset))

@app.post("/api/datasets/upload")
def upload_dataset(d: DatasetUpload):
    if not d.name.strip():
        raise HTTPException(400, "Заполните название.")
    clean_name = _ensure_dataset_name_unique(d.name, d.id or None)
    previous = STORE.get_dataset(d.id) if d.id else None
    db_id = (
        d.db_id.strip()
        or str((previous or {}).get("db_id") or "").strip()
        or _infer_dataset_db_id(clean_name, d.file_name)
    )
    if not db_id:
        raise HTTPException(400, "Не удалось определить db_id по имени датасета или файла.")
    try:
        path, cases_count = save_uploaded_benchmark(d.content, d.file_name, clean_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    dataset = {
        "id": d.id,
        "name": clean_name,
        "benchmark_path": path,
        "db_id": db_id,
        "dsn": "",
        "db_type": d.db_type or "auto",
        "meta": {"uploaded_file": d.file_name, "cases_count": cases_count},
    }
    if previous:
        dataset = _restore_redacted_values(dataset, previous)
        dataset["db_id"] = db_id
    dataset["dsn"] = ""
    dataset["db_type"] = d.db_type or "auto"
    dataset = _resolve_dataset_dsn(dataset)
    saved = STORE.save_dataset(dataset)
    return _public_dataset({**saved, "cases_count": cases_count})

@app.get("/api/datasets/{did}/download")
def download_dataset(did: str):
    dataset = STORE.get_dataset(did)
    if not dataset:
        raise HTTPException(404, "dataset not found")
    path = Path(dataset.get("benchmark_path") or "")
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "benchmark file not found on disk")
    filename = _download_name(dataset.get("name") or path.stem, suffix=".jsonl")
    return FileResponse(
        str(path),
        media_type="application/x-ndjson; charset=utf-8",
        filename=filename,
    )

@app.get("/api/datasets/{did}/cases")
def list_dataset_cases(did: str):
    dataset = STORE.get_dataset(did)
    if not dataset:
        raise HTTPException(404, "dataset not found")
    cases = _dataset_cases_or_error(dataset)
    return _redacted({
        "dataset": _public_dataset(dataset),
        "count": len(cases),
        "cases": [benchmark_case_to_json(case) for case in cases],
    })

@app.put("/api/datasets/{did}/cases/{case_id}")
def update_dataset_case(did: str, case_id: str, payload: DatasetCaseEdit):
    dataset = STORE.get_dataset(did)
    if not dataset:
        raise HTTPException(404, "dataset not found")
    cases = _dataset_cases_or_error(dataset)
    old_case_id = case_id
    index = next((i for i, case in enumerate(cases) if case.case_id == old_case_id), None)
    if index is None:
        raise HTTPException(404, "case not found")
    new_case = _case_from_edit(payload)
    if new_case.case_id != old_case_id and any(case.case_id == new_case.case_id for i, case in enumerate(cases) if i != index):
        raise HTTPException(400, f"case_id уже существует: {new_case.case_id}")
    cases[index] = new_case
    path = _ensure_editable_dataset_path(dataset, cases)
    path.write_text(benchmark_cases_to_jsonl(cases), encoding="utf-8")
    saved_dataset = STORE.get_dataset(did) or dataset
    return _redacted({
        "dataset": _public_dataset(saved_dataset),
        "case": benchmark_case_to_json(new_case),
        "count": len(cases),
    })

@app.delete("/api/datasets/{did}")
def del_dataset(did: str):
    STORE.delete_dataset(did); return {"ok": True}


# ---------------- runs ----------------
@app.post("/api/runs")
async def trigger(req: TriggerReq):
    ds = STORE.get_dataset(req.dataset_id)
    conn = STORE.get_connector(req.connector_id)
    if not ds or not conn:
        raise HTTPException(404, "dataset or connector not found")
    _validate_connector_dataset(conn, ds)
    auto_judge = _judge_enabled()
    judge_timeout = _judge_timeout()
    requested_concurrency = _requested_api_concurrency(req.concurrency)
    api_concurrency = _effective_api_concurrency(requested_concurrency)
    api_concurrency_limit = _api_concurrency_limit()
    judge_concurrency = _judge_concurrency()
    judge_max_retries = _judge_max_retries()
    judge_retry_delay = _judge_retry_delay()
    cfg = {"concurrency": api_concurrency, "requested_concurrency": requested_concurrency,
           "api_concurrency_limit": api_concurrency_limit,
           "max_attempts": req.max_attempts,
           "retry_delay": req.retry_delay, "case_timeout": req.case_timeout,
           "auto_judge": auto_judge,
           "judge_model": os.getenv("LLM_MODEL"),
           "judge_timeout": judge_timeout,
           "judge_concurrency": judge_concurrency,
           "impala_concurrency_limit": _impala_concurrency(),
           "judge_max_retries": judge_max_retries,
           "judge_retry_delay": judge_retry_delay}
    judge_cfg = None
    if auto_judge:
        judge_cfg = _judge_cfg_from_env()
        if not judge_cfg:
            raise HTTPException(400, "LLM judge не настроен — задайте env LLM_BASE_URL / LLM_API_KEY / LLM_MODEL "
                                     "для процесса bench_app и перезапустите.")
    run = STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                           connector_id=conn["id"], connector_name=conn["name"], config=cfg)
    append_run_log(run["id"], "run_created", run=compact_run(run), config=cfg)
    bus.publish(_redacted({"type": "run", "run": run}))  # push the queued run to WS clients
    if _use_worker_runner():
        _enqueue_run_job(run["id"], "run", {"source": "api"})
    else:
        _track_task(asyncio.create_task(run_task(STORE, run["id"], ds, conn, concurrency=api_concurrency,
                                                 api_global_concurrency=api_concurrency_limit,
                                                 max_attempts=req.max_attempts, retry_delay=req.retry_delay,
                                                 case_timeout=req.case_timeout,
                                                 judge_cfg=judge_cfg,
                                                 judge_timeout=judge_timeout,
                                                 judge_concurrency=judge_concurrency,
                                                 judge_max_retries=judge_max_retries,
                                                 judge_retry_delay=judge_retry_delay)), run["id"])
    return _public_run(run)


@app.post("/api/runs/{rid}/repeat")
async def repeat_run(rid: str):
    """Create a NEW run with the same dataset/connector/config as an existing one."""
    old = STORE.get_run(rid)
    if not old:
        raise HTTPException(404, "run not found")
    ds = STORE.get_dataset(old.get("dataset_id"))
    conn = STORE.get_connector(old.get("connector_id"))
    if not ds or not conn:
        raise HTTPException(400, "датасет или коннектор этого прогона больше не существует")
    _validate_connector_dataset(conn, ds)
    cfg = old.get("config") or {}
    requested_concurrency = _requested_api_concurrency(cfg.get("requested_concurrency", cfg.get("concurrency") or 1))
    api_concurrency = _effective_api_concurrency(requested_concurrency)
    api_concurrency_limit = _api_concurrency_limit()
    cfg = {**cfg, "concurrency": api_concurrency, "requested_concurrency": requested_concurrency,
           "api_concurrency_limit": api_concurrency_limit,
           "impala_concurrency_limit": _impala_concurrency()}
    judge_cfg = None
    if _judge_enabled():
        judge_cfg = _judge_cfg_from_env()
        if not judge_cfg:
            raise HTTPException(400, "LLM judge не настроен для repeat — задайте env LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.")
        cfg = {**cfg, "auto_judge": True, "judge_model": os.getenv("LLM_MODEL"),
               "judge_timeout": _judge_timeout(), "judge_concurrency": _judge_concurrency(),
               "judge_max_retries": _judge_max_retries(), "judge_retry_delay": _judge_retry_delay()}
    else:
        cfg = {**cfg, "auto_judge": False}
    run = STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                           connector_id=conn["id"], connector_name=conn["name"], config=cfg)
    append_run_log(run["id"], "run_created", run=compact_run(run), config=cfg, repeated_from=rid)
    bus.publish(_redacted({"type": "run", "run": run}))
    if _use_worker_runner():
        _enqueue_run_job(run["id"], "run", {"source": "repeat", "repeated_from": rid})
    else:
        _track_task(asyncio.create_task(run_task(STORE, run["id"], ds, conn,
                                                 concurrency=api_concurrency,
                                                 api_global_concurrency=api_concurrency_limit,
                                                 max_attempts=cfg.get("max_attempts"), retry_delay=cfg.get("retry_delay"),
                                                 case_timeout=cfg.get("case_timeout", 600),
                                                 judge_cfg=judge_cfg,
                                                 judge_timeout=_judge_timeout(),
                                                 judge_concurrency=_judge_concurrency(),
                                                 judge_max_retries=_judge_max_retries(),
                                                 judge_retry_delay=_judge_retry_delay())), run["id"])
    return _public_run(run)


# ---------------- live progress over WebSocket ----------------
@app.websocket("/ws/progress")
async def ws_progress(ws: WebSocket):
    q = None
    try:
        await ws.accept()
        q = bus.subscribe()
        await ws.send_json(await asyncio.to_thread(_progress_snapshot_message))
        snapshot_interval = max(0.0, _env_float("BENCH_APP_WS_SNAPSHOT_INTERVAL_S", 2.0))
        while True:
            try:
                # In worker mode progress events are written to SQLite by a
                # different process, so periodically send durable snapshots.
                timeout = snapshot_interval if snapshot_interval else 30.0
                msg = await asyncio.wait_for(q.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if snapshot_interval:
                    await ws.send_json(await asyncio.to_thread(_progress_snapshot_message))
                else:
                    await ws.send_json({"type": "ping"})
                continue
            await ws.send_json(_redacted(msg))
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        if q is not None:
            bus.unsubscribe(q)

@app.get("/api/runs")
def list_runs(dataset_id: str | None = None):
    return [_public_run(r) for r in STORE.list_runs(dataset_id)]

@app.get("/api/runs/{rid}")
def get_run(rid: str, cases: int = 1, case_payload: int = 1):
    run = STORE.get_run(rid)
    if not run:
        raise HTTPException(404, "run not found")
    if cases:
        run = {**run, "cases": _public_cases(STORE.list_cases(rid, include_payload=bool(case_payload)))}
    return _public_run(run)


@app.get("/api/runs/{rid}/cases/{case_id}")
def get_run_case(rid: str, case_id: str):
    if not STORE.get_run(rid):
        raise HTTPException(404, "run not found")
    case = STORE.get_case(rid, case_id)
    if not case:
        raise HTTPException(404, "case not found")
    return _public_case(case)

@app.delete("/api/runs/{rid}")
def delete_run(rid: str):
    set_control(rid, "stop")   # if it's still running, make the task bail before we delete its rows
    _cancel_run_tasks(rid)
    try:
        STORE.cancel_jobs_for_run(rid)
    except Exception:
        pass
    bus.clear_run(rid)
    STORE.delete_run(rid)
    for p in (run_json_path(rid), answers_json_path(rid), str(JUDGED_DIR / f"{rid}.json"),
              str(JUDGED_DIR / f"{rid}.levels.json"), run_log_path(rid)):
        try:
            os.remove(p)
        except OSError:
            pass
    return {"ok": True}

@app.get("/api/runs/{rid}/logs")
def get_run_logs(rid: str, limit: int = 1000):
    run = STORE.get_run(rid)
    if not run:
        raise HTTPException(404, "run not found")
    limit = max(1, min(int(limit or 1000), 10000))
    return _redacted({"run_id": rid, "path": run_log_path(rid), "events": read_run_log(rid, limit=limit)})

@app.get("/api/runs/{rid}/logs/download")
def download_run_logs(rid: str):
    run = STORE.get_run(rid)
    if not run:
        raise HTTPException(404, "run not found")
    if not Path(run_log_path(rid)).exists():
        append_run_log(rid, "log_created_on_download", run=compact_run(run))
    return JSONResponse(
        content={"run_id": rid, "events": read_run_log(rid)},
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="benchmark_run_{rid}.json"'},
    )

@app.post("/api/runs/{rid}/pause")
def pause_run(rid: str):
    run = STORE.get_run(rid)
    if not run:
        raise HTTPException(404, "run not found")
    set_control(rid, "paused")
    if run.get("status") in {"queued", "running", "judging"}:
        STORE.update_run(rid, status="paused")
    run = STORE.get_run(rid)
    append_run_log(rid, "run_pause_requested", run=compact_run(run))
    bus.publish(_redacted({"type": "run", "run": run}))
    return {"ok": True, "status": "paused", "run": _public_run(run)}

@app.post("/api/runs/{rid}/resume")
def resume_run(rid: str):
    run = STORE.get_run(rid)
    if not run:
        raise HTTPException(404, "run not found")
    set_control(rid, "running")
    if run.get("status") == "paused":
        STORE.update_run(rid, status="running")
    if _use_worker_runner() and not STORE.list_jobs(run_id=rid, statuses=("queued", "running"), limit=1):
        _enqueue_run_job(rid, "continue_run", {"source": "resume"})
    run = STORE.get_run(rid)
    append_run_log(rid, "run_resume_requested", run=compact_run(run))
    bus.publish(_redacted({"type": "run", "run": run}))
    return {"ok": True, "status": "running", "run": _public_run(run)}

@app.post("/api/runs/{rid}/stop")
def stop_run(rid: str):
    run = STORE.get_run(rid)
    if not run:
        raise HTTPException(404, "run not found")
    if run.get("status") not in ("queued", "running", "paused", "judging"):
        return {"ok": True, "status": run.get("status"), "cancelled_tasks": 0, "run": _public_run(run)}
    set_control(rid, "stop")
    cancelled_jobs = STORE.cancel_jobs_for_run(rid) if _use_worker_runner() else 0
    cancelled = _cancel_run_tasks(rid)
    run = _mark_run_stopped(rid)
    return {"ok": True, "status": "stopped", "cancelled_tasks": cancelled,
            "cancelled_jobs": cancelled_jobs, "run": _public_run(run)}


class RerunCaseReq(BaseModel):
    case_id: str


def _rerun_judge_cfg_or_error():
    if not _judge_enabled():
        return None
    judge_cfg = _judge_cfg_from_env()
    if not judge_cfg:
        raise HTTPException(400, "LLM judge не настроен — задайте env LLM_BASE_URL / LLM_API_KEY / LLM_MODEL "
                             "для процесса bench_app и перезапустите.")
    return judge_cfg


def _rerun_job_payload(source: str, **extra) -> dict:
    payload = {
        "source": source,
        "auto_judge": _judge_enabled(),
        "api_concurrency_limit": _api_concurrency_limit(),
        "judge_timeout": _judge_timeout(),
        "judge_concurrency": _judge_concurrency(),
        "judge_max_retries": _judge_max_retries(),
        "judge_retry_delay": _judge_retry_delay(),
    }
    payload.update(extra)
    return payload


def _mark_run_queued_for_rerun(rid: str) -> None:
    STORE.update_run(rid, status="queued", finished_at=None, error=None)


def _existing_dataset_case_id(dataset: dict, case_id: str) -> str:
    clean = str(case_id or "").strip()
    if not clean:
        raise HTTPException(400, "case_id is required")
    try:
        cases = parse_benchmark_file(dataset["benchmark_path"])
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"не удалось прочитать датасет этого прогона: {safe_exception(exc, limit=300)}") from exc
    if not any(case.case_id == clean for case in cases):
        raise HTTPException(404, f"case_id не найден в текущем датасете: {clean}")
    return clean


@app.post("/api/runs/{rid}/rerun")
async def rerun_failed(rid: str):
    """Re-run missing and non-L4 questions of an existing run, in place."""
    _run_deps_or_error(rid)
    n = count_rerun_targets(STORE, rid)
    if n <= 0:
        return {"ok": True, "status": "no_targets", "targets": 0}
    judge_cfg = _rerun_judge_cfg_or_error()
    _mark_run_queued_for_rerun(rid)
    if _use_worker_runner():
        job = _enqueue_run_job(rid, "rerun", _rerun_job_payload("rerun_failed"))
        return {"ok": True, "status": "queued", "targets": n, "job_id": job.get("id")}
    _track_task(asyncio.create_task(rerun(STORE, rid, api_global_concurrency=_api_concurrency_limit(),
                                          judge_cfg=judge_cfg,
                                          judge_timeout=_judge_timeout(),
                                          judge_max_retries=_judge_max_retries(),
                                          judge_retry_delay=_judge_retry_delay(),
                                          judge_global_concurrency=_judge_concurrency())), rid)
    return {"ok": True, "status": "rerunning", "targets": n}

@app.post("/api/runs/{rid}/rerun-case")
async def rerun_one(rid: str, req: RerunCaseReq):
    """Re-run connector/API for a single question, then judge it again when env judge is enabled."""
    _run, dataset, _connector = _run_deps_or_error(rid)
    case_id = _existing_dataset_case_id(dataset, req.case_id)
    judge_cfg = _rerun_judge_cfg_or_error()
    _mark_run_queued_for_rerun(rid)
    if _use_worker_runner():
        job = _enqueue_run_job(rid, "rerun_case", _rerun_job_payload("rerun_case", case_id=case_id))
        return {"ok": True, "status": "queued", "case_id": case_id, "job_id": job.get("id")}
    _track_task(asyncio.create_task(rerun_api_case(STORE, rid, case_id, judge_cfg=judge_cfg,
                                                   judge_timeout=_judge_timeout(),
                                                   judge_max_retries=_judge_max_retries(),
                                                   judge_retry_delay=_judge_retry_delay(),
                                                   api_global_concurrency=_api_concurrency_limit(),
                                                   judge_global_concurrency=_judge_concurrency())), rid)
    return {"ok": True, "status": "rerunning_api", "case_id": case_id}


@app.post("/api/runs/{rid}/judge-case")
async def rejudge_one(rid: str, req: RerunCaseReq):
    """Re-run only the LLM L0-L4 assessment for an already collected case."""
    if not STORE.get_run(rid):
        raise HTTPException(404, "run not found")
    judge_cfg = _judge_cfg_from_env()
    if not judge_cfg:
        raise HTTPException(400, "LLM judge не настроен — задайте env LLM_BASE_URL / LLM_API_KEY / LLM_MODEL "
                             "для процесса bench_app и перезапустите.")
    if _use_worker_runner():
        job = _enqueue_run_job(rid, "judge_case", {"case_id": req.case_id})
        return {"ok": True, "status": "queued", "case_id": req.case_id, "job_id": job.get("id")}
    _track_task(asyncio.create_task(judge_existing_case(STORE, rid, req.case_id, judge_cfg,
                                                        judge_timeout=_judge_timeout(),
                                                        judge_max_retries=_judge_max_retries(),
                                                        judge_retry_delay=_judge_retry_delay(),
                                                        judge_global_concurrency=_judge_concurrency())), rid)
    return {"ok": True, "status": "rejudging", "case_id": req.case_id}

class GradeReq(BaseModel):
    case_id: str
    level: int | None = None     # 0–4 human override; null clears it (back to auto)


@app.post("/api/runs/{rid}/grade")
def set_grade(rid: str, req: GradeReq):
    """Manually override a case's level. Stored as human_level alongside the auto
    level; the leaderboard counts the human grade when set, and both are shown."""
    run = STORE.get_run(rid)
    if not run:
        raise HTTPException(404, "run not found")
    if req.level is not None and req.level not in (0, 1, 2, 3, 4):
        raise HTTPException(400, "level must be 0..4 or null")
    STORE.set_case_grade(rid, req.case_id, req.level)
    STORE.update_run(rid, summary=_summary_from_cases(STORE.list_cases(rid, include_payload=False), run.get("total_cases")))
    return {"ok": True, "case_id": req.case_id, "human_level": req.level}


@app.get("/api/runs/{rid}/result")
def run_result(rid: str):
    if not STORE.get_run(rid):
        raise HTTPException(404, "run not found")
    return _redacted(build_result(STORE, rid))


@app.get("/api/runs/{rid}/answers")
def run_answers(rid: str):
    if not STORE.get_run(rid):
        raise HTTPException(404, "run not found")
    return _redacted(build_answers(STORE, rid))


@app.get("/api/runs/{rid}/answers/download")
def download_answers(rid: str):
    if not STORE.get_run(rid):
        raise HTTPException(404, "run not found")
    return _json_download(build_answers(STORE, rid), f"benchmark_answers_{rid}.json")


# ---- LLM judge: result JSON -> final judged JSON (per-case L1..L4 assessment) ----
JUDGED_DIR = Path(os.getenv("BENCH_APP_JUDGED_DIR", os.path.join(os.getenv("BENCH_APP_DATA_DIR", "bench_app/data"), "judged")))


@app.post("/api/runs/{rid}/judge-levels")
async def judge_levels(rid: str, req: JudgeLevelsReq | None = None):
    """Run the LLM scoring stage over raw answers and write L0-L4 into the run."""
    run = STORE.get_run(rid)
    if not run:
        raise HTTPException(404, "run not found")
    cfg = _judge_cfg_from_env()
    if not cfg:
        raise HTTPException(400, "LLM judge не настроен — задайте env LLM_BASE_URL / LLM_API_KEY / LLM_MODEL "
                             "для процесса bench_app и перезапустите.")
    answers = build_answers(STORE, rid)
    timeout = _judge_timeout()
    concurrency = _judge_concurrency()
    if _use_worker_runner():
        job = _enqueue_run_job(rid, "judge_levels", {"cases": len(answers.get("cases") or [])})
        return {"ok": True, "status": "queued", "cases": len(answers.get("cases") or []),
                "model": cfg["model"], "job_id": job.get("id")}

    async def task():
        try:
            STORE.update_run(rid, status="judging")
            bus.publish(_redacted({"type": "run", "run": STORE.get_run(rid)}))
            judged = await judge_answers(answers, cfg, timeout=timeout, concurrency=concurrency,
                                         judged_at=time.time(), max_retries=_judge_max_retries(),
                                         retry_delay=_judge_retry_delay())
            if (judged.get("judge_summary") or {}).get("invalid"):
                STORE.update_run(rid, status="error", finished_at=time.time(),
                                 error=f"LLM judge returned invalid levels: {judged['judge_summary']['invalid']}")
                bus.publish(_redacted({"type": "run", "run": STORE.get_run(rid)}))
                return
            apply_judged_levels(STORE, rid, judged)
            STORE.update_run(rid, status="done", finished_at=time.time(),
                             summary=_summary_from_cases(STORE.list_cases(rid, include_payload=False), run.get("total_cases")))
            from bench_app.runner import _dump_json
            _dump_json(STORE, rid)
            JUDGED_DIR.mkdir(parents=True, exist_ok=True)
            (JUDGED_DIR / f"{rid}.levels.json").write_text(json.dumps(judged, ensure_ascii=False, indent=2),
                                                           encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            STORE.update_run(rid, status="error", finished_at=time.time(),
                             error=safe_exception(exc, limit=300))
        bus.publish(_redacted({"type": "run", "run": STORE.get_run(rid)}))

    _track_task(asyncio.create_task(task()), rid)
    return {"ok": True, "status": "judging", "cases": len(answers.get("cases") or []), "model": cfg["model"]}

@app.post("/api/runs/{rid}/judge")
async def judge_run(rid: str):
    if not STORE.get_run(rid):
        raise HTTPException(404, "run not found")
    cfg = llm_config()
    if not cfg:
        raise HTTPException(400, "LLM не настроен — задайте env LLM_BASE_URL / LLM_API_KEY / LLM_MODEL для процесса bench_app и перезапустите.")
    result = build_result(STORE, rid)
    if _use_worker_runner():
        job = _enqueue_run_job(rid, "judge_legacy", {"cases": len(result.get("cases") or [])})
        return {"ok": True, "status": "queued", "cases": len(result.get("cases") or []),
                "model": cfg["model"], "job_id": job.get("id")}

    async def task():
        judged = await judge_result(result, cfg, timeout=_judge_timeout(),
                                    concurrency=_judge_concurrency(), judged_at=time.time())
        JUDGED_DIR.mkdir(parents=True, exist_ok=True)
        (JUDGED_DIR / f"{rid}.json").write_text(json.dumps(judged, ensure_ascii=False, indent=2), encoding="utf-8")

    _track_task(asyncio.create_task(task()), rid)
    return {"ok": True, "status": "judging", "cases": len(result.get("cases") or []), "model": cfg["model"]}

@app.get("/api/runs/{rid}/judged")
def get_judged(rid: str):
    p = JUDGED_DIR / f"{rid}.json"
    if not p.exists():
        raise HTTPException(404, "ещё не пройдено LLM-судьёй (POST /api/runs/{id}/judge)")
    doc = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(doc.get("cases"), list):
        doc["cases"] = _format_cases(doc["cases"])
    return _redacted(doc)


@app.get("/api/runs/{rid}/judged-levels")
def get_judged_levels(rid: str):
    p = JUDGED_DIR / f"{rid}.levels.json"
    if not p.exists():
        raise HTTPException(404, "ещё не пройдена L0-L4 оценка (POST /api/runs/{id}/judge-levels)")
    doc = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(doc.get("cases"), list):
        doc["cases"] = _format_cases(doc["cases"])
    return _redacted(doc)

@app.get("/api/runs/{rid}/download")
def download_run(rid: str):
    if not STORE.get_run(rid):
        raise HTTPException(404, "run not found")
    return _json_download(build_result(STORE, rid), f"benchmark_run_{rid}.json")


# ---- effective level = human override if set, else the auto level ----
def _eff_level(c):
    hl = c.get("human_level")
    return hl if hl is not None else c.get("level")


def _summary_from_cases(cases, total=None):
    """Recompute a run summary using EFFECTIVE levels (human override counts)."""
    cases = [case for case in cases if _case_collected_for_done_count(case)]
    total = total or len(cases)
    levels = {i: 0 for i in range(5)}
    for c in cases:
        lv = _eff_level(c)
        if lv in levels:
            levels[lv] += 1
    passed = levels[4]
    return {"accuracy": round(passed / max(1, total) * 100, 1), "passed": passed, "total": total,
            "done": len(cases), "L0": levels[0], "L1": levels[1], "L2": levels[2],
            "L3": levels[3], "L4": levels[4]}


# ---------------- results (revisions; latest default) ----------------
@app.get("/api/results")
def results(dataset_id: str, run_id: str | None = None):
    runs = STORE.list_runs(dataset_id)
    done = [r for r in runs if r["status"] == "done"] or runs
    if not done:
        raise HTTPException(404, "no runs for dataset")
    target = next((r for r in runs if r["id"] == run_id), None) if run_id else done[0]
    if not target:
        raise HTTPException(404, "run_id not found")
    tcases = STORE.list_cases(target["id"])
    target = {**target, "summary": _summary_from_cases(tcases, target.get("total_cases"))}
    return _redacted({"run": target, "cases": _format_cases(tcases),
            "revisions": [{"id": r["id"], "created_at": r["created_at"], "status": r["status"],
                           "connector_name": r["connector_name"],
                           "accuracy": (r.get("summary") or {}).get("accuracy") if isinstance(r.get("summary"), dict) else None}
                          for r in runs]})


# ---------------- leaderboard (models × benchmarks) ----------------
@app.get("/api/leaderboard")
def leaderboard():
    datasets = STORE.list_datasets()
    ds_names = [d["name"] for d in datasets]
    matrix: dict = {}  # model -> {dataset_name -> cell}
    for ds in datasets:
        seen = set()
        for r in STORE.list_runs(ds["id"]):  # newest first
            if r["status"] != "done":
                continue
            m = r["connector_name"]
            if m in seen:
                continue
            seen.add(m)
            s = r.get("summary") or {}
            matrix.setdefault(m, {})[ds["name"]] = {
                "accuracy": s.get("accuracy"), "passed": s.get("passed"),
                "total": s.get("total"), "run_id": r["id"], "when": r["created_at"]}
    rows = []
    for m, cells in matrix.items():
        accs = [c["accuracy"] for c in cells.values() if c.get("accuracy") is not None]
        rows.append({"model": m, "cells": cells, "avg": round(sum(accs) / len(accs), 1) if accs else None,
                     "benches": len(cells)})
    rows.sort(key=lambda x: (x["avg"] is None, -(x["avg"] or 0)))
    return _redacted({"datasets": ds_names, "rows": rows})


# ---------------- per-benchmark comparison (old-dashboard structure) ----------------
@app.get("/api/compare")
def compare(dataset_id: str, include_unfinished: bool = False):
    ds = STORE.get_dataset(dataset_id)
    if not ds:
        raise HTTPException(404, "dataset not found")
    # by default only finished runs; optionally also in-progress (running/paused)
    ok = {"done", "running", "paused", "judging", "stopped"} if include_unfinished else {"done"}
    runs = [r for r in STORE.list_runs(dataset_id) if r["status"] in ok]
    # latest run per model (connector_name) + all revisions per model
    latest, revs_by_name = {}, {}
    for r in runs:  # newest first
        latest.setdefault(r["connector_name"], r)
        s = r.get("summary") or {}
        revs_by_name.setdefault(r["connector_name"], []).append({
            "run_id": r["id"], "created_at": r["created_at"], "status": r["status"],
            "accuracy": s.get("accuracy"), "passed": s.get("passed"), "total": s.get("total")})
    parts = []
    task_index = {}  # case_id -> {difficulty, question}
    for name, r in latest.items():
        cases = STORE.list_cases(r["id"], include_payload=False)
        cmap = {}
        for c in cases:
            cmap[c["case_id"]] = _redacted({"level": _eff_level(c), "auto_level": c.get("level"),
                                  "human_level": c.get("human_level"), "idx": c.get("idx"),
                                  "predicted_sql": c.get("predicted_sql"),
                                  "error": c.get("error"), "elapsed_s": c.get("elapsed_s")})
            task_index.setdefault(c["case_id"], {"difficulty": c.get("difficulty"), "question": c.get("question"),
                                                 "gold_sql": c.get("gold_sql")})
        s = _summary_from_cases(cases, r.get("total_cases"))   # effective-level (human override counts)
        times = sorted(c.get("elapsed_s") or 0 for c in cases)
        median = (times[len(times) // 2] if len(times) % 2 else
                  (times[len(times) // 2 - 1] + times[len(times) // 2]) / 2) if times else 0
        parts.append({"name": name, "run_id": r["id"], "when": r["created_at"], "status": r["status"],
                      "summary": s, "cases": cmap,
                      "elapsed_total": round(sum(times), 1),
                      "median_elapsed": round(median, 1),
                      "revisions": revs_by_name.get(name, [])})
    parts.sort(key=lambda p: -((p["summary"] or {}).get("accuracy") or 0))
    # order tasks by difficulty then case_id
    diff_order = {"Simple": 0, "Moderate": 1, "Hard": 2, "Extra-Hard": 3}
    tasks = [{"case_id": cid, **info} for cid, info in task_index.items()]
    tasks.sort(key=lambda t: (diff_order.get(t.get("difficulty"), 9), t["case_id"]))
    return _redacted({"dataset": {"name": ds["name"], "db_id": ds.get("db_id"),
                        "db_type": ds.get("db_type") or "postgres", "meta": ds.get("meta") or {}},
            "task_count": len(tasks), "tasks": tasks, "participants": parts})


@app.get("/api/case")
def case_detail(dataset_id: str, case_id: str, run_ids: str = ""):
    """Per-case detail across models. By default uses the latest done run per
    model; `run_ids` (comma list) pins specific revisions for the models they
    belong to (so it follows the leaderboard's per-model revision selection)."""
    runs = [r for r in STORE.list_runs(dataset_id) if r["status"] == "done"]
    sel = set(x for x in run_ids.split(",") if x)
    latest = {}
    for r in runs:  # selected revisions win, then newest fills the rest
        if r["id"] in sel:
            latest[r["connector_name"]] = r
    for r in runs:
        latest.setdefault(r["connector_name"], r)
    gold = {"question": None, "gold_sql": None, "gold_result": None, "difficulty": None}
    models = []
    for name, r in latest.items():
        for c in STORE.list_cases(r["id"]):
            if c["case_id"] != case_id:
                continue
            if gold["gold_sql"] is None:
                gold = {"question": c.get("question"), "gold_sql": c.get("gold_sql"),
                        "gold_result": c.get("gold_result"), "difficulty": c.get("difficulty")}
            models.append(_redacted({"name": name, "run_id": r["id"], "level": _eff_level(c),
                           "auto_level": c.get("level"), "human_level": c.get("human_level"),
                           "predicted_sql": c.get("predicted_sql"), "agent_result": c.get("agent_result"),
                           "raw_response": c.get("raw_response"),
                           "error": c.get("error"), "elapsed_s": c.get("elapsed_s"), "reason": c.get("reason")}))
            break
    models.sort(key=lambda m: -(m["level"] or 0))
    return _redacted({"case_id": case_id, **gold, "models": models})


# ---------------- middleware: Vanna AI (mod) ----------------
# The modified Vanna speaks a streaming `chat_poll` contract: POST {message} ->
# {"chunks":[...]} where the SQL is buried in status_card chunks (metadata.sql)
# and authoritatively in the final text chunk after a `## FINAL_SQL` marker.
# That's awkward to express as a generic templated connector, so we expose a clean
# /sql shim here and point a normal `sql`-style connector at it — the bench core
# stays unchanged (it just sees question -> {sql}).
VANNA_MOD_URL = os.getenv("VANNA_MOD_URL", "http://194.87.86.14/vanna/api/vanna/v2/chat_poll")


class MwReq(BaseModel):
    question: str
    database: str = ""
    url: str = ""        # upstream URL — passed per-request so ONE middleware serves many instances
    api_key: str = ""    # optional upstream auth (e.g. Dify Bearer)


def _vanna_extract_sql(payload: dict) -> str | None:
    final, card = None, None
    for ch in (payload.get("chunks") or []):
        rich = ch.get("rich") if isinstance(ch, dict) else None
        if not isinstance(rich, dict):
            continue
        data = rich.get("data") or {}
        md = data.get("metadata") if isinstance(data, dict) else None
        if isinstance(md, dict) and md.get("sql"):
            card = md["sql"]                       # last executed run_sql
        if rich.get("type") == "text" and isinstance(data, dict):
            content = data.get("content") or ""
            if "## FINAL_SQL" in content:          # authoritative final answer
                block = content.split("## FINAL_SQL", 1)[1].strip()
                m = re.search(r"```(?:sql)?\s*(.*?)```", block, re.DOTALL | re.IGNORECASE)
                final = (m.group(1) if m else block).strip()
    return final or card


@app.post("/middleware/vanna-mod/sql")
async def vanna_mod_sql(r: MwReq):
    url = r.url or VANNA_MOD_URL   # upstream from the request → one middleware, many instances
    q = r.question + (f"\n\nUse the `{r.database}` database." if r.database else "")
    try:
        async with httpx.AsyncClient(verify=httpx_verify("CONNECTOR_SSL_VERIFY")) as client:
            resp = await client.post(url, headers={"Content-Type": "application/json"},
                                     json={"message": q}, timeout=200)
    except Exception as exc:  # noqa: BLE001
        return {"sql": None, "error": safe_exception(exc, extra_secrets=[url, r.api_key], limit=200)}
    if resp.status_code != 200:
        return {"sql": None, "error": f"HTTP {resp.status_code}: {redact_text(resp.text)[:200]}"}
    try:
        payload = resp.json()
    except Exception:
        return {"sql": None, "error": "non-JSON response from vanna-mod"}
    sql = (_vanna_extract_sql(payload) or "").strip() or None   # strict: SQL string or null
    return {"sql": sql, "error": None if sql else "no SQL found in chunks"}


# ---------------- middleware: QueryWeaver (Dify workflow) ----------------
# Direct blocking calls to the Dify app 504 (nginx cuts at 60s; the workflow runs
# minutes). Calling it in STREAMING mode keeps the connection alive (continuous
# SSE), so we consume the stream, accumulate the answer, and pull the SQL — from
# the final message answer or, as a fallback, from any node output that looks like
# SQL. The bench just sees a normal question -> {sql} endpoint.
QW_URL = os.getenv("QW_URL", "http://mas.144.91.85.207.nip.io:8080/v1/chat-messages")
QW_API_KEY = os.getenv("QW_API_KEY", "")


@app.post("/middleware/queryweaver/sql")
async def queryweaver_sql(r: MwReq):
    # REAL QueryWeaver (FalkorDB) on its own host — dev-auth + CSRF + /graphs/{db}.
    # (Previously this hit the Dify app via QW_URL, whose linker flaps; that was wrong.)
    sql, err, _ctx = await queryweaver_native_sql_ctx(r.question, r.database or "dm_mis")
    return {"sql": sql, "error": err}


# ---------------- solution reviews ----------------
REVIEWS = Path(os.getenv("BENCH_APP_REVIEWS_DIR", str(Path(__file__).parent / "reviews")))

def _norm_title(s: str) -> str:
    """Loose key so version variants ('MAS_FW API v1.3' → 'MAS_FW API') collapse
    onto one review, while parenthetical qualifiers ('(mod)' vs '(vanilla)') stay
    distinct so each keeps its own write-up."""
    import re as _re
    s = _re.sub(r"v\d+(\.\d+)*", "", s or "")
    return _re.sub(r"[^a-z0-9]", "", s.lower())

@app.get("/api/reviews")
def reviews():
    """Solution reviews: static markdown briefs from bench_app/reviews/, with any
    connector's own `description` (set on the model-creation form) overriding the
    matching brief — so each model's write-up is editable right where it's defined."""
    items = {}  # norm key -> entry (dict preserves insertion order)
    for f in sorted(REVIEWS.glob("*.md")):
        body = f.read_text(encoding="utf-8")
        title = next((ln[2:].strip() for ln in body.splitlines() if ln.startswith("# ")), f.stem)
        rid = f.stem.split("_", 1)[-1] if f.stem[:2].isdigit() else f.stem
        items[_norm_title(title)] = {"id": rid, "title": title, "body": body, "source": "file"}
    for c in STORE.list_connectors():
        desc = (c.get("description") or "").strip()
        if not desc:
            continue
        items[_norm_title(c["name"])] = {"id": "conn:" + c["id"], "title": c["name"],
                                         "body": desc, "source": "connector", "connector_id": c["id"]}
    return list(items.values())


class ReviewSave(BaseModel):
    id: str
    body: str

@app.post("/api/reviews/save")
def save_review(r: ReviewSave):
    """Edit a solution review in place: connector-sourced reviews persist to the
    connector's `description`; file-sourced briefs are rewritten on disk."""
    if r.id.startswith("conn:"):
        c = STORE.get_connector(r.id[5:])
        if not c:
            raise HTTPException(404, "connector not found")
        c["description"] = r.body
        STORE.save_connector(c)
        return {"ok": True, "source": "connector"}
    for f in REVIEWS.glob("*.md"):
        rid = f.stem.split("_", 1)[-1] if f.stem[:2].isdigit() else f.stem
        if rid == r.id:
            f.write_text(r.body, encoding="utf-8")
            return {"ok": True, "source": "file"}
    raise HTTPException(404, "review not found")


# ---------------- SPA ----------------
_NOCACHE = {"Cache-Control": "no-cache, must-revalidate"}   # always revalidate so edits show on a normal refresh
_NO_SPA_FALLBACK_SEGMENTS = {
    ".git", "_next", "admin", "api", "backend", "frontend", "login",
    "wp-admin", "wp-content", "wp-json", "wordpress", "xmlrpc.php",
}


def _should_spa_fallback(path: str) -> bool:
    parts = [part.lower() for part in path.strip("/").split("/") if part]
    if not parts:
        return True
    if any(part.startswith(".") or part.startswith("wp-") or part in _NO_SPA_FALLBACK_SEGMENTS for part in parts):
        return False
    # Unknown asset/probe paths should be a real 404; normal SPA routes have no extension.
    return "." not in parts[-1]

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers=_NOCACHE)

@app.get("/{path:path}")
def static_files(path: str):
    f = (STATIC / path).resolve()
    if STATIC.resolve() in f.parents and f.is_file():
        return FileResponse(f, headers=_NOCACHE)
    if not _should_spa_fallback(path):
        raise HTTPException(404, "not found")
    return FileResponse(STATIC / "index.html", headers=_NOCACHE)
