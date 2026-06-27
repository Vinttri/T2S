"""Offline unit tests for the bench app — no network, no Postgres.

Covers the pure building blocks: connector templating + SQL extraction, the
L0–L4 scoring rules, and a Store CRUD round-trip on a throwaway SQLite file.
"""
import asyncio
import re
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from leaderboard.comparator import eval_level
from leaderboard.db import PgExecutor, SelectResult
from leaderboard.redaction import redact_obj, redact_text, safe_exception
from leaderboard.benchmark import parse_benchmark_file, parse_benchmark_text
from bench_app.bus import _Bus
from bench_app.connectors import TemplatedConnector, _fill, extract_sql, preview_request, preview_to_curl, validate_plain_http_connector
from bench_app.datasets import save_uploaded_benchmark
from bench_app.defaults import migrate_dataset_paths_to_jsonl, seed_default_datasets
from bench_app.http_client import httpx_verify
from bench_app.judge import (
    _judge_level_case,
    _llm_headers,
    _normalise_level,
    _validate_level_judge_output,
    check_llm_connection,
    llm_config,
)
from bench_app.runner import (
    _GOLD_RESULT_CACHE,
    _GLOBAL_LIMITERS,
    CircuitBreakerOpen,
    RunCircuitBreaker,
    _ensure_scoring_dsn_allowed,
    execute_scoring_select,
    _get_gold_result,
    _prewarm_gold_cache,
    _result_to_dict,
    apply_judged_levels,
    build_answers,
    build_result,
    count_rerun_targets,
    needs_rerun,
    rerun,
)
from bench_app.run_logs import append_run_log, read_run_log, run_log_path
from bench_app.state_graph import (
    CASE_STATUS_LABELS,
    CASE_STATES,
    JOB_STATES,
    RUN_ACTIVE_STATES,
    RUN_FINISHED_STATES,
    RUN_STATES,
    STATE_GRAPHS,
    STATE_SETS,
    InvalidTransition,
    assert_transition_sequence,
    mermaid_graph,
)
from bench_app.sqlfmt import format_sql
from bench_app.store import SQLiteStore


ROOT_DIR = Path(__file__).resolve().parents[2]


# ---------------- connector templating ----------------
def test_dataset_upload_button_frontend_contract():
    subprocess.run(
        ["node", "bench_app/tests/upload_dataset_ui_test.mjs"],
        cwd=ROOT_DIR,
        check=True,
    )


def test_fill_plain_and_json_escape():
    vals = {"question": 'who said "hi"?\nline2', "dialect": "postgres"}
    # plain substitution (e.g. into a URL) keeps the raw value
    assert _fill("ask {{question}}", vals, json_escape=False).startswith("ask who said")
    # json-escaped substitution makes the value safe inside a JSON string literal
    body = _fill('{"q":"{{question}}","d":"{{dialect}}"}', vals, json_escape=True)
    import json
    parsed = json.loads(body)
    assert parsed["q"] == 'who said "hi"?\nline2'
    assert parsed["d"] == "postgres"


def test_database_is_not_a_placeholder():
    # database is baked as a literal at save time (server._bake_db), NOT substituted
    # at runtime — so {{database}} is left untouched by the templating engine.
    assert _fill('{"db":"{{database}}"}', {"database": "x"}, json_escape=True) == '{"db":"{{database}}"}'


def test_preview_request_renders_body_without_sending():
    c = {"method": "post", "url": "http://x/sql",
         "headers": {"X-Dialect": "{{dialect}}"},
         "body_template": '{"question":"{{question}}","database":"dm_mis"}'}
    p = preview_request(c, "count rows", "postgres", "mydb")
    assert p["method"] == "POST"
    assert p["url"] == "http://x/sql"
    assert p["headers"]["X-Dialect"] == "postgres"
    assert "count rows" in p["body"] and "dm_mis" in p["body"]


def test_preview_to_curl_quotes_rendered_request():
    p = {
        "method": "POST",
        "url": "http://x/sql?q=a b",
        "headers": {"Content-Type": "application/json", "X-Test": "a'b"},
        "body": '{"question":"count rows"}',
    }
    curl = preview_to_curl(p)

    assert curl.startswith("curl -X POST ")
    assert "'http://x/sql?q=a b'" in curl
    assert "-H 'Content-Type: application/json'" in curl
    assert "-H 'X-Test: a'\"'\"'b'" in curl
    assert "--data-raw '{\"question\":\"count rows\"}'" in curl


def test_connector_rejects_non_http_and_sse_contracts():
    try:
        validate_plain_http_connector({"method": "POST", "url": "ws://x/sql"})
        assert False, "ws:// connector should fail"
    except ValueError as exc:
        assert "HTTP" in str(exc)

    try:
        preview_request({"method": "POST", "url": "http://x/sql",
                         "headers": {"Accept": "text/event-stream"}}, "q", "postgres")
        assert False, "SSE connector should fail"
    except ValueError as exc:
        assert "SSE" in str(exc)


def test_connector_rejects_event_stream_response():
    async def run():
        async def handler(_request):
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  text="data: {\"sql\":\"SELECT 1\"}\n\n")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await TemplatedConnector({
                "method": "POST",
                "url": "http://model.local/sql",
                "body_template": "{}",
                "sql_extract": {"mode": "json", "field": "sql"},
            }).generate(client, "q", "postgres", 1)

    sql, payload, err = asyncio.run(run())
    assert sql is None
    assert payload["status"] == 200
    assert "SSE" in err


def test_run_log_jsonl_redacts_secret_fields(tmp_path):
    append_run_log("run-1", "case", logs_dir=tmp_path,
                   headers={"Authorization": "Bearer secret"},
                   error="ProgrammingError: invalid connection option user=login password=pwd "
                         "impala://login:pwd@host:31000/core_tmp")
    rows = read_run_log("run-1", logs_dir=tmp_path)
    assert Path(run_log_path("run-1", logs_dir=tmp_path)).exists()
    assert rows[0]["event"] == "case"
    assert rows[0]["time"]
    assert rows[0]["module"] == __name__
    assert rows[0]["headers"]["Authorization"] == "<redacted>"
    assert "login" not in rows[0]["error"]
    assert "pwd" not in rows[0]["error"]


def test_run_log_stdout_mirror_redacts_secret_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BENCH_APP_STDOUT_RUN_LOGS", "1")
    append_run_log("run-stdout", "case", logs_dir=tmp_path,
                   headers={"Authorization": "Bearer secret"},
                   error="ProgrammingError: password=pwd impala://login:pwd@host:31000/core_tmp")
    out = capsys.readouterr().out
    assert "bench_app.run_log " in out
    assert '"run_id": "run-stdout"' in out
    assert '"time": "' in out
    assert f'"module": "{__name__}"' in out
    assert '"Authorization": "<redacted>"' in out
    assert "login" not in out
    assert "pwd" not in out


def test_uvicorn_logs_include_time_and_logger():
    log_config = (ROOT_DIR / "bench_app/logging.ini").read_text(encoding="utf-8")
    host_runner = (ROOT_DIR / "scripts/run-benchmark-host.sh").read_text(encoding="utf-8")
    docker_entrypoint = (ROOT_DIR / "docker/bench_app-entrypoint.sh").read_text(encoding="utf-8")

    assert "bench_app.logging_utils.JsonFormatter" in log_config
    assert "formatter=json" in log_config
    assert "uvicorn.access" in log_config
    assert "--log-config" in host_runner
    assert "--log-config" in docker_entrypoint


def test_docker_compose_uses_split_frontend_backend():
    compose = (ROOT_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    nginx = (ROOT_DIR / "docker/nginx.conf").read_text(encoding="utf-8")
    dockerfile = (ROOT_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "backend:" in compose
    assert "worker:" in compose
    assert "frontend:" in compose
    assert "backup:" in compose
    assert 'PORT: "8090"' in compose
    assert 'BENCH_APP_RUNNER_MODE: "worker"' in compose
    assert "BENCH_WORKER_WATCHDOG_INTERVAL_S" in compose
    assert "BENCH_WORKER_MAX_JOB_ATTEMPTS" in compose
    assert "BENCH_BACKUP_INTERVAL_S" in compose
    assert "mem_limit:" in compose
    assert "cpus:" in compose
    assert 'restart: "no"' in compose
    assert "restart: unless-stopped" not in compose
    assert 'command: ["python", "-m", "bench_app.worker"]' in compose
    assert 'command: ["python", "-m", "bench_app.backup"]' in compose
    assert "disable: true" in compose
    assert "${BENCH_HOST_PORT:-8090}:8080" in compose
    assert "leaderboard-bench-frontend:latest" in compose
    assert "condition: service_healthy" not in compose
    assert "set $backend http://backend:8090" in nginx
    assert "proxy_pass $backend" in nginx
    assert "location /ws/" in nginx
    assert "/api/live" in dockerfile


def _js_const_array(source: str, name: str) -> set[str]:
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*\[(.*?)\];", source, re.S)
    assert match, f"{name} not found"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _js_object_keys(source: str, name: str) -> set[str]:
    match = re.search(rf"const\s+{re.escape(name)}\s*=\s*\{{(.*?)\}};", source, re.S)
    assert match, f"{name} not found"
    return set(re.findall(r"\b([A-Za-z0-9_]+)\s*:", match.group(1)))


def test_state_graph_definitions_are_complete_and_ui_is_in_sync():
    import bench_app.runner as runner_mod
    import bench_app.worker as worker_mod

    for kind, graph in STATE_GRAPHS.items():
        assert set(graph) == set(STATE_SETS[kind])
        assert all(target in STATE_SETS[kind] for targets in graph.values() for target in targets)
        assert mermaid_graph(kind).startswith("flowchart LR")

    assert set(CASE_STATUS_LABELS) == set(CASE_STATES)
    assert runner_mod.ACTIVE_RUN_STATUSES == set(RUN_ACTIVE_STATES)
    assert set(runner_mod.CASE_STATUS_LABELS) == set(CASE_STATES)
    assert worker_mod.ACTIVE_STATUSES == set(RUN_ACTIVE_STATES)
    assert worker_mod.FINISHED_STATUSES == set(RUN_FINISHED_STATES)

    app_js = (ROOT_DIR / "bench_app/static/app.js").read_text(encoding="utf-8")
    assert _js_const_array(app_js, "ACTIVE_ST") == set(RUN_ACTIVE_STATES)
    assert _js_object_keys(app_js, "STATUS_BADGE") == set(RUN_STATES)
    assert _js_object_keys(app_js, "CASE_STATUS_META") == set(CASE_STATES)


def test_state_graph_accepts_real_lifecycle_paths_and_rejects_bad_edges():
    assert_transition_sequence("run", [
        "queued", "running", "paused", "running", "judging", "running",
        "done", "queued", "running", "stopped", "queued", "running", "done",
    ])
    assert_transition_sequence("case", [
        "api_waiting", "api_error", "llm_queued", "sent_to_judge", "judging",
        "judged", "api_waiting", "no_sql", "llm_queued", "judged",
    ])
    assert_transition_sequence("case", ["api_waiting", "done", "api_waiting", "done"])
    assert_transition_sequence("job", ["queued", "running", "queued", "running", "done"])
    assert JOB_STATES == {"queued", "running", "done", "error", "cancelled"}

    with pytest.raises(InvalidTransition):
        assert_transition_sequence("run", ["stopped", "running"])
    with pytest.raises(InvalidTransition):
        assert_transition_sequence("case", ["judged", "done"])
    with pytest.raises(InvalidTransition):
        assert_transition_sequence("job", ["done", "running"])


def test_redaction_masks_dsn_user_password_and_tokens():
    dsn = "impala://login:pwd@impala-host:31000/core_tmp?auth_mechanism=LDAP&use_ssl=true&verify_cert=false"
    err = f"ProgrammingError: invalid connection option auth_mechanism in {dsn} user=login password=pwd Bearer abc.def"
    safe = redact_text(err)
    assert "login" not in safe
    assert "pwd" not in safe
    assert "abc.def" not in safe
    assert "impala-host:31000" in safe
    assert "<redacted>" in safe


def test_container_localhost_scoring_dsn_is_rejected(monkeypatch):
    import bench_app.server as server

    monkeypatch.setenv("BENCH_APP_CONTAINERIZED", "1")
    dataset = {
        "name": "legacy sports",
        "db_id": "sports_events_large",
        "db_type": "postgres",
        "dsn": "postgresql://bank:bankpass@localhost:15432/sports_events_large",
    }

    with pytest.raises(Exception) as excinfo:
        server._reject_container_localhost_dsn(dataset)

    exc = excinfo.value
    assert getattr(exc, "status_code", None) == 400
    assert "localhost" in str(getattr(exc, "detail", exc))
    assert "BENCH_SPORTS_EVENTS_LARGE_POSTGRES_DSN" in str(getattr(exc, "detail", exc))


def test_runner_rejects_container_localhost_scoring_dsn(monkeypatch):
    monkeypatch.setenv("BENCH_APP_CONTAINERIZED", "1")
    with pytest.raises(RuntimeError, match="localhost"):
        _ensure_scoring_dsn_allowed({
            "id": "legacy",
            "name": "legacy",
            "dsn": "postgresql://bank:bankpass@127.0.0.1:15432/sports_events_large",
        })


def test_safe_exception_masks_extra_secret_values():
    exc = RuntimeError("failed with token secret-value and password=secret-value")
    safe = safe_exception(exc, extra_secrets=["secret-value"])
    assert "secret-value" not in safe


def test_redact_obj_masks_login_like_keys():
    obj = redact_obj({"user": "login", "password": "pwd", "nested": {"api_key": "token"}, "passed": 7})
    assert obj["user"] == "<redacted>"
    assert obj["password"] == "<redacted>"
    assert obj["nested"]["api_key"] == "<redacted>"
    assert obj["passed"] == 7


def test_http_ssl_verification_defaults_off(monkeypatch):
    monkeypatch.delenv("BENCH_APP_SSL_VERIFY", raising=False)
    assert httpx_verify() is False
    monkeypatch.setenv("BENCH_APP_SSL_VERIFY", "1")
    assert httpx_verify() is True


def test_llm_headers_default_bearer():
    cfg = {"api_key": "secret-key", "auth_header": "Authorization", "auth_scheme": "Bearer"}
    assert _llm_headers(cfg)["Authorization"] == "Bearer secret-key"


def test_llm_headers_support_raw_custom_header():
    cfg = {"api_key": "secret-key", "auth_header": "X-API-Key", "auth_scheme": "none"}
    assert _llm_headers(cfg)["X-API-Key"] == "secret-key"


def test_llm_config_supports_no_auth(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.local/v1")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_MODEL", "llmgateway/free")
    monkeypatch.setenv("LLM_AUTH_HEADER", "none")
    cfg = llm_config()
    assert cfg["api_key"] == ""
    assert cfg["auth_header"] == "none"


def test_redaction_does_not_mask_working_directory_env(monkeypatch):
    monkeypatch.setenv("PWD", "/root/leaderboard_builder_codex")
    path = "/root/leaderboard_builder_codex/BENCHMARK_TRAIN.jsonl"
    assert redact_text(path) == path


def test_impala_dsn_params_parse_ssl_ldap_and_request_pool():
    dsn = (
        "impala://login:pwd@impala-ext:31000/core_tmp?"
        "auth_mechanism=LDAP&use_ssl=true&verify_cert=false&request_pool=root.core-dbt"
        "&graph=core_tmp&connect_timeout=10"
    )
    params, runtime = PgExecutor(dsn)._impala_params()
    assert params["host"] == "impala-ext"
    assert params["port"] == 31000
    assert params["database"] == "core_tmp"
    assert params["auth_mechanism"] == "LDAP"
    assert params["use_ssl"] is True
    assert params["verify_cert"] is False
    assert params["user"] == "login"
    assert params["password"] == "pwd"
    assert params["timeout"] == 10
    assert runtime["request_pool"] == "root.core-dbt"
    assert "request_pool" not in params
    assert "graph" not in params


def test_impala_session_settings_apply_request_pool_and_query_timeouts():
    class FakeCursor:
        def __init__(self):
            self.calls = []

        def execute(self, sql):
            self.calls.append(sql)

    cur = FakeCursor()
    dsn = "impala://login:pwd@impala-ext:31000/core_tmp?request_pool=root.core-dbt"
    PgExecutor(dsn, statement_timeout_ms=30000)._apply_session_settings(cur)
    assert "SET REQUEST_POOL='root.core-dbt'" in cur.calls
    assert "SET QUERY_TIMEOUT_S=30" in cur.calls
    assert "SET EXEC_TIME_LIMIT_S=30" in cur.calls


def test_uploaded_benchmark_is_saved_and_validated(tmp_path):
    src = Path(__file__).resolve().parents[2] / "BENCHMARK_TRAIN.jsonl"
    path, cases_count = save_uploaded_benchmark(src.read_text(encoding="utf-8"),
                                                "../unsafe name.jsonl", "Uploaded Train",
                                                directory=tmp_path)
    assert cases_count == 3
    assert Path(path).exists()
    assert Path(path).parent == tmp_path.resolve()
    assert Path(path).suffix == ".jsonl"


def test_uploaded_benchmark_rejects_markdown_file(tmp_path):
    with pytest.raises(ValueError, match="JSONL"):
        save_uploaded_benchmark("# old benchmark\n", "old.md", "Old", directory=tmp_path)


def test_benchmark_parser_rejects_markdown_dataset_text():
    with pytest.raises(ValueError, match="JSONL"):
        parse_benchmark_text("# old benchmark\n", source_name="old.md")


# ---------------- SQL extraction modes ----------------
def test_extract_json_field():
    spec = {"mode": "json", "field": "sql"}
    assert extract_sql({"sql": "SELECT 1"}, "", spec) == "SELECT 1"


def test_extract_json_dotted_path_and_list_index():
    spec = {"mode": "json", "field": "data.0.sql"}
    assert extract_sql({"data": [{"sql": "SELECT 2"}]}, "", spec) == "SELECT 2"


def test_extract_regex_group_one():
    spec = {"mode": "regex", "pattern": r"```sql\s*(.*?)```"}
    raw = "here:\n```sql\nSELECT 3;\n```\nbye"
    assert extract_sql(None, raw, spec) == "SELECT 3;"


def test_extract_deep_takes_last_match_in_chunk_list():
    # mimics modified-Vanna chat_poll: sql buried at chunks[].rich.data.metadata.sql,
    # variable index; deep search must find it and return the LAST executed one.
    payload = {"chunks": [
        {"rich": {"data": {"metadata": {"sql": "SELECT 1"}}}},
        {"rich": {"data": {"content": "thinking..."}}},
        {"rich": {"data": {"metadata": {"sql": "SELECT 2 FINAL"}}}},
    ]}
    spec = {"mode": "json", "field": "metadata.sql", "deep": True}
    assert extract_sql(payload, "", spec) == "SELECT 2 FINAL"


def test_extract_deep_none_when_absent():
    assert extract_sql({"chunks": [{"rich": {}}]}, "", {"mode": "json", "field": "metadata.sql", "deep": True}) is None


def test_extract_raw_strips():
    assert extract_sql(None, "  SELECT 4  ", {"mode": "raw"}) == "SELECT 4"


def test_extract_returns_none_when_missing():
    assert extract_sql({"foo": 1}, "", {"mode": "json", "field": "sql"}) is None


# ---------------- L0–L4 scoring ----------------
def _res(rows, ok=True, error=None):
    return SelectResult(ok=ok, rows=rows, columns=["c"], row_count=len(rows), error=error)


def test_level4_exact_match():
    g = _res([(1,)])
    level, _ = eval_level(predicted_sql="SELECT 1", predicted=_res([(1,)]),
                          gold_sql="SELECT 1", gold=g, ordered=False)
    assert level == 4


def test_level3_rows_differ():
    level, _ = eval_level(predicted_sql="SELECT 2", predicted=_res([(2,)]),
                          gold_sql="SELECT 1", gold=_res([(1,)]), ordered=False)
    assert level == 3


def test_level3_unordered_match_when_order_differs():
    # same multiset, different order, unordered compare -> match (L4)
    level, _ = eval_level(predicted_sql="q", predicted=_res([(2,), (1,)]),
                          gold_sql="q", gold=_res([(1,), (2,)]), ordered=False)
    assert level == 4
    # ordered compare on the same data -> rows differ (L3)
    level, _ = eval_level(predicted_sql="q", predicted=_res([(2,), (1,)]),
                          gold_sql="q", gold=_res([(1,), (2,)]), ordered=True)
    assert level == 3


def test_level2_gold_failed():
    level, _ = eval_level(predicted_sql="SELECT 1", predicted=_res([(1,)]),
                          gold_sql="bad", gold=_res([], ok=False, error="boom"), ordered=False)
    assert level == 2


def test_level1_predicted_not_executable():
    level, _ = eval_level(predicted_sql="SELECT bad", predicted=_res([], ok=False, error="syntax"),
                          gold_sql="SELECT 1", gold=_res([(1,)]), ordered=False)
    assert level == 1


def test_level0_no_sql():
    level, _ = eval_level(predicted_sql=None, predicted=None,
                          gold_sql="SELECT 1", gold=_res([(1,)]), ordered=False)
    assert level == 0


def test_result_to_dict_keeps_all_rows():
    res = SelectResult(ok=True, columns=["n"], rows=[(i,) for i in range(75)], row_count=75)
    out = _result_to_dict(res)
    assert out["row_count"] == 75
    assert len(out["rows"]) == 75
    assert out["rows"][-1] == ["74"]
    assert out["truncated"] is False


class _CountingGoldExecutor:
    def __init__(self):
        self.calls = []

    def execute_select(self, sql):
        self.calls.append(sql)
        return SelectResult(ok=True, columns=["sql"], rows=[(sql,)], row_count=1)


class _FailingGoldExecutor:
    def __init__(self):
        self.calls = []

    def execute_select(self, sql):
        self.calls.append(sql)
        return SelectResult(ok=False, columns=[], rows=[], row_count=0, error="connection refused")


def test_gold_result_cache_reuses_same_dsn_and_sql(tmp_path, monkeypatch):
    _GOLD_RESULT_CACHE.clear()
    monkeypatch.delenv("BENCH_APP_GOLD_CACHE", raising=False)
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE_DIR", str(tmp_path / "gold_cache"))
    executor = _CountingGoldExecutor()
    dataset = {"dsn": "postgresql://db/main", "db_id": "main", "db_type": "postgres"}

    first = asyncio.run(_get_gold_result(executor, dataset, "SELECT 1"))
    second = asyncio.run(_get_gold_result(executor, dataset, "SELECT 1"))

    assert executor.calls == ["SELECT 1"]
    assert first.rows == second.rows == [("SELECT 1",)]
    _GOLD_RESULT_CACHE.clear()


def test_gold_result_cache_does_not_persist_failed_results(tmp_path, monkeypatch):
    _GOLD_RESULT_CACHE.clear()
    monkeypatch.delenv("BENCH_APP_GOLD_CACHE", raising=False)
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE_DIR", str(tmp_path / "gold_cache"))
    executor = _FailingGoldExecutor()
    dataset = {"dsn": "postgresql://db/main", "db_id": "main", "db_type": "postgres"}

    first = asyncio.run(_get_gold_result(executor, dataset, "SELECT broken"))
    second = asyncio.run(_get_gold_result(executor, dataset, "SELECT broken"))

    assert first.ok is False
    assert second.ok is False
    assert executor.calls == ["SELECT broken", "SELECT broken"]
    assert not list((tmp_path / "gold_cache").glob("*.json"))
    _GOLD_RESULT_CACHE.clear()


def test_prewarm_gold_cache_deduplicates_and_skips_cached(tmp_path, monkeypatch):
    _GOLD_RESULT_CACHE.clear()
    monkeypatch.delenv("BENCH_APP_GOLD_CACHE", raising=False)
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE_DIR", str(tmp_path / "gold_cache"))
    executor = _CountingGoldExecutor()
    dataset = {"dsn": "postgresql://db/main", "db_id": "main", "db_type": "postgres"}
    cases = [
        SimpleNamespace(gold_sql="SELECT 1"),
        SimpleNamespace(gold_sql="SELECT 1"),
        SimpleNamespace(gold_sql="SELECT 2"),
    ]

    warmed = asyncio.run(_prewarm_gold_cache(executor, dataset, cases))
    warmed_again = asyncio.run(_prewarm_gold_cache(executor, dataset, cases))

    assert warmed == 2
    assert warmed_again == 0
    assert sorted(executor.calls) == ["SELECT 1", "SELECT 2"]
    _GOLD_RESULT_CACHE.clear()


def test_gold_result_cache_respects_memory_max_entries(tmp_path, monkeypatch):
    _GOLD_RESULT_CACHE.clear()
    monkeypatch.delenv("BENCH_APP_GOLD_CACHE", raising=False)
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE_DIR", str(tmp_path / "gold_cache"))
    monkeypatch.setenv("BENCH_APP_GOLD_CACHE_MEMORY_ENTRIES", "2")
    executor = _CountingGoldExecutor()
    dataset = {"dsn": "postgresql://db/main", "db_id": "main", "db_type": "postgres"}

    asyncio.run(_get_gold_result(executor, dataset, "SELECT 1"))
    asyncio.run(_get_gold_result(executor, dataset, "SELECT 2"))
    asyncio.run(_get_gold_result(executor, dataset, "SELECT 1"))
    asyncio.run(_get_gold_result(executor, dataset, "SELECT 3"))

    assert len(_GOLD_RESULT_CACHE) == 2
    assert executor.calls == ["SELECT 1", "SELECT 2", "SELECT 3"]
    assert asyncio.run(_get_gold_result(executor, dataset, "SELECT 2")).rows == [("SELECT 2",)]
    assert executor.calls == ["SELECT 1", "SELECT 2", "SELECT 3"]
    _GOLD_RESULT_CACHE.clear()


class _SlowImpalaExecutor:
    def __init__(self, delay=0.03):
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls = []
        self.lock = threading.Lock()

    def _scheme(self):
        return "impala"

    def execute_select(self, sql):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            self.calls.append(sql)
            return SelectResult(ok=True, columns=["sql"], rows=[(sql,)], row_count=1)
        finally:
            with self.lock:
                self.active -= 1


def test_impala_scoring_select_is_globally_serialized(monkeypatch):
    _GLOBAL_LIMITERS.clear()
    monkeypatch.setenv("BENCH_APP_MAX_IMPALA_CONCURRENCY", "1")
    executor = _SlowImpalaExecutor()
    dataset = {"dsn": "impala://db/main", "db_id": "main", "db_type": "impala"}

    async def scenario():
        return await asyncio.gather(*[
            execute_scoring_select(executor, dataset, f"SELECT {i}")
            for i in range(6)
        ])

    results = asyncio.run(scenario())

    assert [r.rows[0][0] for r in results] == [f"SELECT {i}" for i in range(6)]
    assert sorted(executor.calls) == [f"SELECT {i}" for i in range(6)]
    assert executor.max_active == 1
    _GLOBAL_LIMITERS.clear()


def test_impala_scoring_select_holds_limit_until_cancelled_thread_finishes(monkeypatch):
    _GLOBAL_LIMITERS.clear()
    monkeypatch.setenv("BENCH_APP_MAX_IMPALA_CONCURRENCY", "1")
    executor = _SlowImpalaExecutor(delay=0.12)
    dataset = {"dsn": "impala://db/main", "db_id": "main", "db_type": "impala"}

    async def wait_until_started():
        for _ in range(50):
            with executor.lock:
                if executor.active:
                    return
            await asyncio.sleep(0.005)
        raise AssertionError("first query did not start")

    async def scenario():
        first = asyncio.create_task(execute_scoring_select(executor, dataset, "SELECT first"))
        await wait_until_started()
        first.cancel()
        second = asyncio.create_task(execute_scoring_select(executor, dataset, "SELECT second"))
        await asyncio.sleep(0.04)
        assert executor.max_active == 1
        with pytest.raises(asyncio.CancelledError):
            await first
        second_result = await second
        return second_result

    result = asyncio.run(scenario())

    assert result.rows == [("SELECT second",)]
    assert sorted(executor.calls) == ["SELECT first", "SELECT second"]
    assert executor.max_active == 1
    _GLOBAL_LIMITERS.clear()


def test_format_sql_pretty_prints_without_touching_literals():
    sql = ("select a, count(*) as c from foo join bar on foo.id=bar.id "
           "where a = 'select from' and b > 1 order by c desc;")
    out = format_sql(sql)

    assert out.startswith("SELECT\n  a,\n  count(*) AS c\nFROM foo")
    assert "\nJOIN bar\n  ON foo.id=bar.id" in out
    assert "\nWHERE a = 'select from'\n  AND b > 1" in out
    assert out.endswith("ORDER BY c desc;")
    assert format_sql("(нет SQL)") == "(нет SQL)"
    assert format_sql(None) is None


# ---------------- Store CRUD round-trip ----------------
def test_store_roundtrip(tmp_path):
    store = SQLiteStore(str(tmp_path / "t.db")).init()
    c = store.save_connector({"name": "C", "url": "http://x/sql",
                              "body_template": '{"question":"{{question}}"}', "sql_extract": {}})
    assert c["id"] and store.get_connector(c["id"])["name"] == "C"

    d = store.save_dataset({"name": "D", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                            "db_id": "sports_events_large", "dsn": "postgresql://x"})
    assert d["id"] and any(x["id"] == d["id"] for x in store.list_datasets())

    run = store.create_run(dataset_id=d["id"], dataset_name="D",
                           connector_id=c["id"], connector_name="C")
    store.add_case(run["id"], 1, {"case_id": "t1", "level": 4, "matched": True,
                                  "question": "q", "gold_sql": "g", "predicted_sql": "p"})
    store.update_run(run["id"], status="done", summary={"accuracy": 100.0})
    got = store.get_run(run["id"])
    assert got["status"] == "done" and got["summary"]["accuracy"] == 100.0
    cases = store.list_cases(run["id"])
    assert len(cases) == 1 and cases[0]["case_id"] == "t1" and cases[0]["level"] == 4


def test_store_worker_job_queue_roundtrip(tmp_path):
    store = SQLiteStore(str(tmp_path / "jobs.db")).init()
    run = store.create_run(dataset_id="ds", dataset_name="D",
                           connector_id="conn", connector_name="C")
    job = store.enqueue_job(run["id"], "run", {"source": "test"})

    assert job["status"] == "queued"
    assert job["payload"]["source"] == "test"
    assert store.job_counts()["queued"] == 1

    claimed = store.claim_next_job("worker-1")
    assert claimed["id"] == job["id"]
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1

    store.heartbeat_job(job["id"], "worker-1")
    running = store.get_job(job["id"])
    assert running["heartbeat_at"]

    store.finish_job(job["id"], "worker-1", status="done")
    assert store.get_job(job["id"])["status"] == "done"


def test_store_watchdog_requeues_and_fails_stale_jobs(tmp_path):
    store = SQLiteStore(str(tmp_path / "jobs.db")).init()
    run1 = store.create_run(dataset_id="ds", dataset_name="D",
                            connector_id="conn", connector_name="C")
    run2 = store.create_run(dataset_id="ds", dataset_name="D",
                            connector_id="conn", connector_name="C")
    job1 = store.enqueue_job(run1["id"], "run", {})
    job2 = store.enqueue_job(run2["id"], "run", {})

    claimed1 = store.claim_next_job("worker-1")
    claimed2 = store.claim_next_job("worker-1")
    old = time.time() - 100
    store._exec("UPDATE run_jobs SET heartbeat_at=?, attempts=? WHERE id=?",
                (old, 1, claimed1["id"]))
    store._exec("UPDATE run_jobs SET heartbeat_at=?, attempts=? WHERE id=?",
                (old, 3, claimed2["id"]))

    recovered = store.recover_stale_jobs(stale_after_s=10, max_attempts=3)

    assert recovered == {"stale": 2, "requeued": 1, "failed": 1}
    assert store.get_job(job1["id"])["status"] == "queued"
    assert store.get_job(job2["id"])["status"] == "error"
    assert store.get_run(run2["id"])["status"] == "error"


def test_worker_processes_queued_run_job(tmp_path, monkeypatch):
    import bench_app.worker as worker_mod

    store = SQLiteStore(str(tmp_path / "worker.db")).init()
    connector = store.save_connector({
        "id": "conn",
        "name": "C",
        "url": "http://x/sql",
        "body_template": "{}",
        "sql_extract": {},
    })
    dataset = store.save_dataset({
        "id": "ds",
        "name": "D",
        "benchmark_path": "BENCHMARK_TRAIN.jsonl",
        "db_id": "mock",
        "dsn": "postgresql://mock",
        "db_type": "postgres",
    })
    run = store.create_run(dataset_id=dataset["id"], dataset_name=dataset["name"],
                           connector_id=connector["id"], connector_name=connector["name"],
                           config={"auto_judge": False})
    job = store.enqueue_job(run["id"], "run", {"source": "unit"})

    async def fake_run_task(store_arg, run_id, *_args, **_kwargs):
        store_arg.update_run(run_id, status="done", done_cases=0,
                             summary={"total": 0, "done": 0, "passed": 0, "accuracy": 0.0})

    monkeypatch.setattr(worker_mod, "run_task", fake_run_task)

    processed = asyncio.run(worker_mod.process_one_job(store, "worker-1"))

    assert processed is True
    assert store.get_job(job["id"])["status"] == "done"
    assert store.get_run(run["id"])["status"] == "done"


def test_backup_writes_sqlite_and_data_archives(tmp_path, monkeypatch):
    from bench_app.backup import run_backup_once

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "datasets").mkdir()
    (data_dir / "datasets" / "sample.jsonl").write_text('{"case_id":"c"}\n', encoding="utf-8")
    store = SQLiteStore(str(data_dir / "app.db")).init()
    store.create_run(dataset_id="ds", dataset_name="D", connector_id="c", connector_name="C")
    monkeypatch.setenv("BENCH_APP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("BENCH_STORE_URL", f"sqlite:///{data_dir / 'app.db'}")
    monkeypatch.setenv("BENCH_BACKUP_DIR", str(tmp_path / "backups"))

    result = run_backup_once()

    assert result["ok"] is True
    assert any(path.endswith(".db") for path in result["files"])
    assert any(path.endswith(".tar.gz") for path in result["files"])
    assert list((tmp_path / "backups" / "app-db").glob("*.db"))
    assert list((tmp_path / "backups" / "data").glob("*.tar.gz"))


def test_run_circuit_breaker_opens_after_consecutive_external_failures(monkeypatch):
    monkeypatch.setenv("BENCH_APP_CIRCUIT_BREAKER_FAILURES", "2")
    breaker = RunCircuitBreaker("run-1")

    async def scenario():
        await breaker.record("api", True, "ReadTimeout")
        try:
            await breaker.record("api", True, "ReadTimeout")
            assert False, "breaker should open"
        except CircuitBreakerOpen as exc:
            assert exc.kind == "api"
            assert exc.threshold == 2
        await breaker.record("db", True, "ConnectTimeout")
        await breaker.record("db", False)
        await breaker.record("db", True, "ConnectTimeout")

    asyncio.run(scenario())


def test_store_light_cases_skip_large_payload_and_get_case_loads_one(tmp_path):
    store = SQLiteStore(str(tmp_path / "light.db")).init()
    c = store.save_connector({"name": "C", "url": "http://x/sql",
                              "body_template": "{}", "sql_extract": {}})
    d = store.save_dataset({"name": "D", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                            "db_id": "sports_events_large", "dsn": "postgresql://x"})
    run = store.create_run(dataset_id=d["id"], dataset_name="D",
                           connector_id=c["id"], connector_name="C")
    store.add_case(run["id"], 1, {
        "case_id": "t1",
        "level": 4,
        "matched": True,
        "question": "q",
        "gold_sql": "SELECT 1",
        "predicted_sql": "SELECT 1",
        "raw_response": "x" * 10000,
        "gold_result": {"columns": ["c"], "rows": [["1"]], "row_count": 1},
        "agent_result": {"columns": ["c"], "rows": [["1"]], "row_count": 1},
        "assessment": {"raw_response": "judge" * 1000},
    })

    light = store.list_cases(run["id"], include_payload=False)[0]
    assert light["case_id"] == "t1"
    assert "raw_response" not in light
    assert "gold_result" not in light
    assert "agent_result" not in light
    assert "assessment" not in light

    full = store.get_case(run["id"], "t1")
    assert full["raw_response"].startswith("x")
    assert full["gold_result"]["rows"] == [["1"]]
    assert full["agent_result"]["rows"] == [["1"]]


def test_connector_rename_syncs_existing_runs_and_result_docs(tmp_path):
    store = SQLiteStore(str(tmp_path / "rename.db")).init()
    c = store.save_connector({"id": "conn", "name": "Old name", "url": "http://x/sql",
                              "body_template": "{}", "sql_extract": {}})
    d = store.save_dataset({"id": "ds", "name": "D", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                            "db_id": "sports_events_large", "dsn": "postgresql://x"})
    run = store.create_run(dataset_id=d["id"], dataset_name=d["name"],
                           connector_id=c["id"], connector_name=c["name"])
    store.add_case(run["id"], 1, {"case_id": "t1", "difficulty": "Simple",
                                  "question": "q", "gold_sql": "SELECT 1",
                                  "predicted_sql": "SELECT 1", "level": 4,
                                  "matched": True})

    store.save_connector({"id": "conn", "name": "Renamed connector", "url": "http://x/sql",
                          "body_template": "{}", "sql_extract": {}})

    assert store.get_run(run["id"])["connector_name"] == "Renamed connector"
    assert build_result(store, run["id"])["model"]["name"] == "Renamed connector"
    assert build_answers(store, run["id"])["model"]["name"] == "Renamed connector"


def test_dataset_rename_syncs_existing_runs_and_result_docs(tmp_path):
    store = SQLiteStore(str(tmp_path / "dataset_rename.db")).init()
    c = store.save_connector({"id": "conn", "name": "C", "url": "http://x/sql",
                              "body_template": "{}", "sql_extract": {}})
    d = store.save_dataset({"id": "ds", "name": "Old dataset", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                            "db_id": "sports_events_large", "dsn": "postgresql://x"})
    run = store.create_run(dataset_id=d["id"], dataset_name=d["name"],
                           connector_id=c["id"], connector_name=c["name"])
    store.add_case(run["id"], 1, {"case_id": "t1", "difficulty": "Simple",
                                  "question": "q", "gold_sql": "SELECT 1",
                                  "predicted_sql": "SELECT 1", "level": 4,
                                  "matched": True})

    store.save_dataset({"id": "ds", "name": "Renamed dataset", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                        "db_id": "sports_events_large", "dsn": "postgresql://x"})

    assert store.get_run(run["id"])["dataset_name"] == "Renamed dataset"
    assert build_result(store, run["id"])["benchmark"]["name"] == "Renamed dataset"
    assert build_answers(store, run["id"])["benchmark"]["name"] == "Renamed dataset"


def test_default_dm_mis_impala_datasets_seed(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_DM_MIS_IMPALA_DSN", "postgresql://scoring.local/dm_mis")
    monkeypatch.setenv("BENCH_APP_DATASETS_DIR", str(tmp_path / "datasets"))
    store = SQLiteStore(str(tmp_path / "defaults.db")).init()

    created = seed_default_datasets(store)
    by_id = {d["id"]: d for d in created}

    assert set(by_id) == {"dm_mis_impala_1", "dm_mis_impala_3", "dm_mis_impala_10", "dm_mis_impala_all"}
    assert {d["db_type"] for d in by_id.values()} == {"impala"}
    assert {d["db_id"] for d in by_id.values()} == {"dm_mis"}
    assert {d["dsn"] for d in by_id.values()} == {"postgresql://scoring.local/dm_mis"}
    assert {Path(d["benchmark_path"]).parent for d in by_id.values()} == {tmp_path / "datasets"}
    assert len(parse_benchmark_file(by_id["dm_mis_impala_1"]["benchmark_path"])) == 1
    assert len(parse_benchmark_file(by_id["dm_mis_impala_3"]["benchmark_path"])) == 3
    assert len(parse_benchmark_file(by_id["dm_mis_impala_10"]["benchmark_path"])) == 10
    all_cases = parse_benchmark_file(by_id["dm_mis_impala_all"]["benchmark_path"])
    assert len(all_cases) == 54
    assert {name: sum(1 for c in all_cases if c.difficulty == name) for name in ("Simple", "Medium", "Hard")} == {
        "Simple": 10,
        "Medium": 20,
        "Hard": 24,
    }
    assert seed_default_datasets(store) == []


def test_default_dm_mis_seed_without_env_does_not_use_localhost_fallback(tmp_path, monkeypatch):
    for name in ("BENCH_DM_MIS_IMPALA_DSN", "DM_MIS_IMPALA_DSN", "BENCH_DM_MIS_DSN", "DM_MIS_DSN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BENCH_APP_DATASETS_DIR", str(tmp_path / "datasets"))
    store = SQLiteStore(str(tmp_path / "defaults_no_env.db")).init()

    created = seed_default_datasets(store)

    assert {d["id"] for d in created} == {"dm_mis_impala_1", "dm_mis_impala_3", "dm_mis_impala_10", "dm_mis_impala_all"}
    assert {d["dsn"] for d in created} == {""}


def test_default_seed_preserves_user_edited_dataset_copy(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    source = Path(__file__).resolve().parents[2] / "BENCHMARK_dm_mis_impala_1.jsonl"
    (root / "BENCHMARK_dm_mis_impala_1.jsonl").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    datasets_dir = tmp_path / "data" / "datasets"
    datasets_dir.mkdir(parents=True)
    monkeypatch.setenv("BENCH_APP_DATASETS_DIR", str(datasets_dir))
    store = SQLiteStore(str(tmp_path / "seed_preserve.db")).init()
    created = seed_default_datasets(store, root=root)
    seeded = created[0]
    editable = datasets_dir / "dm_mis_impala_1__dm_mis_impala_1.jsonl"
    editable.write_text(
        '{"benchmark_id":"S1","case_id":"edited","difficulty":"Simple","question":"edited?","normal_phrasing":"","conditions":"","gold_sql":"SELECT 99;"}\n',
        encoding="utf-8",
    )
    store.save_dataset({
        **seeded,
        "benchmark_path": str(editable),
        "meta": {
            **(seeded.get("meta") or {}),
            "format": "jsonl",
            "seeded_default": False,
            "user_edited_dataset": True,
            "editable_copy_from": seeded["benchmark_path"],
        },
    })

    changed = seed_default_datasets(store, root=root)

    saved = store.get_dataset("dm_mis_impala_1")
    assert changed == []
    assert saved["benchmark_path"] == str(editable)
    assert parse_benchmark_file(saved["benchmark_path"])[0].case_id == "edited"


def test_default_seed_recovers_existing_editable_copy_after_old_reset(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    source = Path(__file__).resolve().parents[2] / "BENCHMARK_dm_mis_impala_1.jsonl"
    default_path = root / "BENCHMARK_dm_mis_impala_1.jsonl"
    default_path.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    datasets_dir = tmp_path / "data" / "datasets"
    datasets_dir.mkdir(parents=True)
    monkeypatch.setenv("BENCH_APP_DATASETS_DIR", str(datasets_dir))
    editable = datasets_dir / "dm_mis_impala_1__dm_mis_impala_1.jsonl"
    editable.write_text(
        '{"benchmark_id":"S1","case_id":"recovered","difficulty":"Simple","question":"recovered?","normal_phrasing":"","conditions":"","gold_sql":"SELECT 77;"}\n',
        encoding="utf-8",
    )
    store = SQLiteStore(str(tmp_path / "seed_recover.db")).init()
    store.save_dataset({
        "id": "dm_mis_impala_1",
        "name": "dm_mis impala (1 вопрос)",
        "benchmark_path": str(default_path),
        "db_id": "dm_mis",
        "dsn": "postgresql://x",
        "db_type": "impala",
        "meta": {
            "seeded_default": True,
            "source": "bench_app.defaults",
            "question_count": 1,
            "benchmark_file": "BENCHMARK_dm_mis_impala_1.jsonl",
        },
    })

    changed = seed_default_datasets(store, root=root)

    saved = store.get_dataset("dm_mis_impala_1")
    assert len(changed) == 1
    assert saved["benchmark_path"] == str(editable.resolve())
    assert saved["meta"]["user_edited_dataset"] is True
    assert saved["meta"]["seeded_default"] is False
    assert parse_benchmark_file(saved["benchmark_path"])[0].case_id == "recovered"


def test_migrate_dataset_paths_to_jsonl_updates_old_markdown_rows(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    datasets_dir = tmp_path / "runtime" / "datasets"
    monkeypatch.setenv("BENCH_APP_DATASETS_DIR", str(datasets_dir))
    jsonl = root / "BENCHMARK_TRAIN.jsonl"
    src = Path(__file__).resolve().parents[2] / "BENCHMARK_TRAIN.jsonl"
    jsonl.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    store = SQLiteStore(str(tmp_path / "migrate.db")).init()
    store.save_dataset({
        "id": "old",
        "name": "Old",
        "benchmark_path": "/old/root/BENCHMARK_TRAIN.md",
        "db_id": "sports_events_large",
        "dsn": "postgresql://x",
    })

    changed = migrate_dataset_paths_to_jsonl(store, root=root)

    assert len(changed) == 1
    saved = store.get_dataset("old")
    assert saved["benchmark_path"] == str((datasets_dir / "BENCHMARK_TRAIN.jsonl").resolve())
    assert Path(saved["benchmark_path"]).exists()
    assert saved["meta"]["format"] == "jsonl"
    assert saved["meta"]["migrated_from_markdown_path"] == "/old/root/BENCHMARK_TRAIN.md"


def test_migrate_dataset_paths_materializes_missing_absolute_jsonl_rows(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    datasets_dir = tmp_path / "runtime" / "datasets"
    monkeypatch.setenv("BENCH_APP_DATASETS_DIR", str(datasets_dir))
    source = Path(__file__).resolve().parents[2] / "BENCHMARK_TRAIN.jsonl"
    image_copy = root / "BENCHMARK_TRAIN.jsonl"
    image_copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    store = SQLiteStore(str(tmp_path / "migrate_abs.db")).init()
    old_path = "/root/leaderboard_builder_codex/BENCHMARK_TRAIN.jsonl"
    store.save_dataset({
        "id": "old-jsonl",
        "name": "Old JSONL",
        "benchmark_path": old_path,
        "db_id": "sports_events_large",
        "dsn": "postgresql://x",
    })

    changed = migrate_dataset_paths_to_jsonl(store, root=root)

    assert len(changed) == 1
    saved = store.get_dataset("old-jsonl")
    assert saved["benchmark_path"] == str((datasets_dir / "BENCHMARK_TRAIN.jsonl").resolve())
    assert len(parse_benchmark_file(saved["benchmark_path"])) == len(parse_benchmark_file(image_copy))
    assert saved["meta"]["format"] == "jsonl"
    assert saved["meta"]["materialized_from_path"] == old_path


def test_migrate_dataset_paths_repairs_live_dm_mis_host_path(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    datasets_dir = tmp_path / "runtime" / "datasets"
    monkeypatch.setenv("BENCH_APP_DATASETS_DIR", str(datasets_dir))
    source = Path(__file__).resolve().parents[2] / "BENCHMARK_dm_mis.jsonl"
    image_copy = root / "BENCHMARK_dm_mis.jsonl"
    image_copy.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    store = SQLiteStore(str(tmp_path / "migrate_dm_mis.db")).init()
    old_path = "/root/leaderboard_builder_codex/BENCHMARK_dm_mis.jsonl"
    store.save_dataset({
        "id": "cdc62ca0504b",
        "name": "dm_mis",
        "benchmark_path": old_path,
        "db_id": "dm_mis",
        "dsn": "postgresql://x",
        "db_type": "postgres",
    })

    changed = migrate_dataset_paths_to_jsonl(store, root=root)

    assert len(changed) == 1
    saved = store.get_dataset("cdc62ca0504b")
    assert saved["benchmark_path"] == str((datasets_dir / "BENCHMARK_dm_mis.jsonl").resolve())
    assert len(parse_benchmark_file(saved["benchmark_path"])) == 54
    assert saved["meta"]["format"] == "jsonl"
    assert saved["meta"]["materialized_from_path"] == old_path


def test_build_answers_excludes_l0_l4_fields(tmp_path):
    store = SQLiteStore(str(tmp_path / "answers.db")).init()
    c = store.save_connector({"name": "C", "url": "http://x/sql",
                              "body_template": '{"question":"{{question}}"}', "sql_extract": {}})
    d = store.save_dataset({"name": "D", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                            "db_id": "sports_events_large", "dsn": "postgresql://x"})
    run = store.create_run(dataset_id=d["id"], dataset_name="D",
                           connector_id=c["id"], connector_name="C")
    store.add_case(run["id"], 1, {"case_id": "t1", "difficulty": "Simple",
                                  "question": "q", "gold_sql": "SELECT 1",
                                  "predicted_sql": "SELECT 1", "level": None,
                                  "matched": False, "reason": None,
                                  "gold_result": {"columns": ["c"], "rows": [["1"]], "row_count": 1},
                                  "agent_result": {"columns": ["c"], "rows": [["1"]], "row_count": 1},
                                  "raw_response": '{"sql":"SELECT 1"}'})
    answers = build_answers(store, run["id"], d, c)

    assert answers["schema"] == "bench-answers/v1"
    row = answers["cases"][0]
    assert row["case_id"] == "t1" and row["raw_response"] == '{"sql":"SELECT 1"}'
    assert "level" not in row and "matched" not in row and "reason" not in row


def test_result_documents_keep_executed_sql_text_unformatted(tmp_path):
    store = SQLiteStore(str(tmp_path / "raw_sql.db")).init()
    c = store.save_connector({"name": "C", "url": "http://x/sql",
                              "body_template": '{"question":"{{question}}"}', "sql_extract": {}})
    d = store.save_dataset({"name": "D", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                            "db_id": "sports_events_large", "dsn": "postgresql://x"})
    run = store.create_run(dataset_id=d["id"], dataset_name="D",
                           connector_id=c["id"], connector_name="C")
    gold_sql = "select a, count(*) as c from foo where note = 'select from';"
    predicted_sql = "select a,count(*) c from foo where note='select from';"
    store.add_case(run["id"], 1, {"case_id": "t1", "difficulty": "Simple",
                                  "question": "q", "gold_sql": gold_sql,
                                  "predicted_sql": predicted_sql, "level": 4,
                                  "matched": True})

    result_case = build_result(store, run["id"], d, c)["cases"][0]
    answer_case = build_answers(store, run["id"], d, c)["cases"][0]

    assert result_case["gold_sql"] == gold_sql
    assert result_case["predicted_sql"] == predicted_sql
    assert answer_case["gold_sql"] == gold_sql
    assert answer_case["predicted_sql"] == predicted_sql


def test_apply_judged_levels_sets_final_scores(tmp_path):
    store = SQLiteStore(str(tmp_path / "judged.db")).init()
    c = store.save_connector({"name": "C", "url": "http://x/sql",
                              "body_template": '{"question":"{{question}}"}', "sql_extract": {}})
    d = store.save_dataset({"name": "D", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                            "db_id": "sports_events_large", "dsn": "postgresql://x"})
    run = store.create_run(dataset_id=d["id"], dataset_name="D",
                           connector_id=c["id"], connector_name="C")
    store.add_case(run["id"], 1, {"case_id": "t1", "question": "q",
                                  "gold_sql": "SELECT 1", "predicted_sql": "SELECT 1",
                                  "level": None, "matched": False})

    assessment = {"attempts": 2, "error_category": "correct", "confidence": 0.9}
    apply_judged_levels(store, run["id"], {"cases": [
        {"case_id": "t1", "level": 4, "reason": "candidate result matches gold",
         "assessment": assessment}
    ]})

    got = store.list_cases(run["id"])[0]
    assert got["level"] == 4
    assert got["matched"] is True
    assert got["reason"] == "candidate result matches gold"
    assert got["assessment"] == assessment


def test_rerun_updates_done_cases_and_keeps_completed_cases(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    store = SQLiteStore(str(tmp_path / "rerun.db")).init()
    c = store.save_connector({"name": "C", "url": "http://x/sql",
                              "body_template": "{}", "sql_extract": {}})
    d = store.save_dataset({"name": "D", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                            "db_id": "sports_events_large", "dsn": "postgresql://x"})
    run = store.create_run(dataset_id=d["id"], dataset_name=d["name"],
                           connector_id=c["id"], connector_name=c["name"])
    store.update_run(run["id"], status="queued", total_cases=3, done_cases=1)
    store.add_case(run["id"], 1, {"case_id": "train_count_drivers",
                                  "difficulty": "Simple", "question": "q",
                                  "gold_sql": "SELECT 1", "predicted_sql": "SELECT 1",
                                  "level": 4, "matched": True})

    async def fake_prewarm(*_args, **_kwargs):
        return 0

    async def fake_eval(*args, **_kwargs):
        case = args[4]
        idx = args[5]
        return {
            "idx": idx,
            "case_id": case.case_id,
            "difficulty": case.difficulty,
            "question": case.question,
            "gold_sql": case.gold_sql,
            "predicted_sql": None,
            "level": 0,
            "matched": False,
            "error": "fake connector error",
            "reason": "no predicted SQL",
            "elapsed_s": 0.0,
        }

    monkeypatch.setattr(runner_mod, "PgExecutor", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runner_mod, "TemplatedConnector", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runner_mod, "_prewarm_gold_cache", fake_prewarm)
    monkeypatch.setattr(runner_mod, "_eval_case", fake_eval)

    asyncio.run(rerun(store, run["id"]))

    got = store.get_run(run["id"])
    cases = store.list_cases(run["id"])
    assert got["done_cases"] == 3
    assert got["summary"]["done"] == 3
    assert [c["case_id"] for c in cases] == [
        "train_count_drivers",
        "train_count_circuits",
        "train_count_constructors",
    ]
    assert cases[0]["level"] == 4


def test_rerun_targets_non_l4_cases(tmp_path):
    store = SQLiteStore(str(tmp_path / "rerun-targets.db")).init()
    c = store.save_connector({"name": "C", "url": "http://x/sql",
                              "body_template": "{}", "sql_extract": {}})
    d = store.save_dataset({"name": "D", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                            "db_id": "sports_events_large", "dsn": "postgresql://x"})
    run = store.create_run(dataset_id=d["id"], dataset_name=d["name"],
                           connector_id=c["id"], connector_name=c["name"])
    store.update_run(run["id"], status="done", total_cases=3, done_cases=3)
    store.add_case(run["id"], 1, {"case_id": "train_count_drivers",
                                  "gold_sql": "SELECT 1", "predicted_sql": "SELECT bad",
                                  "level": 1, "matched": False})
    store.add_case(run["id"], 2, {"case_id": "train_count_circuits",
                                  "gold_sql": "SELECT 1", "predicted_sql": "SELECT 1",
                                  "level": 4, "matched": True})
    store.add_case(run["id"], 3, {"case_id": "train_count_constructors",
                                  "gold_sql": "SELECT 1", "predicted_sql": "SELECT 2",
                                  "level": 3, "matched": False})

    assert needs_rerun(store.list_cases(run["id"])[0]) is True
    assert count_rerun_targets(store, run["id"]) == 2


def test_rerun_non_l4_case_uses_llm_judge(tmp_path, monkeypatch):
    import bench_app.runner as runner_mod

    store = SQLiteStore(str(tmp_path / "rerun-judge.db")).init()
    c = store.save_connector({"name": "C", "url": "http://x/sql",
                              "body_template": "{}", "sql_extract": {}})
    d = store.save_dataset({"name": "D", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                            "db_id": "sports_events_large", "dsn": "postgresql://x"})
    run = store.create_run(dataset_id=d["id"], dataset_name=d["name"],
                           connector_id=c["id"], connector_name=c["name"])
    store.update_run(run["id"], status="done", total_cases=3, done_cases=3)
    for idx, case_id in enumerate([
        "train_count_drivers",
        "train_count_circuits",
        "train_count_constructors",
    ], 1):
        store.add_case(run["id"], idx, {
            "case_id": case_id,
            "difficulty": "Simple",
            "question": "q",
            "gold_sql": "SELECT 1",
            "predicted_sql": "SELECT bad" if idx == 1 else "SELECT 1",
            "level": 1 if idx == 1 else 4,
            "matched": idx != 1,
            "reason": "old failure" if idx == 1 else "ok",
        })

    collected = []
    judged_docs = []

    async def fake_prewarm(*_args, **_kwargs):
        return 0

    async def fake_collect(*args, **_kwargs):
        case = args[4]
        idx = args[5]
        collected.append(case.case_id)
        return {
            "idx": idx,
            "case_id": case.case_id,
            "difficulty": case.difficulty,
            "question": case.question,
            "gold_sql": case.gold_sql,
            "predicted_sql": "SELECT 1",
            "level": None,
            "matched": False,
            "error": "",
            "reason": None,
            "elapsed_s": 0.01,
            "gold_result": {"ok": True, "rows": [["1"]], "columns": ["c"], "row_count": 1},
            "agent_result": {"ok": True, "rows": [["1"]], "columns": ["c"], "row_count": 1},
        }

    async def fake_judge(answers_doc, *_args, **_kwargs):
        judged_docs.append(answers_doc)
        case = answers_doc["cases"][0]
        return {"cases": [{
            "case_id": case["case_id"],
            "level": 4,
            "reason": "fixed",
            "assessment": {"attempts": 1, "error_category": "correct", "confidence": 1.0},
        }], "judge_summary": {"invalid": 0}}

    monkeypatch.setattr(runner_mod, "PgExecutor", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runner_mod, "TemplatedConnector", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(runner_mod, "_prewarm_gold_cache", fake_prewarm)
    monkeypatch.setattr(runner_mod, "_collect_case", fake_collect)
    monkeypatch.setattr(runner_mod, "judge_answers", fake_judge)
    monkeypatch.setattr(runner_mod, "_dump_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner_mod, "_dump_answers_json", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runner_mod, "_dump_judged_levels_json", lambda *_args, **_kwargs: None)

    asyncio.run(rerun(store, run["id"], judge_cfg={
        "base_url": "http://judge.local/v1",
        "api_key": "test",
        "model": "judge",
    }))

    cases = store.list_cases(run["id"])
    assert collected == ["train_count_drivers"]
    assert [doc["cases"][0]["case_id"] for doc in judged_docs] == ["train_count_drivers"]
    assert cases[0]["predicted_sql"] == "SELECT 1"
    assert cases[0]["level"] == 4
    assert cases[0]["matched"] is True
    assert cases[0]["reason"] == "fixed"
    assert store.get_run(run["id"])["status"] == "done"
    assert store.get_run(run["id"])["summary"]["L4"] == 3


def test_normalise_level_accepts_l_prefix():
    assert _normalise_level("L0") == 0
    assert _normalise_level("4") == 4
    assert _normalise_level("L5") is None


def test_level_judge_validator_accepts_strict_json_inside_text():
    parsed, err = _validate_level_judge_output(
        '```json\n{"level":"L3","reason":"rows differ","error_category":"wrong_result","confidence":0.72}\n```'
    )
    assert err is None
    assert parsed.level == "L3"
    assert parsed.error_category == "wrong_result"


def test_level_judge_validator_rejects_extra_fields_and_bad_level():
    parsed, err = _validate_level_judge_output(
        '{"level":"L7","reason":"bad","error_category":"wrong_result","confidence":1.2,"extra":true}'
    )
    assert parsed is None
    assert "ValidationError" in err


class _FakeJudgeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class _FakeJudgeClient:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = 0

    async def post(self, *_args, **_kwargs):
        self.calls += 1
        item = self.contents.pop(0)
        if isinstance(item, Exception):
            raise item
        return _FakeJudgeResponse(item)


def test_level_judge_retries_after_invalid_output_and_bad_repair():
    client = _FakeJudgeClient([
        "not json",
        '{"level":"L9","reason":"","error_category":"wrong_result","confidence":2}',
        '{"level":"L4","reason":"results match","error_category":"correct","confidence":0.91}',
    ])

    out = asyncio.run(_judge_level_case(
        client,
        {"base_url": "http://judge.local/v1", "api_key": "test-key", "model": "judge-model"},
        {"case_id": "t1", "question": "q", "gold_sql": "SELECT 1", "predicted_sql": "SELECT 1"},
        timeout=1,
        max_retries=1,
        retry_delay=0,
    ))

    assert out["level"] == 4
    assert out["attempts"] == 2
    assert client.calls == 3


def test_llm_connection_smoke_uses_chat_completion_shape():
    client = _FakeJudgeClient(["BENCH_LLM_OK"])
    out = asyncio.run(check_llm_connection(
        {"base_url": "http://judge.local/v1", "api_key": "test-key", "model": "judge-model"},
        timeout=1,
        client=client,
    ))

    assert out["ok"] is True
    assert out["model"] == "judge-model"
    assert out["content"] == "BENCH_LLM_OK"
    assert client.calls == 1


def test_progress_bus_snapshots_active_case_statuses():
    b = _Bus()
    b.publish({"type": "case", "run_id": "r1", "case": {
        "idx": 1,
        "case_id": "c1",
        "case_status": "api_waiting",
        "case_status_label": "ждем ответ API",
    }})

    snap = b.case_snapshot()
    assert snap == [{"run_id": "r1", "case": {
        "idx": 1,
        "case_id": "c1",
        "case_status": "api_waiting",
        "case_status_label": "ждем ответ API",
    }}]

    b.publish({"type": "case", "run_id": "r1", "case": {
        "idx": 1,
        "case_id": "c1",
        "case_status": "judged",
    }})
    assert b.case_snapshot() == []

    b.publish({"type": "case", "run_id": "r1", "case": {
        "idx": 2,
        "case_id": "c2",
        "case_status": "llm_queued",
        "case_status_label": "в очереди на LLM-оценку",
    }})
    assert b.case_snapshot() == [{"run_id": "r1", "case": {
        "idx": 2,
        "case_id": "c2",
        "case_status": "llm_queued",
        "case_status_label": "в очереди на LLM-оценку",
    }}]
    b.clear_run("r1")
    assert b.case_snapshot() == []


def test_progress_snapshot_restores_persisted_partial_cases(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_STORE_URL", f"sqlite:///{tmp_path / 'server.db'}")
    monkeypatch.setenv("BENCH_APP_SYNC_CONNECTOR_YAML", "0")

    import importlib
    import bench_app.server as server

    server = importlib.reload(server)
    server.bus._cases.clear()
    store = server.STORE
    conn = store.save_connector({"id": "conn", "name": "C", "url": "http://x/sql",
                                 "body_template": "{}", "sql_extract": {}})
    ds = store.save_dataset({"id": "ds", "name": "D", "benchmark_path": "BENCHMARK_TRAIN.jsonl",
                             "db_id": "sports_events_large", "dsn": "postgresql://x"})
    run = store.create_run(dataset_id=ds["id"], dataset_name=ds["name"],
                           connector_id=conn["id"], connector_name=conn["name"])
    store.update_run(run["id"], status="stopped", total_cases=3, done_cases=1)
    store.add_case(run["id"], 1, {"case_id": "t1", "difficulty": "Simple",
                                  "question": "q", "gold_sql": "SELECT 1",
                                  "predicted_sql": "SELECT 1", "level": 4,
                                  "matched": True, "raw_response": "x" * 10000,
                                  "gold_result": {"rows": [["1"]]},
                                  "agent_result": {"rows": [["1"]]}})

    snap = server._progress_case_snapshot([store.get_run(run["id"])])
    assert snap[0]["run_id"] == run["id"]
    assert snap[0]["case"]["case_id"] == "t1"
    assert snap[0]["case"]["level"] == 4
    assert "raw_response" not in snap[0]["case"]
    assert "gold_result" not in snap[0]["case"]
    assert "agent_result" not in snap[0]["case"]

    server.bus.publish({"type": "case", "run_id": run["id"], "case": {
        "idx": 1,
        "case_id": "t1",
        "case_status": "api_waiting",
        "case_status_label": "ждем ответ API",
    }})
    snap = server._progress_case_snapshot([store.get_run(run["id"])])
    assert len(snap) == 1
    assert snap[0]["case"]["case_status"] == "api_waiting"


def test_spa_fallback_rejects_common_probe_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_STORE_URL", f"sqlite:///{tmp_path / 'server.db'}")
    monkeypatch.setenv("BENCH_APP_SYNC_CONNECTOR_YAML", "0")

    import importlib
    import bench_app.server as server

    server = importlib.reload(server)
    assert server._should_spa_fallback("chat")
    assert server._should_spa_fallback("datasets/edit")
    assert not server._should_spa_fallback(".git/config")
    assert not server._should_spa_fallback("app/.git/config")
    assert not server._should_spa_fallback("wp-json/wp/v2/settings")
    assert not server._should_spa_fallback("login")
    assert not server._should_spa_fallback("missing.js")
