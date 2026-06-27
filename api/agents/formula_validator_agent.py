"""Formula validator — a narrow critic that re-checks a resolved metric's column
bindings against the concept DEFINITION and rebinds a mis-bound term.

The resolver (a weak model under a noisy schema) intermittently binds a
plausible-but-wrong column to one term of a formula — e.g. counting "finished"
by a points column when the definition says the finish state is a status/mark
column. The deterministic guards can DROP a structurally-broken formula (phantom
column, identity stub) but cannot tell a valid-but-wrong column from the right
one — that needs reading the definition against the columns' descriptions. This
agent does exactly that, in a TINY focused context (one formula + its definition
+ the candidate columns), and returns the formula with only mis-bound columns
swapped (math/structure untouched). General: no table/column/domain names in the
prompt; it reasons purely over the supplied definition and column descriptions.
"""

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Tuple

from .utils import BaseAgent, run_tool_completion

_VALIDATE_TOOL = [{
    "type": "function",
    "function": {
        "name": "submit_validated_metrics",
        "description": "Return each metric with its column bindings verified/corrected against its definition.",
        "parameters": {
            "type": "object",
            "properties": {
                "metrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "sql_expression": {"type": "string"},
                            "filter": {"type": "string"},
                            "changed": {"type": "boolean"},
                            "why": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["metrics"],
        },
    },
}]


_VALIDATOR_PROMPT = """You VERIFY that each resolved metric's SQL binds every part to the column whose MEANING matches the metric's DEFINITION — and fix ONLY a mis-bound column. Target dialect: {DIALECT}.

A weak resolver sometimes uses a plausible-but-WRONG column for one term (e.g. it counts a "finished/classified" state by a points/score column, when the definition says that state is carried by a status/mark/flag column). Your job: catch that and rebind to the column whose DESCRIPTION matches the definition's wording for that term.

AVAILABLE COLUMNS (each: `table.column — description` / JSON leaf `path — description`). Use ONLY these EXACT names:
{SCHEMA}

For each metric below you get its NAME, its DEFINITION (authoritative meaning), and its current SQL_EXPRESSION and FILTER. For EVERY column the expression/filter references:
- decide what ROLE it plays in the definition (the measured quantity, the thing being counted, the grouping key, the condition…);
- check the column's DESCRIPTION matches that role's wording in the DEFINITION;
- if it matches → keep it; if it is the WRONG column for that role AND another AVAILABLE column's description matches the role BETTER → REBIND that one reference to the better column.

STRICT rules:
- Change ONLY a mis-bound column. Keep every operator, constant, function, CASE/WHEN, and the overall structure IDENTICAL — you are swapping a column, never rewriting the formula.
- Every column you output MUST appear VERBATIM in AVAILABLE COLUMNS. Never invent or rename a column or JSON path.
- A NULL/absence-defined condition stays as-is in form (IS NULL vs IS NOT NULL) unless the column itself was wrong; do not flip polarity on your own.
- If you are not clearly more correct, LEAVE IT UNCHANGED (set changed=false). A needless change is worse than none.
Return via submit_validated_metrics: one item per metric with name, sql_expression, filter, changed (bool), why (<=12 words)."""


def _extract_json_array(text: str):
    if not text:
        return None
    depth = 0
    start = -1
    last = None
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and start >= 0:
                last = text[start:i + 1]
    if last:
        try:
            return json.loads(last)
        except Exception:  # pylint: disable=broad-exception-caught
            return None
    return None


def _parse(answer: str) -> List[Dict[str, Any]]:
    if not answer:
        return []
    obj = None
    try:
        obj = json.loads(answer)
    except Exception:  # pylint: disable=broad-exception-caught
        obj = None
    if isinstance(obj, dict) and isinstance(obj.get("metrics"), list):
        return obj["metrics"]
    if isinstance(obj, list):
        return obj
    arr = _extract_json_array(answer)
    return arr if isinstance(arr, list) else []


class FormulaValidatorAgent(BaseAgent):
    # pylint: disable=too-few-public-methods
    """Re-check + rebind mis-bound columns in resolved metric formulas."""

    async def validate(
        self,
        resolved: List[Dict[str, Any]],
        concept_defs: Dict[str, str],
        schema_context: str,
        database_type: str | None = None,
    ) -> List[Dict[str, Any]]:
        # Only metrics that HAVE a formula and a known definition are worth checking.
        checkable = [
            r for r in (resolved or [])
            if (r.get("sql_expression") or "").strip()
            and concept_defs.get(r.get("name", ""))
        ]
        if not checkable or not (schema_context or "").strip():
            return resolved
        blocks = []
        for r in checkable:
            blocks.append(
                f"### {r['name']}\nDEFINITION: {concept_defs.get(r['name'], '')}\n"
                f"SQL_EXPRESSION: {r.get('sql_expression') or ''}\n"
                f"FILTER: {r.get('filter') or ''}"
            )
        prompt = _VALIDATOR_PROMPT.format(
            DIALECT=(database_type or "SQL").upper(),
            SCHEMA=(schema_context or "(none)").strip(),
        ) + "\n\nMETRICS TO VERIFY:\n" + "\n\n".join(blocks)
        self.messages.append({"role": "user", "content": prompt})

        def _one() -> str:
            return run_tool_completion(
                self.messages, _VALIDATE_TOOL, self.custom_model,
                self.custom_api_key, "submit_validated_metrics",
            )

        try:
            answer = await asyncio.to_thread(_one)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("FormulaValidator failed (%s); keeping resolved as-is", str(exc)[:160])
            return resolved
        items = _parse(answer)
        if not items:
            return resolved
        # Apply corrections by name. Only accept a change that (a) is flagged
        # changed, (b) is non-empty, (c) references no token outside the original
        # plus the schema (cheap guard against a rewrite/hallucination); the
        # downstream grounding guard re-validates columns anyway.
        by_name = {str(it.get("name", "")): it for it in items}
        out: List[Dict[str, Any]] = []
        for r in (resolved or []):
            it = by_name.get(r.get("name", ""))
            if not it or not it.get("changed"):
                out.append(r)
                continue
            new_expr = (it.get("sql_expression") or "").strip()
            new_filter = it.get("filter")
            if new_expr and new_expr != (r.get("sql_expression") or "").strip():
                r = dict(r)
                old = r["sql_expression"]
                r["sql_expression"] = new_expr
                if new_filter is not None:
                    r["filter"] = str(new_filter).strip()
                logging.info("FormulaValidator rebound %s: %s -> %s (%s)",
                             r.get("name"), (old or "")[:80], new_expr[:80],
                             str(it.get("why", ""))[:60])
            out.append(r)
        return out
