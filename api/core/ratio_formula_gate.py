"""Deterministic resolved-ratio enforcement gate (codex #3).

When the metric resolver bound a KB metric to a RATIO formula (numerator /
denominator, e.g. Suspicion Signal Density = keyword_match_count /
msg_count_total) but the generated SQL aggregates only ONE component of the
ratio — ``AVG(keyword_match_count)`` or even ``AVG(msg_count_total)`` — the answer
is wrong and, on a weak model, NON-DETERMINISTICALLY so (observed 9.885 / 50.647 /
0.084 across runs for the same question). Recall + prompt-injection of the formula
help but do not guarantee it. This gate makes it deterministic: if a resolved
ratio is NOT already computed in the SQL, it rewrites the aggregate that wraps a
ratio COMPONENT so it wraps the full bound ratio instead — math/structure of the
rest untouched.

General: it fires only when the resolver itself produced a division formula; it
reads only the resolved expression (already column-bound from the KB definition)
and the SQL AST. No table/column/domain names are hardcoded.
"""
from __future__ import annotations

import logging
import re

try:
    import sqlglot
    from sqlglot import exp
except Exception:  # pylint: disable=broad-exception-caught
    sqlglot = None
    exp = None

_JSON_TYPES = tuple(
    t for t in (getattr(exp, "JSONExtractScalar", None), getattr(exp, "JSONExtract", None))
    if t
) if exp else ()


def _leaf_keys(node) -> set:
    """Normalized references in a node: JSON-extract leaves if present (so two
    leaves of the same JSON column are distinguished), else plain column refs.
    Cast/whitespace-insensitive."""
    keys: set = set()
    if node is None:
        return keys
    js = list(node.find_all(_JSON_TYPES)) if _JSON_TYPES else []
    if js:
        for j in js:
            keys.add(re.sub(r"\s+", "", j.sql().lower()))
    else:
        for c in node.find_all(exp.Column):
            keys.add(re.sub(r"\s+", "", c.sql().lower()))
    return keys


def _ratio_div(expr_sql: str, dialect):
    """Parse a resolved expression and return its Div node (the ratio), or None."""
    try:
        node = sqlglot.parse_one(expr_sql, read=dialect)
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    if isinstance(node, exp.Div):
        return node
    return node.find(exp.Div) if node else None


def enforce_resolved_ratio(sql: str, resolved: list, db_type: str):
    """Return ``(sql, changed)``. Rewrites an aggregate over a ratio COMPONENT to
    aggregate the full resolved ratio, when the SQL does not already compute it."""
    if not sqlglot or not sql or not resolved:
        return sql, False
    try:
        from api.sql_utils.sql_gate import sqlglot_dialect  # pylint: disable=import-outside-toplevel
        dialect = sqlglot_dialect(db_type)
        root = sqlglot.parse_one(sql, read=dialect)
    except Exception:  # pylint: disable=broad-exception-caught
        return sql, False

    changed = False
    for r in resolved:
        expr_sql = str((r or {}).get("sql_expression") or "")
        if "/" not in expr_sql:
            continue
        ratio = _ratio_div(expr_sql, dialect)
        if ratio is None:
            continue
        num_keys = _leaf_keys(ratio.this)
        den_keys = _leaf_keys(ratio.args.get("expression"))
        comp_keys = num_keys | den_keys
        if not num_keys or not den_keys:
            continue
        # already computes this ratio? (some Div whose two sides carry the num/den leaves)
        already = False
        for d in root.find_all(exp.Div):
            if _leaf_keys(d.this) & num_keys and _leaf_keys(d.args.get("expression")) & den_keys:
                already = True
                break
        if already:
            continue
        # find an aggregate (AVG/SUM) wrapping ONLY ratio components, no division,
        # and rebind its argument to the full ratio.
        for agg in list(root.find_all(exp.Avg, exp.Sum)):
            arg = agg.this
            if arg is None or list(arg.find_all(exp.Div)):
                continue
            ak = _leaf_keys(arg)
            if ak and ak <= comp_keys and (ak & comp_keys):
                agg.set("this", ratio.copy())
                changed = True
        if changed:
            logging.info("Ratio-formula gate: enforced resolved ratio %s",
                         (r.get("name") or expr_sql)[:60])
    if not changed:
        return sql, False
    try:
        return root.sql(dialect=dialect), True
    except Exception:  # pylint: disable=broad-exception-caught
        return sql, False
