# bench_app — guide for LLM agents

Text-to-SQL benchmarking app: FastAPI backend (`server.py`) + vanilla-JS SPA
(`static/`). Benchmarks model APIs (connectors) over datasets, scores L0–L4, live
progress via WebSocket. Public URL `http://benchmark.144.91.85.207.nip.io:8080/`.
Full human/dev docs in `README.md`; this file is the fast map for agents.
For full current context, read `/root/leaderboard_builder_codex/CODEX_CONTEXT.md`.

## Run / iterate
- Start: `cd /root/leaderboard_builder_codex && /root/leaderboard_builder/.venv/bin/python -m uvicorn bench_app.server:app --host 0.0.0.0 --port 8090` (host :8090 → ingress :8080).
- **static (index.html/app.js) is served live** — no restart. Restart uvicorn ONLY after `.py` changes. Kill with `fuser -k 8090/tcp`.
- Tests: `/root/leaderboard_builder/.venv/bin/python -m pytest` (pytest.ini). Always run after editing runner/store/connectors/judge.
- Store: `BENCH_STORE_URL` (default `sqlite:///bench_app/data/app.db`).

## File map (where to change X)
- `server.py` — all HTTP/WS routes, SPA serving, helpers `_bake_db` (db_id→body literal), `_norm_dialect` (compat). Pydantic models: `Connector`, `Dataset`, `TestReq`.
- `runner.py` — `run_task` (two-phase full run) · `_collect_case` (raw answer collection) · `rerun` (re-run failed/one case) · `_eval_case` (legacy direct scoring) · `build_answers` (`bench-answers/v1`) · `apply_judged_levels` · `build_result` (canonical JSON) · `RUN_CONTROL`+`_gate`+`set_control` (pause/stop) · `_emit_run` (WS).
- `connectors.py` — `TemplatedConnector.generate`, `extract_sql` (modes json|raw|regex + `deep`), `preview_request`. `PLACEHOLDER_RE` = question|dialect only.
- `connectors_yaml.py` — connector ⇄ `data/connectors/*.yaml` mirror.
- `run_logs.py` — append-only per-run JSONL logs in `data/logs/*.jsonl`.
- `datasets.py` — uploaded benchmark JSONL storage/validation in `data/datasets/*.jsonl`.
- `store.py` — `SqlStore`/`SQLiteStore`/`PostgresStore`, `make_store()`. Tables connectors/datasets/runs/run_cases. `replace_case` (rerun), `delete_run`.
- `judge.py` — LLM level judge, `bench-answers/v1` → `bench-judged-levels/v1`; legacy semantic `bench-judged/v1`; `llm_config()` reads LLM_BASE_URL/API_KEY/MODEL or explicit overrides.
- `bus.py` — async pub/sub for `/ws/progress`.
- `static/app.js` — SPA logic (tabs, leaderboard `lbState`, progress `runsMap`/`casesMap`, WS).
- `reviews/NN_*.md` — Обзоры tab content (served by `/api/reviews`, file-based, live).

## Key contracts / invariants
- **Connector**: one plain HTTP(S) request/response only; no WebSocket/SSE/streaming connector transports. `{{question}}`/`{{dialect}}` substituted; `{{database}}` is baked to a literal from `db_id` at save (`_bake_db`) — NOT a runtime param. `sql_extract={mode, field?, pattern?, deep?}`.
- **One connector per DB**: `db_id` binds it; run blocked (400) if connector.db_id≠dataset.db_id OR dialect≠db_type. Per-DB copies share `name` (so leaderboard groups by name correctly).
- **Two-phase scoring**: default run collects raw answers without `level/matched/reason`, then LLM judge assigns final L0-L4. Legacy execution scorer remains only as fallback when `auto_judge=false`.
- **Levels**: L4 match · L3 executed/rows differ · L2 gold failed · L1 not executable · L0 no SQL.
- **WS** `/ws/progress`: `snapshot` then `run`/`case` events + `ping` (30s heartbeat). Frontend keeps state in `runsMap`/`casesMap`, never polls.
- **Results**: `bench-result/v1` per run in `data/runs/<id>.json`; `build_result` rebuilds on demand.
- **Raw answers**: `bench-answers/v1` in `data/answers/<id>.json`; no L0-L4 fields.
- **Run logs**: append-only JSONL per run in `data/logs/<id>.jsonl`; log compact stage/attempt/error metadata, not secrets.
- All model-calling paths are `async` (httpx.AsyncClient). Sync CRUD endpoints run in FastAPI's threadpool.

## Gotchas
- Re-saving a connector dict from `GET` → coerce `description`/`db_id` `None`→`""` or POST 422 (Pydantic str).
- **Runs are in-process asyncio tasks → a uvicorn restart stops in-flight work**; partial cases/logs stay on disk and startup marks unfinished runs `stopped`; continue via `POST /api/runs/{id}/rerun`.
- `parse_benchmark_file(path)` takes a PATH and supports JSONL as the primary format.
- Connector delete keeps past runs (keyed by connector_name) → leaderboard history survives.
- Scoring Postgres: `postgresql://bank:bankpass@localhost:15432/<db_id>` (sports_events_large, cybermarket_pattern_large, dm_mis).
- Do not mask real L0 with manual/human overrides. If L0 remains after real rerun, leave it visible.

## Extend
- New connector type → keep it as data: one HTTP(S) endpoint returning one response. If an upstream only supports WebSocket/SSE/streaming, wrap it outside this app and expose a sync HTTP endpoint returning SQL.
- New dataset → upload `.jsonl` with `POST /api/datasets/upload` or write `BENCHMARK_*.jsonl` and `POST /api/datasets {name, benchmark_path, db_id, dsn, db_type}`; load the DB into the scoring Postgres.
- New API route → add to server.py (sync `def` for fast DB reads, `async def` if it calls a model/streams).
