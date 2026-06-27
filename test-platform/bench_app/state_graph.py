"""State transition graph for benchmark runs, cases, and worker jobs.

The graph is deliberately small and explicit. It is used by tests as the
contract for legal lifecycle transitions across server, runner, worker, and UI
code. Self-transitions are allowed by the validator because progress updates
can rewrite the same status with fresher counters.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping


RUN_ACTIVE_STATES = frozenset({"queued", "running", "paused", "judging"})
RUN_FINISHED_STATES = frozenset({"done", "error", "stopped"})
RUN_RECOVERABLE_STATES = frozenset({"stopped", "error"})
RUN_STATES = RUN_ACTIVE_STATES | RUN_FINISHED_STATES

RUN_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"running", "paused", "stopped", "error"}),
    "running": frozenset({"queued", "paused", "judging", "done", "error", "stopped"}),
    "paused": frozenset({"queued", "running", "done", "error", "stopped"}),
    "judging": frozenset({"queued", "running", "paused", "done", "error", "stopped"}),
    # Explicit user actions can re-enter a finished run through the queue:
    # rerun failed cases, rerun one case, or re-run judging.
    "done": frozenset({"queued", "judging"}),
    "error": frozenset({"queued", "judging"}),
    "stopped": frozenset({"queued"}),
}


CASE_WAITING_STATES = frozenset({
    "api_waiting",
    "llm_queued",
    "awaiting_judge",
    "sent_to_judge",
    "judging",
})
CASE_FINAL_STATES = frozenset({
    "done",
    "judged",
    "api_error",
    "api_timeout",
    "no_sql",
    "gold_error",
    "sql_error",
    "judge_error",
})
CASE_STATES = CASE_WAITING_STATES | CASE_FINAL_STATES

CASE_STATUS_LABELS: Mapping[str, str] = {
    "api_waiting": "ждем ответ API",
    "llm_queued": "в очереди на LLM-оценку",
    "awaiting_judge": "ожидает LLM-оценку",
    "sent_to_judge": "отправлен на оценку",
    "judging": "оценивается LLM",
    "judged": "оценка готова",
    "done": "готово",
    "api_error": "ошибка API",
    "api_timeout": "тайм-аут API",
    "no_sql": "нет SQL от API",
    "gold_error": "ошибка gold SQL",
    "sql_error": "ошибка SQL модели",
    "judge_error": "ошибка оценки",
}

CASE_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "api_waiting": frozenset({
        "done",
        "llm_queued",
        "api_error",
        "api_timeout",
        "no_sql",
        "gold_error",
        "sql_error",
    }),
    "api_error": frozenset({"api_waiting", "llm_queued"}),
    "api_timeout": frozenset({"api_waiting", "llm_queued"}),
    "no_sql": frozenset({"api_waiting", "llm_queued"}),
    "gold_error": frozenset({"api_waiting", "llm_queued"}),
    "sql_error": frozenset({"api_waiting", "llm_queued"}),
    "done": frozenset({"api_waiting", "llm_queued"}),
    "llm_queued": frozenset({"api_waiting", "awaiting_judge", "sent_to_judge", "judging", "judged", "judge_error"}),
    "awaiting_judge": frozenset({"api_waiting", "llm_queued", "sent_to_judge", "judging", "judged", "judge_error"}),
    "sent_to_judge": frozenset({"api_waiting", "judging", "judged", "judge_error"}),
    "judging": frozenset({"api_waiting", "judged", "judge_error"}),
    "judged": frozenset({"api_waiting", "llm_queued"}),
    "judge_error": frozenset({"api_waiting", "llm_queued"}),
}


JOB_TYPES = frozenset({
    "run",
    "continue_run",
    "rerun",
    "rerun_case",
    "judge_case",
    "judge_levels",
    "judge_legacy",
})
JOB_STATES = frozenset({"queued", "running", "done", "error", "cancelled"})

JOB_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled", "error"}),
    "running": frozenset({"queued", "done", "error", "cancelled"}),
    "done": frozenset(),
    "error": frozenset(),
    "cancelled": frozenset(),
}


STATE_GRAPHS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "run": RUN_TRANSITIONS,
    "case": CASE_TRANSITIONS,
    "job": JOB_TRANSITIONS,
}

STATE_SETS: Mapping[str, frozenset[str]] = {
    "run": RUN_STATES,
    "case": CASE_STATES,
    "job": JOB_STATES,
}


class InvalidTransition(ValueError):
    """Raised when a transition is not present in the lifecycle graph."""


def allowed_transition(kind: str, before: str | None, after: str | None) -> bool:
    """Return whether a status transition is legal for the given graph kind."""
    if after is None:
        return True
    graph = STATE_GRAPHS[kind]
    states = STATE_SETS[kind]
    if after not in states:
        return False
    if before is None or before == "":
        return True
    if before == after:
        return True
    if before not in states:
        return False
    return after in graph.get(before, frozenset())


def assert_transition(kind: str, before: str | None, after: str | None) -> None:
    if not allowed_transition(kind, before, after):
        raise InvalidTransition(f"invalid {kind} transition: {before!r} -> {after!r}")


def assert_transition_sequence(kind: str, states: Iterable[str | None]) -> None:
    previous = None
    for state in states:
        assert_transition(kind, previous, state)
        previous = state


def graph_edges(kind: str) -> list[tuple[str, str]]:
    return sorted((src, dst) for src, targets in STATE_GRAPHS[kind].items() for dst in targets)


def mermaid_graph(kind: str) -> str:
    lines = ["flowchart LR"]
    for state in sorted(STATE_SETS[kind]):
        lines.append(f"  {state}[{state}]")
    for src, dst in graph_edges(kind):
        lines.append(f"  {src} --> {dst}")
    return "\n".join(lines)
