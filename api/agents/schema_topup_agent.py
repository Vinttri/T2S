"""Schema TOP-UP agent for the T2S Text2SQL pipeline.

This agent implements the #1 requested feature: TABLE / COLUMN top-up.

When an upstream agent decides the currently selected schema is insufficient
(it appended a ``missing_tables_request`` / ``missing_columns_request`` to the
blackboard, or generation reported that it cannot map some concept), this agent
retrieves MORE schema by *reusing the existing ``find()`` retrieval* and merges
the newly found tables into the shared "blackboard" JSON object **without
dropping or reordering anything already selected or used**. It then passes the
augmented blackboard forward to the next agent.

Design:
  * Non-destructive merge is delegated to ``merge_topup_tables`` in
    ``api.core.blackboard`` so the legacy generator path keeps byte-identical
    ``find()`` table_info structures and existing tables keep their order/rank.
  * ``find()`` is *injected* as an async callable (``find_callable``) rather than
    imported, to avoid an import cycle (``graph`` <-> agents).
  * The synthetic retrieval query is built from the user query, the missing
    hints, and the already-selected table names ("keep, do not remove"), so the
    retrieval is biased toward the missing concept while preserving context.
  * Termination is guaranteed two ways: each fulfilled request is removed from
    the blackboard after a successful merge (so the loop has nothing left to
    do), and ``can_topup`` caps the number of rounds at ``max_topups``.
  * Retrieval failures never raise — they are logged and the blackboard is
    returned unchanged.

NO hardcoded table/column names live here; everything is driven by the request
arrays on the blackboard. Stdlib + asyncio only.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, List, Optional

from api.core.blackboard import (
    all_table_names,
    can_topup,
    merge_topup_tables,
    table_name,
)

logger = logging.getLogger(__name__)

# Type alias for the injected find() coroutine function.
FindCallable = Callable[..., Awaitable[List[Any]]]


def build_topup_query(
    user_query: str,
    missing_hints: List[str],
    existing_tables: List[str],
) -> str:
    """Produce a focused retrieval query for the top-up ``find()`` call.

    The shape is intentionally explicit so the retriever is steered toward the
    missing concept while being told to preserve the already-selected tables::

        <user_query>
        Need additional schema for: <hints joined>.
        Already selected (keep, do not remove): <existing tables>.
        Return tables/columns that provide the missing concept.

    Empty hints / empty existing-table lists degrade gracefully (the
    corresponding line is omitted). This is a pure function so it can be unit
    tested without any I/O.
    """
    parts: List[str] = []
    base = (user_query or "").strip()
    if base:
        parts.append(base)

    hints = [h.strip() for h in (missing_hints or []) if h and h.strip()]
    if hints:
        parts.append("Need additional schema for: " + "; ".join(hints) + ".")

    keep = [t.strip() for t in (existing_tables or []) if t and t.strip()]
    if keep:
        parts.append(
            "Already selected (keep, do not remove): " + ", ".join(keep) + "."
        )

    parts.append("Return tables/columns that provide the missing concept.")
    return "\n".join(parts)


class SchemaTopUpAgent:
    """Fulfils ``missing_tables_request`` / ``missing_columns_request`` by
    retrieving more schema via the injected ``find()`` and merging it
    non-destructively into the blackboard."""

    def __init__(
        self,
        find_callable: FindCallable,
        namespaced_graph_id: str,
        db_description: str = "",
        knowledge_spec: str = "",
        db: Any = None,
        max_new_tables: int = 6,
        user_rules_spec: str = "",
    ) -> None:
        self._find = find_callable
        self._namespaced_graph_id = namespaced_graph_id
        self._db_description = db_description or ""
        self._knowledge_spec = knowledge_spec or ""
        self._db = db
        self._max_new_tables = max(0, int(max_new_tables))
        # Top-up retrieval must be rule-aware: when the planner asks for a table
        # to satisfy a business rule (e.g. a validity/classification table), the
        # rules steer find() to the right schema. General rules only — no names.
        self._user_rules_spec = user_rules_spec or ""

    # -- hint collection -----------------------------------------------------
    @staticmethod
    def _collect_hints(requests: Optional[List[dict]]) -> List[str]:
        """Pull a usable hint string from each request, preferring
        ``semantic_hint`` and falling back to ``reason``. Order-preserving and
        de-duplicated."""
        hints: List[str] = []
        seen: set[str] = set()
        for req in requests or []:
            if not isinstance(req, dict):
                continue
            hint = str(req.get("semantic_hint") or "").strip()
            if not hint:
                hint = str(req.get("reason") or "").strip()
            if hint and hint.lower() not in seen:
                seen.add(hint.lower())
                hints.append(hint)
        return hints

    @staticmethod
    def _request_id(requests: Optional[List[dict]]) -> Optional[str]:
        for req in requests or []:
            if isinstance(req, dict) and req.get("id"):
                return str(req["id"])
        return None

    # -- main entry point ----------------------------------------------------
    async def topup(self, bb: dict) -> dict:
        """Run one top-up round.

        If the blackboard has unfulfilled ``missing_tables_request`` /
        ``missing_columns_request`` and ``can_topup(bb)`` is True: build a
        synthetic retrieval query, call ``find_callable``, keep only NEW tables
        (not already present in the blackboard), cap to ``max_new_tables``, and
        merge via ``merge_topup_tables``. Consumed requests are cleared so the
        loop can terminate. Returns ``bb`` (augmented or unchanged); never raises
        on retrieval failure.
        """
        table_reqs = bb.get("missing_tables_request") or []
        column_reqs = bb.get("missing_columns_request") or []

        # Nothing to do.
        if not table_reqs and not column_reqs:
            return bb

        # Double-guard the cap (the caller also enforces max_topups).
        if not can_topup(bb):
            logger.info(
                "SchemaTopUpAgent: top-up budget exhausted "
                "(topup_count=%s, max_topups=%s); skipping.",
                bb.get("retrieval", {}).get("topup_count"),
                bb.get("retrieval", {}).get("max_topups"),
            )
            return bb

        hints = self._collect_hints(table_reqs) + self._collect_hints(column_reqs)
        # De-dup across both request kinds while preserving order.
        deduped: List[str] = []
        seen: set[str] = set()
        for h in hints:
            if h.lower() not in seen:
                seen.add(h.lower())
                deduped.append(h)
        hints = deduped

        user_query = str(bb.get("request", {}).get("user_query") or "").strip()
        graph_id = self._namespaced_graph_id or str(
            bb.get("request", {}).get("graph_id") or ""
        )
        existing_names = sorted(all_table_names(bb))

        synthetic_query = build_topup_query(user_query, hints, existing_names)

        # Reuse find()'s history convention: list whose LAST element is the
        # (synthetic) query. Keep the original user query as context if present.
        queries_history: List[str] = []
        if user_query and user_query != synthetic_query:
            queries_history.append(user_query)
        queries_history.append(synthetic_query)

        request_id = (
            self._request_id(table_reqs)
            or self._request_id(column_reqs)
            or "topup"
        )

        # Retrieve more schema; never raise on failure.
        try:
            found = await self._find(
                graph_id,
                queries_history,
                self._db_description,
                knowledge_spec=self._knowledge_spec,
                user_rules_spec=self._user_rules_spec,
                db=self._db,
            )
        except Exception as exc:  # noqa: BLE001 - retrieval must never break the pipeline
            logger.warning(
                "SchemaTopUpAgent: find() failed during top-up "
                "(request_id=%s, graph=%s): %s",
                request_id,
                graph_id,
                exc,
            )
            return bb

        # Filter to NEW tables only (not already on the blackboard), then cap.
        already = all_table_names(bb)
        new_table_infos: List[Any] = []
        picked: set[str] = set()
        for info in found or []:
            if not isinstance(info, (list, tuple)):
                continue
            nm = table_name(info)
            low = nm.lower()
            if not nm or low in already or low in picked:
                continue
            picked.add(low)
            new_table_infos.append(info)
            if len(new_table_infos) >= self._max_new_tables:
                break

        if not new_table_infos:
            logger.info(
                "SchemaTopUpAgent: no NEW tables found for top-up "
                "(request_id=%s, hints=%s); clearing requests to terminate.",
                request_id,
                hints,
            )
            # Still clear the requests: we tried and found nothing new, so the
            # loop must not spin on the same unfulfillable request forever.
            self._clear_requests(bb)
            bb["trace"].append(
                {
                    "agent": "schema_topup_agent",
                    "action": "no_new_schema",
                    "request_id": request_id,
                    "hints": hints,
                }
            )
            return bb

        added = merge_topup_tables(bb, new_table_infos, request_id)

        logger.info(
            "SchemaTopUpAgent: merged %d new table(s) for top-up "
            "(request_id=%s): %s",
            len(added),
            request_id,
            added,
        )

        # Consume the requests so the loop terminates.
        self._clear_requests(bb)
        return bb

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _clear_requests(bb: dict) -> None:
        """Remove the fulfilled top-up requests so the calling loop terminates."""
        bb["missing_tables_request"] = []
        bb["missing_columns_request"] = []
