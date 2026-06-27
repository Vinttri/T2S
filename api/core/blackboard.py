"""Shared JSON "blackboard" contract for the Text2SQL agent pipeline.

A single dict flows through every agent; each one adds / expands / trims it.
It carries the user request, the selected tables+columns (with descriptions,
sample values, roles), evidences, conditions, the selected business rules, and
top-up requests. Agents that need more schema append a missing_tables_request /
missing_columns_request; the SchemaTopUp agent fulfils it by retrieving more and
MERGING (never dropping what is already selected or used).

Design constraints honoured here:
  * NO hardcoded dm_mis table/column names anywhere.
  * Reuse the existing find() table-list format `[name, description,
    foreign_keys, columns]` (columns are dicts with columnName|name,
    keyType|key_type, type, description, nullable, sample_values,
    references_table, references_column ...). The raw table_info objects are
    kept internally so the legacy AnalysisAgent.get_analysis() receives exactly
    what it expects (round-trip fidelity), while the JSON mirror is what agents
    read and mutate.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

SCHEMA_VERSION = "qw.blackboard/1.0"

# "FK→ table(column)" annotations live in column-description prose; the structured
# foreign_keys field carries the same as dicts/strings. Both name the JOIN-target
# tables the planner needs but which rank below the top-N.
_FK_REF_RE = re.compile(r"FK→\s*([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?)\(")
# Full "FK→ table(column)" with the referenced column, to populate the STRUCTURED
# references_table / references_column fields (which downstream integrity checks
# and the binding echo rely on) from the same prose the model already reads.
_FK_FULL_RE = re.compile(r"FK→\s*([A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?)\(([^)]+)\)")


def _short(name: str) -> str:
    return str(name or "").split(".")[-1].lower()


def _referenced_table_names(table_info: list) -> set[str]:
    """Short names of every FK-target table referenced by this table (from its
    foreign_keys field and its columns' FK→ description prose). Schema-agnostic."""
    names: set[str] = set()
    text_parts: list[str] = []
    fk = table_info[2] if len(table_info) > 2 else None
    if isinstance(fk, str):
        text_parts.append(fk)
    elif isinstance(fk, (list, tuple)):
        for it in fk:
            if isinstance(it, dict):
                rt = (it.get("referenced_table") or it.get("references_table")
                      or it.get("ref_table") or it.get("table"))
                if rt:
                    names.add(_short(rt))
            else:
                text_parts.append(str(it))
    cols = table_info[3] if len(table_info) > 3 else None
    for c in (cols or []):
        if not isinstance(c, dict):
            continue
        text_parts.append(str(c.get("description") or ""))
        rt = c.get("references_table") or c.get("referencesTable")
        if rt:
            names.add(_short(rt))
    for txt in text_parts:
        for m in _FK_REF_RE.finditer(txt):
            names.add(_short(m.group(1)))
    return names


# --- column/table helpers (tolerant of both naming conventions) -------------
def col_name(column: dict) -> str:
    return str(column.get("columnName") or column.get("name") or "")


def col_key_type(column: dict) -> str:
    return str(
        column.get("keyType")
        or column.get("key_type")
        or column.get("key")
        or ""
    )


def table_name(table_info: list) -> str:
    try:
        return str(table_info[0] or "")
    except Exception:  # noqa: BLE001
        return ""


def _column_samples(column: dict) -> list:
    """find() returns samples under `sampleValues` (camelCase), often as a JSON
    string; the legacy schema path uses `sample_values`. Normalize to a list."""
    raw = (column.get("sample_values")
           or column.get("sampleValues")
           or column.get("sample_value") or [])
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            import json as _json
            parsed = _json.loads(raw)
            return parsed if isinstance(parsed, list) else [str(parsed)]
        except Exception:  # noqa: BLE001
            return [raw]
    return list(raw) if isinstance(raw, (list, tuple)) else []


def _column_to_bb(column: dict, table: str = "") -> dict:
    """Project a raw find() column dict into the blackboard JSON view.

    Every column carries an explicit ``table`` back-reference and a canonical
    ``ref`` (``table.column``) so it stays self-describing even when handled
    apart from its parent table — that is how any agent or tool response can
    tell *which* table a field belongs to and where its description came from.
    """
    name = col_name(column)
    desc = column.get("description") or ""
    # Structured FK: prefer an explicit field; otherwise recover it from the
    # "FK→ table(column)" prose graph.py appends to the description, so the
    # integrity check / binding echo / `-> FK` render all work (the model
    # already reads the prose; this just mirrors it into structured fields).
    ref_table = (column.get("references_table") or column.get("referencesTable"))
    ref_column = (column.get("references_column") or column.get("referencesColumn"))
    if not ref_table:
        m = _FK_FULL_RE.search(desc)
        if m:
            ref_table = m.group(1)
            ref_column = (m.group(2) or "").strip() or None
    return {
        "name": name,
        "table": str(table or ""),                 # parent-table back-reference
        "ref": f"{table}.{name}" if table else name,  # canonical identifier
        "type": (column.get("type") or column.get("dataType")
                 or column.get("data_type")),
        "description": desc,
        "nullable": column.get("nullable"),
        # Data-grounded NULL-ness + range fact (from profiling); overrides the
        # declared nullability for the IS NULL vs `> D` decision.
        "data_profile": column.get("data_profile") or "",
        "key_type": col_key_type(column),
        "sample_values": _column_samples(column),
        "references_table": ref_table,
        "references_column": ref_column,
        "role": None,            # measure | date_filter | filter | key | label | near_miss
        "status": "selected",    # selected | candidate | rejected
    }


def _table_to_bb(table_info: list, source: str, rank: int | None) -> dict:
    name, description, foreign_keys, columns = (list(table_info) + [None] * 4)[:4]
    tname = str(name or "")
    return {
        "name": tname,
        "rank": rank,
        "source": source,                # initial_find | topup:<id>
        "status": "selected",            # selected | candidate | rejected
        "description": str(description or ""),
        "foreign_keys": foreign_keys or [],
        "rationale": None,
        "columns": [
            _column_to_bb(c, tname) for c in (columns or []) if isinstance(c, dict)
        ],
    }


def column_fk(column: dict) -> str | None:
    """Render a column's foreign-key target as ``ref_table.ref_col`` or None."""
    ref_t = column.get("references_table")
    if not ref_t:
        return None
    ref_c = column.get("references_column")
    return f"{ref_t}.{ref_c}" if ref_c else str(ref_t)


def column_binding(table: str, column: dict) -> dict:
    """Self-describing binding for a resolved column — the canonical echo a tool
    returns so the model SEES exactly which table.column it touched together
    with the metadata bound to it (type, description, nullability, key, FK,
    samples, role, status). Resolved from canonical blackboard state, never from
    the model's raw arguments."""
    name = col_name(column)
    return {
        "ref": column.get("ref") or (f"{table}.{name}" if table else name),
        "table": column.get("table") or str(table or ""),
        "column": name,
        "type": column.get("type"),
        "description": column.get("description") or "",
        "nullable": column.get("nullable"),
        "data_profile": column.get("data_profile") or "",
        "key_type": column.get("key_type") or "",
        "fk": column_fk(column),
        "samples": list(column.get("sample_values") or [])[:5],
        "role": column.get("role"),
        "status": column.get("status"),
    }


# --- construction -----------------------------------------------------------
def new_blackboard(
    user_query: str,
    db_type: str | None,
    graph_id: str,
    chat_history: list | None = None,
    initial_limit: int = 12,
    max_topups: int = 2,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "request": {
            "user_query": user_query,
            "chat_history": chat_history or [],
            "db_type": (db_type or "").lower() or None,
            "graph_id": graph_id,
        },
        "retrieval": {
            "initial_table_limit": initial_limit,
            "ranking_policy": "current_find_ranking",
            "topup_count": 0,
            "max_topups": max_topups,
        },
        "tables": [],
        "evidences": [],
        "conditions": [],
        "joins": [],
        "selected_business_rules": [],
        "missing_tables_request": [],
        "missing_columns_request": [],
        # Generated SQL is kept in the blackboard so later user remarks refine
        # from accumulated state rather than starting over.
        "sql": {"draft": None, "final": None, "tables_used": [], "columns_used": []},
        # User remarks/corrections on the answer; appended across turns and fed
        # back into generation so the pipeline works from the prior SQL + remark.
        "user_feedback": [],
        "validation": {"gate": [], "semantic": [], "execution": []},
        "trace": [],
        # internal: raw find() table_info objects, keyed by lowercase name, so
        # the legacy generator path receives byte-identical structures.
        "_table_infos": {},
    }


def from_find_tables(
    user_query: str,
    table_infos: list,
    db_type: str | None,
    graph_id: str,
    initial_limit: int = 12,
    max_topups: int = 2,
    chat_history: list | None = None,
) -> dict:
    """Build the initial blackboard from the (already ranked) find() output.

    Keeps the first `initial_limit` ranked tables. Ranking is the current
    find() ranking — we only truncate.
    """
    bb = new_blackboard(
        user_query, db_type, graph_id, chat_history, initial_limit, max_topups
    )
    valid = [t for t in (table_infos or []) if isinstance(t, (list, tuple))]
    kept = valid[:initial_limit]
    kept_names = {_short(table_name(t)) for t in kept}

    # Preserve JOIN-target tables: the table-finder's FK expansion deliberately
    # retrieves the dimension/classification tables the top-N reference (e.g. an
    # entity-type table needed to filter «юридические лица»), but they often rank
    # just below the cut. Truncating to top-N alone would drop them and leave the
    # planner unable to join — so add any FK-target of a kept table that find()
    # already returned in the tail. Bounded so the planner is not diluted.
    wanted: set[str] = set()
    for t in kept:
        wanted |= _referenced_table_names(t)
    wanted -= kept_names
    max_fk_extra = max(0, initial_limit)  # safety cap
    fk_extra = []
    for t in valid[initial_limit:]:
        if len(fk_extra) >= max_fk_extra:
            break
        if _short(table_name(t)) in wanted:
            fk_extra.append(t)

    selected = kept + fk_extra
    for rank, t in enumerate(selected):
        nm = table_name(t)
        if not nm:
            continue
        bb["_table_infos"][nm.lower()] = list(t)
        src = "initial_find" if rank < len(kept) else "fk_target"
        bb["tables"].append(_table_to_bb(t, src, rank))
    bb["trace"].append(
        {"agent": "schema_initializer", "action": "created_initial_blackboard",
         "added_tables": len(bb["tables"]), "initial_limit": initial_limit,
         "fk_target_tables": [table_name(t) for t in fk_extra]}
    )
    return bb


# --- adapters back to the legacy generator path -----------------------------
def selected_legacy_tables(bb: dict) -> list:
    """Return raw find() table_info objects for the SELECTED tables, in the
    blackboard's current order. This is what AnalysisAgent.get_analysis() and
    _format_schema() consume unchanged."""
    out = []
    for t in bb.get("tables", []):
        if t.get("status") != "selected":
            continue
        raw = bb.get("_table_infos", {}).get(str(t.get("name", "")).lower())
        if raw is not None:
            out.append(raw)
    return out


def all_table_names(bb: dict) -> set[str]:
    return {str(t.get("name", "")).lower() for t in bb.get("tables", [])}


# --- top-up (non-destructive merge) -----------------------------------------
def merge_topup_tables(bb: dict, new_table_infos: list, request_id: str) -> list[str]:
    """Add retrieved tables to the blackboard WITHOUT removing or reordering
    anything already present. Returns the list of newly added table names."""
    existing = all_table_names(bb)
    added: list[str] = []
    for t in (new_table_infos or []):
        if not isinstance(t, (list, tuple)):
            continue
        nm = table_name(t)
        low = nm.lower()
        if not nm or low in existing:
            continue
        bb["_table_infos"][low] = list(t)
        bb["tables"].append(_table_to_bb(t, f"topup:{request_id}", None))
        existing.add(low)
        added.append(nm)
    bb["retrieval"]["topup_count"] = bb["retrieval"].get("topup_count", 0) + 1
    bb["trace"].append(
        {"agent": "schema_topup_agent", "action": "added_missing_schema",
         "request_id": request_id, "added_tables": added}
    )
    return added


def can_topup(bb: dict) -> bool:
    r = bb.get("retrieval", {})
    return int(r.get("topup_count", 0)) < int(r.get("max_topups", 0))


def add_missing_tables_request(bb: dict, requested_by: str, semantic_hint: str,
                               reason: str = "", required: bool = True) -> str:
    rid = f"miss_t_{len(bb['missing_tables_request']) + 1}"
    bb["missing_tables_request"].append(
        {"id": rid, "requested_by": requested_by, "reason": reason,
         "semantic_hint": semantic_hint, "required": required}
    )
    return rid


# --- business rules ---------------------------------------------------------
def set_selected_rules(bb: dict, rules: list[dict]) -> None:
    bb["selected_business_rules"] = list(rules or [])
    bb["trace"].append(
        {"agent": "business_rule_rag_agent", "action": "selected_rules",
         "count": len(bb["selected_business_rules"])}
    )


def selected_rules_as_text(bb: dict) -> str:
    """Render the selected business rules as the user_rules_spec text the
    generator prompt already knows how to consume."""
    rules = bb.get("selected_business_rules") or []
    if not rules:
        return ""
    lines = ["User Rules & Specifications", ""]
    for i, r in enumerate(rules, 1):
        title = str(r.get("title") or "").strip()
        text = str(r.get("text") or "").strip()
        head = f"{i}. {title}".rstrip(". ") + ". " if title else f"{i}. "
        lines.append((head + text).strip())
    return "\n".join(lines)


# --- generated SQL + user remarks (kept in the blackboard) -------------------
def set_sql_draft(bb: dict, sql: str | None) -> None:
    bb.setdefault("sql", {})["draft"] = sql or None
    bb["trace"].append({"agent": "analysis_sql_agent", "action": "sql_draft"})


def set_sql_final(bb: dict, sql: str | None, tables_used: list | None = None,
                  columns_used: list | None = None) -> None:
    s = bb.setdefault("sql", {})
    s["final"] = sql or None
    if tables_used is not None:
        s["tables_used"] = list(tables_used)
    if columns_used is not None:
        s["columns_used"] = list(columns_used)
    bb["trace"].append({"agent": "pipeline", "action": "sql_final"})


def add_user_feedback(bb: dict, text: str, role: str = "user",
                      prior_sql: str | None = None) -> None:
    """Append a user remark/correction. `prior_sql` records the SQL the remark
    is about, so generation can refine from (prior SQL + remark)."""
    text = str(text or "").strip()
    if not text:
        return
    bb.setdefault("user_feedback", []).append(
        {"text": text, "role": role, "about_sql": prior_sql}
    )


def seed_user_feedback(bb: dict, chat_history: list | None,
                       result_history: list | None = None) -> None:
    """Seed prior-turn user messages as feedback so the JSON reflects the whole
    conversation. The newest item in chat_history is the current question and is
    NOT added here (it lives in request.user_query)."""
    prior = list(chat_history or [])[:-1] if chat_history else []
    last_sql = None
    if result_history:
        for r in reversed(result_history):
            cand = (r.get("sql_query") or r.get("sql")) if isinstance(r, dict) else None
            if cand:
                last_sql = cand
                break
    for msg in prior:
        text = msg if isinstance(msg, str) else (
            msg.get("content") or msg.get("text") if isinstance(msg, dict) else ""
        )
        add_user_feedback(bb, text, role="user", prior_sql=last_sql)


def feedback_as_text(bb: dict) -> str:
    fb = bb.get("user_feedback") or []
    if not fb:
        return ""
    lines = ["Prior user remarks / corrections (refine the SQL accordingly):"]
    for f in fb:
        about = f.get("about_sql")
        lines.append(f"- {f.get('text')}")
        if about:
            lines.append(f"  (regarding SQL: {str(about)[:300]})")
    return "\n".join(lines)


# --- referential integrity of the assembled blackboard ----------------------
def _ci_find_table(bb: dict, name: str) -> dict | None:
    low = str(name or "").lower()
    if not low:
        return None
    for t in bb.get("tables", []) or []:
        if str(t.get("name", "")).lower() == low:
            return t
    return None


def _ci_find_column(bb: dict, table: str, column: str) -> dict | None:
    t = _ci_find_table(bb, table)
    if t is None:
        return None
    low = str(column or "").lower()
    for c in t.get("columns", []) or []:
        if col_name(c).lower() == low:
            return c
    return None


def _is_pk(column: dict) -> bool:
    kt = str(column.get("key_type") or "").upper()
    return ("PRI" in kt) or ("PK" in kt) or ("PRIMARY" in kt)


def integrity_check(bb: dict) -> list[dict]:
    """Verify the assembled blackboard is referentially coherent so an agent can
    always tell which table a field belongs to, where a description came from,
    and that every relationship/PK/selected reference resolves.

    Schema-agnostic (no hardcoded names). Returns a list of issue dicts
    ``{check, severity, table, column, message}``. Severities: ``error`` for a
    dangling reference the pipeline relies on; ``warn`` for an FK whose target
    is absent; ``info`` for a missing PK marker.
    """
    issues: list[dict] = []

    def add(check: str, severity: str, table: str, column: str, message: str) -> None:
        issues.append({"check": check, "severity": severity, "table": table,
                       "column": column, "message": message})

    # 1. Per-column: back-reference consistency, FK targets, PK presence.
    for t in bb.get("tables", []) or []:
        tname = str(t.get("name", ""))
        has_pk = False
        for c in t.get("columns", []) or []:
            cname = col_name(c)
            # back-reference must match the parent table (self-describing column)
            back = str(c.get("table") or "")
            if back and back.lower() != tname.lower():
                add("column_table_mismatch", "error", tname, cname,
                    f"column.table back-reference '{back}' != parent table '{tname}'")
            if _is_pk(c):
                has_pk = True
            # FK target must resolve to a real table (and column when named)
            ref_t = c.get("references_table")
            if ref_t:
                tgt = _ci_find_table(bb, ref_t)
                if tgt is None:
                    add("fk_target_table_absent", "warn", tname, cname,
                        f"FK -> {column_fk(c)} but table '{ref_t}' is not in the "
                        "blackboard (retrieve it via top-up if the join is needed)")
                else:
                    ref_c = c.get("references_column")
                    if ref_c and _ci_find_column(bb, ref_t, ref_c) is None:
                        add("fk_target_column_absent", "warn", tname, cname,
                            f"FK -> {column_fk(c)} but column '{ref_c}' is absent "
                            f"on '{ref_t}'")
        if t.get("status") == "selected" and not has_pk:
            add("no_pk_marked", "info", tname, "",
                "no primary-key column is marked on this selected table "
                "(grain/fan-out cannot be checked deterministically)")

    # 2. Every referenced table.column in the plan must resolve to a real column.
    def check_ref(table: str, column: str, where: str) -> None:
        if table and column and _ci_find_column(bb, table, column) is None:
            add("dangling_reference", "error", str(table), str(column),
                f"{where} references {table}.{column}, which is not present in the "
                "blackboard")

    measure = bb.get("measure") or {}
    if measure.get("column"):
        check_ref(measure.get("table", ""), measure.get("column", ""), "measure")
    for d in (bb.get("grain") or {}).get("dimensions", []) or []:
        check_ref(d.get("table", ""), d.get("column", ""), "grain dimension")
    for c in bb.get("conditions", []) or []:
        check_ref(c.get("table", ""), c.get("column", ""), "condition")
    for j in bb.get("joins", []) or []:
        for k in j.get("keys", []) or []:
            check_ref(j.get("left_table", ""), k.get("left_column", ""), "join key (left)")
            check_ref(j.get("right_table", ""), k.get("right_column", ""), "join key (right)")

    return issues
