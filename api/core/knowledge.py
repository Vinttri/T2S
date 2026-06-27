"""Helpers for applying database-specific knowledge to text-to-SQL prompts."""

from __future__ import annotations

import re


_CONCEPT_RE = re.compile(
    r"(?ms)^- \[(?P<idx>\d+)\] (?P<title>[^\n]+)\n"
    r"\s+Description: (?P<description>.*?)\n"
    r"\s+Definition: (?P<definition>.*?)(?=^- \[\d+\] |\Z)"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


_COMPONENT_METRIC_EXAMPLES = (
    "points",
    "position",
    "duration",
    "time",
    "count",
)


def _tokens(text: str) -> set[str]:
    result = set()
    for token in _TOKEN_RE.findall(text.lower()):
        if len(token) <= 2:
            continue
        result.add(token)
        if token.startswith("perform"):
            result.add("perform")
            result.add("performance")
        if token.startswith("finish"):
            result.add("finish")
            result.add("finisher")
        if token.startswith("session"):
            result.add("session")
    return result


def focus_knowledge_for_query(knowledge_spec: str, user_query: str) -> str:
    """Return the most relevant concepts from a loaded knowledge document.

    Full KB files are useful for coverage, but they are noisy in prompts. This
    keeps formulas that match the current question near the model and reduces
    the chance of choosing a component metric, such as points, instead of a
    named business metric, such as a performance index.
    """
    knowledge_spec = (knowledge_spec or "").strip()
    if not knowledge_spec:
        return ""

    concepts = []
    for match in _CONCEPT_RE.finditer(knowledge_spec):
        concept = match.groupdict()
        title = concept["title"]
        description = " ".join(concept["description"].split())
        definition = " ".join(concept["definition"].split())
        concepts.append({
            "idx": concept["idx"],
            "title": title,
            "description": description,
            "definition": definition,
        })

    if not concepts:
        return knowledge_spec

    query_tokens = _tokens(user_query)
    scored = []
    for concept in concepts:
        title_tokens = _tokens(concept["title"])
        description_tokens = _tokens(concept["description"])
        definition_tokens = _tokens(concept["definition"])
        # General lexical relevance only — name match weighted highest, then
        # description, then definition. No database/domain-specific term nudges
        # (those belong in the delivered KB, never in engine code).
        score = (
            5 * len(query_tokens & title_tokens)
            + 3 * len(query_tokens & description_tokens)
            + len(query_tokens & definition_tokens)
        )

        if score > 0:
            scored.append((score, concept))

    if not scored:
        return knowledge_spec

    selected = [
        concept for _, concept in sorted(
            scored,
            key=lambda item: (-item[0], int(item[1]["idx"]))
        )[:5]
    ]
    primary = selected[0]

    lines = [
        "Selected database knowledge relevant to the current query.",
        "The primary matched concept is the best lexical match, not a guaranteed final intent.",
        "MANDATORY: Compare the user's wording against every top candidate below before deciding which concept to apply.",
        "If an alternative candidate better matches the user's wording, use that candidate's definition instead of the primary concept.",
        "If the user query can be answered by a selected concept, the SQL metric must implement that concept's definition.",
        "Do not answer with only a component/input metric when a selected concept defines an index, score, rate, classification, or unit conversion.",
        "",
        "Primary matched concept:",
        f"- [{primary['idx']}] {primary['title']}",
        f"  Description: {primary['description']}",
        f"  Definition: {primary['definition']}",
    ]

    for component in _COMPONENT_METRIC_EXAMPLES:
        if component in query_tokens:
            lines.append(
                f"- The word '{component}' may identify an input to the primary concept; "
                "do not use it as the final metric when the primary concept applies."
            )

    alternatives = selected[1:]
    if not alternatives:
        return "\n".join(lines)

    lines.extend([
        "",
        "Alternative candidate concepts from the same top-5 match set:",
    ])
    for concept in alternatives:
        lines.extend([
            f"- [{concept['idx']}] {concept['title']}",
            f"  Description: {concept['description']}",
            f"  Definition: {concept['definition']}",
        ])

    return "\n".join(lines)
