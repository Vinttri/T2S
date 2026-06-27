#!/usr/bin/env bash
set -Eeuo pipefail

APP_ROOT="${APP_ROOT:-/app}"
DATA_DIR="${BENCH_APP_DATA_DIR:-/data}"
REVIEWS_DIR="${BENCH_APP_REVIEWS_DIR:-/reviews}"
REVIEWS_SEED_DIR="${BENCH_APP_REVIEWS_SEED_DIR:-$APP_ROOT/.seed/reviews}"

mkdir -p \
  "$DATA_DIR" \
  "$DATA_DIR/runs" \
  "$DATA_DIR/answers" \
  "$DATA_DIR/judged" \
  "$DATA_DIR/logs" \
  "$DATA_DIR/datasets" \
  "$DATA_DIR/connectors" \
  "$REVIEWS_DIR"

if [[ -d "$REVIEWS_SEED_DIR" ]] && ! find "$REVIEWS_DIR" -mindepth 1 -maxdepth 1 -type f 2>/dev/null | grep -q .; then
  cp -a "$REVIEWS_SEED_DIR/." "$REVIEWS_DIR/"
fi

export BENCH_STORE_URL="${BENCH_STORE_URL:-sqlite:///$DATA_DIR/app.db}"
export BENCH_APP_DATA_DIR="$DATA_DIR"
export BENCH_APP_RUNS_DIR="${BENCH_APP_RUNS_DIR:-$DATA_DIR/runs}"
export BENCH_APP_ANSWERS_DIR="${BENCH_APP_ANSWERS_DIR:-$DATA_DIR/answers}"
export BENCH_APP_JUDGED_DIR="${BENCH_APP_JUDGED_DIR:-$DATA_DIR/judged}"
export BENCH_APP_RUN_LOGS_DIR="${BENCH_APP_RUN_LOGS_DIR:-$DATA_DIR/logs}"
export BENCH_APP_DATASETS_DIR="${BENCH_APP_DATASETS_DIR:-$DATA_DIR/datasets}"
export BENCH_APP_CONNECTORS_YAML_DIR="${BENCH_APP_CONNECTORS_YAML_DIR:-$DATA_DIR/connectors}"
export BENCH_APP_REVIEWS_DIR="$REVIEWS_DIR"

if [[ $# -eq 0 ]]; then
  UVICORN_LOG_CONFIG="${UVICORN_LOG_CONFIG:-$APP_ROOT/bench_app/logging.ini}"
  set -- python -m uvicorn bench_app.server:app --host 0.0.0.0 --port "${PORT:-8090}" --ws "${UVICORN_WS:-wsproto}"
  if [[ -n "$UVICORN_LOG_CONFIG" && -f "$UVICORN_LOG_CONFIG" ]]; then
    set -- "$@" --log-config "$UVICORN_LOG_CONFIG"
  fi
fi

exec "$@"
