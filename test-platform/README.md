# Text-to-SQL Benchmark App

Current live benchmark application: FastAPI backend + vanilla JS dashboard for
running Text-to-SQL connectors against benchmark JSONL files and scoring the
answers with an env-configured LLM judge.

Authoritative coding context for future LLM agents is in `CODEX_CONTEXT.md`.

## What Is Included

- `bench_app/` - current live app, API, runner, store, dashboard, tests.
- `leaderboard/` - shared benchmark parser, comparator, and Postgres executor
  used by `bench_app`.
- `BENCHMARK_*.jsonl` - benchmark cases used by datasets.
- `Dockerfile`, `docker/`, `scripts/build-bench-app-docker.sh`,
  `scripts/export-bench-app-offline.sh` - Docker and offline deployment path.
- `DOCKER.md`, `DEPLOY_DOCKER_SIMPLE.md` - deployment notes.

Runtime state is intentionally not tracked: SQLite DBs, connector YAML with
secrets, raw run answers/results, `dist/`, `output/`, `.env`, and image archives.

## Run Locally

This workspace currently uses the Python environment from the sibling directory:

```bash
cd /root/leaderboard_builder_codex
/root/leaderboard_builder/.venv/bin/python -m uvicorn bench_app.server:app \
  --host 0.0.0.0 --port 8090
```

Open:

```text
http://127.0.0.1:8090/
```

## Test

```bash
cd /root/leaderboard_builder_codex
/root/leaderboard_builder/.venv/bin/python -m pytest
npm --prefix frontend run build
```

## Docker

Build:

```bash
scripts/build-bench-app-docker.sh --image leaderboard-bench-app --tag latest
```

Run:

```bash
cp .env.example .env
# edit .env, then:
mkdir -p data reviews
docker compose up -d
```

For host folders:

```bash
mkdir -p /opt/leaderboard/data /opt/leaderboard/reviews
# set BENCH_DATA_DIR=/opt/leaderboard/data and
# BENCH_REVIEWS_DIR=/opt/leaderboard/reviews in .env
docker compose up -d
```

Export an offline bundle:

```bash
scripts/export-bench-app-offline.sh --tag offline
```

## LLM Judge

The L0-L4 judge is configured only through env:

```env
# .env
BENCH_APP_AUTO_JUDGE=1
LLM_BASE_URL=http://llm-gateway.local/v1
LLM_API_KEY=replace-me
LLM_MODEL=llmgateway/free
LLM_JUDGE_TIMEOUT=60
BENCH_APP_MAX_API_CONCURRENCY=1
LLM_JUDGE_CONCURRENCY=1
LLM_JUDGE_MAX_RETRIES=2
LLM_JUDGE_RETRY_DELAY=2
```

The UI shows these settings read-only on the Settings tab.

Per-run JSONL events are also mirrored to stdout by default, so `docker logs`
contains benchmark progress and attempt details. Tune with
`BENCH_APP_STDOUT_RUN_LOGS` and `BENCH_APP_STDOUT_RUN_LOG_MAX_CHARS`.
