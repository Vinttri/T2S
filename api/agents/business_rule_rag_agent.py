"""Business-rule RAG selector for the Text2SQL pipeline (deterministic, no deps).

Instead of injecting the full ~13K-char user-rules blob into every
SQL-generation prompt, this agent SELECTS the subset of business rules relevant
to the current query and writes the selection onto the shared blackboard via
``api.core.blackboard.set_selected_rules``. The downstream generator renders
them with ``blackboard.selected_rules_as_text``.

Design constraints (honoured here):
  * DETERMINISTIC and dependency-free: pure-Python stdlib only (``re``). No
    embeddings, no LLM call, no new pip deps. Lexical/keyword scoring only.
    Upgradable to embeddings later behind the same ``select()`` API.
  * The preamble and the "Invariants (apply always):" paragraph are ALWAYS
    included as a single synthetic ``invariants`` chunk and are NOT counted
    against ``max_chars`` (they are mandatory craft).
  * A small ALWAYS-ON CORE of numbered rules (schema-binding, role checks,
    fan-out, literal handling) is included on every query, chosen by TAG, not
    by hardcoded rule number.
  * SITUATIONAL rules are scored by tag-overlap with the query's detected
    intents plus raw keyword overlap with the query+schema text, then packed
    into the remaining ``max_chars`` budget, recall-biased.
  * NO hardcoded dm_mis (or any database) table/column names. Robust to a blob
    that lacks the exact "Invariants" header (degrades gracefully).

The blob format this parses (one markdown string):
    <preamble lines>
    Invariants (apply always):
    <one invariants paragraph>
    1. <Title sentence>. <body...>
    2. <Title sentence>. <body...>
    ...
Numbered rules are split on lines matching ``^\\s*\\d+\\.``.
"""
from __future__ import annotations

import re
from typing import Iterable

from api.core.blackboard import col_name, set_selected_rules, table_name  # noqa: F401

# --- tag cue families (lowercase, RU + EN) ----------------------------------
# Each entry maps a tag -> list of substring cues. Cues are matched as plain
# lowercase substrings against rule text (for tagging) and against the query /
# schema text (for intent detection). Stems (e.g. "отчётн", "действ") are
# deliberately short so they catch inflected RU forms.
TAG_CUES: dict[str, list[str]] = {
    "dates": [
        "date", "as of", "as-of", "report", "snapshot", "balance date",
        "reporting", "period", "today", "current date",
        "дат", "текущ", "отчётн", "отчетн", "период", "снимок", "на сегодня",
    ],
    "validity": [
        "active", "open", "closed", "close", "current", "latest", "valid",
        "validity", "effective", "expire",
        "действ", "закры", "актуальн", "валид", "открыт", "последн", "силе",
    ],
    "balance_measure": [
        "balance", "rest", "amount", "turnover", "rate", "currency", "sum",
        "measure", "metric", "value", "money",
        "остат", "оборот", "ставк", "сумм", "валют", "сальдо", "курс",
    ],
    "aggregation_fanout": [
        "count", "sum", "avg", "average", "distinct", "join", "grain",
        "fan-out", "fanout", "duplicate", "aggregat", "group by",
        "агрегац", "джойн", "соедин", "грануляц", "дубл", "распре",
    ],
    "literals_codes": [
        "literal", "code", "iso", "lower", "case-insensitive", "value-kind",
        "класс", "код", "литерал", "регистр",
    ],
    "roles": [
        "role", "owner", "counterparty", "assignment", "link table", "leg",
        "владел", "контрагент", "роль", "инн", "нога", "ссудн", "расчётн",
    ],
    "domain": [
        "credit", "repo", "broker", "deposit", "settlement", "collateral",
        "domain", "product", "loan", "agreement", "deal", "contract",
        "кредит", "репо", "брокер", "рко", "депозит", "сделк", "договор",
    ],
    "shares_dupes": [
        "share", "percentage", "percent", "duplicate", "proportion",
        "доля", "процент", "дубл", "удельн",
    ],
    "memory_clarify": [
        "memory", "clarify", "clarification", "ambiguous", "ask once",
        "guess", "память", "уточн", "неоднознач", "спрос",
    ],
}

# Tag families whose rules are universal SQL craft and therefore always-on.
# These are needed on almost every query: schema/literal binding, role
# verification, and join fan-out control. Chosen by TAG, not by rule number.
CORE_TAGS: tuple[str, ...] = ("roles", "literals_codes", "aggregation_fanout")

# How many always-on core rules to keep (small; recall-biased by tag, capped).
CORE_MAX_RULES = 6

# Token splitter for lexical overlap: unicode word chars (keeps RU letters).
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
# A line that starts a numbered rule, e.g. "  12. Pin the snapshot ...".
_NUM_RULE_RE = re.compile(r"^\s*(\d+)\.\s")
# The Invariants header (tolerant: any case, optional trailing colon/parenthetical).
_INVARIANTS_RE = re.compile(r"^\s*invariants\b.*:?\s*$", re.IGNORECASE)
# Short tokens that carry no lexical signal for overlap scoring.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "by",
        "is", "are", "be", "as", "at", "it", "its", "with", "that", "this",
        "from", "not", "no", "any", "every", "each", "all", "use", "user",
        "и", "в", "на", "по", "не", "за", "из", "для", "то", "что", "как",
        "или", "the", "a",
    }
)


def _tokenize(text: str) -> set[str]:
    """Lowercase word tokens (>=3 chars), stopwords removed."""
    out: set[str] = set()
    for m in _TOKEN_RE.findall((text or "").lower()):
        if len(m) >= 3 and m not in _STOPWORDS:
            out.add(m)
    return out


def _first_sentence(text: str) -> str:
    """Title = first sentence of the rule paragraph (up to the first period)."""
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r"\.(?:\s|$)", text)
    title = text[: m.start()] if m else text
    return title.strip()[:200]


def _detect_tags(text: str) -> list[str]:
    """Return tag families whose cues appear in ``text`` (insertion order)."""
    low = (text or "").lower()
    found: list[str] = []
    for tag, cues in TAG_CUES.items():
        if any(cue in low for cue in cues):
            found.append(tag)
    return found


# --- pure module-level API (unit-testable) ----------------------------------
def tag_rule(rule: dict) -> list[str]:
    """Derive tag families from a rule's text by scanning TAG_CUES.

    ``rule`` is a chunk dict; its ``text`` (falling back to ``title``) is
    scanned. Returns a deterministic, de-duplicated list of tag names.
    """
    text = str(rule.get("text") or rule.get("title") or "")
    return _detect_tags(text)


def parse_rules(all_rules_text: str) -> list[dict]:
    """Parse the rules blob into chunks.

    Returns a list of ``{id, title, text, tags}`` dicts:
      * ``id="invariants"`` — the preamble + the Invariants paragraph, joined.
        Always present (even if the explicit "Invariants" header is missing, in
        which case it captures whatever precedes rule 1, degrading gracefully).
      * ``id="rule.N"`` for each numbered rule N, ``text`` being its full
        (possibly multi-line) paragraph and ``title`` its first sentence.

    Splitting is tolerant: a numbered rule begins on any line matching
    ``^\\s*\\d+\\.``; everything before the first such line is the preamble.
    """
    text = all_rules_text or ""
    lines = text.splitlines()

    preamble_lines: list[str] = []
    rule_starts: list[tuple[int, int]] = []  # (line_index, rule_number)
    for i, line in enumerate(lines):
        m = _NUM_RULE_RE.match(line)
        if m:
            rule_starts.append((i, int(m.group(1))))

    chunks: list[dict] = []

    # Everything before the first numbered rule is the preamble (incl. the
    # Invariants header + paragraph). If there are no numbered rules, the whole
    # blob is the invariants chunk.
    first_rule_line = rule_starts[0][0] if rule_starts else len(lines)
    preamble_lines = lines[:first_rule_line]
    invariants_text = "\n".join(preamble_lines).strip()
    if invariants_text:
        chunks.append(
            {
                "id": "invariants",
                "title": "Invariants (apply always)",
                "text": invariants_text,
                "tags": ["invariants"],
            }
        )

    # Each numbered rule spans from its start line up to the next rule start.
    for idx, (start, num) in enumerate(rule_starts):
        end = rule_starts[idx + 1][0] if idx + 1 < len(rule_starts) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        # Strip the leading "N. " marker so title/text read naturally.
        body = _NUM_RULE_RE.sub("", body, count=1).strip()
        if not body:
            continue
        chunk = {
            "id": f"rule.{num}",
            "title": _first_sentence(body),
            "text": body,
            "tags": [],
        }
        chunk["tags"] = tag_rule(chunk)
        chunks.append(chunk)

    return chunks


def score_rule(rule: dict, query_tokens: set, schema_text: str) -> float:
    """Lexical relevance of a numbered rule to the current query + schema.

    Combines:
      * tag overlap: how many of the rule's tag families are present in the
        query intents (passed in via ``query_tokens`` already? no — see note),
      * raw keyword overlap between the rule's tokens and the query tokens
        (weighted high) and the schema tokens (weighted lower).

    Note: tag-overlap is folded in by the caller (``select``) which knows the
    query's detected intents; this function provides the keyword component plus
    a length-normalized overlap so callers can add tag weight on top. It is
    self-contained and deterministic given its inputs.
    """
    rule_tokens = _tokenize(str(rule.get("text") or ""))
    if not rule_tokens:
        return 0.0
    schema_tokens = _tokenize(schema_text or "")

    q_overlap = len(rule_tokens & (query_tokens or set()))
    s_overlap = len(rule_tokens & schema_tokens)

    # Query overlap dominates; schema overlap is a gentle tie-breaker so rules
    # that name concepts present in the selected schema float up.
    score = 2.0 * float(q_overlap) + 0.5 * float(s_overlap)
    # Normalize lightly by rule length so very long rules don't always win on
    # raw overlap alone (recall-biased: a mild divisor, not a hard penalty).
    score /= 1.0 + (len(rule_tokens) / 80.0)
    return round(score, 4)


# --- schema-text helpers ----------------------------------------------------
def _schema_text_from_bb(bb: dict) -> str:
    """Concatenate descriptions/roles of SELECTED tables and columns.

    Used both for intent detection and for the schema-overlap component of
    scoring. No hardcoded names — purely reads what the blackboard carries.
    """
    parts: list[str] = []
    for t in bb.get("tables", []) or []:
        if t.get("status") not in (None, "selected"):
            continue
        parts.append(str(t.get("name") or ""))
        parts.append(str(t.get("description") or ""))
        for c in t.get("columns", []) or []:
            if c.get("status") not in (None, "selected"):
                continue
            parts.append(col_name(c))
            parts.append(str(c.get("description") or ""))
            parts.append(str(c.get("role") or ""))
            sv = c.get("sample_values") or []
            if isinstance(sv, (list, tuple)):
                parts.extend(str(v) for v in sv[:5])
    return " ".join(p for p in parts if p)


def _query_text_from_bb(bb: dict) -> str:
    req = bb.get("request", {}) or {}
    return str(req.get("user_query") or "")


# --- the agent --------------------------------------------------------------
class BusinessRuleRagAgent:
    """Selects relevant business rules onto the blackboard, deterministically.

    Parameters
    ----------
    all_rules_text:
        The full user-rules markdown blob (preamble + Invariants + numbered
        rules). Parsed once at construction.
    max_chars:
        Character budget for SITUATIONAL (non-always-on) rule bodies. The
        invariants chunk and the always-on core are NOT counted against it.
    """

    def __init__(self, all_rules_text: str, max_chars: int = 3500):
        self.all_rules_text = all_rules_text or ""
        self.max_chars = int(max_chars)
        self.chunks = parse_rules(self.all_rules_text)

        # Partition once: the mandatory invariants chunk, and the numbered rules.
        self._invariants = next(
            (c for c in self.chunks if c.get("id") == "invariants"), None
        )
        self._rules = [c for c in self.chunks if c.get("id") != "invariants"]

        # Always-on core = numbered rules whose tags intersect CORE_TAGS,
        # chosen by tag (not by number), capped to CORE_MAX_RULES. Order is the
        # rules' natural (document) order so the prompt reads coherently.
        core: list[dict] = []
        for r in self._rules:
            if set(r.get("tags") or ()) & set(CORE_TAGS):
                core.append(r)
            if len(core) >= CORE_MAX_RULES:
                break
        self._core_ids = {r["id"] for r in core}
        self._core = core

    # -- public API ----------------------------------------------------------
    def select(self, bb: dict) -> dict:
        """Mutate ``bb`` in place (via ``set_selected_rules``) and return it.

        Builds: [invariants] + [always-on core] + [situational top-K within
        ``max_chars``]. Situational rules are ranked by tag-overlap with the
        query's detected intents plus keyword overlap with the query + schema.
        """
        query_text = _query_text_from_bb(bb)
        schema_text = _schema_text_from_bb(bb)
        query_tokens = _tokenize(query_text)
        # Intents: tag families detected in the query AND the selected schema,
        # so e.g. a date column in the schema pulls in the "dates" intent even
        # if the user's wording is terse.
        intents = set(_detect_tags(query_text)) | set(_detect_tags(schema_text))

        selected: list[dict] = []

        # 1) Invariants — always on, not budgeted.
        if self._invariants is not None:
            selected.append(
                _as_selected_dict(
                    self._invariants, score=1_000_000.0, always_on=True
                )
            )

        # 2) Always-on core — always on, not budgeted.
        core_ids = set(self._core_ids)
        for r in self._core:
            selected.append(_as_selected_dict(r, score=1_000.0, always_on=True))

        # 3) Situational — score the remaining rules and pack into the budget.
        situational: list[tuple[float, dict]] = []
        for r in self._rules:
            if r["id"] in core_ids:
                continue
            kw = score_rule(r, query_tokens, schema_text)
            tag_overlap = len(set(r.get("tags") or ()) & intents)
            # Tag overlap is the primary signal; keyword overlap refines ties.
            total = 3.0 * float(tag_overlap) + kw
            situational.append((round(total, 4), r))

        # Sort by score desc; stable tie-break by document order (rule number).
        def _rule_num(rd: dict) -> int:
            m = re.search(r"(\d+)$", str(rd.get("id") or ""))
            return int(m.group(1)) if m else 1_000_000

        situational.sort(key=lambda pr: (-pr[0], _rule_num(pr[1])))

        budget = self.max_chars
        for total, r in situational:
            # Recall-biased: include a positive-scoring rule if it fits; once a
            # rule overflows the budget, keep trying smaller ones (greedy pack).
            if total <= 0.0:
                continue
            cost = len(str(r.get("text") or ""))
            if cost > budget:
                continue
            selected.append(
                _as_selected_dict(r, score=total, always_on=False)
            )
            budget -= cost

        set_selected_rules(bb, selected)
        return bb


# --- selected-dict projection -----------------------------------------------
def _as_selected_dict(chunk: dict, score: float, always_on: bool) -> dict:
    """Project a parsed chunk into the set_selected_rules contract dict."""
    return {
        "id": str(chunk.get("id") or ""),
        "title": str(chunk.get("title") or ""),
        "text": str(chunk.get("text") or ""),
        "score": float(score),
        "always_on": bool(always_on),
        "tags": list(chunk.get("tags") or []),
    }
