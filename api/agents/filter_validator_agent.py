"""Filter validator — one focused job: check that every WHERE/HAVING predicate in
a generated query is JUSTIFIED, and drop the ones that are not.

The generator (a weak model) sometimes invents a restricting condition the user
never asked for — most often a numeric threshold lifted from a metric FORMULA
constant (turning a literal used inside the metric expression into a WHERE bound). That
silently changes the result set. The linker already emits the JUSTIFIED filters
separately into the plan JSON; this agent compares the SQL's actual predicates
against the question + those authorized filters and removes any that are invented,
leaving everything else (tables, joins, grouping, projections, ordering, the
metric expressions) byte-identical.

Fail-safe: any parse/LLM error returns the original SQL untouched.
"""

import asyncio
import json
import logging
import re

from .utils import BaseAgent, run_tool_completion, run_completion


_WHERE_RE = re.compile(r"\b(where|having)\b", re.IGNORECASE)


_FILTER_TOOL = [{
    "type": "function",
    "function": {
        "name": "submit_validated_sql",
        "description": "Return the SQL with only JUSTIFIED filters kept (invented predicates removed).",
        "parameters": {
            "type": "object",
            "properties": {
                "sql_query": {"type": "string", "description": "the SQL with any unjustified WHERE/HAVING predicate removed; everything else (tables, joins, grouping, projections, ordering, metric expressions) byte-identical"},
                "removed": {"type": "array", "items": {"type": "string"}, "description": "each predicate removed, with why it was not justified"},
                "changed": {"type": "boolean"},
            },
            "required": ["sql_query", "changed"],
        },
    },
}]


FILTER_VALIDATOR_PROMPT = """You validate ONLY the FILTERS of an already-generated {DIALECT} SQL query. Do NOT change its tables, joins, grouping, projections, ordering, or metric expressions — touch ONLY the WHERE / HAVING predicates.

A predicate is JUSTIFIED only if at least one holds:
- the QUESTION explicitly states it (a concrete value, threshold, range, status, date, "at least N", "more than N", "in <period>", a named status/category, etc.), OR
- it appears in the AUTHORIZED FILTERS below, OR
- it is a NULL/existence guard required to make a chosen column meaningful (e.g. "<col> IS NOT NULL" when the metric needs a present value).

REMOVE any predicate that is NOT justified — in particular:
- a numeric constant that is part of a metric's FORMULA (a literal used INSIDE the metric expression is arithmetic, NOT a filter) — never turn such a formula literal into a WHERE/HAVING threshold,
- any restricting condition the question never asked for.

Keep every justified predicate exactly as-is. If all predicates are justified, return the SQL unchanged with changed=false.

QUESTION:
{QUESTION}

AUTHORIZED FILTERS (from the schema-link plan / resolved concepts):
{FILTERS}

SQL:
```sql
{SQL}
```

Return your result by calling submit_validated_sql."""


class FilterValidatorAgent(BaseAgent):
    # pylint: disable=too-few-public-methods
    """Drop WHERE/HAVING predicates not justified by the question or the plan."""

    @staticmethod
    def should_run(sql_query: str | None) -> bool:
        """Only worth an LLM call when the SQL actually has a filter clause."""
        return bool(sql_query and _WHERE_RE.search(sql_query))

    async def validate(
        self,
        user_query: str,
        sql_query: str,
        authorized_filters: str | None = None,
        database_type: str | None = None,
    ) -> dict:
        original = sql_query or ""
        if not original.strip() or not self.should_run(original):
            return {"sql_query": original, "changed": False, "removed": []}
        prompt = FILTER_VALIDATOR_PROMPT.format(
            DIALECT=(database_type or "SQL").upper(),
            QUESTION=user_query or "",
            FILTERS=(authorized_filters or "(none specified)").strip(),
            SQL=original,
        )
        self.messages.append({"role": "user", "content": prompt})
        logging.info("FilterValidator checking: sql_chars=%d filters_chars=%d",
                     len(original), len(authorized_filters or ""))
        try:
            answer = await asyncio.to_thread(
                run_tool_completion, self.messages, _FILTER_TOOL,
                self.custom_model, self.custom_api_key, "submit_validated_sql",
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("FilterValidator tool failed (%s); plain completion", str(exc)[:120])
            try:
                answer = await asyncio.to_thread(
                    run_completion, self.messages, self.custom_model,
                    self.custom_api_key, temperature=0,
                )
            except Exception as exc2:  # pylint: disable=broad-exception-caught
                logging.warning("FilterValidator failed (%s); keeping original", str(exc2)[:160])
                return {"sql_query": original, "changed": False, "removed": []}
        try:
            obj = json.loads(answer, strict=False)
        except (json.JSONDecodeError, TypeError):
            obj = None
        if not isinstance(obj, dict):
            return {"sql_query": original, "changed": False, "removed": []}
        new_sql = str(obj.get("sql_query") or "").strip()
        if not new_sql:
            return {"sql_query": original, "changed": False, "removed": []}
        removed = obj.get("removed") or []
        changed = new_sql != original.strip()
        if changed:
            logging.info("FilterValidator removed predicate(s): %s",
                         "; ".join(map(str, removed))[:240])
        else:
            logging.info("FilterValidator: all filters justified (no change)")
        return {"sql_query": new_sql, "changed": changed,
                "removed": removed if isinstance(removed, list) else [str(removed)]}
