"""Column linker — one strict job: pick the exact column for each requested value.

Weak small-context models are unreliable when the big generator prompt asks them
to simultaneously plan joins, apply formulas, AND choose the right column among
look-alikes (two similarly-named measures, or a numeric surrogate key vs a
readable reference for an entity id). This agent does ONLY column selection, in a tiny focused context (the
question + a compact candidate-column list), with a strict one-purpose system
rule — which even a weak model handles reliably.

If two or more columns match a requested value about equally and choosing wrong
would change the result, the linker marks it AMBIGUOUS rather than guessing; the
pipeline then asks the user which source to use (human clarification), instead of
silently picking one.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List

from .utils import BaseAgent, run_completion, run_tool_completion

# Schema-link PLAN tool: the linker fills the shared protocol's link section via a
# function call, so its output is valid JSON out of the box (no free-text parse
# failures) and it spends no tokens on the wrapper shape.
_LINK_ITEM = {
    "type": "object",
    "properties": {
        "asks_for": {"type": "string"},
        "column": {"type": "string"},
        "evidence": {"type": "string"},
    },
}
_LINK_TOOL = [{
    "type": "function",
    "function": {
        "name": "submit_link_plan",
        "description": "Return the schema-link plan: the exact columns/joins/grouping (with evidence) the SQL writer must use.",
        "parameters": {
            "type": "object",
            "properties": {
                "select": {"type": "array", "items": _LINK_ITEM},
                "filters": {"type": "array", "items": _LINK_ITEM},
                "joins": {"type": "array", "items": {"type": "object", "properties": {
                    "join": {"type": "string"}, "evidence": {"type": "string"}}}},
                "group_by": {"type": "array", "items": _LINK_ITEM},
                "ambiguous": {"type": "array", "items": {"type": "object", "properties": {
                    "asks_for": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"}}}},
            },
        },
    },
}]

# The agent's single, immutable purpose.
LINKER_SYSTEM = (
    "You are a COLUMN SELECTOR for SQL generation. Your ONLY job is to map each "
    "value the user's question asks to output or filter by to the single "
    "best-matching column from the provided schema. You do not write SQL, plan "
    "joins, or compute anything. Decide only from the column names and "
    "descriptions given.\n"
    "Key rule: a STATISTICAL or DERIVED value — consistency, stability, "
    "variability, volatility, or an average/min/max/sum/count/rate/ratio/growth/"
    "age OF something — is COMPUTED from a BASE MEASURE column; it is NOT a "
    "pre-named column that merely shares a word. For such a value pick the base "
    "measure column it is computed from (e.g. for an 'X consistency' pick the "
    "column that holds X), never a different entity's similarly-named column. Never "
    "match on a shared word alone — the description must fit the requested "
    "meaning. Match the column whose description is about the SAME subject as the "
    "question; prefer the main subject's own measure over a lookup/secondary "
    "table that happens to contain a like-named field.\n"
    "SOURCE CONSISTENCY: values that belong to the SAME metric or the same kind of "
    "record (e.g. a thing's count of events STARTED and FINISHED, or the SCORE and "
    "STATUS of one result row) must come from the SAME table when one table holds "
    "them all. Once you bind a metric's primary value to a table, bind its other "
    "parts (status/result/count) to THAT SAME table — do not scatter one metric "
    "across a look-alike column on a different table (e.g. do not read 'started' "
    "from a per-result table but 'finished/status' from a different session's "
    "table). Pick the table whose grain matches the metric (a per-EVENT detail row "
    "for a per-event metric; a finer sub-event row for a per-sub-unit metric).\n"
    "A NAME or label the question asks to show is a TEXT value — a string column "
    "or a JSON path whose description says it holds the entity's name — NEVER an "
    "integer key/id/code/reference column. If the name lives inside a JSON column "
    "you already use for another field, map the name to that SAME JSON column "
    "(the SQL writer extracts the name path); do not substitute a numeric key.\n"
    "When the question asks to OUTPUT an entity's IDENTIFIER (its 'id', 'code', "
    "'reference', or simply 'which <entity>'), prefer the entity's human-readable "
    "business identifier — a short TEXT reference/code/short-name (often a "
    "'reference' or 'code' field, sometimes inside the entity's identity JSON) — "
    "over an internal NUMERIC surrogate key (an integer primary/foreign key whose "
    "description says it is used to identify/join rows, e.g. 'Unique identifier "
    "... PK'), WHENEVER such a readable identifier exists among the candidates. "
    "The numeric surrogate key is for joining/filtering, not for display. Only "
    "when the entity has NO readable reference/code does its numeric key become "
    "the identifier to output.\n"
    "DURATION vs POINT-IN-TIME — both are often called 'time', so read the "
    "description, not just the name. A DURATION/ELAPSED measure is HOW LONG "
    "something took (an elapsed or total time, a session/segment length) — "
    "usually a NUMBER of milliseconds/seconds/minutes, described as a "
    "'time'/'duration'/'elapsed'/'length' MEASURE you can average or sum. A "
    "POINT-IN-TIME is WHEN something happened (a timestamp, clock time, "
    "start/finish/'final' time, scheduled time, or a date). When the question "
    "asks about a DURATION metric ('average time', 'fastest time', 'how "
    "long', 'total time'), pick the column that holds that elapsed DURATION — "
    "NOT a timestamp/clock/'final time'/date column, even if its name also "
    "contains 'time'. When the question asks WHEN / 'at what time' / 'on what "
    "date', pick the timestamp/date instead. Match the kind of time the question "
    "means to the kind the column stores.\n"
    "COLUMNS ONLY — every 'column' value is a SINGLE table.column or JSON path, "
    "VERBATIM from the candidates. NEVER put a function call, arithmetic, "
    "AVG()/FLOOR()/SUM(), or any formula in 'column' — for a computed/derived "
    "value bind the BASE column it is computed from (the writer builds the "
    "formula). A 'column' containing '(' is WRONG."
)

LINKER_PROMPT = """TARGET SQL DIALECT: {dialect} (the SQL writer targets this dialect; pick identifiers/JSON paths exactly as given).
QUESTION:
{question}

RELEVANT METRIC DEFINITIONS (when the question names one of these metrics/concepts, READ its definition and bind it to the BASE column it is computed FROM — a metric defined "per <unit>" / "for each <unit>" comes from that per-unit source table, NOT a higher-grain or different-entity column that only shares a word):
{concepts}

CANDIDATE COLUMNS (table.column (type) — description; JSON columns list their dotted nested paths):
{columns}

VERIFIED JOINS available (use only these to connect tables):
{joins}

Produce the schema-link PLAN the SQL writer will follow. For every value the question asks to OUTPUT, FILTER BY, GROUP BY, or ORDER BY, pick the ONE column (or JSON path) whose description best matches, and give a one-clause evidence grounded in that description.
- Pick decisively when one column clearly matches.
- Prefer a column on the MAIN subject table over an alias / lookup / secondary table when both could match (e.g. the entity's own name over an "alias" column), unless the question explicitly asks for the alias/alternate.
- AS-OF / COMPUTED values: when the question asks for a value "at the time of <event>" — or any age / standing / value tied to a specific event or date — do NOT bind it to a pre-computed or snapshot column (a stored value holding a CURRENT / as-of-load number, which is WRONG for a historical "at the time" question, especially a column with NO description you cannot verify). Instead plan it from the BASE attribute(s) it is computed from (a base date, a per-event measure) PLUS the EVENT's own date column, and add the join needed to reach that event date. Surface those base + event-date columns and the join, not the shortcut snapshot.
- Mark a value AMBIGUOUS only when two or more columns match about equally AND the wrong pick changes the result. An alias vs the primary column is NOT ambiguous — pick the primary. Don't mark trivial cases.
- filters: ONLY an EXPLICIT restricting condition the question states — a specific value, threshold, range, status, or date the result must satisfy (e.g. "at least 5", "in 2009", "finished"). A phrase that merely names WHICH dataset / entity-type / metric the question is about, and is ALREADY implied by the chosen tables (a category/type label that the rows you selected already represent), is CONTEXT, NOT a filter: do NOT add a filter for it, and NEVER join an extra table just to restrict on a type/label already implied by the tables you chose. Give each real filter its column AND evidence; keep filters SEPARATE from outputs.
- joins: list only the verified joins actually needed to reach the chosen columns, and give EACH join its own evidence (which relationship/FK it follows and why it is needed to connect the chosen columns). Keep joins SEPARATE from filters.
- group_by: the column(s) the result must be GROUPED BY. Include (a) the OUTPUT grain — the per-entity/per-period the question reports one row for (e.g. "per <entity>", "for each <group>", "by <period>"), and (b) any grouping a per-group metric needs at an inner step (e.g. a metric defined "per <event>" is computed grouped by that event's key first). Give each its column + evidence (why the result is grouped by it). These come from possibly DIFFERENT tables. Only fill group_by when the question asks for per-group results or a grouped/aggregated metric; leave empty for a single overall value.
- EVERY kept item — every output, every filter, every join, every group_by key — must carry a one-clause evidence grounded in the column description / relationship. If you cannot justify an item, drop it.
- Use only columns/joins from the lists above; use exact "table.column" / dotted JSON path.

Return the plan by CALLING the submit_link_plan tool. Each select/filter/group_by item is {{asks_for, column, evidence}}; each join is {{join, evidence}}; each ambiguous is {{asks_for, options[], reason}}. Leave a section empty if it does not apply.
"""


def _extract_json_object(text: str) -> Dict[str, Any] | None:
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
            obj = json.loads(block, strict=False)
            if isinstance(obj, dict) and any(
                k in obj for k in ("select", "filters", "joins", "group_by", "ambiguous")
            ):
                return obj
        except json.JSONDecodeError:
            continue
    return None


class LinkerAgent(BaseAgent):
    # pylint: disable=too-few-public-methods
    """Pick exact columns for the question's requested values (or flag ambiguity)."""

    async def link(
        self,
        question: str,
        columns_block: str,
        joins_block: str = "",
        database_type: str | None = None,
        concepts_block: str = "",
    ) -> Dict[str, Any]:
        """Return the link PLAN: ``{"select":[...], "filters":[...], "joins":[...],
        "ambiguous":[...]}`` (each select/filter item has column + evidence).

        Fail-safe: on any error returns an empty plan (pipeline proceeds without
        link hints), never raises.
        """
        empty = {"select": [], "filters": [], "joins": [],
                 "group_by": [], "ambiguous": []}
        if not (columns_block or "").strip():
            return empty
        # Fresh focused conversation: only the strict system rule + this task.
        self.messages = [
            {"role": "system", "content": LINKER_SYSTEM},
            {"role": "user", "content": LINKER_PROMPT.format(
                dialect=(database_type or "SQL").upper(),
                question=question or "",
                concepts=(concepts_block.strip() or "(none)"),
                columns=columns_block.strip(),
                joins=(joins_block.strip() or "(none)"))},
        ]
        logging.info("LinkerAgent linking: columns_chars=%d joins_chars=%d",
                     len(columns_block or ""), len(joins_block or ""))
        try:
            answer = await asyncio.to_thread(
                run_tool_completion, self.messages, _LINK_TOOL,
                self.custom_model, self.custom_api_key, "submit_link_plan",
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            # Endpoint without tool support -> plain completion (parsed below).
            logging.warning("LinkerAgent tool path failed (%s); plain completion", str(exc)[:120])
            try:
                answer = await asyncio.to_thread(
                    run_completion, self.messages, self.custom_model,
                    self.custom_api_key, temperature=0,
                )
            except Exception as exc2:  # pylint: disable=broad-exception-caught
                logging.warning("LinkerAgent failed (%s)", str(exc2)[:160])
                return empty
        obj = _extract_json_object(answer) or {}

        def _lst(key):
            v = obj.get(key)
            return v if isinstance(v, list) else []

        # A 'column' is a single table.column / JSON path. If the model put a
        # FORMULA there (function call / arithmetic, marked by a '('), it is not a
        # bindable column — drop the item so a malformed plan can't break the
        # writer (deterministic guard; the writer builds formulas, not the linker).
        def _col_ok(b) -> bool:
            return (isinstance(b, dict) and bool(b.get("column"))
                    and "(" not in str(b.get("column") or ""))
        _dropped_formula = [b for b in (_lst("select") + _lst("filters") + _lst("group_by"))
                            if isinstance(b, dict) and b.get("column") and "(" in str(b.get("column"))]
        if _dropped_formula:
            logging.info("LinkerAgent dropped %d formula-in-column item(s): %s",
                         len(_dropped_formula),
                         "; ".join(str(b.get("column"))[:60] for b in _dropped_formula)[:200])
        select = [b for b in _lst("select") if _col_ok(b)]
        filters = [b for b in _lst("filters") if _col_ok(b)]
        # joins: normalise to {"join","evidence"} dicts; accept a legacy bare
        # "t.c = t2.c2" string (evidence "") so older outputs still work.
        joins = []
        for j in _lst("joins"):
            if isinstance(j, dict) and "=" in str(j.get("join") or ""):
                joins.append({"join": str(j["join"]).strip(),
                              "evidence": str(j.get("evidence") or "").strip()})
            elif isinstance(j, str) and "=" in j:
                joins.append({"join": j.strip(), "evidence": ""})
        group_by = [b for b in _lst("group_by") if _col_ok(b)]
        ambiguous = [a for a in _lst("ambiguous")
                     if isinstance(a, dict) and a.get("asks_for") and a.get("options")]
        logging.info("LinkerAgent plan: select=%d filters=%d joins=%d group_by=%d ambiguous=%d",
                     len(select), len(filters), len(joins), len(group_by), len(ambiguous))
        # Decision detail (what the linker actually CHOSE) — the high-signal
        # diagnostic for wrong-source / decoy-column failures.
        try:
            sel = "; ".join(f"{b.get('asks_for')}->{b.get('column')}" for b in select)
            flt = "; ".join(f"{b.get('asks_for')}->{b.get('column')}" for b in filters)
            amb = "; ".join(f"{a.get('asks_for')}?[{','.join(str(o) for o in (a.get('options') or []))}]"
                            for a in ambiguous)
            grp = "; ".join(f"{b.get('asks_for')}->{b.get('column')}" for b in group_by)
            logging.info("LinkerAgent decisions: SELECT[%s] FILTER[%s] JOIN[%s] GROUP[%s] AMBIG[%s]",
                         sel, flt, " | ".join(j.get("join", "") for j in joins), grp, amb)
        except Exception:  # pragma: no cover - logging must never break linking
            pass
        return {"select": select, "filters": filters, "joins": joins,
                "group_by": group_by, "ambiguous": ambiguous}


def render_plan_block(plan: Dict[str, Any]) -> str:
    """Render the linker PLAN as a hard directive the generator must follow —
    exact columns for outputs/filters + the joins to use. The generator does not
    re-pick these; it only writes the SQL around them.
    """
    select = plan.get("select") or []
    filters = plan.get("filters") or []
    joins = plan.get("joins") or []
    group_by = plan.get("group_by") or []
    if not (select or filters or joins or group_by):
        return ""
    lines = ["SCHEMA-LINK PLAN (use EXACTLY these columns/paths and joins; do not "
             "substitute a similar column or invent a join). EXCEPTION: if a value "
             "here is also defined under RESOLVED METRICS above, COMPUTE that "
             "formula instead of selecting the single column listed here. Each item "
             "carries its evidence (why it was chosen):"]

    def _ev(b):
        e = (b.get("evidence") or "").strip() if isinstance(b, dict) else ""
        return f"   — {e}" if e else ""

    if select:
        lines.append("OUTPUTS (select exactly these):")
        for b in select:
            lines.append(f"- \"{b.get('asks_for')}\" -> {b.get('column')}{_ev(b)}")
    if filters:
        lines.append("FILTERS (apply these as WHERE conditions on these exact columns):")
        for b in filters:
            lines.append(f"- \"{b.get('asks_for')}\" -> {b.get('column')}{_ev(b)}")
    if joins:
        lines.append("JOINS (connect tables ONLY via these):")
        for j in joins:
            js = j.get("join") if isinstance(j, dict) else j
            lines.append(f"- {js}{_ev(j)}")
    if group_by:
        lines.append("GROUP BY (the result grain — GROUP BY exactly these keys; "
                     "an inner per-group metric groups by its key first):")
        for b in group_by:
            lines.append(f"- \"{b.get('asks_for')}\" -> {b.get('column')}{_ev(b)}")
    return "\n".join(lines)


def render_clarification(ambiguous: List[Dict[str, Any]]) -> str:
    """A concise human-clarification question for genuinely ambiguous sources."""
    if not ambiguous:
        return ""
    parts = ["I need you to confirm the source for:"]
    for a in ambiguous:
        opts = ", ".join(str(o) for o in (a.get("options") or []))
        parts.append(f"- \"{a.get('asks_for')}\": {opts}"
                     + (f" ({a.get('reason')})" if a.get("reason") else ""))
    return "\n".join(parts)
