#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/.env}"
if [[ "$CONFIG_FILE" != /* ]]; then
  CONFIG_FILE="$SCRIPT_DIR/$CONFIG_FILE"
fi
if [[ -f "$CONFIG_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
elif [[ -f "$SCRIPT_DIR/offline.env" ]]; then
  # Backward-compatible fallback for older bundles. New bundles use .env only.
  set -a
  # shellcheck disable=SC1091
  . "$SCRIPT_DIR/offline.env"
  set +a
fi

IMAGE="${IMAGE:-leaderboard-bench-app:latest}"
BENCH_BACKEND_IMAGE="${BENCH_BACKEND_IMAGE:-$IMAGE}"
BENCH_FRONTEND_IMAGE="${BENCH_FRONTEND_IMAGE:-leaderboard-bench-frontend:latest}"
IMAGE_ARCHIVE="${IMAGE_ARCHIVE:-$SCRIPT_DIR/image.tar.gz}"
BENCH_HOST_PORT="${BENCH_HOST_PORT:-${HOST_PORT:-8090}}"
BENCH_DATA_DIR="${BENCH_DATA_DIR:-${DATA_HOST_DIR:-$SCRIPT_DIR/data}}"
BENCH_REVIEWS_DIR="${BENCH_REVIEWS_DIR:-${REVIEWS_HOST_DIR:-$SCRIPT_DIR/reviews}}"
BENCH_BACKUPS_DIR="${BENCH_BACKUPS_DIR:-$SCRIPT_DIR/backups}"
BENCH_BACKEND_CONTAINER_NAME="${BENCH_BACKEND_CONTAINER_NAME:-${CONTAINER_NAME:-benchmark-backend}}"
BENCH_WORKER_CONTAINER_NAME="${BENCH_WORKER_CONTAINER_NAME:-benchmark-worker}"
BENCH_FRONTEND_CONTAINER_NAME="${BENCH_FRONTEND_CONTAINER_NAME:-benchmark-frontend}"
BENCH_BACKUP_CONTAINER_NAME="${BENCH_BACKUP_CONTAINER_NAME:-benchmark-backup}"
ENV_FILE="${ENV_FILE:-$CONFIG_FILE}"
PASS_ENV_NAMES="${PASS_ENV_NAMES:-}"
COMPOSE_FILE="${COMPOSE_FILE:-$SCRIPT_DIR/docker-compose.yml}"

if [[ "$IMAGE_ARCHIVE" != /* ]]; then
  IMAGE_ARCHIVE="$SCRIPT_DIR/$IMAGE_ARCHIVE"
fi
if [[ -n "$ENV_FILE" && "$ENV_FILE" != /* ]]; then
  ENV_FILE="$SCRIPT_DIR/$ENV_FILE"
fi

if ! docker image inspect "$BENCH_BACKEND_IMAGE" >/dev/null 2>&1 || ! docker image inspect "$BENCH_FRONTEND_IMAGE" >/dev/null 2>&1; then
  if [[ ! -f "$IMAGE_ARCHIVE" ]]; then
    echo "One or more required images are not loaded and $IMAGE_ARCHIVE does not exist." >&2
    exit 1
  fi
  echo "Loading Docker image from $IMAGE_ARCHIVE..."
  case "$IMAGE_ARCHIVE" in
    *.gz|*.tgz)
      gzip -dc "$IMAGE_ARCHIVE" | docker load
      ;;
    *)
      docker load -i "$IMAGE_ARCHIVE"
      ;;
  esac
fi

if ! docker image inspect "$BENCH_BACKEND_IMAGE" >/dev/null 2>&1; then
  echo "Backend image $BENCH_BACKEND_IMAGE is not loaded." >&2
  exit 1
fi
if ! docker image inspect "$BENCH_FRONTEND_IMAGE" >/dev/null 2>&1; then
  echo "Frontend image $BENCH_FRONTEND_IMAGE is not loaded." >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  compose_cmd=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose_cmd=(docker-compose)
else
  echo "Docker Compose is required for the split frontend/backend deployment." >&2
  exit 1
fi

mkdir -p "$BENCH_DATA_DIR" "$BENCH_REVIEWS_DIR" "$BENCH_BACKUPS_DIR"

export BENCH_BACKEND_IMAGE BENCH_FRONTEND_IMAGE BENCH_HOST_PORT
export BENCH_DATA_DIR BENCH_REVIEWS_DIR BENCH_BACKUPS_DIR
export BENCH_BACKEND_CONTAINER_NAME BENCH_WORKER_CONTAINER_NAME BENCH_FRONTEND_CONTAINER_NAME BENCH_BACKUP_CONTAINER_NAME
export BENCH_ENV_FILE="${BENCH_ENV_FILE:-$ENV_FILE}"

for name in $PASS_ENV_NAMES; do
  if [[ -n "${!name-}" ]]; then
    export "$name=${!name}"
  fi
done

compose_args=(-f "$COMPOSE_FILE")
if [[ -n "$CONFIG_FILE" && -f "$CONFIG_FILE" ]]; then
  compose_args=(--env-file "$CONFIG_FILE" "${compose_args[@]}")
fi

echo "Starting $BENCH_BACKEND_IMAGE + $BENCH_FRONTEND_IMAGE with Docker Compose..."
cd "$SCRIPT_DIR"
exec "${compose_cmd[@]}" "${compose_args[@]}" up -d
