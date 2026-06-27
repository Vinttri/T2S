# bench_app — Text-to-SQL benchmarking application

A self-contained **app** (FastAPI backend + vanilla-JS SPA) that benchmarks
Text-to-SQL APIs: you describe each model as a **connector**, point it at a
**dataset** (questions + gold SQL + a scoring Postgres), trigger **runs**, watch
live progress, grade each case **L0–L4**, compare models on a **leaderboard**,
re-run failed questions, and pull results as JSON.

It replaced the old static baked dashboard. Single public URL:
**http://benchmark.144.91.85.207.nip.io:8080/**

---

## Run it

```bash
cd /root/leaderboard_builder_codex
scripts/run-benchmark-host.sh
```

- Host process on **:8090**, exposed publicly on **:8080** by the current host
  routing/proxy setup. The portable deployment path for this repo is plain
  Docker, not Kubernetes. See `DOCKER.md` and `DEPLOY_DOCKER_SIMPLE.md`.
- Host launch reads `.env` (or `BENCH_ENV_FILE`) before starting uvicorn; Docker
  uses the compose `env_file`.
- Storage: `BENCH_STORE_URL` (default `sqlite:///bench_app/data/app.db`; also supports
  `postgresql://…`).
- Tests: `.venv/bin/python -m pytest` (config in `pytest.ini`).

> ⚠️ Runs are in-process `asyncio` tasks. **Restarting uvicorn stops in-flight work**:
> persisted partial cases/logs remain on disk, and startup marks unfinished runs as `stopped`.
> Continue them with the run's “↻ перепрогнать невыполненные”.

---

## Concepts

### Connector (a model / “ручка”)
How to call one Text-to-SQL API. A **templated plain HTTP(S) request/response** + how to pull SQL out of the
response. Connector transports are deliberately limited to one synchronous request and one response:
no WebSocket, SSE, or streaming responses. Fields: `name`, `method`, `url`, `headers`, `body_template`, `sql_extract`,
`default_dialect` (= the SQL dialect / DB type it targets), `db_id` (the **one** DB it's
bound to), `timeout`, `max_attempts`, `description` (Markdown → shows in Reviews).

- **Placeholders** in `url`/`headers`/`body_template`: `{{question}}`, `{{dialect}}`.
  `{{database}}` is **not** a runtime placeholder — `db_id` is baked into the body as a
  literal on save (one connector per DB).
- **SQL extraction** (`sql_extract`): `mode: json` (+ dotted `field`, e.g. `sql` or
  `data.0.sql`; `deep: true` to search the field anywhere in the tree, taking the last
  match), `mode: raw` (whole response), `mode: regex` (+ `pattern`, group 1).
- If an upstream only supports streaming/SSE/WebSocket, wrap it outside the app and expose a
  sync HTTP endpoint that returns one response containing SQL.
- Connectors are **mirrored to YAML** under `data/connectors/*.yaml` (see Storage). You can
  hand-author/edit/version a connector as a YAML file.

### Dataset (a benchmark)
A `BENCHMARK_*.jsonl` (one JSON object per question with gold SQL + conditions) + a scoring DB `dsn` + `db_id`
+ `db_type` (postgres/impala/…). `dsn` is the backend connection string used to execute
gold SQL and model SQL for result comparison. Parsed by
`leaderboard.benchmark.parse_benchmark_file`.

Fresh stores seed four default Impala datasets for `dm_mis`:
`dm_mis_impala_1`, `dm_mis_impala_3`, `dm_mis_impala_10`, and `dm_mis_impala_all` (54 Impala
questions from `DM_MIS запросы_v2.9 (1).docx`). Override their scoring connection string with
`BENCH_DM_MIS_IMPALA_DSN` (or `DM_MIS_IMPALA_DSN`).

### Run
One connector executed over one dataset. Default flow is two-phase:

1. collect raw connector answers and execution evidence into `bench-answers/v1` without any
   L0–L4 fields;
2. call an OpenAI-compatible LLM judge, merge the final L0–L4 levels into the run, and write
   `bench-result/v1` plus `bench-judged-levels/v1`.

The old direct execution-match scorer (`leaderboard.comparator.eval_level`) is still available
for legacy/manual runs when `auto_judge=false`. Statuses:
`queued`/`running`/`paused`/`judging`/`done`/`stopped`/`error`.

**Levels:** L4 exact match · L3 executed but rows/shape differ · L2 gold SQL failed ·
L1 predicted SQL didn't execute · L0 no SQL.

### Compatibility guard
A run is **blocked (HTTP 400)** if the connector's dialect ≠ dataset's `db_type`, or if the
connector's `db_id` ≠ dataset's `db_id`. The Run tab shows a green/red note and disables the
button.

---

## SPA tabs

- **🏆 Лидерборд** — per-benchmark comparison (latest done run per model). Summary cards +
  Chart.js bar (accuracy) & scatter (accuracy vs **median** API time) + task×model table with
  L-badges. Model **filter pills**, **per-model revision `<select>`** (default latest), a
  **⬇ JSON** export, and **«подробнее»** per task → gold SQL/result + every model's SQL/result.
- **🔌 Коннекторы** — build/edit/delete connectors; **Тест** (calls the model with the
  benchmark's first question, auto-filled by `db_id`) + **Превью запроса** (no send) + full raw
  response. Switching connector clears stale test output.
- **▶️ Запуск** — pick dataset + connector (compatibility note), launch; lists datasets &
  connectors (with dialect/db_id badges).
- **📈 Тесты** — all runs as collapsible cards, grouped **⏳ выполняется / ✅ завершено**
  (groups collapsible). Per run: live progress bar, level pills, **⏸ pause / ▶ resume / ⏹ stop**,
  **↻ перепрогнать невыполненные**. Per case: click → metadata (question, gold/predicted
  SQL+results), **↻** to re-run that one question (shows a spinning “перезапуск…” marker).
- **📊 Результаты** — pick dataset + revision (latest default) → per-case detail + **⬇ JSON**.
- **📝 Обзоры решений** — solution briefs (Markdown), editable in place.

A fixed top-right badge shows **WebSocket status + last-update time**. The active tab and theme
(light/dark toggle) persist in `localStorage`.

---

## Live progress over WebSocket (no polling)

`GET /ws/progress` sends `{type:"snapshot", runs:[…]}` on connect, then pushes `{type:"run",…}`
(start / each case / done) and `{type:"case",…}` events. A `{type:"ping"}` heartbeat every 30s
keeps the nginx ingress (300s read-timeout) from dropping the socket. The frontend keeps
`runsMap`/`casesMap` in memory and re-renders on each event; auto-reconnects on close.

---

## Raw Answers JSON (`bench-answers/v1`)

During the first phase each run is written to `data/answers/<run_id>.json` and served by
`GET /api/runs/{id}/answers` / `…/answers/download`:

```jsonc
{
  "schema": "bench-answers/v1",
  "run_id": "...",
  "benchmark": { "dataset_id", "name", "db_id", "db_type" },
  "model": { "name", "connector_id", "dialect", "endpoint" },
  "cases": [ { "case_id","difficulty","question","gold_sql","predicted_sql",
               "error","elapsed_s","attempts","gold_result","agent_result","raw_response" } ]
}
```

There is deliberately no `level`, `matched`, or `reason` in this document.

## Results JSON (`bench-result/v1`)

Each run is written to `data/runs/<run_id>.json` on completion and served by
`GET /api/runs/{id}/result` / `…/download`:

```jsonc
{
  "schema": "bench-result/v1",
  "run_id": "...", "status": "done", "created_at": ..., "started_at": ..., "finished_at": ...,
  "benchmark": { "dataset_id", "name", "db_id", "db_type" },
  "model":     { "name", "connector_id", "dialect", "endpoint" },
  "summary":   { "accuracy", "passed", "total", "levels": {"L0".."L4"},
                 "elapsed_total_s", "median_elapsed_s" },
  "cases": [ { "case_id","difficulty","question","gold_sql","predicted_sql","level","matched",
               "reason","error","elapsed_s","attempts","gold_result","agent_result" } ]
}
```

## LLM level judge (`bench-judged-levels/v1`)

New runs use the LLM judge by default. Judge settings are env-only:
`BENCH_APP_AUTO_JUDGE`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`,
`LLM_JUDGE_TIMEOUT`, `LLM_JUDGE_CONCURRENCY`, `LLM_JUDGE_MAX_RETRIES`, and
`LLM_JUDGE_RETRY_DELAY`. The Run tab does not accept judge credentials or model
settings. If auto-judge is enabled and env credentials are missing, the backend
returns 400 before starting the run.

Connector/API wait concurrency is also env-limited through
`BENCH_APP_MAX_API_CONCURRENCY` (default `1`). The backend clamps requested run
concurrency to that value; the Settings tab shows the effective limit read-only.

Per-run JSONL operational events are mirrored to stdout by default for
`docker logs`: `BENCH_APP_STDOUT_RUN_LOGS=1`,
`BENCH_APP_STDOUT_RUN_LOG_MAX_CHARS=20000`. These stdout records use the same
redaction as persisted run logs.

Manual scoring of an already collected run:
`POST /api/runs/{id}/judge-levels`; it uses the same env-only judge settings.
Fetch the saved judge output via `GET /api/runs/{id}/judged-levels`.
Manual per-case operations are also available:
`POST /api/runs/{id}/rerun-case` re-runs connector/API for one question, and
`POST /api/runs/{id}/judge-case` re-runs only the LLM L0-L4 assessment.

## Legacy semantic judge (`bench-judged/v1`, optional)

`POST /api/runs/{id}/judge` (background) → an LLM grades each case on the L1–L4 scale and adds
a per-case `assessment {assessed_level, error_category, explanation, agrees_with_auto}` +
`judge_summary`. Fetch via `GET /api/runs/{id}/judged`. Needs an OpenAI-compatible gateway in
env: `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` (else 400).

## HTTP API

| Method · path | What |
|---|---|
| `GET/POST /api/connectors`, `DELETE /api/connectors/{id}` | list / upsert / delete connectors (POST mirrors to YAML) |
| `POST /api/connectors/preview` · `/test` | render request (no send) · call the model, return extracted SQL + raw response |
| `GET /api/first-question?db_id=` | first question of that DB's benchmark (test autofill) |
| `GET/POST /api/datasets`, `DELETE /api/datasets/{id}` | list / upsert / delete datasets |
| `POST /api/datasets/upload` | save uploaded benchmark JSONL to `data/datasets/` and register it |
| `GET /api/settings` | read-only runtime settings from env; secrets are redacted |
| `POST /api/settings/llm-test` | real env-configured LLM connectivity check; secrets are redacted |
| `POST /api/runs` | trigger a run; default env `BENCH_APP_AUTO_JUDGE=true` requires env judge credentials |
| `GET /api/runs` · `GET /api/runs/{id}` · `DELETE /api/runs/{id}` | list / one (with cases) / delete |
| `POST /api/runs/{id}/pause` · `/resume` · `/stop` | control a running run |
| `POST /api/runs/{id}/rerun` · `/rerun-case` {case_id} · `/judge-case` {case_id} | re-run unfinished/errored cases · one API case · one LLM judge case |
| `GET /api/runs/{id}/answers` · `/answers/download` | raw connector answers JSON, no L0–L4 |
| `GET /api/runs/{id}/result` · `/download` | canonical results JSON |
| `GET /api/runs/{id}/logs` · `/logs/download` | append-only per-run JSONL operational log |
| `POST /api/runs/{id}/judge-levels` · `GET /api/runs/{id}/judged-levels` | final L0–L4 judge using env settings |
| `POST /api/runs/{id}/judge` · `GET /api/runs/{id}/judged` | legacy semantic judge (needs LLM_* env) |
| `GET /api/results?dataset_id=&run_id=` | one revision + the revision list |
| `GET /api/leaderboard` | models × benchmarks matrix |
| `GET /api/compare?dataset_id=` | per-benchmark: tasks + participants (latest run + `revisions`) |
| `GET /api/case?dataset_id=&case_id=&run_ids=` | per-case detail across models (respects selected revisions) |
| `WS /ws/progress` | live run/case events + heartbeat |
| `GET /api/reviews` · `POST /api/reviews/save` | solution briefs (Markdown) |

---

## Storage

`store.py` — `SqlStore` base (+ `SQLiteStore` default, `PostgresStore`), factory `make_store()`.
Tables: `connectors`, `datasets`, `runs`, `run_cases`. Connectors also mirrored to readable
**YAML** (`connectors_yaml.py`) under `data/connectors/*.yaml`: SQLite is the runtime source, but
save/delete sync the YAML and startup imports YAML (upsert) — so connectors are hand-editable &
versionable. Per-run JSON snapshots live in `data/runs/`; raw answers in `data/answers/`;
judged JSON in `data/judged/`; append-only run logs in `data/logs/*.jsonl`; uploaded benchmark
JSONL files in `data/datasets/`.

## Files

| Path | Role |
|---|---|
| `server.py` | FastAPI: routes + serves the SPA + the compatibility/bake helpers |
| `runner.py` | two-phase `run_task`, raw `build_answers`, final `build_result`, `rerun`; RUN_CONTROL + `_gate` |
| `connectors.py` | `TemplatedConnector`, `extract_sql` (json/raw/regex + deep), `preview_request` |
| `connectors_yaml.py` | connector ⇄ YAML mirror (export/load) |
| `run_logs.py` | per-run append-only JSONL logs |
| `datasets.py` | uploaded benchmark JSONL save/validate helpers |
| `store.py` | pluggable Store (SQLite/Postgres) |
| `judge.py` | LLM level judge (`bench-judged-levels/v1`) + legacy semantic judge (`bench-judged/v1`) |
| `bus.py` | in-process async pub/sub for the WebSocket |
| `static/{index.html,app.js}` | the SPA |
| `reviews/*.md` | solution briefs shown in the Обзоры tab |
| `tests/` | pytest (unit: templating/extraction/scoring/store; e2e: full run vs mock API + real PG) |

## Conventions / gotchas

- Re-saving a connector fetched from `GET` must coerce `description`/`db_id` `None`→`""` (Pydantic
  str fields → 422 otherwise).
- The leaderboard groups by **connector_name**, so per-DB connector copies share the model name
  (differ by `db_id`) to keep the cross-DB comparison intact.
- Deleting a connector keeps its past runs (runs key on connector_name), so leaderboard history
  survives.
- A connector wired to DB *A* run against a dataset for DB *B* scores ~0 — that's a real, visible
  result, not a bug (the compatibility guard now prevents the mismatch by default).
