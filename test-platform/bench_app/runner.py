"""Run orchestration.

The preferred path is two-phase:
1. ask the connector and write raw answers (`bench-answers/v1`) without L0-L4;
2. call an LLM judge that assigns final L0-L4 and then write result JSON.

The old execution-match scorer is kept as a fallback for local tests and legacy
manual tooling when no judge config is supplied.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from urllib.parse import urlparse

import httpx

from leaderboard.benchmark import parse_benchmark_file
from leaderboard.comparator import eval_level
from leaderboard.db import PgExecutor, SelectResult
from leaderboard.redaction import redact_obj, redact_text, safe_exception
from bench_app.connectors import TemplatedConnector
from bench_app.bus import bus
from bench_app.http_client import httpx_verify
from bench_app.judge import judge_answers
from bench_app.run_logs import append_run_log, compact_case, compact_run
from bench_app.state_graph import CASE_STATUS_LABELS, RUN_ACTIVE_STATES

DATA_DIR = os.getenv("BENCH_APP_DATA_DIR", "bench_app/data")
RUNS_DIR = os.getenv("BENCH_APP_RUNS_DIR", os.path.join(DATA_DIR, "runs"))
ANSWERS_DIR = os.getenv("BENCH_APP_ANSWERS_DIR", os.path.join(DATA_DIR, "answers"))
JUDGED_DIR = os.getenv("BENCH_APP_JUDGED_DIR", os.path.join(DATA_DIR, "judged"))


def _emit_run(store, run_id, case=None):
    """Push a live progress event to any subscribed WebSocket clients."""
    try:
        run = store.get_run(run_id)
        if run is None:      # run was deleted mid-flight — don't emit a null event
            return
        try:
            append_run_log(run_id, "run", run=compact_run(run))
        except Exception:
            pass
        bus.publish(redact_obj({"type": "run", "run": run}))
        if case is not None:
            try:
                append_run_log(run_id, "case", case=compact_case(case))
            except Exception:
                pass
            bus.publish(redact_obj({"type": "case", "run_id": run_id, "case": case}))
    except Exception:
        pass


# run_id -> "paused" | "stop" (absent = run normally). Drives pause/stop buttons.
RUN_CONTROL: dict = {}
_GLOBAL_LIMITERS: dict[tuple[str, int, int], asyncio.Semaphore] = {}
_SCORING_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_SCORING_EXECUTOR_WORKERS = 0


def set_control(run_id: str, state: str | None):
    """state: 'paused' | 'running' (resume) | 'stop' | None (clear)."""
    if state in (None, "running"):
        RUN_CONTROL.pop(run_id, None)
    else:
        RUN_CONTROL[run_id] = state


def _run_status(store, run_id: str) -> str:
    try:
        run = store.get_run(run_id) or {}
        return str(run.get("status") or "")
    except Exception:
        return ""


def _stop_requested(store, run_id: str) -> bool:
    return RUN_CONTROL.get(run_id) == "stop" or _run_status(store, run_id) in {"stopped", "stop_requested", "cancelled"}


def _update_run_respecting_control(store, run_id: str, **updates) -> bool:
    """Update run progress without reviving a user-paused/stopped worker run.

    In Docker compose the stop/pause endpoint runs in the backend process while
    benchmark execution runs in the worker process. The DB status is therefore
    the only cross-process control signal. Long external calls may return after
    the user has already paused or stopped the run; in that case the worker must
    not overwrite the control status with a stale "running"/"judging" update.
    """
    if not updates:
        return True
    current = _run_status(store, run_id)
    if current in {"stopped", "stop_requested", "cancelled"}:
        return False
    clean = dict(updates)
    requested = str(clean.get("status") or "")
    if current == "paused" and requested in {"queued", "running", "judging"}:
        clean.pop("status", None)
        summary = clean.get("summary")
        if isinstance(summary, dict):
            clean["summary"] = {**summary, "status": "paused"}
    if clean:
        store.update_run(run_id, **clean)
    return True


def _global_limiter(name: str, limit: int) -> asyncio.Semaphore:
    """Process-wide limiter for long external calls across concurrent runs."""
    limit = max(1, int(limit or 1))
    loop_id = id(asyncio.get_running_loop())
    key = (name, loop_id, limit)
    sem = _GLOBAL_LIMITERS.get(key)
    if sem is None:
        stale = [k for k in _GLOBAL_LIMITERS if k[0] == name and k[1] == loop_id]
        for old in stale:
            _GLOBAL_LIMITERS.pop(old, None)
        sem = asyncio.Semaphore(limit)
        _GLOBAL_LIMITERS[key] = sem
    return sem


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def impala_concurrency_limit() -> int:
    return max(1, _env_int("BENCH_APP_MAX_IMPALA_CONCURRENCY", 1))


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _circuit_enabled() -> bool:
    return _env_bool("BENCH_APP_CIRCUIT_BREAKER_ENABLED", True)


def _circuit_threshold(kind: str) -> int:
    env = {
        "api": "BENCH_APP_CIRCUIT_BREAKER_API_FAILURES",
        "db": "BENCH_APP_CIRCUIT_BREAKER_DB_FAILURES",
        "llm": "BENCH_APP_CIRCUIT_BREAKER_LLM_FAILURES",
    }.get(kind, "BENCH_APP_CIRCUIT_BREAKER_FAILURES")
    return max(1, _env_int(env, _env_int("BENCH_APP_CIRCUIT_BREAKER_FAILURES", 5)))


class CircuitBreakerOpen(RuntimeError):
    def __init__(self, kind: str, threshold: int, detail: str | None = None):
        self.kind = kind
        self.threshold = threshold
        self.detail = redact_text(detail or "")
        super().__init__(
            f"circuit breaker opened for {kind}: {threshold} consecutive external failures"
            + (f"; last={self.detail[:300]}" if self.detail else "")
        )


class RunCircuitBreaker:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.enabled = _circuit_enabled()
        self.counts = {"api": 0, "db": 0, "llm": 0}
        self._lock = asyncio.Lock()

    async def record(self, kind: str, failed: bool, detail: str | None = None) -> None:
        if not self.enabled:
            return
        async with self._lock:
            if not failed:
                self.counts[kind] = 0
                return
            self.counts[kind] = int(self.counts.get(kind, 0)) + 1
            threshold = _circuit_threshold(kind)
            if self.counts[kind] >= threshold:
                raise CircuitBreakerOpen(kind, threshold, detail)


def _looks_like_external_timeout_or_transport(err: str | None) -> bool:
    text = (err or "").lower()
    if not text:
        return False
    needles = (
        "timeout", "timed out", "connecttimeout", "readtimeout",
        "connecterror", "connection refused", "connection reset",
        "network", "transport", "ttransport", "temporarily unavailable",
        "rate limit", "ratelimit", "429", "502", "503", "504",
    )
    return any(n in text for n in needles)


def _api_external_failure(api_err: str | None, *, timed_out: bool) -> bool:
    return bool(timed_out or _looks_like_external_timeout_or_transport(api_err))


def _db_external_failure(db_err: str | None) -> bool:
    text = (db_err or "").lower()
    if not text:
        return False
    # SQL/model mistakes are benchmark evidence, not infra failure.
    if "analysisexception" in text or "syntax" in text or "could not resolve column" in text:
        return False
    return _looks_like_external_timeout_or_transport(text)


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


def _scoring_dsn_points_to_container_localhost(dataset: dict) -> bool:
    if not _running_in_container():
        return False
    return _dsn_host(str(dataset.get("dsn") or "")) in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _ensure_scoring_dsn_allowed(dataset: dict) -> None:
    if not str(dataset.get("dsn") or "").strip():
        raise RuntimeError(f"DSN scoring-базы не задан для датасета {dataset.get('name') or dataset.get('id') or '?'}")
    if _scoring_dsn_points_to_container_localhost(dataset):
        raise RuntimeError(
            "DSN scoring-базы у датасета указывает на localhost/127.0.0.1. "
            "В Docker это сам контейнер backend/worker, а не внешняя БД. "
            "Задайте доступный DSN через env и обновите датасет."
        )


def _llm_external_failure(detail: str | None) -> bool:
    return _looks_like_external_timeout_or_transport(detail)


def _scoring_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _SCORING_EXECUTOR, _SCORING_EXECUTOR_WORKERS
    workers = max(1, _env_int("BENCH_APP_SCORING_THREADS", 4))
    if _SCORING_EXECUTOR is None or _SCORING_EXECUTOR_WORKERS != workers:
        old = _SCORING_EXECUTOR
        _SCORING_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="bench-scoring",
        )
        _SCORING_EXECUTOR_WORKERS = workers
        if old is not None:
            old.shutdown(wait=False, cancel_futures=True)
    return _SCORING_EXECUTOR


def _is_impala_executor(executor, dataset: dict | None = None) -> bool:
    db_type = str((dataset or {}).get("db_type") or "").lower().strip()
    if db_type == "impala":
        return True
    try:
        return executor._scheme() == "impala"
    except Exception:  # noqa: BLE001
        return False


async def execute_scoring_select(executor, dataset: dict | None, sql: str):
    async def run_in_thread():
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(_scoring_executor(), executor.execute_select, sql)
        try:
            return await task
        except asyncio.CancelledError:
            task.cancel()
            raise

    if _is_impala_executor(executor, dataset):
        sem = _global_limiter("impala_scoring_db", impala_concurrency_limit())
        await sem.acquire()
        loop = asyncio.get_running_loop()
        task = loop.run_in_executor(_scoring_executor(), executor.execute_select, sql)
        released = False

        def release_later(_future):
            loop.call_soon_threadsafe(sem.release)

        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(release_later)
            released = True
            raise
        finally:
            if not released:
                sem.release()
    return await run_in_thread()


async def _gate(store, run_id) -> bool:
    """Block while paused; return False if the run should STOP."""
    paused = False
    while RUN_CONTROL.get(run_id) == "paused" or _run_status(store, run_id) == "paused":
        if not paused:
            store.update_run(run_id, status="paused")
            _emit_run(store, run_id)
            paused = True
        await asyncio.sleep(1)
    if paused:
        if _stop_requested(store, run_id):
            return False
        store.update_run(run_id, status="running")
        _emit_run(store, run_id)
    return not _stop_requested(store, run_id)


def _ordered(conditions: str) -> bool:
    text = (conditions or "").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and "order" in obj:
                return bool(obj.get("order"))
        except Exception:
            pass
    return bool(re.search(r"order\s*=\s*true", conditions or "", re.IGNORECASE))


def _result_to_dict(res):
    if res is None:
        return None
    def cell(v):
        return "" if v is None else str(v)
    rows = [[cell(v) for v in r] for r in (res.rows or [])]
    return {"columns": res.columns, "rows": rows, "row_count": res.row_count,
            "truncated": False, "error": redact_text(res.error), "ok": res.ok}


def _progress_case(case, idx: int, status: str, label: str, *, elapsed_s: float | None = None) -> dict:
    """Transient row for WebSocket progress before the case is persisted."""
    rec = {
        "idx": idx,
        "case_id": case.case_id,
        "difficulty": case.difficulty,
        "question": case.question,
        "gold_sql": case.gold_sql,
        "predicted_sql": None,
        "level": None,
        "matched": False,
        "error": None,
        "reason": None,
        "elapsed_s": elapsed_s,
        "gold_result": None,
        "agent_result": None,
        "attempts": None,
        "raw_response": None,
        "case_status": status,
        "case_status_label": label,
    }
    return rec


def _with_case_status(rec: dict, status: str) -> dict:
    return {**rec, "case_status": status, "case_status_label": _case_status_label(status)}


def _persist_case_status(store, run_id: str, idx: int, rec: dict, status: str | None = None) -> dict:
    case = _with_case_status(rec, status) if status else dict(rec)
    store.replace_case(run_id, idx, case)
    return case


def _case_collected(case_rec: dict) -> bool:
    status = case_rec.get("case_status")
    if status == "api_waiting":
        return False
    return (
        case_rec.get("attempts") is not None
        or bool(case_rec.get("predicted_sql"))
        or bool(case_rec.get("error"))
        or case_rec.get("level") is not None
        or case_rec.get("gold_result") is not None
        or case_rec.get("agent_result") is not None
    )


def _collected_cases(case_recs) -> list[dict]:
    return [c for c in case_recs if _case_collected(c)]


ACTIVE_RUN_STATUSES = set(RUN_ACTIVE_STATES)
_GOLD_RESULT_CACHE: OrderedDict[str, object] = OrderedDict()
_GOLD_RESULT_CACHE_LOCK = threading.RLock()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disable", "disabled"}


def _gold_cache_enabled() -> bool:
    return _env_bool("BENCH_APP_GOLD_CACHE", True)


def _gold_cache_concurrency() -> int:
    try:
        return max(1, int(os.getenv("BENCH_APP_GOLD_CACHE_CONCURRENCY", "4")))
    except ValueError:
        return 4


def _gold_cache_max_entries() -> int:
    try:
        return max(0, int(os.getenv(
            "BENCH_APP_GOLD_CACHE_MEMORY_ENTRIES",
            os.getenv("BENCH_APP_GOLD_CACHE_MAX_ENTRIES", "0"),
        )))
    except ValueError:
        return 0


def _gold_cache_dir() -> str:
    return os.getenv("BENCH_APP_GOLD_CACHE_DIR", os.path.join(DATA_DIR, "gold_cache"))


def _gold_cache_key(dataset: dict, gold_sql: str) -> str:
    """Stable in-memory key without storing DSN credentials in the key itself."""
    material = json.dumps({
        "dsn": dataset.get("dsn") or "",
        "db_id": dataset.get("db_id") or "",
        "db_type": dataset.get("db_type") or "",
        "gold_sql": gold_sql or "",
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _gold_cache_get(key: str):
    with _GOLD_RESULT_CACHE_LOCK:
        result = _GOLD_RESULT_CACHE.get(key)
        if result is not None:
            _GOLD_RESULT_CACHE.move_to_end(key)
            return result
    path = os.path.join(_gold_cache_dir(), f"{key}.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            result = _select_result_from_json(json.load(f))
    except FileNotFoundError:
        return None
    except Exception:
        return None
    if not _gold_cache_result_cacheable(result):
        try:
            os.unlink(path)
        except OSError:
            pass
        return None
    _gold_memory_set(key, result)
    return result


def _gold_cache_result_cacheable(result) -> bool:
    return bool(getattr(result, "ok", False))


def _gold_memory_set(key: str, result):
    with _GOLD_RESULT_CACHE_LOCK:
        max_entries = _gold_cache_max_entries()
        if max_entries <= 0:
            return result
        _GOLD_RESULT_CACHE[key] = result
        _GOLD_RESULT_CACHE.move_to_end(key)
        while len(_GOLD_RESULT_CACHE) > max_entries:
            _GOLD_RESULT_CACHE.popitem(last=False)
        return result


def _select_result_to_json(result) -> dict:
    return {
        "ok": bool(getattr(result, "ok", False)),
        "columns": list(getattr(result, "columns", []) or []),
        "rows": [list(row) for row in (getattr(result, "rows", []) or [])],
        "row_count": int(getattr(result, "row_count", 0) or 0),
        "error": getattr(result, "error", None),
    }


def _select_result_from_json(data: dict) -> SelectResult:
    return SelectResult(
        ok=bool((data or {}).get("ok")),
        columns=list((data or {}).get("columns") or []),
        rows=[tuple(row) for row in ((data or {}).get("rows") or [])],
        row_count=int((data or {}).get("row_count") or 0),
        error=(data or {}).get("error"),
    )


def _gold_cache_set(key: str, result):
    if not _gold_cache_result_cacheable(result):
        return result
    cache_dir = _gold_cache_dir()
    try:
        os.makedirs(cache_dir, exist_ok=True)
        path = os.path.join(cache_dir, f"{key}.json")
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_select_result_to_json(result), f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass
    return _gold_memory_set(key, result)


async def _get_gold_result(executor, dataset: dict, gold_sql: str):
    if not _gold_cache_enabled():
        return await execute_scoring_select(executor, dataset, gold_sql)
    key = _gold_cache_key(dataset, gold_sql)
    cached = _gold_cache_get(key)
    if cached is not None:
        return cached
    result = await execute_scoring_select(executor, dataset, gold_sql)
    return _gold_cache_get(key) or _gold_cache_set(key, result)


async def _prewarm_gold_cache(executor, dataset: dict, cases) -> int:
    """Execute missing gold SQL once up front and keep results in process memory."""
    if not _gold_cache_enabled():
        return 0
    todo: list[str] = []
    seen: set[str] = set()
    for case in cases:
        sql = case.gold_sql
        key = _gold_cache_key(dataset, sql)
        if key in seen or _gold_cache_get(key) is not None:
            continue
        seen.add(key)
        todo.append(sql)
    if not todo:
        return 0
    sem = asyncio.Semaphore(min(_gold_cache_concurrency(), len(todo)))

    async def warm(sql: str) -> None:
        async with sem:
            await _get_gold_result(executor, dataset, sql)

    await asyncio.gather(*(warm(sql) for sql in todo))
    return len(todo)


def _case_status_label(status: str | None) -> str:
    return CASE_STATUS_LABELS.get(status or "", status or "")


def _log_api_attempt(run_id: str | None, event: str, case, idx: int, attempt: int, **fields):
    if not run_id:
        return
    append_run_log(
        run_id,
        event,
        case={
            "idx": idx,
            "case_id": case.case_id,
            "difficulty": case.difficulty,
            "question": case.question,
        },
        attempt=attempt,
        **redact_obj(fields),
    )


def _case_error_status(rec: dict) -> str | None:
    err = rec.get("error") or ""
    gold = rec.get("gold_result") or {}
    agent = rec.get("agent_result") or {}
    if gold.get("error"):
        return "gold_error"
    if "тайм-аут" in err.lower() or "timeout" in err.lower():
        return "api_timeout"
    if not rec.get("predicted_sql"):
        return "no_sql" if not err else "api_error"
    if agent.get("error"):
        return "sql_error"
    if err:
        return "api_error"
    return None


def _answers_doc_for_case(store, run_id: str, dataset: dict, connector: dict, rec: dict) -> dict:
    """Build a valid bench-answers/v1 payload for a single freshly collected case."""
    run = store.get_run(run_id) or {}
    return redact_obj({
        "schema": ANSWERS_SCHEMA,
        "run_id": run_id,
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "benchmark": {
            "dataset_id": run.get("dataset_id"),
            "name": run.get("dataset_name") or dataset.get("name"),
            "db_id": dataset.get("db_id"),
            "db_type": dataset.get("db_type") or "postgres",
        },
        "model": {
            "name": run.get("connector_name") or connector.get("name"),
            "connector_id": run.get("connector_id"),
            "dialect": connector.get("default_dialect") or "postgres",
            "endpoint": connector.get("url"),
        },
        "cases": [_answer_case(rec)],
    })


def _judged_levels_doc(store, run_id: str, dataset: dict, connector: dict, judged_cases: list[dict],
                       judge_cfg: dict | None, judged_at: float | None = None) -> dict:
    """Compose the full judged-levels artifact from per-case judge results."""
    base = build_answers(store, run_id, dataset, connector)
    order = {c.get("case_id"): i for i, c in enumerate(base.get("cases") or [])}
    cases = sorted(judged_cases, key=lambda c: order.get(c.get("case_id"), 10**9))
    by_level = {f"L{i}": 0 for i in range(5)}
    invalid = 0
    for c in cases:
        lv = c.get("level")
        if lv in (0, 1, 2, 3, 4):
            by_level[f"L{lv}"] += 1
        else:
            invalid += 1
    return redact_obj({
        **base,
        "schema": "bench-judged-levels/v1",
        "judge": {"model": (judge_cfg or {}).get("model"), "judged_at": judged_at},
        "cases": cases,
        "judge_summary": {
            "levels": by_level,
            "invalid": invalid,
            "cases_judged": len(cases),
        },
    })


async def run_task(store, run_id: str, dataset: dict, connector: dict, concurrency: int = 1,
                   api_global_concurrency: int = 1,
                   max_attempts=None, retry_delay=None, case_timeout=120,
                   judge_cfg: dict | None = None, judge_timeout: float = 60,
                   judge_concurrency: int = 1, judge_max_retries: int = 0,
                   judge_retry_delay: float = 0.0):
    """Background task: execute the whole benchmark for one connector.
    concurrency = how many questions to process at once (default 1 = strictly
    one-after-another; >1 runs that many in parallel). max_attempts/retry_delay
    override the connector's defaults for this run (None = use connector's)."""
    judge_tasks: set[asyncio.Task] = set()
    if not _update_run_respecting_control(store, run_id, status="running", started_at=time.time()):
        return
    try:
        cases = parse_benchmark_file(dataset["benchmark_path"])
        total = len(cases)
        store.update_run(run_id, total_cases=total)
        _emit_run(store, run_id)
        _ensure_scoring_dsn_allowed(dataset)
        executor = PgExecutor(dataset["dsn"], statement_timeout_ms=30000)
        dialect = connector.get("default_dialect", "postgres")
        conn = TemplatedConnector(connector)
        timeout = float(connector.get("timeout", 200))
        max_attempts = int(max_attempts if max_attempts is not None else connector.get("max_attempts", 1))
        retry_delay = float(retry_delay if retry_delay is not None else (connector.get("retry_delay", 0) or 0))
        concurrency = max(1, int(concurrency or 1))
        api_global_concurrency = max(1, int(api_global_concurrency or 1))

        set_control(run_id, None)
        st = {"passed": 0, "done": 0, "judged": 0, "judge_started": 0, "judge_errors": 0, "stopped": False}
        sem = asyncio.Semaphore(concurrency)
        judge_sem = asyncio.Semaphore(max(1, int(judge_concurrency or 1)))
        api_global_sem = _global_limiter("connector_api", api_global_concurrency)
        judge_global_sem = _global_limiter("llm_judge", judge_concurrency)
        circuit = RunCircuitBreaker(run_id)
        levels = {0: 0, 1: 0, 2: 0, 3: 0, 4: 0}
        judged_cases: list[dict] = []
        judged_at = None
        state_lock = asyncio.Lock()

        def track_judge_task(task: asyncio.Task) -> None:
            judge_tasks.add(task)
            task.add_done_callback(judge_tasks.discard)

        if _gold_cache_enabled():
            store.update_run(run_id, summary={"status": "warming_gold", "total": total, "done": 0})
            _emit_run(store, run_id)
            warmed = await _prewarm_gold_cache(executor, dataset, cases)
            if warmed:
                append_run_log(run_id, "gold_cache", warmed=warmed, total=total)

        def progress_summary(status: str) -> dict:
            summary = {
                "status": status,
                "total": total,
                "done": st["done"],
                "judged": st["judged"],
                "judge_errors": st["judge_errors"],
                "awaiting_judge": max(0, st["done"] - st["judged"] - st["judge_errors"]),
                "llm_queued": max(0, st["done"] - st["judge_started"]),
                "llm_in_progress": max(0, st["judge_started"] - st["judged"] - st["judge_errors"]),
            }
            if st["judged"]:
                summary.update({
                    "accuracy": round(st["passed"] / max(1, st["judged"]) * 100, 1),
                    "passed": st["passed"],
                    "L0": levels[0], "L1": levels[1], "L2": levels[2],
                    "L3": levels[3], "L4": levels[4],
                })
            return summary

        async with httpx.AsyncClient(verify=httpx_verify("CONNECTOR_SSL_VERIFY")) as client:
            async def judge_case(idx, case, rec):
                nonlocal judged_at
                try:
                    queued = _persist_case_status(store, run_id, idx, rec, "llm_queued")
                    _emit_run(store, run_id, case=queued)
                    async with judge_sem:
                        async with judge_global_sem:
                            async with state_lock:
                                st["judge_started"] += 1
                                store.update_run(run_id, summary=progress_summary("judging"))
                            sent = _persist_case_status(store, run_id, idx, rec, "sent_to_judge")
                            _emit_run(store, run_id, case=sent)
                            judging = _persist_case_status(store, run_id, idx, rec, "judging")
                            _emit_run(store, run_id, case=judging)
                            judged = await judge_answers(_answers_doc_for_case(store, run_id, dataset, connector, rec),
                                                         judge_cfg, timeout=judge_timeout, concurrency=1,
                                                         judged_at=time.time(), max_retries=judge_max_retries,
                                                         retry_delay=judge_retry_delay)

                    async with state_lock:
                        if _stop_requested(store, run_id):
                            st["stopped"] = True
                            return
                        if (judged.get("judge_summary") or {}).get("invalid"):
                            detail = _judge_failure_detail(judged)
                            await circuit.record("llm", _llm_external_failure(detail), detail)
                            err_msg = "LLM judge returned invalid level"
                            bad = {**rec, "case_status": "judge_error",
                                   "case_status_label": "ошибка оценки",
                                   "error": (rec.get("error") or err_msg)}
                            store.replace_case(run_id, idx, bad)
                            st["judge_errors"] += 1
                            store.update_run(run_id, summary=progress_summary("judge_error"))
                            _emit_run(store, run_id, case=bad)
                            return

                        apply_judged_levels(store, run_id, judged)
                        await circuit.record("llm", False)
                        judged_cases.extend(judged.get("cases") or [])
                        updated = next((c for c in store.list_cases(run_id)
                                        if c.get("case_id") == rec.get("case_id")), rec)
                        lv = updated.get("level")
                        if lv in levels:
                            levels[lv] += 1
                        if lv == 4:
                            st["passed"] += 1
                        st["judged"] += 1
                        judged_at = time.time()
                        store.update_run(run_id, summary=progress_summary(
                            "judging" if st["judged"] + st["judge_errors"] < st["done"] else "judged"))
                    judged_case = _persist_case_status(store, run_id, idx, updated, "judged")
                    _emit_run(store, run_id, case=judged_case)
                except CircuitBreakerOpen:
                    raise
                except Exception as exc:  # noqa: BLE001
                    err = safe_exception(exc, limit=500)
                    await circuit.record("llm", _llm_external_failure(err), err)
                    async with state_lock:
                        bad = {**rec, "case_status": "judge_error",
                               "case_status_label": "ошибка оценки",
                               "error": rec.get("error") or err[:200]}
                        store.replace_case(run_id, idx, bad)
                        st["judge_errors"] += 1
                        store.update_run(run_id, summary=progress_summary("judge_error"))
                    _emit_run(store, run_id, case=bad)

            async def work(idx, case):
                if st["stopped"] or not await _gate(store, run_id):
                    st["stopped"] = True
                    return
                async with sem:
                    if st["stopped"]:
                        return
                    waiting = _persist_case_status(
                        store, run_id, idx,
                        _progress_case(case, idx, "api_waiting", "ждем ответ API"),
                    )
                    _emit_run(store, run_id, case=waiting)
                    async with api_global_sem:
                        if judge_cfg:
                            rec = await _collect_case(executor, dataset, conn, client, case, idx, dialect,
                                                      timeout, max_attempts, dataset.get("db_id", ""),
                                                      retry_delay=retry_delay, case_timeout=case_timeout,
                                                      stop_check=lambda: _stop_requested(store, run_id),
                                                      run_id=run_id, circuit=circuit)
                        else:
                            rec = await _eval_case(executor, dataset, conn, client, case, idx, dialect,
                                                   timeout, max_attempts, dataset.get("db_id", ""),
                                                   retry_delay=retry_delay, case_timeout=case_timeout,
                                                   stop_check=lambda: _stop_requested(store, run_id),
                                                   run_id=run_id, circuit=circuit)
                if _stop_requested(store, run_id):
                    st["stopped"] = True
                    return
                if judge_cfg:
                    stage_status = _case_error_status(rec) or "llm_queued"
                    async with state_lock:
                        st["done"] += 1
                        d = st["done"]
                        rec = _persist_case_status(store, run_id, idx, rec, stage_status)
                        store.update_run(run_id, done_cases=d, summary=progress_summary("collecting_answers"))
                    _emit_run(store, run_id, case=rec)
                    track_judge_task(asyncio.create_task(judge_case(idx, case, rec)))
                else:
                    if rec["matched"]:
                        st["passed"] += 1
                    levels[rec["level"]] += 1
                    st["done"] += 1
                    d = st["done"]
                    done_status = _case_error_status(rec) or "done"
                    rec = _persist_case_status(store, run_id, idx, rec, done_status)
                    store.update_run(run_id, done_cases=d, summary={
                        "accuracy": round(st["passed"] / d * 100, 1), "passed": st["passed"], "total": total,
                        "done": d, "L0": levels[0], "L1": levels[1], "L2": levels[2],
                        "L3": levels[3], "L4": levels[4]})
                    _emit_run(store, run_id, case=rec)

            if concurrency <= 1:
                for idx, case in enumerate(cases, 1):
                    await work(idx, case)
                    if st["stopped"]:
                        break
            else:
                await asyncio.gather(*(work(i, c) for i, c in enumerate(cases, 1)))

            if judge_cfg and judge_tasks:
                async with state_lock:
                    pending = st["done"] - st["judged"] - st["judge_errors"]
                    if pending > 0:
                        _update_run_respecting_control(store, run_id, status="judging", summary=progress_summary("judging"))
                        _emit_run(store, run_id)
                await asyncio.gather(*list(judge_tasks))

        set_control(run_id, None)
        if judge_cfg:
            await _dump_answers_json_async(store, run_id, dataset, connector)
            if judged_cases:
                await _dump_judged_levels_json_async(run_id, _judged_levels_doc(
                    store, run_id, dataset, connector, judged_cases, judge_cfg,
                    judged_at=judged_at or time.time()))
            if st["stopped"]:
                store.update_run(run_id, status="stopped", finished_at=time.time(),
                                 summary={"status": "stopped", "total": total, "done": st["done"],
                                          "judged": st["judged"],
                                          "judge_errors": st["judge_errors"],
                                          "awaiting_judge": max(0, st["done"] - st["judged"] - st["judge_errors"]),
                                          "llm_queued": max(0, st["done"] - st["judge_started"]),
                                          "llm_in_progress": max(0, st["judge_started"] - st["judged"] - st["judge_errors"])})
                _emit_run(store, run_id)
                return
            final_summary = _summary(store.list_cases(run_id), total)
            final_summary["judged"] = st["judged"]
            final_summary["judge_errors"] = st["judge_errors"]
            final_summary["awaiting_judge"] = 0
            final_summary["llm_queued"] = 0
            final_summary["llm_in_progress"] = 0
            final_status = "error" if st["judge_errors"] else "done"
            final_error = f"ошибка оценки LLM в {st['judge_errors']} кейс(ах)" if st["judge_errors"] else None
            _update_run_respecting_control(store, run_id, status=final_status, finished_at=time.time(),
                                           summary=final_summary, error=final_error)
            await _dump_json_async(store, run_id, dataset, connector)
        else:
            final_cases = store.list_cases(run_id)
            _update_run_respecting_control(
                store, run_id, status="stopped" if st["stopped"] else "done", finished_at=time.time(),
                done_cases=len(_collected_cases(final_cases)), summary=_summary(final_cases, total)
            )
            await _dump_json_async(store, run_id, dataset, connector)
        _emit_run(store, run_id)
    except asyncio.CancelledError:
        for task in list(judge_tasks):
            if not task.done():
                task.cancel()
        if judge_tasks:
            await asyncio.gather(*list(judge_tasks), return_exceptions=True)
        set_control(run_id, None)
        _mark_cancelled_run(store, run_id)
        raise
    except CircuitBreakerOpen as exc:
        for task in list(judge_tasks):
            if not task.done():
                task.cancel()
        if judge_tasks:
            await asyncio.gather(*list(judge_tasks), return_exceptions=True)
        set_control(run_id, "paused")
        err = safe_exception(exc)
        current = store.get_run(run_id) or {}
        summary = current.get("summary") if isinstance(current.get("summary"), dict) else {}
        _update_run_respecting_control(store, run_id, status="paused", error=err,
                                       summary={**summary, "status": "paused_external_failure",
                                                "circuit_breaker": exc.kind})
        append_run_log(run_id, "circuit_breaker_open", kind=exc.kind,
                       threshold=exc.threshold, detail=exc.detail,
                       run=compact_run(store.get_run(run_id)))
        _emit_run(store, run_id)
        return
    except Exception as exc:  # noqa: BLE001
        _update_run_respecting_control(run_id=run_id, store=store, status="error", finished_at=time.time(),
                                       error=safe_exception(exc))
        _emit_run(store, run_id)
        raise


def _judge_failure_detail(judged: dict | None) -> str:
    parts: list[str] = []
    for case in (judged or {}).get("cases") or []:
        assessment = case.get("assessment") or {}
        reason = assessment.get("reason") or case.get("reason")
        if reason:
            parts.append(str(reason))
    return "; ".join(parts)[:1000]


async def _eval_case(executor, dataset, conn, client, case, idx, dialect, timeout, max_attempts, db_id,
                     retry_delay=0.0, stop_check=None, case_timeout=120.0,
                     run_id: str | None = None, circuit: RunCircuitBreaker | None = None):
    """Ask the connector for SQL, execute predicted + gold, score L0..L4 -> case record.
    Retries on error/no-SQL up to max_attempts (0 = infinite), sleeping retry_delay
    seconds between attempts; stop_check() (if given) breaks the loop early.
    case_timeout (sec, 0 = off): hard wall-clock cap per question — if a case spins
    longer than this it's dropped (recorded as failed) and the run moves on."""
    gold = await _get_gold_result(executor, dataset, case.gold_sql)
    ordered = _ordered(case.conditions)
    sql, api_err, predicted, attempts = None, None, None, 0
    _payload = None
    t0 = time.time()
    deadline = (t0 + case_timeout) if case_timeout and case_timeout > 0 else None
    while True:
        attempts += 1
        attempt_t0 = time.time()
        rem = (deadline - time.time()) if deadline else None
        if rem is not None and rem <= 0:
            api_err = api_err or f"тайм-аут кейса > {int(case_timeout)}с"
            break
        _log_api_attempt(run_id, "case_api_attempt_start", case, idx, attempts,
                         dialect=dialect, db_id=db_id, timeout_s=timeout,
                         case_timeout_s=case_timeout,
                         remaining_s=round(rem, 2) if rem is not None else None)
        timed_out = False
        try:
            gen = conn.generate(client, case.question, dialect, timeout, db_id)
            sql, _payload, api_err = await (asyncio.wait_for(gen, timeout=rem) if rem else gen)
        except asyncio.TimeoutError:
            api_err = f"тайм-аут кейса > {int(case_timeout)}с"; sql = None
            timed_out = True
        except Exception as exc:  # noqa: BLE001
            api_err = safe_exception(exc, limit=500)
            _log_api_attempt(run_id, "case_api_attempt_error", case, idx, attempts,
                             elapsed_s=round(time.time() - attempt_t0, 2),
                             sql_present=bool(sql), api_error=api_err)
            raise
        stop_requested = bool(stop_check and stop_check())
        if sql and not stop_requested:
            predicted = await execute_scoring_select(executor, dataset, sql)
        if circuit:
            await circuit.record("api", _api_external_failure(api_err, timed_out=timed_out), api_err)
            await circuit.record(
                "db",
                _db_external_failure(predicted.error if predicted else None),
                predicted.error if predicted else None,
            )
        success = bool(sql and predicted and predicted.ok)
        limit_reached = bool(max_attempts and attempts >= max_attempts)
        stop_requested = stop_requested or bool(stop_check and stop_check())
        budget_spent = bool(deadline and time.time() >= deadline)
        will_retry = not (success or timed_out or limit_reached or stop_requested or budget_spent)
        _log_api_attempt(run_id, "case_api_attempt_finish", case, idx, attempts,
                         elapsed_s=round(time.time() - attempt_t0, 2),
                         sql_present=bool(sql), api_error=redact_text(api_err),
                         db_ok=predicted.ok if predicted else None,
                         db_error=redact_text(predicted.error) if predicted else None,
                         success=success, will_retry=will_retry,
                         limit_reached=limit_reached, stop_requested=stop_requested,
                         timed_out=timed_out or budget_spent)
        if timed_out:
            break
        if success:
            break
        if limit_reached:
            break
        if stop_requested:
            break
        if budget_spent:
            api_err = api_err or f"тайм-аут кейса > {int(case_timeout)}с"
            break
        if retry_delay > 0:
            nap = retry_delay if not deadline else max(0.0, min(retry_delay, deadline - time.time()))
            if nap > 0:
                await asyncio.sleep(nap)
    level, reason = eval_level(predicted_sql=sql, predicted=predicted,
                               gold_sql=case.gold_sql, gold=gold, ordered=ordered)
    return redact_obj({
        "idx": idx, "case_id": case.case_id, "difficulty": case.difficulty,
        "question": case.question, "gold_sql": case.gold_sql,
        "predicted_sql": sql, "level": level, "matched": level == 4,
        "error": redact_text(api_err or (predicted.error if predicted else None)),
        "reason": reason, "elapsed_s": round(time.time() - t0, 2),
        "gold_result": _result_to_dict(gold), "agent_result": _result_to_dict(predicted),
        "attempts": attempts,
        "raw_response": (redact_text(json.dumps(_payload, ensure_ascii=False)[:8000]) if _payload else None),
    })


async def _collect_case(executor, dataset, conn, client, case, idx, dialect, timeout, max_attempts, db_id,
                        retry_delay=0.0, stop_check=None, case_timeout=120.0,
                        run_id: str | None = None, circuit: RunCircuitBreaker | None = None):
    """Ask connector + execute evidence, but do not assign L0-L4."""
    gold = await _get_gold_result(executor, dataset, case.gold_sql)
    sql, api_err, predicted, attempts = None, None, None, 0
    _payload = None
    t0 = time.time()
    deadline = (t0 + case_timeout) if case_timeout and case_timeout > 0 else None
    while True:
        attempts += 1
        attempt_t0 = time.time()
        rem = (deadline - time.time()) if deadline else None
        if rem is not None and rem <= 0:
            api_err = api_err or f"тайм-аут кейса > {int(case_timeout)}с"
            break
        _log_api_attempt(run_id, "case_api_attempt_start", case, idx, attempts,
                         dialect=dialect, db_id=db_id, timeout_s=timeout,
                         case_timeout_s=case_timeout,
                         remaining_s=round(rem, 2) if rem is not None else None)
        timed_out = False
        try:
            gen = conn.generate(client, case.question, dialect, timeout, db_id)
            sql, _payload, api_err = await (asyncio.wait_for(gen, timeout=rem) if rem else gen)
        except asyncio.TimeoutError:
            api_err = f"тайм-аут кейса > {int(case_timeout)}с"; sql = None
            timed_out = True
        except Exception as exc:  # noqa: BLE001
            api_err = safe_exception(exc, limit=500)
            _log_api_attempt(run_id, "case_api_attempt_error", case, idx, attempts,
                             elapsed_s=round(time.time() - attempt_t0, 2),
                             sql_present=bool(sql), api_error=api_err)
            raise
        stop_requested = bool(stop_check and stop_check())
        if sql and not stop_requested:
            predicted = await execute_scoring_select(executor, dataset, sql)
        if circuit:
            await circuit.record("api", _api_external_failure(api_err, timed_out=timed_out), api_err)
            await circuit.record(
                "db",
                _db_external_failure(predicted.error if predicted else None),
                predicted.error if predicted else None,
            )
        success = bool(sql and predicted and predicted.ok)
        limit_reached = bool(max_attempts and attempts >= max_attempts)
        stop_requested = stop_requested or bool(stop_check and stop_check())
        budget_spent = bool(deadline and time.time() >= deadline)
        will_retry = not (success or timed_out or limit_reached or stop_requested or budget_spent)
        _log_api_attempt(run_id, "case_api_attempt_finish", case, idx, attempts,
                         elapsed_s=round(time.time() - attempt_t0, 2),
                         sql_present=bool(sql), api_error=redact_text(api_err),
                         db_ok=predicted.ok if predicted else None,
                         db_error=redact_text(predicted.error) if predicted else None,
                         success=success, will_retry=will_retry,
                         limit_reached=limit_reached, stop_requested=stop_requested,
                         timed_out=timed_out or budget_spent)
        if timed_out:
            break
        if success:
            break
        if limit_reached:
            break
        if stop_requested:
            break
        if budget_spent:
            api_err = api_err or f"тайм-аут кейса > {int(case_timeout)}с"
            break
        if retry_delay > 0:
            nap = retry_delay if not deadline else max(0.0, min(retry_delay, deadline - time.time()))
            if nap > 0:
                await asyncio.sleep(nap)
    return redact_obj({
        "idx": idx, "case_id": case.case_id, "difficulty": case.difficulty,
        "question": case.question, "gold_sql": case.gold_sql,
        "predicted_sql": sql, "level": None, "matched": False,
        "error": redact_text(api_err or (predicted.error if predicted else None)),
        "reason": None, "elapsed_s": round(time.time() - t0, 2),
        "gold_result": _result_to_dict(gold), "agent_result": _result_to_dict(predicted),
        "attempts": attempts,
        "raw_response": (redact_text(json.dumps(_payload, ensure_ascii=False)[:8000]) if _payload else None),
    })


def _summary(case_recs, total):
    case_recs = _collected_cases(case_recs)
    levels = {i: 0 for i in range(5)}
    passed = 0
    for c in case_recs:
        lv = c.get("level")
        if lv in levels:
            levels[lv] += 1
        if lv == 4:
            passed += 1
    return {"accuracy": round(passed / max(1, total) * 100, 1), "passed": passed, "total": total,
            "done": len(case_recs), "L0": levels[0], "L1": levels[1], "L2": levels[2],
            "L3": levels[3], "L4": levels[4]}


def _mark_cancelled_run(store, run_id: str, total: int | None = None):
    run = store.get_run(run_id)
    if not run:
        return
    cases = store.list_cases(run_id)
    done_cases = len(_collected_cases(cases))
    total = total or run.get("total_cases") or len(cases)
    store.update_run(run_id, status="stopped", finished_at=time.time(),
                     done_cases=done_cases, summary=_summary(cases, total),
                     error=run.get("error") or "остановлено пользователем")
    _emit_run(store, run_id)


def needs_rerun(case_rec) -> bool:
    """A case should be re-run when it is missing, errored, or not L4."""
    if bool(case_rec.get("error")) or not case_rec.get("predicted_sql"):
        return True
    level = case_rec.get("level")
    if level is None:
        return True
    try:
        return int(level) != 4
    except (TypeError, ValueError):
        return True


async def rerun(store, run_id: str, case_ids=None, api_global_concurrency: int = 1,
                judge_cfg: dict | None = None, judge_timeout: float = 60,
                judge_max_retries: int = 0, judge_retry_delay: float = 0.0,
                judge_global_concurrency: int = 1):
    """Re-run cases of an existing run IN PLACE: by default only the unfinished/
    errored ones; or a specific set of case_ids. Recomputes summary + JSON."""
    run = store.get_run(run_id)
    if not run:
        return
    dataset = store.get_dataset(run.get("dataset_id")) or {}
    connector = store.get_connector(run.get("connector_id")) or {}
    if not _update_run_respecting_control(store, run_id, status="running"):
        return
    _emit_run(store, run_id)
    try:
        cases = parse_benchmark_file(dataset["benchmark_path"])
        existing = {c["case_id"]: c for c in store.list_cases(run_id)}
        if case_ids:
            targets = set(case_ids)
        else:
            targets = {c.case_id for c in cases
                       if c.case_id not in existing or needs_rerun(existing[c.case_id])}
        _ensure_scoring_dsn_allowed(dataset)
        executor = PgExecutor(dataset["dsn"], statement_timeout_ms=30000)
        dialect = connector.get("default_dialect", "postgres")
        conn = TemplatedConnector(connector)
        timeout = float(connector.get("timeout", 200))
        cfg = run.get("config") or {}
        max_attempts = int(cfg.get("max_attempts") if cfg.get("max_attempts") is not None else connector.get("max_attempts", 1))
        retry_delay = float(cfg.get("retry_delay") if cfg.get("retry_delay") is not None else (connector.get("retry_delay", 0) or 0))
        case_timeout = cfg.get("case_timeout", 120)
        api_global_sem = _global_limiter("connector_api", api_global_concurrency)
        judge_global_sem = _global_limiter("llm_judge", judge_global_concurrency)
        set_control(run_id, None)
        circuit = RunCircuitBreaker(run_id)
        stopped = False
        judge_errors = 0
        judged_cases: list[dict] = []
        judged_at = None
        await _prewarm_gold_cache(executor, dataset, [c for c in cases if c.case_id in targets])
        async with httpx.AsyncClient(verify=httpx_verify("CONNECTOR_SSL_VERIFY")) as client:
            for idx, case in enumerate(cases, 1):
                if case.case_id not in targets:
                    continue
                if not await _gate(store, run_id):
                    stopped = True
                    break
                waiting = _persist_case_status(
                    store, run_id, idx,
                    _progress_case(case, idx, "api_waiting", "ждем ответ API"),
                )
                _emit_run(store, run_id, case=waiting)
                async with api_global_sem:
                    if judge_cfg:
                        rec = await _collect_case(executor, dataset, conn, client, case, idx, dialect,
                                                  timeout, max_attempts, dataset.get("db_id", ""),
                                                  retry_delay=retry_delay, case_timeout=case_timeout,
                                                  stop_check=lambda: _stop_requested(store, run_id),
                                                  run_id=run_id, circuit=circuit)
                    else:
                        rec = await _eval_case(executor, dataset, conn, client, case, idx, dialect,
                                               timeout, max_attempts, dataset.get("db_id", ""),
                                               retry_delay=retry_delay, case_timeout=case_timeout,
                                                  stop_check=lambda: _stop_requested(store, run_id),
                                                  run_id=run_id, circuit=circuit)
                if _stop_requested(store, run_id):
                    stopped = True
                    break
                if judge_cfg:
                    stage_status = _case_error_status(rec) or "llm_queued"
                    rec = _persist_case_status(store, run_id, idx, rec, stage_status)
                    current_cases = store.list_cases(run_id)
                    store.update_run(run_id, done_cases=len(_collected_cases(current_cases)),
                                     summary={**_summary(current_cases, len(cases)),
                                              "status": "collecting_answers",
                                              "llm_queued": 1,
                                              "llm_in_progress": 0})
                    _emit_run(store, run_id, case=rec)
                    queued = _persist_case_status(store, run_id, idx, rec, "llm_queued")
                    _emit_run(store, run_id, case=queued)
                    try:
                        async with judge_global_sem:
                            _update_run_respecting_control(
                                store, run_id, status="judging",
                                summary={**_summary(store.list_cases(run_id), len(cases)),
                                         "status": "judging",
                                         "llm_queued": 0,
                                         "llm_in_progress": 1},
                            )
                            sent = _persist_case_status(store, run_id, idx, rec, "sent_to_judge")
                            _emit_run(store, run_id, case=sent)
                            judging = _persist_case_status(store, run_id, idx, rec, "judging")
                            _emit_run(store, run_id, case=judging)
                            judged = await judge_answers(_answers_doc_for_case(store, run_id, dataset, connector, rec),
                                                         judge_cfg, timeout=judge_timeout, concurrency=1,
                                                         judged_at=time.time(), max_retries=judge_max_retries,
                                                         retry_delay=judge_retry_delay)
                        if (judged.get("judge_summary") or {}).get("invalid"):
                            detail = _judge_failure_detail(judged)
                            await circuit.record("llm", _llm_external_failure(detail), detail)
                            raise ValueError("LLM judge returned invalid level")
                        apply_judged_levels(store, run_id, judged)
                        await circuit.record("llm", False)
                        judged_cases.extend(judged.get("cases") or [])
                        judged_at = time.time()
                        if _stop_requested(store, run_id):
                            stopped = True
                            break
                        updated = next((c for c in store.list_cases(run_id)
                                        if c.get("case_id") == rec.get("case_id")), rec)
                        _update_run_respecting_control(
                            store, run_id, status="running",
                            summary={**_summary(store.list_cases(run_id), len(cases)),
                                     "status": "running",
                                     "llm_queued": 0,
                                     "llm_in_progress": 0},
                        )
                        judged_case = _persist_case_status(store, run_id, idx, updated, "judged")
                        _emit_run(store, run_id, case=judged_case)
                    except CircuitBreakerOpen:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        err = safe_exception(exc, limit=500)
                        await circuit.record("llm", _llm_external_failure(err), err)
                        judge_errors += 1
                        bad = {**rec, "level": None, "matched": False, "reason": None,
                               "error": rec.get("error") or err[:200]}
                        bad = _persist_case_status(store, run_id, idx, bad, "judge_error")
                        _update_run_respecting_control(
                            store, run_id, status="running",
                            summary={**_summary(store.list_cases(run_id), len(cases)),
                                     "status": "judge_error",
                                     "judge_errors": judge_errors,
                                     "llm_queued": 0,
                                     "llm_in_progress": 0},
                        )
                        _emit_run(store, run_id, case=bad)
                else:
                    rec = _persist_case_status(store, run_id, idx, rec, _case_error_status(rec) or "done")
                    _emit_run(store, run_id, case=rec)
        set_control(run_id, None)
        final_cases = store.list_cases(run_id)
        final_summary = _summary(final_cases, len(cases))
        if judge_cfg:
            final_summary.update({
                "judged": sum(1 for c in final_cases if c.get("level") is not None),
                "judge_errors": judge_errors,
                "awaiting_judge": 0,
                "llm_queued": 0,
                "llm_in_progress": 0,
            })
            await _dump_answers_json_async(store, run_id, dataset, connector)
            if judged_cases:
                await _dump_judged_levels_json_async(run_id, _judged_levels_doc(
                    store, run_id, dataset, connector, judged_cases, judge_cfg,
                    judged_at=judged_at or time.time()))
        final_status = "stopped" if stopped else ("error" if judge_errors else "done")
        final_error = f"ошибка оценки LLM в {judge_errors} кейс(ах)" if judge_errors else None
        _update_run_respecting_control(store, run_id, status=final_status, finished_at=time.time(),
                                       done_cases=len(_collected_cases(final_cases)),
                                       summary=final_summary, error=final_error)
        await _dump_json_async(store, run_id, dataset, connector)
        _emit_run(store, run_id)
    except asyncio.CancelledError:
        set_control(run_id, None)
        _mark_cancelled_run(store, run_id, run.get("total_cases"))
        raise
    except CircuitBreakerOpen as exc:
        set_control(run_id, "paused")
        err = safe_exception(exc)
        current = store.get_run(run_id) or {}
        summary = current.get("summary") if isinstance(current.get("summary"), dict) else {}
        _update_run_respecting_control(store, run_id, status="paused", error=err,
                                       summary={**summary, "status": "paused_external_failure",
                                                "circuit_breaker": exc.kind})
        append_run_log(run_id, "circuit_breaker_open", kind=exc.kind,
                       threshold=exc.threshold, detail=exc.detail,
                       run=compact_run(store.get_run(run_id)))
        _emit_run(store, run_id)
        return
    except Exception as exc:  # noqa: BLE001
        _update_run_respecting_control(store, run_id, status="error", finished_at=time.time(),
                                       error=safe_exception(exc))
        _emit_run(store, run_id)
        raise


async def judge_existing_case(store, run_id: str, case_id: str, judge_cfg: dict,
                              judge_timeout: float = 60, judge_max_retries: int = 0,
                              judge_retry_delay: float = 0.0,
                              restore_status: str | None = None,
                              judge_global_concurrency: int = 1):
    """Re-run only the LLM L0-L4 assessment for an already collected case."""
    run = store.get_run(run_id)
    if not run:
        return
    final_base_status = restore_status or run.get("status")
    dataset = store.get_dataset(run.get("dataset_id")) or {}
    connector = store.get_connector(run.get("connector_id")) or {}
    cases = store.list_cases(run_id)
    rec = next((c for c in cases if c.get("case_id") == case_id), None)
    if not rec:
        return
    idx = rec["idx"]
    queued = _persist_case_status(store, run_id, idx, rec, "llm_queued")
    _emit_run(store, run_id, case=queued)
    if not _update_run_respecting_control(store, run_id, status="judging"):
        return
    _emit_run(store, run_id)
    circuit = RunCircuitBreaker(run_id)
    try:
        async with _global_limiter("llm_judge", judge_global_concurrency):
            sent = _persist_case_status(store, run_id, idx, rec, "sent_to_judge")
            _emit_run(store, run_id, case=sent)
            judging = _persist_case_status(store, run_id, idx, rec, "judging")
            _emit_run(store, run_id, case=judging)
            judged = await judge_answers(_answers_doc_for_case(store, run_id, dataset, connector, rec),
                                         judge_cfg, timeout=judge_timeout, concurrency=1,
                                         judged_at=time.time(), max_retries=judge_max_retries,
                                         retry_delay=judge_retry_delay)
        if _stop_requested(store, run_id):
            return
        if (judged.get("judge_summary") or {}).get("invalid"):
            detail = _judge_failure_detail(judged)
            await circuit.record("llm", _llm_external_failure(detail), detail)
            err_msg = "LLM judge returned invalid level"
            bad = {**rec, "level": None, "matched": False, "reason": None,
                   "error": rec.get("error") or err_msg}
            bad = _persist_case_status(store, run_id, idx, bad, "judge_error")
            _update_run_respecting_control(
                store, run_id, status="error", finished_at=time.time(),
                summary=_summary(store.list_cases(run_id), run.get("total_cases") or len(cases)),
                error=f"ошибка оценки LLM в кейсе {case_id}",
            )
            _emit_run(store, run_id, case=bad)
            _emit_run(store, run_id)
            return
        await circuit.record("llm", False)
        err = rec.get("error") or ""
        clean = {**rec, "level": None, "matched": False, "reason": None}
        if "judge" in err.lower() or "оцен" in err.lower():
            clean["error"] = None
        store.replace_case(run_id, idx, clean)
        apply_judged_levels(store, run_id, judged)
        updated = next((c for c in store.list_cases(run_id) if c.get("case_id") == case_id), clean)
        summary = _summary(store.list_cases(run_id), run.get("total_cases") or len(cases))
        final_status = final_base_status if final_base_status in ACTIVE_RUN_STATUSES else "done"
        update = {"status": final_status, "summary": summary, "error": None}
        if final_status == "done":
            update["finished_at"] = time.time()
        _update_run_respecting_control(store, run_id, **update)
        await _dump_json_async(store, run_id, dataset, connector)
        judged_case = _persist_case_status(store, run_id, idx, updated, "judged")
        _emit_run(store, run_id, case=judged_case)
        _emit_run(store, run_id)
    except asyncio.CancelledError:
        set_control(run_id, None)
        _mark_cancelled_run(store, run_id, run.get("total_cases") or len(cases))
        raise
    except CircuitBreakerOpen as exc:
        set_control(run_id, "paused")
        err = safe_exception(exc)
        summary = _summary(store.list_cases(run_id), run.get("total_cases") or len(cases))
        _update_run_respecting_control(store, run_id, status="paused", error=err,
                                       summary={**summary, "status": "paused_external_failure",
                                                "circuit_breaker": exc.kind})
        append_run_log(run_id, "circuit_breaker_open", kind=exc.kind,
                       threshold=exc.threshold, detail=exc.detail,
                       run=compact_run(store.get_run(run_id)))
        _emit_run(store, run_id)
        return
    except Exception as exc:  # noqa: BLE001
        bad = {**rec, "level": None, "matched": False, "reason": None,
               "error": rec.get("error") or safe_exception(exc, limit=200)}
        bad = _persist_case_status(store, run_id, idx, bad, "judge_error")
        _update_run_respecting_control(
            store, run_id, status="error", finished_at=time.time(),
            summary=_summary(store.list_cases(run_id), run.get("total_cases") or len(cases)),
            error=f"ошибка оценки LLM в кейсе {case_id}",
        )
        _emit_run(store, run_id, case=bad)
        _emit_run(store, run_id)


async def rerun_api_case(store, run_id: str, case_id: str, judge_cfg: dict | None = None,
                         judge_timeout: float = 60, judge_max_retries: int = 0,
                         judge_retry_delay: float = 0.0,
                         api_global_concurrency: int = 1,
                         judge_global_concurrency: int = 1):
    """Re-run connector/API for one case, then optionally re-run the LLM judge."""
    run = store.get_run(run_id)
    if not run:
        return
    restore_status = run.get("status")
    dataset = store.get_dataset(run.get("dataset_id")) or {}
    connector = store.get_connector(run.get("connector_id")) or {}
    cases = parse_benchmark_file(dataset["benchmark_path"])
    target = next(((i, c) for i, c in enumerate(cases, 1) if c.case_id == case_id), None)
    if not target:
        return
    idx, case = target
    _ensure_scoring_dsn_allowed(dataset)
    executor = PgExecutor(dataset["dsn"], statement_timeout_ms=30000)
    dialect = connector.get("default_dialect", "postgres")
    conn = TemplatedConnector(connector)
    timeout = float(connector.get("timeout", 200))
    cfg = run.get("config") or {}
    max_attempts = int(cfg.get("max_attempts") if cfg.get("max_attempts") is not None else connector.get("max_attempts", 1))
    retry_delay = float(cfg.get("retry_delay") if cfg.get("retry_delay") is not None else (connector.get("retry_delay", 0) or 0))
    case_timeout = cfg.get("case_timeout", 120)
    if not _update_run_respecting_control(store, run_id, status="running"):
        return
    _emit_run(store, run_id)
    circuit = RunCircuitBreaker(run_id)
    waiting = _persist_case_status(
        store, run_id, idx,
        _progress_case(case, idx, "api_waiting", "ждем ответ API"),
    )
    _emit_run(store, run_id, case=waiting)
    try:
        await _prewarm_gold_cache(executor, dataset, [case])
        async with httpx.AsyncClient(verify=httpx_verify("CONNECTOR_SSL_VERIFY")) as client:
            async with _global_limiter("connector_api", api_global_concurrency):
                if judge_cfg:
                    rec = await _collect_case(executor, dataset, conn, client, case, idx, dialect, timeout, max_attempts,
                                              dataset.get("db_id", ""), retry_delay=retry_delay,
                                              case_timeout=case_timeout,
                                              stop_check=lambda: _stop_requested(store, run_id),
                                              run_id=run_id, circuit=circuit)
                else:
                    rec = await _eval_case(executor, dataset, conn, client, case, idx, dialect, timeout, max_attempts,
                                           dataset.get("db_id", ""), retry_delay=retry_delay,
                                           case_timeout=case_timeout,
                                           stop_check=lambda: _stop_requested(store, run_id),
                                           run_id=run_id, circuit=circuit)
        if _stop_requested(store, run_id):
            return
        stage_status = _case_error_status(rec) or ("llm_queued" if judge_cfg else "done")
        rec = _persist_case_status(store, run_id, idx, rec, stage_status)
        await _dump_answers_json_async(store, run_id, dataset, connector)
        current_cases = store.list_cases(run_id)
        store.update_run(run_id, done_cases=len(_collected_cases(current_cases)),
                         summary=_summary(current_cases, run.get("total_cases") or len(cases)))
        _emit_run(store, run_id, case=rec)
        if judge_cfg:
            await judge_existing_case(store, run_id, case_id, judge_cfg, judge_timeout,
                                      judge_max_retries, judge_retry_delay,
                                      restore_status=restore_status,
                                      judge_global_concurrency=judge_global_concurrency)
        else:
            final_status = restore_status if restore_status in ACTIVE_RUN_STATUSES else "done"
            update = {"status": final_status,
                      "summary": _summary(store.list_cases(run_id), run.get("total_cases") or len(cases))}
            if final_status == "done":
                update["finished_at"] = time.time()
            _update_run_respecting_control(store, run_id, **update)
            await _dump_json_async(store, run_id, dataset, connector)
            _emit_run(store, run_id)
    except asyncio.CancelledError:
        set_control(run_id, None)
        _mark_cancelled_run(store, run_id, run.get("total_cases") or len(cases))
        raise
    except CircuitBreakerOpen as exc:
        set_control(run_id, "paused")
        err = safe_exception(exc)
        summary = _summary(store.list_cases(run_id), run.get("total_cases") or len(cases))
        _update_run_respecting_control(store, run_id, status="paused", error=err,
                                       summary={**summary, "status": "paused_external_failure",
                                                "circuit_breaker": exc.kind})
        append_run_log(run_id, "circuit_breaker_open", kind=exc.kind,
                       threshold=exc.threshold, detail=exc.detail,
                       run=compact_run(store.get_run(run_id)))
        _emit_run(store, run_id)
        return


def count_rerun_targets(store, run_id: str) -> int:
    """Cases that a rerun/continue would run: missing plus non-L4 cases."""
    run = store.get_run(run_id)
    if not run:
        return 0
    ds = store.get_dataset(run.get("dataset_id")) or {}
    try:
        cases = parse_benchmark_file(ds["benchmark_path"])
    except Exception:
        return sum(1 for c in store.list_cases(run_id) if needs_rerun(c))
    existing = {c["case_id"]: c for c in store.list_cases(run_id)}
    return sum(1 for c in cases if c.case_id not in existing or needs_rerun(existing[c.case_id]))


RESULT_SCHEMA = "bench-result/v1"
ANSWERS_SCHEMA = "bench-answers/v1"
_CASE_FIELDS = ("case_id", "difficulty", "question", "gold_sql", "predicted_sql",
                "level", "matched", "reason", "error", "elapsed_s", "attempts",
                "gold_result", "agent_result", "assessment")
_ANSWER_CASE_FIELDS = ("case_id", "difficulty", "question", "gold_sql", "predicted_sql",
                       "error", "elapsed_s", "attempts", "gold_result",
                       "agent_result", "raw_response")


def _effective_level(case: dict):
    return case.get("human_level") if case.get("human_level") is not None else case.get("level")


def _result_case(case: dict) -> dict:
    out = {k: case.get(k) for k in _CASE_FIELDS}
    auto_level = case.get("level")
    effective_level = _effective_level(case)
    out["auto_level"] = auto_level
    out["human_level"] = case.get("human_level")
    out["level"] = effective_level
    out["matched"] = effective_level == 4
    return redact_obj(out)


def _answer_case(case: dict) -> dict:
    return redact_obj({k: case.get(k) for k in _ANSWER_CASE_FIELDS})


def _median(times):
    t = sorted(x or 0 for x in times)
    if not t:
        return 0
    n = len(t)
    return t[n // 2] if n % 2 else (t[n // 2 - 1] + t[n // 2]) / 2


def build_result(store, run_id: str, dataset: dict | None = None, connector: dict | None = None) -> dict:
    """Canonical, self-contained results document for one benchmark run."""
    run = store.get_run(run_id) or {}
    cases = store.list_cases(run_id)
    if dataset is None:
        dataset = store.get_dataset(run.get("dataset_id")) or {}
    if connector is None:
        connector = store.get_connector(run.get("connector_id")) or {}
    s = run.get("summary") or {}
    times = [c.get("elapsed_s") for c in cases]
    return redact_obj({
        "schema": RESULT_SCHEMA,
        "run_id": run_id,
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "benchmark": {
            "dataset_id": run.get("dataset_id"),
            "name": run.get("dataset_name") or dataset.get("name"),
            "db_id": dataset.get("db_id"),
            "db_type": dataset.get("db_type") or "postgres",
        },
        "model": {
            "name": run.get("connector_name") or connector.get("name"),
            "connector_id": run.get("connector_id"),
            "dialect": connector.get("default_dialect") or "postgres",
            "endpoint": redact_text(connector.get("url")),
        },
        "summary": {
            "accuracy": s.get("accuracy"),
            "passed": s.get("passed"),
            "total": s.get("total"),
            "levels": {f"L{i}": s.get(f"L{i}", 0) for i in range(5)},
            "elapsed_total_s": round(sum(x or 0 for x in times), 1),
            "median_elapsed_s": round(_median(times), 1),
        },
        "cases": [_result_case(c) for c in cases],
    })


def build_answers(store, run_id: str, dataset: dict | None = None, connector: dict | None = None) -> dict:
    """Raw connector answers + execution evidence, deliberately without L0-L4."""
    run = store.get_run(run_id) or {}
    cases = store.list_cases(run_id)
    if dataset is None:
        dataset = store.get_dataset(run.get("dataset_id")) or {}
    if connector is None:
        connector = store.get_connector(run.get("connector_id")) or {}
    return redact_obj({
        "schema": ANSWERS_SCHEMA,
        "run_id": run_id,
        "status": run.get("status"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "benchmark": {
            "dataset_id": run.get("dataset_id"),
            "name": run.get("dataset_name") or dataset.get("name"),
            "db_id": dataset.get("db_id"),
            "db_type": dataset.get("db_type") or "postgres",
        },
        "model": {
            "name": run.get("connector_name") or connector.get("name"),
            "connector_id": run.get("connector_id"),
            "dialect": connector.get("default_dialect") or "postgres",
            "endpoint": redact_text(connector.get("url")),
        },
        "cases": [_answer_case(c) for c in _collected_cases(cases)],
    })


def apply_judged_levels(store, run_id: str, judged: dict):
    """Merge bench-judged-levels/v1 case levels back into Store rows."""
    existing = {c["case_id"]: c for c in store.list_cases(run_id)}
    for jc in judged.get("cases") or []:
        old = existing.get(jc.get("case_id"))
        if not old:
            continue
        level = jc.get("level")
        rec = {**old, "level": level, "matched": level == 4,
               "reason": jc.get("reason") or ((jc.get("assessment") or {}).get("reason")),
               "assessment": jc.get("assessment"),
               "case_status": "judged",
               "case_status_label": _case_status_label("judged")}
        store.replace_case(run_id, old["idx"], rec)


def _dump_json(store, run_id: str, dataset: dict | None = None, connector: dict | None = None):
    """Persist the canonical results JSON for a run (one file per benchmark run)."""
    os.makedirs(RUNS_DIR, exist_ok=True)
    with open(os.path.join(RUNS_DIR, f"{run_id}.json"), "w", encoding="utf-8") as f:
        json.dump(build_result(store, run_id, dataset, connector), f, ensure_ascii=False, indent=2)


def _dump_answers_json(store, run_id: str, dataset: dict | None = None, connector: dict | None = None):
    os.makedirs(ANSWERS_DIR, exist_ok=True)
    with open(os.path.join(ANSWERS_DIR, f"{run_id}.json"), "w", encoding="utf-8") as f:
        json.dump(build_answers(store, run_id, dataset, connector), f, ensure_ascii=False, indent=2)


def _dump_judged_levels_json(run_id: str, judged: dict):
    os.makedirs(JUDGED_DIR, exist_ok=True)
    with open(os.path.join(JUDGED_DIR, f"{run_id}.levels.json"), "w", encoding="utf-8") as f:
        json.dump(judged, f, ensure_ascii=False, indent=2)


async def _dump_json_async(store, run_id: str, dataset: dict | None = None, connector: dict | None = None):
    await asyncio.to_thread(_dump_json, store, run_id, dataset, connector)


async def _dump_answers_json_async(store, run_id: str, dataset: dict | None = None, connector: dict | None = None):
    await asyncio.to_thread(_dump_answers_json, store, run_id, dataset, connector)


async def _dump_judged_levels_json_async(run_id: str, judged: dict):
    await asyncio.to_thread(_dump_judged_levels_json, run_id, judged)


def run_json_path(run_id: str) -> str:
    return os.path.join(RUNS_DIR, f"{run_id}.json")


def answers_json_path(run_id: str) -> str:
    return os.path.join(ANSWERS_DIR, f"{run_id}.json")
