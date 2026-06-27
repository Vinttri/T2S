FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8090 \
    APP_ROOT=/app \
    BENCH_APP_DATA_DIR=/data \
    BENCH_STORE_URL=sqlite:////data/app.db \
    BENCH_APP_RUNS_DIR=/data/runs \
    BENCH_APP_ANSWERS_DIR=/data/answers \
    BENCH_APP_JUDGED_DIR=/data/judged \
    BENCH_APP_RUN_LOGS_DIR=/data/logs \
    BENCH_APP_DATASETS_DIR=/data/datasets \
    BENCH_APP_CONNECTORS_YAML_DIR=/data/connectors \
    BENCH_APP_REVIEWS_DIR=/reviews \
    BENCH_APP_STDOUT_RUN_LOGS=1 \
    BENCH_APP_STDOUT_RUN_LOG_MAX_CHARS=20000

WORKDIR /app

ARG PIP_INDEX_URL=
ARG PIP_EXTRA_INDEX_URL=
ARG PIP_TRUSTED_HOST=

COPY requirements.bench_app.txt ./
RUN set -eux; \
    pip_args=""; \
    if [ -n "$PIP_INDEX_URL" ]; then pip_args="$pip_args --index-url $PIP_INDEX_URL"; fi; \
    if [ -n "$PIP_EXTRA_INDEX_URL" ]; then pip_args="$pip_args --extra-index-url $PIP_EXTRA_INDEX_URL"; fi; \
    if [ -n "$PIP_TRUSTED_HOST" ]; then pip_args="$pip_args --trusted-host $PIP_TRUSTED_HOST"; fi; \
    python -m pip install --no-cache-dir $pip_args --upgrade pip; \
    python -m pip install --no-cache-dir $pip_args -r requirements.bench_app.txt

COPY . .

RUN mkdir -p /data /reviews /app/.seed/reviews \
    && if [ -d bench_app/reviews ]; then cp -a bench_app/reviews/. /app/.seed/reviews/; fi \
    && chmod +x docker/bench_app-entrypoint.sh

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/live' % os.getenv('PORT','8090'), timeout=3).read(1)" || exit 1

ENTRYPOINT ["docker/bench_app-entrypoint.sh"]
