# CLAUDE.md — T2S (Text-to-SQL AI Platform)

T2S turns natural-language questions into SQL over a relational database, using a
FalkorDB **graph** of the schema (tables, columns, PK/FK, descriptions, vector
embeddings) for retrieval, a multi-agent generation pipeline, a deterministic
**SQL gate**, and an execute→heal loop. It runs **fully locally on macOS via
Docker** (Apple Silicon / arm64).

> Fork lineage: T2S is a rebranded, de-banked, localized fork of an upstream
> graph-Text2SQL engine. The underlying graph engine is **FalkorDB** (kept as a
> functional dependency). Licensed **AGPL-3.0-or-later** (see `LICENSE`).

---

## Quick start

```bash
./install.sh                 # zero-arg: builds + runs EVERYTHING, indexes, loads rules
# open http://localhost:5000
```
Optional flags (everything has a default — no flag is required):
- `--port N` — UI/API host port (default `5000`).
- `--llm-api-base URL` / `--llm-api-key KEY` / `--llm-model M` — the **external
  completion LLM**. Defaults to LM Studio on the host: `http://host.docker.internal:1234/v1`,
  model `openai/qwen`. `localhost`/`127.0.0.1` you pass are auto-rewritten to
  `host.docker.internal` (the app is containerized).

`install.sh` is the single entry point: it writes `.env`, builds the image,
starts Postgres + embeddings + T2S, **auto-indexes** both demo DBs into graphs,
and loads their generic rules + business knowledge.

---

## Architecture (3 parts, one `docker compose`)

1. **`t2s` app container** (`Dockerfile`, base `falkordb/falkordb:v4.18.10`) — a
   single container running:
   - **FastAPI** backend (`api/`) + the **built React frontend** (`app/` → served by FastAPI). Port **5000**.
   - **Embedded FalkorDB** (Redis + graph module) — holds the schema graph,
     vector indexes, and rules/knowledge. Data dir `/var/lib/falkordb/data` (volume `t2s_graph_data`).
   - The app calls the **`embeddings`** sibling container over HTTP
     (`EMBEDDING_API_BASE=http://embeddings:7997/v1`); it loads no torch itself.
   - Startup order (`start.sh`): launch FalkorDB → `exec uvicorn`.
2. **`embeddings` container** (separate sibling, `Dockerfile.embeddings`) — serves
   **Qwen/Qwen3-Embedding-0.6B** via `embedding_server.py` (OpenAI-compatible, port
   **7997**, device auto CUDA→MPS→CPU; model baked in). Model + settings are **fixed /
   not user-configurable**. Reached at `http://embeddings:7997/v1` over `t2s-net`.
   (Docker on macOS can't pass the Mac GPU to a Linux container → CPU on Mac; a
   Linux+NVIDIA host auto-uses CUDA via the compose `deploy.devices` block.)
3. **`postgres` container** (separate / "external" DB) — the source-of-truth
   relational DB, **pre-seeded** on first boot from `db-init/01_sports_events_large.sql`
   (the **Sports** demo/test DB: 61 tables, 28,536 rows, F1-style motorsport).
   Volume `t2s_pg_data`. Reached by the app as `postgresql://t2s:t2s@postgres:5432/sports_events_large`.

Embeddings run in their **own container**; only the **completion LLM is external**.

---

## Repo layout

```
api/                FastAPI backend
  app_factory.py    app + MCP + CSRF/auth wiring (AUTH_DISABLED=true → dev user, CSRF skipped)
  routes/           database.py (POST /database, POST /database/enrich), graphs.py
                    (/graphs/{g}/sql, /graphs/sql, /graphs/{g}, user-rules, knowledge), settings.py, auth.py, tokens.py
  core/             text2sql.py (pipeline), schema_loader.py, pipeline.py, *_models.py
  agents/           blackboard + analysis + relevancy + healer + sql_semantic_validator +
                    schema_topup + schema_enrichment_agent.py (NEW, load-time)
  loaders/          postgres_loader, impala_loader, snowflake_loader, yaml_loader,
                    graph_loader (DB→graph writer), graph_merge.py (shared merge engine),
                    agent_loader.py (NEW), doc_text.py (NEW, arbitrary-doc text extract)
  sql_utils/        sql_gate.py (deterministic sqlglot AST gate)
  config.py, extensions.py, tls.py, graph.py (FalkorDB read/write, rules/knowledge)
t2s/                importable Python SDK (class T2SClient)
app/                React/Vite/Tailwind/shadcn frontend (theme = T2S blue #2176B6 + green #4FA84E)
  public/img/t2s-logo.png, public/favicon.ico
business-rules/     user_rules.md (generic SQL/semantic rules), business_knowledge.md (generic)
db-init/            01_sports_events_large.sql (Sports seed, auto-loaded by Postgres)
docker-compose.yml  postgres + embeddings + t2s (network: t2s-net)
Dockerfile          app image (frontend build + backend + FalkorDB)
Dockerfile.embeddings  embedding service image (torch + sentence-transformers + Qwen3-Embedding model)
start.sh            app container entrypoint (FalkorDB + uvicorn)
install.sh          one-command installer (zero-arg)
BENCHMARK.md        questions + gold SQL + expected values (self-verify, both DBs)
.t2s-build-notes.md internal build decisions (gitignored)
```

---

## Schema loading — DB first, then agent enrichment (replaces YAML)

- **Initial graph is ALWAYS built from the database.** `POST /database {"url": ...}`
  → `schema_loader.load_database` → a DB loader → `graph_loader.load_to_graph`
  writes `:Database`, `:Table` (with FK JSON), `:Column` (type, nullable, key_type,
  sample_values, description, vector embedding), and `:REFERENCES` (FK) edges.
- **Agent-loader (enrichment)** — `POST /database/enrich` with **arbitrary uploaded
  documents** (.md/.txt/.csv/.json/.pdf/.docx/.xlsx/…) + optional rules/knowledge.
  `api/loaders/agent_loader.py` snapshots the live graph, extracts doc text
  (`doc_text.py`), and an LLM (`agents/schema_enrichment_agent.py`) proposes
  descriptions / PK / NOT NULL / FK links. A deterministic gate keeps only items
  that reference real schema and only **fills gaps** (never overrides a DB-asserted
  type/PK/NOT-NULL). Applied via the shared `graph_merge.merge_graph_data`.
  (The old YAML route `POST /database/yaml` remains as a fallback; no bank YAMLs ship.)

---

## Key API endpoints (same as upstream)

- `POST /database` — index a DB into the graph (streamed; `|||FALKORDB_MESSAGE_BOUNDARY|||`-delimited).
- `POST /database/enrich` — agent enrichment from documents (streamed).
- `POST /graphs/{graph_id}/sql` and `POST /graphs/sql` — **non-streaming**, returns
  one JSON `{sql, is_valid, confidence, explanation, ...}`. **Use these to get clean SQL.**
- `POST /graphs/{graph_id}` — streaming chat/query (SQL + answer).
- `PUT /graphs/{graph_id}/user-rules` `{user_rules}` · `PUT /graphs/{graph_id}/knowledge` `{knowledge}`.
- `GET /graphs` · `GET /graphs/{graph_id}/data` (schema).

Graph naming: with `AUTH_DISABLED=true` the user is `dev`; the FalkorDB graph is
internally `dev_<dbname>` (e.g. `dev_sports_events_large`), but the **API uses the
bare db name** (`sports_events_large`) as `{graph_id}`.

---

## Configuration

- **Embeddings**: fixed, in their own `embeddings` container (no `.env`/UI knobs). Never set
  `EMBEDDING_DIMENSION` — Qwen3-Embedding is served at native dim; sending a
  `dimensions` param to a non-MRL endpoint 400s and yields an empty graph.
- **Completion / memory LLM**: external, via `.env` (`COMPLETION_*`, `MEMORY_*`)
  written by `install.sh` from the `--llm-*` flags. `LLM_TIMEOUT_SECONDS=300`
  (reasoning models are slow). LM Studio (host `:1234`) must be running for queries.
- `.env` is generated by `install.sh` (FASTAPI_SECRET_KEY, AUTH_DISABLED=true, LLM).

---

## Verifying

`BENCHMARK.md` lists every question, its gold SQL, and the expected value for both
shipped DBs. Verify head-less, e.g.:
`curl -s -X POST http://localhost:5050/graphs/<db>/sql -H 'Content-Type: application/json' -d '{"question":"…","use_knowledge":true}'`

---

## Dev notes / conventions

- Build image: `docker compose build t2s` (first build is large — torch + the embed model).
- Frontend dev: `cd app && npm install && npm run build` (Vite). Theme tokens live in
  `app/src/index.css` (CSS vars) → never hardcode `purple-*`; use `bg-primary`/`ring-ring`/etc.
- Python deps: `uv` (`pyproject.toml` + `uv.lock`; Dockerfile uses `uv sync --frozen`).
  If you change deps or the package name, run `uv lock`.
- SDK: `from t2s import T2SClient`.
- Tests: `Makefile` targets / `pytest tests/`.
- **Keep `falkordb`** (the graph engine) and the **AGPL LICENSE + attribution**.
  There must be **no "QueryWeaver"/"weaver" and no bank/finance** branding anywhere —
  the codebase has been scrubbed; keep it that way.
- The deterministic SQL gate is sqlglot-AST based (`api/sql_utils/sql_gate.py`) — add
  structural checks there, never regex over SQL strings; domain guidance goes in
  `business-rules/user_rules.md` (general concepts only, no concrete table/column names).

## Gotchas

- First `./install.sh` build is multi-GB / several minutes (torch + Qwen model baked in).
- Completion requires the external LLM to be reachable; embeddings/indexing do not.
- `host.docker.internal` is how containers reach host services (LM Studio); compose
  sets `extra_hosts` for it.
