# Benchmark State Graph

Authoritative code contract: `bench_app/state_graph.py`.

This document describes legal status transitions for benchmark runs, individual
cases, and durable worker jobs. Same-state updates are allowed everywhere:
workers often refresh counters or timestamps without changing the status.

## Run Statuses

- `queued`: the run is waiting for inline execution or a worker job.
- `running`: connector API and scoring SQL collection are active.
- `paused`: user pause or circuit breaker pause.
- `judging`: LLM judging is active.
- `done`: finished successfully.
- `error`: failed and needs user action or rerun.
- `stopped`: stopped by user or cancellation.

Important rule: a stopped run must not go directly to `running`. Any rerun or
case rerun must go through `queued` first.

```mermaid
flowchart LR
  queued --> running
  queued --> paused
  queued --> stopped
  queued --> error

  running --> queued
  running --> paused
  running --> judging
  running --> done
  running --> error
  running --> stopped

  paused --> queued
  paused --> running
  paused --> done
  paused --> error
  paused --> stopped

  judging --> queued
  judging --> running
  judging --> paused
  judging --> done
  judging --> error
  judging --> stopped

  done --> queued
  done --> judging
  error --> queued
  error --> judging
  stopped --> queued
```

## Case Statuses

- `api_waiting`: waiting for connector API.
- `done`: collected without LLM judge.
- `llm_queued`: collected and waiting for LLM judge capacity.
- `awaiting_judge`: compatibility label for a collected case waiting on judge.
- `sent_to_judge`: request to LLM judge was sent.
- `judging`: LLM judge is currently evaluating.
- `judged`: final L0-L4 level is ready.
- `api_error`, `api_timeout`, `no_sql`, `gold_error`, `sql_error`: connector,
  scoring, or SQL collection outcome before judging.
- `judge_error`: LLM judge failed.

Collection errors may still transition to `llm_queued`: the judge must classify
real L0/L1/L2 outcomes instead of hiding them.

```mermaid
flowchart LR
  api_waiting --> done
  api_waiting --> llm_queued
  api_waiting --> api_error
  api_waiting --> api_timeout
  api_waiting --> no_sql
  api_waiting --> gold_error
  api_waiting --> sql_error

  api_error --> api_waiting
  api_error --> llm_queued
  api_timeout --> api_waiting
  api_timeout --> llm_queued
  no_sql --> api_waiting
  no_sql --> llm_queued
  gold_error --> api_waiting
  gold_error --> llm_queued
  sql_error --> api_waiting
  sql_error --> llm_queued
  done --> api_waiting
  done --> llm_queued

  llm_queued --> api_waiting
  llm_queued --> awaiting_judge
  llm_queued --> sent_to_judge
  llm_queued --> judging
  llm_queued --> judged
  llm_queued --> judge_error
  awaiting_judge --> api_waiting
  awaiting_judge --> llm_queued
  awaiting_judge --> sent_to_judge
  awaiting_judge --> judging
  awaiting_judge --> judged
  awaiting_judge --> judge_error
  sent_to_judge --> api_waiting
  sent_to_judge --> judging
  sent_to_judge --> judged
  sent_to_judge --> judge_error
  judging --> api_waiting
  judging --> judged
  judging --> judge_error
  judged --> api_waiting
  judged --> llm_queued
  judge_error --> api_waiting
  judge_error --> llm_queued
```

## Worker Job Statuses

- `queued`: job is waiting for a worker.
- `running`: job is claimed by a worker and heartbeating.
- `done`: job finished.
- `error`: job failed.
- `cancelled`: job was cancelled by user/worker shutdown.

Watchdog recovery is the only normal path from `running` back to `queued`.

```mermaid
flowchart LR
  queued --> running
  queued --> cancelled
  queued --> error
  running --> queued
  running --> done
  running --> error
  running --> cancelled
```
