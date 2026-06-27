<div align="center">

# 🧠 T2S — Text-to-SQL

### Graph-grounded · multi-agent · runs **fully local**

Turn plain-English (or multilingual) questions into one correct, read-only **SQL**
statement over your relational database — with per-column evidence you can audit.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white)](docker-compose.yml)
[![Python 3.12](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![Graph: FalkorDB](https://img.shields.io/badge/graph-FalkorDB-FF1E56)](https://www.falkordb.com/)
[![Inspired by QueryWeaver](https://img.shields.io/badge/inspired%20by-QueryWeaver-8A2BE2)](https://github.com/FalkorDB/QueryWeaver)

[**Quick start**](#-quick-start) ·
[**Results**](#-results) ·
[**How it works**](#-how-it-works) ·
[**Architecture**](architecture.md) ·
[**Benchmarks**](tests.md)

</div>

---

T2S builds a **FalkorDB knowledge graph** of your schema (tables, columns, PK/FK,
data-grounded descriptions, value + JSON-leaf samples, vector embeddings), runs a
**multi-agent** retrieval→generation pipeline, validates every query with
deterministic **sqlglot** gates, and runs an **execute → heal** loop. It is tuned
to stay correct on a **small local LLM** and on **unseen schemas** — not just the
database it was demoed on.

```bash
git clone https://github.com/Vinttri/T2S.git && cd T2S
./install.sh            # one command → stack + TWO demo DBs + CPU embeddings
# open http://localhost:5050
```

> **Zero external setup for embeddings.** With no flag, T2S serves embeddings from
> a **bundled CPU container** (Qwen3-Embedding-0.6B). The only always-external
> dependency is a **completion LLM** with an OpenAI-compatible API. The quick start
> comes up with **two ready-to-query databases** (Formula-1 analytics + an
> online-marketplace), so you can ask questions immediately.

---

## ✨ Features

| | |
|---|---|
| 🔌 **Any DB** | Postgres · MySQL · Snowflake · Impala loaders; one graph per database. |
| 🧩 **Graph-grounded retrieval** | rich per-column embeddings, FK-neighbour surfacing, value-routing — finds the right table/column even on an **unseen** schema. |
| 🧠 **Business knowledge (RAG)** | define metrics once (`## Concept` + formula); the resolver binds them to real columns and the generator implements the **full formula**, not a look-alike column. |
| 🛡️ **Deterministic gates** | sqlglot-AST validators (CamelCase quoting, JSON paths, case-fold, integer division, NULLS-LAST, **ratio-formula adherence**) + execute→heal. |
| 🌍 **Multilingual** | ask in any language; a country/demonym normalizer + entity extraction bridge the question to an English schema. |
| 🔍 **Auditable** | every answer ships the SQL, the result rows, **per-column evidence**, and the kept/dropped schema. |
| 🏠 **Fully local** | Docker, Apple-Silicon friendly; CPU embeddings bundled; bring your own local/hosted LLM. |
| 📊 **Benchmark platform** | a self-hosted leaderboard that scores any Text-to-SQL engine with an LLM judge. |

---

## 🚀 Quick start

**Prerequisites:** Docker Desktop (arm64 OK) and a **completion LLM** on an
OpenAI-compatible API (default: [LM Studio](https://lmstudio.ai/) on the host at
`:1234`). macOS note: the UI defaults to port **5050** (AirPlay occupies 5000).

```bash
./install.sh                                   # all defaults: 2 DBs + CPU embeddings
./install.sh --port 8080                       # change UI/API port
./install.sh --llm-api-base http://127.0.0.1:1234 \
             --llm-api-key sk-... --llm-model openai/my-model
./install.sh --embedding-api-base http://127.0.0.1:1234 \   # OPTIONAL external embeddings
             --embedding-model openai/my-embed --embedding-dimension 768
./install.sh --no-tests                        # skip the benchmark platform
```

`install.sh` builds the stack, seeds **both** demo DBs into Postgres, indexes each
into a graph (descriptions → samples → embeddings), and loads their knowledge +
rules. Full walk-through and every flag: **[`quick_start.md`](quick_start.md)**.

```bash
# first query (head-less)
curl -s -X POST http://localhost:5050/graphs/sports_events_large/sql \
  -H 'Content-Type: application/json' \
  -d '{"question":"What is the fastest lap time ever recorded, in seconds?","use_knowledge":true}'
```

---

## 📊 Results

Verified on **two** shipped databases — including a **non-training** marketplace
schema (the real test: an unseen schema). Graded by a semantic LLM judge.
Full report + config: **[`tests.md`](tests.md)**.

<div align="center">
  <img src="docs/sports_benchmark.svg" alt="Sports DB benchmark — 11/14 analytical L4, 14/14 factual" width="720">
</div>

| Question | DB | Gold | T2S |
|---|---|:---:|:---:|
| Total fraud **and** cross-border transactions | cybermarket *(unseen)* | 154 | ✅ **154** |
| Shoppers using advanced authentication | cybermarket | 329 | ✅ **329** |
| Avg **keyword-hitting** value for high-risk patterns (ratio metric) | cybermarket | 0.084 | ✅ **0.084** |
| Grand Prix at circuits in Italy *(asked in Russian, JSON-nested country)* | sports | 107 | ✅ **107** |
| Sports analytical benchmark (14 cases, semantic judge) | sports | — | **11 / 14 L4** |

The 3 non-L4 analytical cases are **gold-vs-knowledge conflicts** (the gold deviates
from the loaded KB / data), not pipeline failures — shown case-by-case in
[`tests.md`](tests.md).

> _Measured with completion LLM `gemma-4-12b-it-qat` + a `nomic-embed-text-v1.5`
> embedding endpoint. The zero-config quick start bundles a CPU `Qwen3-Embedding-0.6B`._

---

## 🛠️ How it works

```mermaid
flowchart LR
    Q["NL question<br/>(any language)"] --> G{"Relevancy<br/>guard"}
    G -->|on-topic| F["Entity finder<br/>column-RAG · FK expand · value-routing"]
    F --> R["Metric resolver<br/>KB concept → column-bound formula"]
    R --> GEN["SQL generator<br/>(small local LLM)"]
    GEN --> V["Deterministic gates<br/>sqlglot AST · ratio/value validators"]
    V --> H[("execute → heal")]
    H --> A["answer + SQL<br/>+ per-column evidence"]
    G -->|off-topic| X["clear 'this DB has no…' reply"]
```

1. **Index time** — the schema is introspected into a FalkorDB graph: PK/FK, data-
   grounded column descriptions, value + JSON-leaf samples, and vector embeddings;
   the `db_description` is grounded in the real table names so retrieval works on an
   unseen schema.
2. **Retrieve** — entity extraction → rich per-column vector search → FK-neighbour
   surfacing → value-routing (a literal like *Italy* routes to the column whose
   domain holds it) → dedup → rank → prune.
3. **Resolve** — KB concepts named by meaning are recalled semantically and bound to
   real columns (a ratio stays a per-row ratio).
4. **Generate → validate → heal** — the SQL passes deterministic sqlglot gates
   (quoting, JSON paths, case-fold, ratio-formula adherence, …) then an execute→heal
   loop.

**Deep dive → [`architecture.md`](architecture.md):** the **agent-interaction
diagram + the shared JSON contract** (how agents pass one JSON and mutate it via
typed tool calls), the **RAG-base build & enrichment** block diagram (index-time
grounding), the full agent table, and the key features.

---

## 🗄️ Use your own database

T2S indexes any database into a schema graph (one graph per DB).

**UI:** *Add / connect database* → paste a SQLAlchemy URL
(`postgresql://user:pass@host:5432/db`) → it introspects, generates descriptions,
samples values, and builds the graph + embeddings.

**Head-less:**
```bash
curl -X POST http://localhost:5050/database \
  -H 'Content-Type: application/json' \
  -d '{"url":"postgresql://user:pass@host:5432/dbname"}'
# crash-safe re-index (after schema / embedding-model changes):
curl -X POST http://localhost:5050/graphs/<graph_id>/refresh
```

> **Idempotent by design:** all description/embedding generation happens at **index
> time** in code — a from-scratch install reproduces the exact graph. Don't hand-edit
> the live graph; change the source the indexer reads, then re-index.

### Business knowledge ("business RAG")

Define each metric/term as its own `##` concept (stored as a `:Knowledge` node,
embedded, retrieved only when relevant):

```markdown
## Suspicion Signal Density
Keyword hits per message volume in communication.
Definition: SSD = keyword_matches / total_messages
```

```bash
curl -X PUT http://localhost:5050/graphs/<graph_id>/knowledge \
  -H 'Content-Type: application/json' \
  -d "{\"knowledge\": \"$(cat business_knowledge.md)\"}"
```

### User-rules

General, DB-agnostic **behavioural** guidance (no table/column/value specifics) —
delivered to the linker, resolver, and generator. Set via the UI editor or
`PUT /graphs/<graph_id>/user-rules`.

> **Policy:** if a wrong answer is caused by missing metadata/knowledge, fix *that*
> (description / KB), not a per-question rule — and never overfit to a gold value.

---

## ⚙️ Settings

All settings are environment variables (in `.env`, written by `install.sh`);
model/temperature can also be overridden live per-user from the Settings UI
(stored on `:AppSettings`, overriding env). Key groups:

- **Completion LLM** — `COMPLETION_API_BASE/_API_KEY/_MODEL/_TEMPERATURE/_MAX_TOKENS`.
- **Embeddings** — `EMBEDDING_API_BASE/_MODEL/_DIMENSION` (default: bundled CPU
  container; change model/dimension ⇒ re-index).
- **Retrieval & pruning** — `TABLE_RETRIEVAL_TOP_K`, `TABLE_CONTEXT_MAX`,
  `TABLE_VECTOR_SEED_MAX`, `CONCEPT_RETRIEVAL_TOPK`, `SCHEMA_PRUNING_ENABLED`, …
- **Generation & healing** — `FORMULA_VALIDATOR_ENABLED`, `SQL_HEALING_MAX_ATTEMPTS`,
  `GENERATE_PREFLIGHT_HEAL_ENABLED`, `BUSINESS_CURRENT_DATE`.

Provider-agnostic via the OpenAI-compatible API (`OPENAI_*`, `GEMINI_API_KEY`,
`ANTHROPIC_API_KEY`, `OLLAMA_*`, … are recognized when set).

---

## 🔌 API

Base URL `http://localhost:<port>`; full spec at `/openapi.json`.

| Method & path | Purpose |
|---|---|
| `POST /graphs/{id}/sql` | generate SQL for a question (main endpoint) |
| `POST /database` · `POST /graphs/{id}/refresh` | index / re-index a database |
| `GET·PUT /graphs/{id}/knowledge` · `…/user-rules` | business knowledge / rules |
| `GET /graphs` · `DELETE /{id}` | list / delete graphs |
| `POST /context-test` | follow-up / session-context resolution |

---

## 📁 Project layout

```
api/               FastAPI backend
  agents/          pipeline agents (finder, resolver, generator, validators)
  core/            text2sql orchestration · schema graph · gates · grounding
  loaders/         postgres / mysql / snowflake / impala introspection
business-rules/    user_rules.md + *_business_knowledge.md (loaded at index time)
db-init/           the two demo DBs (seeded into Postgres on first boot)
test-platform/     benchmark + comparison leaderboard (see tests.md)
install.sh         one-command local install
docker-compose.yml postgres + embeddings + t2s
```

---

## 🙏 Credits

T2S is **inspired by [QueryWeaver](https://github.com/FalkorDB/QueryWeaver)** — the
open-source, graph-based Text-to-SQL engine by [FalkorDB](https://www.falkordb.com/).
It keeps that graph-grounded foundation and the **FalkorDB** graph engine, and adds
a localized, fully-offline deployment, a hardened multi-agent pipeline, data-grounded
indexing, deterministic sqlglot validators, and a benchmark/leaderboard platform.

Thanks to the FalkorDB team and the QueryWeaver project.

## 📄 License

[**AGPL-3.0-or-later**](LICENSE). As a derivative of an AGPL graph-Text2SQL engine,
T2S is released under the same license.
