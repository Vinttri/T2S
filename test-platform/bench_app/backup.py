"""Periodic on-disk backups for the mounted benchmark state.

This intentionally stays simple: copy SQLite through its backup API and archive
append-only/runtime directories to tar.gz files under /data/backups by default.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import tarfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

from bench_app.logging_utils import configure_basic_json_logging

LOG = logging.getLogger("bench_app.backup")


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off", "disabled"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _data_dir() -> Path:
    return Path(os.getenv("BENCH_APP_DATA_DIR", "/data"))


def _backup_dir() -> Path:
    return Path(os.getenv("BENCH_BACKUP_DIR", str(_data_dir() / "backups")))


def _sqlite_path() -> Path | None:
    url = os.getenv("BENCH_STORE_URL", "sqlite:////data/app.db")
    if not url.startswith("sqlite:///"):
        return None
    raw = url[len("sqlite:///"):]
    return Path(unquote(urlsplit(raw).path or raw))


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def backup_sqlite(store_path: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"app-{_stamp()}.db"
    tmp = target.with_suffix(".db.tmp")
    src = sqlite3.connect(str(store_path), timeout=60)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, target)
    return target


def backup_tree(data_dir: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"data-{_stamp()}.tar.gz"
    tmp = target.with_suffix(".tar.gz.tmp")
    names = ("datasets", "connectors", "runs", "answers", "judged", "logs", "gold_cache")
    with tarfile.open(tmp, "w:gz") as tar:
        for name in names:
            path = data_dir / name
            if path.exists():
                tar.add(path, arcname=name)
    os.replace(tmp, target)
    return target


def prune_backups(root: Path, keep: int) -> int:
    if keep <= 0 or not root.exists():
        return 0
    removed = 0
    for sub in ("app-db", "data"):
        files = sorted((root / sub).glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            try:
                if old.is_dir():
                    shutil.rmtree(old)
                else:
                    old.unlink()
                removed += 1
            except FileNotFoundError:
                pass
    return removed


def run_backup_once() -> dict:
    data_dir = _data_dir()
    root = _backup_dir()
    keep = max(1, _env_int("BENCH_BACKUP_KEEP", 48))
    out: dict = {"ok": True, "backup_dir": str(root), "files": []}
    store_path = _sqlite_path()
    if store_path and store_path.exists():
        db_file = backup_sqlite(store_path, root / "app-db")
        out["files"].append(str(db_file))
    elif store_path:
        out["sqlite_warning"] = f"store file not found: {store_path}"
    else:
        out["sqlite_warning"] = "BENCH_STORE_URL is not sqlite; app.db backup skipped"
    data_file = backup_tree(data_dir, root / "data")
    out["files"].append(str(data_file))
    out["pruned"] = prune_backups(root, keep)
    return out


def main() -> None:
    configure_basic_json_logging(os.getenv("BENCH_BACKUP_LOG_LEVEL", "INFO"))
    if not _env_flag("BENCH_BACKUP_ENABLED", True):
        LOG.info("backup disabled")
        return
    interval = max(60, _env_int("BENCH_BACKUP_INTERVAL_S", 1800))
    while True:
        try:
            result = run_backup_once()
            LOG.info("backup completed", extra={"result": result})
        except Exception:  # noqa: BLE001
            LOG.exception("backup failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
