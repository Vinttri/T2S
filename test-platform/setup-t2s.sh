#!/usr/bin/env bash
# =====================================================================
# Build + bring up the benchmark platform, configured to test T2S on the
# Sports DB. Joins the T2S docker network (t2s-net) so it can reach the app
# (t2s-app:5000) and the scoring Postgres (t2s-postgres:5432) by name.
# Seeds a "T2S" connector + the sports dataset. Does NOT run any benchmark.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

BENCH_URL="http://localhost:8090"
CONNECTOR_URL="http://t2s-app:5000/graphs/sports_events_large/sql"
SCORING_DSN="postgresql://t2s:t2s@t2s-postgres:5432/sports_events_large"

# ---- 1. build bench images (backend + static frontend) ----
echo "[test] building benchmark images..."
docker build -q -t leaderboard-bench-app:latest -f Dockerfile . >/dev/null
docker build -q -t leaderboard-bench-frontend:latest -f Dockerfile.frontend . >/dev/null

# ---- 2. env: scoring DB = Sports Postgres; LLM judge reuses T2S's completion LLM ----
mkdir -p data reviews backups
# Reuse T2S's completion endpoint for the auto-judge (same local LM Studio).
T2S_ENV="$(dirname "$0")/../.env"
JUDGE_BASE="http://host.docker.internal:1234/v1"; JUDGE_KEY=""; JUDGE_MODEL="gemma-4-12b-it-qat"
if [ -f "$T2S_ENV" ]; then
  _b="$(grep -E '^COMPLETION_API_BASE=' "$T2S_ENV" | head -1 | cut -d= -f2-)"; [ -n "$_b" ] && JUDGE_BASE="$_b"
  _k="$(grep -E '^COMPLETION_API_KEY=' "$T2S_ENV" | head -1 | cut -d= -f2-)"; [ -n "$_k" ] && JUDGE_KEY="$_k"
  _m="$(grep -E '^COMPLETION_MODEL=' "$T2S_ENV" | head -1 | cut -d= -f2- | sed 's#^openai/##')"; [ -n "$_m" ] && JUDGE_MODEL="$_m"
fi
cat > .env <<EOF
BENCH_STORE_URL=sqlite:////data/app.db
BENCH_APP_SSL_VERIFY=0
BENCH_APP_AUTO_JUDGE=1
BENCH_APP_MAX_API_CONCURRENCY=1
BENCH_APP_STDOUT_RUN_LOGS=1
BENCH_SCORING_DSN=$SCORING_DSN
BENCH_SPORTS_EVENTS_LARGE_POSTGRES_DSN=$SCORING_DSN
LLM_BASE_URL=$JUDGE_BASE
LLM_API_KEY=$JUDGE_KEY
LLM_MODEL=$JUDGE_MODEL
JUDGE_TIMEOUT=600
EOF

# ---- 3. attach the bench stack to the T2S network (external) ----
cat > docker-compose.t2s.yml <<'EOF'
networks:
  default:
    name: t2s-net
    external: true
EOF

# ---- 4. bring it up on t2s-net ----
echo "[test] starting benchmark platform on t2s-net..."
docker compose -f docker-compose.yml -f docker-compose.t2s.yml up -d

# ---- 5. wait for the bench API ----
for _ in $(seq 1 60); do curl -fsS "$BENCH_URL/api/live" >/dev/null 2>&1 && break || sleep 3; done

# ---- 6. seed the sports dataset + the T2S connector (best-effort; run NOT started) ----
echo "[test] seeding sports dataset + T2S connector (benchmark NOT executed)..."
# NOTE: /api/datasets/upload takes a JSON body (DatasetUpload), not multipart.
python3 - "$BENCH_URL" <<'PY' && echo "[test]  dataset 'sports_events_large' uploaded" || echo "[test]  (upload via UI if this failed)"
import json, sys, urllib.request
base = sys.argv[1].rstrip("/")
content = open("BENCHMARK_sport_events.jsonl", encoding="utf-8").read()
payload = {"name": "sports_events_large", "file_name": "BENCHMARK_sport_events.jsonl",
           "content": content, "db_id": "sports_events_large", "db_type": "postgres"}
req = urllib.request.Request(base + "/api/datasets/upload",
    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
urllib.request.urlopen(req, timeout=120).read()
PY

curl -sS -X POST "$BENCH_URL/api/connectors" -H 'Content-Type: application/json' -d @- >/dev/null 2>&1 <<EOF && echo "[test]  connector 'T2S' created" || echo "[test]  (create connector in UI if this failed)"
{
  "name": "T2S",
  "kind": "templated",
  "url": "$CONNECTOR_URL",
  "method": "POST",
  "headers": {"Content-Type": "application/json"},
  "body_template": "{\"question\": \"{{question}}\", \"use_user_rules\": true, \"use_knowledge\": true}",
  "sql_extract": {"field": "sql", "mode": "raw"},
  "timeout": 600
}
EOF

cat <<EOF

[test] Benchmark platform ready (configured, NOT run):
  Dashboard : $BENCH_URL/
  Connector : T2S  ->  $CONNECTOR_URL   (sql_extract field="sql")
  Dataset   : sports_events_large  (BENCHMARK_sport_events.jsonl, 14 cases)
  Scoring DB: $SCORING_DSN
  To run: open the dashboard, pick dataset=sports + connector=T2S, Start Run.
EOF
