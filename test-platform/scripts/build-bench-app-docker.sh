#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_NAME="${IMAGE_NAME:-leaderboard-bench-app}"
FRONTEND_IMAGE_NAME="${FRONTEND_IMAGE_NAME:-leaderboard-bench-frontend}"
TAG="${TAG:-latest}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"
FRONTEND_DOCKERFILE="${FRONTEND_DOCKERFILE:-Dockerfile.frontend}"
PLATFORM="${PLATFORM:-}"
NO_CACHE="${NO_CACHE:-0}"
PIP_INDEX_URL="${PIP_INDEX_URL:-}"
PIP_EXTRA_INDEX_URL="${PIP_EXTRA_INDEX_URL:-}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-}"

usage() {
  cat <<'EOF'
Usage: scripts/build-bench-app-docker.sh [options]

Options:
  -i, --image NAME      Docker image name. Default: leaderboard-bench-app
      --frontend-image NAME
                         Frontend image name. Default: leaderboard-bench-frontend
  -t, --tag TAG         Docker image tag. Default: latest
  -f, --file PATH       Dockerfile path. Default: Dockerfile
      --frontend-file PATH
                         Frontend Dockerfile path. Default: Dockerfile.frontend
      --platform VALUE  Optional docker build platform, e.g. linux/amd64
      --no-cache        Build without Docker cache
      --pip-index URL   Internal PyPI/simple index URL for Docker build
      --pip-extra URL   Extra PyPI/simple index URL for Docker build
      --pip-trusted HOST  Trusted host for internal HTTP PyPI
  -h, --help            Show this help

Environment variables with the same names are also supported:
IMAGE_NAME, FRONTEND_IMAGE_NAME, TAG, DOCKERFILE, FRONTEND_DOCKERFILE, PLATFORM, NO_CACHE,
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
    -f|--file)
      DOCKERFILE="$2"
      shift 2
      ;;
    --frontend-file)
      FRONTEND_DOCKERFILE="$2"
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

args=(build -f "$DOCKERFILE" -t "${IMAGE_NAME}:${TAG}")
if [[ -n "$PLATFORM" ]]; then
  args+=(--platform "$PLATFORM")
fi
if [[ "$NO_CACHE" == "1" ]]; then
  args+=(--no-cache)
fi
if [[ -n "$PIP_INDEX_URL" ]]; then
  args+=(--build-arg "PIP_INDEX_URL=$PIP_INDEX_URL")
fi
if [[ -n "$PIP_EXTRA_INDEX_URL" ]]; then
  args+=(--build-arg "PIP_EXTRA_INDEX_URL=$PIP_EXTRA_INDEX_URL")
fi
if [[ -n "$PIP_TRUSTED_HOST" ]]; then
  args+=(--build-arg "PIP_TRUSTED_HOST=$PIP_TRUSTED_HOST")
fi
args+=(.)

docker "${args[@]}"

frontend_args=(build -f "$FRONTEND_DOCKERFILE" -t "${FRONTEND_IMAGE_NAME}:${TAG}")
if [[ -n "$PLATFORM" ]]; then
  frontend_args+=(--platform "$PLATFORM")
fi
if [[ "$NO_CACHE" == "1" ]]; then
  frontend_args+=(--no-cache)
fi
frontend_args+=(.)

docker "${frontend_args[@]}"

cat <<EOF

Built:
  ${IMAGE_NAME}:${TAG}
  ${FRONTEND_IMAGE_NAME}:${TAG}

Run locally with split frontend/backend/worker/backup:
  docker compose up -d

Open:
  http://127.0.0.1:8090/
EOF
