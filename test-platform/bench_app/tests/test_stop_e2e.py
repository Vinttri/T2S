import asyncio
import importlib
import json
from pathlib import Path

import httpx
import pytest

from leaderboard.benchmark import parse_benchmark_file
from leaderboard.db import SelectResult
from bench_app.state_graph import assert_transition_sequence
from bench_app.store import SQLiteStore


BENCH_JSONL = "\n".join([
    '{"benchmark_id":"S1","case_id":"case_one","difficulty":"Simple","question":"one?","normal_phrasing":"","conditions":"","gold_sql":"SELECT 1;"}',
    '{"benchmark_id":"S2","case_id":"case_two","difficulty":"Simple","question":"two?","normal_phrasing":"","conditions":"","gold_sql":"SELECT 2;"}',
    '{"benchmark_id":"S3","case_id":"case_three","difficulty":"Simple","question":"three?","normal_phrasing":"","conditions":"","gold_sql":"SELECT 3;"}',
]) + "\n"


class FakeExecutor:
    def __init__(self, *_args, **_kwargs):
        pass

    def execute_select(self, _sql):
        return SelectResult(ok=True, rows=[("1",)], columns=["c"], row_count=1)


def write_benchmark(tmp_path):
    path = tmp_path / "BENCHMARK_MOCK.jsonl"
    path.write_text(BENCH_JSONL, encoding="utf-8")
    return str(path)


def write_large_benchmark(tmp_path, count=40):
    path = tmp_path / "BENCHMARK_LARGE.jsonl"
    rows = []
    for idx in range(1, count + 1):
        rows.append(json.dumps({
            "benchmark_id": f"S{idx}",
            "case_id": f"case_{idx}",
            "difficulty": "Simple",
            "question": f"question {idx}?",
            "normal_phrasing": "",
            "conditions": "",
            "gold_sql": "SELECT 1;",
        }, ensure_ascii=False))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return str(path)


def make_store_with_run(tmp_path, *, status="running", done_cases=0):
    store = SQLiteStore(str(tmp_path / "runner.db")).init()
    bench_path = write_benchmark(tmp_path)
    conn = store.save_connector({
        "id": "conn",
        "name": "Mock connector",
        "url": "http://mock.local/sql",
        "body_template": "{}",
        "sql_extract": {"mode": "json", "field": "sql"},
        "timeout": 60,
    })
    ds = store.save_dataset({
        "id": "ds",
        "name": "Mock dataset",
        "benchmark_path": bench_path,
        "db_id": "mock",
        "dsn": "postgresql://mock",
    })
    run = store.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                           connector_id=conn["id"], connector_name=conn["name"],
                           config={"case_timeout": 60})
    store.update_run(run["id"], status=status, total_cases=3, done_cases=done_cases)
    return store, ds, conn, store.get_run(run["id"])


def install_runner_fakes(monkeypatch, runner_mod, connector_cls):
    monkeypatch.setattr(runner_mod, "PgExecutor", FakeExecutor)
    monkeypatch.setattr(runner_mod, "TemplatedConnector", connector_cls)
    monkeypatch.setattr(runner_mod, "_dump_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner_mod, "_dump_answers_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner_mod, "_dump_judged_levels_json", lambda *_args, **_kwargs: None)
    runner_mod._GOLD_RESULT_CACHE.clear()


def load_test_server(monkeypatch, tmp_path):
    monkeypatch.setenv("BENCH_STORE_URL", f"sqlite:///{tmp_path / 'server.db'}")
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE_DIR", str(tmp_path / "gold_cache"))
    monkeypatch.setenv("BENCH_APP_SYNC_CONNECTOR_YAML", "0")
    monkeypatch.setenv("BENCH_APP_AUTO_JUDGE", "0")
    monkeypatch.setenv("BENCH_APP_RUNNER_MODE", "inline")
    monkeypatch.setenv("BENCH_MOCK_POSTGRES_DSN", "postgresql://env_user:env_pass@db.local/mock")
    import bench_app.server as server

    server = importlib.reload(server)
    server._TASKS.clear()
    server._RUN_TASKS.clear()
    server.bus._cases.clear()
    return server


def connector_payload(*, name="Mock connector", url="http://mock.local/sql", **overrides):
    data = {
        "id": "conn",
        "name": name,
        "url": url,
        "body_template": "{}",
        "sql_extract": {"mode": "json", "field": "sql"},
        "default_dialect": "postgres",
        "timeout": 60,
    }
    data.update(overrides)
    return data


def dataset_payload(tmp_path, *, name="Mock dataset", **overrides):
    data = {
        "id": "ds",
        "name": name,
        "benchmark_path": write_benchmark(tmp_path),
        "db_id": "mock",
        "dsn": "postgresql://mock",
        "db_type": "postgres",
    }
    data.update(overrides)
    return data


async def wait_for_run(client, run_id, predicate, *, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = (await client.get(f"/api/runs/{run_id}", params={"cases": 0})).json()
        if predicate(last):
            return last
        await asyncio.sleep(0.01)
    raise AssertionError(f"run did not reach expected state: {last}")


async def wait_until(predicate, *, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition did not become true")


def test_stop_endpoint_marks_stopped_and_cancels_run_task(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_run_task(store, run_id, *_args, **_kwargs):
        store.update_run(run_id, status="running", total_cases=3)
        server.bus.publish({"type": "run", "run": store.get_run(run_id)})
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(server, "run_task", fake_run_task)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            connector = (await client.post("/api/connectors", json={
                "id": "conn",
                "name": "Mock connector",
                "url": "http://mock.local/sql",
                "body_template": "{}",
                "sql_extract": {"mode": "json", "field": "sql"},
                "default_dialect": "postgres",
            })).json()
            dataset = (await client.post("/api/datasets", json={
                "id": "ds",
                "name": "Mock dataset",
                "benchmark_path": write_benchmark(tmp_path),
                "db_id": "mock",
                "dsn": "postgresql://mock",
                "db_type": "postgres",
            })).json()
            run = (await client.post("/api/runs", json={
                "dataset_id": dataset["id"],
                "connector_id": connector["id"],
                "case_timeout": 60,
            })).json()
            await asyncio.wait_for(started.wait(), timeout=1)

            stopped = (await client.post(f"/api/runs/{run['id']}/stop")).json()
            assert stopped["status"] == "stopped"
            assert stopped["cancelled_tasks"] == 1
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            got = (await client.get(f"/api/runs/{run['id']}")).json()
            assert got["status"] == "stopped"
            assert got["error"] == "остановлено пользователем"
            assert server._RUN_TASKS.get(run["id"]) in (None, set())

    asyncio.run(scenario())


def test_env_concurrency_limits_are_reported_and_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_APP_MAX_API_CONCURRENCY", "1")
    monkeypatch.setenv("LLM_JUDGE_CONCURRENCY", "1")
    monkeypatch.setenv("BENCH_APP_MAX_IMPALA_CONCURRENCY", "1")
    server = load_test_server(monkeypatch, tmp_path)
    seen = []

    async def fake_run_task(store, run_id, *_args, **kwargs):
        seen.append(kwargs)
        store.update_run(run_id, status="done", total_cases=0, done_cases=0,
                         summary={"total": 0, "done": 0})
        server.bus.publish({"type": "run", "run": store.get_run(run_id)})

    monkeypatch.setattr(server, "run_task", fake_run_task)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            settings = (await client.get("/api/settings")).json()
            assert settings["limits"]["api_concurrency"] == 1
            assert settings["limits"]["judge_concurrency"] == 1
            assert settings["limits"]["impala_concurrency"] == 1
            assert settings["limits"]["env"]["api_concurrency"] == "BENCH_APP_MAX_API_CONCURRENCY"
            assert settings["limits"]["env"]["impala_concurrency"] == "BENCH_APP_MAX_IMPALA_CONCURRENCY"

            await client.post("/api/connectors", json=connector_payload(db_id="mock"))
            await client.post("/api/datasets", json=dataset_payload(tmp_path, db_id="mock"))
            run = (await client.post("/api/runs", json={
                "dataset_id": "ds",
                "connector_id": "conn",
                "concurrency": 5,
                "case_timeout": 60,
            })).json()
            await wait_until(lambda: len(seen) == 1)
            assert run["config"]["requested_concurrency"] == 5
            assert run["config"]["concurrency"] == 1
            assert run["config"]["api_concurrency_limit"] == 1
            assert run["config"]["impala_concurrency_limit"] == 1
            assert seen[0]["concurrency"] == 1
            assert seen[0]["api_global_concurrency"] == 1
            assert seen[0]["judge_concurrency"] == 1

            repeated = (await client.post(f"/api/runs/{run['id']}/repeat")).json()
            await wait_until(lambda: len(seen) == 2)
            assert repeated["config"]["requested_concurrency"] == 5
            assert repeated["config"]["concurrency"] == 1
            assert repeated["config"]["api_concurrency_limit"] == 1
            assert repeated["config"]["impala_concurrency_limit"] == 1
            assert seen[1]["concurrency"] == 1
            assert seen[1]["api_global_concurrency"] == 1
            assert seen[1]["judge_concurrency"] == 1

    asyncio.run(scenario())


def test_worker_mode_trigger_enqueues_job_without_inline_task(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    monkeypatch.setenv("BENCH_APP_RUNNER_MODE", "worker")
    called = False

    async def fail_run_task(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("worker mode must not run inline")

    monkeypatch.setattr(server, "run_task", fail_run_task)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/connectors", json=connector_payload(db_id="mock"))
            await client.post("/api/datasets", json=dataset_payload(tmp_path, db_id="mock"))
            resp = await client.post("/api/runs", json={
                "dataset_id": "ds",
                "connector_id": "conn",
                "concurrency": 3,
                "case_timeout": 60,
            })
            assert resp.status_code == 200
            run = resp.json()
            jobs = server.STORE.list_jobs(run_id=run["id"], statuses=("queued", "running"))
            assert len(jobs) == 1
            assert jobs[0]["job_type"] == "run"
            assert jobs[0]["payload"]["source"] == "api"
            assert server._RUN_TASKS == {}
            assert called is False
            health = (await client.get("/api/health")).json()
            assert health["runner_mode"] == "worker"
            assert health["jobs"]["queued"] == 1

    asyncio.run(scenario())


def test_connector_rename_during_active_run_syncs_run_name_but_keeps_task_snapshot(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()
    seen = {}

    async def fake_run_task(store, run_id, dataset, connector, *_args, **_kwargs):
        seen["connector_name"] = connector["name"]
        seen["connector_url"] = connector["url"]
        store.update_run(run_id, status="running", total_cases=1)
        server.bus.publish({"type": "run", "run": store.get_run(run_id)})
        started.set()
        await release.wait()
        store.add_case(run_id, 1, {"case_id": "case_one", "difficulty": "Simple",
                                   "question": "one?", "gold_sql": "SELECT 1",
                                   "predicted_sql": "SELECT 1", "level": 4,
                                   "matched": True})
        store.update_run(run_id, status="done", done_cases=1,
                         summary={"total": 1, "done": 1, "passed": 1,
                                  "accuracy": 100.0, "L0": 0, "L1": 0,
                                  "L2": 0, "L3": 0, "L4": 1})
        server.bus.publish({"type": "run", "run": store.get_run(run_id)})

    monkeypatch.setattr(server, "run_task", fake_run_task)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/connectors", json=connector_payload(name="Original connector",
                                                                        url="http://mock.local/original"))
            await client.post("/api/datasets", json=dataset_payload(tmp_path))
            run = (await client.post("/api/runs", json={
                "dataset_id": "ds",
                "connector_id": "conn",
                "case_timeout": 60,
            })).json()
            await asyncio.wait_for(started.wait(), timeout=1)

            await client.post("/api/connectors", json=connector_payload(name="Renamed connector",
                                                                        url="http://mock.local/renamed"))
            renamed = (await client.get(f"/api/runs/{run['id']}", params={"cases": 0})).json()
            assert renamed["connector_id"] == "conn"
            assert renamed["connector_name"] == "Renamed connector"
            assert seen == {
                "connector_name": "Original connector",
                "connector_url": "http://mock.local/original",
            }

            release.set()
            done = await wait_for_run(client, run["id"], lambda r: r["status"] == "done")
            assert done["connector_name"] == "Renamed connector"

    asyncio.run(scenario())


def test_connector_rename_publishes_run_update_to_progress_bus(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload(name="Original connector"))
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"])
    q = server.bus.subscribe()

    async def scenario():
        try:
            transport = httpx.ASGITransport(app=server.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/api/connectors",
                                         json=connector_payload(name="Renamed connector"))
                assert resp.status_code == 200
                msg = await asyncio.wait_for(q.get(), timeout=1)
                assert msg["type"] == "run"
                assert msg["run"]["id"] == run["id"]
                assert msg["run"]["connector_id"] == "conn"
                assert msg["run"]["connector_name"] == "Renamed connector"
        finally:
            server.bus.unsubscribe(q)

    asyncio.run(scenario())


def test_invalid_connector_edit_does_not_mutate_existing_runs(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload(name="Stable connector",
                                                         url="http://mock.local/stable"))
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"])

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/connectors",
                                     json=connector_payload(name="Bad edit",
                                                            url="ws://mock.local/socket"))
            assert resp.status_code == 400
            assert server.STORE.get_connector("conn")["name"] == "Stable connector"
            assert server.STORE.get_connector("conn")["url"] == "http://mock.local/stable"
            assert server.STORE.get_run(run["id"])["connector_name"] == "Stable connector"

    asyncio.run(scenario())


def test_connector_config_edit_during_active_run_only_affects_future_repeat(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    first_started = asyncio.Event()
    first_release = asyncio.Event()
    snapshots = []

    async def fake_run_task(store, run_id, _dataset, connector, *_args, **_kwargs):
        snapshots.append({
            "name": connector.get("name"),
            "url": connector.get("url"),
            "body_template": connector.get("body_template"),
            "sql_extract": connector.get("sql_extract"),
            "timeout": connector.get("timeout"),
            "max_attempts": connector.get("max_attempts"),
            "retry_delay": connector.get("retry_delay"),
            "default_dialect": connector.get("default_dialect"),
            "db_id": connector.get("db_id"),
        })
        store.update_run(run_id, status="running", total_cases=1)
        server.bus.publish({"type": "run", "run": store.get_run(run_id)})
        if len(snapshots) == 1:
            first_started.set()
            await first_release.wait()
        store.add_case(run_id, 1, {"case_id": "case_one", "difficulty": "Simple",
                                   "question": "one?", "gold_sql": "SELECT 1",
                                   "predicted_sql": "SELECT 1", "level": 4,
                                   "matched": True})
        store.update_run(run_id, status="done", done_cases=1,
                         summary={"total": 1, "done": 1, "passed": 1,
                                  "accuracy": 100.0, "L0": 0, "L1": 0,
                                  "L2": 0, "L3": 0, "L4": 1})
        server.bus.publish({"type": "run", "run": store.get_run(run_id)})

    monkeypatch.setattr(server, "run_task", fake_run_task)

    original = connector_payload(
        name="Original connector",
        url="http://mock.local/original",
        body_template='{"question":"{{question}}","version":"old"}',
        sql_extract={"mode": "json", "field": "sql"},
        timeout=10,
        max_attempts=1,
        retry_delay=0,
        db_id="mock",
    )
    edited = connector_payload(
        name="Edited connector",
        url="http://mock.local/edited",
        body_template='{"prompt":"{{question}}","version":"new"}',
        sql_extract={"mode": "regex", "pattern": r"SQL:\\s*(.*)"},
        timeout=99,
        max_attempts=5,
        retry_delay=2.5,
        db_id="mock",
    )

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/connectors", json=original)
            await client.post("/api/datasets", json=dataset_payload(tmp_path))
            run = (await client.post("/api/runs", json={
                "dataset_id": "ds",
                "connector_id": "conn",
                "case_timeout": 60,
            })).json()
            await asyncio.wait_for(first_started.wait(), timeout=1)

            await client.post("/api/connectors", json=edited)
            assert snapshots[0]["name"] == "Original connector"
            assert snapshots[0]["url"] == "http://mock.local/original"
            assert snapshots[0]["body_template"] == original["body_template"]
            assert snapshots[0]["sql_extract"] == original["sql_extract"]
            assert snapshots[0]["timeout"] == 10
            assert snapshots[0]["max_attempts"] == 1
            assert snapshots[0]["retry_delay"] == 0

            first_release.set()
            done = await wait_for_run(client, run["id"], lambda r: r["status"] == "done")
            assert done["connector_name"] == "Edited connector"

            repeated = (await client.post(f"/api/runs/{run['id']}/repeat")).json()
            assert repeated["connector_name"] == "Edited connector"
            await wait_until(lambda: len(snapshots) == 2)
            assert snapshots[1]["name"] == "Edited connector"
            assert snapshots[1]["url"] == "http://mock.local/edited"
            assert snapshots[1]["body_template"] == edited["body_template"]
            assert snapshots[1]["sql_extract"] == edited["sql_extract"]
            assert snapshots[1]["timeout"] == 99
            assert snapshots[1]["max_attempts"] == 5
            assert snapshots[1]["retry_delay"] == 2.5

    asyncio.run(scenario())


def test_rerun_and_repeat_reject_incompatible_connector_edits(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path, db_id="mock", db_type="postgres"))
    conn = server.STORE.save_connector(connector_payload(name="Compatible connector", db_id="mock",
                                                         default_dialect="postgres"))
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"])
    server.STORE.update_run(run["id"], status="done", total_cases=3, done_cases=3)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/connectors",
                              json=connector_payload(name="Impala edit", db_id="mock",
                                                     default_dialect="impala"))
            for path in (f"/api/runs/{run['id']}/repeat", f"/api/runs/{run['id']}/rerun"):
                resp = await client.post(path)
                assert resp.status_code == 400
                assert "Несовместимо" in resp.json()["detail"]

            await client.post("/api/connectors",
                              json=connector_payload(name="Wrong DB edit", db_id="another_db",
                                                     default_dialect="postgres"))
            case_resp = await client.post(f"/api/runs/{run['id']}/rerun-case",
                                          json={"case_id": "case_one"})
            assert case_resp.status_code == 400
            assert "привязан к БД" in case_resp.json()["detail"]
            repeat_resp = await client.post(f"/api/runs/{run['id']}/repeat")
            assert repeat_resp.status_code == 400
            assert "привязан к БД" in repeat_resp.json()["detail"]

    asyncio.run(scenario())


def test_rerun_endpoint_passes_env_judge_for_non_l4_targets(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    monkeypatch.setenv("BENCH_APP_AUTO_JUDGE", "1")
    monkeypatch.setenv("LLM_BASE_URL", "http://judge.local/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "judge-model")

    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload())
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"])
    server.STORE.update_run(run["id"], status="done", total_cases=3, done_cases=3)
    server.STORE.add_case(run["id"], 1, {"case_id": "case_one", "difficulty": "Simple",
                                         "question": "q1", "gold_sql": "SELECT 1",
                                         "predicted_sql": "SELECT bad", "level": 1,
                                         "matched": False})
    server.STORE.add_case(run["id"], 2, {"case_id": "case_two", "difficulty": "Simple",
                                         "question": "q2", "gold_sql": "SELECT 2",
                                         "predicted_sql": "SELECT 2", "level": 4,
                                         "matched": True})
    server.STORE.add_case(run["id"], 3, {"case_id": "case_three", "difficulty": "Simple",
                                         "question": "q3", "gold_sql": "SELECT 3",
                                         "predicted_sql": "SELECT 3", "level": 4,
                                         "matched": True})

    called = asyncio.Event()
    captured = {}

    async def fake_rerun(_store, rid, **kwargs):
        captured["run_id"] = rid
        captured.update(kwargs)
        called.set()

    monkeypatch.setattr(server, "rerun", fake_rerun)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/runs/{run['id']}/rerun")
            assert resp.status_code == 200
            body = resp.json()
            assert body["targets"] == 1
            await asyncio.wait_for(called.wait(), timeout=1)
            assert captured["run_id"] == run["id"]
            assert captured["judge_cfg"]["model"] == "judge-model"
            assert captured["judge_cfg"]["base_url"] == "http://judge.local/v1"
            assert captured["api_global_concurrency"] == 1
            assert captured["judge_global_concurrency"] == 1

    asyncio.run(scenario())


def test_worker_rerun_failed_from_stopped_run_queues_and_uses_runtime_config(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    monkeypatch.setenv("BENCH_APP_RUNNER_MODE", "worker")
    monkeypatch.setenv("BENCH_APP_AUTO_JUDGE", "1")
    monkeypatch.setenv("LLM_BASE_URL", "http://judge.local/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "runtime-judge")
    monkeypatch.setenv("LLM_JUDGE_TIMEOUT", "77")
    monkeypatch.setenv("LLM_JUDGE_CONCURRENCY", "1")
    monkeypatch.setenv("LLM_JUDGE_MAX_RETRIES", "3")
    monkeypatch.setenv("LLM_JUDGE_RETRY_DELAY", "4")
    monkeypatch.setenv("BENCH_APP_MAX_API_CONCURRENCY", "1")
    import bench_app.worker as worker_mod

    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload())
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"],
                                  config={"auto_judge": False, "api_concurrency_limit": 9,
                                          "judge_timeout": 5})
    server.STORE.update_run(run["id"], status="stopped", total_cases=3, done_cases=3,
                            error="остановлено пользователем")
    server.STORE.add_case(run["id"], 1, {"case_id": "case_one", "difficulty": "Simple",
                                         "question": "q1", "gold_sql": "SELECT 1",
                                         "predicted_sql": "SELECT bad", "level": 1,
                                         "matched": False})
    server.STORE.add_case(run["id"], 2, {"case_id": "case_two", "difficulty": "Simple",
                                         "question": "q2", "gold_sql": "SELECT 2",
                                         "predicted_sql": "SELECT 2", "level": 4,
                                         "matched": True})
    server.STORE.add_case(run["id"], 3, {"case_id": "case_three", "difficulty": "Simple",
                                         "question": "q3", "gold_sql": "SELECT 3",
                                         "predicted_sql": "SELECT 3", "level": 4,
                                         "matched": True})
    captured = {}

    async def fake_rerun(store_arg, rid, **kwargs):
        captured["run_id"] = rid
        captured.update(kwargs)
        store_arg.update_run(rid, status="done", done_cases=3,
                             summary={"total": 3, "done": 3})

    monkeypatch.setattr(worker_mod, "rerun", fake_rerun)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/runs/{run['id']}/rerun")
            assert resp.status_code == 200
            body = resp.json()
            assert body["targets"] == 1
            assert body["status"] == "queued"
            assert server.STORE.get_run(run["id"])["status"] == "queued"
            jobs = server.STORE.list_jobs(run_id=run["id"], statuses=("queued",))
            assert len(jobs) == 1
            assert jobs[0]["payload"]["auto_judge"] is True
            assert jobs[0]["payload"]["api_concurrency_limit"] == 1

            processed = await worker_mod.process_one_job(server.STORE, "worker-1")
            assert processed is True
            assert captured["run_id"] == run["id"]
            assert captured["judge_cfg"]["model"] == "runtime-judge"
            assert captured["judge_timeout"] == 77
            assert captured["judge_max_retries"] == 3
            assert captured["judge_retry_delay"] == 4
            assert captured["api_global_concurrency"] == 1
            assert captured["judge_global_concurrency"] == 1
            assert server.STORE.get_run(run["id"])["status"] == "done"

    asyncio.run(scenario())


def test_inline_rerun_failed_from_stopped_run_reenters_through_queued(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload())
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"])
    server.STORE.update_run(run["id"], status="stopped", total_cases=3, done_cases=1,
                            error="остановлено пользователем")
    server.STORE.add_case(run["id"], 1, {"case_id": "case_one", "difficulty": "Simple",
                                         "question": "q1", "gold_sql": "SELECT 1",
                                         "predicted_sql": None, "level": 0,
                                         "matched": False, "error": "no SQL extracted from response"})
    states = ["queued", "stopped"]
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_rerun(store_arg, rid, **_kwargs):
        states.append(store_arg.get_run(rid)["status"])
        store_arg.update_run(rid, status="running", finished_at=None, error=None)
        states.append(store_arg.get_run(rid)["status"])
        started.set()
        await release.wait()
        store_arg.update_run(rid, status="done", done_cases=3,
                             summary={"total": 3, "done": 3, "passed": 3, "accuracy": 100.0})
        states.append(store_arg.get_run(rid)["status"])

    monkeypatch.setattr(server, "rerun", fake_rerun)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/runs/{run['id']}/rerun")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "rerunning"
            assert body["targets"] == 3
            await asyncio.wait_for(started.wait(), timeout=1)
            assert states[:4] == ["queued", "stopped", "queued", "running"]
            release.set()
            done = await wait_for_run(client, run["id"], lambda r: r["status"] == "done")
            assert done["status"] == "done"

    asyncio.run(scenario())
    assert_transition_sequence("run", states)


def test_worker_rerun_case_from_stopped_run_queues_and_uses_runtime_config(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    monkeypatch.setenv("BENCH_APP_RUNNER_MODE", "worker")
    monkeypatch.setenv("BENCH_APP_AUTO_JUDGE", "1")
    monkeypatch.setenv("LLM_BASE_URL", "http://judge.local/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "runtime-judge")
    monkeypatch.setenv("LLM_JUDGE_TIMEOUT", "88")
    import bench_app.worker as worker_mod

    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload())
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"],
                                  config={"auto_judge": False, "judge_timeout": 5})
    server.STORE.update_run(run["id"], status="stopped", total_cases=3, done_cases=1,
                            error="остановлено пользователем")
    captured = {}

    async def fake_rerun_api_case(store_arg, rid, case_id, **kwargs):
        captured["run_id"] = rid
        captured["case_id"] = case_id
        captured.update(kwargs)
        store_arg.update_run(rid, status="done", done_cases=1,
                             summary={"total": 3, "done": 1})

    monkeypatch.setattr(worker_mod, "rerun_api_case", fake_rerun_api_case)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/runs/{run['id']}/rerun-case",
                                     json={"case_id": "case_one"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "queued"
            assert body["case_id"] == "case_one"
            assert server.STORE.get_run(run["id"])["status"] == "queued"
            jobs = server.STORE.list_jobs(run_id=run["id"], statuses=("queued",))
            assert len(jobs) == 1
            assert jobs[0]["payload"]["case_id"] == "case_one"
            assert jobs[0]["payload"]["auto_judge"] is True

            processed = await worker_mod.process_one_job(server.STORE, "worker-1")
            assert processed is True
            assert captured["run_id"] == run["id"]
            assert captured["case_id"] == "case_one"
            assert captured["judge_cfg"]["model"] == "runtime-judge"
            assert captured["judge_timeout"] == 88
            assert server.STORE.get_run(run["id"])["status"] == "done"

    asyncio.run(scenario())


def test_rerun_case_rejects_missing_dataset_case_without_queuing(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    monkeypatch.setenv("BENCH_APP_RUNNER_MODE", "worker")
    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload())
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"])
    server.STORE.update_run(run["id"], status="stopped", total_cases=3, done_cases=1,
                            error="остановлено пользователем")

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/runs/{run['id']}/rerun-case",
                                     json={"case_id": "case_deleted"})
            assert resp.status_code == 404
            assert "case_id" in resp.json()["detail"]
            assert server.STORE.get_run(run["id"])["status"] == "stopped"
            assert server.STORE.list_jobs(run_id=run["id"], statuses=("queued",)) == []

    asyncio.run(scenario())


def test_active_run_finishes_after_connector_deleted(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run_task(store, run_id, *_args, **_kwargs):
        store.update_run(run_id, status="running", total_cases=1)
        server.bus.publish({"type": "run", "run": store.get_run(run_id)})
        started.set()
        await release.wait()
        store.add_case(run_id, 1, {"case_id": "case_one", "difficulty": "Simple",
                                   "question": "one?", "gold_sql": "SELECT 1",
                                   "predicted_sql": "SELECT 1", "level": 4,
                                   "matched": True})
        store.update_run(run_id, status="done", done_cases=1,
                         summary={"total": 1, "done": 1, "passed": 1,
                                  "accuracy": 100.0, "L0": 0, "L1": 0,
                                  "L2": 0, "L3": 0, "L4": 1})
        server.bus.publish({"type": "run", "run": store.get_run(run_id)})

    monkeypatch.setattr(server, "run_task", fake_run_task)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/connectors", json=connector_payload(name="Disposable connector"))
            await client.post("/api/datasets", json=dataset_payload(tmp_path))
            run = (await client.post("/api/runs", json={
                "dataset_id": "ds",
                "connector_id": "conn",
                "case_timeout": 60,
            })).json()
            await asyncio.wait_for(started.wait(), timeout=1)
            assert (await client.delete("/api/connectors/conn")).json() == {"ok": True}
            assert server.STORE.get_connector("conn") is None

            release.set()
            done = await wait_for_run(client, run["id"], lambda r: r["status"] == "done")
            assert done["connector_name"] == "Disposable connector"

    asyncio.run(scenario())


def test_rerun_endpoints_reject_deleted_connector_cleanly(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload())
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"])
    server.STORE.update_run(run["id"], status="stopped", total_cases=3, done_cases=0)
    server.STORE.delete_connector(conn["id"])

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            rerun_resp = await client.post(f"/api/runs/{run['id']}/rerun")
            assert rerun_resp.status_code == 400
            assert "коннектор" in rerun_resp.json()["detail"]
            case_resp = await client.post(f"/api/runs/{run['id']}/rerun-case",
                                          json={"case_id": "case_one"})
            assert case_resp.status_code == 400
            assert "коннектор" in case_resp.json()["detail"]
            assert server.STORE.get_run(run["id"])["status"] == "stopped"

    asyncio.run(scenario())


def test_autocontinue_plan_does_not_rerun_scored_failures(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload())
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"],
                                  config={"auto_judge": True})
    server.STORE.update_run(run["id"], status="running", total_cases=3, done_cases=2)
    server.STORE.add_case(run["id"], 1, {
        "case_id": "case_one",
        "difficulty": "Simple",
        "question": "one?",
        "gold_sql": "SELECT 1;",
        "predicted_sql": None,
        "level": 0,
        "matched": False,
        "error": "no SQL extracted from response",
        "case_status": "judged",
    })
    server.STORE.add_case(run["id"], 2, {
        "case_id": "case_two",
        "difficulty": "Simple",
        "question": "two?",
        "gold_sql": "SELECT 2;",
        "predicted_sql": "SELECT 2;",
        "level": None,
        "matched": False,
        "attempts": 1,
        "case_status": "llm_queued",
    })

    connector_targets, judge_targets, total = server._autocontinue_plan(
        server.STORE.get_run(run["id"]),
        ds,
    )

    assert total == 3
    assert connector_targets == ["case_three"]
    assert judge_targets == ["case_two"]


def test_autocontinue_schedules_active_and_legacy_restart_runs_only(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload())
    active = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                     connector_id=conn["id"], connector_name=conn["name"])
    user_stopped = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                           connector_id=conn["id"], connector_name=conn["name"])
    legacy_stopped = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                             connector_id=conn["id"], connector_name=conn["name"])
    server.STORE.update_run(active["id"], status="running", total_cases=3)
    server.STORE.update_run(user_stopped["id"], status="stopped", total_cases=3,
                            error="остановлено пользователем")
    server.STORE.update_run(legacy_stopped["id"], status="stopped", total_cases=3,
                            error="прервано перезапуском сервера")
    called = []

    async def fake_autocontinue(run_id):
        called.append(run_id)
        server.STORE.update_run(run_id, status="done")

    monkeypatch.setattr(server, "_autocontinue_run", fake_autocontinue)

    async def scenario():
        result = await server._autocontinue_unfinished_runs()
        tasks = list(server._TASKS)
        if tasks:
            await asyncio.gather(*tasks)
        return result

    result = asyncio.run(scenario())

    assert result == {"enabled": True, "scheduled": 2, "failed": 0}
    assert called == [active["id"], legacy_stopped["id"]]
    assert server.STORE.get_run(user_stopped["id"])["status"] == "stopped"


def test_autocontinue_marks_error_when_required_judge_is_not_configured(tmp_path, monkeypatch):
    for key in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(key, raising=False)
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload())
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"],
                                  config={"auto_judge": True})
    server.STORE.update_run(run["id"], status="queued", total_cases=3)

    asyncio.run(server._autocontinue_run_safe(run["id"]))

    got = server.STORE.get_run(run["id"])
    assert got["status"] == "error"
    assert "LLM judge не настроен" in got["error"]


def test_rerun_endpoint_rejects_deleted_dataset_cleanly(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path))
    conn = server.STORE.save_connector(connector_payload())
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"])
    server.STORE.update_run(run["id"], status="stopped", total_cases=3, done_cases=0)
    server.STORE.delete_dataset(ds["id"])

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(f"/api/runs/{run['id']}/rerun")
            assert resp.status_code == 400
            assert "датасет" in resp.json()["detail"]
            assert server.STORE.get_run(run["id"])["status"] == "stopped"

    asyncio.run(scenario())


def test_dataset_download_endpoint_returns_jsonl_file(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path, name="Download dataset"))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/datasets/{ds['id']}/download")
            assert resp.status_code == 200
            assert "attachment" in resp.headers.get("content-disposition", "")
            assert "Download_dataset.jsonl" in resp.headers.get("content-disposition", "")
            assert '"case_id":"case_one"' in resp.text
            missing = await client.get("/api/datasets/missing/download")
            assert missing.status_code == 404

    asyncio.run(scenario())


def test_dataset_case_editor_lists_and_updates_questions(tmp_path, monkeypatch):
    runtime_datasets = tmp_path / "runtime_datasets"
    monkeypatch.setenv("BENCH_APP_DATASETS_DIR", str(runtime_datasets))
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path, name="Editable dataset"))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            listed = await client.get(f"/api/datasets/{ds['id']}/cases")
            assert listed.status_code == 200
            body = listed.json()
            assert body["count"] == 3
            assert body["cases"][0]["case_id"] == "case_one"

            edited = {
                "benchmark_id": "S1",
                "case_id": "case_one_edited",
                "difficulty": "Medium",
                "question": "one edited?",
                "normal_phrasing": "one normal",
                "conditions": {"order": True},
                "gold_sql": "SELECT 10",
            }
            resp = await client.put(f"/api/datasets/{ds['id']}/cases/case_one", json=edited)
            assert resp.status_code == 200
            saved = resp.json()
            assert saved["case"]["case_id"] == "case_one_edited"
            saved_path = Path(server.STORE.get_dataset(ds["id"])["benchmark_path"])
            assert saved_path.parent == runtime_datasets.resolve()
            meta = server.STORE.get_dataset(ds["id"])["meta"]
            assert meta["user_edited_dataset"] is True
            assert meta["seeded_default"] is False
            cases = parse_benchmark_file(saved_path)
            assert cases[0].case_id == "case_one_edited"
            assert cases[0].question == "one edited?"
            assert cases[0].conditions == '{"order":true}'
            first_saved_path = saved_path
            first_saved_text = saved_path.read_text(encoding="utf-8")
            assert '"gold_sql": "SELECT 10"' in first_saved_text

            second = await client.put(f"/api/datasets/{ds['id']}/cases/case_one_edited", json={
                **edited,
                "question": "one edited again?",
                "gold_sql": "SELECT 11",
            })
            assert second.status_code == 200
            saved_path = Path(server.STORE.get_dataset(ds["id"])["benchmark_path"])
            assert saved_path == first_saved_path
            second_saved_text = saved_path.read_text(encoding="utf-8")
            assert "one edited again?" in second_saved_text
            assert '"gold_sql": "SELECT 11"' in second_saved_text
            assert '"gold_sql": "SELECT 10"' not in second_saved_text

            duplicate = await client.put(f"/api/datasets/{ds['id']}/cases/case_two", json={
                **edited,
                "case_id": "case_three",
                "question": "duplicate?",
                "gold_sql": "SELECT 20",
            })
            assert duplicate.status_code == 400
            assert "case_id" in duplicate.json()["detail"]

    asyncio.run(scenario())


def test_dataset_edit_resolves_dsn_from_env_and_syncs_run_name(tmp_path, monkeypatch):
    env_dsn = "postgresql://env_user:env_pass@db.local/mock"
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = (await client.post("/api/datasets", json={
                **dataset_payload(tmp_path, name="Original dataset"),
                "dsn": "postgresql://login:pwd@db.local/ignored",
            })).json()
            conn = server.STORE.save_connector(connector_payload())
            run = server.STORE.create_run(dataset_id=created["id"], dataset_name=created["name"],
                                          connector_id=conn["id"], connector_name=conn["name"])
            listed = (await client.get("/api/datasets")).json()
            current = next(d for d in listed if d["id"] == created["id"])
            assert "<redacted>" in current["dsn"]

            updated = (await client.post("/api/datasets", json={
                "id": current["id"],
                "name": "Renamed dataset",
                "benchmark_path": current["benchmark_path"],
                "db_id": "mock",
                "dsn": current["dsn"],
                "db_type": "postgres",
            })).json()

            assert updated["name"] == "Renamed dataset"
            assert server.STORE.get_dataset(current["id"])["dsn"] == env_dsn
            assert server.STORE.get_dataset(current["id"])["meta"]["dsn_source_env"] == "BENCH_MOCK_POSTGRES_DSN"
            assert server.STORE.get_run(run["id"])["dataset_name"] == "Renamed dataset"

    asyncio.run(scenario())


def test_dataset_edit_recomputes_redacted_dsn_when_db_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_MOCK_POSTGRES_DSN", "postgresql://env_user:env_pass@db.local/mock")
    env_dsn = "postgresql://other_user:other_pass@db.local/other"
    monkeypatch.setenv("BENCH_OTHER_POSTGRES_DSN", env_dsn)
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = (await client.post("/api/datasets", json={
                **dataset_payload(tmp_path, name="Original dataset"),
                "dsn": "postgresql://login:pwd@db.local/ignored",
            })).json()
            visible = (await client.get("/api/datasets")).json()
            current = next(d for d in visible if d["id"] == created["id"])
            assert "<redacted>" in current["dsn"]

            updated = (await client.post("/api/datasets", json={
                "id": current["id"],
                "name": "Other dataset",
                "benchmark_path": current["benchmark_path"],
                "db_id": "other",
                "dsn": current["dsn"],
                "db_type": "postgres",
            })).json()

            internal = server.STORE.get_dataset(current["id"])
            assert updated["db_id"] == "other"
            assert internal["dsn"] == env_dsn
            assert internal["meta"]["dsn_source_env"] == "BENCH_OTHER_POSTGRES_DSN"

    asyncio.run(scenario())


def test_dataset_api_rejects_markdown_benchmark_path(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            payload = dataset_payload(tmp_path, name="Bad dataset")
            payload["benchmark_path"] = str(tmp_path / "BENCHMARK_BAD.md")
            resp = await client.post("/api/datasets", json=payload)
            assert resp.status_code == 400
            assert "JSONL" in resp.json()["detail"]

    asyncio.run(scenario())


def test_dataset_upload_can_update_existing_dataset(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path, name="Upload target",
                                                   dsn="postgresql://login:pwd@db.local/mock"))
    new_content = BENCH_JSONL

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            visible = (await client.get("/api/datasets")).json()
            redacted = next(d for d in visible if d["id"] == ds["id"])
            resp = await client.post("/api/datasets/upload", json={
                "id": ds["id"],
                "name": "Upload target edited",
                "file_name": "edited.jsonl",
                "content": new_content,
                "db_id": "mock",
                "dsn": redacted["dsn"],
                "db_type": "postgres",
            })
            assert resp.status_code == 200
            saved = resp.json()
            assert saved["id"] == ds["id"]
            assert saved["name"] == "Upload target edited"
            assert server.STORE.get_dataset(ds["id"])["dsn"] == "postgresql://env_user:env_pass@db.local/mock"
            saved_path = Path(server.STORE.get_dataset(ds["id"])["benchmark_path"])
            assert saved_path.suffix == ".jsonl"
            assert parse_benchmark_file(saved_path)[0].case_id == "case_one"

    asyncio.run(scenario())


def test_dataset_api_rejects_duplicate_names(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = (await client.post("/api/datasets", json={
                **dataset_payload(tmp_path, name="Sports Events"),
                "id": "sports-a",
            })).json()
            same = await client.post("/api/datasets", json={
                **dataset_payload(tmp_path, name="  sports   events  "),
                "id": "sports-b",
            })
            assert same.status_code == 400
            assert "уже существует" in same.json()["detail"]

            update_same_id = await client.post("/api/datasets", json={
                **first,
                "name": "Sports Events",
            })
            assert update_same_id.status_code == 200
            assert update_same_id.json()["id"] == first["id"]

    asyncio.run(scenario())


def test_dataset_upload_rejects_duplicate_names_before_saving_file(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.post("/api/datasets/upload", json={
                "name": "Upload Duplicate",
                "file_name": "first.jsonl",
                "content": BENCH_JSONL,
                "db_id": "mock",
                "dsn": "postgresql://login:pwd@db.local/mock",
                "db_type": "postgres",
            })
            assert first.status_code == 200
            existing_paths = {
                Path(item["benchmark_path"])
                for item in server.STORE.list_datasets()
                if item.get("benchmark_path")
            }
            duplicate = await client.post("/api/datasets/upload", json={
                "name": " upload duplicate ",
                "file_name": "second.jsonl",
                "content": BENCH_JSONL,
                "db_id": "mock",
                "dsn": "postgresql://login:pwd@db.local/mock",
                "db_type": "postgres",
            })
            assert duplicate.status_code == 400
            assert "уже существует" in duplicate.json()["detail"]
            current_paths = {
                Path(item["benchmark_path"])
                for item in server.STORE.list_datasets()
                if item.get("benchmark_path")
            }
            assert current_paths == existing_paths
            assert not any(path.name.startswith("second__") for path in tmp_path.rglob("*.jsonl"))

    asyncio.run(scenario())


def test_dataset_upload_resolves_scoring_dsn_from_env(tmp_path, monkeypatch):
    env_dsn = "postgresql://env_user:env_pass@db.local/env_only"
    monkeypatch.setenv("BENCH_ENV_ONLY_POSTGRES_DSN", env_dsn)
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/datasets/upload", json={
                "name": "Env only dataset",
                "file_name": "env_only.jsonl",
                "content": BENCH_JSONL,
                "db_id": "env_only",
                "db_type": "postgres",
            })
            assert resp.status_code == 200
            saved = resp.json()
            internal = server.STORE.get_dataset(saved["id"])
            assert internal["dsn"] == env_dsn
            assert internal["meta"]["dsn_source_env"] == "BENCH_ENV_ONLY_POSTGRES_DSN"
            assert Path(internal["benchmark_path"]).suffix == ".jsonl"

    asyncio.run(scenario())


def test_dataset_upload_requires_only_name_and_file_then_adds_dataset(tmp_path, monkeypatch):
    env_dsn = "postgresql://sports_user:sports_pass@db.local/sports_upload"
    monkeypatch.setenv("BENCH_SPORTS_UPLOAD_POSTGRES_DSN", env_dsn)
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/datasets/upload", json={
                "name": "Sports Upload",
                "file_name": "questions.jsonl",
                "content": BENCH_JSONL,
            })
            assert resp.status_code == 200
            saved = resp.json()
            internal = server.STORE.get_dataset(saved["id"])
            assert saved["name"] == "Sports Upload"
            assert saved["cases_count"] == 3
            assert internal["db_id"] == "sports_upload"
            assert internal["dsn"] == env_dsn
            assert internal["db_type"] == "postgres"
            assert internal["meta"]["dsn_source_env"] == "BENCH_SPORTS_UPLOAD_POSTGRES_DSN"
            assert Path(internal["benchmark_path"]).suffix == ".jsonl"
            assert parse_benchmark_file(internal["benchmark_path"])[0].case_id == "case_one"

    asyncio.run(scenario())


def test_dataset_upload_ignores_payload_dsn_and_infers_db_type_from_env_dsn(tmp_path, monkeypatch):
    env_dsn = "impala://env_user:env_pass@impala.local:21050/dm_mis"
    monkeypatch.setenv("BENCH_DM_MIS_IMPALA_DSN", env_dsn)
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/datasets/upload", json={
                "name": "DM MIS auto env",
                "file_name": "dm_mis.jsonl",
                "content": BENCH_JSONL,
                "db_id": "dm_mis",
                "dsn": "postgresql://manual_user:manual_pass@wrong.local/db",
                "db_type": "auto",
            })
            assert resp.status_code == 200
            saved = resp.json()
            internal = server.STORE.get_dataset(saved["id"])
            assert internal["dsn"] == env_dsn
            assert internal["db_type"] == "impala"
            assert internal["meta"]["dsn_source_env"] == "BENCH_DM_MIS_IMPALA_DSN"

    asyncio.run(scenario())


def test_dataset_upload_uses_global_scoring_dsn_for_unknown_db(tmp_path, monkeypatch):
    env_dsn = "postgresql://global_user:global_pass@db.local/scoring"
    for name in [
        "BENCH_TRAINING_E2E_TEST_SPORTS_EVENTS_IMPALA_DSN",
        "TRAINING_E2E_TEST_SPORTS_EVENTS_IMPALA_DSN",
        "BENCH_TRAINING_E2E_TEST_SPORTS_EVENTS_DSN",
        "TRAINING_E2E_TEST_SPORTS_EVENTS_DSN",
        "BENCH_IMPALA_DSN",
        "SCORING_DSN",
    ]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BENCH_SCORING_DSN", env_dsn)
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/datasets/upload", json={
                "name": "Training e2e test sports events",
                "file_name": "training_e2e_test_sports_events.jsonl",
                "content": BENCH_JSONL,
                "db_id": "training_e2e_test_sports_events",
                "db_type": "impala",
            })
            assert resp.status_code == 200
            saved = resp.json()
            internal = server.STORE.get_dataset(saved["id"])
            assert internal["dsn"] == env_dsn
            assert internal["meta"]["dsn_source_env"] == "BENCH_SCORING_DSN"

    asyncio.run(scenario())


def test_dataset_upload_requires_env_scoring_dsn(tmp_path, monkeypatch):
    for name in [
        "BENCH_MISSING_ENV_DSN_POSTGRES_DSN",
        "MISSING_ENV_DSN_POSTGRES_DSN",
        "BENCH_MISSING_ENV_DSN_DSN",
        "MISSING_ENV_DSN_DSN",
        "BENCH_POSTGRES_DSN",
        "BENCH_SCORING_DSN",
        "SCORING_DSN",
    ]:
        monkeypatch.delenv(name, raising=False)
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/datasets/upload", json={
                "name": "Missing env dataset",
                "file_name": "missing_env.jsonl",
                "content": BENCH_JSONL,
                "db_id": "missing_env_dsn",
                "db_type": "postgres",
            })
            assert resp.status_code == 400
            assert "DSN scoring-базы" in resp.json()["detail"]
            assert "BENCH_MISSING_ENV_DSN_POSTGRES_DSN" in resp.json()["detail"]
            assert "BENCH_SCORING_DSN" in resp.json()["detail"]

    asyncio.run(scenario())


def test_dataset_upload_rejects_when_env_missing_even_if_existing_db_dsn_exists(tmp_path, monkeypatch):
    for name in [
        "BENCH_MOCK_POSTGRES_DSN",
        "MOCK_POSTGRES_DSN",
        "BENCH_MOCK_DSN",
        "MOCK_DSN",
        "BENCH_POSTGRES_DSN",
        "BENCH_SCORING_DSN",
        "SCORING_DSN",
    ]:
        monkeypatch.delenv(name, raising=False)
    server = load_test_server(monkeypatch, tmp_path)
    monkeypatch.delenv("BENCH_MOCK_POSTGRES_DSN", raising=False)
    existing = server.STORE.save_dataset(dataset_payload(
        tmp_path,
        name="Existing mock dataset",
        dsn="postgresql://reuse_user:reuse_pass@db.local/mock",
        db_id="mock",
        db_type="postgres",
    ))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/datasets/upload", json={
                "name": "Reuse DSN dataset",
                "file_name": "reuse.jsonl",
                "content": BENCH_JSONL,
                "db_id": "mock",
                "db_type": "postgres",
            })
            assert resp.status_code == 400
            assert "DSN scoring-базы" in resp.json()["detail"]
            assert "BENCH_MOCK_POSTGRES_DSN" in resp.json()["detail"]
            assert server.STORE.get_dataset(existing["id"])["dsn"] == "postgresql://reuse_user:reuse_pass@db.local/mock"

    asyncio.run(scenario())


def test_dataset_upload_rejects_when_env_missing_even_if_alias_existing_db_dsn_exists(tmp_path, monkeypatch):
    for name in [
        "BENCH_TRAINING_E2E_TEST_SPORTS_EVENTS_POSTGRES_DSN",
        "TRAINING_E2E_TEST_SPORTS_EVENTS_POSTGRES_DSN",
        "BENCH_TRAINING_E2E_TEST_SPORTS_EVENTS_DSN",
        "TRAINING_E2E_TEST_SPORTS_EVENTS_DSN",
        "BENCH_SPORTS_EVENTS_LARGE_POSTGRES_DSN",
        "SPORTS_EVENTS_LARGE_POSTGRES_DSN",
        "BENCH_SPORTS_EVENTS_LARGE_DSN",
        "SPORTS_EVENTS_LARGE_DSN",
        "BENCH_POSTGRES_DSN",
        "BENCH_SCORING_DSN",
        "SCORING_DSN",
    ]:
        monkeypatch.delenv(name, raising=False)
    server = load_test_server(monkeypatch, tmp_path)
    existing = server.STORE.save_dataset(dataset_payload(
        tmp_path,
        name="Existing sports dataset",
        dsn="postgresql://sports_user:sports_pass@db.local/sports",
        db_id="sports_events_large",
        db_type="postgres",
    ))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/datasets/upload", json={
                "name": "Training e2e test sports events",
                "file_name": "training_e2e_test_sports_events.jsonl",
                "content": BENCH_JSONL,
                "db_id": "training_e2e_test_sports_events",
                "db_type": "postgres",
            })
            assert resp.status_code == 400
            assert "DSN scoring-базы" in resp.json()["detail"]
            assert "BENCH_TRAINING_E2E_TEST_SPORTS_EVENTS_POSTGRES_DSN" in resp.json()["detail"]
            assert server.STORE.get_dataset(existing["id"])["dsn"] == "postgresql://sports_user:sports_pass@db.local/sports"

    asyncio.run(scenario())


def test_connector_curl_endpoint_returns_redacted_command(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/connectors/curl", json={
                "connector": connector_payload(
                    headers={"Authorization": "Bearer secret-token", "Content-Type": "application/json"},
                    body_template='{"question":"{{question}}"}',
                ),
                "question": "count rows",
                "dialect": "postgres",
                "database": "mock",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["curl"].startswith("curl -X POST")
            assert "secret-token" not in data["curl"]
            assert "<redacted>" in data["curl"]
            assert "count rows" in data["curl"]

    asyncio.run(scenario())


def test_connector_chat_endpoint_uses_saved_connector(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    seen = {}

    class FakeConnector:
        def __init__(self, connector):
            seen["connector"] = connector

        async def generate(self, _client, question, dialect, timeout, database):
            seen.update({"question": question, "dialect": dialect, "timeout": timeout, "database": database})
            return "SELECT 42", {"answer": "SQL: SELECT 42"}, None

    monkeypatch.setattr(server, "TemplatedConnector", FakeConnector)
    server.STORE.save_connector(connector_payload(name="Chat connector", timeout=77, db_id="mock"))

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/connectors/chat", json={
                "connector_id": "conn",
                "question": "Сколько строк?",
                "dialect": "impala",
                "database": "dm_mis",
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["connector_name"] == "Chat connector"
            assert data["sql"] == "SELECT 42"
            assert data["error"] is None
            assert seen["question"] == "Сколько строк?"
            assert seen["dialect"] == "impala"
            assert seen["database"] == "dm_mis"
            assert seen["timeout"] == 77

            missing = await client.post("/api/connectors/chat", json={
                "connector_id": "missing",
                "question": "q",
            })
            assert missing.status_code == 404

    asyncio.run(scenario())


def test_connector_chat_executes_returned_sql_when_dataset_selected(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    server.STORE.save_connector(connector_payload(name="Chat connector", timeout=77, db_id="mock"))
    ds = server.STORE.save_dataset(dataset_payload(tmp_path, dsn="postgresql://mock-db"))
    seen = {}

    class FakeConnector:
        def __init__(self, _connector):
            pass

        async def generate(self, _client, _question, _dialect, _timeout, _database):
            return "SELECT 42", {"sql": "SELECT 42"}, None

    class FakePgExecutor:
        def __init__(self, dsn, statement_timeout_ms=30000):
            seen["dsn"] = dsn
            seen["timeout"] = statement_timeout_ms

        def execute_select(self, sql):
            seen["sql"] = sql
            return SelectResult(ok=True, rows=[(42,)], columns=["answer"], row_count=1)

    monkeypatch.setattr(server, "TemplatedConnector", FakeConnector)
    monkeypatch.setattr(server, "PgExecutor", FakePgExecutor)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/connectors/chat", json={
                "connector_id": "conn",
                "question": "answer?",
                "dataset_id": ds["id"],
            })
            assert resp.status_code == 200
            data = resp.json()
            assert data["sql"] == "SELECT 42"
            assert data["sql_result"]["ok"] is True
            assert data["sql_result"]["columns"] == ["answer"]
            assert data["sql_result"]["rows"] == [["42"]]
            assert data["sql_result"]["dataset_id"] == ds["id"]
            assert seen == {"dsn": "postgresql://mock-db", "timeout": 30000, "sql": "SELECT 42"}

    asyncio.run(scenario())


def test_sql_execute_endpoint_runs_readonly_and_rejects_writes(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    ds = server.STORE.save_dataset(dataset_payload(tmp_path, dsn="postgresql://mock-db"))
    seen = {}

    class FakePgExecutor:
        def __init__(self, dsn, statement_timeout_ms=30000):
            seen["dsn"] = dsn
            seen["timeout"] = statement_timeout_ms

        def execute_select(self, sql):
            seen["sql"] = sql
            return SelectResult(ok=True, rows=[("ok",)], columns=["status"], row_count=1)

    monkeypatch.setattr(server, "PgExecutor", FakePgExecutor)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ok = await client.post("/api/sql/execute", json={
                "dataset_id": ds["id"],
                "sql": "WITH x AS (SELECT 1) SELECT * FROM x",
                "timeout_ms": 1234,
            })
            assert ok.status_code == 200
            data = ok.json()
            assert data["result"]["ok"] is True
            assert data["result"]["rows"] == [["ok"]]
            assert seen["timeout"] == 1234

            bad = await client.post("/api/sql/execute", json={
                "dataset_id": ds["id"],
                "sql": "DROP TABLE secret",
            })
            assert bad.status_code == 400
            assert "read-only" in bad.json()["detail"]

    asyncio.run(scenario())


def test_stop_endpoint_marks_orphan_running_run_stopped(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    bench_path = write_benchmark(tmp_path)
    ds = server.STORE.save_dataset({
        "id": "ds",
        "name": "Mock dataset",
        "benchmark_path": bench_path,
        "db_id": "mock",
        "dsn": "postgresql://mock",
        "db_type": "postgres",
    })
    conn = server.STORE.save_connector({
        "id": "conn",
        "name": "Mock connector",
        "url": "http://mock.local/sql",
        "body_template": "{}",
        "sql_extract": {"mode": "json", "field": "sql"},
        "default_dialect": "postgres",
    })
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"])
    server.STORE.update_run(run["id"], status="running", total_cases=3)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            stopped = (await client.post(f"/api/runs/{run['id']}/stop")).json()
            assert stopped["status"] == "stopped"
            assert stopped["cancelled_tasks"] == 0
            got = (await client.get(f"/api/runs/{run['id']}")).json()
            assert got["status"] == "stopped"
            assert got["error"] == "остановлено пользователем"

    asyncio.run(scenario())


def test_stop_endpoint_does_not_overwrite_done_run(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    bench_path = write_benchmark(tmp_path)
    ds = server.STORE.save_dataset({
        "id": "ds",
        "name": "Mock dataset",
        "benchmark_path": bench_path,
        "db_id": "mock",
        "dsn": "postgresql://mock",
        "db_type": "postgres",
    })
    conn = server.STORE.save_connector({
        "id": "conn",
        "name": "Mock connector",
        "url": "http://mock.local/sql",
        "body_template": "{}",
        "sql_extract": {"mode": "json", "field": "sql"},
        "default_dialect": "postgres",
    })
    run = server.STORE.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                                  connector_id=conn["id"], connector_name=conn["name"])
    server.STORE.update_run(run["id"], status="done", total_cases=3, done_cases=3,
                            summary={"total": 3, "done": 3, "passed": 3, "accuracy": 100.0})

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            stopped = (await client.post(f"/api/runs/{run['id']}/stop")).json()
            assert stopped["status"] == "done"
            assert stopped["cancelled_tasks"] == 0
            got = (await client.get(f"/api/runs/{run['id']}")).json()
            assert got["status"] == "done"
            assert got.get("error") is None

    asyncio.run(scenario())


def test_delete_run_cancels_active_task(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_run_task(store, run_id, *_args, **_kwargs):
        store.update_run(run_id, status="running", total_cases=3)
        server.bus.publish({"type": "run", "run": store.get_run(run_id)})
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(server, "run_task", fake_run_task)

    async def scenario():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/connectors", json={
                "id": "conn",
                "name": "Mock connector",
                "url": "http://mock.local/sql",
                "body_template": "{}",
                "sql_extract": {"mode": "json", "field": "sql"},
                "default_dialect": "postgres",
            })
            await client.post("/api/datasets", json={
                "id": "ds",
                "name": "Mock dataset",
                "benchmark_path": write_benchmark(tmp_path),
                "db_id": "mock",
                "dsn": "postgresql://mock",
                "db_type": "postgres",
            })
            run = (await client.post("/api/runs", json={
                "dataset_id": "ds",
                "connector_id": "conn",
                "case_timeout": 60,
            })).json()
            await asyncio.wait_for(started.wait(), timeout=1)
            assert (await client.delete(f"/api/runs/{run['id']}")).json() == {"ok": True}
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            assert server.STORE.get_run(run["id"]) is None
            assert server._RUN_TASKS.get(run["id"]) in (None, set())

    asyncio.run(scenario())


def test_run_task_cancel_during_connector_call_marks_stopped(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate(self, *_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    install_runner_fakes(monkeypatch, runner_mod, SlowConnector)
    store, ds, conn, run = make_store_with_run(tmp_path)

    async def scenario():
        task = asyncio.create_task(runner_mod.run_task(store, run["id"], ds, conn,
                                                       case_timeout=60, judge_cfg=None))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(cancelled.wait(), timeout=1)
        got = store.get_run(run["id"])
        assert got["status"] == "stopped"
        assert got["done_cases"] == 0
        assert got["error"] == "остановлено пользователем"

    asyncio.run(scenario())


def test_run_task_persists_api_waiting_case_before_slow_connector_returns(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    started = asyncio.Event()
    release = asyncio.Event()

    class SlowConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate(self, *_args, **_kwargs):
            started.set()
            await release.wait()
            return "SELECT 1", {"sql": "SELECT 1"}, None

    install_runner_fakes(monkeypatch, runner_mod, SlowConnector)
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE", "0")
    store, ds, conn, run = make_store_with_run(tmp_path)

    async def scenario():
        task = asyncio.create_task(runner_mod.run_task(store, run["id"], ds, conn,
                                                       case_timeout=60, judge_cfg=None))
        await asyncio.wait_for(started.wait(), timeout=1)

        got = store.get_run(run["id"])
        cases = store.list_cases(run["id"])
        assert got["status"] == "running"
        assert got["done_cases"] == 0
        assert len(cases) == 1
        assert cases[0]["case_id"] == "case_one"
        assert cases[0]["case_status"] == "api_waiting"
        assert cases[0]["case_status_label"] == "ждем ответ API"
        assert cases[0]["predicted_sql"] is None
        assert runner_mod.build_answers(store, run["id"], ds, conn)["cases"] == []

        release.set()
        await asyncio.wait_for(task, timeout=2)
        done = store.get_run(run["id"])
        final_cases = store.list_cases(run["id"])
        assert done["status"] == "done"
        assert done["done_cases"] == 3
        assert final_cases[0]["case_status"] == "done"
        assert final_cases[0]["predicted_sql"] == "SELECT 1"

    asyncio.run(scenario())


def test_run_task_db_stop_during_connector_call_does_not_revive_run(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    started = asyncio.Event()
    release = asyncio.Event()

    class SlowConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate(self, *_args, **_kwargs):
            started.set()
            await release.wait()
            return "SELECT 1", {"sql": "SELECT 1"}, None

    install_runner_fakes(monkeypatch, runner_mod, SlowConnector)
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE", "0")
    store, ds, conn, run = make_store_with_run(tmp_path)

    async def scenario():
        task = asyncio.create_task(runner_mod.run_task(store, run["id"], ds, conn,
                                                       case_timeout=60, judge_cfg=None))
        await asyncio.wait_for(started.wait(), timeout=1)
        store.update_run(run["id"], status="stopped", error="остановлено пользователем")
        release.set()
        await asyncio.wait_for(task, timeout=2)

        got = store.get_run(run["id"])
        cases = store.list_cases(run["id"])
        assert got["status"] == "stopped"
        assert got["error"] == "остановлено пользователем"
        assert got["done_cases"] == 0
        assert cases[0]["case_status"] == "api_waiting"
        assert cases[0]["predicted_sql"] is None

    asyncio.run(scenario())


def test_run_task_db_pause_during_connector_call_does_not_revive_running(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    started = asyncio.Event()
    release = asyncio.Event()

    class SlowConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate(self, *_args, **_kwargs):
            started.set()
            await release.wait()
            return "SELECT 1", {"sql": "SELECT 1"}, None

    install_runner_fakes(monkeypatch, runner_mod, SlowConnector)
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE", "0")
    store, ds, conn, run = make_store_with_run(tmp_path)

    async def scenario():
        task = asyncio.create_task(runner_mod.run_task(store, run["id"], ds, conn,
                                                       case_timeout=60, judge_cfg=None))
        await asyncio.wait_for(started.wait(), timeout=1)
        store.update_run(run["id"], status="paused")
        release.set()
        await wait_until(lambda: (store.get_run(run["id"]) or {}).get("done_cases") == 1, timeout=1)

        got = store.get_run(run["id"])
        assert got["status"] == "paused"
        assert got["done_cases"] == 1

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_run_task_cancel_during_llm_judge_marks_stopped(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    judge_started = asyncio.Event()
    judge_cancelled = asyncio.Event()

    class FastConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate(self, *_args, **_kwargs):
            return "SELECT 1", {"sql": "SELECT 1"}, None

    async def slow_judge(*_args, **_kwargs):
        judge_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            judge_cancelled.set()
            raise

    install_runner_fakes(monkeypatch, runner_mod, FastConnector)
    monkeypatch.setattr(runner_mod, "judge_answers", slow_judge)
    store, ds, conn, run = make_store_with_run(tmp_path)

    async def scenario():
        task = asyncio.create_task(runner_mod.run_task(
            store, run["id"], ds, conn, case_timeout=60,
            judge_cfg={"base_url": "http://judge.local/v1", "api_key": "test", "model": "judge"},
            judge_concurrency=1,
        ))
        await asyncio.wait_for(judge_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(judge_cancelled.wait(), timeout=1)
        got = store.get_run(run["id"])
        assert got["status"] == "stopped"
        assert got["done_cases"] == 1
        assert got["summary"]["done"] == 1

    asyncio.run(scenario())


def test_rerun_cancel_during_connector_call_marks_stopped(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    started = asyncio.Event()

    class SlowConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate(self, *_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

    install_runner_fakes(monkeypatch, runner_mod, SlowConnector)
    store, _ds, _conn, run = make_store_with_run(tmp_path, status="queued", done_cases=0)

    async def scenario():
        task = asyncio.create_task(runner_mod.rerun(store, run["id"]))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        got = store.get_run(run["id"])
        assert got["status"] == "stopped"
        assert got["error"] == "остановлено пользователем"

    asyncio.run(scenario())


def test_rerun_api_case_cancel_marks_stopped(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    started = asyncio.Event()

    class SlowConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate(self, *_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

    install_runner_fakes(monkeypatch, runner_mod, SlowConnector)
    store, _ds, _conn, run = make_store_with_run(tmp_path, status="done", done_cases=0)

    async def scenario():
        task = asyncio.create_task(runner_mod.rerun_api_case(store, run["id"], "case_one"))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.get_run(run["id"])["status"] == "stopped"

    asyncio.run(scenario())


def test_judge_existing_case_cancel_marks_stopped(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    started = asyncio.Event()
    store, _ds, _conn, run = make_store_with_run(tmp_path, status="done", done_cases=1)
    store.add_case(run["id"], 1, {"case_id": "case_one", "difficulty": "Simple",
                                  "question": "one?", "gold_sql": "SELECT 1",
                                  "predicted_sql": "SELECT 1", "level": None,
                                  "matched": False})

    async def slow_judge(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(runner_mod, "judge_answers", slow_judge)
    monkeypatch.setattr(runner_mod, "_dump_json", lambda *_args, **_kwargs: None)

    async def scenario():
        task = asyncio.create_task(runner_mod.judge_existing_case(
            store, run["id"], "case_one",
            {"base_url": "http://judge.local/v1", "api_key": "test", "model": "judge"},
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert store.get_run(run["id"])["status"] == "stopped"

    asyncio.run(scenario())


def test_large_run_api_and_llm_concurrency_stress(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    bench_path = write_large_benchmark(tmp_path, count=40)
    store = SQLiteStore(str(tmp_path / "stress.db")).init()
    ds = store.save_dataset({
        "id": "ds",
        "name": "Large dataset",
        "benchmark_path": bench_path,
        "db_id": "mock",
        "dsn": "postgresql://mock",
        "db_type": "postgres",
    })
    conn = store.save_connector({
        "id": "conn",
        "name": "Stress connector",
        "url": "http://mock.local/sql",
        "body_template": "{}",
        "sql_extract": {"mode": "json", "field": "sql"},
        "default_dialect": "postgres",
        "timeout": 60,
    })
    run = store.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                           connector_id=conn["id"], connector_name=conn["name"],
                           config={"case_timeout": 60})

    api_active = 0
    api_max_active = 0
    judge_active = 0
    judge_max_active = 0
    api_lock = asyncio.Lock()
    judge_lock = asyncio.Lock()
    statuses: list[str] = []

    class StressConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate(self, *_args, **_kwargs):
            nonlocal api_active, api_max_active
            async with api_lock:
                api_active += 1
                api_max_active = max(api_max_active, api_active)
            try:
                await asyncio.sleep(0.005)
                return "SELECT 1", {"sql": "SELECT 1"}, None
            finally:
                async with api_lock:
                    api_active -= 1

    async def fake_judge_answers(answers_doc, *_args, **_kwargs):
        nonlocal judge_active, judge_max_active
        async with judge_lock:
            judge_active += 1
            judge_max_active = max(judge_max_active, judge_active)
        try:
            await asyncio.sleep(0.01)
            return {
                "cases": [
                    {
                        "case_id": case["case_id"],
                        "level": 4,
                        "reason": "ok",
                        "assessment": {"attempts": 1, "error_category": "correct", "confidence": 1.0},
                    }
                    for case in answers_doc.get("cases") or []
                ],
                "judge_summary": {"invalid": 0},
            }
        finally:
            async with judge_lock:
                judge_active -= 1

    async def fake_execute_scoring_select(*_args, **_kwargs):
        return SelectResult(ok=True, rows=[("1",)], columns=["c"], row_count=1)

    original_publish = runner_mod.bus.publish

    def capture_publish(msg):
        if msg.get("type") == "case":
            status = (msg.get("case") or {}).get("case_status")
            if status:
                statuses.append(status)
        original_publish(msg)

    install_runner_fakes(monkeypatch, runner_mod, StressConnector)
    monkeypatch.setattr(runner_mod, "judge_answers", fake_judge_answers)
    monkeypatch.setattr(runner_mod, "execute_scoring_select", fake_execute_scoring_select)
    monkeypatch.setattr(runner_mod.bus, "publish", capture_publish)
    monkeypatch.setattr(runner_mod, "append_run_log", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE", "0")
    monkeypatch.setenv("BENCH_APP_STDOUT_RUN_LOGS", "0")
    runner_mod._GLOBAL_LIMITERS.clear()

    async def scenario():
        await asyncio.wait_for(runner_mod.run_task(
            store, run["id"], ds, conn,
            concurrency=12,
            api_global_concurrency=3,
            case_timeout=60,
            judge_cfg={"base_url": "http://judge.local/v1", "api_key": "test", "model": "judge"},
            judge_concurrency=2,
        ), timeout=60)

    asyncio.run(scenario())

    got = store.get_run(run["id"])
    cases = store.list_cases(run["id"])
    assert got["status"] == "done"
    assert got["done_cases"] == 40
    assert got["summary"]["judged"] == 40
    assert got["summary"]["judge_errors"] == 0
    assert got["summary"]["llm_queued"] == 0
    assert got["summary"]["llm_in_progress"] == 0
    assert len(cases) == 40
    assert all(case["level"] == 4 for case in cases)
    assert api_max_active <= 3
    assert judge_max_active <= 2
    assert "llm_queued" in statuses
    assert "sent_to_judge" in statuses
    assert "judging" in statuses
    assert statuses.index("llm_queued") < statuses.index("sent_to_judge")


def test_parallel_large_runs_keep_global_api_and_llm_concurrency_at_one(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    bench_path = write_large_benchmark(tmp_path, count=50)
    store = SQLiteStore(str(tmp_path / "parallel_stress.db")).init()
    ds = store.save_dataset({
        "id": "ds",
        "name": "Parallel stress dataset",
        "benchmark_path": bench_path,
        "db_id": "mock",
        "dsn": "postgresql://mock",
        "db_type": "postgres",
    })
    conn = store.save_connector({
        "id": "conn",
        "name": "Parallel stress connector",
        "url": "http://mock.local/sql",
        "body_template": "{}",
        "sql_extract": {"mode": "json", "field": "sql"},
        "default_dialect": "postgres",
        "timeout": 60,
    })
    runs = [
        store.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                         connector_id=conn["id"], connector_name=f"{conn['name']} {idx}",
                         config={"case_timeout": 60})
        for idx in range(4)
    ]

    api_active = 0
    api_max_active = 0
    api_calls = 0
    judge_active = 0
    judge_max_active = 0
    judge_calls = 0
    api_lock = asyncio.Lock()
    judge_lock = asyncio.Lock()
    statuses_by_run: dict[str, set[str]] = {run["id"]: set() for run in runs}

    class ParallelStressConnector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def generate(self, _client, question, *_args, **_kwargs):
            nonlocal api_active, api_max_active, api_calls
            idx = int(question.rsplit(" ", 1)[-1].rstrip("?"))
            async with api_lock:
                api_active += 1
                api_calls += 1
                api_max_active = max(api_max_active, api_active)
            try:
                await asyncio.sleep(0.001 + (idx % 5) * 0.0005)
                return "SELECT 1", {"sql": "SELECT 1", "delay_bucket": idx % 5}, None
            finally:
                async with api_lock:
                    api_active -= 1

    async def fake_judge_answers(answers_doc, *_args, **_kwargs):
        nonlocal judge_active, judge_max_active, judge_calls
        case = (answers_doc.get("cases") or [{}])[0]
        idx = int(str(case.get("case_id", "case_1")).rsplit("_", 1)[-1])
        async with judge_lock:
            judge_active += 1
            judge_calls += 1
            judge_max_active = max(judge_max_active, judge_active)
        try:
            await asyncio.sleep(0.001 + (idx % 7) * 0.0004)
            return {
                "cases": [{
                    "case_id": case.get("case_id"),
                    "level": 4,
                    "reason": "ok",
                    "assessment": {"attempts": 1, "error_category": "correct", "confidence": 1.0},
                }],
                "judge_summary": {"invalid": 0},
            }
        finally:
            async with judge_lock:
                judge_active -= 1

    def capture_publish(msg):
        if msg.get("type") == "case":
            run_id = msg.get("run_id")
            status = (msg.get("case") or {}).get("case_status")
            if run_id in statuses_by_run and status:
                statuses_by_run[run_id].add(status)

    install_runner_fakes(monkeypatch, runner_mod, ParallelStressConnector)
    monkeypatch.setattr(runner_mod, "judge_answers", fake_judge_answers)
    monkeypatch.setattr(runner_mod.bus, "publish", capture_publish)
    monkeypatch.setattr(runner_mod, "append_run_log", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE", "0")
    monkeypatch.setenv("BENCH_APP_STDOUT_RUN_LOGS", "0")
    runner_mod._GLOBAL_LIMITERS.clear()

    async def scenario():
        await asyncio.wait_for(asyncio.gather(*[
            runner_mod.run_task(
                store, run["id"], ds, conn,
                concurrency=1,
                api_global_concurrency=1,
                case_timeout=60,
                judge_cfg={"base_url": "http://judge.local/v1", "api_key": "test", "model": "judge"},
                judge_concurrency=1,
            )
            for run in runs
        ]), timeout=180)

    asyncio.run(scenario())

    assert api_calls == 200
    assert judge_calls == 200
    assert api_max_active == 1
    assert judge_max_active == 1
    for run in runs:
        got = store.get_run(run["id"])
        cases = store.list_cases(run["id"])
        assert got["status"] == "done"
        assert got["done_cases"] == 50
        assert got["summary"]["judged"] == 50
        assert got["summary"]["judge_errors"] == 0
        assert got["summary"]["llm_queued"] == 0
        assert got["summary"]["llm_in_progress"] == 0
        assert len(cases) == 50
        assert all(case["level"] == 4 for case in cases)
        assert all(case["case_status"] == "judged" for case in cases)
        assert {"api_waiting", "llm_queued", "sent_to_judge", "judging", "judged"} <= statuses_by_run[run["id"]]


def test_many_mock_runs_start_stop_stress(tmp_path, monkeypatch):
    server = load_test_server(monkeypatch, tmp_path)

    async def scenario():
        release_done = asyncio.Event()

        async def fake_run_task(store, run_id, *_args, **_kwargs):
            store.update_run(run_id, status="running", total_cases=3)
            server.bus.publish({"type": "run", "run": store.get_run(run_id)})
            try:
                await release_done.wait()
                for idx in range(1, 4):
                    store.add_case(run_id, idx, {"case_id": f"case_{idx}", "difficulty": "Simple",
                                                 "question": "q", "gold_sql": "SELECT 1",
                                                 "predicted_sql": "SELECT 1", "level": 4,
                                                 "matched": True})
                store.update_run(run_id, status="done", done_cases=3,
                                 summary={"total": 3, "done": 3, "passed": 3,
                                          "accuracy": 100.0, "L0": 0, "L1": 0,
                                          "L2": 0, "L3": 0, "L4": 3})
                server.bus.publish({"type": "run", "run": store.get_run(run_id)})
            except asyncio.CancelledError:
                raise

        monkeypatch.setattr(server, "run_task", fake_run_task)
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post("/api/connectors", json={
                "id": "conn",
                "name": "Mock connector",
                "url": "http://mock.local/sql",
                "body_template": "{}",
                "sql_extract": {"mode": "json", "field": "sql"},
                "default_dialect": "postgres",
            })
            await client.post("/api/datasets", json={
                "id": "ds",
                "name": "Mock dataset",
                "benchmark_path": write_benchmark(tmp_path),
                "db_id": "mock",
                "dsn": "postgresql://mock",
                "db_type": "postgres",
            })
            created = await asyncio.gather(*[
                client.post("/api/runs", json={"dataset_id": "ds", "connector_id": "conn", "case_timeout": 60})
                for _ in range(30)
            ])
            run_ids = [r.json()["id"] for r in created]
            await asyncio.sleep(0.005)
            stopped_ids = set(run_ids[::2])
            await asyncio.gather(*[client.post(f"/api/runs/{rid}/stop") for rid in stopped_ids])
            release_done.set()
            await asyncio.sleep(0.05)
            runs = (await client.get("/api/runs")).json()
            by_id = {r["id"]: r for r in runs if r["id"] in run_ids}
            assert len(by_id) == len(run_ids)
            assert all(by_id[rid]["status"] == "stopped" for rid in stopped_ids)
            assert all(by_id[rid]["status"] in {"done", "stopped"} for rid in run_ids)
            health = (await client.get("/api/health")).json()
            assert health["ok"] is True
            assert "event_loop_lag_ms" in health
            live = (await client.get("/api/live")).json()
            assert live == {"ok": True}
            ready = await client.get("/api/ready")
            assert ready.status_code == 200
            assert ready.json()["ok"] is True
            snapshot = server._progress_case_snapshot(list(by_id.values()))
            assert len(snapshot) <= 500
            assert not server._RUN_TASKS

    asyncio.run(scenario())
