"""Durable benchmark worker.

The web container should stay small and responsive: it accepts API requests,
serves snapshots from the store, and enqueues long-running benchmark work.
This worker owns connector calls, scoring DB calls, and LLM judging.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from pathlib import Path
from typing import Any

from leaderboard.benchmark import parse_benchmark_file
from leaderboard.redaction import safe_exception
from bench_app.logging_utils import configure_basic_json_logging
from bench_app.defaults import migrate_dataset_paths_to_jsonl, seed_default_datasets
from bench_app.judge import judge_answers, judge_result, llm_config
from bench_app.run_logs import append_run_log, compact_run
from bench_app.runner import (
    _collected_cases,
    _dump_judged_levels_json_async,
    _dump_json_async,
    _summary,
    apply_judged_levels,
    build_answers,
    build_result,
    judge_existing_case,
    rerun,
    rerun_api_case,
    run_task,
)
from bench_app.state_graph import RUN_ACTIVE_STATES, RUN_FINISHED_STATES
from bench_app.store import make_store

LOG = logging.getLogger("bench_app.worker")
ACTIVE_STATUSES = set(RUN_ACTIVE_STATES)
FINISHED_STATUSES = set(RUN_FINISHED_STATES)


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


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


def _api_concurrency_limit() -> int:
    return max(1, _env_int("BENCH_APP_MAX_API_CONCURRENCY", 1))


def _judge_concurrency() -> int:
    return max(1, _env_int("LLM_JUDGE_CONCURRENCY", 1))


def _judge_timeout() -> float:
    return max(1.0, _env_float("LLM_JUDGE_TIMEOUT", 3600.0))


def _judge_max_retries() -> int:
    return max(0, _env_int("LLM_JUDGE_MAX_RETRIES", 2))


def _judge_retry_delay() -> float:
    return max(0.0, _env_float("LLM_JUDGE_RETRY_DELAY", 2.0))


def _judge_enabled() -> bool:
    return _env_flag("BENCH_APP_AUTO_JUDGE", True)


def _run_uses_judge(run: dict) -> bool:
    cfg = run.get("config") or {}
    if "auto_judge" in cfg:
        return bool(cfg.get("auto_judge"))
    return _judge_enabled()


def _judge_cfg_for_run(run: dict) -> dict | None:
    if not _run_uses_judge(run):
        return None
    cfg = llm_config()
    if not cfg:
        raise RuntimeError("LLM judge не настроен: задайте LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
    return cfg


def _payload_bool(payload: dict[str, Any], key: str, default: bool) -> bool:
    if key not in payload:
        return default
    raw = payload.get(key)
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _payload_int(payload: dict[str, Any], cfg: dict, key: str, default: int) -> int:
    raw = payload.get(key) if key in payload else cfg.get(key)
    try:
        return int(raw if raw is not None else default)
    except (TypeError, ValueError):
        return default


def _payload_float(payload: dict[str, Any], cfg: dict, key: str, default: float) -> float:
    raw = payload.get(key) if key in payload else cfg.get(key)
    try:
        return float(raw if raw is not None else default)
    except (TypeError, ValueError):
        return default


def _judge_cfg_for_job(run: dict, payload: dict[str, Any]) -> dict | None:
    if "auto_judge" not in payload:
        return _judge_cfg_for_run(run)
    if not _payload_bool(payload, "auto_judge", True):
        return None
    cfg = llm_config()
    if not cfg:
        raise RuntimeError("LLM judge не настроен: задайте LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
    return cfg


def _case_collected_for_done_count(case: dict | None) -> bool:
    if not case or case.get("case_status") == "api_waiting":
        return False
    return (
        case.get("attempts") is not None
        or bool(case.get("predicted_sql"))
        or bool(case.get("error"))
        or case.get("level") is not None
        or case.get("gold_result") is not None
        or case.get("agent_result") is not None
    )


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


def _run_deps(store, run_id: str) -> tuple[dict, dict, dict]:
    run = store.get_run(run_id)
    if not run:
        raise RuntimeError(f"run not found: {run_id}")
    dataset = store.get_dataset(run.get("dataset_id"))
    if not dataset:
        raise RuntimeError("датасет этого прогона удален")
    connector = store.get_connector(run.get("connector_id"))
    if not connector:
        raise RuntimeError("коннектор этого прогона удален")
    return run, dataset, connector


async def _wait_until_runnable(store, run_id: str) -> bool:
    while True:
        run = store.get_run(run_id) or {}
        status = run.get("status")
        if status == "paused":
            await asyncio.sleep(1)
            continue
        return status not in {"stopped", "cancelled", "error"}


def _run_config(run: dict) -> dict:
    cfg = dict(run.get("config") or {})
    cfg.setdefault("concurrency", 1)
    cfg.setdefault("api_concurrency_limit", _api_concurrency_limit())
    cfg.setdefault("case_timeout", 120)
    cfg.setdefault("judge_timeout", _judge_timeout())
    cfg.setdefault("judge_concurrency", _judge_concurrency())
    cfg.setdefault("judge_max_retries", _judge_max_retries())
    cfg.setdefault("judge_retry_delay", _judge_retry_delay())
    return cfg


async def _run_new(store, run_id: str) -> None:
    run, dataset, connector = _run_deps(store, run_id)
    if run.get("status") in FINISHED_STATUSES:
        return
    cfg = _run_config(run)
    await run_task(
        store,
        run_id,
        dataset,
        connector,
        concurrency=int(cfg.get("concurrency") or 1),
        api_global_concurrency=int(cfg.get("api_concurrency_limit") or _api_concurrency_limit()),
        max_attempts=cfg.get("max_attempts"),
        retry_delay=cfg.get("retry_delay"),
        case_timeout=float(cfg.get("case_timeout") or 120),
        judge_cfg=_judge_cfg_for_run(run),
        judge_timeout=float(cfg.get("judge_timeout") or _judge_timeout()),
        judge_concurrency=int(cfg.get("judge_concurrency") or _judge_concurrency()),
        judge_max_retries=int(cfg.get("judge_max_retries") or _judge_max_retries()),
        judge_retry_delay=float(cfg.get("judge_retry_delay") or _judge_retry_delay()),
    )


def _continue_targets(store, run: dict, dataset: dict) -> tuple[list[str], list[str], int]:
    cases = parse_benchmark_file(dataset["benchmark_path"])
    existing = {c.get("case_id"): c for c in store.list_cases(run["id"], include_payload=False)}
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


async def _finish_if_no_targets(store, run_id: str, total: int) -> None:
    run = store.get_run(run_id)
    if not run:
        return
    cases = store.list_cases(run_id)
    summary = _summary(cases, total)
    done = len(_collected_cases(cases))
    finished = done >= total
    store.update_run(
        run_id,
        status="done" if finished else "stopped",
        done_cases=done,
        finished_at=time.time() if finished else None,
        summary=summary,
        error=None if finished else "автопродолжение не нашло недоделанные кейсы",
    )
    await _dump_json_async(store, run_id)


async def _continue_run(store, run_id: str) -> None:
    run, dataset, _connector = _run_deps(store, run_id)
    if run.get("status") == "stopped" and run.get("error") != "прервано перезапуском сервера":
        return
    judge_cfg = _judge_cfg_for_run(run)
    cfg = _run_config(run)
    connector_targets, judge_targets, total = _continue_targets(store, run, dataset)
    append_run_log(
        run_id,
        "worker_continue_plan",
        run=compact_run(run),
        connector_targets=len(connector_targets),
        judge_targets=len(judge_targets),
        total=total,
    )
    if connector_targets:
        await rerun(
            store,
            run_id,
            case_ids=connector_targets,
            api_global_concurrency=int(cfg.get("api_concurrency_limit") or _api_concurrency_limit()),
            judge_cfg=judge_cfg,
            judge_timeout=float(cfg.get("judge_timeout") or _judge_timeout()),
            judge_max_retries=int(cfg.get("judge_max_retries") or _judge_max_retries()),
            judge_retry_delay=float(cfg.get("judge_retry_delay") or _judge_retry_delay()),
            judge_global_concurrency=int(cfg.get("judge_concurrency") or _judge_concurrency()),
        )
    for case_id in judge_targets:
        await judge_existing_case(
            store,
            run_id,
            case_id,
            judge_cfg,
            judge_timeout=float(cfg.get("judge_timeout") or _judge_timeout()),
            judge_max_retries=int(cfg.get("judge_max_retries") or _judge_max_retries()),
            judge_retry_delay=float(cfg.get("judge_retry_delay") or _judge_retry_delay()),
            restore_status="running",
            judge_global_concurrency=int(cfg.get("judge_concurrency") or _judge_concurrency()),
        )
    if not connector_targets and not judge_targets:
        await _finish_if_no_targets(store, run_id, total)


async def _rerun_failed(store, run_id: str, payload: dict[str, Any] | None = None) -> None:
    payload = payload or {}
    run, _dataset, _connector = _run_deps(store, run_id)
    cfg = _run_config(run)
    await rerun(
        store,
        run_id,
        api_global_concurrency=_payload_int(payload, cfg, "api_concurrency_limit", _api_concurrency_limit()),
        judge_cfg=_judge_cfg_for_job(run, payload),
        judge_timeout=_payload_float(payload, cfg, "judge_timeout", _judge_timeout()),
        judge_max_retries=_payload_int(payload, cfg, "judge_max_retries", _judge_max_retries()),
        judge_retry_delay=_payload_float(payload, cfg, "judge_retry_delay", _judge_retry_delay()),
        judge_global_concurrency=_payload_int(payload, cfg, "judge_concurrency", _judge_concurrency()),
    )


async def _rerun_case(store, run_id: str, payload: dict[str, Any]) -> None:
    run, _dataset, _connector = _run_deps(store, run_id)
    cfg = _run_config(run)
    case_id = str(payload.get("case_id") or "").strip()
    if not case_id:
        raise RuntimeError("case_id is required")
    await rerun_api_case(
        store,
        run_id,
        case_id,
        judge_cfg=_judge_cfg_for_job(run, payload),
        judge_timeout=_payload_float(payload, cfg, "judge_timeout", _judge_timeout()),
        judge_max_retries=_payload_int(payload, cfg, "judge_max_retries", _judge_max_retries()),
        judge_retry_delay=_payload_float(payload, cfg, "judge_retry_delay", _judge_retry_delay()),
        api_global_concurrency=_payload_int(payload, cfg, "api_concurrency_limit", _api_concurrency_limit()),
        judge_global_concurrency=_payload_int(payload, cfg, "judge_concurrency", _judge_concurrency()),
    )


async def _judge_case(store, run_id: str, payload: dict[str, Any]) -> None:
    run, _dataset, _connector = _run_deps(store, run_id)
    cfg = _run_config(run)
    case_id = str(payload.get("case_id") or "").strip()
    if not case_id:
        raise RuntimeError("case_id is required")
    await judge_existing_case(
        store,
        run_id,
        case_id,
        _judge_cfg_for_run(run) or llm_config(),
        judge_timeout=float(cfg.get("judge_timeout") or _judge_timeout()),
        judge_max_retries=int(cfg.get("judge_max_retries") or _judge_max_retries()),
        judge_retry_delay=float(cfg.get("judge_retry_delay") or _judge_retry_delay()),
        judge_global_concurrency=int(cfg.get("judge_concurrency") or _judge_concurrency()),
    )


async def _judge_levels(store, run_id: str) -> None:
    run, _dataset, _connector = _run_deps(store, run_id)
    cfg = _run_config(run)
    judge_cfg = _judge_cfg_for_run(run) or llm_config()
    if not judge_cfg:
        raise RuntimeError("LLM judge не настроен")
    answers = build_answers(store, run_id)
    store.update_run(run_id, status="judging")
    judged = await judge_answers(
        answers,
        judge_cfg,
        timeout=float(cfg.get("judge_timeout") or _judge_timeout()),
        concurrency=int(cfg.get("judge_concurrency") or _judge_concurrency()),
        judged_at=time.time(),
        max_retries=int(cfg.get("judge_max_retries") or _judge_max_retries()),
        retry_delay=float(cfg.get("judge_retry_delay") or _judge_retry_delay()),
    )
    if (judged.get("judge_summary") or {}).get("invalid"):
        store.update_run(run_id, status="error", finished_at=time.time(),
                         error=f"LLM judge returned invalid levels: {judged['judge_summary']['invalid']}")
        return
    apply_judged_levels(store, run_id, judged)
    store.update_run(run_id, status="done", finished_at=time.time(),
                     summary=_summary(store.list_cases(run_id), run.get("total_cases")))
    await _dump_json_async(store, run_id)
    await _dump_judged_levels_json_async(run_id, judged)


async def _judge_legacy(store, run_id: str) -> None:
    cfg = llm_config()
    if not cfg:
        raise RuntimeError("LLM не настроен")
    judged = await judge_result(build_result(store, run_id), cfg, timeout=_judge_timeout(),
                                concurrency=_judge_concurrency(), judged_at=time.time())
    data_dir = os.getenv("BENCH_APP_DATA_DIR", "bench_app/data")
    judged_dir = Path(os.getenv("BENCH_APP_JUDGED_DIR", os.path.join(data_dir, "judged")))
    judged_dir.mkdir(parents=True, exist_ok=True)
    (judged_dir / f"{run_id}.json").write_text(json.dumps(judged, ensure_ascii=False, indent=2), encoding="utf-8")


async def dispatch_job(store, job: dict) -> None:
    run_id = job.get("run_id")
    job_type = job.get("job_type")
    payload = job.get("payload") or {}
    if not run_id:
        raise RuntimeError("job has no run_id")
    run = store.get_run(run_id)
    append_run_log(run_id, "worker_job_start", job_id=job.get("id"), job_type=job_type,
                   job_attempts=job.get("attempts"), run=compact_run(run))
    if not await _wait_until_runnable(store, run_id):
        append_run_log(run_id, "worker_job_skipped", job_id=job.get("id"), job_type=job_type,
                       reason="run is stopped/error", run=compact_run(store.get_run(run_id)))
        return
    if job_type == "run":
        await _run_new(store, run_id)
    elif job_type == "continue_run":
        await _continue_run(store, run_id)
    elif job_type == "rerun":
        await _rerun_failed(store, run_id, payload)
    elif job_type == "rerun_case":
        await _rerun_case(store, run_id, payload)
    elif job_type == "judge_case":
        await _judge_case(store, run_id, payload)
    elif job_type == "judge_levels":
        await _judge_levels(store, run_id)
    elif job_type == "judge_legacy":
        await _judge_legacy(store, run_id)
    else:
        raise RuntimeError(f"unknown job_type: {job_type}")
    append_run_log(run_id, "worker_job_finish", job_id=job.get("id"), job_type=job_type,
                   run=compact_run(store.get_run(run_id)))


async def _heartbeat(store, job_id: str, worker_id: str, interval_s: float):
    while True:
        await asyncio.sleep(max(1.0, interval_s))
        store.heartbeat_job(job_id, worker_id)


def _worker_max_job_attempts() -> int:
    return max(1, _env_int("BENCH_WORKER_MAX_JOB_ATTEMPTS", 3))


async def process_one_job(store, worker_id: str, *, stale_after_s: float = 900.0,
                          max_job_attempts: int | None = None) -> bool:
    max_job_attempts = max_job_attempts or _worker_max_job_attempts()
    recovered = store.recover_stale_jobs(stale_after_s=stale_after_s, max_attempts=max_job_attempts)
    if recovered.get("stale"):
        LOG.warning("stale jobs recovered", extra={"recovered": recovered})
    job = store.claim_next_job(worker_id, stale_after_s=stale_after_s, max_attempts=max_job_attempts)
    if not job:
        return False
    heartbeat_task = asyncio.create_task(_heartbeat(store, job["id"], worker_id, _env_float("BENCH_WORKER_HEARTBEAT_S", 5.0)))
    try:
        await dispatch_job(store, job)
        store.finish_job(job["id"], worker_id, status="done")
    except asyncio.CancelledError:
        store.finish_job(job["id"], worker_id, status="cancelled", error="worker cancelled")
        raise
    except Exception as exc:  # noqa: BLE001
        err = safe_exception(exc, limit=1000)
        if job.get("run_id"):
            append_run_log(job["run_id"], "worker_job_error", job_id=job.get("id"),
                           job_type=job.get("job_type"), error=err)
            run = store.get_run(job["run_id"])
            if run and run.get("status") not in FINISHED_STATUSES:
                store.update_run(job["run_id"], status="error", finished_at=time.time(), error=err)
        store.finish_job(job["id"], worker_id, status="error", error=err)
    finally:
        heartbeat_task.cancel()
        await asyncio.gather(heartbeat_task, return_exceptions=True)
    return True


def enqueue_unfinished_runs(store) -> int:
    if not _env_flag("BENCH_APP_AUTOCONTINUE_RUNS", True):
        return 0
    active_jobs = store.list_jobs(statuses=("queued", "running"), limit=10000)
    active_run_ids = {job.get("run_id") for job in active_jobs}
    count = 0
    for run in reversed(store.list_runs()):
        run_id = run.get("id")
        if not run_id or run_id in active_run_ids:
            continue
        status = run.get("status")
        legacy_restart = status == "stopped" and run.get("error") == "прервано перезапуском сервера"
        if status not in ACTIVE_STATUSES and not legacy_restart:
            continue
        store.update_run(run_id, status="queued", finished_at=None, error=None)
        store.enqueue_job(run_id, "continue_run", {"source": "worker_autocontinue"})
        append_run_log(run_id, "worker_autocontinue_queued", run=compact_run(store.get_run(run_id)))
        count += 1
    return count


async def _watchdog(store, *, stale_after_s: float, max_job_attempts: int, interval_s: float) -> None:
    while True:
        await asyncio.sleep(max(5.0, interval_s))
        try:
            recovered = await asyncio.to_thread(
                store.recover_stale_jobs,
                stale_after_s=stale_after_s,
                max_attempts=max_job_attempts,
            )
            if recovered.get("stale"):
                LOG.warning("watchdog recovered stale jobs", extra={"recovered": recovered})
        except Exception:  # noqa: BLE001
            LOG.exception("watchdog failed")


async def run_worker() -> None:
    configure_basic_json_logging(os.getenv("BENCH_WORKER_LOG_LEVEL", "INFO"))
    worker_id = os.getenv("BENCH_WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"
    poll_s = max(0.2, _env_float("BENCH_WORKER_POLL_INTERVAL_S", 1.0))
    stale_after_s = max(5.0, _env_float("BENCH_WORKER_STALE_AFTER_S", 900.0))
    max_job_attempts = _worker_max_job_attempts()
    watchdog_interval_s = max(5.0, _env_float("BENCH_WORKER_WATCHDOG_INTERVAL_S", 30.0))
    store = make_store()
    migrate_dataset_paths_to_jsonl(store)
    seed_default_datasets(store)
    queued = enqueue_unfinished_runs(store)
    LOG.info(
        "worker started",
        extra={
            "worker_id": worker_id,
            "queued_unfinished": queued,
            "stale_after_s": stale_after_s,
            "max_job_attempts": max_job_attempts,
            "watchdog_interval_s": watchdog_interval_s,
        },
    )
    watchdog_task = asyncio.create_task(
        _watchdog(store, stale_after_s=stale_after_s,
                  max_job_attempts=max_job_attempts,
                  interval_s=watchdog_interval_s)
    )
    try:
        while True:
            processed = await process_one_job(
                store,
                worker_id,
                stale_after_s=stale_after_s,
                max_job_attempts=max_job_attempts,
            )
            if not processed:
                await asyncio.sleep(poll_s)
    finally:
        watchdog_task.cancel()
        await asyncio.gather(watchdog_task, return_exceptions=True)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
