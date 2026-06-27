# Leaderboard Builder Codex Context

Last updated: 2026-06-10.

This file is for the next coding LLM working in `/root/leaderboard_builder_codex`.
It captures the current system state, architecture, commands, and operational
rules so the next agent does not restart from stale assumptions.

## Current State

- Active workspace: `/root/leaderboard_builder_codex`
- Public benchmark URL: `http://benchmark.144.91.85.207.nip.io:8080/`
- Live deployment now runs as split Docker Compose:
  `benchmark-frontend` (nginx static UI + `/api`/`/ws` proxy) listens on host
  port `8090`, `benchmark-backend` (FastAPI/uvicorn) is only exposed on the
  internal compose network, `benchmark-worker` executes benchmark jobs from the
  durable `run_jobs` queue in `bench_app/data/app.db`, and `benchmark-backup`
  periodically backs up `/data/app.db` plus runtime artifact dirs to `/backups`.
  Runtime env for the live compose stack is `.env.compose.live`; it contains
  secrets and must not be committed.
- Target deployment for the portable/offline package is Docker Compose, not
  Kubernetes. The compose stack is split into `backend` (FastAPI/uvicorn),
  `worker` (durable benchmark executor), `backup` (periodic on-disk backups),
  and `frontend` (nginx static UI + `/api`/`/ws` proxy). Use `Dockerfile`,
  `Dockerfile.frontend`, `docker-compose.yml`,
  `scripts/build-bench-app-docker.sh`, and
  `scripts/export-bench-app-offline.sh`; do not prepare k8s manifests unless
  the user explicitly asks for Kubernetes.
- Current service was switched from `/root/leaderboard_builder` to
  `/root/leaderboard_builder_codex`.
- The old host/systemd uvicorn service `leaderboard-bench-8090.service` should
  remain stopped while compose owns host port `8090`. The host script
  `scripts/run-benchmark-host.sh` is now a fallback/manual debug path, not the
  normal live deployment.
- Dataset paths inside live Docker `bench_app/data/app.db` must point to
  container-visible runtime files under `/data/datasets/*.jsonl`. Shipped
  `BENCHMARK_*.jsonl` files may exist in the image at `/app`, but startup
  migration materializes them into `/data/datasets` before use. Host paths like
  `/root/leaderboard_builder_codex/BENCHMARK_*.jsonl` are valid only for host
  debugging and must not be stored in the live Docker DB.
- Dataset DSNs are resolved only from env on create/upload/update. The UI must
  not ask for or persist a manual DSN; `db_type` is inferred from the resolved
  DSN scheme (`impala://`, `postgresql://`, etc.).
- Dataset upload UI must only ask for the dataset display name and the JSONL
  file. `db_id` is hidden and inferred from the dataset name or uploaded file
  name, then the scoring DSN is resolved from env using that `db_id`.
- The old `/root/leaderboard_builder` directory still exists but is no longer
  the intended coding root for this work.

Verify live containers:

```bash
cd /root/leaderboard_builder_codex
docker compose ps
curl -fsS http://127.0.0.1:8090/api/health
curl -fsS http://127.0.0.1:8090/api/live
curl -fsS http://127.0.0.1:8090/api/ready
systemctl is-active leaderboard-bench-8090.service || true
```

## What The App Does

`bench_app` is the live FastAPI + vanilla JS benchmark app. It benchmarks
Text-to-SQL connector APIs across datasets, stores every run in SQLite, exposes
live progress over WebSocket, and serves the dashboard.

The repository has been trimmed to the current live `bench_app` stack plus the
small shared `leaderboard/` parser/comparator/executor package. Legacy static
dashboard and experimental `bench_v2` code were removed from git.

## Current Benchmark URL

Use this as the user-facing link:

```text
http://benchmark.144.91.85.207.nip.io:8080/
```

Ingress maps public port `8080` to host service port `8090`; host `8090` is now
  the nginx frontend container, which proxies `/api` and `/ws` to the backend
  container. Backend enqueues long benchmark work when
  `BENCH_APP_RUNNER_MODE=worker`; the worker container processes `run_jobs`.

## Main Code Map

- `bench_app/server.py`
  FastAPI routes, WebSocket progress, SPA serving, connector/dataset/run APIs,
  manual grading endpoint, raw answers endpoints, LLM judge endpoints.

- `bench_app/runner.py`
  Run orchestration. Preferred run path is now two-phase:
  collect raw connector answers first, then use an LLM judge to assign L0-L4.
  Also contains legacy execution-match fallback, rerun support, JSON builders,
  and run control.

- `bench_app/judge.py`
  OpenAI-compatible LLM judge. Converts `bench-answers/v1` into
  `bench-judged-levels/v1`; legacy semantic judge over already-scored
  `bench-result/v1` still exists.

- `bench_app/store.py`
  SQLite/Postgres store implementation. Runtime default is
  `sqlite:///bench_app/data/app.db`. Also owns the durable `run_jobs` queue
  used by `benchmark-worker`.

- `bench_app/worker.py`
  Durable benchmark worker. Claims `run_jobs` rows, heartbeats them, resumes
  unfinished runs after restart, periodically recovers stale jobs through the
  watchdog, and executes `run_task`/`rerun`/judge jobs outside the FastAPI web
  process.

- `bench_app/backup.py`
  Periodic backup sidecar process. Uses SQLite backup API for `/data/app.db`,
  archives runtime dirs, and prunes old backups by retention.

- `bench_app/connectors.py`
  Templated plain HTTP(S) request/response connector and SQL extraction modes.

- `bench_app/run_logs.py`
  Append-only per-run JSONL operational logs.

- `bench_app/datasets.py`
  Uploaded benchmark JSONL save/validate helpers.

- `bench_app/static/index.html`
  Legacy dashboard HTML kept as host fallback.

- `bench_app/static/app.js`
  Legacy vanilla dashboard JS kept as host fallback.

- `Dockerfile.frontend`
  nginx frontend image that serves the vanilla static dashboard from
  `bench_app/static` and proxies `/api`/`/ws` to the backend.

- `bench_app/data/connectors/*.yaml`
  YAML mirrors of connectors.

- `bench_app/data/runs/*.json`
  Final per-run `bench-result/v1` snapshots.

- `bench_app/data/answers/*.json`
  Raw per-run `bench-answers/v1` snapshots.

- `bench_app/data/judged/*.levels.json`
  LLM judge output `bench-judged-levels/v1`.

- `bench_app/data/logs/*.jsonl`
  Per-run append-only operational logs: run/case status transitions, attempts,
  errors and compact LLM assessment metadata.
  The same compact/redacted events are mirrored to stdout by default for
  `docker logs` via `BENCH_APP_STDOUT_RUN_LOGS=1`.

- `bench_app/data/datasets/*.jsonl`
  Benchmark JSONL files uploaded through the dashboard.

## Two-Phase Benchmark Algorithm

The current required flow:

1. Parse benchmark cases from `BENCHMARK_*.jsonl`.
2. For each case, call the connector API.
3. Extract SQL and save the raw upstream response.
4. Execute gold SQL and predicted SQL on the scoring Postgres.
5. Store a raw case row without final L0-L4 assessment.
6. Build and save `bench-answers/v1`.
7. Call the OpenAI-compatible LLM judge.
8. Judge returns exactly one final `level` in `L0..L4`, plus reason/category.
9. Merge judged levels into `run_cases`.
10. Save final `bench-result/v1` and `bench-judged-levels/v1`.

Raw answer schema deliberately excludes:

- `level`
- `matched`
- `reason`

Relevant APIs:

- `GET /api/runs/{id}/answers`
- `GET /api/runs/{id}/answers/download`
- `POST /api/runs/{id}/judge-levels`
- `GET /api/runs/{id}/judged-levels`
- `GET /api/runs/{id}/result`
- `GET /api/runs/{id}/download`
- `GET /api/runs/{id}/logs/download`

## L0 Policy

This is important.

Do not use human/manual L1 overrides to hide real L0. If a connector produced no
SQL, that is L0 unless a real rerun produces SQL and a real judge/evaluation
changes the result.

If the final dashboard has L0:

1. Inspect the case raw response/error.
2. Rerun the failed case manually or through the rerun API.
3. Merge only observed rerun results.
4. Re-run `/judge-levels` if needed.
5. Keep remaining L0 visible if the connector still returns no SQL.

Manual grade UI exists for review convenience, but it must not be used to fake
benchmark quality.

## LLM Judge Credentials

The L0-L4 judge is configured only through environment variables:

- `BENCH_APP_AUTO_JUDGE` (default: on)
- `BENCH_APP_MAX_API_CONCURRENCY` (default: `1`, max connector/API questions
  waiting on model responses across active runs)
- `BENCH_APP_MAX_IMPALA_CONCURRENCY` (default: `1`, max active Impala scoring
  SQL executions across active runs and manual SQL/DB checks)
- `BENCH_APP_GOLD_CACHE_DIR` (default: `bench_app/data/gold_cache`; gold SQL
  result cache is persisted on disk)
- `BENCH_APP_GOLD_CACHE_MEMORY_ENTRIES` (default: `0`; optional in-process LRU
  for gold SQL results, keep at `0` in low-RAM/offline deployments)
- `BENCH_APP_AUTOCONTINUE_RUNS` (default: on; after process restart, active
  `queued`/`running`/`paused`/`judging` runs are automatically continued from
  disk without rerunning already judged L0-L4 cases)
- `BENCH_APP_RUNNER_MODE` (`worker` in Docker Compose; `inline` is only for
  local tests/debug)
- `BENCH_APP_WS_SNAPSHOT_INTERVAL_S` (default in compose: `2`; backend pushes
  compact DB snapshots over WebSocket so worker progress is visible without
  browser reload)
- `BENCH_APP_CIRCUIT_BREAKER_ENABLED` (default: on; repeated external
  API/DB/LLM transport failures pause the run instead of letting it spin)
- `BENCH_APP_CIRCUIT_BREAKER_FAILURES` and per-kind overrides
  `BENCH_APP_CIRCUIT_BREAKER_API_FAILURES`,
  `BENCH_APP_CIRCUIT_BREAKER_DB_FAILURES`,
  `BENCH_APP_CIRCUIT_BREAKER_LLM_FAILURES` (default: `5`)
- `BENCH_WORKER_STALE_AFTER_S`, `BENCH_WORKER_WATCHDOG_INTERVAL_S`,
  `BENCH_WORKER_MAX_JOB_ATTEMPTS`
- `BENCH_BACKUP_ENABLED`, `BENCH_BACKUP_INTERVAL_S`, `BENCH_BACKUP_KEEP`,
  `BENCH_BACKUP_DIR`
- `LLM_BASE_URL`
- `LLM_API_KEY`
- `LLM_MODEL`
- `LLM_JUDGE_TIMEOUT`
- `LLM_JUDGE_CONCURRENCY` (default: `1`, max questions being LLM-judged across
  active runs)
- `LLM_JUDGE_MAX_RETRIES` (default `2`)
- `LLM_JUDGE_RETRY_DELAY` (default `2`, seconds)

The Run tab does not accept judge credentials or model settings. The Settings
tab shows the env-derived values read-only; `LLM_API_KEY` must only be shown as
set/not set. The backend must ignore client-supplied judge credentials.

OpenAI-compatible expected endpoint:

```text
{LLM_BASE_URL}/chat/completions
```

with bearer auth and a JSON body containing `model`, `temperature`, and
`messages`.

When searching for credentials in the filesystem, never print actual tokens.
Redact anything resembling `sk-*`, `sk-or-*`, bearer tokens, or API keys.

## Important Commands

Run tests from the codex workspace:

```bash
cd /root/leaderboard_builder_codex
/root/leaderboard_builder/.venv/bin/python -m pytest
```

Syntax check:

```bash
cd /root/leaderboard_builder_codex
/root/leaderboard_builder/.venv/bin/python -m py_compile \
  bench_app/judge.py bench_app/runner.py bench_app/server.py
```

Start/restart the live split compose stack:

```bash
cd /root/leaderboard_builder_codex
BENCH_ENV_FILE=.env.compose.live \
BENCH_DATA_DIR=./bench_app/data \
BENCH_REVIEWS_DIR=./bench_app/reviews \
BENCH_BACKUPS_DIR=./bench_app/data/backups \
BENCH_HOST_PORT=8090 \
docker compose up -d
```

Restart live safely:

```bash
cd /root/leaderboard_builder_codex
/root/leaderboard_builder/.venv/bin/python - <<'PY'
from bench_app.store import make_store
s = make_store()
print([(r['id'], r['dataset_name'], r['connector_name'], r['status'])
       for r in s.list_runs()
       if r.get('status') in ('queued', 'running', 'paused', 'judging')])
PY

BENCH_ENV_FILE=.env.compose.live \
BENCH_DATA_DIR=./bench_app/data \
BENCH_REVIEWS_DIR=./bench_app/reviews \
BENCH_BACKUPS_DIR=./bench_app/data/backups \
BENCH_HOST_PORT=8090 \
docker compose up -d --force-recreate
```

Check public service:

```bash
curl -fsS http://127.0.0.1:8090/api/datasets | head -c 200
curl -fsS --max-time 10 \
  http://benchmark.144.91.85.207.nip.io:8080/api/datasets | head -c 200
```

Build live Docker images:

```bash
cd /root/leaderboard_builder_codex
scripts/build-bench-app-docker.sh --image leaderboard-bench-app --tag latest
```

Inside an isolated contour with internal PyPI, pass:

```bash
scripts/build-bench-app-docker.sh \
  --tag offline \
  --pip-index http://pypi.local/simple \
  --pip-trusted pypi.local
```

The Docker host still needs `python:3.12-slim` and `nginx:1.27-alpine`
available locally or via an internal registry if building from source inside
the contour. The offline export bundles already-built backend and frontend
images.

Run it with persistent runtime data through split compose. Compose intentionally
uses `restart: "no"` for backend and frontend so crashes are visible instead of
being hidden by restart loops. On the live host, use the existing app data
directory:

```bash
cd /root/leaderboard_builder_codex
BENCH_ENV_FILE=.env.compose.live \
BENCH_DATA_DIR=./bench_app/data \
BENCH_REVIEWS_DIR=./bench_app/reviews \
BENCH_BACKUPS_DIR=./bench_app/data/backups \
BENCH_HOST_PORT=8090 \
docker compose up -d
```

The backend image uses `/app` as `WORKDIR`. Runtime state is not baked into the
images. Compose mounts data into backend `/data` and editable review markdown
into backend `/reviews`. The frontend image serves static files through nginx
and proxies `/api` and `/ws` to `backend:8090`. Runtime storage defaults to
SQLite: `sqlite:////data/app.db`. Main mounted paths:

- `/data/app.db`
- `/data/connectors/*.yaml`
- `/data/answers/*.json`
- `/data/judged/*.levels.json`
- `/data/runs/*.json`
- `/data/logs/*.jsonl`
- `/data/datasets/*.jsonl`
- `/reviews/*.md`

Docker stdout also receives compact per-run events by default:
`BENCH_APP_STDOUT_RUN_LOGS=1`,
`BENCH_APP_STDOUT_RUN_LOG_MAX_CHARS=20000`.

To use Postgres instead, pass `BENCH_STORE_URL=postgresql://...` via Docker env
or `bench-app.env` in the offline bundle. See `DOCKER.md`.

Build an offline deployment bundle for an isolated network with no internet:

```bash
cd /root/leaderboard_builder_codex
scripts/export-bench-app-offline.sh --tag offline
```

Copy `dist/offline/leaderboard-bench-app_offline.tar.gz` into the contour,
unpack it, and run `./run-bench-app-offline.sh`. It loads the backend and
frontend Docker images from the included `image.tar.gz`; Python dependencies
are already installed in the backend image. Docker Engine and Docker Compose
still need to be present in the target contour. The vanilla static dashboard
ships with local Chart.js and marked assets, so the browser does not need CDN
access.

Check raw-answer contract:

```bash
curl -fsS http://127.0.0.1:8090/api/runs/<run_id>/answers \
| /root/leaderboard_builder/.venv/bin/python -c \
'import json,sys; obj=json.load(sys.stdin); c=(obj.get("cases") or [{}])[0]; print(obj.get("schema"), len(obj.get("cases") or []), sorted(k for k in ("level","matched","reason") if k in c))'
```

Expected output shape:

```text
bench-answers/v1 <case_count> []
```

## Datasets

Runtime datasets in `bench_app/data/app.db`:

- `sports_events_large`
- `cybermarket_pattern_large`
- `dm_mis`
- training/smoke variants for those datasets
- default seeded `dm_mis` Impala variants:
  `dm_mis_impala_1`, `dm_mis_impala_3`, `dm_mis_impala_10`, `dm_mis_impala_all`
  (54 Impala questions from `DM_MIS запросы_v2.9 (1).docx`)

Scoring Postgres DSN pattern:

```text
postgresql://bank:bankpass@localhost:15432/<db_id>
```

For the seeded `dm_mis` Impala datasets, override the scoring connection string
with `BENCH_DM_MIS_IMPALA_DSN` (or `DM_MIS_IMPALA_DSN`). If unset, startup
reuses an existing `dm_mis` dataset DSN or falls back to the historical local
Postgres scoring DSN.

Every dataset `benchmark_path` should point to `/root/leaderboard_builder_codex`.
If paths regress, fix SQLite:

```bash
cd /root/leaderboard_builder_codex
/root/leaderboard_builder/.venv/bin/python - <<'PY'
import sqlite3
p = 'bench_app/data/app.db'
conn = sqlite3.connect(p)
cur = conn.cursor()
cur.execute("""
UPDATE datasets
SET benchmark_path = replace(
  benchmark_path,
  '/root/leaderboard_builder/',
  '/root/leaderboard_builder_codex/'
)
WHERE benchmark_path LIKE '/root/leaderboard_builder/%'
""")
conn.commit()
print(cur.rowcount)
conn.close()
PY
```

## Current Known Results Context

Earlier benchmark cleanup was done honestly:

- No `human_level=1` masking of real L0 remains.
- Static and live dashboards were regenerated/updated from actual store data.
- Remaining L0 cases were left visible where upstreams still failed/no-SQL.

At that point, known remaining real L0 included:

- `sports_events_large`: Vanna AI (mod) had 5 L0 from repeated upstream 504s.
- `dm_mis`: qwen had 11 L0 from connection errors.
- `dm_mis`: MAS_FW API had 12 L0 from 502.
- `dm_mis`: Dify had 26 L0 from no SQL in stream.
- `dm_mis`: Vanna AI (vanilla) had 7 L0.
- `cybermarket_pattern_large`: cleaned to 0 L0.

Treat these as historical context; inspect current store before reporting.

## Manual Rerun / Rejudge Workflow

For a run with questionable L0:

1. Open run cases:
   `GET /api/runs/{run_id}`
2. Inspect the case row: `raw_response`, `error`, `predicted_sql`.
3. Rerun only connector/API for one case:
   `POST /api/runs/{run_id}/rerun-case` with `{"case_id": "..."}`
4. Rerun only LLM L0-L4 judging for one case:
   `POST /api/runs/{run_id}/judge-case` with `{"case_id": "..."}`
5. Rerun unfinished/errored cases:
   `POST /api/runs/{run_id}/rerun`
6. If raw answers changed and LLM judge is configured, call:
   `POST /api/runs/{run_id}/judge-levels`
7. Download/check:
   `/api/runs/{run_id}/answers`
   `/api/runs/{run_id}/judged-levels`
   `/api/runs/{run_id}/result`

## Frontend Notes

Run tab shows the L0-L4 judge status and global concurrency limits read-only
from `/api/settings`. Runtime judge settings live in env only; do not add
browser inputs for judge credentials, model, timeout, concurrency, max retries,
or retry delay. Connector/API concurrency is env-limited by
`BENCH_APP_MAX_API_CONCURRENCY`; the backend clamps requested run concurrency.

Progress UI treats `judging` as active, renders per-case API/judge stages, and
has per-case buttons for API rerun and LLM rejudge in expanded question cards.

Static files are served by the nginx frontend container. JS/HTML edits require
rebuilding/recreating `leaderboard-bench-frontend`; Python edits require
rebuilding/recreating `leaderboard-bench-app`.

## Tests Added For Two-Phase Pipeline

Unit tests in `bench_app/tests/test_unit.py` verify:

- `build_answers()` emits `bench-answers/v1`
- raw answer cases exclude `level`, `matched`, `reason`
- `apply_judged_levels()` merges final levels into `run_cases`
- `_normalise_level()` accepts `L0` and numeric strings

Latest verified test result from codex workspace:

```text
27 passed
```

## Do Not Do

- Do not run destructive git commands such as `git reset --hard`.
- Do not revert user/manual benchmark data unless explicitly requested.
- Do not edit old `/root/leaderboard_builder` as the primary workspace.
- Do not print secrets.
- Do not fake score improvements via manual/human grading.
