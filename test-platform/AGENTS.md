# Coding Agent Context

This repository is the active Text-to-SQL benchmark workspace.

Authoritative context for future LLM coding agents is in `CODEX_CONTEXT.md`.
Read it before editing code. The repository is trimmed around the current live
`bench_app` benchmark app; do not reintroduce legacy static-dashboard pipeline
work unless the user explicitly asks for it.

Current live app:
- Root: `/root/leaderboard_builder_codex`
- Public URL: `http://benchmark.144.91.85.207.nip.io:8080/`
- Backend: `bench_app.server:app` on host port `8090`
- Store: `bench_app/data/app.db`
- Main app code: `bench_app/`
- Tests: `/root/leaderboard_builder/.venv/bin/python -m pytest`

Important rules:
- Do not hide real L0 results with human/manual overrides.
- If a case is L0 because the connector returned no SQL, rerun the case or run
  the raw-answer + LLM-judge stage again; only merge real observed results.
- Raw connector answers must remain separate from L0-L4 scoring:
  `bench-answers/v1` first, then `bench-judged-levels/v1`, then final
  `bench-result/v1`.
- Do not print API keys or tokens in chat/logs. Redact secrets when inspecting
  credentials.
