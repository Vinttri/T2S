# T2S — Quick Start

One command builds the whole stack, indexes a demo database, and loads its rules.

```bash
./install.sh
# → open http://localhost:5050
```

---

## 1. Prerequisites

- **Docker Desktop** (Apple Silicon / arm64 supported). `docker compose` v2.
- A **completion LLM** with an OpenAI-compatible API. The default expects
  **LM Studio** running on the host at `http://localhost:1234` with a chat model
  loaded (default model id `openai/gemma-4-12b-it-qat`). Any OpenAI-compatible
  endpoint works — see `--llm-*` flags below.
- Embeddings need **no** setup: they run in a bundled container (or any endpoint
  you point `EMBEDDING_API_BASE` at).
- ~No GPU required (CPU works; GPU is auto-used if present). First build is large
  (downloads torch + the embedding model).

> macOS note: the default UI port is **5050**, because macOS AirPlay Receiver
> occupies port 5000.

---

## 2. `install.sh` — all parameters

```text
./install.sh [--port N]
             [--llm-api-base URL] [--llm-api-key KEY] [--llm-model MODEL]
             [--embedding-api-base URL] [--embedding-api-key KEY]
             [--embedding-model MODEL] [--embedding-dimension N]
             [-h | --help]
```

| Flag | Default | What it does |
|---|---|---|
| `--port N` | `5050` | Host port for the T2S UI/API. |
| `--llm-api-base URL` | `http://host.docker.internal:1234/v1` | Base URL of the **completion** LLM (OpenAI-compatible). `localhost`/`127.0.0.1` are auto-rewritten to `host.docker.internal` (the app is containerized) and `/v1` is appended if missing. |
| `--llm-api-key KEY` | `lm-studio` (placeholder) | API key for the completion LLM. |
| `--llm-model MODEL` | `openai/gemma-4-12b-it-qat` | Model id used for generation (and memory). |
| `--embedding-api-base URL` | _(unset ⇒ **bundled CPU container**)_ | Use an external OpenAI-compatible **embedding** endpoint instead of the bundled one. Same `localhost`→`host.docker.internal` + `/v1` rewriting. |
| `--embedding-api-key KEY` | `local` | API key for the embedding endpoint. |
| `--embedding-model MODEL` | `openai/qwen3-embedding` | Embedding model id (only with `--embedding-api-base`). |
| `--embedding-dimension N` | `1024` | Vector size the graph is indexed with — must match the embedding model (change ⇒ re-index). |
| `-h`, `--help` | — | Print usage and exit. |

> **Embeddings need zero setup.** With no `--embedding-*` flag, T2S serves
> embeddings from the bundled **`t2s-embeddings`** container (Qwen3-Embedding-0.6B,
> CPU by default, GPU auto-used if present). Pass `--embedding-api-base` only to use
> a faster/hosted endpoint.

Examples:
```bash
./install.sh                                            # everything, defaults
./install.sh --port 8080                                # serve on :8080
./install.sh --llm-api-base http://127.0.0.1:1234 \     # local LM Studio (auto-rewritten)
             --llm-api-key sk-... --llm-model openai/qwen2.5-coder
./install.sh --llm-api-base https://api.example.com/v1 \ # a remote OpenAI-compatible API
             --llm-api-key sk-... --llm-model gpt-4o-mini
```

---

## 3. What `install.sh` does (in order)

1. **Writes `.env`** — a fresh `FASTAPI_SECRET_KEY` and the completion/memory LLM
   settings from your flags. (`AUTH_DISABLED=true` for simple local indexing.)
   Embedding vars are written **only if** you passed `--embedding-*`; otherwise the
   stack defaults to the bundled CPU embedding container.
2. **Builds + starts** three containers: `postgres` (pre-seeded with **both** demo
   DBs — `sports_events_large` + `cybermarket_pattern_large`), `embeddings`
   (Qwen3-Embedding-0.6B, auto GPU/CPU — used by default, no setup), and `t2s`
   (FalkorDB + backend + UI).
3. **Waits** until `t2s-app` is healthy (the healthcheck runs *inside* the
   container, so it is immune to host-port conflicts).
4. **For EACH demo DB** (sports, then cybermarket): drops any pre-existing graph
   (clean re-index), **indexes** it into a schema graph (introspect → grounded
   descriptions → value + JSON-leaf samples → embeddings) via `POST /database`,
   then **loads** its business knowledge (`*_business_knowledge.md`) and the
   general user-rules (`user_rules.md`).

On success it prints the URLs:

```text
================= T2S READY =================
 UI / API : http://localhost:5050/
 API docs : http://localhost:5050/openapi.json
 DBs      : sports_events_large (Formula-1, 61 tables) + cybermarket_pattern_large (marketplace, 56 tables)
 Embedding: bundled container t2s-embeddings (Qwen3-Embedding-0.6B, auto GPU/CPU)
 LLM      : openai/gemma-4-12b-it-qat @ http://host.docker.internal:1234/v1
 Verify   : see BENCHMARK.md (questions + gold SQL + expected values, both DBs)
=============================================
```

---

## 4. First query

UI: open `http://localhost:5050`, pick the Sports DB, ask e.g.
*"What's the fastest lap time ever recorded, in seconds?"*

Head-less:
```bash
curl -s -X POST http://localhost:5050/graphs/sports_events_large/sql \
  -H 'Content-Type: application/json' \
  -d '{"question":"What is the fastest lap time ever recorded, in seconds?",
       "use_knowledge":true,"use_user_rules":true}' | python3 -m json.tool
```

---

## 5. Common follow-ups

- **Change the model later:** edit `COMPLETION_MODEL` in `.env` and
  `docker compose up -d t2s`, **or** set it live in the Settings UI (the
  per-user `AppSettings` override beats `.env`).
- **Re-run from scratch:** `./install.sh` is safe to re-run — it rewrites `.env`,
  rebuilds, and re-indexes the demo DB cleanly.
- **Add your own DB / knowledge / rules:** see [`README.md`](README.md) §2–§4.
- **Stop / start:** `docker compose down` / `docker compose up -d`.
  (The indexed graph persists in the `t2s_graph_data` volume.)

---

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `app did not become healthy` | `docker compose logs t2s` — usually the embedding model still loading on first boot; wait and retry. |
| Generation errors / timeouts | Confirm your completion LLM is up and reachable at `--llm-api-base`; raise `LLM_TIMEOUT_SECONDS` in `.env` for slow local models. |
| Port already in use | `./install.sh --port <free-port>`. |
| Empty results after changing embedding model | the graph must be re-indexed for the new vector space: `POST /graphs/<graph_id>/refresh`. |
| Want a different embedding endpoint | set `EMBEDDING_API_BASE` / `EMBEDDING_MODEL` / `EMBEDDING_DIMENSION` in `.env`, then re-index. |

Full settings reference: [`README.md`](README.md) §6.
