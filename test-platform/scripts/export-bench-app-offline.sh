#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-leaderboard-bench-app}"
FRONTEND_IMAGE_NAME="${FRONTEND_IMAGE_NAME:-leaderboard-bench-frontend}"
TAG="${TAG:-latest}"
OUT_DIR="${OUT_DIR:-$ROOT_DIR/dist/offline}"
PLATFORM="${PLATFORM:-}"
NO_CACHE="${NO_CACHE:-0}"
SKIP_BUILD="${SKIP_BUILD:-0}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
PIP_EXTRA_INDEX_URL="${PIP_EXTRA_INDEX_URL:-}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-}"

usage() {
  cat <<'EOF'
Usage: scripts/export-bench-app-offline.sh [options]

Builds Docker images and exports an offline deployment bundle that can be
copied into an isolated network with no internet access.

Options:
  -i, --image NAME      Docker image name. Default: leaderboard-bench-app
      --frontend-image NAME
                         Frontend image name. Default: leaderboard-bench-frontend
  -t, --tag TAG         Docker image tag. Default: latest
  -o, --out-dir PATH    Output directory. Default: dist/offline
      --platform VALUE  Optional docker build platform, e.g. linux/amd64
      --no-cache        Build without Docker cache
      --skip-build      Do not build; export an already existing local image
      --pip-index URL   Internal PyPI/simple index URL for Docker build
      --pip-extra URL   Extra PyPI/simple index URL for Docker build
      --pip-trusted HOST  Trusted host for internal HTTP PyPI
  -h, --help            Show this help

Environment variables with the same names are also supported:
IMAGE_NAME, FRONTEND_IMAGE_NAME, TAG, OUT_DIR, PLATFORM, NO_CACHE, SKIP_BUILD,
PIP_INDEX_URL, PIP_EXTRA_INDEX_URL, PIP_TRUSTED_HOST.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -i|--image)
      IMAGE_NAME="$2"
      shift 2
      ;;
    --frontend-image)
      FRONTEND_IMAGE_NAME="$2"
      shift 2
      ;;
    -t|--tag)
      TAG="$2"
      shift 2
      ;;
    -o|--out-dir)
      OUT_DIR="$2"
      shift 2
      ;;
    --platform)
      PLATFORM="$2"
      shift 2
      ;;
    --no-cache)
      NO_CACHE=1
      shift
      ;;
    --skip-build)
      SKIP_BUILD=1
      shift
      ;;
    --pip-index)
      PIP_INDEX_URL="$2"
      shift 2
      ;;
    --pip-extra)
      PIP_EXTRA_INDEX_URL="$2"
      shift 2
      ;;
    --pip-trusted)
      PIP_TRUSTED_HOST="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

cd "$ROOT_DIR"

if [[ "$SKIP_BUILD" != "1" ]]; then
  build_args=(--image "$IMAGE_NAME" --frontend-image "$FRONTEND_IMAGE_NAME" --tag "$TAG")
  if [[ -n "$PLATFORM" ]]; then
    build_args+=(--platform "$PLATFORM")
  fi
  if [[ "$NO_CACHE" == "1" ]]; then
    build_args+=(--no-cache)
  fi
  if [[ -n "$PIP_INDEX_URL" ]]; then
    build_args+=(--pip-index "$PIP_INDEX_URL")
  fi
  if [[ -n "$PIP_EXTRA_INDEX_URL" ]]; then
    build_args+=(--pip-extra "$PIP_EXTRA_INDEX_URL")
  fi
  if [[ -n "$PIP_TRUSTED_HOST" ]]; then
    build_args+=(--pip-trusted "$PIP_TRUSTED_HOST")
  fi
  scripts/build-bench-app-docker.sh "${build_args[@]}"
fi

if ! docker image inspect "${IMAGE_NAME}:${TAG}" >/dev/null 2>&1; then
  echo "Docker image ${IMAGE_NAME}:${TAG} not found." >&2
  exit 1
fi
if ! docker image inspect "${FRONTEND_IMAGE_NAME}:${TAG}" >/dev/null 2>&1; then
  echo "Docker image ${FRONTEND_IMAGE_NAME}:${TAG} not found." >&2
  exit 1
fi

EXPORT_TAG="latest"
if [[ "$TAG" != "$EXPORT_TAG" ]]; then
  echo "Tagging ${IMAGE_NAME}:${TAG} as ${IMAGE_NAME}:${EXPORT_TAG} for offline export..."
  docker tag "${IMAGE_NAME}:${TAG}" "${IMAGE_NAME}:${EXPORT_TAG}"
  echo "Tagging ${FRONTEND_IMAGE_NAME}:${TAG} as ${FRONTEND_IMAGE_NAME}:${EXPORT_TAG} for offline export..."
  docker tag "${FRONTEND_IMAGE_NAME}:${TAG}" "${FRONTEND_IMAGE_NAME}:${EXPORT_TAG}"
fi

safe_name="$(printf '%s_%s' "$IMAGE_NAME" "$TAG" | sed 's#[^A-Za-z0-9_.-]#_#g')"
bundle_dir="$OUT_DIR/$safe_name"
bundle_tar="$OUT_DIR/$safe_name.tar.gz"

rm -rf "$bundle_dir" "$bundle_tar"
mkdir -p "$bundle_dir"

echo "Saving Docker images ${IMAGE_NAME}:${EXPORT_TAG} and ${FRONTEND_IMAGE_NAME}:${EXPORT_TAG}..."
docker save "${IMAGE_NAME}:${EXPORT_TAG}" "${FRONTEND_IMAGE_NAME}:${EXPORT_TAG}" | gzip -c > "$bundle_dir/image.tar.gz"

cp docker/run-bench-app-offline.sh "$bundle_dir/run-bench-app-offline.sh"
chmod +x "$bundle_dir/run-bench-app-offline.sh"
cp docker-compose.yml "$bundle_dir/docker-compose.yml"

cat > "$bundle_dir/.env.example" <<EOF
# Copy this file to .env and edit values for your contour.
# The run script reads this file for both Docker launch settings and app runtime env.

# Docker Compose launch settings.
BENCH_BACKEND_IMAGE=${IMAGE_NAME}:${EXPORT_TAG}
BENCH_FRONTEND_IMAGE=${FRONTEND_IMAGE_NAME}:${EXPORT_TAG}
IMAGE_ARCHIVE=./image.tar.gz
BENCH_BACKEND_CONTAINER_NAME=benchmark-backend
BENCH_WORKER_CONTAINER_NAME=benchmark-worker
BENCH_FRONTEND_CONTAINER_NAME=benchmark-frontend
BENCH_BACKUP_CONTAINER_NAME=benchmark-backup
BENCH_HOST_PORT=8090
BENCH_DATA_DIR=./data
BENCH_REVIEWS_DIR=./reviews
BENCH_BACKUPS_DIR=./backups
BENCH_ENV_FILE=./.env

# Optional local container resource caps.
BENCH_BACKEND_MEM_LIMIT=512m
BENCH_BACKEND_CPUS=1.0
BENCH_WORKER_MEM_LIMIT=2g
BENCH_WORKER_CPUS=2.0
BENCH_FRONTEND_MEM_LIMIT=128m
BENCH_FRONTEND_CPUS=0.5
BENCH_BACKUP_MEM_LIMIT=256m
BENCH_BACKUP_CPUS=0.25

# App runtime storage. SQLite in mounted /data is the default.
BENCH_STORE_URL=sqlite:////data/app.db

# App runtime flags.
BENCH_APP_SYNC_CONNECTOR_YAML=1
BENCH_APP_RUNNER_MODE=worker
BENCH_APP_WS_SNAPSHOT_INTERVAL_S=2
BENCH_APP_GOLD_CACHE=1
BENCH_APP_GOLD_CACHE_CONCURRENCY=4
BENCH_APP_MAX_API_CONCURRENCY=1
BENCH_APP_MAX_IMPALA_CONCURRENCY=1
BENCH_APP_STDOUT_RUN_LOGS=1
BENCH_APP_STDOUT_RUN_LOG_MAX_CHARS=20000
BENCH_APP_CIRCUIT_BREAKER_ENABLED=1
BENCH_APP_CIRCUIT_BREAKER_FAILURES=5
BENCH_WORKER_POLL_INTERVAL_S=1
BENCH_WORKER_HEARTBEAT_S=5
BENCH_WORKER_STALE_AFTER_S=900
BENCH_WORKER_WATCHDOG_INTERVAL_S=30
BENCH_WORKER_MAX_JOB_ATTEMPTS=3

# Periodic on-disk backups of /data/app.db and runtime JSONL/artifact dirs.
BENCH_BACKUP_ENABLED=1
BENCH_BACKUP_INTERVAL_S=1800
BENCH_BACKUP_KEEP=48
BENCH_BACKUP_DIR=/backups
BENCH_APP_SSL_VERIFY=0

# Run L0-L4 judging through your internal OpenAI-compatible LLM gateway.
BENCH_APP_AUTO_JUDGE=1
LLM_BASE_URL=http://your-llm-gateway/v1
LLM_API_KEY=replace-me
LLM_MODEL=llmgateway/free
LLM_AUTH_HEADER=Authorization
LLM_AUTH_SCHEME=Bearer
# LLM request limits and retries.
LLM_JUDGE_TIMEOUT=3600
LLM_TEST_TIMEOUT=3600
LLM_JUDGE_CONCURRENCY=1
LLM_JUDGE_MAX_RETRIES=2
LLM_JUDGE_RETRY_DELAY=3

# Scoring DB for datasets. Dataset DSNs are not entered in the UI; the app
# resolves them from env by db_id/db_type, for example:
# BENCH_<DB_ID>_<DB_TYPE>_DSN, BENCH_<DB_ID>_DSN, or BENCH_SCORING_DSN.
BENCH_SCORING_DSN=

# Scoring DB for seeded dm_mis Impala datasets.
# BENCH_DM_MIS_IMPALA_DSN=impala://login:password@impala-host:31000/core_tmp?auth_mechanism=LDAP&use_ssl=true&verify_cert=false&request_pool=root.core-dbt&connect_timeout=10
EOF

cat > "$bundle_dir/README_OFFLINE.md" <<'EOF'
# Offline Deployment

This directory contains everything needed to run the benchmark app Docker images
in an isolated network with no internet access. The app runs as four compose
services: `frontend` (nginx/static UI), `backend` (FastAPI), `worker`
(durable benchmark executor), and `backup` (periodic on-disk backups).

Requirements inside the isolated network:

- Docker Engine already installed.
- `tar`, `gzip`, and `bash` available on the host.

Load and run:

```bash
tar -xzf leaderboard-bench-app_*.tar.gz
cd leaderboard-bench-app_*
cp .env.example .env
# edit .env if needed
./run-bench-app-offline.sh
```

Open:

```text
http://127.0.0.1:8090/
```

Runtime data lives in host folders by default:

- `./data` -> backend+worker `/data`
- `./reviews` -> backend+worker `/reviews`
- `./backups` -> backup `/backups`

To use other host folders, set these in `.env` before running:

```text
BENCH_DATA_DIR=/opt/leaderboard/data
BENCH_REVIEWS_DIR=/opt/leaderboard/reviews
BENCH_BACKUPS_DIR=/opt/leaderboard/backups
```

Runtime storage defaults to SQLite:

```text
sqlite:////data/app.db
```

If the scoring DB is on the same Docker host, use `host.docker.internal` in the
DSN. Dataset DSNs are resolved from env, not entered in the UI. Set
`BENCH_<DB_ID>_<DB_TYPE>_DSN`, `BENCH_<DB_ID>_DSN`, or `BENCH_SCORING_DSN` to an
address reachable from the backend container.

Optional environment variables such as `BENCH_APP_AUTO_JUDGE`,
`BENCH_APP_SYNC_CONNECTOR_YAML`, `BENCH_APP_MAX_API_CONCURRENCY`,
`BENCH_APP_RUNNER_MODE`, `BENCH_APP_WS_SNAPSHOT_INTERVAL_S`,
`BENCH_APP_STDOUT_RUN_LOGS`, `BENCH_APP_STDOUT_RUN_LOG_MAX_CHARS`,
`BENCH_APP_CIRCUIT_BREAKER_*`, `BENCH_WORKER_*`, `BENCH_BACKUP_*`,
`LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_JUDGE_TIMEOUT`,
`LLM_JUDGE_CONCURRENCY`, `LLM_JUDGE_MAX_RETRIES`, and `LLM_JUDGE_RETRY_DELAY`
live in `.env`. The run script uses Docker Compose and passes `.env` to the
backend and worker services; it does not copy ambient shell variables into the
container by default.
EOF

(
  cd "$OUT_DIR"
  tar -czf "$bundle_tar" "$safe_name"
)

cat <<EOF

Offline bundle created:
  $bundle_tar

Copy this file into the isolated network, then run:
  tar -xzf $(basename "$bundle_tar")
  cd $safe_name
  cp .env.example .env
  ./run-bench-app-offline.sh
EOF
