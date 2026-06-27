"""Deterministic per-column SQL comments from agent-provided column evidence.

Pure sqlglot AST. The SQL-writer agent emits, alongside ``sql_query``, a
``column_evidence`` list of ``{"table","column","role","reason"}`` entries that
justify every filter / metric / join column. This module renders a SEPARATE,
human-readable copy of the SQL with each justification attached inline as a
``/* role: reason */`` comment on the matching column.

The EXECUTABLE SQL is never mutated by the caller — this only produces a
display/explained copy. The function never raises and returns ``""`` when it
cannot produce a commented copy, so the caller simply falls back to the plain
SQL. Comments are inert in every SQL dialect (the rendered string also
re-parses), so the commented copy is itself valid SQL.
"""

import logging

import re

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

# Impala/Hive map to sqlglot's hive dialect; everything else passes through.
_DIALECT_ALIASES = {"impala": "hive", "hive2": "hive", "hiveql": "hive"}

_INLINE_COMMENT_RE = re.compile(r"/\*(.*?)\*/", re.DOTALL)


def _comments_to_line_end(sql_text):
    """Move sqlglot's inline ``/* ... */`` comments to the END of their line as a
    tab-separated ``-- ...`` trailing comment.

    sqlglot renders node comments inline (mid-expression); this rewrites the
    DISPLAY copy so every justification sits at the end of its line, one tab away
    from the SQL — e.g. ``WHERE msec_val > 0\\t-- filter: exclude 0 laps``.
    Multiple comments on one line are de-duplicated and joined with '; '."""
    out_lines = []
    for line in sql_text.split("\n"):
        notes = [m.group(1).strip() for m in _INLINE_COMMENT_RE.finditer(line)]
        if not notes:
            out_lines.append(line)
            continue
        stripped = _INLINE_COMMENT_RE.sub("", line)
        stripped = re.sub(r"[ \t]{2,}", " ", stripped)
        stripped = re.sub(r" +([),])", r"\1", stripped).rstrip()
        seen, uniq = set(), []
        for note in notes:
            if note and note not in seen:
                seen.add(note)
                uniq.append(note)
        comment = "-- " + "; ".join(uniq)
        if stripped.strip():
            out_lines.append(f"{stripped}\t{comment}")
        else:
            indent = line[: len(line) - len(line.lstrip())]
            out_lines.append(f"{indent}{comment}")
    return "\n".join(out_lines)


def _resolve_dialect(db_type):
    """Return a dialect name sqlglot can parse, or None for its permissive default."""
    name = (db_type or "").strip().lower()
    try:
        from sqlglot.dialects.dialect import Dialect
        available = set(Dialect.classes.keys())
    except Exception:  # pylint: disable=broad-except
        available = set()
    if name in available:
        return name
    alias = _DIALECT_ALIASES.get(name)
    if alias and alias in available:
        return alias
    return None


def _norm(name):
    """Lowercase + strip surrounding quotes/backticks from an identifier."""
    if name is None:
        return ""
    return str(name).strip().strip('"').strip("`").lower()


def _node_role(col_node):
    """Classify where a column sits in the query so the matching evidence role
    (metric vs filter vs join ...) can be picked for same-named columns."""
    if col_node.find_ancestor(exp.Sum, exp.Avg, exp.Min, exp.Max, exp.Count) is not None:
        return "metric"
    if col_node.find_ancestor(exp.Having) is not None:
        return "having"
    if col_node.find_ancestor(exp.Where) is not None:
        return "filter"
    if col_node.find_ancestor(exp.Join) is not None:
        return "join"
    if col_node.find_ancestor(exp.Group) is not None:
        return "group"
    if col_node.find_ancestor(exp.Order) is not None:
        return "order"
    return "select"


# Roles that are interchangeable when matching a position to an evidence entry.
_ROLE_ALIASES = {
    "metric": {"metric", "aggregate"},
    "aggregate": {"metric", "aggregate"},
    "having": {"having", "filter"},
    "filter": {"filter", "having"},
}


def _qualifier_matches(table, qualifier):
    """Best-effort match of an evidence table against a SQL alias/qualifier."""
    if not qualifier or not table:
        return not qualifier  # unqualified column matches any evidence table
    return table == qualifier or table.endswith(qualifier) or qualifier.endswith(table)


def _pick_candidate(cands, pos_role, qualifier):
    """Choose (role, reason) for one column occurrence.

    The column's POSITION (metric/filter/join/...) is the primary discriminator
    — it is read reliably from the AST. The qualifier only breaks ties among
    several same-role entries for the same column name (different tables)."""
    wanted = _ROLE_ALIASES.get(pos_role, {pos_role})
    pool = [c for c in cands if c[1] in wanted] or list(cands)
    if len(pool) > 1 and qualifier:
        for table, role, reason in pool:
            if _qualifier_matches(table, qualifier):
                return role, reason
    _table, role, reason = pool[0]
    return role, reason


def _index_evidence(column_evidence):
    """Map column_name_lower -> list of (table_lower, role, reason) with a reason."""
    by_col: dict[str, list] = {}
    for ev in column_evidence or []:
        if not isinstance(ev, dict):
            continue
        col = _norm(ev.get("column"))
        reason = str(ev.get("reason") or "").strip()
        if not col or not reason:
            continue
        # Keep the reason a single line and free of comment delimiters so it
        # renders cleanly both inline and as a trailing `-- ...` comment.
        reason = reason.replace("*/", " ").replace("/*", " ")
        reason = " ".join(reason.split())
        role = str(ev.get("role") or "").strip().lower()
        by_col.setdefault(col, []).append((_norm(ev.get("table")), role, reason))
    return by_col


def render_commented_sql(sql, column_evidence, db_type="postgres"):
    """Return a copy of ``sql`` with per-column justifications as inline comments.

    For every column in the SQL that has a matching ``column_evidence`` entry
    (by column name; the table/qualifier disambiguates when names collide), a
    ``/* role: reason */`` comment is attached. Returns ``""`` if there is no
    usable evidence, the SQL cannot be parsed, or nothing was annotated.
    """
    if not sql or not isinstance(sql, str):
        return ""
    by_col = _index_evidence(column_evidence)
    if not by_col:
        return ""

    dialect = _resolve_dialect(db_type)
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("render_commented_sql parse failed: %s", str(exc)[:200])
        return ""
    if parsed is None:
        return ""

    annotated = 0
    try:
        for col_node in parsed.find_all(exp.Column):
            cands = by_col.get(_norm(col_node.name))
            if not cands:
                continue
            qualifier = _norm(col_node.table)
            role, reason = _pick_candidate(
                cands, _node_role(col_node), qualifier,
            )
            label = f"{role}: {reason}" if role else reason
            existing = list(col_node.comments or [])
            if label in existing:
                continue
            col_node.comments = existing + [label]
            annotated += 1
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("render_commented_sql annotate failed: %s", str(exc)[:200])
        return ""

    if not annotated:
        return ""

    rendered = ""
    for kwargs in ({"dialect": dialect}, {}):
        try:
            rendered = parsed.sql(pretty=True, comments=True, **kwargs)
            break
        except Exception as exc:  # pylint: disable=broad-except
            logger.debug("render_commented_sql render failed: %s", str(exc)[:200])
    if not rendered:
        return ""
    # Reposition every inline /* ... */ to the end of its line as a tabbed `-- ...`.
    return _comments_to_line_end(rendered)
