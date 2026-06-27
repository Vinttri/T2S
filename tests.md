# T2S — Benchmark & Test Results

T2S is verified on **two** databases that ship with the quick start:

| DB | Domain | Size | Role |
|---|---|---|---|
| `sports_events_large` | Formula-1 (obfuscated Ergast) | 61 tables / ~28.5k rows | analytical + factual benchmarks |
| `cybermarket_pattern_large` | online-marketplace / risk | 56 tables / JSON-nested | **non-training** DB (stress test on an unseen schema) |

Every question has a gold SQL and is graded by a semantic **LLM judge** (result-set
equivalence, not string match: `9` == `9.00`, `57.481` == `57.4810000`).

### Config these numbers were measured with
- **Completion LLM:** `gemma-4-12b-it-qat` (a ~12B local model) over an
  OpenAI-compatible API, `temperature=0`, reasoning off.
- **Embedding:** `text-embedding-nomic-embed-text-v1.5` (768-d) over an
  OpenAI-compatible endpoint.
- The **zero-config quick start** instead bundles a CPU `Qwen3-Embedding-0.6B`
  (1024-d) so it runs with no external embedding service; to reproduce the exact
  numbers below, point `--embedding-api-base` at the same nomic model.
- Everything is deterministic (`temperature=0`) except mild run-to-run variance in
  the small model's free-form reasoning; the gates/validators are fixed.

---

## 1. Scoring levels

| Level | Meaning |
|---|---|
| **L4** | Result **semantically equivalent** to gold (the pass bar). |
| **L3** | Runs, but the result differs from gold. |
| **L1** | SQL executes with an error. |
| **L0** | No SQL produced. |

---

## 2. Verified hard cases (this build)

The cases below are the ones hardened in the current build — each was failing
before and is now correct. Re-run any of them head-less (see [§5](#5-reproduce-head-less)).

### 2a. Cybermarket — a NON-training schema (the real stress test)

| Question | Gold | T2S | ✓ |
|---|---|---|---|
| Total transactions both marked as fraud **and** cross-border | `154` | **154** | ✅ |
| How many shoppers use **advanced authentication**? | `329` | **329** | ✅ |
| How many platforms show as **'active'** right now? | `244` | **244** | ✅ |
| Average **keyword-hitting value** for high-risk patterns (round 3) | `0.084` | **0.084** | ✅ (stable ×4) |

These exercise the parts that make a schema "unseen": a buried fraud-probability
table that the question names only by meaning; CamelCase identifiers; 0/1 flag
filters; and a **KB-defined ratio metric** ("Suspicion Signal Density =
keyword_matches / total_messages") the model must apply as a **mean of per-row
ratios** (`AVG(kw / NULLIF(total,0)) = 0.084`), not `AVG(kw) = 9.885`.

### 2b. Cross-lingual + factual (sports)

| Question (RU) | Means | Gold | T2S | ✓ |
|---|---|---|---|---|
| «Сколько Гран-при прошло на автодромах Италии?» | GPs at circuits in Italy | `107` | **107** | ✅ |
| «Сколько всего гонщиков в базе?» | total drivers | `861` | **861** | ✅ |

The Italy case is a 2-table join + a country value nested in JSON
(`circuits.location_metadata->'location'->>'country'`), asked in Russian — it
forces value-routing (the literal "Italy" recovered past the model's mistranslation)
and the FK join the embedding model ranks low.

### 2c. Sports — factual set (14 cases, all ✅)

From [`BENCHMARK_sport_events_factual.jsonl`](test-platform/BENCHMARK_sport_events_factual.jsonl):
`1125` Grand Prix · `861` drivers · `77` circuits · `107` Italian-circuit GPs ·
`22` races in 2023 · British = `108` drivers · 2024 = `24` races · `86` poles ·
fastest lap `57.481`s · avg pit `83.399`s · Hamilton `18` poles · Mercedes `21`
poles · `35` countries.

---

## 3. Sports — analytical benchmark (14 cases)

**11 / 14 = 79 % L4** (semantic judge) on
[`BENCHMARK_sport_events.jsonl`](test-platform/BENCHMARK_sport_events.jsonl).

<div align="center">
  <img src="docs/sports_benchmark.svg" alt="Sports DB benchmark — analytical L4 by difficulty + factual" width="720">
</div>

| Difficulty | L4 / total |
|---|---|
| Simple | 3 / 3 |
| Moderate | 3 / 3 |
| Hard | 3 / 5 |
| Extra-Hard | 2 / 3 |
| **Total** | **11 / 14** |

The 3 non-L4 cases are **not pipeline failures** — they are points where the
**gold deviates from the loaded business knowledge / the data** (verified by
executing SQL on the DB): a sample- vs population-`STDDEV` lap-consistency formula
(H4), a "podium" with no race-podium column in any of the 61 tables + a year-sub
vs date-diff age (H5), and a prior-only `LAG()` vs cumulative points-per-race
definition (E2). Matching those golds would require contradicting the KB or
overfitting to a specific value — which the design forbids. They are flagged for
the benchmark owner.

| ID | Case | Difficulty | Result | Note |
|---|---|---|---|---|
| S1 | fastest lap (MIN, unit ÷1000) | Simple | ✅ L4 | |
| S2 | average pit-stop duration | Simple | ✅ L4 | |
| S3 | high-altitude circuits (JSON leaf) | Simple | ✅ L4 | |
| M1 | circuits with environmental traits | Moderate | ✅ L4 | |
| M2 | average age of sprint winners | Moderate | ✅ L4 | |
| M3 | rank drivers by Sprint Performance Index | Moderate | ✅ L4 | |
| H1 | top-8 sprint performance average | Hard | ✅ L4 | |
| H2 | constructor with best finishing record | Hard | ✅ L4 | |
| H3 | average stops per car per event | Hard | ✅ L4 | |
| H4 | lap-time consistency | Hard | ⚠️ L3 | gold sample-STDDEV ↮ KB population formula |
| H5 | youngest podium finishers | Hard | ⚠️ L3 | no race-podium column exists; gold age ↮ KB |
| E1 | constructor reliability rate | Extra-Hard | ✅ L4 | |
| E2 | points-per-race ranking | Extra-Hard | ⚠️ L3 | gold `LAG()` prior-only ↮ KB cumulative |
| E3 | McLaren cumulative CPS over seasons | Extra-Hard | ✅ L4 | window + chained metric |

---

## 4. Run it yourself — the comparison leaderboard

`install.sh` brings up a self-hosted **leaderboard** platform (FastAPI runner +
dashboard) configured against T2S (unless `--no-tests`):

```text
Leaderboard / comparison UI:  http://localhost:8090/
```

Pick a benchmark JSONL + a judge model, select one or more engines (T2S is
pre-configured), **Run** → per-case L0–L4 verdicts, totals, and a cross-engine
leaderboard. It is self-hosted (it carries connector configs/secrets), so it is
not a public URL.

---

## 5. Reproduce head-less

```bash
# cybermarket (non-training DB)
curl -s -X POST http://localhost:5050/graphs/cybermarket_pattern_large/sql \
  -H 'Content-Type: application/json' \
  -d '{"question":"I want the average keyword-hitting value for all chats to identify high-risk patterns, round to 3 decimals.","use_knowledge":true,"use_user_rules":true}'

# sports (cross-lingual)
curl -s -X POST http://localhost:5050/graphs/sports_events_large/sql \
  -H 'Content-Type: application/json' \
  -d '{"question":"Сколько Гран-при прошло на автодромах Италии?","use_knowledge":true,"use_user_rules":true}'
```

---

## 6. How these cases are made correct (general, no overfit)

No table/column/value is hardcoded; every fix is a general mechanism:
- **Grounded `db_description` + rich per-column embeddings** at index time so the
  retriever finds the right table even on an unseen schema (a placeholder
  description made the weak model hallucinate a generic e-commerce schema).
- **Vector/RAG seeds + FK-neighbour surfacing + value-routing** so a join partner
  or a value-holding column the embedder ranks low still reaches the generator.
- **Semantic KB-concept recall + a deterministic ratio gate** so a metric named by
  meaning is found and a ratio formula is applied as a mean of per-row ratios.
- **A schema-driven sqlglot gate** (CamelCase quoting, JSON paths, case-fold,
  integer division, NULLS-LAST) + an **execute→heal** loop.

See [`architecture.md`](architecture.md) for the full pipeline.
