#!/bin/bash
set -e


# Set default values if not set
FALKORDB_HOST="${FALKORDB_HOST:-localhost}"
FALKORDB_PORT="${FALKORDB_PORT:-6379}"

# Embeddings run in a SEPARATE sibling container ("embeddings"); the app reaches
# it via EMBEDDING_API_BASE (set in docker-compose). Nothing to launch here.

# Start FalkorDB Redis server in background
# Persist the graph into the mounted volume: without --dir the RDB dump lands
# in the container layer (/app) and every recreate loses the indexed RAG.
FALKORDB_DATA_DIR="${FALKORDB_DATA_DIR:-/var/lib/falkordb/data}"
mkdir -p "$FALKORDB_DATA_DIR"
redis-server --loadmodule /var/lib/falkordb/bin/falkordb.so \
    --dir "$FALKORDB_DATA_DIR" --save 60 1 --appendonly no | cat &

# Wait until FalkorDB is ready
echo "Waiting for FalkorDB to start on $FALKORDB_HOST:$FALKORDB_PORT..."

while ! nc -z "$FALKORDB_HOST" "$FALKORDB_PORT"; do
  sleep 0.5
done


echo "FalkorDB is up - launching FastAPI..."
# Determine whether to run in reload (debug) mode. The project uses FASTAPI_DEBUG
# environment variable historically; keep compatibility by honoring it here.
if [ "${FASTAPI_DEBUG:-False}" = "True" ] || [ "${FASTAPI_DEBUG:-true}" = "true" ]; then
  RELOAD_FLAG="--reload"
else
  RELOAD_FLAG=""
fi

echo "FalkorDB is up - launching FastAPI (uvicorn)..."
exec uvicorn api.index:app --host "${HOST:-0.0.0.0}" --port "${PORT:-5000}" $RELOAD_FLAG
