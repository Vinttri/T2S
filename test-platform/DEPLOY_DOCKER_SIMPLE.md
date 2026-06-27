# Простое развертывание через Docker

Целевой деплой: обычный Docker, не Kubernetes.

## Что нужно в контуре

- Docker Engine уже установлен.
- Есть архив:

```text
leaderboard-bench-app_offline.tar.gz
```

Python-пакеты, frontend-библиотеки и начальные данные уже внутри архива. Интернет
для запуска не нужен.

## Быстрый запуск

Скопируйте архив на сервер в контуре и выполните:

```bash
tar -xzf leaderboard-bench-app_offline.tar.gz
cd leaderboard-bench-app_offline
cp .env.example .env
# отредактируйте .env под контур
./run-bench-app-offline.sh
```

Открыть:

```text
http://<server-host>:8090/
```

При первом запуске скрипт сам загрузит Docker image из `image.tar.gz` и создаст
Docker volumes:

```text
bench_app_data    -> /data
bench_app_reviews -> /reviews
```

Если нужны именно host-папки, отредактируйте `.env` перед запуском:

```env
DATA_HOST_DIR=/opt/leaderboard/data
REVIEWS_HOST_DIR=/opt/leaderboard/reviews
```

## Настроить внутреннюю LLM

Все настройки передаются через `.env` рядом с `run-bench-app-offline.sh`:

```env
LLM_BASE_URL=http://your-llm-gateway/v1
LLM_API_KEY=dummy-or-real-key
LLM_MODEL=your-model
LLM_JUDGE_MAX_RETRIES=2
LLM_JUDGE_RETRY_DELAY=2
```

Важно: `LLM_BASE_URL` должен быть OpenAI-compatible base URL. Приложение будет
вызывать:

```text
<LLM_BASE_URL>/chat/completions
```

Если gateway не требует ключ, всё равно задайте непустой `LLM_API_KEY=dummy`.
`LLM_JUDGE_MAX_RETRIES` задает число повторов при ошибке LLM-оценки,
`LLM_JUDGE_RETRY_DELAY` — паузу между повторами в секундах.

## База приложения

По умолчанию приложение использует SQLite в примонтированной папке `/data`:

```text
sqlite:////data/app.db
```

Это рекомендуемый вариант для старта.

Если нужно хранить runtime-данные приложения в Postgres, добавьте в
`.env`:

```env
BENCH_STORE_URL=postgresql://user:password@postgres-host:5432/bench_app
```

## Scoring Postgres для новых прогонов

Для просмотра уже сохраненных результатов scoring Postgres не нужен.

Для новых benchmark-прогонов должны быть доступны scoring databases:

```text
sports_events_large
dm_mis
cybermarket_pattern_large
```

Если scoring Postgres находится на том же сервере, что и Docker, отредактируйте
`.env`:

```env
HOST_NETWORK=1
```

Если scoring Postgres на другом хосте, поменяйте DSN датасетов в UI приложения
на адрес, доступный из контейнера.

## Проверка

После запуска:

```bash
docker ps
curl http://127.0.0.1:8090/api/datasets
```

Ожидаемо: API вернет список датасетов.

## Остановить

Если запускали через `./run-bench-app-offline.sh` в текущем терминале:

```bash
Ctrl+C
```

Если контейнер запущен в фоне:

```bash
docker rm -f bench-app
```

Данные останутся в Docker volume `bench_app_data`.

## Что Хранится В Mounts

```text
/data/app.db                 SQLite runtime store
/data/connectors/*.yaml      коннекторы, если включен YAML sync
/data/answers/*.json         сырые ответы коннекторов
/data/judged/*.levels.json   оценки L0-L4 от LLM
/data/runs/*.json            финальные результаты прогонов
/data/logs/*.jsonl           отдельный лог каждого запуска
/data/datasets/*.md          загруженные benchmark-файлы
/reviews/*.md                обзоры решений
```
