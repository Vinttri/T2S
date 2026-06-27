"""Dataset file helpers for uploaded benchmark JSONL files."""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from leaderboard.benchmark import benchmark_cases_to_jsonl, parse_benchmark_text


DATA_DIR = os.getenv("BENCH_APP_DATA_DIR", "bench_app/data")
DATASETS_DIR = os.getenv("BENCH_APP_DATASETS_DIR", os.path.join(DATA_DIR, "datasets"))
MAX_UPLOAD_BYTES = int(os.getenv("BENCH_APP_DATASET_UPLOAD_MAX_BYTES", str(2 * 1024 * 1024)))


def _slug(value: str) -> str:
    value = Path(value or "benchmark").stem
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return value or "benchmark"


def save_uploaded_benchmark(content: str, file_name: str, name: str = "",
                            *, directory: str | os.PathLike[str] | None = None) -> tuple[str, int]:
    suffix = Path(file_name or "").suffix.lower()
    if suffix not in {".jsonl", ".ndjson", ".json"}:
        raise ValueError("Датасеты benchmark можно загружать только как JSONL (.jsonl/.ndjson) или JSON, который будет сохранен как JSONL.")
    raw = (content or "").encode("utf-8")
    if not raw.strip():
        raise ValueError("Файл бенчмарка пустой.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError(f"Файл бенчмарка слишком большой: максимум {MAX_UPLOAD_BYTES} байт.")

    target_dir = Path(directory or DATASETS_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    stem = _slug(name or file_name)
    try:
        cases = parse_benchmark_text(content, source_name=file_name or "")
    except Exception as exc:
        raise ValueError(f"Не удалось прочитать benchmark JSONL: {exc}") from exc
    if not cases:
        raise ValueError("В benchmark-файле не найдено ни одного кейса JSONL.")
    path = target_dir / f"{stem}__{int(time.time() * 1000)}.jsonl"
    path.write_text(benchmark_cases_to_jsonl(cases), encoding="utf-8")
    return str(path.resolve()), len(cases)
