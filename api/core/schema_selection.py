"""Structured selected/removed schema sidecar for the legacy Text2SQL path.

The RAG/prune stage (`AnalysisAgent._prune_schema_for_prompt`) keeps only the
strongest columns per table IN THE GENERATION PROMPT and drops the rest. This
module mirrors that decision into a JSON sidecar that agents/UI can read without
changing what the LLM sees:

  schema_json = {
    "tables": [{
      "name", "description", "status": "selected"|"removed",
      "columns": [{
        "table", "column", "ref", "type", "nullable", "key_type",
        "description", "samples", "references_table", "references_column",
        "status": "selected"|"removed",
        "role": "key|measure|date_filter|filter|label"|None,
        "role_source": "heuristic"|"llm"|"promoted",
        "evidence_source": "schema_linker"|"schema_pruner"|"llm"|"promoted",
        "reason": "...",                       # LLM justification, when known
        # removed-only deterministic pruning evidence:
        "prune_score": int, "prune_signals": [..], "prune_reason": "..."
      }]
    }],
    "counts": {"tables", "columns_selected", "columns_removed"}
  }

Design (reviewed): REMOVED columns are NEVER injected back into the generation
prompt by default — they exist only for search/promotion. Their evidence is
DETERMINISTIC (a prune score + the signals that matched + a reason) with a
HEURISTIC role; a true LLM ``reason`` is written only once a column is
*promoted* into the selected set. Pure functions, no LLM calls, never raise.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

# --- tolerant accessors (find()/graph columns use mixed conventions) --------
_STOPWORDS = {
    "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "by", "with",
    "is", "are", "be", "as", "at", "from", "per", "all", "any", "each", "что",
    "the", "id", "code", "name",
}


def _norm(value: Any) -> str:
    return str(value or "").strip().strip('"').strip("`").lower()


# Alias / lookup / cross-reference table naming conventions (alternate-value
# tables). Their columns are de-prioritised in column ranking — the primary
# entity table is the canonical source. General convention, not a per-DB literal.
_ALIAS_TABLE_RE = re.compile(r"(?:^|_)(alias|lookup|xref)", re.IGNORECASE)


def _tokens(text: Any) -> set[str]:
    return {
        tok for tok in re.split(r"[^a-z0-9а-яё]+", str(text or "").lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def _col_name(col: dict) -> str:
    return str(col.get("columnName") or col.get("name") or "")


def _col_desc(col: dict) -> str:
    return str(col.get("description") or "")


def _col_type(col: dict) -> str:
    return str(col.get("type") or col.get("dataType") or col.get("data_type") or "")


def _col_key_type(col: dict) -> str:
    return str(col.get("keyType") or col.get("key_type") or col.get("key") or "")


def _col_ref_table(col: dict) -> str | None:
    return col.get("references_table") or col.get("referencesTable") or None


def _col_ref_column(col: dict) -> str | None:
    return col.get("references_column") or col.get("referencesColumn") or None


def _col_samples(col: dict) -> list:
    raw = (col.get("sample_values") or col.get("sampleValues")
           or col.get("sample_value") or [])
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


def _table_name(table_info: list) -> str:
    try:
        return str(table_info[0] or "")
    except Exception:  # noqa: BLE001
        return ""


def table_grain_lines(tables: list) -> list[str]:
    """One line per table stating its ROW GRAIN, derived from the PRIMARY-KEY
    columns: ``<table>: one row per <pk cols>``. Lets an agent distinguish a
    detail / fact table (fine grain) from a snapshot / summary / standings table
    (coarse grain) and pick the source whose grain matches the question (e.g. a
    "per event" / "after each <event>" request needs the per-event detail table,
    not a pre-aggregated snapshot). Graph-driven (PK metadata), fully general —
    any DB. Tables with no detectable PK are skipped. This is RAG/schema delivery
    (the DB's own structure), not engine-prompt text.
    """
    out: list[str] = []
    for t in tables or []:
        if not isinstance(t, (list, tuple)) or len(t) < 4:
            continue
        tname = str(t[0] or "")
        pk: list[str] = []
        for c in (t[3] or []):
            if not isinstance(c, dict):
                continue
            kt = str(_col_key_type(c)).upper()
            desc = str(_col_desc(c)).upper()
            if "PRIMARY" in kt or kt in ("PK", "P") or "(PRIMARY KEY)" in desc:
                nm = _col_name(c)
                if nm:
                    pk.append(nm)
        if tname and pk:
            out.append(f"{tname}: one row per {', '.join(pk)}")
    return out


def _table_columns(table_info: list) -> list[dict]:
    cols = table_info[3] if isinstance(table_info, (list, tuple)) and len(table_info) > 3 else None
    return [c for c in (cols or []) if isinstance(c, dict)]


# --- heuristics: role + pruning signals (schema-agnostic, no domain names) ---
_NUMERIC_TYPE_RE = re.compile(r"int|numeric|decimal|float|double|real|money|number", re.I)
_DATE_TYPE_RE = re.compile(r"date|timestamp", re.I)
# Tokens that mark a DATE/period column. NOTE: "time" is deliberately excluded —
# it appears in durations ("lap time in milliseconds"), which are measures.
_DATE_TOK = {"date", "year", "month", "day", "timestamp", "datetime", "season",
             "period", "week", "quarter"}
_FILTER_TOK = {"code", "type", "status", "category", "kind", "group", "class", "flag", "state"}
_LABEL_TOK = {"name", "title", "label", "description", "text", "comment", "note"}


def _is_keyish(col: dict, fk_names: set[str]) -> bool:
    kt = _col_key_type(col).upper()
    if any(m in kt for m in ("PK", "PRI", "PRIMARY", "FK", "FOREIGN", "MUL", "UNI")):
        return True
    if _col_ref_table(col):
        return True
    return _norm(_col_name(col)) in fk_names


def guess_role(col: dict, fk_names: set[str] | None = None) -> str | None:
    """Heuristic column role: key > date_filter > measure > filter > label.

    Conservative and advisory only (the LLM overwrites the role for columns it
    actually uses). Date detection prefers the TYPE, then date-ish NAME/desc
    tokens; numeric columns default to ``measure``."""
    fk_names = fk_names or set()
    toks = _tokens(_col_name(col)) | _tokens(_col_desc(col))
    ctype = _col_type(col)
    if _is_keyish(col, fk_names):
        return "key"
    if _DATE_TYPE_RE.search(ctype):
        return "date_filter"
    if _NUMERIC_TYPE_RE.search(ctype):
        return "date_filter" if (toks & _DATE_TOK) else "measure"
    if toks & _DATE_TOK:
        return "date_filter"
    if toks & _FILTER_TOK:
        return "filter"
    if toks & _LABEL_TOK:
        return "label"
    return None


_SIGNAL_WEIGHTS = {
    "name_match": 40,
    "rule_match": 30,
    "description_match": 25,
    "sample_match": 20,
    "key_or_fk": 10,
}


def _column_signals(col: dict, query_tokens: set[str], rule_ids: set[str],
                    fk_names: set[str]) -> list[str]:
    signals: list[str] = []
    name_toks = _tokens(_col_name(col))
    if name_toks & query_tokens:
        signals.append("name_match")
    if _norm(_col_name(col)) in rule_ids or (name_toks & rule_ids):
        signals.append("rule_match")
    if _tokens(_col_desc(col)) & query_tokens:
        signals.append("description_match")
    sample_toks: set[str] = set()
    for s in _col_samples(col)[:8]:
        sample_toks |= _tokens(s)
    if sample_toks & query_tokens:
        signals.append("sample_match")
    if _is_keyish(col, fk_names):
        signals.append("key_or_fk")
    return signals


def _signal_score(signals: Iterable[str]) -> int:
    return min(100, sum(_SIGNAL_WEIGHTS.get(s, 0) for s in signals))


def _prune_reason(signals: list[str], score: int) -> str:
    if score == 0:
        return "weak_query_match"
    if "name_match" in signals or "description_match" in signals:
        return "lower_score_than_selected"
    return "below_table_budget"


# --- FK column-name set for a table (mirror of blackboard helper) ------------
def _fk_names_for_table(table_info: list) -> set[str]:
    names: set[str] = set()
    fk = table_info[2] if isinstance(table_info, (list, tuple)) and len(table_info) > 2 else None
    if isinstance(fk, (list, tuple)):
        for it in fk:
            if isinstance(it, dict):
                col = (it.get("column") or it.get("source_column")
                       or it.get("from_column") or it.get("columnName"))
                if col:
                    names.add(_norm(col))
    for c in _table_columns(table_info):
        if _col_ref_table(c):
            names.add(_norm(_col_name(c)))
    return names


def _project_column(col: dict, table: str, status: str, query_tokens: set[str],
                    rule_ids: set[str], fk_names: set[str]) -> dict:
    name = _col_name(col)
    out = {
        "table": table,
        "column": name,
        "ref": f"{table}.{name}" if table else name,
        "type": _col_type(col) or None,
        "nullable": col.get("nullable"),
        "key_type": _col_key_type(col),
        "description": _col_desc(col),
        "samples": _col_samples(col)[:5],
        "references_table": _col_ref_table(col),
        "references_column": _col_ref_column(col),
        "status": status,
        "role": guess_role(col, fk_names),
        "role_source": "heuristic",
        "evidence_source": "schema_linker" if status == "selected" else "schema_pruner",
        "reason": "",
    }
    if status == "removed":
        signals = _column_signals(col, query_tokens, rule_ids, fk_names)
        score = _signal_score(signals)
        out["prune_signals"] = signals
        out["prune_score"] = score
        out["prune_reason"] = _prune_reason(signals, score)
    return out


def build_schema_json(combined_tables: list, selected_tables: list,
                      user_query: str = "", user_rules_spec: str | None = None) -> dict:
    """Build the selected/removed sidecar from the full candidate set
    (``combined_tables``) and the prompt-pruned set (``selected_tables``).

    A column is ``selected`` iff the prune kept it (it is present on its table in
    ``selected_tables``); every other candidate column is ``removed``. Table and
    column descriptions come from the FULL candidate set (not the compacted
    prompt copy). Never raises.
    """
    try:
        query_tokens = _tokens(user_query)
        rule_ids = _tokens(user_rules_spec or "")

        # name -> set(selected column names) from the pruned (prompt) schema
        selected_cols: dict[str, set[str]] = {}
        for t in (selected_tables or []):
            if not isinstance(t, (list, tuple)):
                continue
            selected_cols[_norm(_table_name(t))] = {
                _norm(_col_name(c)) for c in _table_columns(t)
            }

        tables_out: list[dict] = []
        n_selected = n_removed = 0
        for t in (combined_tables or []):
            if not isinstance(t, (list, tuple)):
                continue
            tname = _table_name(t)
            tlow = _norm(tname)
            sel_set = selected_cols.get(tlow, set())
            fk_names = _fk_names_for_table(t)
            cols_out: list[dict] = []
            for c in _table_columns(t):
                status = "selected" if _norm(_col_name(c)) in sel_set else "removed"
                cols_out.append(
                    _project_column(c, tname, status, query_tokens, rule_ids, fk_names)
                )
                if status == "selected":
                    n_selected += 1
                else:
                    n_removed += 1
            tdesc = ""
            if isinstance(t, (list, tuple)) and len(t) > 1:
                tdesc = str(t[1] or "")
            tables_out.append({
                "name": tname,
                "description": tdesc,
                "status": "selected" if sel_set else "removed",
                "columns": cols_out,
            })

        return {
            "tables": tables_out,
            "counts": {
                "tables": len(tables_out),
                "columns_selected": n_selected,
                "columns_removed": n_removed,
            },
        }
    except Exception:  # noqa: BLE001  - sidecar must never break analysis
        return {"tables": [], "counts": {"tables": 0, "columns_selected": 0,
                                         "columns_removed": 0}}


def overlay_column_evidence(schema_json: dict, column_evidence: list | None) -> dict:
    """Write the LLM's per-column justification onto the SELECTED columns.

    For each ``column_evidence`` entry the matching selected column gets the
    LLM role/reason (``role_source``/``evidence_source`` = ``llm``). Removed
    columns are left with their deterministic pruning evidence."""
    if not schema_json or not column_evidence:
        return schema_json or {}
    try:
        # index selected columns by (table, column) and by column name
        by_ref: dict[tuple[str, str], dict] = {}
        by_col: dict[str, list[dict]] = {}
        for t in schema_json.get("tables", []):
            for c in t.get("columns", []):
                if c.get("status") != "selected":
                    continue
                by_ref[(_norm(c.get("table")), _norm(c.get("column")))] = c
                by_col.setdefault(_norm(c.get("column")), []).append(c)
        for ev in column_evidence:
            if not isinstance(ev, dict):
                continue
            col = _norm(ev.get("column"))
            if not col:
                continue
            target = by_ref.get((_norm(ev.get("table")), col))
            if target is None:
                cands = by_col.get(col) or []
                target = cands[0] if cands else None
            if target is None:
                continue
            role = str(ev.get("role") or "").strip().lower()
            if role:
                target["role"] = role
                target["role_source"] = "llm"
            reason = str(ev.get("reason") or "").strip()
            if reason:
                target["reason"] = reason
                target["evidence_source"] = "llm"
    except Exception:  # noqa: BLE001
        return schema_json
    return schema_json


# --- promote-on-search: removed columns are retrievable, never bulk-injected -
def search_removed_columns(schema_json: dict, query: str,
                           table_names: list[str] | None = None,
                           limit: int = 10, min_score: int = 20) -> list[dict]:
    """Deterministic search over REMOVED columns by token overlap with ``query``
    (column name + description + samples). Returns the best matches with their
    metadata so a later stage can decide to :func:`promote_columns`. Never
    injects anything into a prompt — retrieval only."""
    if not schema_json:
        return []
    q = _tokens(query)
    if not q:
        return []
    want_tables = {_norm(t) for t in (table_names or [])} or None
    scored: list[tuple[int, dict]] = []
    try:
        for t in schema_json.get("tables", []):
            if want_tables is not None and _norm(t.get("name")) not in want_tables:
                continue
            for c in t.get("columns", []):
                if c.get("status") != "removed":
                    continue
                hay = _tokens(c.get("column")) | _tokens(c.get("description"))
                for s in (c.get("samples") or [])[:8]:
                    hay |= _tokens(s)
                overlap = len(hay & q)
                if overlap <= 0:
                    continue
                score = min(100, overlap * 25 + int(c.get("prune_score") or 0) // 2)
                if score >= min_score:
                    scored.append((score, {**c, "match_score": score}))
    except Exception:  # noqa: BLE001
        return []
    scored.sort(key=lambda x: x[0], reverse=True)
    return [m for _s, m in scored[:max(1, limit)]]


def rank_columns_by_relevance(tables: list, query: str, top_k: int = 40,
                              pinned: set | None = None,
                              boost_tname: bool = False) -> list[str]:
    """Rank candidate columns by token overlap of the QUERY against each column's
    NAME + DESCRIPTION + SAMPLES, most-relevant first.

    The description carries the meaning even when the physical name is obfuscated
    (e.g. ``msec_val`` whose description is "lap time in milliseconds"), so a
    name-only match misses it but this does not. ``pinned`` refs (linker-selected
    columns, join keys) are always included at the front regardless of score, so
    a needed column is never dropped. Returns ``table.column`` refs (lowercased),
    capped at ``max(top_k, len(pinned))``. General + deterministic, no embeddings.

    ``boost_tname`` adds the TABLE-name↔query overlap to each column's score, so a
    metric ABOUT an entity ("constructor reliability") prefers columns in the
    table NAMED for that entity over a same-token column in an unrelated table.
    Use ONLY where the query is a concept name+definition (the resolver's
    per-concept ranking) — NOT globally (it shifted the generator column-prune and
    regressed other cases). Scoped opt-in.
    """
    q = _tokens(query)
    pinned_l = [str(p).lower() for p in (pinned or [])]
    out: list[str] = []
    seen: set[str] = set()
    for ref in pinned_l:  # pinned first, order preserved
        if ref and ref not in seen:
            out.append(ref)
            seen.add(ref)
    if not q:
        return out[: max(top_k, len(out))]
    scored: list[tuple[int, str]] = []
    for t in tables or []:
        if not isinstance(t, (list, tuple)) or not t:
            continue
        tname = str(t[0] or "").lower()
        # Alias / lookup / cross-reference tables hold ALTERNATE values of an
        # entity (e.g. circuit_aliases.signagename is an alternate track name).
        # The primary entity table is almost always the right source, so strongly
        # de-prioritise alias-table columns: they sink below the cap and out of
        # the candidate list unless little else matches. General naming heuristic.
        alias_penalty = 100 if _ALIAS_TABLE_RE.search(tname) else 0
        tname_match = len(_tokens(tname.replace("_", " ")) & q) if boost_tname else 0
        cols = t[3] if len(t) >= 4 and t[3] else []
        for c in cols:
            if not isinstance(c, dict):
                continue
            name = _col_name(c)
            if not name:
                continue
            ref = f"{tname}.{name.lower()}"
            if ref in seen:
                continue
            hay = _tokens(name) | _tokens(_col_desc(c))
            for s in (c.get("samples") or c.get("examples") or [])[:6]:
                hay |= _tokens(s)
            scored.append((len(hay & q) + tname_match - alias_penalty, ref))
    scored.sort(key=lambda x: -x[0])
    cap = max(top_k, len(out))
    for _score, ref in scored:
        if len(out) >= cap:
            break
        if ref not in seen:
            out.append(ref)
            seen.add(ref)
    return out


def promote_columns(schema_json: dict, refs: list[str], reason: str = "",
                    requested_by: str = "") -> list[str]:
    """Flip the given ``table.column`` refs from removed -> selected so the next
    prompt render can include them. Records who promoted and why. Returns the
    refs actually promoted."""
    if not schema_json or not refs:
        return []
    wanted = {_norm(r) for r in refs}
    promoted: list[str] = []
    try:
        for t in schema_json.get("tables", []):
            tsel = False
            for c in t.get("columns", []):
                ref = _norm(c.get("ref")) or f"{_norm(c.get('table'))}.{_norm(c.get('column'))}"
                if ref in wanted and c.get("status") == "removed":
                    c["status"] = "selected"
                    c["evidence_source"] = "promoted"
                    c["promoted_by"] = requested_by or "unknown"
                    if reason:
                        c["reason"] = reason
                    promoted.append(c.get("ref") or ref)
                if c.get("status") == "selected":
                    tsel = True
            if tsel:
                t["status"] = "selected"
    except Exception:  # noqa: BLE001
        return promoted
    return promoted


# --------------------------------------------------------------------------- #
# Column-aware retrieval keys (knowledge-RAG by columns) + compact schema
# --------------------------------------------------------------------------- #
def _json_leaf_meanings(desc: str) -> dict:
    """Map dotted JSON leaf path -> short meaning, parsed from a column's
    ``Nested fields: {...}`` description. Lets the candidate text label each leaf
    (``date_set -> Date set for the race``) so a binder picks the RIGHT leaf and
    does not invent a flat column. Returns {} if there is no parseable map."""
    out: dict = {}
    if not desc or "Nested fields:" not in desc:
        return out
    s = desc.partition("Nested fields:")[2].strip()
    start = s.find("{")
    if start < 0:
        return out
    depth = 0
    end = -1
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return out
    try:
        obj = json.loads(s[start:end])
    except Exception:  # noqa: BLE001
        return out
    if not isinstance(obj, dict):
        return out

    def _walk(node: dict, path: list[str]) -> None:
        for key, val in node.items():
            pp = path + [str(key)]
            if isinstance(val, dict):
                _walk(val, pp)
                continue
            # Keep the FULL leaf description — type + meaning + NULL-semantics +
            # examples — exactly what a flat column line delivers. Stripping it to
            # the bare meaning hid from every agent that e.g. final_position is
            # INTEGER and that "NULL means the driver did not finish" — so the
            # generator neither cast it as INTEGER nor excluded non-finishers.
            # Only drop markdown emphasis and collapse whitespace.
            text = re.sub(r"\s+", " ", str(val).replace("**", "")).strip()
            if text:
                out[".".join(pp)] = text

    _walk(obj, [])
    return out


def _render_json_leaf_paths(tname: str, col_name: str, json_paths: dict,
                            col_desc: str = "", rel_tokens: set | None = None,
                            max_leaves: int | None = None) -> list[str]:
    """Explicit, bindable JSON leaf paths for a column — the FULL ready-to-copy
    expression for EVERY leaf, including deeply nested ones, each labelled with
    its dotted path, e.g.::

        races.event_schedule->'sessions'->'sprint'->>'date'   (leaf: sessions.sprint.date)

    The model copies the exact expression instead of deriving it from the raw
    nested-dict description — where it mis-navigates deep paths, e.g. applies
    ``->>`` to an intermediate object (``->>'sprint'`` on the sprint object),
    which then fails to cast. Rendering every FULL path (not only the
    unambiguous last-key leaves the repair gate keeps) is what makes deep leaves
    like ``sessions.sprint.date`` available verbatim. General: any JSON column,
    any DB. The chain stays ``->`` for intermediates + ``->>`` for the leaf so
    the JSON gates still recognise/repair it.
    """
    info = (json_paths or {}).get(str(col_name or "").lower())
    if not info:
        return []
    base = f"{tname}.{col_name}" if tname else str(col_name)
    paths = info.get("full") or set()
    if not paths:  # fall back to the deduped leaf->path map if no full set
        paths = {tuple(p) for p in (info.get("leaves") or {}).values()
                 if isinstance(p, (list, tuple))}
    meanings = _json_leaf_meanings(col_desc)
    rows: list[tuple[int, str]] = []
    for path in sorted(paths):
        parts = [str(k) for k in path if str(k) != ""]
        if not parts:
            continue
        expr = base
        for k in parts[:-1]:
            expr += f"->'{k}'"
        expr += f"->>'{parts[-1]}'"
        dotted = ".".join(parts)
        mean = meanings.get(dotted)
        line = f"{expr}   (leaf: {dotted}{' — ' + mean if mean else ''})"
        score = 0
        if rel_tokens:
            score = len(_tokens(dotted.replace(".", " ") + " " + (mean or "")) & rel_tokens)
        rows.append((score, line))
    # When a cap is set, keep the leaves most relevant to the query/concepts and drop
    # the rest, so a JSON column with many nested fields does not dump ALL of them and
    # dilute the binder (it then drops a real formula term). Relevance-ranked; with no
    # tokens it degrades to original order. General — any JSON column, any DB.
    if max_leaves and len(rows) > max_leaves:
        rows.sort(key=lambda r: -r[0])
        kept = rows[:max_leaves]
        # Date/time leaves ALWAYS survive the cap: they are few and critical for
        # joins, filters, and "as of <event>" computations, yet relevance scoring
        # often misses them because the question names a time concept ("at the
        # time", "when", an age/standing as-of) rather than the leaf's own key.
        # General — any JSON column, any DB.
        seen = {ln for _, ln in kept}
        for sc, ln in rows[max_leaves:]:
            low = ln.lower()
            if ("date" in low or "timestamp" in low or "datetime" in low) and ln not in seen:
                kept.append((sc, ln))
                seen.add(ln)
        rows = kept
    return [line for _, line in rows]


def candidate_columns_retrieval_text(tables: list, max_cols: int = 80,
                                     json_paths: dict | None = None,
                                     rel_query: str | None = None,
                                     max_json_leaves: int | None = None) -> str:
    """Compact "table.column: description" lines from find()'s candidate tables.

    Used to make the GENERATOR's business-knowledge retrieval column-aware
    (query + candidate columns), so a needed concept keyed to a specific column
    is retrieved even when the bare question does not name it. When ``json_paths``
    is given, each JSON column also emits explicit bindable leaf paths
    (``col->>'key'``) so a binder copies the exact path instead of hallucinating
    a flat column for a JSON-stored field (the race_date/birth_date problem).
    """
    _rel = _tokens(rel_query) if rel_query else None
    lines: list[str] = []
    for table in tables or []:
        if not isinstance(table, (list, tuple)) or len(table) < 1:
            continue
        tname = str(table[0] or "")
        columns = table[3] if len(table) >= 4 and table[3] else []
        for col in columns:
            if not isinstance(col, dict):
                continue
            name = _col_name(col)
            if not name:
                continue
            desc = _col_desc(col).strip()
            ref = f"{tname}.{name}" if tname else name
            # Complete per-field context the binder needs: TYPE + NOT-NULL beside
            # the description (a NULLABLE status column changes IS NULL semantics; a
            # type guides casts). General — read from the column dict, any DB.
            _ty = str(col.get("type") or col.get("dataType")
                      or col.get("data_type") or "").strip()
            _nullable = str(col.get("nullable") or "").strip().lower()
            _keyt = str(col.get("key_type") or col.get("keyType")
                        or col.get("key") or "").upper()
            _notnull = _nullable in ("no", "false", "not_null", "notnull", "0") \
                or _keyt in ("PK", "PRI", "PRIMARY KEY")
            _meta = ", ".join(p for p in (_ty, "NOT NULL" if _notnull else "") if p)
            _head = f"{ref} ({_meta})" if _meta else ref
            # For a JSON column, the raw "Nested fields: {...}" blob is noise next
            # to the explicit per-leaf lines rendered below — it made the binder
            # skim past the ready-to-copy paths and invent a flat column. Keep only
            # the short summary; the leaf lines (with meanings) carry the detail.
            is_json = bool(json_paths and str(name).lower() in json_paths)
            if is_json and "Nested fields:" in desc:
                desc = desc.split("Nested fields:")[0].strip().rstrip(". ")
            lines.append(f"{_head}: {desc}" if desc else _head)
            if len(lines) >= max_cols:
                return "\n".join(lines)
            for leaf_line in _render_json_leaf_paths(
                    tname, name, json_paths, _col_desc(col),
                    rel_tokens=_rel, max_leaves=max_json_leaves):
                lines.append(leaf_line)
                if len(lines) >= max_cols:
                    return "\n".join(lines)
    return "\n".join(lines)


def _selected_columns(schema_json: dict | None):
    """Yield the selected column dicts from a schema_json sidecar."""
    if not schema_json:
        return
    for table in schema_json.get("tables") or []:
        for col in table.get("columns") or []:
            if col.get("status") == "selected":
                yield col


def selected_columns_retrieval_text(schema_json: dict | None, max_cols: int = 60) -> str:
    """"table.column: description" lines for the SELECTED columns only.

    Used to make the GATE's business-rule retrieval reflect the columns the
    generated SQL actually relies on (owner's "RAG by selected columns").
    """
    lines: list[str] = []
    for col in _selected_columns(schema_json):
        ref = f"{col.get('table')}.{col.get('column')}"
        desc = str(col.get("description") or "").strip()
        lines.append(f"{ref}: {desc}" if desc else ref)
        if len(lines) >= max_cols:
            break
    return "\n".join(lines)


def selected_schema_compact(schema_json: dict | None, max_cols: int = 60) -> str:
    """Compact selected-column context for the rule-gate prompt.

    One line per selected column: ``table.column (type): description e.g. s1, s2``.
    Deliberately small — the gate edits SQL for rule-compliance, it does not
    re-pick tables, so it never needs the full schema.
    """
    lines: list[str] = []
    for col in _selected_columns(schema_json):
        ref = f"{col.get('table')}.{col.get('column')}"
        typ = str(col.get("type") or "").strip()
        desc = str(col.get("description") or "").strip()
        samples = col.get("samples") or []
        sm = ""
        if samples:
            sm = " e.g. " + ", ".join(str(s) for s in list(samples)[:4])
        line = "- " + ref
        if typ:
            line += f" ({typ})"
        if desc:
            line += f": {desc}"
        line += sm
        lines.append(line)
        if len(lines) >= max_cols:
            break
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys

    # full candidate set: 1 table, 4 columns; prune keeps only 2.
    COMBINED = [[
        "lap_times",
        "Per-lap timing rows for each driver in a race.",
        [{"column": "race_id", "referenced_table": "races"}],
        [
            {"name": "msec_val", "type": "int",
             "description": "Lap time in milliseconds.", "nullable": False,
             "key_type": "", "sample_values": ["91234", "88712"]},
            {"name": "race_id", "type": "int", "description": "FK to races.",
             "key_type": "MUL", "references_table": "races",
             "references_column": "race_id", "nullable": False},
            {"name": "position_order", "type": "int",
             "description": "Finishing position order on this lap.",
             "nullable": True, "sample_values": ["1", "2", "3"]},
            {"name": "driver_label", "type": "varchar",
             "description": "Driver display name for the lap.",
             "nullable": True, "sample_values": ["HAM", "VET"]},
        ],
    ]]
    SELECTED = [[
        "lap_times", "Per-lap timing rows.",
        [{"column": "race_id", "referenced_table": "races"}],
        [
            {"name": "msec_val", "type": "int", "description": "Lap time in ms.",
             "nullable": False},
            {"name": "race_id", "type": "int", "description": "FK to races.",
             "key_type": "MUL", "references_table": "races"},
        ],
    ]]

    results = []

    def check(label, cond):
        print(f"[{'PASS' if cond else 'FAIL'}] {label}")
        results.append(cond)

    sj = build_schema_json(COMBINED, SELECTED, "fastest lap time in milliseconds")
    cols = {c["column"]: c for c in sj["tables"][0]["columns"]}

    check("(a) table description present",
          sj["tables"][0]["description"].startswith("Per-lap"))
    check("(b) all 4 columns present with descriptions",
          len(cols) == 4 and all(c["description"] for c in cols.values()))
    check("(c) selected = the 2 kept columns",
          cols["msec_val"]["status"] == "selected" and cols["race_id"]["status"] == "selected")
    check("(d) removed = the 2 dropped columns",
          cols["position_order"]["status"] == "removed" and cols["driver_label"]["status"] == "removed")
    check("(e) counts correct",
          sj["counts"] == {"tables": 1, "columns_selected": 2, "columns_removed": 2})
    check("(f) removed columns carry deterministic prune evidence",
          "prune_score" in cols["driver_label"] and "prune_signals" in cols["driver_label"]
          and cols["driver_label"]["evidence_source"] == "schema_pruner")
    check("(g) role heuristics: FK->key, numeric->measure, label",
          cols["race_id"]["role"] == "key" and cols["msec_val"]["role"] == "measure"
          and cols["driver_label"]["role"] == "label")

    overlay_column_evidence(sj, [
        {"table": "lap_times", "column": "msec_val", "role": "metric",
         "reason": "fastest lap = MIN(msec)"}])
    cols = {c["column"]: c for c in sj["tables"][0]["columns"]}
    check("(h) overlay writes LLM role/reason on selected column",
          cols["msec_val"]["role"] == "metric" and cols["msec_val"]["evidence_source"] == "llm"
          and cols["msec_val"]["reason"].startswith("fastest"))
    check("(i) overlay does NOT touch removed columns",
          cols["driver_label"]["evidence_source"] == "schema_pruner")

    matches = search_removed_columns(sj, "driver name", limit=5, min_score=20)
    check("(j) search finds a removed column by description",
          any(m["column"] == "driver_label" for m in matches))
    check("(k) search never returns selected columns",
          all(m["status"] == "removed" for m in matches))

    promoted = promote_columns(sj, ["lap_times.driver_label"], reason="user asked for driver",
                               requested_by="test")
    cols = {c["column"]: c for c in sj["tables"][0]["columns"]}
    check("(l) promote flips removed -> selected",
          promoted == ["lap_times.driver_label"] and cols["driver_label"]["status"] == "selected")

    print()
    if all(results):
        print(f"ALL {len(results)} ASSERTIONS PASSED")
        sys.exit(0)
    print(f"{sum(1 for r in results if not r)} ASSERTION(S) FAILED")
    sys.exit(1)
