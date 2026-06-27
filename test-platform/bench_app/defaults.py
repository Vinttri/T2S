"""Default datasets shipped with the benchmark app."""
from __future__ import annotations

import os
import re
from pathlib import Path

from leaderboard.benchmark import benchmark_cases_to_jsonl, parse_benchmark_file, parse_legacy_benchmark_markdown


APP_ROOT = Path(__file__).resolve().parents[1]
DM_MIS_SCORING_DSN_FALLBACK = ""

DEFAULT_DM_MIS_IMPALA_DATASETS = (
    {
        "id": "dm_mis_impala_1",
        "name": "dm_mis impala (1 вопрос)",
        "benchmark_file": "BENCHMARK_dm_mis_impala_1.jsonl",
        "question_count": 1,
    },
    {
        "id": "dm_mis_impala_3",
        "name": "dm_mis impala (3 вопроса)",
        "benchmark_file": "BENCHMARK_dm_mis_impala_3.jsonl",
        "question_count": 3,
    },
    {
        "id": "dm_mis_impala_10",
        "name": "dm_mis impala (10 вопросов)",
        "benchmark_file": "BENCHMARK_dm_mis_impala_10.jsonl",
        "question_count": 10,
    },
    {
        "id": "dm_mis_impala_all",
        "name": "dm_mis impala (54 вопроса, all)",
        "benchmark_file": "BENCHMARK_dm_mis_impala.jsonl",
        "question_count": 54,
    },
)


def _safe_dataset_stem(value: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip()).strip("._")
    return stem or "benchmark"


def _runtime_datasets_dir() -> Path:
    data_dir = Path(os.getenv("BENCH_APP_DATA_DIR", "bench_app/data"))
    return Path(os.getenv("BENCH_APP_DATASETS_DIR", str(data_dir / "datasets")))


def _dataset_path_candidates(path: Path, *, root: Path, datasets_dir: Path) -> list[Path]:
    candidates = [path]
    if path.name:
        candidates.extend([datasets_dir / path.name, root / path.name])
    if not path.is_absolute():
        candidates.extend([root / path, datasets_dir / path])
    seen: set[Path] = set()
    unique = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(candidate)
    return unique


def _materialize_jsonl_dataset_file(path: Path, *, root: Path, datasets_dir: Path) -> Path | None:
    """Copy a shipped/old JSONL dataset into the writable runtime dataset dir."""
    if path.suffix.lower() not in {".jsonl", ".ndjson", ".json"}:
        return None
    target = datasets_dir / f"{_safe_dataset_stem(path.stem)}.jsonl"
    if target.exists() and target.is_file():
        return target.resolve()
    for candidate in _dataset_path_candidates(path, root=root, datasets_dir=datasets_dir):
        if not candidate.exists() or not candidate.is_file():
            continue
        datasets_dir.mkdir(parents=True, exist_ok=True)
        cases = parse_benchmark_file(candidate)
        if not cases:
            continue
        target.write_text(benchmark_cases_to_jsonl(cases), encoding="utf-8")
        return target.resolve()
    return None


def _editable_copy_candidates(dataset: dict, *, datasets_dir: Path) -> list[Path]:
    safe_id = _safe_dataset_stem(dataset.get("id") or "")
    target_name = _safe_dataset_stem(f"{dataset.get('name') or dataset.get('id') or 'benchmark'}__{dataset.get('id') or 'benchmark'}")
    candidates = [datasets_dir / f"{target_name}.jsonl"]
    if safe_id:
        candidates.extend(sorted(datasets_dir.glob(f"*__{safe_id}.jsonl")))
    seen: set[Path] = set()
    unique = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(candidate)
    return unique


def _recover_user_edited_dataset_copy(store, dataset: dict) -> dict | None:
    """Recover an editable /data/datasets copy after an old startup reset path."""
    datasets_dir = _runtime_datasets_dir()
    if not datasets_dir.exists():
        return None
    for candidate in _editable_copy_candidates(dataset, datasets_dir=datasets_dir):
        if candidate.exists() and candidate.is_file():
            meta = dict(dataset.get("meta") or {})
            meta["format"] = "jsonl"
            meta["seeded_default"] = False
            meta["user_edited_dataset"] = True
            meta.setdefault("editable_copy_recovered", True)
            dataset = {**dataset, "benchmark_path": str(candidate.resolve()), "meta": meta}
            return store.save_dataset(dataset)
    return None


def _jsonl_replacement_for_markdown(path: Path, *, root: Path, datasets_dir: Path) -> Path | None:
    candidates = [
        path.with_suffix(".jsonl"),
        root / f"{path.stem}.jsonl",
        datasets_dir / f"{path.stem}.jsonl",
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return _materialize_jsonl_dataset_file(candidate, root=root, datasets_dir=datasets_dir) or candidate.resolve()

    if not path.exists() or not path.is_file():
        return None

    cases = parse_legacy_benchmark_markdown(path)
    if not cases:
        return None
    datasets_dir.mkdir(parents=True, exist_ok=True)
    target = datasets_dir / f"{_safe_dataset_stem(path.stem)}.jsonl"
    target.write_text(benchmark_cases_to_jsonl(cases), encoding="utf-8")
    return target.resolve()


def migrate_dataset_paths_to_jsonl(store, *, root: Path | None = None) -> list[dict]:
    """Move old dataset rows from host/image/Markdown paths to runtime JSONL paths.

    This only touches benchmark dataset rows. Markdown docs/reviews remain
    Markdown, but dataset storage must be JSONL.
    """
    root = root or APP_ROOT
    datasets_dir = _runtime_datasets_dir()
    changed = []
    for dataset in store.list_datasets():
        raw_path = dataset.get("benchmark_path") or ""
        path = Path(raw_path)
        suffix = path.suffix.lower()
        if suffix in {".md", ".markdown"}:
            replacement = _jsonl_replacement_for_markdown(path, root=root, datasets_dir=datasets_dir)
            meta_key = "migrated_from_markdown_path"
        elif suffix in {".jsonl", ".ndjson", ".json"}:
            recovered = _recover_user_edited_dataset_copy(store, dataset)
            if recovered is not None:
                changed.append(recovered)
                continue
            replacement = _materialize_jsonl_dataset_file(path, root=root, datasets_dir=datasets_dir)
            meta_key = "materialized_from_path"
        else:
            continue
        if replacement is None:
            continue
        if str(replacement) == raw_path:
            continue
        meta = dataset.get("meta") or {}
        meta["format"] = "jsonl"
        meta[meta_key] = raw_path
        dataset["benchmark_path"] = str(replacement)
        dataset["meta"] = meta
        changed.append(store.save_dataset(dataset))
    return changed


def _dm_mis_scoring_dsn_from_env() -> str | None:
    for name in ("BENCH_DM_MIS_IMPALA_DSN", "DM_MIS_IMPALA_DSN", "BENCH_DM_MIS_DSN", "DM_MIS_DSN"):
        value = os.getenv(name)
        if value and value.strip():
            return value.strip()
    return None


def _dm_mis_scoring_dsn(store) -> str:
    """Resolve scoring DSN for seeded dm_mis Impala datasets.

    Env wins; otherwise reuse an existing dm_mis Impala dataset DSN from the
    same store. We deliberately do not fall back to localhost because the live
    app runs in Docker, where localhost is the backend/worker container.
    """
    env_dsn = _dm_mis_scoring_dsn_from_env()
    if env_dsn:
        return env_dsn

    try:
        for dataset in store.list_datasets():
            if (
                dataset.get("db_id") == "dm_mis"
                and str(dataset.get("db_type") or "").lower() == "impala"
                and dataset.get("dsn")
            ):
                return dataset["dsn"]
    except Exception:  # noqa: BLE001
        pass

    return DM_MIS_SCORING_DSN_FALLBACK


def seed_default_datasets(store, *, root: Path | None = None) -> list[dict]:
    """Create or refresh default datasets without overwriting user-owned rows."""
    root = root or APP_ROOT
    existing_by_id = {d["id"]: d for d in store.list_datasets()}
    fallback_dsn = _dm_mis_scoring_dsn(store)
    env_dsn = _dm_mis_scoring_dsn_from_env()
    changed = []

    for spec in DEFAULT_DM_MIS_IMPALA_DATASETS:
        existing = existing_by_id.get(spec["id"])
        meta = existing.get("meta") or {} if existing else {}
        if existing and (meta.get("user_edited_dataset") or meta.get("editable_copy_from")):
            continue
        if existing and meta.get("seeded_default"):
            recovered = _recover_user_edited_dataset_copy(store, existing)
            if recovered is not None:
                changed.append(recovered)
                continue
        if existing and not meta.get("seeded_default"):
            continue
        source_path = (root / spec["benchmark_file"]).resolve()
        benchmark_path = _materialize_jsonl_dataset_file(source_path, root=root, datasets_dir=_runtime_datasets_dir())
        if benchmark_path is None:
            continue
        desired_dsn = env_dsn or (existing or {}).get("dsn") or fallback_dsn
        row = {
            "id": spec["id"],
            "name": spec["name"],
            "benchmark_path": str(benchmark_path),
            "db_id": "dm_mis",
            "dsn": desired_dsn,
            "db_type": "impala",
            "meta": {
                "seeded_default": True,
                "source": "bench_app.defaults",
                "question_count": spec["question_count"],
                "benchmark_file": spec["benchmark_file"],
                "format": "jsonl",
                "runtime_copy_from": str(source_path),
            },
        }
        if existing:
            comparable = {k: existing.get(k) for k in ("name", "benchmark_path", "db_id", "dsn", "db_type", "meta")}
            if comparable == {k: row.get(k) for k in comparable}:
                continue
        changed.append(store.save_dataset(row))

    return changed
