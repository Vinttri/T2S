"""Tool-based two-phase blackboard agent for T2S Text2SQL.

This module implements a TOOL-CALL agent (OpenAI-style ``tools`` /
``tool_choice="required"``) that mutates the shared JSON *blackboard* via
structured tool calls instead of free-text JSON. Free-text JSON re-introduces
exactly the parse-error failure mode this design is trying to remove, so every
semantic decision (column selection, measure, grain, conditions, joins, the
final SQL, clarifications and metadata top-up requests) is expressed as a
validated tool call whose arguments are checked against the blackboard *before*
any mutation happens.

Two phases:

  1. **Planner / field selector** -- rule-aware. Tools: ``decide_column``,
     ``set_measure``, ``set_grain``, ``add_condition``, ``add_join``,
     ``request_metadata_topup``, ``need_clarification``. Max 4 rounds.
     If a blocking ``request_metadata_topup`` fires and a ``topup_fn`` is
     provided, the top-up is performed and the planner is rerun once.

  2. **SQL generator** -- dialect-aware. Tools: ``set_sql``,
     ``request_metadata_topup``, ``need_clarification``. Max 2 rounds.

The pipeline returns a LEGACY-SHAPED answer dict so the existing
gate / executor / healer path downstream works unchanged.

Design constraints:
  * NO hardcoded table/column names anywhere (no dm_mis specifics).
  * stdlib + litellm + api.config + api.core.blackboard only.
  * Tool calls are the single source of truth; prose content is ignored but
    recorded in ``bb["trace"]`` for debugging.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re as _re
from typing import Any, Callable, Optional

from litellm import completion

from api.config import Config
from api.core.blackboard import (
    add_missing_tables_request,
    all_table_names,
    can_topup,
    col_name,
    column_binding,
    column_fk,
    feedback_as_text,
    integrity_check,
    merge_topup_tables,
    selected_rules_as_text,
    set_sql_draft,
    set_sql_final,
    table_name,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI-style). ``additionalProperties: false`` everywhere.
# ``strict`` is intentionally left off to stay compatible across gateways.
# ---------------------------------------------------------------------------
_RULE_IDS_SCHEMA = {"type": "array", "items": {"type": "string"}}

_DECIDE_COLUMN = {
    "type": "function",
    "function": {
        "name": "decide_column",
        "description": "Select or reject a column for a specific semantic role, "
        "with rule-aware rationale.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["decision", "role", "table", "column", "reason", "rule_ids"],
            "properties": {
                "decision": {"type": "string", "enum": ["select", "reject"]},
                "role": {
                    "type": "string",
                    "enum": ["measure", "grain", "filter", "join_key", "sort", "candidate"],
                },
                "table": {"type": "string"},
                "column": {"type": "string"},
                "reason": {"type": "string"},
                "rule_ids": _RULE_IDS_SCHEMA,
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

_SET_MEASURE = {
    "type": "function",
    "function": {
        "name": "set_measure",
        "description": "Set the primary measure or metric requested by the user.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "measure_id", "label", "table", "column", "aggregation",
                "reason", "rule_ids",
            ],
            "properties": {
                "measure_id": {"type": "string"},
                "label": {"type": "string"},
                "table": {"type": "string"},
                "column": {"type": "string"},
                "aggregation": {
                    "type": "string",
                    "enum": ["sum", "avg", "min", "max", "count", "count_distinct", "none"],
                },
                "reason": {"type": "string"},
                "rule_ids": _RULE_IDS_SCHEMA,
            },
        },
    },
}

_SET_GRAIN = {
    "type": "function",
    "function": {
        "name": "set_grain",
        "description": "Set grouping dimensions for the query result.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["dimensions", "reason", "rule_ids"],
            "properties": {
                "dimensions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["table", "column"],
                        "properties": {
                            "table": {"type": "string"},
                            "column": {"type": "string"},
                            "bucket": {
                                "type": "string",
                                "enum": ["none", "day", "week", "month", "quarter", "year"],
                            },
                        },
                    },
                },
                "reason": {"type": "string"},
                "rule_ids": _RULE_IDS_SCHEMA,
            },
        },
    },
}

_ADD_CONDITION = {
    "type": "function",
    "function": {
        "name": "add_condition",
        "description": "Add a filter, having clause, or business condition.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "condition_id", "kind", "table", "column", "operator", "value",
                "reason", "rule_ids",
            ],
            "properties": {
                "condition_id": {"type": "string"},
                "kind": {"type": "string", "enum": ["where", "having"]},
                "table": {"type": "string"},
                "column": {"type": "string"},
                "operator": {
                    "type": "string",
                    "enum": [
                        "=", "!=", "<", "<=", ">", ">=", "in", "not_in",
                        "between", "like", "is_null", "is_not_null",
                    ],
                },
                "value": {},
                "reason": {"type": "string"},
                "rule_ids": _RULE_IDS_SCHEMA,
            },
        },
    },
}

_ADD_JOIN = {
    "type": "function",
    "function": {
        "name": "add_join",
        "description": "Add a join using complete key columns.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "join_id", "left_table", "right_table", "join_type", "keys",
                "reason", "rule_ids",
            ],
            "properties": {
                "join_id": {"type": "string"},
                "left_table": {"type": "string"},
                "right_table": {"type": "string"},
                "join_type": {"type": "string", "enum": ["inner", "left"]},
                "keys": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["left_column", "right_column"],
                        "properties": {
                            "left_column": {"type": "string"},
                            "right_column": {"type": "string"},
                        },
                    },
                },
                "reason": {"type": "string"},
                "rule_ids": _RULE_IDS_SCHEMA,
            },
        },
    },
}

_REQUEST_METADATA_TOPUP = {
    "type": "function",
    "function": {
        "name": "request_metadata_topup",
        "description": "Pull missing context back from the RAG before producing the "
        "SQL: missing TABLES, missing COLUMNS (with their descriptions, sample "
        "values and FK relationships), and/or relevant BUSINESS RULES — set "
        "blocking=true to be re-run with the retrieved context visible.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["missing_tables", "missing_columns", "reason", "blocking"],
            "properties": {
                "rules": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Topics to check the business rules for (e.g. "
                    "'validity on a date', 'currency conversion') — relevant rules, "
                    "including any earlier pruned, are surfaced back.",
                },
                "missing_tables": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["table_hint", "purpose"],
                        "properties": {
                            "table_hint": {"type": "string"},
                            "purpose": {"type": "string"},
                        },
                    },
                },
                "missing_columns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["table", "column_hint", "purpose"],
                        "properties": {
                            "table": {"type": "string"},
                            "column_hint": {"type": "string"},
                            "purpose": {"type": "string"},
                        },
                    },
                },
                "reason": {"type": "string"},
                "blocking": {"type": "boolean"},
            },
        },
    },
}

_SET_SQL = {
    "type": "function",
    "function": {
        "name": "set_sql",
        "description": "Store generated SQL in the blackboard.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["stage", "sql", "reason", "confidence"],
            "properties": {
                "stage": {"type": "string", "enum": ["draft", "final"]},
                "sql": {"type": "string"},
                "reason": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
        },
    },
}

_NEED_CLARIFICATION = {
    "type": "function",
    "function": {
        "name": "need_clarification",
        "description": "Terminate because the user must clarify missing or ambiguous "
        "information.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["reason", "questions", "blocking_missing_info"],
            "properties": {
                "reason": {"type": "string"},
                "questions": {"type": "array", "items": {"type": "string"}},
                "blocking_missing_info": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

_FINALIZE_PLAN = {
    "type": "function",
    "function": {
        "name": "finalize_plan",
        "description": "Call this as soon as the plan is complete (all requested "
        "outputs, the measure(s), grain, filters and joins are decided). This "
        "ENDS the planning phase immediately - call it instead of repeating "
        "decisions.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["summary"],
            "properties": {"summary": {"type": "string"}},
        },
    },
}

_PRUNE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "prune_schema",
        "description": "Everything is already loaded. Remove ONLY whole TABLES that "
        "are DEFINITELY not needed to answer the question (clearly unrelated to its "
        "outputs, filters, grouping, measures and join paths). Every table you do "
        "NOT list STAYS — with ALL of its columns, their FK relationships and "
        "descriptions intact. When in doubt, KEEP the table. Judge by MEANING.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["drop_tables", "reason"],
            "properties": {
                "drop_tables": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Names of tables clearly irrelevant to the question.",
                },
                "reason": {"type": "string"},
            },
        },
    },
}

_PRUNE_RULES = {
    "type": "function",
    "function": {
        "name": "prune_rules",
        "description": "Remove ONLY the business rules that are DEFINITELY not "
        "relevant to this question — clearly about a domain or operation the "
        "question never touches (e.g. a REPO-leg rule on a question with no "
        "securities). Everything you do NOT list STAYS. When in doubt, KEEP the "
        "rule. The invariants are never removable.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["drop_rule_ids", "reason"],
            "properties": {
                "drop_rule_ids": {"type": "array", "items": {"type": "string"}},
                "reason": {"type": "string"},
            },
        },
    },
}

# Public tool sets ----------------------------------------------------------
TOOLS_PLANNER = [
    _DECIDE_COLUMN,
    _SET_MEASURE,
    _SET_GRAIN,
    _ADD_CONDITION,
    _ADD_JOIN,
    _REQUEST_METADATA_TOPUP,
    _FINALIZE_PLAN,
    _NEED_CLARIFICATION,
]

TOOLS_SQL = [
    _SET_SQL,
    _REQUEST_METADATA_TOPUP,
    _NEED_CLARIFICATION,
]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class ToolValidationError(Exception):
    """Raised when tool arguments fail blackboard-aware validation."""


def _bb_tables(bb: dict) -> list:
    return bb.get("tables") or []


def find_table(bb: dict, name: str) -> Optional[dict]:
    """Case-insensitive lookup of a table in the blackboard."""
    if not name:
        return None
    low = str(name).lower()
    for t in _bb_tables(bb):
        if str(t.get("name", "")).lower() == low:
            return t
    return None


def find_column(bb: dict, table: str, column: str) -> Optional[dict]:
    """Case-insensitive lookup of a column within a table."""
    t = find_table(bb, table)
    if t is None:
        return None
    low = str(column or "").lower()
    for c in t.get("columns", []) or []:
        if col_name(c).lower() == low:
            return c
    return None


def _require_known_table(bb: dict, table: str) -> dict:
    t = find_table(bb, table)
    if t is None:
        raise ToolValidationError(
            f"Unknown table '{table}'. It is not in the blackboard. "
            "Use request_metadata_topup to ask for it instead of inventing it."
        )
    return t


def _require_known_column(bb: dict, table: str, column: str) -> dict:
    c = find_column(bb, table, column)
    if c is None:
        # Distinguish unknown table from unknown column for a clearer message.
        if find_table(bb, table) is None:
            raise ToolValidationError(
                f"Unknown table '{table}'. Use request_metadata_topup instead of "
                "inventing names."
            )
        raise ToolValidationError(
            f"Unknown column '{column}' on table '{table}'. It is not in the "
            "blackboard. Use request_metadata_topup instead of inventing names."
        )
    return c


def _selected_rule_ids(bb: dict) -> set:
    return {
        str(r.get("id"))
        for r in (bb.get("selected_business_rules") or [])
        if r.get("id") is not None
    }


def _validate_rule_ids(bb: dict, args: dict) -> None:
    """If rule_ids are present, every id must exist in selected_business_rules.

    An empty list is allowed (the spec: 'rule_ids=[] with a reason when no
    selected rule applies')."""
    rule_ids = args.get("rule_ids")
    if not rule_ids:
        return
    known = _selected_rule_ids(bb)
    unknown = [rid for rid in rule_ids if str(rid) not in known]
    if unknown:
        raise ToolValidationError(
            f"Unknown rule_ids {unknown}. Valid selected rule ids are "
            f"{sorted(known) or '[] (no rules selected; use rule_ids=[])'}."
        )


# Some models (Hermes/XML tool-format fallbacks) intermittently leak closing tool
# tags like ``</parameter>`` / ``</function>`` into the END of string argument values
# (e.g. stage="draft\n</parameter >"). That is malformed OUTPUT, not the user's intent;
# strip it so a flaky tool-call round does not dead-end the writer. This is output
# parsing, not a validation gate.
_TOOL_TAG_LEAK_RE = _re.compile(
    r"(?:\s*<\s*/?\s*(?:parameter|function|tool_call|tool|arg|args|invoke)\b[^>]*>?\s*)+$",
    _re.IGNORECASE,
)


def _strip_tag_leakage(value: str) -> str:
    """Remove trailing XML tool-format tag artifacts (and surrounding whitespace)."""
    if not isinstance(value, str):
        return value
    return _TOOL_TAG_LEAK_RE.sub("", value).strip()


def _sanitize_tool_args(args: dict) -> dict:
    """Defensively clean tag-leakage out of top-level string args before validation."""
    for k, v in list(args.items()):
        if isinstance(v, str):
            args[k] = _strip_tag_leakage(v)
    return args


def validate_tool_args(name: str, raw_args: Any, bb: dict) -> dict:
    """Parse JSON args and validate table/column/rule existence before mutation.

    Returns the parsed args dict on success; raises ToolValidationError on any
    failure so the caller can hand the error back to the model for self-repair.
    """
    if isinstance(raw_args, dict):
        args = raw_args
    else:
        try:
            args = json.loads(raw_args or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            raise ToolValidationError(f"Arguments are not valid JSON: {exc}") from exc
    if not isinstance(args, dict):
        raise ToolValidationError("Arguments must be a JSON object.")
    args = _sanitize_tool_args(args)

    if name == "decide_column":
        _require_known_column(bb, args.get("table", ""), args.get("column", ""))
        _validate_rule_ids(bb, args)
    elif name == "set_measure":
        _require_known_column(bb, args.get("table", ""), args.get("column", ""))
        _validate_rule_ids(bb, args)
    elif name == "set_grain":
        dims = args.get("dimensions") or []
        if not isinstance(dims, list):
            raise ToolValidationError("'dimensions' must be a list.")
        for dim in dims:
            _require_known_column(bb, dim.get("table", ""), dim.get("column", ""))
        _validate_rule_ids(bb, args)
    elif name == "add_condition":
        _require_known_column(bb, args.get("table", ""), args.get("column", ""))
        _validate_rule_ids(bb, args)
    elif name == "add_join":
        _require_known_table(bb, args.get("left_table", ""))
        _require_known_table(bb, args.get("right_table", ""))
        keys = args.get("keys") or []
        if not keys:
            raise ToolValidationError("'keys' must contain at least one key pair.")
        for key in keys:
            _require_known_column(
                bb, args.get("left_table", ""), key.get("left_column", "")
            )
            _require_known_column(
                bb, args.get("right_table", ""), key.get("right_column", "")
            )
        _validate_rule_ids(bb, args)
    elif name == "request_metadata_topup":
        # missing_* refer to things NOT in the blackboard, so no existence check.
        if not isinstance(args.get("missing_tables", []), list) or not isinstance(
            args.get("missing_columns", []), list
        ):
            raise ToolValidationError(
                "missing_tables and missing_columns must be lists."
            )
    elif name == "set_sql":
        sql = args.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ToolValidationError("'sql' must be a non-empty string.")
        if args.get("stage") not in ("draft", "final"):
            raise ToolValidationError("'stage' must be 'draft' or 'final'.")
    elif name == "need_clarification":
        pass
    elif name == "finalize_plan":
        pass
    else:
        raise ToolValidationError(f"Unknown tool '{name}'.")

    return args


# ---------------------------------------------------------------------------
# Dispatch (apply_* mutators). Each returns {"ok": bool, ...}.
# ---------------------------------------------------------------------------
def _upsert_by_id(items: list, args: dict, id_key: str) -> None:
    target = args.get(id_key)
    for i, existing in enumerate(items):
        if existing.get(id_key) == target:
            items[i] = args
            return
    items.append(args)


def apply_decide_column(args: dict, bb: dict) -> dict:
    col = _require_known_column(bb, args["table"], args["column"])
    col["status"] = "selected" if args.get("decision") == "select" else "rejected"
    if args.get("role"):
        col["role"] = args["role"]
    col.setdefault("decisions", []).append(args)
    bb.setdefault("trace", []).append({"tool": "decide_column", "args": args})
    # Echo the fully-resolved binding so the model SEES which table.column it
    # touched and the metadata bound to it (not just its own arguments).
    return {"ok": True, "bound": column_binding(args["table"], col)}


def apply_set_measure(args: dict, bb: dict) -> dict:
    col = _require_known_column(bb, args["table"], args["column"])
    bb["measure"] = args
    bb.setdefault("trace", []).append({"tool": "set_measure", "args": args})
    return {
        "ok": True,
        "bound": column_binding(args["table"], col),
        "aggregation": args.get("aggregation"),
        "label": args.get("label"),
    }


def apply_set_grain(args: dict, bb: dict) -> dict:
    bound = []
    for dim in args.get("dimensions", []) or []:
        col = _require_known_column(bb, dim["table"], dim["column"])
        b = column_binding(dim["table"], col)
        if dim.get("bucket"):
            b["bucket"] = dim["bucket"]
        bound.append(b)
    bb["grain"] = args
    bb.setdefault("trace", []).append({"tool": "set_grain", "args": args})
    return {"ok": True, "bound": bound}


def apply_add_condition(args: dict, bb: dict) -> dict:
    col = _require_known_column(bb, args["table"], args["column"])
    _upsert_by_id(bb.setdefault("conditions", []), args, "condition_id")
    bb.setdefault("trace", []).append({"tool": "add_condition", "args": args})
    return {
        "ok": True,
        "bound": column_binding(args["table"], col),
        "operator": args.get("operator"),
        "value": args.get("value"),
    }


def apply_add_join(args: dict, bb: dict) -> dict:
    _require_known_table(bb, args["left_table"])
    _require_known_table(bb, args["right_table"])
    key_bindings = []
    for key in args.get("keys", []) or []:
        lcol = _require_known_column(bb, args["left_table"], key["left_column"])
        rcol = _require_known_column(bb, args["right_table"], key["right_column"])
        # Surface whether this key pair is a DECLARED FK relationship (either
        # direction), so the model can see the table<->table link it is using.
        lfk = column_fk(lcol)
        rfk = column_fk(rcol)
        left_ref = (lcol.get("ref") or f"{args['left_table']}.{key['left_column']}")
        right_ref = (rcol.get("ref") or f"{args['right_table']}.{key['right_column']}")
        is_fk = (lfk == right_ref) or (rfk == left_ref)
        key_bindings.append({
            "left": column_binding(args["left_table"], lcol),
            "right": column_binding(args["right_table"], rcol),
            "is_declared_fk": is_fk,
        })
    _upsert_by_id(bb.setdefault("joins", []), args, "join_id")
    bb.setdefault("trace", []).append({"tool": "add_join", "args": args})
    return {"ok": True, "keys": key_bindings}


def _topup_unreject(bb: dict, args: dict) -> int:
    """Cheap re-add: a requested table/column that was PRUNED (focus/column-prune)
    is still in the blackboard, just hidden — un-hide it here, no retrieval needed.
    Matches a column hint lexically against the pruned columns of the named table.
    Returns how many items were re-surfaced."""
    re_added = 0

    def _short(n):
        return str(n or "").split(".")[-1].lower()

    for mt in args.get("missing_tables", []) or []:
        hint = _short(mt.get("table_hint"))
        for t in _bb_tables(bb):
            if _short(t.get("name")) == hint and t.get("status") == "rejected":
                t["status"] = "selected"
                re_added += 1

    for mc in args.get("missing_columns", []) or []:
        tl = _short(mc.get("table"))
        htok = _query_tokens(mc.get("column_hint") or "")
        for t in _bb_tables(bb):
            if _short(t.get("name")) != tl:
                continue
            if t.get("status") == "rejected":
                t["status"] = "selected"
                re_added += 1
            best, best_score = None, 0
            for c in (t.get("columns") or []):
                if c.get("status") != "rejected":
                    continue
                ctok = _query_tokens(col_name(c) + " " + str(c.get("description") or ""))
                score = len(htok & ctok)
                # exact name hit wins outright
                if col_name(c).lower() == str(mc.get("column_hint") or "").lower():
                    best, best_score = c, 999
                    break
                if score > best_score:
                    best, best_score = c, score
            if best is not None and best_score > 0:
                best["status"] = "candidate"
                re_added += 1
    return re_added


def _surface_rules(bb: dict, topics: list) -> int:
    """Bring back business rules relevant to the given topics — including any that
    rule-cleanup pruned (the full set is stashed in ``_all_rules``). Recall-biased
    lexical match; never duplicates. Returns how many rules were surfaced."""
    allr = bb.get("_all_rules") or bb.get("selected_business_rules") or []
    if not allr:
        return 0
    qtok: set = set()
    for t in (topics or []):
        qtok |= _query_tokens(str(t))
    if not qtok:
        return 0
    selected = bb.setdefault("selected_business_rules", [])
    have = {str(r.get("id")) for r in selected}
    added = 0
    for r in allr:
        rid = str(r.get("id"))
        if rid in have or rid == "invariants":
            continue
        if _lex_related(qtok, str(r.get("text") or r.get("title") or "")):
            selected.append(r)
            have.add(rid)
            added += 1
    return added


def apply_request_metadata_topup(args: dict, bb: dict) -> dict:
    # First try to satisfy the request from the blackboard itself (un-hide a
    # pruned table/column) — no retrieval round-trip needed.
    re_added = _topup_unreject(bb, args)
    # Also surface any business rules relevant to the requested topics.
    re_added += _surface_rules(bb, args.get("rules") or [])

    requests = bb.setdefault("missing_tables_request", [])
    existing_hints = {
        str(r.get("semantic_hint", "")).lower()
        for r in requests
        if isinstance(r, dict)
    }
    for mt in args.get("missing_tables", []) or []:
        hint = str(mt.get("table_hint", "")).strip()
        if hint and hint.lower() not in existing_hints:
            add_missing_tables_request(
                bb,
                requested_by="tool_planner",
                semantic_hint=hint,
                reason=str(mt.get("purpose", "")),
                required=bool(args.get("blocking")),
            )
            existing_hints.add(hint.lower())

    col_requests = bb.setdefault("missing_columns_request", [])
    seen_cols = {
        (str(r.get("table", "")).lower(), str(r.get("column_hint", "")).lower())
        for r in col_requests
        if isinstance(r, dict)
    }
    for mc in args.get("missing_columns", []) or []:
        key = (
            str(mc.get("table", "")).lower(),
            str(mc.get("column_hint", "")).lower(),
        )
        if key in seen_cols or not key[1]:
            continue
        col_requests.append(
            {
                "id": f"miss_c_{len(col_requests) + 1}",
                "requested_by": "tool_planner",
                "table": mc.get("table", ""),
                "column_hint": mc.get("column_hint", ""),
                "purpose": mc.get("purpose", ""),
                "required": bool(args.get("blocking")),
            }
        )
        seen_cols.add(key)

    bb.setdefault("trace", []).append(
        {"tool": "request_metadata_topup", "args": args, "re_added": re_added}
    )
    # If the top-up added/un-hid something, keep it blocking so the agent is re-run and
    # SEES the refreshed schema. But a NO-OP top-up (re_added==0) must NOT block: the
    # requested fields are already present (e.g. an enum mapping that lives in the column
    # DESCRIPTION) or unfindable — re-running only lets the agent ask again, which dead-
    # ends the writer in an unsatisfiable loop (-> no SQL). Make it non-blocking and tell
    # the agent the info is already in front of it so it proceeds to write SQL.
    if re_added == 0:
        return {
            "ok": True,
            "blocking": False,
            "re_added": 0,
            "instruction": (
                "No new schema was added: the fields/tables/rules you asked for are "
                "ALREADY in the schema you were given — read their descriptions, their "
                "[ДАННЫЕ: …] data profiles and their e.g. sample values above (enum/code "
                "meanings live in the column description). Do NOT request them again; "
                "decide from what is shown and call set_sql now."
            ),
        }
    return {"ok": True, "blocking": bool(args.get("blocking")), "re_added": re_added}


def apply_set_sql(args: dict, bb: dict) -> dict:
    stage = args.get("stage", "draft")
    if stage == "final":
        set_sql_final(bb, args.get("sql"))
    else:
        set_sql_draft(bb, args.get("sql"))
    sql_obj = bb.setdefault("sql", {})
    sql_obj[stage] = args.get("sql")
    sql_obj["last_reason"] = args.get("reason")
    sql_obj["confidence"] = args.get("confidence")
    redacted = dict(args)
    if isinstance(redacted.get("sql"), str) and len(redacted["sql"]) > 400:
        redacted["sql"] = redacted["sql"][:400] + "...[truncated]"
    bb.setdefault("trace", []).append({"tool": "set_sql", "args": redacted})
    return {"ok": True}


def apply_need_clarification(args: dict, bb: dict) -> dict:
    bb["missing_information"] = args.get("blocking_missing_info", [])
    bb["clarification_questions"] = args.get("questions", [])
    bb["clarification_reason"] = args.get("reason", "")
    bb.setdefault("trace", []).append({"tool": "need_clarification", "args": args})
    return {"ok": True}


def apply_finalize_plan(args: dict, bb: dict) -> dict:
    bb.setdefault("trace", []).append({"tool": "finalize_plan", "args": args})
    return {"ok": True}


DISPATCH: dict[str, Callable[[dict, dict], dict]] = {
    "decide_column": apply_decide_column,
    "set_measure": apply_set_measure,
    "set_grain": apply_set_grain,
    "add_condition": apply_add_condition,
    "add_join": apply_add_join,
    "request_metadata_topup": apply_request_metadata_topup,
    "set_sql": apply_set_sql,
    "need_clarification": apply_need_clarification,
    "finalize_plan": apply_finalize_plan,
}


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------
def _tool_completion(messages: list, tools: list, model: str, api_key: Optional[str]):
    """Single tool-call completion. Returns the message object (.content,
    .tool_calls). ``response_format`` is removed because it conflicts with
    ``tools``."""
    args = Config.completion_kwargs(
        custom_model=model,
        custom_api_key=api_key,
        messages=messages,
        top_p=1,
        tools=tools,
        tool_choice="required",
    )
    args.pop("response_format", None)
    resp = completion(**args)
    return resp.choices[0].message


def _assistant_msg_dict(msg: Any) -> dict:
    """Serialize an assistant message (with tool_calls) for the message list."""
    return {
        "role": "assistant",
        "content": getattr(msg, "content", None) or "",
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {
                    "name": c.function.name,
                    "arguments": c.function.arguments,
                },
            }
            for c in (getattr(msg, "tool_calls", None) or [])
        ],
    }


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
def run_tool_agent(
    messages: list,
    tools: list,
    allowed_terminals: set,
    max_rounds: int,
    bb: dict,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
) -> tuple[str, dict]:
    """Run the tool loop until a terminal tool succeeds or rounds are exhausted.

    A terminal is ``set_sql`` / ``need_clarification`` / a *blocking*
    ``request_metadata_topup``. Returns ``(outcome, info)`` where ``outcome`` is
    the terminal tool name or ``"max_rounds_exceeded"`` / ``"no_tool_calls"``,
    and ``info`` carries the terminal tool result (e.g. ``{"blocking": True}``).
    """
    reprompted = False
    for _ in range(max_rounds):
        try:
            msg = _tool_completion(messages, tools, model, api_key)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("tool_blackboard: completion failed: %s", str(exc)[:300])
            bb.setdefault("trace", []).append(
                {"type": "llm_error", "error": str(exc)[:300]}
            )
            return "llm_error", {"error": str(exc)[:300]}

        tool_calls = getattr(msg, "tool_calls", None) or []

        if not tool_calls:
            # Record ignored prose, then re-prompt once to force a tool call.
            if getattr(msg, "content", None):
                bb.setdefault("trace", []).append(
                    {"type": "ignored_model_content", "content": msg.content}
                )
            if reprompted:
                return "no_tool_calls", {}
            reprompted = True
            messages.append(
                {
                    "role": "user",
                    "content": "You must call one of the provided tools. "
                    "Do not answer in prose.",
                }
            )
            continue

        # Tool calls are source of truth; prose is recorded but ignored.
        if getattr(msg, "content", None):
            bb.setdefault("trace", []).append(
                {"type": "ignored_model_content", "content": msg.content}
            )

        messages.append(_assistant_msg_dict(msg))

        terminal_seen: Optional[str] = None
        terminal_info: dict = {}

        for call in tool_calls:
            name = call.function.name
            raw_args = call.function.arguments or "{}"
            try:
                args = validate_tool_args(name, raw_args, bb)
                result = DISPATCH[name](args, bb)
            except ToolValidationError as exc:
                result = {
                    "ok": False,
                    "error": str(exc),
                    "instruction": "Retry with valid arguments.",
                }
            except KeyError:
                result = {
                    "ok": False,
                    "error": f"Unknown tool '{name}'.",
                    "instruction": "Retry with valid arguments.",
                }

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": name,
                    "content": json.dumps(result),
                }
            )

            if _debug_enabled():
                logger.warning(
                    "BB-DEBUG action tool=%s ok=%s args=%s",
                    name, result.get("ok"),
                    (json.dumps(args, ensure_ascii=False)[:300]
                     if isinstance(locals().get("args"), dict) else str(raw_args)[:200]),
                )

            if name in allowed_terminals and result.get("ok"):
                # A non-blocking metadata top-up is NOT terminal: keep planning.
                if name == "request_metadata_topup" and not result.get("blocking"):
                    continue
                terminal_seen = name
                terminal_info = result

        if terminal_seen:
            return terminal_seen, terminal_info

    return "max_rounds_exceeded", {}


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------
def _trunc(text: Any, limit: int) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _render_rules(bb: dict) -> str:
    rules = bb.get("selected_business_rules") or []
    if not rules:
        return "Selected business rules: (none selected for this query)"
    lines = ["Selected business rules (cite their id in rule_ids):"]
    for r in rules:
        rid = str(r.get("id") or "")
        title = str(r.get("title") or "").strip()
        text = str(r.get("text") or "").strip()
        head = f"- [{rid}]"
        if title:
            head += f" {title}"
        lines.append(head)
        if text:
            # Deliver the FULL rule body. A 600-char cut silently dropped the END
            # of long rules (e.g. the per-table validity clause), so the agent
            # never saw the decisive part. Rules are general and bounded; render
            # them whole.
            lines.append(f"    {_trunc(text, 4000)}")
    return "\n".join(lines)


def _render_schema(bb: dict, include_status: bool = True) -> str:
    """Compact schema view of ONLY the blackboard tables/columns."""
    out = []
    for t in _bb_tables(bb):
        if t.get("status") == "rejected":
            continue
        nm = str(t.get("name", ""))
        desc = _trunc(t.get("description"), 320)
        out.append(f"TABLE {nm}" + (f" -- {desc}" if desc else ""))
        for c in t.get("columns", []) or []:
            if c.get("status") == "rejected":
                continue  # column pruned as unrelated; re-addable via topup
            name = col_name(c)
            ctype = c.get("type") or ""
            # Do NOT over-truncate: the disambiguating phrase (authoritative /
            # fan-out / numeric-vs-categorical) often lives at the END of the
            # description, so cutting it drops the decisive signal.
            cdesc = _trunc(c.get("description"), 400)
            key = c.get("key_type") or ""
            samples = c.get("sample_values") or []
            status = c.get("status") or ""
            ref_t = c.get("references_table")
            ref_c = c.get("references_column")
            parts = [f"  - {name}"]
            if ctype:
                parts.append(f"({ctype})")
            if key:
                parts.append(f"[{key}]")
            # NOT NULL must be visible: otherwise the model writes
            # "<col> IS NULL" / "<col> = 0" as an "active/closed" predicate on a
            # mandatory column, which can never exclude a row (returns nothing).
            _nn = str(c.get("nullable") or "").strip().upper()
            if _nn in ("NO", "FALSE", "0", "NOT NULL", "NOT_NULL") or c.get("nullable") is False:
                parts.append("[NOT NULL]")
            elif _nn in ("YES", "TRUE", "1", "NULL", "NULLABLE") or c.get("nullable") is True:
                # Explicit nullable marker so the model distinguishes «may be NULL»
                # from «unknown», not just by the absence of [NOT NULL].
                parts.append("[NULL]")
            if ref_t and "FK→" not in (cdesc or ""):
                # Structured join path — only when the prose FK→ isn't already in
                # the description (prose carries all polymorphic targets).
                parts.append(f"-> FK {ref_t}.{ref_c or ''}".rstrip("."))
            if cdesc:
                parts.append(f"-- {cdesc}")
            if samples:
                sample_str = ", ".join(str(s) for s in samples[:5])
                parts.append(f"e.g. {_trunc(sample_str, 120)}")
            # Data-grounded fact (NULL-ness + range) — overrides declared
            # nullability for the IS NULL vs `> D` / range decision.
            prof = c.get("data_profile")
            if prof:
                parts.append(f"[ДАННЫЕ: {_trunc(prof, 110)}]")
            if include_status and status:
                parts.append(f"<status={status}>")
            out.append(" ".join(parts))
    return "\n".join(out) if out else "(no tables in blackboard)"


def _render_table_catalog(bb: dict) -> str:
    """Compact catalogue (name + description + PK + a few non-id columns) for the
    table-focus selector — enough to judge relevance without the full schema."""
    lines = []
    for t in _bb_tables(bb):
        if t.get("status") == "rejected":
            continue
        nm = str(t.get("name", ""))
        desc = _trunc(t.get("description"), 320)
        pk = [col_name(c) for c in (t.get("columns") or []) if "PRI" in
              str(c.get("key_type") or "").upper() or "PK" in
              str(c.get("key_type") or "").upper()]
        cols = [col_name(c) for c in (t.get("columns") or [])][:14]
        head = f"- {nm}"
        if desc:
            head += f" — {desc}"
        lines.append(head)
        if pk:
            lines.append(f"    PK: {', '.join(pk)}")
        if cols:
            lines.append(f"    columns: {', '.join(cols)}")
    return "\n".join(lines) if lines else "(no tables)"


def _render_rule_catalog(bb: dict) -> str:
    lines = []
    for r in bb.get("selected_business_rules") or []:
        rid = str(r.get("id") or "")
        if rid == "invariants":
            continue  # always kept
        title = str(r.get("title") or "").strip()
        lines.append(f"- [{rid}] {_trunc(title, 200)}")
    return "\n".join(lines) if lines else "(none)"


def _schema_focus_system_prompt(bb: dict) -> str:
    schema = _render_schema(bb, include_status=False)
    return f"""You are the SCHEMA CLEANER (a retrieval agent) for a Text-to-SQL \
system. The FULL candidate schema below is already loaded — your job is to REMOVE \
only whole TABLES that are DEFINITELY not needed to answer the question, so the \
downstream planner sees a cleaner schema. Every table you do NOT remove STAYS, \
with ALL of its columns, their FK relationships and descriptions intact.

HOW TO CLEAN (recall-biased — when in DOUBT, KEEP the table):
- Remove a TABLE only when it is clearly unrelated — nothing in the question's \
outputs, filters, grouping, measures or join paths needs it. A table whose \
description shows it holds a requested entity, or that bridges a join between two \
needed tables, MUST STAY.
- Do NOT remove columns — keep every column of every kept table (the planner will \
pick among them). Judge tables BY MEANING using their descriptions, not word \
overlap.
- A removed-but-needed table breaks the answer; a kept-but-unused one only adds a \
little noise. So keep generously.

SCHEMA (tables with their descriptions and all columns):
{schema}

Call prune_schema ONCE with only the table names to REMOVE."""


def _rule_focus_system_prompt(bb: dict) -> str:
    schema = _render_schema(bb, include_status=False)
    rule_catalog = _render_rule_catalog(bb)
    return f"""You are the RULE CLEANER for a Text-to-SQL system. All business \
rules are loaded; REMOVE only the ones DEFINITELY not relevant to the user's \
question on the FOCUSED schema below, so the planner is not diluted. Everything you \
do NOT remove STAYS.

RECALL-BIASED — when in DOUBT whether a rule could apply, KEEP it (do not list it). \
A removed-but-needed rule causes a wrong answer; a kept-but-unused one costs only a \
little noise. Remove a rule ONLY when it is clearly about a domain or operation \
this question never touches (e.g. a REPO-leg rule on a question with no \
securities/REPO). The invariants are never removable. Judge by MEANING.

FOCUSED SCHEMA (what the query will actually use):
{schema}

BUSINESS RULES:
{rule_catalog}

Call prune_rules ONCE with the ids to REMOVE."""


_TOK_RE = _re.compile(r"[A-Za-zА-Яа-яЁё0-9_]{3,}")
_TOK_STOP = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "выведите", "выведи",
    "запрос", "необходимо", "должен", "каждого", "каждой", "также", "первыми",
    "записей", "ограничьте", "вывод", "покажите", "за", "на", "по", "для", "или",
    "все", "как", "что", "это", "его", "при", "над", "под",
})


def _query_tokens(text: str) -> set:
    return {m.group(0).lower() for m in _TOK_RE.finditer(str(text or ""))
            if m.group(0).lower() not in _TOK_STOP}


def _lex_related(qtok: set, blob: str) -> bool:
    """Recall-biased lexical relevance: exact token overlap OR a shared 5-char
    PREFIX (so Russian word-forms «налоговыми»↔«налогообложения», «договор»↔
    «договоров» still match and a needed-but-inflected column/rule is KEPT)."""
    btok = _query_tokens(blob)
    if qtok & btok:
        return True
    qpref = {t[:5] for t in qtok if len(t) >= 5}
    if not qpref:
        return False
    return any(t[:5] in qpref for t in btok if len(t) >= 5)


def _prune_columns(bb: dict) -> None:
    """Within the (focused) tables, hide columns clearly UNRELATED to the question
    so the planner is not diluted by audit/technical columns. Recall-biased — keep
    anything structural or with ANY relevance signal, drop only the clearly-far:
      keep = PK/FK (joins) | date/timestamp (filters) | numeric (measures) |
             a query token in the column name OR its (Russian) description.
    Pruned columns stay in the blackboard (status=rejected) and can be re-added on
    demand via request_metadata_topup. Schema-agnostic; never raises."""
    if not getattr(Config, "BLACKBOARD_COLUMN_PRUNE_ENABLED", True):
        return
    qtok = _query_tokens((bb.get("request", {}) or {}).get("user_query", ""))
    if not qtok:
        return
    for t in _bb_tables(bb):
        if t.get("status") == "rejected":
            continue
        cols = [c for c in (t.get("columns") or []) if c.get("status") != "rejected"]
        if len(cols) <= 6:
            continue  # already small — pruning adds no value
        pruned = 0
        for c in cols:
            if c.get("role"):
                continue
            kt = str(c.get("key_type") or "").upper()
            typ = str(c.get("type") or "").lower()
            structural = ("PRI" in kt or "PK" in kt or "FK" in kt
                          or "FOREIGN" in kt or c.get("references_table"))
            dateish = ("date" in typ) or ("timestamp" in typ)
            numeric = any(k in typ for k in
                          ("int", "double", "decimal", "float", "numeric", "real"))
            name = str(c.get("name") or "").lower()
            blob = name + " " + str(c.get("description") or "").lower()
            relevant = _lex_related(qtok, blob)
            # numeric audit ids (uid/user_id) carry no measure signal.
            tech_numeric = numeric and _re.search(r"(uid|user_id)", name)
            if structural or dateish or relevant or (numeric and not tech_numeric):
                continue
            c["status"] = "rejected"
            pruned += 1
        # Never strip a table down to nothing meaningful.
        if pruned and not any(cc.get("status") != "rejected" for cc in cols):
            for cc in cols:
                cc["status"] = "selected"
            pruned = 0
        if pruned:
            bb.setdefault("trace", []).append(
                {"agent": "column_prune", "table": t.get("name"), "pruned": pruned})


def _prune_rules(bb: dict) -> None:
    """Drop business/user rules UNRELATED to this question, so the planner is not
    diluted by domain rules for domains the query never touches (e.g. REPO-leg
    rules on an РКО question). Recall-biased: ALWAYS keep the invariants and any
    rule sharing a token with the question OR ≥2 tokens with the FOCUSED schema
    (so universal SQL-craft rules, which name generic concepts present in the
    schema, survive). Deterministic; schema-agnostic; never raises."""
    if not getattr(Config, "BLACKBOARD_RULE_PRUNE_ENABLED", True):
        return
    rules = bb.get("selected_business_rules") or []
    if len(rules) <= 5:
        return
    qtok = _query_tokens((bb.get("request", {}) or {}).get("user_query", ""))
    schema_tok: set = set()
    for t in _bb_tables(bb):
        if t.get("status") == "rejected":
            continue
        schema_tok |= _query_tokens(str(t.get("description") or ""))
        for c in (t.get("columns") or []):
            if c.get("status") == "rejected":
                continue
            schema_tok |= _query_tokens(col_name(c) + " " + str(c.get("description") or ""))
    if not qtok and not schema_tok:
        return
    kept = []
    for r in rules:
        if str(r.get("id") or "") == "invariants":
            kept.append(r)
            continue
        text = str(r.get("text") or r.get("title") or "")
        rtok = _query_tokens(text)
        if _lex_related(qtok, text) or len(schema_tok & rtok) >= 2:
            kept.append(r)
    if kept and len(kept) < len(rules):
        bb["selected_business_rules"] = kept
        bb.setdefault("trace", []).append(
            {"agent": "rule_prune", "kept": len(kept), "from": len(rules)})


def _planner_system_prompt(bb: dict) -> str:
    rules = _render_rules(bb)
    schema = _render_schema(bb, include_status=True)
    knowledge = str(bb.get("knowledge") or "").strip()
    knowledge_block = (
        f"\n\nDATABASE BUSINESS KNOWLEDGE (domain conventions — use to choose the "
        f"authoritative source/measure):\n{knowledge[:3500]}" if knowledge else ""
    )
    feedback = feedback_as_text(bb)
    feedback_block = (
        f"\n\n{feedback}" if feedback else ""
    )
    decisive_block = (
        "\n\nDECISIVE MODE: a clarification was already considered. Re-read the "
        "schema descriptions and COMMIT to the single best mapping the metadata "
        "supports. The ONLY exception is genuine DOMAIN ambiguity: the wording "
        "names a generic object (договор/счёт/сделка) the schema realizes as "
        "SEVERAL distinct product/domain families (кредитный/РЕПО/РКО/брокерский) "
        "AND nothing in the wording — no domain word, no account-type cue "
        "(ссудный/расчётный…), no product/code — picks one; only THERE call "
        "need_clarification (committing would answer the wrong domain). In every "
        "other case do NOT ask — commit to the single best mapping."
        if bb.get("_decisive_mode") else ""
    )
    return f"""You are the PLANNER for a Text-to-SQL system. You select fields and \
relationships by CALLING TOOLS only. You never write prose answers and you never \
write SQL in this phase.

{rules}{knowledge_block}

SCHEMA (only these tables/columns exist; do NOT invent any other name):
{schema}{feedback_block}{decisive_block}

How to plan:
- DOMAIN FIRST — before binding any column, fix the object's product/domain family. \
If the wording's core object is a GENERIC «договор/сделка» that carries NO domain \
qualifier (none of кредитный/РЕПО/РКО/брокерский/депозитный, no account-type cue like \
«ссудный/расчётный», no product/code, no domain-specific column) AND the schema has \
more than one contract/deal family, there is NO basis to choose one family over \
another — call need_clarification asking which domain; never silently default to the \
most prominent or first-retrieved family. When a domain word or an unambiguous cue IS \
present, commit. (A generic «счёт» backed by a single account dimension is not \
ambiguous — commit.)
- MULTIPLE SOURCES FOR ONE CONCEPT — ASK, don't guess: when a SINGLE concept the \
question asks for (a metric, a status/flag, or a filter value) plausibly maps to MORE \
THAN ONE distinct column or table that would yield DIFFERENT results — e.g. several \
different fraud/score/amount columns living in different tables — and NOTHING in the \
wording picks one (no column or table name, no qualifier, no cue, no rule that names \
the source), call need_clarification with ONE short question that lists the candidate \
interpretations (table.column each) and asks which to use. Do NOT silently pick the \
first-retrieved or most prominent one. If a name, qualifier, or selected rule clearly \
identifies the intended source, COMMIT — do not ask. Asking is for genuine \
many-equally-valid-sources cases only, never as a default.
- Choose columns using the selected business rules above.
- Every decide_column, set_measure, set_grain, add_condition and add_join call \
MUST include rule_ids. If a selected rule supports the choice, cite its id(s). \
If NO selected rule applies, use rule_ids=[] and say so in the reason.
- Prefer an explicit numeric/measure column over a text code when the user asks \
for a quantity or amount.
- DATA PROFILE — DECISIVE: a column may show [ДАННЫЕ: …] with the GROUND TRUTH \
from the real data (NULL-ness + value range). It OVERRIDES the declared \
nullability. You MUST decide `IS NULL` vs `> D` from it: a date / event / \
execution / close column shown as «никогда не NULL (всегда заполнено)» means \
«ещё не наступило / не исполнено / не закрыто на D» = add_condition with \
operator `>` and value D — NEVER `is_null` (that column is always populated, so \
IS NULL returns nothing). Only when the profile shows the column CAN be NULL use \
the NULL-safe form (col IS NULL OR col > D). Pick >0 / range guards from the \
numeric profile when the metric excludes zeros.
- AS-OF VALIDITY (structural — applies even when NO word like \
действующий/актуальный is present): for EVERY table you use, separate its \
snapshot/report/balance date from a ROW-EFFECTIVITY WINDOW — a begin-of-effect + \
end-of-effect date pair whose DESCRIPTIONS say they bound when THAT \
row/link/assignment/role/balance-version is in effect. If a table exposes BOTH and \
request is as of a date D (including a plain «на дату D / за дату D»), add_condition \
for BOTH on that table: the snapshot date = D, AND begin <= D, AND the end-vs-D \
comparison from the end column's profile — the end date is INCLUSIVE (the last day \
in effect), so use `end >= D` when the end is [NOT NULL] / open sentinel like \
9999-12-31, or `(end IS NULL OR end >= D)` only when the profile says it can be \
NULL. (This is the row's effect window — NOT a not-yet-event date, which stays \
`> D`.) A snapshot-date filter does NOT satisfy \
row-effectivity; apply the window independently to EVERY perioded alias that has \
one. Do NOT treat an object's open/close/termination lifecycle date, an event date, \
or an audit/load date as a row-effectivity window — those are the lifecycle bullet.
- TEMPORAL / lifecycle: if the question says действующий/актуальный/активный/ \
открытый/«текущий статус» or asks open/closed/not-yet-happened, also add the \
lifecycle predicate on the OBJECT's own close/final/end date per its [ДАННЫЕ: …] \
profile; for a history/SCD table pick the current record; actual flag = 1 where \
present. Never use IS NULL on a [NOT NULL] column. For "за период/за месяц/за год" \
record a date RANGE (between start and end), not a single date.
- The schema above is FOCUSED: tables and columns unrelated to the question were \
pruned to keep you sharp. If you need a table or column that is NOT shown, do NOT \
invent it — call request_metadata_topup with a hint and purpose and it will be \
re-added (it is retrieved or un-hidden). Set blocking=true only if you cannot \
proceed without it.
- Call need_clarification ONLY in the genuinely undecidable DOMAIN case: the \
wording names a GENERIC object (договор/счёт/сделка/контрагент) the schema \
realizes as SEVERAL distinct product/domain families (кредитный/РЕПО/РКО/ \
брокерский) AND nothing in the wording picks one — no domain word, no \
account-type cue (ссудный/расчётный…), no product/code, no domain-specific \
column. If ANY such cue points to a family, or one source is clearly the best \
fit, COMMIT and do NOT ask. Never use request_metadata_topup to voice domain \
uncertainty — topup only fetches schema, it does not ask the user. In all other \
ambiguity, make the single most reasonable assumption and proceed.
- Select ONLY the columns the user explicitly asked to output, plus the \
keys/filters/joins needed to compute them. Do NOT add extra metrics, counts or \
columns the user did not ask for.
- When the plan is complete, call finalize_plan ONCE to end the planning phase. \
Do NOT repeat decisions you have already made.

You MUST respond with tool calls, never prose."""


def _render_plan(bb: dict) -> str:
    """Render the finalized plan from the blackboard for the SQL writer."""
    lines = []

    selected_cols = []
    for t in _bb_tables(bb):
        if t.get("status") == "rejected":
            continue
        for c in t.get("columns", []) or []:
            if c.get("status") == "selected":
                role = c.get("role") or "?"
                selected_cols.append(f"{t.get('name')}.{col_name(c)} (role={role})")
    if selected_cols:
        lines.append("Selected columns:")
        lines.extend(f"  - {x}" for x in selected_cols)

    measure = bb.get("measure")
    if measure:
        lines.append(
            "Measure: "
            f"{measure.get('aggregation')}({measure.get('table')}."
            f"{measure.get('column')}) as {measure.get('label')}"
        )

    grain = bb.get("grain")
    if grain and grain.get("dimensions"):
        dims = ", ".join(
            f"{d.get('table')}.{d.get('column')}"
            + (f"[{d.get('bucket')}]" if d.get("bucket") and d.get("bucket") != "none" else "")
            for d in grain["dimensions"]
        )
        lines.append(f"Grain (group by): {dims}")

    conditions = bb.get("conditions") or []
    if conditions:
        lines.append("Conditions:")
        for c in conditions:
            lines.append(
                f"  - [{c.get('kind')}] {c.get('table')}.{c.get('column')} "
                f"{c.get('operator')} {c.get('value')!r}"
            )

    joins = bb.get("joins") or []
    if joins:
        lines.append("Joins:")
        for j in joins:
            keys = ", ".join(
                f"{j.get('left_table')}.{k.get('left_column')}="
                f"{j.get('right_table')}.{k.get('right_column')}"
                for k in (j.get("keys") or [])
            )
            lines.append(
                f"  - {j.get('join_type')} {j.get('left_table')} <-> "
                f"{j.get('right_table')} ON {keys}"
            )

    return "\n".join(lines) if lines else "(planner produced no explicit plan)"


def _sql_system_prompt(bb: dict) -> str:
    dialect = (bb.get("request", {}) or {}).get("db_type") or "impala"
    plan = _render_plan(bb)
    schema = _render_schema(bb, include_status=False)
    # The SQL writer is the agent that emits the FINAL SQL and is also the agent
    # the validator's repair loop re-runs. It MUST see the business rules and the
    # domain knowledge — otherwise any rule the planner failed to encode as an
    # explicit condition (e.g. an active-link validity window) is lost here.
    rules = _render_rules(bb)
    knowledge = str(bb.get("knowledge") or "").strip()
    knowledge_block = (
        f"\n\nDATABASE BUSINESS KNOWLEDGE (domain conventions):\n{knowledge[:3500]}"
        if knowledge else ""
    )
    feedback = feedback_as_text(bb)
    feedback_block = f"\n\n{feedback}" if feedback else ""
    vfb = bb.get("_validator_feedback")
    if vfb:
        feedback_block += (
            "\n\nVALIDATION FEEDBACK on your previous SQL (you MUST fix all of "
            f"these):\n{vfb}"
        )
    return f"""You are the SQL WRITER for a Text-to-SQL system. You produce ONE \
read-only SQL statement for the {dialect} dialect by CALLING the set_sql tool. \
You never answer in prose.

{rules}{knowledge_block}

FINALIZED PLAN (a starting point — you MUST still enforce EVERY business rule \
above on the final SQL, adding any validity/period/grain predicate the plan \
omitted):
{plan}

SCHEMA (use ONLY these tables/columns; do NOT invent any other name):
{schema}{feedback_block}

Rules:
- Write exactly one read-only SELECT statement for the {dialect} dialect.
- Use ONLY the tables and columns shown above and follow the finalized plan \
(measure, grain, conditions, joins).
- SELECT ONLY the columns the user explicitly requested as outputs. Do NOT add \
extra metrics, counts or columns that were not asked for.
- "За период / за N месяцев / за год" filters the EVENT/report date over the \
whole interval (date BETWEEN start AND end), NOT a single latest snapshot or an \
open-ended >= bound.
- DATA PROFILE — DECISIVE: a column may show [ДАННЫЕ: …] = the ground truth from \
the real data (NULL-ness + range), which OVERRIDES the declared nullability. A \
date/event/execution column shown «никогда не NULL» is ALWAYS populated: express \
«ещё не наступило / не исполнено / не закрыто на D» as `col > D`, NEVER \
`col IS NULL` (returns nothing). Use (col IS NULL OR col > D) only when the \
profile says the column can be NULL.
- AS-OF VALIDITY (structural — applies even when NO keyword is present): for every \
alias, separate its snapshot/report/balance date from a ROW-EFFECTIVITY WINDOW (a \
begin/end date pair whose DESCRIPTIONS bound when THAT \
row/link/assignment/role/balance-version is in effect). If an alias has BOTH and the \
(including a plain «на дату D / за дату D» with no word действующий/актуальный), you \
MUST emit BOTH on it: the snapshot date = D, AND begin <= D, AND the end-vs-D \
comparison from the end column's [ДАННЫЕ: …] profile — the end date is INCLUSIVE, so \
`end >= D` for [NOT NULL] / open sentinel, or `(… IS NULL OR … >= D)` only if the \
profile says it can be NULL (a not-yet-event date is different and stays `> D`). The \
snapshot-date predicate does NOT satisfy row-effectivity; add the window to EVERY \
perioded alias that has one. Do not treat an object's open/close lifecycle date, an \
event date, or an audit/load date as a row-effectivity window.
- ACTIVE / CURRENT (lifecycle): if the question says действующий/актуальный/активный/ \
открытый/не закрыт/«текущий статус», you MUST add the validity condition on the \
object's own close/end/final date, choosing `> as-of` vs `(… IS NULL OR … > as-of)` \
from that column's [ДАННЫЕ: …] profile. For an SCD/history table with a start AND \
final date, select the CURRENT record — do NOT return all historical rows. Use an \
actual/current flag (=1) where the schema has one. NEVER write `IS NULL` / `= 0` \
on a column marked [NOT NULL] or shown «никогда не NULL»: it can never match.
- Include zero/empty values in AVG and SUM BY DEFAULT; add a `>0` / IS NOT NULL \
guard ONLY when the question explicitly asks to exclude inactive/zero rows. Do not \
assume «средний оборот / средний остаток» excludes zeros.
- TWO-SIDED TURNOVER (UNION): when the measure is an account's TOTAL turnover or \
operation count over a transaction/proceeding fact that records the account on TWO \
sides — a debit-side account key AND a credit-side account key (e.g. db_account_id / \
cr_account_id) — the account participates on EITHER side. Emit a `UNION ALL` of a \
debit-side branch and a credit-side branch: each branch selects that side's account \
key as the account id plus the operation amount/id under the SAME period filter; then \
JOIN the account dimension on that unified account id and aggregate per account. Do \
NOT flatten it into one query with two separate joins to the account dimension (that \
double-counts or drops rows). The finalized plan may show only one side's join — still \
emit BOTH sides via the UNION.
- Call set_sql with stage="draft", the SQL string, a short reason, and a \
confidence between 0 and 1.
- The schema and rules are FOCUSED — unrelated ones were pruned. If you need a \
field, table or relationship (join path) that is NOT shown, OR want to CHECK the \
business rules for guidance on something (validity, currency, grain, …), do NOT \
guess: call request_metadata_topup (missing_tables / missing_columns / \
rules=[topic]) with blocking=true. The fields/tables/relations/rules are \
retrieved and you are re-run with them visible — request, then use them.
- Call need_clarification only if the plan is too incomplete to write any SQL.

You MUST respond with a tool call, never prose."""


# ---------------------------------------------------------------------------
# Top-up bridge (sync or async callable)
# ---------------------------------------------------------------------------
def _run_topup(topup_fn: Callable, bb: dict) -> dict:
    """Call topup_fn(bb)->bb, supporting both sync and async callables."""
    if asyncio.iscoroutinefunction(topup_fn):
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already inside an event loop: run in a fresh loop on a thread.
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(
                        lambda: asyncio.new_event_loop().run_until_complete(topup_fn(bb))
                    ).result()
            return loop.run_until_complete(topup_fn(bb))
        except RuntimeError:
            return asyncio.new_event_loop().run_until_complete(topup_fn(bb))
    result = topup_fn(bb)
    if asyncio.iscoroutine(result):
        return asyncio.new_event_loop().run_until_complete(result)
    return result


# ---------------------------------------------------------------------------
# Legacy-answer mapping
# ---------------------------------------------------------------------------
def _build_query_analysis(bb: dict) -> str:
    parts = []
    measure = bb.get("measure")
    if measure:
        parts.append(
            f"measure={measure.get('aggregation')}({measure.get('column')})"
        )
    grain = bb.get("grain")
    if grain and grain.get("dimensions"):
        dims = ", ".join(
            f"{d.get('table')}.{d.get('column')}" for d in grain["dimensions"]
        )
        parts.append(f"grain=[{dims}]")
    conds = bb.get("conditions") or []
    if conds:
        parts.append(f"{len(conds)} condition(s)")
    joins = bb.get("joins") or []
    if joins:
        parts.append(f"{len(joins)} join(s)")
    selected = [
        f"{t.get('name')}.{col_name(c)}"
        for t in _bb_tables(bb)
        for c in (t.get("columns") or [])
        if c.get("status") == "selected"
    ]
    if selected:
        parts.append("columns: " + ", ".join(selected[:12]))
    return "Plan: " + ("; ".join(parts) if parts else "no explicit plan recorded")


# Canonical SCD / bitemporal validity-window column-name conventions. A
# row-effectivity window is a (begin-of-effect, end-of-effect) pair. These names
# deliberately EXCLUDE object-lifecycle dates (open/close of the account/contract
# itself, e.g. close_dt / open_date / plan_close_dt) and event dates (exec/payment):
# those are handled by the lifecycle rule, not by the as-of row-effectivity window.
_VALIDITY_START_TOKENS = (
    "date_from", "start_date", "valid_from", "effective_from", "effective_start",
    "begin_date", "dt_from", "period_from", "active_from", "actual_from", "eff_from",
)
_VALIDITY_END_TOKENS = (
    "date_to", "final_date", "valid_to", "effective_to", "effective_end",
    "end_date", "dt_to", "period_to", "active_to", "actual_to", "eff_to",
)
_SNAPSHOT_NAME_TOKENS = (
    "report_date", "balance_date", "rep_date", "rep_dt", "as_of", "asof",
    "snapshot", "oper_date", "operday", "on_date", "actual_date",
)
_SNAPSHOT_DESC_CUES = ("отчет", "отчёт", "баланс", "report", "snapshot", "as-of", "as of")
# PLANNED/expected dates are NOT factual row-effectivity (rule 11 forbids planned
# dates for closure), so a begin/end candidate carrying a planned cue is not a window
# bound — this keeps the gate off object plan-term dates like 'planned_final_date'.
_PLANNED_CUES = ("plan", "планир", "планов", "expected", "ожида", "прогноз", "forecast")
# Subject of the effect period. A begin/end pair is a ROW-effectivity window only when
# its description denotes the ROW/link/assignment/role/balance/status/record (not the
# OBJECT itself). When the description names ONLY the object's own term (договор/счёт/
# сделка) with no relationship/row word, it is OBJECT LIFECYCLE -> rule 11, not the gate.
_ROW_SUBJECT_CUES = (
    "связ", "остат", "примен", "роль", "роли", "назначен", "привязк", "запис",
    "версии", "версия", "статус", "атрибут", "страновы", "резидент", "okved",
    "link", "assignment", "relationship", "balance", "rest", "record", "version",
    "status", "role", "country",
)
_OBJECT_SUBJECT_CUES = (
    "договор", "контракт", "сделк", "соглашен", "agreement", "contract", "deal",
)


def _is_object_lifecycle(desc: str) -> bool:
    """True if the column description names ONLY the object's own term (no row/link/
    role/balance/status word) — i.e. object lifecycle, handled by rule 11, not the gate."""
    d = desc or ""
    if any(cue in d for cue in _ROW_SUBJECT_CUES):
        return False
    return any(cue in d for cue in _OBJECT_SUBJECT_CUES)


def _name_matches(name: str, tokens: tuple) -> bool:
    """True if the (lowercased) column name equals or ends with a canonical token,
    e.g. 'leg1_start_date' matches 'start_date'. Name-convention based, schema-agnostic."""
    return any(name == tok or name.endswith("_" + tok) or name.endswith(tok)
               for tok in tokens)


def _temporal_roles(columns: list) -> dict:
    """Classify a table's date columns into {snapshots:[...], windows:[[start,end],...]}
    from METADATA ONLY (column name conventions + description cues) — never from SQL or
    the question. A validity window requires BOTH a start-of-effect and an end-of-effect
    column by canonical SCD naming; object open/close lifecycle dates and planned/expected
    dates are intentionally not matched, so the gate does not over-apply to them."""
    starts: list = []
    ends: list = []
    snaps: list = []
    for c in columns or []:
        nm = col_name(c).lower()
        if not nm:
            continue
        typ = str(c.get("type") or "").lower()
        is_date = ("date" in typ) or ("time" in typ)
        desc = str(c.get("description") or "").lower()
        if any(tok in nm for tok in _SNAPSHOT_NAME_TOKENS) or (
            is_date and any(cue in desc for cue in _SNAPSHOT_DESC_CUES)
        ):
            snaps.append(nm)
        # An object's own lifecycle term (договора/счёта, with no relationship/row word)
        # belongs to rule 11, not the as-of window gate. NOTE: a "плановая"/planned word
        # in the DESCRIPTION is NOT an exclusion — a row's effect window is often planned
        # (e.g. «окончания действия остатка (плановая)»); only a planned NAME is excluded.
        if any(cue in nm for cue in _PLANNED_CUES):
            continue
        if _is_object_lifecycle(desc):
            continue
        if _name_matches(nm, _VALIDITY_START_TOKENS):
            starts.append(nm)
        if _name_matches(nm, _VALIDITY_END_TOKENS):
            ends.append(nm)
    windows: list = []
    for i in range(min(len(starts), len(ends))):
        windows.append([starts[i], ends[i]])
    return {"snapshots": snaps, "windows": windows}


def _schema_for_validator(bb: dict) -> dict:
    """Build the {table: {columns:{col:{nullable,key,ref_table,ref_col,samples}},
    pk:[...], snapshot_dates:[...], validity_windows:[[start,end],...]}} map the
    deterministic validator needs, from the blackboard tables. The temporal-role
    fields are classified from metadata so the validator stays pure-AST."""
    sch: dict = {}
    for t in bb.get("tables", []):
        tn = str(t.get("name", "")).lower()
        cols: dict = {}
        pk: list = []
        for c in (t.get("columns") or []):
            cn = col_name(c).lower()
            if not cn:
                continue
            nv = c.get("nullable")
            not_null = (str(nv).strip().upper() in ("NO", "FALSE", "0")) or (nv is False)
            kt = str(c.get("key_type") or "").upper()
            is_pk = ("PRI" in kt) or ("PK" in kt) or ("PRIMARY" in kt)
            ref_t = str(c.get("references_table") or "").lower() or None
            cols[cn] = {
                "nullable": not not_null,
                "key": "PK" if is_pk else ("FK" if ref_t else ""),
                "ref_table": ref_t,
                "ref_col": str(c.get("references_column") or "").lower() or None,
                "samples": list(c.get("sample_values") or []),
            }
            if is_pk:
                pk.append(cn)
        roles = _temporal_roles(t.get("columns") or [])
        sch[tn] = {
            "columns": cols,
            "pk": pk,
            "snapshot_dates": roles["snapshots"],
            "validity_windows": roles["windows"],
        }
    return sch


def _legacy_answer(bb: dict, sql_produced: bool, clarified: bool) -> dict:
    sql_obj = bb.get("sql") or {}
    sql_query = sql_obj.get("draft") or sql_obj.get("final") or ""
    # Deterministic LAST WORD on the blackboard's answer SQL: the model can emit
    # mechanically-fixable defects (e.g. asymmetric case-folding `col = LOWER('X')`
    # → folds only the literal → zero rows). Run the sqlglot gate registry here, on
    # the exact SQL that becomes the answer/executed query. General + fail-safe.
    if sql_query:
        try:
            import logging as _lg
            from api.sql_utils.gate_registry import run_gates as _rg, GateContext as _GC
            _db_type = (bb.get("request", {}) or {}).get("db_type") or "postgresql"
            _g, _gi, _gr = _rg(sql_query, _GC(db_type=_db_type))
            _lg.info("DBG _legacy_answer gate db=%s repaired=%s issues=%s in=%r",
                     _db_type, _gr, _gi, sql_query[:90])
            if _gr and _g:
                sql_query = _g
                sql_obj["draft"] = _g
        except Exception as _e:  # pylint: disable=broad-exception-caught
            try:
                import logging as _lg2
                _lg2.warning("legacy_answer gate skipped: %s", str(_e)[:120])
            except Exception:  # pylint: disable=broad-exception-caught
                pass
    confidence = sql_obj.get("confidence")
    questions = bb.get("clarification_questions") or []
    missing = bb.get("missing_information") or []
    translatable = bool(sql_produced and sql_query and not clarified)
    return {
        "is_sql_translatable": translatable,
        "sql_query": sql_query,
        "query_analysis": _build_query_analysis(bb),
        "missing_information": "; ".join(str(m) for m in missing),
        "ambiguities": "; ".join(str(q) for q in questions),
        "confidence": float(confidence) if isinstance(confidence, (int, float)) else (
            0.7 if translatable else 0.0
        ),
        "output_mode": "AGGREGATED_METRIC",
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def _debug_enabled() -> bool:
    """Debug capture is OFF unless QW_BB_DEBUG_DUMP is explicitly enabled."""
    return os.getenv("QW_BB_DEBUG_DUMP", "").strip().lower() in {"1", "true", "yes", "on"}


def _record_conversation(bb: dict, phase: str, messages: list) -> None:
    """Capture the FULL agent transcript for a phase — the system prompt the
    agent was given plus every assistant tool call and tool result — so the
    whole prompt→action chain is visible. Debug-only."""
    turns = []
    for m in messages or []:
        entry = {"role": m.get("role")}
        if m.get("content"):
            entry["content"] = m.get("content")
        if m.get("tool_calls"):
            entry["tool_calls"] = [
                {"name": (c.get("function") or {}).get("name"),
                 "arguments": (c.get("function") or {}).get("arguments")}
                for c in m["tool_calls"]
            ]
        if m.get("role") == "tool":
            entry["tool_name"] = m.get("name")
            entry["result"] = m.get("content")
        turns.append(entry)
    bb.setdefault("debug_conversations", []).append({"phase": phase, "turns": turns})


def _dump_debug(bb: dict, planner_prompt: Optional[str]) -> None:
    """In debug mode, write the EXACT material the agents worked from — the
    rendered planner & SQL-writer prompts, every blackboard column with its full
    metadata (samples, data_profile, nullability, FK, description), the selected
    rules, the knowledge, the tool calls, the assembled plan and the SQL — to
    /tmp/qw_debug/<slug>.json so a human can see what info was (or wasn't) there.
    Never raises."""
    try:
        d = os.getenv("QW_BB_DEBUG_DIR", "/tmp/qw_debug")
        os.makedirs(d, exist_ok=True)
        q = (bb.get("request", {}) or {}).get("user_query", "")
        slug = _re.sub(r"[^0-9A-Za-zА-Яа-я]+", "_", q)[:48].strip("_") or "q"
        h = hashlib.md5(q.encode("utf-8")).hexdigest()[:6]
        col_keys = ("name", "type", "nullable", "data_profile", "key_type",
                    "sample_values", "references_table", "references_column",
                    "description", "role", "status")
        payload = {
            "question": q,
            "candidates_in_bb": [t.get("name") for t in bb.get("tables", [])],
            "tables": [
                {
                    "name": t.get("name"),
                    "status": t.get("status"),
                    "description": t.get("description"),
                    "columns": [
                        {k: c.get(k) for k in col_keys}
                        for c in (t.get("columns") or [])
                    ],
                }
                for t in bb.get("tables", [])
            ],
            "selected_rules": [
                {"id": r.get("id"), "title": r.get("title")}
                for r in bb.get("selected_business_rules", [])
            ],
            "knowledge": (bb.get("knowledge") or "")[:2000],
            "planner_system_prompt": planner_prompt,
            "sql_writer_system_prompt": _sql_system_prompt(bb),
            "plan": {
                "measure": bb.get("measure"),
                "grain": bb.get("grain"),
                "conditions": bb.get("conditions"),
                "joins": bb.get("joins"),
            },
            "tool_calls": [t for t in bb.get("trace", []) if isinstance(t, dict)],
            # Full prompt->action transcript of EVERY agent phase (planner, each
            # sql-writer / repair round): system prompt + tool calls + results.
            "conversations": bb.get("debug_conversations", []),
            "validation": bb.get("validation"),
            "sql": bb.get("sql"),
        }
        with open(os.path.join(d, f"{slug}_{h}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        logger.warning("BB-DEBUG dumped: %s/%s_%s.json", d, slug, h)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("BB-DEBUG dump failed: %s", str(exc)[:200])


class ToolBlackboardPipeline:
    """Two-phase tool-call pipeline: rule-aware planner -> SQL writer."""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        max_planner_rounds: int = 4,
        max_sql_rounds: int = 2,
        max_repair_rounds: int = 2,
    ):
        self.model = model or Config.COMPLETION_MODEL
        self.api_key = api_key or Config.COMPLETION_API_KEY
        self.max_planner_rounds = max_planner_rounds
        self.max_sql_rounds = max_sql_rounds
        self.max_repair_rounds = max_repair_rounds

    # -- Phase 1 ----------------------------------------------------------
    def _run_planner(self, bb: dict) -> tuple[str, dict]:
        user_query = (bb.get("request", {}) or {}).get("user_query", "")
        messages = [
            {"role": "system", "content": _planner_system_prompt(bb)},
            {
                "role": "user",
                "content": f"User question: {user_query}\n\n"
                "Plan the answer by calling tools.",
            },
        ]
        result = run_tool_agent(
            messages,
            TOOLS_PLANNER,
            allowed_terminals={
                "finalize_plan",
                "need_clarification",
                "request_metadata_topup",
            },
            max_rounds=self.max_planner_rounds,
            bb=bb,
            model=self.model,
            api_key=self.api_key,
        )
        if _debug_enabled():
            _record_conversation(bb, "planner", messages)
        return result

    # -- Phase 2 ----------------------------------------------------------
    def _run_sql_writer(self, bb: dict) -> tuple[str, dict]:
        user_query = (bb.get("request", {}) or {}).get("user_query", "")
        messages = [
            {"role": "system", "content": _sql_system_prompt(bb)},
            {
                "role": "user",
                "content": f"User question: {user_query}\n\n"
                "Write the SQL by calling set_sql.",
            },
        ]
        result = run_tool_agent(
            messages,
            TOOLS_SQL,
            allowed_terminals={
                "set_sql",
                "need_clarification",
                "request_metadata_topup",
            },
            max_rounds=self.max_sql_rounds,
            bb=bb,
            model=self.model,
            api_key=self.api_key,
        )
        if _debug_enabled():
            _record_conversation(bb, "sql_writer", messages)
        return result

    def _run_schema_cleanup(self, bb: dict) -> None:
        """CYCLE 1 — AI schema cleanup at the TABLE level. The FULL FK/semantic-
        related schema is already loaded; the agent REMOVES only whole tables that
        are DEFINITELY unrelated, judging by MEANING. Every kept table keeps ALL of
        its columns (with their FK relationships and descriptions) — columns are
        never pruned (that risks dropping a needed one; the planner selects among
        them). Recall-biased ("сомнения оставить"); a dropped table is re-addable
        точечно via request_metadata_topup. Never raises."""
        if not getattr(Config, "BLACKBOARD_TABLE_FOCUS_ENABLED", True):
            return
        live = [t for t in _bb_tables(bb) if t.get("status") != "rejected"]
        if len(live) <= 2:
            return
        user_query = (bb.get("request", {}) or {}).get("user_query", "")
        messages = [
            {"role": "system", "content": _schema_focus_system_prompt(bb)},
            {"role": "user", "content": f"User question: {user_query}\n\n"
             "Remove the whole tables that are DEFINITELY not needed by calling "
             "prune_schema. Keep every table you are unsure about (with all its "
             "columns)."},
        ]
        try:
            msg = _tool_completion(messages, [_PRUNE_SCHEMA], self.model, self.api_key)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("schema_cleanup: completion failed: %s", str(exc)[:200])
            return
        drop_tables: set = set()
        for c in (getattr(msg, "tool_calls", None) or []):
            if c.function.name != "prune_schema":
                continue
            try:
                args = json.loads(c.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for nm in (args.get("drop_tables") or []):
                drop_tables.add(str(nm).split(".")[-1].lower())

        if not drop_tables:
            return
        for t in _bb_tables(bb):
            short = str(t.get("name", "")).split(".")[-1].lower()
            if short in drop_tables:
                t["status"] = "rejected"
        # Guard: never strip every table away.
        if not any(t.get("status") != "rejected" for t in _bb_tables(bb)):
            for t in _bb_tables(bb):
                t["status"] = "selected"
        bb.setdefault("trace", []).append(
            {"agent": "schema_cleanup", "dropped_tables": sorted(drop_tables)})

    def _run_rule_cleanup(self, bb: dict) -> None:
        """CYCLE 2 — AI rule cleanup (separate from schema). The agent REMOVES only
        the rules DEFINITELY irrelevant to the question on the focused schema.
        RECALL-BIASED: keep when in doubt; the invariants are never removed.
        Never raises."""
        if not getattr(Config, "BLACKBOARD_RULE_PRUNE_ENABLED", True):
            return
        rules = bb.get("selected_business_rules") or []
        numbered = [r for r in rules if str(r.get("id")) != "invariants"]
        if len(numbered) <= 4:
            return
        user_query = (bb.get("request", {}) or {}).get("user_query", "")
        messages = [
            {"role": "system", "content": _rule_focus_system_prompt(bb)},
            {"role": "user", "content": f"User question: {user_query}\n\n"
             "Remove the rules DEFINITELY not relevant by calling prune_rules. "
             "Keep everything you are unsure about."},
        ]
        try:
            msg = _tool_completion(messages, [_PRUNE_RULES], self.model, self.api_key)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning("rule_cleanup: completion failed: %s", str(exc)[:200])
            return
        drop_r: set = set()
        for c in (getattr(msg, "tool_calls", None) or []):
            if c.function.name != "prune_rules":
                continue
            try:
                args = json.loads(c.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            for rid in (args.get("drop_rule_ids") or []):
                drop_r.add(str(rid).strip())
        drop_r.discard("invariants")
        if drop_r:
            kept = [r for r in rules if str(r.get("id")) not in drop_r]
            if kept and len(kept) < len(rules):
                bb["selected_business_rules"] = kept
                bb.setdefault("trace", []).append(
                    {"agent": "rule_cleanup", "dropped": sorted(drop_r),
                     "from": len(rules), "kept": len(kept)})

    def run(self, bb: dict, topup_fn: Optional[Callable] = None) -> dict:
        """Run planner then SQL writer; mutate ``bb``; return a legacy answer dict."""
        bb.setdefault("trace", [])

        # Keep the FULL ruleset so a rule pruned below stays re-addable on demand
        # (request_metadata_topup rules=[...]).
        if "_all_rules" not in bb:
            bb["_all_rules"] = list(bb.get("selected_business_rules") or [])

        # Phase 0: AI-driven focus in two cycles — (1) schema cleanup (drop the
        # tables/columns definitely not needed), then (2) rule cleanup (drop the
        # rules definitely not relevant). Pruned items (fields, tables, relations,
        # rules) are re-addable точечно via request_metadata_topup.
        self._run_schema_cleanup(bb)
        self._run_rule_cleanup(bb)

        # Capture the planner prompt AFTER focus, so the debug dump shows the
        # exact (focused) schema/rules/profile material the planner first saw.
        _dbg_prompt = _planner_system_prompt(bb) if _debug_enabled() else None

        # Phase 1: planner.
        outcome, info = self._run_planner(bb)

        # Blocking metadata top-up: fulfil once and rerun the planner.
        if (
            outcome == "request_metadata_topup"
            and info.get("blocking")
            and topup_fn is not None
            and can_topup(bb)
        ):
            try:
                updated = _run_topup(topup_fn, bb)
                if isinstance(updated, dict):
                    bb = updated
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("tool_blackboard: topup_fn failed: %s", str(exc)[:300])
                bb.setdefault("trace", []).append(
                    {"type": "topup_error", "error": str(exc)[:300]}
                )
            outcome, info = self._run_planner(bb)

        # Planner asked for clarification -> not translatable.
        if outcome == "need_clarification":
            if _dbg_prompt is not None:
                _dump_debug(bb, _dbg_prompt)
            return _legacy_answer(bb, sql_produced=False, clarified=True)

        # Phase 2: SQL writer. The GENERATOR can точечно pull missing fields /
        # tables / relationships back from the RAG via request_metadata_topup —
        # apply un-hides any pruned item immediately, and a blocking request runs
        # the topup retrieval; then we RE-RUN the writer so it SEES (and uses) the
        # re-added schema in its refreshed prompt.
        sql_outcome, info = self._run_sql_writer(bb)
        for _ in range(int(getattr(Config, "BLACKBOARD_WRITER_TOPUP_ROUNDS", 2))):
            if sql_outcome != "request_metadata_topup":
                break
            if info.get("blocking") and topup_fn is not None and can_topup(bb):
                try:
                    updated = _run_topup(topup_fn, bb)
                    if isinstance(updated, dict):
                        bb = updated
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    logger.warning("tool_blackboard: writer topup failed: %s", str(exc)[:300])
            sql_outcome, info = self._run_sql_writer(bb)

        # Phase 2b: deterministic semantic validation -> repair via SQL writer.
        if sql_outcome not in ("need_clarification", "request_metadata_topup"):
            sql_outcome = self._validate_and_repair(bb, sql_outcome)

        clarified = sql_outcome == "need_clarification"
        sql_obj = bb.get("sql") or {}
        sql_produced = bool(sql_obj.get("draft") or sql_obj.get("final"))

        if _dbg_prompt is not None:
            _dump_debug(bb, _dbg_prompt)
        return _legacy_answer(bb, sql_produced=sql_produced and not clarified,
                              clarified=clarified)

    def _validate_and_repair(self, bb: dict, sql_outcome: str) -> str:
        """Run the deterministic semantic validator on the draft SQL; on issues,
        feed them back to the SQL writer for a repair round (gate validates +
        bounces to the model; it never rewrites SQL itself).

        A referential-integrity pass over the assembled blackboard runs first,
        so a plan that references a table/column/FK absent from the JSON is
        surfaced (and any error-severity dangle is fed back too)."""
        try:
            from api.agents.sql_semantic_validator import validate_sql
        except Exception:  # pylint: disable=broad-exception-caught
            validate_sql = None
        schema = _schema_for_validator(bb)
        db_type = (bb.get("request", {}) or {}).get("db_type") or "impala"

        # Referential integrity of the blackboard itself (observability + errors).
        try:
            integ = integrity_check(bb)
        except Exception:  # pylint: disable=broad-exception-caught
            integ = []
        bb.setdefault("validation", {})["integrity"] = integ
        integ_errors = [i for i in integ if i.get("severity") == "error"]

        for _ in range(getattr(self, "max_repair_rounds", 2)):
            sql = (bb.get("sql") or {}).get("draft")
            if not sql:
                break
            try:
                issues = validate_sql(sql, schema, db_type) if validate_sql else []
            except Exception:  # pylint: disable=broad-exception-caught
                issues = []
            bb.setdefault("validation", {})["semantic"] = issues
            combined = list(issues) + list(integ_errors)
            if not combined:
                break
            bb.setdefault("trace", []).append(
                {"agent": "semantic_validator",
                 "issues": [i.get("check") for i in combined]})
            bb["_validator_feedback"] = (
                "The previous SQL failed deterministic validation. FIX every issue:\n"
                + "\n".join(
                    f"- [{i.get('check')}] {i.get('table', '')}.{i.get('column', '')}: "
                    f"{i.get('message', '')} FIX: {i.get('fix_hint', '')}"
                    for i in combined)
            )
            sql_outcome, _ = self._run_sql_writer(bb)
            integ_errors = []  # only surfaced once; semantic check re-runs each round
            if sql_outcome == "need_clarification":
                break
        bb.pop("_validator_feedback", None)

        # Deterministic LAST WORD on the blackboard's SQL: the semantic validator
        # only FLAGS + bounces to the LLM (never rewrites), so model defects that
        # are mechanically fixable survive — notably asymmetric case-folding
        # `col = LOWER('X')` (folds only the literal → zero rows when the stored
        # value has any other case). Run the sqlglot gate registry to repair the
        # draft/final in place. General + dialect-aware; fail-safe.
        try:
            from api.sql_utils.gate_registry import run_gates as _run_gates, GateContext as _GateCtx
            _sqlobj = bb.get("sql") or {}
            for _k in ("draft", "final"):
                _cur = _sqlobj.get(_k)
                if not _cur:
                    continue
                _g, _gi, _gr = _run_gates(_cur, _GateCtx(db_type=db_type))
                if _gr and _g:
                    _sqlobj[_k] = _g
                    bb.setdefault("trace", []).append({"agent": "sqlglot_gate", "repaired": _gi})
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        return sql_outcome


# ---------------------------------------------------------------------------
# Self-test (no LLM calls)
# ---------------------------------------------------------------------------
def _self_test() -> int:
    failures = 0

    def check(label: str, cond: bool) -> None:
        nonlocal failures
        if cond:
            print(f"PASS: {label}")
        else:
            failures += 1
            print(f"FAIL: {label}")

    # Fake blackboard with two tables, one rule.
    bb = {
        "request": {"user_query": "total amount by region", "db_type": "impala"},
        "tables": [
            {
                "name": "orders",
                "description": "orders fact",
                "status": "selected",
                "columns": [
                    {"name": "amount", "type": "double", "description": "order amount",
                     "key_type": "", "sample_values": [1, 2], "status": "candidate"},
                    {"name": "region_id", "type": "int", "description": "region fk",
                     "key_type": "FK", "sample_values": [], "status": "candidate"},
                ],
            },
            {
                "name": "regions",
                "description": "region dim",
                "status": "selected",
                "columns": [
                    {"name": "id", "type": "int", "description": "pk",
                     "key_type": "PK", "sample_values": [], "status": "candidate"},
                    {"name": "name", "type": "string", "description": "region name",
                     "key_type": "", "sample_values": ["west"], "status": "candidate"},
                ],
            },
        ],
        "conditions": [],
        "joins": [],
        "selected_business_rules": [
            {"id": "rule.1", "title": "Amount metric", "text": "amount is the measure"},
        ],
        "missing_tables_request": [],
        "missing_columns_request": [],
        "sql": {"draft": None, "final": None},
        "user_feedback": [],
        "validation": {},
        "trace": [],
        "retrieval": {"topup_count": 0, "max_topups": 2},
        "_table_infos": {},
    }

    # 1. Unknown column is rejected (no mutation).
    raised = False
    try:
        validate_tool_args(
            "decide_column",
            json.dumps({"decision": "select", "role": "measure", "table": "orders",
                        "column": "nonexistent", "reason": "x", "rule_ids": []}),
            bb,
        )
    except ToolValidationError:
        raised = True
    check("unknown column rejected by validation", raised)

    # 1b. Unknown rule_id is rejected.
    raised = False
    try:
        validate_tool_args(
            "decide_column",
            json.dumps({"decision": "select", "role": "measure", "table": "orders",
                        "column": "amount", "reason": "x", "rule_ids": ["rule.999"]}),
            bb,
        )
    except ToolValidationError:
        raised = True
    check("unknown rule_id rejected by validation", raised)

    # 2. Valid decide_column sets status to selected.
    args = validate_tool_args(
        "decide_column",
        json.dumps({"decision": "select", "role": "measure", "table": "orders",
                    "column": "amount", "reason": "the measure", "rule_ids": ["rule.1"]}),
        bb,
    )
    apply_decide_column(args, bb)
    col = find_column(bb, "orders", "amount")
    check("valid decide_column sets status=selected", col["status"] == "selected")
    check("decide_column sets role", col.get("role") == "measure")

    # 3. add_condition upserts by condition_id.
    cond_args = {
        "condition_id": "c1", "kind": "where", "table": "regions", "column": "name",
        "operator": "=", "value": "west", "reason": "filter", "rule_ids": [],
    }
    apply_add_condition(validate_tool_args("add_condition", json.dumps(cond_args), bb), bb)
    apply_add_condition(
        validate_tool_args(
            "add_condition",
            json.dumps({**cond_args, "value": "east"}),
            bb,
        ),
        bb,
    )
    check("add_condition upserts by id (1 entry)", len(bb["conditions"]) == 1)
    check("add_condition upsert keeps latest value",
          bb["conditions"][0]["value"] == "east")

    # 3b. case-insensitive column lookup.
    ci_args = validate_tool_args(
        "decide_column",
        json.dumps({"decision": "select", "role": "grain", "table": "REGIONS",
                    "column": "NAME", "reason": "x", "rule_ids": []}),
        bb,
    )
    apply_decide_column(ci_args, bb)
    check("case-insensitive table/column lookup",
          find_column(bb, "regions", "name")["status"] == "selected")

    # 4. request_metadata_topup populates the requests.
    topup_args = validate_tool_args(
        "request_metadata_topup",
        json.dumps({
            "missing_tables": [{"table_hint": "customers", "purpose": "need customer"}],
            "missing_columns": [{"table": "orders", "column_hint": "order_date",
                                 "purpose": "time filter"}],
            "reason": "incomplete", "blocking": True,
        }),
        bb,
    )
    res = apply_request_metadata_topup(topup_args, bb)
    check("request_metadata_topup returns blocking flag", res.get("blocking") is True)
    check("request_metadata_topup populates table requests",
          len(bb["missing_tables_request"]) == 1)
    check("request_metadata_topup populates column requests",
          len(bb["missing_columns_request"]) == 1)

    # 5. set_sql writes the draft.
    sql_args = validate_tool_args(
        "set_sql",
        json.dumps({"stage": "draft",
                    "sql": "SELECT regions.name, SUM(orders.amount) FROM orders "
                           "JOIN regions ON orders.region_id = regions.id "
                           "GROUP BY regions.name",
                    "reason": "draft", "confidence": 0.8}),
        bb,
    )
    apply_set_sql(sql_args, bb)
    check("set_sql writes bb['sql']['draft']", bool(bb["sql"]["draft"]))
    check("set_sql stores confidence", bb["sql"].get("confidence") == 0.8)

    # 6. add_join validates known tables/columns and upserts.
    join_args = validate_tool_args(
        "add_join",
        json.dumps({"join_id": "j1", "left_table": "orders", "right_table": "regions",
                    "join_type": "inner",
                    "keys": [{"left_column": "region_id", "right_column": "id"}],
                    "reason": "join", "rule_ids": []}),
        bb,
    )
    apply_add_join(join_args, bb)
    check("add_join upserts join", len(bb["joins"]) == 1)

    # 6b. add_join with unknown column is rejected.
    raised = False
    try:
        validate_tool_args(
            "add_join",
            json.dumps({"join_id": "j2", "left_table": "orders",
                        "right_table": "regions", "join_type": "inner",
                        "keys": [{"left_column": "nope", "right_column": "id"}],
                        "reason": "join", "rule_ids": []}),
            bb,
        )
    except ToolValidationError:
        raised = True
    check("add_join with unknown column rejected", raised)

    # 7. legacy answer mapping shape.
    answer = _legacy_answer(bb, sql_produced=True, clarified=False)
    expected_keys = {"is_sql_translatable", "sql_query", "query_analysis",
                     "missing_information", "ambiguities", "confidence", "output_mode"}
    check("legacy answer has all keys", set(answer.keys()) == expected_keys)
    check("legacy answer is_sql_translatable=True when sql present",
          answer["is_sql_translatable"] is True)
    check("legacy answer sql_query == draft", answer["sql_query"] == bb["sql"]["draft"])
    check("legacy answer output_mode", answer["output_mode"] == "AGGREGATED_METRIC")

    # 8. clarified -> not translatable.
    bb2 = dict(bb)
    answer2 = _legacy_answer(bb2, sql_produced=True, clarified=True)
    check("clarified answer is not translatable", answer2["is_sql_translatable"] is False)

    # 9. Tool responses echo the resolved binding (self-describing).
    echo = apply_decide_column(
        validate_tool_args(
            "decide_column",
            json.dumps({"decision": "select", "role": "grain", "table": "regions",
                        "column": "name", "reason": "x", "rule_ids": []}),
            bb,
        ),
        bb,
    )
    check("decide_column echoes a 'bound' binding", isinstance(echo.get("bound"), dict))
    check("binding carries table+column",
          echo["bound"].get("table") == "regions" and echo["bound"].get("column") == "name")
    check("binding carries samples from canonical state",
          echo["bound"].get("samples") == ["west"])

    measure_echo = apply_set_measure(
        validate_tool_args(
            "set_measure",
            json.dumps({"measure_id": "m1", "label": "total", "table": "orders",
                        "column": "amount", "aggregation": "sum", "reason": "x",
                        "rule_ids": []}),
            bb,
        ),
        bb,
    )
    check("set_measure echoes binding + aggregation",
          measure_echo.get("bound", {}).get("ref")
          and measure_echo.get("aggregation") == "sum")

    join_echo = apply_add_join(
        validate_tool_args(
            "add_join",
            json.dumps({"join_id": "j3", "left_table": "orders",
                        "right_table": "regions", "join_type": "inner",
                        "keys": [{"left_column": "region_id", "right_column": "id"}],
                        "reason": "x", "rule_ids": []}),
            bb,
        ),
        bb,
    )
    check("add_join echoes per-key bindings with FK flag",
          isinstance(join_echo.get("keys"), list)
          and "is_declared_fk" in (join_echo["keys"][0] if join_echo["keys"] else {}))

    # 10. integrity_check resolves FK targets, PK marks and dangling references.
    from api.core.blackboard import integrity_check as _integrity, _column_to_bb
    # back-reference is stamped by _column_to_bb
    col_bb = _column_to_bb({"name": "amount", "type": "double"}, "orders")
    check("_column_to_bb stamps table back-reference", col_bb.get("table") == "orders")
    check("_column_to_bb stamps canonical ref", col_bb.get("ref") == "orders.amount")

    integ_bb = {
        "tables": [
            {"name": "fact", "status": "selected", "columns": [
                {"name": "id", "table": "fact", "ref": "fact.id", "key_type": "PRI"},
                {"name": "dim_id", "table": "fact", "ref": "fact.dim_id",
                 "key_type": "FK", "references_table": "missingdim",
                 "references_column": "id"},
            ]},
        ],
        "conditions": [
            {"condition_id": "c", "table": "fact", "column": "ghost",
             "operator": "=", "value": 1},
        ],
        "joins": [], "measure": {}, "grain": {},
    }
    integ = _integrity(integ_bb)
    checks_by = {i["check"] for i in integ}
    check("integrity flags FK target table absent", "fk_target_table_absent" in checks_by)
    check("integrity flags dangling condition reference", "dangling_reference" in checks_by)
    # clean bb (orders/regions, regions has PK) yields no error-severity issues
    integ_clean = _integrity(bb)
    check("integrity on clean bb has no error-severity issues",
          not any(i.get("severity") == "error" for i in integ_clean))

    print()
    if failures:
        print(f"SELF-TEST FAILED: {failures} failure(s)")
    else:
        print("SELF-TEST PASSED")
    return failures


if __name__ == "__main__":
    import sys

    sys.exit(1 if _self_test() else 0)
