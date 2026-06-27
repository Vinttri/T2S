"""Metric/concept resolver — bind named business-KB concepts to exact SQL.

A weak model reliably *applies* a formula it is handed, but unreliably *derives*
one from an abstract definition (e.g. Sprint Performance Index came out as
``9 - …`` on one run and ``21 - …`` on the next). This focused agent runs in a
TINY context — only the question, the matched KB concept definitions, and a
compact list of the candidate columns — and returns, per concept, the EXACT
column-bound SQL expression / filter the generator must copy verbatim. Splitting
this narrow task out of the big generator prompt is what makes it stable on weak,
small-context models.

Detection is deterministic (concept-name match against the question), so the LLM
call only happens when the question actually invokes a defined concept; otherwise
the pipeline is unchanged and pays nothing.
"""

import asyncio
import json
import logging
import os
import re
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Tuple

from .utils import BaseAgent, run_completion, run_tool_completion

# Resolved-metrics tool: the resolver writes its formulas into the shared protocol
# via a function call (valid JSON out of the box; no free-text array parsing).
_RESOLVE_TOOL = [{
    "type": "function",
    "function": {
        "name": "submit_resolved_metrics",
        "description": "Return each named metric/concept bound to an exact SQL expression and/or filter.",
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
                            "grain": {"type": "string"},
                            "filter": {"type": "string"},
                            "note": {"type": "string"},
                        },
                        "required": ["name"],
                    },
                },
            },
            "required": ["metrics"],
        },
    },
}]

_STOP = {"the", "and", "for", "system", "with", "per", "from", "into", "data",
         "explained", "structure"}


def detect_concepts(query: str, kb_text: str, max_concepts: int = 5) -> List[Tuple[str, str]]:
    """Deterministically pick KB concepts whose NAME is invoked by the question.

    Parses ``## Name`` headers + their definition body from the KB blob and keeps
    a concept when (almost) all of its distinctive name tokens appear in the
    question, or its full multiword name is a substring. Returns ``[(name, defn)]``
    best-first, capped. No LLM — cheap and runs every query.
    """
    if not query or not kb_text:
        return []
    q = f" {query.lower()} "
    blocks: Dict[str, List[str]] = {}
    current = None
    for line in kb_text.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = match.group(1).strip()
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    scored: List[Tuple[float, str, str]] = []
    for name, body in blocks.items():
        nm = name.lower()
        toks = [t for t in re.findall(r"[a-z]+", nm) if len(t) > 2 and t not in _STOP]
        if not toks:
            continue
        phrase = " ".join(toks)
        ql = query.lower()
        qwords = set(re.findall(r"[a-z]+", ql))
        hits = sum(1 for t in set(toks) if t in q)
        hit_frac = hits / len(set(toks))
        # Stem/prefix match so a question word resolves a related concept token
        # even with a different suffix: "perform" <-> "performance", "consist"
        # <-> "consistency", "reliab" <-> "reliability". Catches concepts the
        # question names only partially.
        def _stem_hit(tok: str) -> bool:
            if len(tok) < 5:
                return False
            pre = tok[:5]
            return any(w.startswith(pre) or tok.startswith(w[:5])
                       for w in qwords if len(w) >= 5)
        stem_hits = any(_stem_hit(t) for t in set(toks))
        # Generous recall: full phrase, a single distinctive token, >=50% of name
        # tokens, or any distinctive token stem-matched. The resolver itself omits
        # any matched concept the question does not actually compute, and the
        # grounding/nested-aggregate guards drop unusable formulas, so erring
        # toward recall here is safe.
        strong = (phrase in ql) or (len(toks) == 1 and toks[0] in q) \
            or hit_frac >= 0.5 or stem_hits
        if strong:
            defn = "\n".join(body).strip()
            scored.append((hit_frac + (1.0 if phrase in query.lower() else 0.0), name, defn))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(n, d) for _, n, d in scored[:max_concepts] if d]


def _name_variants(name: str) -> List[str]:
    """Lowercased forms of a concept NAME to search for inside another concept's
    body: the full name, the name with any parenthetical removed, and an
    all-caps abbreviation in parentheses (e.g. "(PPR)"). General — relies only
    on the KB's own naming, no domain assumptions."""
    base = (name or "").strip().lower()
    variants = {base}
    without_paren = re.sub(r"\s*\([^)]*\)", "", base).strip()
    if without_paren:
        variants.add(without_paren)
    for abbr in re.findall(r"\(([A-Z]{2,})\)", name or ""):
        variants.add(abbr.lower())
    return [v for v in variants if len(v) >= 3]


def _norm_name(name: str) -> str:
    return re.sub(r"\s*\([^)]*\)", "", (name or "").strip().lower()).strip()


def expand_concept_references(
    selected: List[Tuple[str, str]], kb_text: str,
    max_support: int = 3, depth: int = 2,
) -> List[Tuple[str, str]]:
    """Concepts transitively referenced BY NAME inside the selected concepts.

    A composite metric whose formula names other metrics (e.g. a score defined
    as ``points x reliability rate``) is only usable if those referenced
    concepts are delivered too. This walks the reference graph from the selected
    concepts and returns the additional ``(name, body)`` pairs, so a downstream
    agent can decompose the metric to its base inputs — and therefore choose the
    detail source that holds those inputs instead of binding a pre-computed
    look-alike column. Bounded by ``depth`` and ``max_support`` to stay compact.
    No LLM; same ``## Name`` parsing as :func:`detect_concepts`. Fully general.
    """
    if not selected or not kb_text:
        return []
    blocks: Dict[str, List[str]] = {}
    current = None
    for line in kb_text.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if match:
            current = match.group(1).strip()
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    all_names = list(blocks.keys())
    if not all_names:
        return []
    variants_by_name = {n: _name_variants(n) for n in all_names}

    def _hit(name: str, text: str) -> bool:
        for variant in variants_by_name[name]:
            if len(variant) <= 4:
                if re.search(r"(?<![a-z0-9])" + re.escape(variant) + r"(?![a-z0-9])", text):
                    return True
            elif variant in text:
                return True
        return False

    seen = set()
    for n, _ in selected:
        seen.add(_norm_name(n))
    support: List[Tuple[str, str]] = []
    # Seed the frontier with each selected concept's body (its definition text
    # is where references to sibling concepts appear).
    frontier: List[str] = []
    for n, d in selected:
        body = "\n".join(blocks.get(n, []))
        frontier.append(((d or "") + " " + body).lower())
    level = 0
    while frontier and level < depth and len(support) < max_support:
        nxt: List[str] = []
        for text in frontier:
            for name in all_names:
                if _norm_name(name) in seen:
                    continue
                if _hit(name, text):
                    seen.add(_norm_name(name))
                    body = "\n".join(blocks.get(name, [])).strip()
                    support.append((name, body))
                    nxt.append((name + " " + body).lower())
                    if len(support) >= max_support:
                        break
            if len(support) >= max_support:
                break
        frontier = nxt
        level += 1
    return support


def _extract_json_array(text: str):
    """Last top-level JSON array in *text*, tolerant of ```json fences/prose."""
    if not text:
        return None
    depth = 0
    start = None
    blocks = []
    for i, ch in enumerate(text):
        if ch == "[":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    blocks.append(text[start:i + 1])
                    start = None
    for block in reversed(blocks):
        try:
            parsed = json.loads(block)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _resolver_n() -> int:
    """How many resolver samples to draw and merge (1 = off). The weak model
    intermittently OMITS a concept it can resolve or binds it differently between
    identical calls; drawing N and merging recovers the dropped one and majority-
    votes the binding."""
    try:
        return max(1, min(5, int(os.getenv("RESOLVER_SELF_CONSISTENCY", "2") or 2)))
    except (ValueError, TypeError):
        return 2


def _parse_metrics(answer: str) -> List[Dict[str, Any]]:
    """Parse one resolver answer (tool object {"metrics":[...]} or bare array) into
    a filtered list of metric dicts. Drops items with no name and no expr/filter."""
    items = None
    try:
        parsed = json.loads(answer, strict=False)
        if isinstance(parsed, dict):
            items = parsed.get("metrics")
        elif isinstance(parsed, list):
            items = parsed
    except (json.JSONDecodeError, TypeError):
        items = None
    if not isinstance(items, list):
        items = _extract_json_array(answer) or []
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        if not (str(it.get("sql_expression") or "").strip()
                or str(it.get("filter") or "").strip()):
            continue
        out.append({
            "name": str(it.get("name")),
            "sql_expression": str(it.get("sql_expression") or "").strip(),
            "grain": str(it.get("grain") or "").strip(),
            "filter": str(it.get("filter") or "").strip(),
            "note": str(it.get("note") or "").strip(),
        })
    return out


def _merge_metric_samples(samples: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Union metrics across self-consistency samples to beat run-to-run drops.

    Include any metric that at least one sample resolved (recovers a concept the
    model intermittently omits), and majority-vote its exact expression/filter
    across the samples that did resolve it (so a one-off mis-binding is outvoted).
    A wrong one-off binding that slips through is still caught downstream by the
    deterministic grounding/validation gates."""
    by_name: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for sample in samples:
        for m in sample:
            by_name.setdefault(m["name"].strip().lower(), []).append(m)
    out: List[Dict[str, Any]] = []
    for variants in by_name.values():
        def _majority(field: str) -> str:
            vals = [v[field].strip() for v in variants if str(v.get(field) or "").strip()]
            return Counter(vals).most_common(1)[0][0] if vals else ""
        expr = _majority("sql_expression")
        filt = _majority("filter")
        if not (expr or filt):
            continue
        rep = next((v for v in variants
                    if expr and v.get("sql_expression", "").strip() == expr), variants[0])
        out.append({
            "name": rep["name"],
            "sql_expression": expr,
            "grain": str(rep.get("grain") or "").strip(),
            "filter": filt,
            "note": str(rep.get("note") or "").strip(),
        })
    return out


RESOLVER_PROMPT = """You bind named business metrics/concepts to EXACT {DIALECT} SQL so a SQL writer can copy them verbatim.

QUESTION:
{QUESTION}

MATCHED BUSINESS-KNOWLEDGE CONCEPTS (authoritative definitions/formulas):
{CONCEPTS}

GENERAL GUIDANCE (behavioral rules — apply the ones that affect HOW a formula or
its grain is chosen: where a value is computed per-row vs cumulatively/over a
window, output-plausibility of a binding, and the natural unit/precision of a
quantity):
{USER_RULES}

AVAILABLE COLUMNS. Each line is either "table.column — type — description", OR a ready-to-copy JSON path already written out IN FULL and labelled "(leaf: a.b.c)". Use these EXACT names/paths — for a JSON field, find the matching "(leaf: ...)" line and copy its expression verbatim:
{SCHEMA}

Resolve the MATCHED CONCEPTS listed above that this question uses (to output, rank by, compute, or filter by) — INCLUDING a composite concept AND the sub-concepts it is built from. Output one item PER CONCEPT, with:
- "name": the concept's OWN name, copied VERBATIM from MATCHED CONCEPTS above. Do NOT invent a question-paraphrase metric (e.g. "Average <X> for top <N>", "Total <Y>"): resolve the LISTED concept itself. When the question applies an aggregate (average/total/count/max) or a row filter (top N, finished only) to a concept, that aggregate/filter is NOT part of the concept — leave it OUT of sql_expression (write the concept's plain per-row formula) and let the SQL writer add the AVG/COUNT/GROUP BY/WHERE around it. A concept named "... Index/Rate/Score/Value" is the per-row formula, never an AVG of itself.
- "sql_expression": the concept's formula written as an exact SQL expression over the available columns (keep every constant and operator from the definition; bind each term to the column/JSON-path whose description matches it; cast as needed). Empty string if the concept is only a filter, not a measure.
  Understand the TASK before writing it (you already know how to write any SQL the task needs):
  - SOURCE + GRAIN of each value: bind each term to the column whose DESCRIPTION and row-grain match what is asked. A column described as cumulative / running / total / snapshot already holds an aggregated value — use it as-is (do NOT re-sum it) ONLY when the question wants exactly that stored value at that column's own grain. But when the question wants a value computed PER event or AS OF a specific event/time (it "changes over time", "after each", "at the time of"), compute it from the BASE detail rows — the raw attributes plus that event's own date — NOT from a stored snapshot/total (which holds a current or whole-period value and is wrong for an as-of/per-event question). If a concept HAS a formula, emit that formula bound to its base columns even when a same-named stored column exists; a stored column is a fallback only when no base columns exist. Take any age / standing / "as of" term as of THAT event's own date, not today.
  - UNITS: read each base column's stored unit from its DESCRIPTION (it may be a sub-unit, e.g. "in milliseconds", "in cents"). Express the metric in the unit the QUESTION asks for; if the question names no unit, use the standard base unit for that quantity (e.g. seconds for a time, not milliseconds) — apply the conversion factor inside the expression. Do NOT convert when the column is already in the requested/base unit.
  - GRAIN (which EVENT the formula spans): identify the SMALLEST event or period the definition says the formula is computed within — look for time/event phrases such as "during an event", "over the course of an event", "per sub-unit", "within a period". That phrase is the formula's grain. Crucially the grain is the EVENT, NOT the entity the metric is ABOUT: a metric describing "an entity's sub-unit values during an event" has grain = one event (not the entity), even though the wording also names the entity. Write "sql_expression" as the plain formula for ONE such unit (e.g. a STDDEV over the sub-rows of a single event) and do NOT pre-collapse it across all of an entity's rows, nor pre-aggregate it up to the question's reporting grain — the SQL writer nests the levels itself. Record this grain (quote the phrase) in "grain".
- "grain": the smallest event/period the formula is computed within, quoted from the definition (e.g. "one event", "per sub-unit") — the EVENT, not the entity the metric is about. Empty only if the definition names no such event/period (an ordinary row-level value).
- "filter": the exact SQL boolean condition the concept implies (e.g. an age/threshold/position filter), or empty string if none.
- "note": <=12 words on the binding.

Rules: copy constants/operators EXACTLY from the definition (do not change 9 to 21). ABSENCE-DEFINED CONDITION: when a definition states a condition as the ABSENCE of a marker — wording such as "not marked", "unmarked", "not flagged", "no special mark/code", or it names the normal/expected state as null/empty/blank — translate that condition to `<column> IS NULL` (or `= ''`/the default), NEVER `IS NOT NULL`. The PRESENCE of the mark is the exception; the stated condition is its absence. (A double negative like "counts as X when the mark is not specially set (null)" means `column IS NULL`, so the count of X is `SUM(CASE WHEN column IS NULL THEN 1 ELSE 0 END)`.) SUM/COMBINATION FIDELITY: if the definition combines two or more quantities (e.g. A + B, or A weighted by B), your sql_expression MUST contain EVERY one of them joined by the SAME operators — never collapse a multi-term formula down to a single term (dropping a summand silently changes the metric). Bind each term to its own column/path. For a standard statistical measure (standard deviation, variance, average, median, correlation) use the dialect's BUILT-IN aggregate function over the base column — STDDEV(x), VARIANCE(x), AVG(x) — and NEVER hand-expand it into a manual nested-aggregate formula like SQRT(SUM(POWER(x - SUM(x)/COUNT(x), 2))/COUNT(x)): nesting an aggregate inside another aggregate is invalid SQL. Every column you reference MUST appear VERBATIM in AVAILABLE COLUMNS above — do not invent or rename a column. If a value lives in a JSON field, COPY the exact path shown on its "(leaf: ...)" line VERBATIM (it is already written out in full, e.g. col->'a'->'b'->>'c') — do NOT rebuild it from a nested description, never substitute a flat column name, and NEVER apply ->> to an intermediate object key: use -> for every key except the final leaf, which uses ->>. If no available column matches a term of the formula, set "sql_expression" to "" (empty) rather than guessing a column — a wrong column is worse than none. If a concept is mentioned but not computed/filtered by this question, omit it.

Return your result by CALLING the submit_resolved_metrics tool: a "metrics" array of {{name, sql_expression, grain, filter, note}} (sql_expression or filter may be empty per the rules above; grain empty unless the formula has a special grain).
"""


class MetricResolverAgent(BaseAgent):
    # pylint: disable=too-few-public-methods
    """Resolve matched KB concepts to exact column-bound SQL expressions/filters."""

    async def resolve(
        self,
        query: str,
        concepts: List[Tuple[str, str]],
        schema_context: str,
        database_type: str | None = None,
        user_rules: str = "",
    ) -> List[Dict[str, Any]]:
        if not concepts:
            return []
        blocks = "\n\n".join(f"### {name}\n{defn}" for name, defn in concepts)
        prompt = RESOLVER_PROMPT.format(
            DIALECT=(database_type or "SQL").upper(),
            QUESTION=query or "",
            CONCEPTS=blocks,
            USER_RULES=(user_rules or "(none)").strip(),
            SCHEMA=(schema_context or "(none)").strip(),
        )
        self.messages.append({"role": "user", "content": prompt})
        logging.info(
            "MetricResolver: concepts=%s schema_ctx_chars=%d",
            [n for n, _ in concepts], len(schema_context or ""),
        )
        def _one_sample() -> str:
            try:
                return run_tool_completion(
                    self.messages, _RESOLVE_TOOL, self.custom_model,
                    self.custom_api_key, "submit_resolved_metrics",
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning("MetricResolver tool path failed (%s); plain completion",
                                str(exc)[:120])
                return run_completion(
                    self.messages, self.custom_model, self.custom_api_key, temperature=0,
                )

        # Draw N independent samples (run_*_completion does NOT mutate self.messages,
        # so each call sees the same clean prompt) and merge — beats the model's
        # run-to-run dropping/rebinding of concepts under a real, noisy schema.
        n = _resolver_n()
        samples: List[List[Dict[str, Any]]] = []
        for _i in range(n):
            try:
                answer = await asyncio.to_thread(_one_sample)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning("MetricResolver sample %d failed (%s)", _i, str(exc)[:160])
                continue
            parsed = _parse_metrics(answer)
            if parsed:
                samples.append(parsed)
        if not samples:
            return []
        out = _merge_metric_samples(samples)
        logging.info("MetricResolver resolved %d concept(s) from %d/%d samples",
                     len(out), len(samples), n)
        for r in out:
            grain = f"  [grain: {r['grain']}]" if r.get("grain") else ""
            logging.info("MetricResolver expr: %s = %s%s%s", r["name"],
                         (r["sql_expression"] or "(filter-only)")[:240], grain,
                         (f"  [filter: {r['filter'][:120]}]" if r["filter"] else ""))
        return out


def render_resolved_block(resolved: List[Dict[str, Any]]) -> str:
    """Render resolved concepts as a compact, copy-me block for the generator."""
    if not resolved:
        return ""
    lines = ["COMPUTED METRICS — the question asks for these KB-defined metrics. Compute "
             "each value with the column-bound formula below (it encodes the KB definition, "
             "already bound to real columns). Use the formula VERBATIM as that value — keep all "
             "of its operators, constants and function variants; do not simplify it or replace "
             "it with a single raw column. Adjust only the table aliases to match your FROM/JOIN. "
             "Each formula is written at its OWN grain (shown as [grain: ...] when it is not a "
             "plain row value). If the question reports a metric at a COARSER grain than its "
             "formula's grain, first compute the formula at its stated grain in a subquery/CTE, "
             "then aggregate it up to the grain the question asks for — chaining as many levels "
             "as the grains require; never flatten several grain levels into one aggregate:"]
    for r in resolved:
        expr = r.get("sql_expression")
        if expr:
            grain = f"  [grain: {r['grain']}]" if r.get("grain") else ""
            lines.append(f"- {r['name']}: {expr}{grain}")
        if r.get("filter"):
            lines.append(f"- {r['name']} filter: {r['filter']}")
    return "\n".join(lines)
