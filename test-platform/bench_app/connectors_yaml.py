"""Connectors are mirrored to human-readable YAML files under
`bench_app/data/connectors/` — one file per connector. The SQLite Store stays
the runtime source of truth, but every save/delete keeps the YAML in sync, and
on startup any YAML files are imported back into the Store (upsert). So you can
read, edit in git, or hand-author a connector as a YAML file.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

CONN_DIR = Path(os.getenv("BENCH_APP_CONNECTORS_YAML_DIR", str(Path(__file__).parent / "data" / "connectors")))
# fields we persist to YAML (skip created_at/updated_at bookkeeping)
FIELDS = ("id", "name", "db_id", "default_dialect", "method", "url", "headers",
          "body_template", "sql_extract", "timeout", "max_attempts", "retry_delay", "description")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-") or "conn"


def _path(c: dict) -> Path:
    return CONN_DIR / f"{_slug(c.get('name'))}__{c.get('db_id') or 'any'}__{(c.get('id') or '')[:8]}.yaml"


def export_connector(c: dict):
    CONN_DIR.mkdir(parents=True, exist_ok=True)
    doc = {k: c.get(k) for k in FIELDS}
    _path(c).write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


def delete_yaml(cid: str):
    for f in CONN_DIR.glob(f"*__{(cid or '')[:8]}.yaml"):
        try:
            f.unlink()
        except OSError:
            pass


def export_all(store):
    for c in store.list_connectors():
        export_connector(c)


def load_into_store(store):
    """Import YAML connector files into the Store (upsert). YAML is a valid way to
    define a connector: drop a file here and it appears after restart."""
    if not CONN_DIR.exists():
        return 0
    n = 0
    for f in sorted(CONN_DIR.glob("*.yaml")):
        try:
            doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if doc.get("name") and doc.get("url"):
                store.save_connector(doc)
                n += 1
        except Exception:
            pass
    return n
