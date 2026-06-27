"""Pluggable storage for the benchmark app.

A thin DB-agnostic interface (`Store`) with a SQL implementation that works over
any DB-API connection. `SQLiteStore` is the default; `PostgresStore` plugs the
same schema into Postgres. Pick via `make_store(url)`:
    sqlite:///path/to/app.db   (default)
    postgresql://user:pass@host/db

Everything a run produces is also mirrored to a JSON file per revision (see
runner.finalize) so results can be downloaded independently of the DB.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any


def _now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS connectors (
        id TEXT PRIMARY KEY, name TEXT, method TEXT, url TEXT,
        headers TEXT, body_template TEXT, sql_extract TEXT,
        default_dialect TEXT, timeout INTEGER, max_attempts INTEGER,
        description TEXT, db_id TEXT, retry_delay REAL, created_at REAL, updated_at REAL )""",
    """CREATE TABLE IF NOT EXISTS datasets (
        id TEXT PRIMARY KEY, name TEXT, benchmark_path TEXT, db_id TEXT,
        dsn TEXT, db_type TEXT, meta TEXT, created_at REAL )""",
    """CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY, dataset_id TEXT, dataset_name TEXT,
        connector_id TEXT, connector_name TEXT, status TEXT,
        total_cases INTEGER, done_cases INTEGER, summary TEXT, error TEXT,
        config TEXT, created_at REAL, started_at REAL, finished_at REAL )""",
    """CREATE TABLE IF NOT EXISTS run_cases (
        run_id TEXT, idx INTEGER, case_id TEXT, difficulty TEXT, question TEXT,
        gold_sql TEXT, predicted_sql TEXT, level INTEGER, matched INTEGER,
        error TEXT, reason TEXT, elapsed_s REAL, gold_result TEXT,
        agent_result TEXT, attempts INTEGER, assessment TEXT,
        case_status TEXT, case_status_label TEXT )""",
    """CREATE TABLE IF NOT EXISTS run_jobs (
        id TEXT PRIMARY KEY, run_id TEXT, job_type TEXT, payload TEXT,
        status TEXT, attempts INTEGER, locked_by TEXT, locked_at REAL,
        heartbeat_at REAL, error TEXT, created_at REAL, updated_at REAL,
        started_at REAL, finished_at REAL )""",
    "CREATE INDEX IF NOT EXISTS ix_runcases_run ON run_cases(run_id)",
    "CREATE INDEX IF NOT EXISTS ix_runs_dataset ON runs(dataset_id)",
    "CREATE INDEX IF NOT EXISTS ix_runjobs_status ON run_jobs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS ix_runjobs_run ON run_jobs(run_id)",
]


CASE_COLUMNS = (
    "run_id", "idx", "case_id", "difficulty", "question", "gold_sql",
    "predicted_sql", "level", "matched", "error", "reason", "elapsed_s",
    "gold_result", "agent_result", "attempts", "raw_response", "assessment",
    "case_status", "case_status_label", "human_level",
)
CASE_LIGHT_COLUMNS = (
    "run_id", "idx", "case_id", "difficulty", "question", "gold_sql",
    "predicted_sql", "level", "matched", "error", "reason", "elapsed_s",
    "attempts", "case_status", "case_status_label", "human_level",
)


class SqlStore:
    """Base implementation over a DB-API connection. Subclasses set `self.ph`
    (paramstyle placeholder) and provide `_connect()`."""

    ph = "?"  # sqlite

    def _connect(self):  # pragma: no cover
        raise NotImplementedError

    def _exec(self, sql: str, params: tuple = (), *, fetch: str | None = None):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            out = None
            if fetch == "one":
                row = cur.fetchone()
                out = dict(zip([d[0] for d in cur.description], row)) if row else None
            elif fetch == "all":
                cols = [d[0] for d in cur.description]
                out = [dict(zip(cols, r)) for r in cur.fetchall()]
            conn.commit()
            return out
        finally:
            conn.close()

    def init(self):
        for stmt in SCHEMA:
            self._exec(stmt)
        # best-effort migrations for pre-existing tables
        for alter in ("ALTER TABLE datasets ADD COLUMN meta TEXT",
                      "ALTER TABLE datasets ADD COLUMN db_type TEXT",
                      "ALTER TABLE connectors ADD COLUMN description TEXT",
                      "ALTER TABLE connectors ADD COLUMN db_id TEXT",
                      "ALTER TABLE connectors ADD COLUMN retry_delay REAL",
                      "ALTER TABLE runs ADD COLUMN config TEXT",
                      "ALTER TABLE run_cases ADD COLUMN raw_response TEXT",
                      "ALTER TABLE run_cases ADD COLUMN human_level INTEGER",
                      "ALTER TABLE run_cases ADD COLUMN assessment TEXT",
                      "ALTER TABLE run_cases ADD COLUMN case_status TEXT",
                      "ALTER TABLE run_cases ADD COLUMN case_status_label TEXT",
                      "ALTER TABLE run_jobs ADD COLUMN attempts INTEGER",
                      "ALTER TABLE run_jobs ADD COLUMN locked_by TEXT",
                      "ALTER TABLE run_jobs ADD COLUMN locked_at REAL",
                      "ALTER TABLE run_jobs ADD COLUMN heartbeat_at REAL",
                      "ALTER TABLE run_jobs ADD COLUMN error TEXT",
                      "ALTER TABLE run_jobs ADD COLUMN started_at REAL",
                      "ALTER TABLE run_jobs ADD COLUMN finished_at REAL"):
            try:
                self._exec(alter)
            except Exception:
                pass
        return self

    # ---- connectors ----
    def save_connector(self, c: dict) -> dict:
        c = dict(c)
        if not c.get("id"):
            c["id"] = new_id()
        c["updated_at"] = _now()
        c.setdefault("created_at", c["updated_at"])
        p = self.ph
        self._exec(
            f"INSERT INTO connectors (id,name,method,url,headers,body_template,sql_extract,"
            f"default_dialect,timeout,max_attempts,description,db_id,retry_delay,created_at,updated_at) VALUES "
            f"({','.join([p]*15)}) ON CONFLICT(id) DO UPDATE SET "
            f"name=excluded.name,method=excluded.method,url=excluded.url,headers=excluded.headers,"
            f"body_template=excluded.body_template,sql_extract=excluded.sql_extract,"
            f"default_dialect=excluded.default_dialect,timeout=excluded.timeout,"
            f"max_attempts=excluded.max_attempts,description=excluded.description,db_id=excluded.db_id,"
            f"retry_delay=excluded.retry_delay,updated_at=excluded.updated_at",
            (c["id"], c.get("name"), c.get("method", "POST"), c.get("url"),
             json.dumps(c.get("headers", {}), ensure_ascii=False),
             c.get("body_template", ""), json.dumps(c.get("sql_extract", {}), ensure_ascii=False),
             c.get("default_dialect", "postgres"), int(c.get("timeout", 200)),
             int(c.get("max_attempts", 1)), c.get("description", ""), c.get("db_id", ""),
             float(c.get("retry_delay", 0) or 0), c["created_at"], c["updated_at"]),
        )
        if c.get("name"):
            self._exec(f"UPDATE runs SET connector_name={p} WHERE connector_id={p}",
                       (c["name"], c["id"]))
        return self.get_connector(c["id"])

    def _row_connector(self, r):
        if not r:
            return None
        r["headers"] = json.loads(r.get("headers") or "{}")
        r["sql_extract"] = json.loads(r.get("sql_extract") or "{}")
        return r

    def get_connector(self, cid):
        return self._row_connector(self._exec(f"SELECT * FROM connectors WHERE id={self.ph}", (cid,), fetch="one"))

    def list_connectors(self):
        return [self._row_connector(r) for r in self._exec("SELECT * FROM connectors ORDER BY updated_at DESC", fetch="all")]

    def delete_connector(self, cid):
        self._exec(f"DELETE FROM connectors WHERE id={self.ph}", (cid,))

    # ---- datasets ----
    def save_dataset(self, d: dict) -> dict:
        d = dict(d)
        if not d.get("id"):
            d["id"] = new_id()
        d.setdefault("created_at", _now())
        p = self.ph
        self._exec(
            f"INSERT INTO datasets (id,name,benchmark_path,db_id,dsn,db_type,meta,created_at) VALUES ({','.join([p]*8)}) "
            f"ON CONFLICT(id) DO UPDATE SET name=excluded.name,benchmark_path=excluded.benchmark_path,"
            f"db_id=excluded.db_id,dsn=excluded.dsn,db_type=excluded.db_type,meta=excluded.meta",
            (d["id"], d.get("name"), d.get("benchmark_path"), d.get("db_id"), d.get("dsn"),
             d.get("db_type") or "postgres",
             json.dumps(d.get("meta") or {}, ensure_ascii=False), d["created_at"]),
        )
        if d.get("name"):
            self._exec(f"UPDATE runs SET dataset_name={p} WHERE dataset_id={p}",
                       (d["name"], d["id"]))
        return self.get_dataset(d["id"])

    def _row_dataset(self, r):
        if r and r.get("meta"):
            try:
                r["meta"] = json.loads(r["meta"])
            except Exception:
                pass
        return r

    def get_dataset(self, did):
        return self._row_dataset(self._exec(f"SELECT * FROM datasets WHERE id={self.ph}", (did,), fetch="one"))

    def list_datasets(self):
        return [self._row_dataset(r) for r in self._exec("SELECT * FROM datasets ORDER BY name", fetch="all")]

    def delete_dataset(self, did):
        self._exec(f"DELETE FROM datasets WHERE id={self.ph}", (did,))

    # ---- runs ----
    def create_run(self, **kw) -> dict:
        rid = new_id(); now = _now()
        p = self.ph
        self._exec(
            f"INSERT INTO runs (id,dataset_id,dataset_name,connector_id,connector_name,status,"
            f"total_cases,done_cases,summary,error,config,created_at,started_at,finished_at) "
            f"VALUES ({','.join([p]*14)})",
            (rid, kw.get("dataset_id"), kw.get("dataset_name"), kw.get("connector_id"),
             kw.get("connector_name"), "queued", int(kw.get("total_cases", 0)), 0,
             None, None, json.dumps(kw.get("config") or {}, ensure_ascii=False), now, None, None),
        )
        return self.get_run(rid)

    def update_run(self, rid, **kw):
        if not kw:
            return
        if "summary" in kw and isinstance(kw["summary"], (dict, list)):
            kw["summary"] = json.dumps(kw["summary"], ensure_ascii=False)
        sets = ",".join(f"{k}={self.ph}" for k in kw)
        self._exec(f"UPDATE runs SET {sets} WHERE id={self.ph}", (*kw.values(), rid))

    def _row_run(self, r):
        if not r:
            return None
        for k in ("summary", "config"):
            if r.get(k):
                try:
                    r[k] = json.loads(r[k])
                except Exception:
                    pass
        return r

    def get_run(self, rid):
        return self._row_run(self._exec(f"SELECT * FROM runs WHERE id={self.ph}", (rid,), fetch="one"))

    def delete_run(self, rid):
        self._exec(f"DELETE FROM run_jobs WHERE run_id={self.ph}", (rid,))
        self._exec(f"DELETE FROM run_cases WHERE run_id={self.ph}", (rid,))
        self._exec(f"DELETE FROM runs WHERE id={self.ph}", (rid,))

    def list_runs(self, dataset_id=None):
        if dataset_id:
            rows = self._exec(f"SELECT * FROM runs WHERE dataset_id={self.ph} ORDER BY created_at DESC", (dataset_id,), fetch="all")
        else:
            rows = self._exec("SELECT * FROM runs ORDER BY created_at DESC", fetch="all")
        return [self._row_run(r) for r in rows]

    # ---- durable worker queue ----
    def _row_job(self, r):
        if not r:
            return None
        if r.get("payload"):
            try:
                r["payload"] = json.loads(r["payload"])
            except Exception:
                r["payload"] = {}
        else:
            r["payload"] = {}
        return r

    def enqueue_job(self, run_id: str, job_type: str, payload: dict | None = None) -> dict:
        jid = new_id()
        now = _now()
        p = self.ph
        self._exec(
            f"INSERT INTO run_jobs (id,run_id,job_type,payload,status,attempts,locked_by,locked_at,"
            f"heartbeat_at,error,created_at,updated_at,started_at,finished_at) "
            f"VALUES ({','.join([p]*14)})",
            (jid, run_id, job_type, json.dumps(payload or {}, ensure_ascii=False),
             "queued", 0, None, None, None, None, now, now, None, None),
        )
        return self.get_job(jid)

    def get_job(self, job_id: str):
        return self._row_job(self._exec(f"SELECT * FROM run_jobs WHERE id={self.ph}", (job_id,), fetch="one"))

    def list_jobs(self, *, run_id: str | None = None, statuses: list[str] | tuple[str, ...] | None = None, limit: int = 100):
        params: list[Any] = []
        where: list[str] = []
        if run_id:
            where.append(f"run_id={self.ph}")
            params.append(run_id)
        if statuses:
            where.append(f"status IN ({','.join([self.ph] * len(statuses))})")
            params.extend(statuses)
        sql = "SELECT * FROM run_jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [self._row_job(r) for r in self._exec(sql, tuple(params), fetch="all")]

    def job_counts(self) -> dict:
        rows = self._exec("SELECT status, COUNT(*) AS count FROM run_jobs GROUP BY status", fetch="all")
        return {str(r.get("status") or "unknown"): int(r.get("count") or 0) for r in rows}

    def recover_stale_jobs(self, *, stale_after_s: float = 900.0, max_attempts: int = 3) -> dict:
        """Recover jobs whose worker heartbeat disappeared.

        A job is requeued while it still has attempts left. After that it is
        marked failed and the owning active run is marked error so the UI does
        not show an eternal "running" benchmark.
        """
        p = self.ph
        conn = self._connect()
        now = _now()
        stale_before = now - max(1.0, float(stale_after_s or 900.0))
        max_attempts = max(1, int(max_attempts or 3))
        try:
            cur = conn.cursor()
            cur.execute(
                f"SELECT * FROM run_jobs WHERE status={p} "
                f"AND COALESCE(heartbeat_at, locked_at, started_at, created_at) < {p}",
                ("running", stale_before),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            requeued = failed = 0
            for job in rows:
                attempts = int(job.get("attempts") or 0)
                if attempts >= max_attempts:
                    err = f"worker heartbeat stale after {attempts} attempt(s); job failed"
                    cur.execute(
                        f"UPDATE run_jobs SET status={p}, error={p}, updated_at={p}, finished_at={p} "
                        f"WHERE id={p}",
                        ("error", err, now, now, job["id"]),
                    )
                    if job.get("run_id"):
                        cur.execute(
                            f"UPDATE runs SET status={p}, error={p}, finished_at={p} "
                            f"WHERE id={p} AND status IN ({p},{p},{p},{p})",
                            ("error", err, now, job["run_id"], "queued", "running", "paused", "judging"),
                        )
                    failed += 1
                else:
                    cur.execute(
                        f"UPDATE run_jobs SET status={p}, locked_by=NULL, locked_at=NULL, heartbeat_at=NULL,"
                        f"updated_at={p}, error={p} WHERE id={p}",
                        ("queued", now, "worker heartbeat stale; requeued", job["id"]),
                    )
                    requeued += 1
            conn.commit()
            return {"stale": len(rows), "requeued": requeued, "failed": failed}
        finally:
            conn.close()

    def claim_next_job(self, worker_id: str, *, stale_after_s: float = 900.0, max_attempts: int = 3):
        """Atomically claim the oldest queued job. Stale running jobs are returned
        to the queue first so a restarted worker can continue from disk."""
        p = self.ph
        conn = self._connect()
        now = _now()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM run_jobs WHERE status={p} ORDER BY created_at ASC LIMIT 1", ("queued",))
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None
            cols = [d[0] for d in cur.description]
            job = dict(zip(cols, row))
            cur.execute(
                f"UPDATE run_jobs SET status={p}, attempts=COALESCE(attempts,0)+1, locked_by={p},"
                f"locked_at={p}, heartbeat_at={p}, updated_at={p}, started_at=COALESCE(started_at,{p}) "
                f"WHERE id={p}",
                ("running", worker_id, now, now, now, now, job["id"]),
            )
            conn.commit()
            return self.get_job(job["id"])
        finally:
            conn.close()

    def heartbeat_job(self, job_id: str, worker_id: str) -> None:
        now = _now()
        self._exec(
            f"UPDATE run_jobs SET heartbeat_at={self.ph}, updated_at={self.ph} "
            f"WHERE id={self.ph} AND locked_by={self.ph} AND status={self.ph}",
            (now, now, job_id, worker_id, "running"),
        )

    def finish_job(self, job_id: str, worker_id: str, *, status: str = "done", error: str | None = None) -> None:
        now = _now()
        self._exec(
            f"UPDATE run_jobs SET status={self.ph}, error={self.ph}, heartbeat_at={self.ph},"
            f"updated_at={self.ph}, finished_at={self.ph} "
            f"WHERE id={self.ph} AND locked_by={self.ph}",
            (status, error, now, now, now, job_id, worker_id),
        )

    def cancel_jobs_for_run(self, run_id: str) -> int:
        jobs = self.list_jobs(run_id=run_id, statuses=("queued", "running"), limit=10000)
        now = _now()
        self._exec(
            f"UPDATE run_jobs SET status={self.ph}, error={self.ph}, updated_at={self.ph}, finished_at={self.ph} "
            f"WHERE run_id={self.ph} AND status IN ({self.ph},{self.ph})",
            ("cancelled", "cancelled by user", now, now, run_id, "queued", "running"),
        )
        return len(jobs)

    # ---- case results ----
    def add_case(self, rid, idx, c: dict):
        p = self.ph
        self._exec(
            f"INSERT INTO run_cases (run_id,idx,case_id,difficulty,question,gold_sql,predicted_sql,"
            f"level,matched,error,reason,elapsed_s,gold_result,agent_result,attempts,raw_response,assessment,"
            f"case_status,case_status_label) "
            f"VALUES ({','.join([p]*19)})",
            (rid, idx, c.get("case_id"), c.get("difficulty"), c.get("question"),
             c.get("gold_sql"), c.get("predicted_sql"), c.get("level"),
             1 if c.get("matched") else 0, c.get("error"), c.get("reason"),
             c.get("elapsed_s"), json.dumps(c.get("gold_result"), ensure_ascii=False),
             json.dumps(c.get("agent_result"), ensure_ascii=False), c.get("attempts", 1),
             c.get("raw_response"),
             json.dumps(c.get("assessment"), ensure_ascii=False) if c.get("assessment") is not None else None,
             c.get("case_status"), c.get("case_status_label")),
        )

    def replace_case(self, rid, idx, c: dict):
        """Re-run support: drop the existing row for this (run, idx) and re-insert."""
        self._exec(f"DELETE FROM run_cases WHERE run_id={self.ph} AND idx={self.ph}", (rid, idx))
        self.add_case(rid, idx, c)

    def set_case_grade(self, rid, case_id, level):
        """Human override of a case's level (L0–L4); pass None to clear → back to auto."""
        self._exec(f"UPDATE run_cases SET human_level={self.ph} WHERE run_id={self.ph} AND case_id={self.ph}",
                   (level, rid, case_id))

    def list_cases(self, rid, *, include_payload: bool = True):
        cols = CASE_COLUMNS if include_payload else CASE_LIGHT_COLUMNS
        rows = self._exec(
            f"SELECT {','.join(cols)} FROM run_cases WHERE run_id={self.ph} ORDER BY idx",
            (rid,),
            fetch="all",
        )
        return self.list_cases_from_rows(rows)

    def get_case(self, rid, case_id: str, *, include_payload: bool = True):
        cols = CASE_COLUMNS if include_payload else CASE_LIGHT_COLUMNS
        row = self._exec(
            f"SELECT {','.join(cols)} FROM run_cases WHERE run_id={self.ph} AND case_id={self.ph} ORDER BY idx LIMIT 1",
            (rid, case_id),
            fetch="one",
        )
        if not row:
            return None
        return self.list_cases_from_rows([row])[0]

    def list_cases_from_rows(self, rows):
        for r in rows:
            r["matched"] = bool(r.get("matched"))
            for k in ("gold_result", "agent_result", "assessment"):
                if k not in r:
                    continue
                try:
                    r[k] = json.loads(r.get(k) or "null")
                except Exception:
                    pass
        return rows


class SQLiteStore(SqlStore):
    ph = "?"

    def __init__(self, path: str):
        self.path = path

    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(self.path, timeout=60, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn


class PostgresStore(SqlStore):
    ph = "%s"

    def __init__(self, dsn: str):
        self.dsn = dsn

    def _connect(self):
        import psycopg
        return psycopg.connect(self.dsn)


def make_store(url: str | None = None):
    """Factory: sqlite:///path or postgresql://… ; default sqlite app.db."""
    import os
    url = url or os.getenv("BENCH_STORE_URL") or "sqlite:///bench_app/data/app.db"
    if url.startswith("sqlite:///"):
        path = url[len("sqlite:///"):]
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        return SQLiteStore(path).init()
    if url.startswith(("postgres://", "postgresql://")):
        return PostgresStore(url).init()
    raise SystemExit(f"unsupported BENCH_STORE_URL: {url}")
