"""Rule-gate agent: apply user preferences + business rules to generated SQL.

This is the rule-enforcement pass the owner specified: it sits AFTER the
generator and BEFORE the deterministic ``sql_gate`` (sqlglot). It receives the
user's latest question, the generated SQL, ALL user rules (generation
preferences such as "exclude zero values", "Russian column aliases", "prefer
CTEs"), and the RAG-selected business rules, plus a COMPACT schema context
(only the tables/columns the SQL already uses or that were selected). It rewrites
the SQL to comply with those rules and returns it.

Deliberately NOT a re-generation agent: it does not get the full schema, does
not pick tables, and is told to change as little as possible. The deterministic
``sql_gate`` runs after it and is the safety net for hallucinated identifiers, so
this agent is conservative by construction — when a rule cannot be applied
safely it leaves the SQL unchanged and reports the rule as unapplied.

Fail-safe: any parse/LLM error returns the original SQL untouched, so enabling
the gate can never make a previously-valid query worse than "no-op".
"""

import asyncio
import json
import logging
from typing import Any, Dict, List

from .utils import BaseAgent, run_completion


def _has_rule_text(text: str | None) -> bool:
    """True when *text* carries at least one non-trivial rule line."""
    if not text:
        return False
    stripped = "\n".join(
        line for line in text.splitlines() if line.strip()
    ).strip()
    # A bare heading ("# Rules") or a couple of stray chars is not actionable.
    return len(stripped) >= 8


def _extract_json_object(response: str) -> Dict[str, Any] | None:
    """Extract the last balanced top-level JSON object from *response*.

    Tolerant of ```json fences and of leading/trailing prose (gemma habitually
    wraps JSON in a code fence and adds a sentence around it).
    """
    if not response:
        return None
    blocks: List[str] = []
    depth = 0
    start = None
    for i, char in enumerate(response):
        if char == "{":
            if depth == 0:
                start = i
            depth += 1
        elif char == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    blocks.append(response[start:i + 1])
                    start = None
    for block in reversed(blocks):
        try:
            parsed = json.loads(block)
            if isinstance(parsed, dict) and "sql_query" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue
    return None


RULE_GATE_PROMPT = """You are a SQL RULE-COMPLIANCE editor for {DIALECT}.

You are given a SQL query that was already generated and is assumed schema-correct. Your ONLY job is to make it comply with the rules below by editing as little as possible. You do NOT design queries, pick tables, or change the business meaning.

USER QUESTION:
{QUESTION}

GENERATED SQL:
```sql
{SQL}
```

COMPACT SCHEMA CONTEXT (only columns already used or selected — name, type, description, sample values; this is the ONLY schema you may rely on):
{SCHEMA_CONTEXT}

USER GENERATION PREFERENCES (hard requirements on HOW the SQL should be written — apply every one that is relevant):
{USER_RULES}

BUSINESS RULES (domain conventions retrieved for this question and these columns — apply only those that clearly bear on this SQL):
{BUSINESS_RULES}

HOW TO EDIT:
1. Change ONLY what a rule requires. Preserve the query's tables, joins, filters, grouping, and result meaning otherwise.
2. Use ONLY identifiers that already appear in the GENERATED SQL or in the COMPACT SCHEMA CONTEXT. Never invent a table or column name.
3. If a rule cannot be applied safely with the given context (e.g. it needs a column that is not present), DO NOT guess — leave that part unchanged and list the rule under "unapplied_rules".
4. If the SQL already complies with every relevant rule, return it byte-for-byte unchanged with "changed": false.
5. Keep the SQL a single read-only statement valid for {DIALECT}.

RESPONSE FORMAT (return ONLY this JSON object, no prose, no code fence):
{{
  "sql_query": "<the SQL, edited or unchanged>",
  "changed": <true|false>,
  "applied_rules": ["short description of each rule you applied"],
  "unapplied_rules": ["short description of each relevant rule you could not apply, with the reason"]
}}
"""


class SqlGateAgent(BaseAgent):
    # pylint: disable=too-few-public-methods
    """Apply user + business rules to an already-generated SQL query."""

    @staticmethod
    def should_run(user_rules: str | None, business_rules: str | None) -> bool:
        """Skip the (slow) LLM pass when there is nothing actionable to apply."""
        return _has_rule_text(user_rules) or _has_rule_text(business_rules)

    async def apply(
        self,
        user_query: str,
        sql_query: str,
        *,
        user_rules: str | None = None,
        business_rules: str | None = None,
        schema_context: str | None = None,
        database_type: str | None = None,
    ) -> Dict[str, Any]:
        """Return ``{sql_query, changed, applied_rules, unapplied_rules}``.

        Fail-safe: on any error the original ``sql_query`` is returned unchanged.
        """
        original = sql_query or ""
        if not original.strip():
            return {"sql_query": original, "changed": False,
                    "applied_rules": [], "unapplied_rules": []}
        if not self.should_run(user_rules, business_rules):
            return {"sql_query": original, "changed": False,
                    "applied_rules": [], "unapplied_rules": []}

        dialect = (database_type or "SQL").upper()
        prompt = RULE_GATE_PROMPT.format(
            DIALECT=dialect,
            QUESTION=user_query or "",
            SQL=original,
            SCHEMA_CONTEXT=(schema_context or "(none provided)").strip(),
            USER_RULES=(user_rules or "(none)").strip(),
            BUSINESS_RULES=(business_rules or "(none)").strip(),
        )
        self.messages.append({"role": "user", "content": prompt})
        logging.info(
            "SqlGateAgent applying rules: dialect=%s user_rules_chars=%d "
            "business_rules_chars=%d schema_ctx_chars=%d sql_chars=%d",
            dialect, len(user_rules or ""), len(business_rules or ""),
            len(schema_context or ""), len(original),
        )
        try:
            answer = await asyncio.to_thread(
                run_completion,
                self.messages, self.custom_model, self.custom_api_key,
                temperature=0,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("SqlGateAgent LLM call failed (%s); keeping original SQL",
                            str(exc)[:200])
            return {"sql_query": original, "changed": False,
                    "applied_rules": [], "unapplied_rules": []}
        self.messages.append({"role": "assistant", "content": answer})

        parsed = _extract_json_object(answer)
        if not parsed:
            logging.warning("SqlGateAgent: unparseable response; keeping original SQL")
            return {"sql_query": original, "changed": False,
                    "applied_rules": [], "unapplied_rules": []}
        new_sql = str(parsed.get("sql_query") or "").strip()
        if not new_sql:
            return {"sql_query": original, "changed": False,
                    "applied_rules": [], "unapplied_rules": []}
        changed = new_sql != original.strip()
        applied = parsed.get("applied_rules") or []
        unapplied = parsed.get("unapplied_rules") or []
        if changed:
            logging.info(
                "SqlGateAgent rewrote SQL: applied=%s unapplied=%s",
                "; ".join(map(str, applied))[:300],
                "; ".join(map(str, unapplied))[:200],
            )
        else:
            logging.info("SqlGateAgent: SQL already compliant (no change)")
        return {
            "sql_query": new_sql,
            "changed": changed,
            "applied_rules": applied if isinstance(applied, list) else [str(applied)],
            "unapplied_rules": unapplied if isinstance(unapplied, list) else [str(unapplied)],
        }
