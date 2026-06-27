"""Client-echo session continuity for the legacy Text2SQL path.

The browser keeps the previous turn's trimmed JSON and resends it as
``session_context`` so a *follow-up* question refines the SAME plan instead of
rebuilding from scratch. There is NO server-side session store — the client is
the source of truth (cleared by the "New Session" button).

``session_context`` shape (built by the frontend from the prior response):

    {
      "db_id": "<graph id>",
      "prior_question": "...",
      "prior_sql": "<clean executable SQL>",
      "selected_columns": [{"table","column","role","reason"}, ...]   # = column_evidence
    }

Because the client echoes this on EVERY next turn (even an unrelated new
question), a DETERMINISTIC guard decides whether to actually use it — we do NOT
trust the model alone (a prior cross-request cache that merged unrelated
questions was a real bug). The prior plan is injected as a clearly-labelled
PROMPT block only; it never force-pins columns through retrieval/pruning.
Pure functions, no LLM, never raise.
"""

from __future__ import annotations

import re
from typing import Any

_STOPWORDS = {
    "the", "a", "an", "of", "for", "to", "in", "on", "and", "or", "by", "with",
    "is", "are", "be", "as", "at", "from", "per", "all", "any", "show", "list",
    "get", "give", "me", "what", "which", "how", "many", "much", "do", "does",
    "что", "как", "и", "в", "на", "по", "за", "с", "у", "о",
}

# Anaphoric / corrective markers that signal a follow-up even WITHOUT a shared
# content word ("exclude those", "same but quarterly"). Deliberately STRONG only
# — generic analytic words (top/sort/group/limit/add) are excluded because they
# appear in standalone questions and would cause false continuity.
_FOLLOWUP_MARKERS = {
    "instead", "also", "additionally", "besides", "exclude", "excluding",
    "same", "previous", "prior", "those", "these", "former", "latter",
    "вместо", "также", "тоже", "исключи", "исключив", "убери", "кроме",
    "предыдущ", "аналогично", "теперь", "тот", "те", "этот", "эти", "та",
}

_MAX_SELECTED = 24          # cap echoed columns
_MAX_BLOCK_CHARS = 4000     # cap injected block size
_SHORT_QUESTION_TOKENS = 4  # <= this many content tokens => treat as elliptical


def _norm(value: Any) -> str:
    return str(value or "").strip().strip('"').strip("`").lower()


def _tokens(text: Any) -> set[str]:
    return {
        tok for tok in re.split(r"[^a-z0-9а-яё]+", str(text or "").lower())
        if len(tok) >= 3 and tok not in _STOPWORDS
    }


def _selected(session_context: dict) -> list[dict]:
    cols = session_context.get("selected_columns")
    return [c for c in (cols or []) if isinstance(c, dict)][:_MAX_SELECTED]


def _is_valid_context(session_context: Any) -> bool:
    if not isinstance(session_context, dict):
        return False
    return bool(
        str(session_context.get("prior_question") or "").strip()
        or str(session_context.get("prior_sql") or "").strip()
    )


def context_applies(session_context: Any, current_question: str, graph_id: str) -> bool:
    """Deterministic guard: should the prior turn's plan be injected for THIS
    question? Requires a matching db and a relatedness signal (follow-up marker,
    shared content token with the prior question or its columns, or a short/
    elliptical question). Never raises."""
    try:
        if not _is_valid_context(session_context):
            return False
        # db must match (the client echoes db_id; reject cross-db reuse).
        db_id = _norm(session_context.get("db_id"))
        if db_id and _norm(graph_id) and db_id != _norm(graph_id):
            return False

        cq = _tokens(current_question)
        if not cq:
            return False  # nothing to relate

        # Short / elliptical follow-up ("and for 2009?", "only active ones").
        if len(cq) <= _SHORT_QUESTION_TOKENS:
            return True

        # Anaphoric / corrective marker present.
        low = f" {str(current_question or '').lower()} "
        if cq & _FOLLOWUP_MARKERS or any(f" {m} " in low for m in _FOLLOWUP_MARKERS):
            return True

        # Shared content token with the prior question.
        if cq & _tokens(session_context.get("prior_question")):
            return True

        # Shared content token with a previously selected table/column.
        col_tokens: set[str] = set()
        for c in _selected(session_context):
            col_tokens |= _tokens(c.get("column"))
            col_tokens |= _tokens(c.get("table"))
        if cq & col_tokens:
            return True

        return False
    except Exception:  # noqa: BLE001
        return False


def render_prior_turn_block(session_context: dict) -> str:
    """Render the labelled PREVIOUS-TURN block for the analysis prompt."""
    prior_q = str(session_context.get("prior_question") or "").strip()
    prior_sql = str(session_context.get("prior_sql") or "").strip()
    lines = [
        "PREVIOUS TURN (the current question is a follow-up — refine this plan, "
        "do not start from scratch):",
    ]
    if prior_q:
        lines.append(f"- Previous question: {prior_q}")
    if prior_sql:
        lines.append("- Previous SQL:")
        lines.append(prior_sql)
    cols = _selected(session_context)
    if cols:
        lines.append("- Columns used previously (table.column [role] — why):")
        for c in cols:
            table = str(c.get("table") or "").strip()
            col = str(c.get("column") or "").strip()
            if not col:
                continue
            ref = f"{table}.{col}" if table else col
            role = str(c.get("role") or "").strip()
            reason = str(c.get("reason") or "").strip()
            tail = f" — {reason}" if reason else ""
            role_tag = f" [{role}]" if role else ""
            lines.append(f"  - {ref}{role_tag}{tail}")
    lines.append(
        "Reuse the same tables/joins/columns where they still apply and change "
        "only what the current question asks. Do NOT carry over a previous "
        "filter, limit, grouping, or metric that the current question does not "
        "imply. If the current question is actually unrelated, ignore this block."
    )
    return "\n".join(lines)[:_MAX_BLOCK_CHARS]


def build_prior_turn_block(session_context: Any, current_question: str,
                           graph_id: str) -> str:
    """Guard + render. Returns the prompt block, or "" when the prior turn must
    not be used for this question. Never raises."""
    try:
        if not context_applies(session_context, current_question, graph_id):
            return ""
        return render_prior_turn_block(session_context)
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys

    CTX = {
        "db_id": "sports_events_large",
        "prior_question": "What is the fastest lap time in seconds?",
        "prior_sql": "SELECT MIN(msec_val) / 1000.0 FROM lap_times",
        "selected_columns": [
            {"table": "lap_times", "column": "msec_val", "role": "metric",
             "reason": "lap time source"},
        ],
    }
    results = []

    def check(label, cond):
        print(f"[{'PASS' if cond else 'FAIL'}] {label}")
        results.append(cond)

    # follow-up sharing a content token ("lap")
    check("(a) shared-token follow-up applies",
          context_applies(CTX, "show the slowest lap time too", "sports_events_large"))
    # short / elliptical
    check("(b) short elliptical follow-up applies",
          context_applies(CTX, "and in milliseconds?", "sports_events_large"))
    # anaphoric marker, no shared token
    check("(c) anaphoric marker applies",
          context_applies(CTX, "exclude those rows please", "sports_events_large"))
    # unrelated full question -> does NOT apply
    check("(d) unrelated full question ignored",
          not context_applies(CTX, "which sponsors funded marketing campaigns this year",
                              "sports_events_large"))
    # db mismatch -> rejected even if related
    check("(e) db mismatch rejected",
          not context_applies(CTX, "fastest lap time", "other_db"))
    # empty / invalid context
    check("(f) empty context rejected",
          not context_applies({}, "fastest lap", "sports_events_large")
          and not context_applies(None, "fastest lap", "sports_events_large"))
    # block renders prior SQL + columns + directive
    blk = build_prior_turn_block(CTX, "and in milliseconds?", "sports_events_large")
    check("(g) block has prior SQL, column, directive",
          "Previous SQL:" in blk and "lap_times.msec_val" in blk
          and "do not start from scratch" in blk.lower())
    check("(h) unrelated -> empty block",
          build_prior_turn_block(CTX, "which sponsors funded marketing campaigns this year",
                                 "sports_events_large") == "")

    print()
    if all(results):
        print(f"ALL {len(results)} ASSERTIONS PASSED")
        sys.exit(0)
    print(f"{sum(1 for r in results if not r)} ASSERTION(S) FAILED")
    sys.exit(1)
