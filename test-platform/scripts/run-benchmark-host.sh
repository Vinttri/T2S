#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${BENCH_ENV_FILE:-$ROOT_DIR/.env}"
PYTHON_BIN="${PYTHON_BIN:-/root/leaderboard_builder/.venv/bin/python}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8090}"
UVICORN_LOG_CONFIG="${UVICORN_LOG_CONFIG:-bench_app/logging.ini}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
elif [[ -f "$ROOT_DIR/dist/offline/leaderboard-bench-app.env" ]]; then
  set -a
  # Backward-compatible fallback for this host workspace.
  # shellcheck disable=SC1091
  . "$ROOT_DIR/dist/offline/leaderboard-bench-app.env"
  set +a
fi

cd "$ROOT_DIR"
args=(-m uvicorn bench_app.server:app --host "$HOST" --port "$PORT")
if [[ -n "$UVICORN_LOG_CONFIG" && -f "$UVICORN_LOG_CONFIG" ]]; then
  args+=(--log-config "$UVICORN_LOG_CONFIG")
fi
exec "$PYTHON_BIN" "${args[@]}"
