"""In-process cache for read-only SQL execution results."""

from __future__ import annotations

import copy
import hashlib
import logging
import re
import time
from collections import OrderedDict
from typing import Any, Callable

from api.config import Config

_CACHEABLE_SQL_RE = re.compile(r"^\s*(SELECT|WITH|SHOW|DESCRIBE|EXPLAIN)\b", re.IGNORECASE)
_QUERY_CACHE: "OrderedDict[str, tuple[float, list[dict[str, Any]]]]" = OrderedDict()


def _normal_sql(sql_query: str) -> str:
    return " ".join(str(sql_query or "").split())


def _cache_key(db_type: str, db_url: str, sql_query: str) -> str:
    key_material = f"{db_type}\n{db_url}\n{_normal_sql(sql_query)}"
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()


def _cacheable(sql_query: str) -> bool:
    return bool(_CACHEABLE_SQL_RE.match(sql_query or ""))


def execute_with_cache(
    execute_sql_func: Callable[[str], list[dict[str, Any]]],
    sql_query: str,
    *,
    db_url: str,
    db_type: str,
) -> list[dict[str, Any]]:
    """Execute SQL with a connector-independent in-memory cache."""
    # Deterministic LAST-LINE repair on EVERY executed query, path-independent
    # (main flow, blackboard pipeline, self-consistency execution all funnel here).
    # Fixes mechanically-broken SQL a generation path may emit — notably asymmetric
    # case-folding `col = LOWER('X')` (folds only the literal -> 0 rows) -> symmetric
    # `LOWER(col) = LOWER('X')`. Fail-safe: on any error the SQL is executed as-is.
    try:
        from api.sql_utils.gate_registry import run_gates as _rg, GateContext as _GC  # pylint: disable=import-outside-toplevel
        _gsql, _, _grep = _rg(sql_query, _GC(db_type=db_type))
        if _grep and _gsql:
            sql_query = _gsql
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    if not getattr(Config, "SQL_QUERY_CACHE_ENABLED", True) or not _cacheable(sql_query):
        return execute_sql_func(sql_query)

    key = _cache_key(db_type, db_url, sql_query)
    cached = _QUERY_CACHE.get(key)
    ttl = int(getattr(Config, "SQL_QUERY_CACHE_TTL_SECONDS", 10800))
    now = time.time()
    if cached:
        created_at, rows = cached
        if now - created_at <= ttl:
            _QUERY_CACHE.move_to_end(key)
            logging.info(
                "SQL query cache hit: db_type=%s key=%s rows=%d",
                db_type,
                key[:12],
                len(rows),
            )
            return copy.deepcopy(rows)
        _QUERY_CACHE.pop(key, None)

    rows = execute_sql_func(sql_query)
    if len(rows) <= int(getattr(Config, "SQL_QUERY_CACHE_MAX_ROWS", 1000)):
        _QUERY_CACHE[key] = (now, copy.deepcopy(rows))
        _QUERY_CACHE.move_to_end(key)
        max_entries = int(getattr(Config, "SQL_QUERY_CACHE_MAX_ENTRIES", 512))
        while len(_QUERY_CACHE) > max_entries:
            _QUERY_CACHE.popitem(last=False)
    return rows
