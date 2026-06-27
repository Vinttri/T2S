"""End-to-end test of the full run pipeline.

Spins up a tiny local HTTP server that plays the role of a participant API
(question -> SQL), points a real TemplatedConnector at it, and runs the whole
`run_task` against the Training (e2e) benchmark on the scoring Postgres
(localhost:15432). Asserts every case scores L4 and a JSON revision is dumped.

Skips automatically if Postgres / the sports_events_large DB isn't reachable, so
the offline unit suite still runs everywhere.
"""
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from leaderboard.db import PgExecutor
from bench_app.store import SQLiteStore
from bench_app.runner import run_task, run_json_path

DSN = "postgresql://bank:bankpass@localhost:15432/sports_events_large"
BENCH = os.path.join(os.path.dirname(__file__), "..", "..", "BENCHMARK_TRAIN.jsonl")

# question keyword -> gold-equivalent SQL the mock "model" returns
ANSWERS = {
    "driver": "SELECT COUNT(*) AS driver_count FROM drivers;",
    "circuit": "SELECT COUNT(*) AS circuit_count FROM circuits;",
    "constructor": "SELECT COUNT(*) AS constructor_count FROM constructors;",
}


def _db_available() -> bool:
    try:
        return PgExecutor(DSN, statement_timeout_ms=3000).execute_select("SELECT 1").ok
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="scoring Postgres not reachable")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        q = (body.get("question") or "").lower()
        sql = next((v for k, v in ANSWERS.items() if k in q), "SELECT 1")
        payload = json.dumps({"sql": sql, "error": None}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def mock_api():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/sql"
    srv.shutdown()


def test_full_run_scores_all_l4(tmp_path, mock_api):
    store = SQLiteStore(str(tmp_path / "e2e.db")).init()
    dataset = {"id": "ds1", "name": "Training (e2e)", "benchmark_path": os.path.abspath(BENCH),
               "db_id": "sports_events_large", "dsn": DSN}
    connector = {"id": "cn1", "name": "MockModel", "method": "POST", "url": mock_api,
                 "headers": {"Content-Type": "application/json"},
                 "body_template": '{"question":"{{question}}","database":"{{database}}"}',
                 "sql_extract": {"mode": "json", "field": "sql"},
                 "default_dialect": "postgres", "timeout": 30, "max_attempts": 1}
    run = store.create_run(dataset_id="ds1", dataset_name="Training (e2e)",
                           connector_id="cn1", connector_name="MockModel")

    asyncio.run(run_task(store, run["id"], dataset, connector))

    got = store.get_run(run["id"])
    assert got["status"] == "done"
    s = got["summary"]
    assert s["total"] == 3 and s["passed"] == 3 and s["accuracy"] == 100.0
    assert s["L4"] == 3 and s["L0"] == s["L1"] == s["L2"] == s["L3"] == 0

    cases = store.list_cases(run["id"])
    assert {c["case_id"] for c in cases} == {
        "train_count_drivers", "train_count_circuits", "train_count_constructors"}
    for c in cases:
        assert c["level"] == 4 and c["matched"]
        assert c["gold_result"] and c["agent_result"]

    # JSON revision snapshot was written and is self-contained
    p = run_json_path(run["id"])
    assert os.path.exists(p)
    blob = json.load(open(p, encoding="utf-8"))
    assert blob["schema"] == "bench-result/v1"
    assert blob["run_id"] == run["id"] and len(blob["cases"]) == 3
    assert blob["benchmark"]["db_id"] == "sports_events_large"
    assert blob["summary"]["levels"]["L4"] == 3


def test_wrong_sql_scores_l3(tmp_path, mock_api):
    """A connector that always returns a valid-but-wrong query scores L3, not L4."""
    store = SQLiteStore(str(tmp_path / "e2e2.db")).init()
    dataset = {"id": "ds1", "name": "Training (e2e)", "benchmark_path": os.path.abspath(BENCH),
               "db_id": "sports_events_large", "dsn": DSN}
    # returns a constant 0 count -> executes fine but rows differ from gold
    connector = {"id": "cn2", "name": "WrongModel", "method": "POST", "url": mock_api.replace("/sql", "/sql"),
                 "headers": {"Content-Type": "application/json"},
                 "body_template": '{"question":"nonsense"}',  # never matches a keyword -> SELECT 1
                 "sql_extract": {"mode": "json", "field": "sql"},
                 "default_dialect": "postgres", "timeout": 30, "max_attempts": 1}
    run = store.create_run(dataset_id="ds1", dataset_name="Training (e2e)",
                           connector_id="cn2", connector_name="WrongModel")
    asyncio.run(run_task(store, run["id"], dataset, connector))
    got = store.get_run(run["id"])
    assert got["status"] == "done"
    # SELECT 1 executes but never equals the gold counts -> all L3
    assert got["summary"]["L3"] == 3 and got["summary"]["passed"] == 0
