"""Query-memory recommender — advisory prior-example recall.

Replaces graphiti's fragile conversational entity-extraction for the one use
case that actually helps SQL generation: "have we answered something like this
before?". A local/weak model handles graphiti's multi-step entity/edge
extraction unreliably; this agent instead does ONE narrow, robust job.

Given the NEW question and a handful of vector-similar PRIOR queries (each a
``question -> SQL`` pair with a success flag), it asks the model to judge which
prior questions ask for the SAME thing, then hands the generator those prior
SQLs as EXAMPLES plus a one-line recommendation.

It is ADVISORY ONLY. Saved SQL can be correct OR wrong, so the agent never
returns SQL as the answer and never short-circuits the pipeline — it only
suggests. The generator, which has the schema/RAG, makes the final decision to
reuse, adapt, or ignore each example. The agent reports "this looks 1:1" vs
"similar pattern", but the call is the generator's.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Tuple

from .utils import run_completion

MEMORY_JUDGE_SYSTEM = (
    "You are a QUERY-MEMORY MATCHER for a text-to-SQL system. Your ONLY job is "
    "to decide which PRIOR answered questions ask for the SAME THING as a NEW "
    "question, so their SQL can be offered to the SQL writer as an example. You "
    "do NOT write, run, or fix SQL. Two questions share intent when the answer "
    "would be computed essentially the same way (same entities, filters, "
    "grouping, output) even if worded differently or with different literal "
    "values. Sharing a few words is NOT the same intent. Be strict about "
    "'exact': use it only when the new question would be answered by the very "
    "same query shape."
)

MEMORY_JUDGE_PROMPT = """NEW QUESTION:
{question}

PRIOR ANSWERED QUESTIONS (id | prior run | question):
{candidates}

For every prior question that has the same or a closely-related intent to the NEW question, output it with a relation:
- "exact": asks for effectively the SAME result as the NEW question (only literal values may differ).
- "similar": different, but a useful example/pattern for writing the new SQL.
Omit prior questions that are unrelated. Prefer precision over recall.

Return ONLY this JSON (no prose):
{{"recommendation":"<=20 words to the SQL writer (e.g. 'Identical prior question — reuse its SQL if it fits the schema.', 'Related patterns below.', or 'No useful prior queries.')",
  "matches":[{{"id":<int>,"relation":"exact"|"similar"}}]}}
"""


def _extract_json_object(text: str) -> Dict[str, Any] | None:
    """Last top-level JSON object in *text*, tolerant of prose/```json fences."""
    if not text:
        return None
    depth = 0
    start = None
    blocks: List[str] = []
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    blocks.append(text[start:i + 1])
                    start = None
    for block in reversed(blocks):
        try:
            obj = json.loads(block)
            if isinstance(obj, dict) and ("matches" in obj or "recommendation" in obj):
                return obj
        except json.JSONDecodeError:
            continue
    return None


class MemoryAgent:  # pylint: disable=too-few-public-methods
    """Recall prior similar queries as advisory examples for the generator."""

    def __init__(self, custom_api_key: str | None = None, custom_model: str | None = None,
                 max_candidates: int = 8, max_examples: int = 3):
        self.custom_api_key = custom_api_key
        self.custom_model = custom_model
        self.max_candidates = max_candidates
        self.max_examples = max_examples

    async def recall(self, question: str, memory_tool: Any) -> str:
        """Return a generator-ready ADVISORY block of prior similar queries.

        Empty string when memory is unavailable, holds nothing similar, or the
        judge rejects every candidate. Never raises — memory must not break SQL.
        """
        if not (question or "").strip() or memory_tool is None:
            return ""
        try:
            candidates = await memory_tool.retrieve_similar_queries(
                question, limit=self.max_candidates,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("MemoryAgent retrieval failed (%s)", str(exc)[:160])
            return ""
        candidates = [
            c for c in (candidates or [])
            if isinstance(c, dict) and str(c.get("user_query") or "").strip()
            and str(c.get("sql_query") or "").strip()
        ]
        if not candidates:
            return ""

        cand_lines = []
        for i, c in enumerate(candidates):
            status = "success" if c.get("success") else "FAILED"
            cand_lines.append(f'{i} | {status} | "{str(c.get("user_query"))[:300]}"')
        messages = [
            {"role": "system", "content": MEMORY_JUDGE_SYSTEM},
            {"role": "user", "content": MEMORY_JUDGE_PROMPT.format(
                question=question.strip(), candidates="\n".join(cand_lines))},
        ]
        logging.info("MemoryAgent judging: candidates=%d", len(candidates))
        try:
            answer = await asyncio.to_thread(
                run_completion, messages, self.custom_model,
                self.custom_api_key, temperature=0,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("MemoryAgent judge failed (%s)", str(exc)[:160])
            return ""
        obj = _extract_json_object(answer) or {}
        recommendation = str(obj.get("recommendation") or "").strip()
        matches = obj.get("matches") if isinstance(obj.get("matches"), list) else []

        picked: List[Tuple[Dict[str, Any], str]] = []
        for rel_want in ("exact", "similar"):  # exact first
            for m in matches:
                if not isinstance(m, dict):
                    continue
                idx = m.get("id")
                rel = str(m.get("relation") or "").strip().lower()
                if rel != rel_want or not isinstance(idx, int):
                    continue
                if 0 <= idx < len(candidates):
                    cand = candidates[idx]
                    if all(cand is not p[0] for p in picked):
                        picked.append((cand, rel_want))
        picked = picked[:self.max_examples]
        try:
            logging.info(
                "MemoryAgent picked %d/%d example(s): %s",
                len(picked), len(candidates),
                "; ".join(f"{rel}:{str(c.get('user_query'))[:60]}" for c, rel in picked),
            )
        except Exception:  # pragma: no cover - logging must never break recall
            pass
        if not picked:
            return ""
        return self._render(recommendation, picked)

    @staticmethod
    def _render(recommendation: str, picked: List[Tuple[Dict[str, Any], str]]) -> str:
        out = [
            "PRIOR SIMILAR QUERIES (from memory — SUGGESTIONS ONLY, NOT "
            "authoritative: saved SQL may be outdated or WRONG. Treat each as an "
            "example pattern; verify every table/column/join against the SCHEMA "
            "above and reuse it only if it truly fits the question — otherwise "
            "adapt it or ignore it. The final decision is yours.)"
        ]
        if recommendation:
            out.append(f"Memory recommendation: {recommendation}")
        for cand, rel in picked:
            if cand.get("success"):
                status = "prior run: success"
            else:
                status = "prior run: FAILED — may be wrong, use with caution"
            tag = "EXACT match" if rel == "exact" else "similar"
            out.append(f'- [{tag}; {status}] Q: "{str(cand.get("user_query"))[:300]}"')
            out.append(f'  SQL: {str(cand.get("sql_query")).strip()}')
        return "\n".join(out)
