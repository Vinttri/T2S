"""Graph-related routes for the text2sql API."""
# pylint: disable=line-too-long,trailing-whitespace

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Optional, TYPE_CHECKING, Union

from pydantic import BaseModel
from redis import ResponseError, RedisError

from api.core.errors import GraphNotFoundError, InternalError, InvalidArgumentError
from api.core.schema_loader import load_database
from api.core.pipeline import (
    parenthesize_json_in_operators,
    promote_bare_to_json_leaf,
    MESSAGE_DELIMITER,
    auto_quote_sql_identifiers,
    fix_json_operator_chain,
    build_destructive_confirmation_message,
    check_schema_modification,
    detect_destructive_operation,
    format_ai_response,
    get_database_type_and_loader,
    graph_name,
    is_general_graph,
    quote_identifiers_from_graph,
    sanitize_log_input,
    sanitize_query,
    save_memory_background,
    validate_read_only_sql,
    validate_and_truncate_chat,
    validate_custom_model,
)
from api.core.knowledge import focus_knowledge_for_query
from api.core.query_cache import execute_with_cache
from api.agents import AnalysisAgent, RelevancyAgent, FollowUpAgent
from api.agents.healer_agent import HealerAgent
from api.agents.sql_gate_agent import SqlGateAgent
from api.agents.filter_validator_agent import FilterValidatorAgent
from api.agents.metric_resolver_agent import (
    MetricResolverAgent,
    detect_concepts,
    expand_concept_references,
    render_resolved_block,
)
from api.agents.formula_validator_agent import FormulaValidatorAgent
from api.agents.linker_agent import (
    LinkerAgent,
    render_plan_block,
    render_clarification,
)
from api.core.schema_selection import (
    candidate_columns_retrieval_text,
    table_grain_lines,
    selected_columns_retrieval_text,
    rank_columns_by_relevance,
    _render_json_leaf_paths,
    selected_schema_compact,
)
from api.agents.sql_semantic_validator import (
    check_evidence_grounding,
    evidence_repair_hint,
)
from api.core.session_context import build_prior_turn_block
from api.sql_utils import sql_gate
from api.sql_utils.gate_registry import run_gates, GateContext
from api.sql_utils.sql_comments import render_commented_sql
from api.config import Config
from api.core.db_resolver import resolve_db
from api.core.result_models import QueryAnalysis, QueryMetadata, QueryResult, RefreshResult
from api.graph import (
    compute_join_skeleton,
    column_json_paths,
    json_leaf_owner_tables,
    fetch_table_entries,
    materialize_fk_edges,
    copy_graph,
    drop_graph,
    find,
    get_db_description,
    get_document_sources,
    get_knowledge,
    get_user_rules,
    graph_exists,
    index_text_chunks,
    retrieve_indexed_context,
    retrieve_concept_chunks,
    set_knowledge,
    set_user_rules,
)

if TYPE_CHECKING:
    from falkordb.asyncio import FalkorDB


async def _create_memory_tool(user_id: str, graph_id: str, db=None):
    """Lazy-create a MemoryTool.

    ``graphiti_core`` lives in the ``[server]`` extra; deferring the import
    keeps ``pip install t2s`` (no extras) working for SDK callers
    that pass ``use_memory=False`` (the SDK default).
    """
    # pylint: disable=import-outside-toplevel
    from api.memory.graphiti_tool import MemoryTool
    return await MemoryTool.create(user_id, graph_id, db=db)


async def _memory_context_or_none(
    memory_tool_task,
    query: str,
    custom_model: str | None = None,
    custom_api_key: str | None = None,
):
    """Recall prior SIMILAR queries as ADVISORY examples for the generator.

    Uses the dedicated :class:`MemoryAgent` (which judges intent similarity and
    hands back prior ``question -> SQL`` examples + a recommendation) instead of
    graphiti's fragile conversational entity-graph search. The block is
    suggestions only — the generator decides. Memory outages must not break SQL,
    so any failure returns ``(None, None)`` and the pipeline proceeds.
    """
    if memory_tool_task is None:
        return None, None
    try:
        memory_tool = await memory_tool_task
        if memory_tool is None:
            return None, None
        from api.agents.memory_agent import MemoryAgent  # pylint: disable=import-outside-toplevel
        block = await MemoryAgent(
            custom_api_key=custom_api_key, custom_model=custom_model,
        ).recall(query, memory_tool)
        return memory_tool, (block or "")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning(
            "Memory recall disabled for this query: %s",
            sanitize_log_input(str(exc))[:300],
        )
        return None, None


def _direct_follow_up_from_analysis(analysis: dict) -> str:
    """Return a deterministic follow-up when analysis already explains a gap."""
    if not isinstance(analysis, dict):
        return ""
    missing = " ".join(str(analysis.get("missing_information") or "").split())
    ambiguities = " ".join(str(analysis.get("ambiguities") or "").split())
    explanation = " ".join(str(analysis.get("explanation") or "").split())
    message = missing or ambiguities or explanation
    if not message:
        return ""
    max_chars = 700
    if len(message) > max_chars:
        message = message[: max_chars - 1].rstrip() + "…"
    return message


class GraphData(BaseModel):
    """Graph data model.

    Args:
        BaseModel (_type_): _description_
    """
    database: str


class ChatRequest(BaseModel):
    """Chat request model.

    Args:
        BaseModel (_type_): _description_
    """
    chat: list[str]
    result: list[str] | None = None
    instructions: str | None = None
    custom_api_key: str | None = None
    custom_model: str | None = None
    use_user_rules: bool = True  # If True, fetch user rules from database
    use_knowledge: bool = True  # If True, fetch database-specific knowledge
    use_memory: bool = False
    # Client-echoed prior-turn JSON for session continuity (refine the same plan
    # on a follow-up). Shape: {db_id, prior_question, prior_sql, selected_columns}.
    session_context: dict | None = None


class ConfirmRequest(BaseModel):
    """Confirmation request model.

    Args:
        BaseModel (_type_): _description_
    """
    sql_query: str
    confirmation: str = ""
    chat: list = []
    custom_api_key: str | None = None
    custom_model: str | None = None
    use_memory: bool = False



async def get_schema(user_id: str, graph_id: str, db=None):  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    """Return all nodes and edges for the specified database schema (namespaced to the user).

    This endpoint returns a JSON object with two keys: `nodes` and `edges`.
    Nodes contain a minimal set of properties (id, name, labels, props).
    Edges contain source and target node names (or internal ids), type and props.

        args:
            graph_id (str): The ID of the graph to query (the database name).
    """
    namespaced = graph_name(user_id, graph_id)
    try:
        graph = resolve_db(db).select_graph(namespaced)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Failed to select graph %s: %s", sanitize_log_input(namespaced), e)
        raise GraphNotFoundError("Graph not found or database error") from e

    # Build table nodes with columns and table-to-table links (foreign keys)
    tables_query = """
    MATCH (t:Table)
    OPTIONAL MATCH (c:Column)-[:BELONGS_TO]->(t)
    OPTIONAL MATCH (c)-[out:REFERENCES]->(target_col:Column)-[:BELONGS_TO]->(target_table:Table)
    OPTIONAL MATCH (source_col:Column)-[incoming:REFERENCES]->(c)
    OPTIONAL MATCH (source_col)-[:BELONGS_TO]->(source_table:Table)
    RETURN
        t.name AS table,
        t.description AS description,
        collect(DISTINCT {
            name: c.name,
            type: c.type,
            description: c.description,
            nullable: c.nullable,
            key_type: c.key_type,
            sample_values: c.sample_values,
            references_table: target_table.name,
            references_column: target_col.name,
            references_note: out.note,
            referenced_by_table: source_table.name,
            referenced_by_column: source_col.name,
            referenced_by_note: incoming.note
        }) AS columns
    ORDER BY table
    """

    links_query = """
    MATCH (src_col:Column)-[:BELONGS_TO]->(src_table:Table),
          (tgt_col:Column)-[:BELONGS_TO]->(tgt_table:Table),
          (src_col)-[:REFERENCES]->(tgt_col)
    RETURN DISTINCT
        src_table.name AS source,
        tgt_table.name AS target,
        src_col.name AS source_column,
        tgt_col.name AS target_column
    """

    try:
        tables_res = (await graph.query(tables_query)).result_set
        links_res = (await graph.query(links_query)).result_set
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.error("Error querying graph data for %s: %s", sanitize_log_input(namespaced), e)
        raise InternalError("Failed to read graph data") from e

    nodes = []
    for row in tables_res:
        try:
            table_name, table_description, columns = row
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        # Normalize columns: ensure a list of dicts with name/type
        if not isinstance(columns, list):
            columns = [] if columns is None else [columns]

        normalized_by_name: dict[str, dict[str, Any]] = {}
        for col in columns:
            try:
                # col may be a mapping-like object or a simple value
                if not col:
                    continue
                # Some drivers may return a tuple or list for the collected map
                if isinstance(col, (list, tuple)) and len(col) >= 2:
                    # try to interpret as (name, type)
                    name = col[0]
                    ctype = col[1] if len(col) > 1 else None
                elif isinstance(col, dict):
                    name = col.get('name') or col.get('columnName')
                    ctype = col.get('type') or col.get('dataType')
                    description = col.get('description') or ""
                    nullable = col.get('nullable') or ""
                    key_type = col.get('key_type') or col.get('keyType') or ""
                    sample_values = col.get('sample_values') or col.get('sampleValues') or ""
                    references_table = col.get('references_table')
                    references_column = col.get('references_column')
                    references_note = col.get('references_note') or ""
                    referenced_by_table = col.get('referenced_by_table')
                    referenced_by_column = col.get('referenced_by_column')
                    referenced_by_note = col.get('referenced_by_note') or ""
                else:
                    name = str(col)
                    ctype = None
                    description = ""
                    nullable = ""
                    key_type = ""
                    sample_values = ""
                    references_table = None
                    references_column = None
                    references_note = ""
                    referenced_by_table = None
                    referenced_by_column = None
                    referenced_by_note = ""

                if not name:
                    continue

                column = normalized_by_name.setdefault(str(name), {
                    "name": str(name),
                    "type": ctype,
                    "description": description,
                    "nullable": nullable,
                    "key_type": key_type,
                    "sample_values": sample_values,
                    "references": [],
                    "referenced_by": [],
                })
                if not column.get("type") and ctype:
                    column["type"] = ctype
                if not column.get("description") and description:
                    column["description"] = description
                if not column.get("nullable") and nullable:
                    column["nullable"] = nullable
                if not column.get("key_type") and key_type:
                    column["key_type"] = key_type
                if not column.get("sample_values") and sample_values:
                    column["sample_values"] = sample_values
                if references_table and references_column:
                    ref = {
                        "table": references_table,
                        "column": references_column,
                        "note": references_note,
                    }
                    if ref not in column["references"]:
                        column["references"].append(ref)
                if referenced_by_table and referenced_by_column:
                    ref_by = {
                        "table": referenced_by_table,
                        "column": referenced_by_column,
                        "note": referenced_by_note,
                    }
                    if ref_by not in column["referenced_by"]:
                        column["referenced_by"].append(ref_by)
            except Exception:  # pylint: disable=broad-exception-caught
                continue

        normalized = sorted(normalized_by_name.values(), key=lambda item: item["name"])
        nodes.append({
            "id": table_name,
            "name": table_name,
            "description": table_description or "",
            "columns": normalized,
        })

    links = []
    seen = set()
    for row in links_res:
        try:
            source, target, source_column, target_column = row
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        key = (source, target, source_column, target_column)
        if key in seen:
            continue
        seen.add(key)
        links.append({
            "source": source,
            "target": target,
            "source_column": source_column,
            "target_column": target_column,
        })

    return {"nodes": nodes, "links": links}


# ---------------------------------------------------------------------------
# Unified text2sql pipeline
#
# ``run_query`` and ``run_confirmed`` are async generators that yield wire-format
# progress events as plain dicts and end with a ``_Final(QueryResult)`` sentinel.
#
# • Streaming consumers (api/routes/graphs.py): serialize each yielded dict as
#   ``json + MESSAGE_DELIMITER`` and stop when ``_Final`` arrives — the user-facing
#   "final" event was already emitted as a regular dict before the sentinel.
# • SDK consumers (t2s): use ``collect_result`` to drop progress
#   events and return the final ``QueryResult``.
#
# This is the one source of truth for the text2sql pipeline.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Final:
    """Sentinel terminating the pipeline generator with a structured result."""
    value: QueryResult


async def collect_result(
    gen: AsyncGenerator[Union[dict, _Final], None],
) -> QueryResult:
    """Drain a pipeline generator, returning the final ``QueryResult``.

    Used by SDK consumers that don't care about progress events. Streaming
    consumers iterate manually so they can serialize each dict event.
    """
    async for event in gen:
        if isinstance(event, _Final):
            return event.value
    raise InternalError("Pipeline produced no final result")


async def _emit_schema_refresh(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    loader_class,
    namespaced: str,
    db_url: str,
    operation_type: str,
    *,
    db: Optional["FalkorDB"] = None,
    mark_final_response: bool = False,
) -> AsyncGenerator[dict, None]:
    """Refresh the graph schema and yield the standard wire events.

    ``mark_final_response`` adds the ``final_response: False`` field to events.
    The streaming path (``run_query``) sets it; the confirm path historically
    omits it. Threading the divergence through one parameter keeps the two
    callers from drifting again.
    """
    base = {"final_response": False} if mark_final_response else {}
    yield {
        **base,
        "type": "reasoning_step",
        "message": "Step 3: Schema change detected - refreshing graph...",
    }

    refresh_success, refresh_message = await loader_class.refresh_graph_schema(
        namespaced, db_url, db=db,
    )
    if refresh_success:
        yield {
            **base,
            "type": "schema_refresh",
            "message": (
                f"✅ Schema change detected ({operation_type} operation)\n\n"
                "🔄 Graph schema has been automatically refreshed with the "
                "latest database structure."
            ),
            "refresh_status": "success",
        }
    else:
        yield {
            **base,
            "type": "schema_refresh",
            "message": (
                f"⚠️ Schema was modified but graph refresh failed: "
                f"{refresh_message}"
            ),
            "refresh_status": "failed",
        }


def _build_query_result(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    sql_query: str,
    results: list,
    ai_response: str,
    *,
    confidence: float = 0.0,
    is_valid: bool = True,
    is_destructive: bool = False,
    requires_confirmation: bool = False,
    execution_time: float = 0.0,
    missing_information: str = "",
    ambiguities: str = "",
    explanation: str = "",
    error_message: Optional[str] = None,
    sql_commented: str = "",
    column_evidence: Optional[list] = None,
    evidence_issues: Optional[list] = None,
    schema_json: Optional[dict] = None,
) -> QueryResult:
    """Assemble a ``QueryResult`` from the pipeline's loose state."""
    # Deterministic LAST-LINE repair on the SQL that is RETURNED to the caller
    # (universal — every response path funnels through here), so the displayed SQL
    # is gate-clean regardless of generation path. Notably fixes asymmetric
    # case-folding `col = LOWER('X')` -> `LOWER(col) = LOWER('X')`. Fail-safe.
    if sql_query and sql_query.strip():
        try:
            from api.sql_utils.gate_registry import run_gates as _rg, GateContext as _GC  # pylint: disable=import-outside-toplevel
            _gsql, _, _grep = _rg(sql_query, _GC(db_type="postgresql"))
            if _grep and _gsql:
                sql_query = _gsql
        except Exception:  # pylint: disable=broad-exception-caught
            pass
    return QueryResult(
        sql_query=sql_query,
        results=results,
        ai_response=ai_response,
        sql_commented=sql_commented,
        column_evidence=column_evidence or [],
        evidence_issues=evidence_issues or [],
        schema_json=schema_json or {},
        metadata=QueryMetadata(
            confidence=confidence,
            is_valid=is_valid,
            is_destructive=is_destructive,
            requires_confirmation=requires_confirmation,
            execution_time=execution_time,
        ),
        analysis=QueryAnalysis(
            missing_information=missing_information,
            ambiguities=ambiguities,
            explanation=explanation,
        ),
        error_message=error_message,
    )


def _knowledge_primary_for_log(knowledge_spec: str | None) -> str:
    """Return the focused primary concept label without logging the full KB body."""
    if not knowledge_spec:
        return "none"

    lines = [line.strip() for line in knowledge_spec.splitlines()]
    for index, line in enumerate(lines[:-1]):
        if line == "Primary matched concept:" and lines[index + 1].startswith("- ["):
            return sanitize_log_input(lines[index + 1])[:180]

    for line in lines:
        if re.match(r"^- \[[^\]]+\] ", line):
            return sanitize_log_input(line)[:180]

    return "unstructured"


_SAFE_SQL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TEXT_TYPE_MARKERS = ("char", "text", "string")
_VALUE_SAMPLE_NAME_MARKERS = (
    "_cd",
    "code",
    "type",
    "role",
    "status",
    "group",
    "class",
    "category",
    "kind",
    "flag",
    "currency",
)
_VALUE_SAMPLE_SKIP_MARKERS = (
    "date",
    "time",
    "amount",
    "balance",
    "sum",
    "avg",
    "number",
    "nbr",
    "phone",
    "email",
    "address",
)
_VALUE_SAMPLE_STRONG_MARKERS = (
    "_cd",
    "code",
    "type",
    "role",
    "status",
    "group",
)
_VALUE_SAMPLE_STOPWORDS = {
    "найдите",
    "найти",
    "покажите",
    "показать",
    "выведите",
    "вывести",
    "которых",
    "которые",
    "который",
    "таким",
    "такие",
    "каждого",
    "каждой",
    "подходящего",
    "атрибутов",
    "значение",
    "значением",
    "заполнен",
    "заполнено",
    "пустой",
    "пустое",
    "непустой",
    "непустое",
    "суммарный",
    "суммарная",
    "общий",
    "общая",
    "превышает",
    "средний",
    "средняя",
    "номер",
    "краткое",
    "наименование",
    "уникальное",
    "количество",
    "всем",
    "все",
    "всех",
    "where",
    "select",
    "from",
    "group",
    "order",
    "limit",
}


def _bounded_int_env(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return min(max(value, min_value), max_value)


def _safe_table_ref(table_name: str) -> str | None:
    parts = str(table_name or "").split(".")
    if not parts or not all(_SAFE_SQL_NAME_RE.match(part) for part in parts):
        return None
    return ".".join(parts)


def _safe_column_ref(column_name: str) -> str | None:
    column_name = str(column_name or "")
    if not _SAFE_SQL_NAME_RE.match(column_name):
        return None
    return column_name


def _is_value_sample_candidate(column: dict) -> bool:
    column_name = str(column.get("columnName") or column.get("name") or "").lower()
    column_type = str(column.get("dataType") or column.get("type") or "").lower()
    if not column_name or not any(marker in column_type for marker in _TEXT_TYPE_MARKERS):
        return False
    if column_name.endswith("_id") or column_name == "id":
        return False
    has_marker = any(marker in column_name for marker in _VALUE_SAMPLE_NAME_MARKERS)
    if not has_marker:
        return False
    has_strong_marker = any(marker in column_name for marker in _VALUE_SAMPLE_STRONG_MARKERS)
    if (
        any(marker in column_name for marker in _VALUE_SAMPLE_SKIP_MARKERS)
        and not has_strong_marker
    ):
        return False
    return True


def _value_search_terms(user_query: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-zА-Яа-яЁё]{4,}", (user_query or "").lower()):
        if token in _VALUE_SAMPLE_STOPWORDS:
            continue
        if re.search(r"[а-яё]", token):
            # A short stem is enough to match common inflected word forms.
            term = token[:4] if len(token) >= 5 else token
        else:
            term = token
        if term not in terms:
            terms.append(term)
        if len(terms) >= 8:
            break
    return terms


def _extract_sample_values(rows: list, limit: int) -> list[str]:
    values: list[str] = []
    seen = set()
    for row in rows or []:
        if isinstance(row, dict):
            raw_value = next(iter(row.values()), None)
        elif isinstance(row, (list, tuple)) and row:
            raw_value = row[0]
        else:
            raw_value = row
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value[:100])
        if len(values) >= limit:
            break
    return values


def _merge_column_sample_values(column: dict, values: list[str], limit: int) -> None:
    """Merge runtime sample values into the structured column sample field."""
    if not values:
        return

    existing_raw = (
        column.get("sample_values")
        or column.get("sampleValues")
        or column.get("samples")
        or []
    )
    existing: list[str] = []
    if isinstance(existing_raw, str):
        try:
            parsed = json.loads(existing_raw)
            if isinstance(parsed, list):
                existing = [str(item).strip() for item in parsed if str(item).strip()]
        except Exception:  # pylint: disable=broad-exception-caught
            existing = [existing_raw.strip()] if existing_raw.strip() else []
    elif isinstance(existing_raw, (list, tuple)):
        existing = [str(item).strip() for item in existing_raw if str(item).strip()]

    merged: list[str] = []
    seen: set[str] = set()
    for value in [*existing, *values]:
        value = str(value).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        merged.append(value[:100])
        if len(merged) >= limit:
            break
    column["sample_values"] = merged


def _query_results_preview(rows: list, max_rows: int = 20, max_chars: int = 4000) -> str:
    """Return a compact JSON preview suitable for chat memory."""
    if not rows:
        return ""
    preview_rows = rows[:max_rows]
    text = json.dumps(preview_rows, ensure_ascii=False, default=str)
    if len(rows) > max_rows:
        text += f"\n... {len(rows) - max_rows} more row(s) omitted"
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text


def _fast_response(sql_query: str, query_results: list) -> str:
    """Return a deterministic response without an extra formatter LLM call."""
    row_count = len(query_results or [])
    if row_count == 0:
        return (
            "Запрос выполнен, строки не найдены.\n\n"
            f"SQL:\n```sql\n{sql_query}\n```"
        )

    preview = _query_results_preview(query_results, max_rows=10, max_chars=3000)
    return (
        f"Запрос выполнен. Получено строк: {row_count}.\n\n"
        f"SQL:\n```sql\n{sql_query}\n```\n\n"
        f"Первые строки:\n```json\n{preview}\n```"
    )


# ---------------------------------------------------------------------------
# Temporal profiling (universal, data-driven): probe validity-interval tables
# ONCE per process (cached) to learn how their effective start/end dates really
# behave, then append a data-grounded hint to the table description so SQL
# generation chooses "latest row by start date" vs "NULL-safe as-of" instead of
# a naive CURRENT_DATE BETWEEN that silently drops rows whose end date is NULL
# or already in the past. Bounded: one cheap aggregate per interval table.
# ---------------------------------------------------------------------------

_TEMPORAL_PROFILE_HINT_CACHE: dict = {}
_TEMPORAL_DATE_TYPE_MARKERS = ("date", "timestamp", "datetime")
_TEMPORAL_START_RE = re.compile(
    r"начал|старт|\bstart|\bbegin|откры|date_from|\bfrom\b", re.IGNORECASE
)
_TEMPORAL_END_RE = re.compile(
    r"оконч|заверш|закры|\bend\b|\bfinal|\bclose|date_to|\bto\b|\bthru", re.IGNORECASE
)
_TEMPORAL_TECH_RE = re.compile(
    r"вставк|создани|изменени|insert|update|\bload|t_datetime|date_time_insert|"
    r"date_time_update|кхд|created|modified",
    re.IGNORECASE,
)


def _temporal_col_name(column: dict) -> str:
    return str(column.get("columnName") or column.get("name") or "")


def _temporal_is_date_col(column: dict) -> bool:
    col_type = str(column.get("dataType") or column.get("type") or "").lower()
    if any(marker in col_type for marker in _TEMPORAL_DATE_TYPE_MARKERS):
        return True
    name = _temporal_col_name(column).lower()
    blob = f"{name} {str(column.get('description') or '').lower()}"
    return "дата" in blob or "date" in name


def _detect_validity_interval(columns) -> tuple:
    """Return (start_col, end_col, key_col) for a validity-interval table, else
    (None, None, None). Description/name driven; technical insert/update
    timestamps are ignored."""
    start = end = None
    for column in columns or []:
        if not isinstance(column, dict):
            continue
        blob = f"{_temporal_col_name(column)} {column.get('description') or ''}"
        if _TEMPORAL_TECH_RE.search(blob) or not _temporal_is_date_col(column):
            continue
        if end is None and _TEMPORAL_END_RE.search(blob):
            end = _temporal_col_name(column)
            continue
        if start is None and _TEMPORAL_START_RE.search(blob):
            start = _temporal_col_name(column)
    if not (start and end):
        return None, None, None
    key = None
    key_pk = None
    for column in columns or []:
        if not isinstance(column, dict) or _temporal_is_date_col(column):
            continue
        name = _temporal_col_name(column)
        if not name or name in (start, end):
            continue
        desc = str(column.get("description") or "").upper()
        key_type = str(column.get("keyType") or column.get("key_type") or "").upper()
        if "FOREIGN KEY" in desc or "FK" in key_type:
            key = name
            break
        is_pk = "PRIMARY KEY" in desc or "PRI" in key_type or "PK" in key_type
        if is_pk and key_pk is None and name.lower().endswith(("id", "guid", "uuid")):
            key_pk = name
    return start, end, (key or key_pk)


def _build_temporal_hint(rows, has_key, start, end, key) -> str:
    """FACT-ONLY temporal profile: state what the data actually shows about the
    validity interval, and let the validity RULE decide the SQL. Deliberately
    prescribes NO SQL form (no ROW_NUMBER / as-of / BETWEEN) so it cannot fight
    the user_rules; business logic lives in the rules, this only supplies the
    discovered fact the rule consumes."""
    row = None
    for candidate in rows or []:
        row = candidate
        break
    if not isinstance(row, dict):
        return ""

    def _num(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    total = _num(row.get("n"))
    n_keys = _num(row.get("n_keys")) if has_key else 0
    n_open = _num(row.get("n_open"))
    n_past = _num(row.get("n_past"))
    history = bool(has_key and n_keys and total > n_keys)
    single_per_key = bool(has_key and n_keys and total == n_keys)
    end_nullable = n_open > 0
    end_past = n_past > 0
    if not (history or end_nullable or end_past):
        return ""
    facts = []
    if history:
        facts.append(f"несколько записей на ключ «{key}» (история версий)")
    elif single_per_key:
        facts.append(f"одна запись на ключ «{key}»")
    if end_nullable:
        facts.append(f"«{end}» бывает NULL")
    if end_past:
        facts.append(f"«{end}» бывает в прошлом у актуальных (последних) записей")
    return (
        f"ВРЕМЕННОЙ ПРОФИЛЬ (факт по данным): {'; '.join(facts)}. "
        f"Учитывай этот факт при выборе «текущей/последней» записи согласно правилу о валидности; "
        f"окно действия не определяет актуальность само по себе."
    )


async def _enrich_tables_with_temporal_profile(
    tables: list,
    loader_class,
    db_url: str,
    db_type: str,
) -> list:
    """Append a data-grounded temporal hint to validity-interval tables.

    One bounded aggregate per interval table, probed at most once per process
    (cached in _TEMPORAL_PROFILE_HINT_CACHE)."""
    if os.getenv("QW_TEMPORAL_PROFILE_ENABLED", "true").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return tables
    if not tables or not loader_class or not db_url:
        return tables
    if db_type not in {"impala", "postgresql", "postgres", "mysql", "snowflake"}:
        return tables
    today = "CURRENT_DATE()" if db_type == "impala" else "CURRENT_DATE"
    sample_cap = int(getattr(Config, "QW_TEMPORAL_PROFILE_SAMPLE", 200000) or 200000)
    max_tables = int(getattr(Config, "QW_TEMPORAL_PROFILE_MAX_TABLES", 8) or 8)
    profiled = 0
    for table_info in tables:
        if profiled >= max_tables:
            break
        if not isinstance(table_info, list) or len(table_info) < 4:
            continue
        table_ref = _safe_table_ref(table_info[0])
        if not table_ref:
            continue
        start, end, key = _detect_validity_interval(table_info[3])
        if not (start and end):
            continue
        start_ref = _safe_column_ref(start)
        end_ref = _safe_column_ref(end)
        key_ref = _safe_column_ref(key) if key else None
        if not (start_ref and end_ref):
            continue
        cache_key = (db_url, table_ref)
        hint = _TEMPORAL_PROFILE_HINT_CACHE.get(cache_key)
        if hint is None:
            key_select = f"COUNT(DISTINCT {key_ref}) AS n_keys, " if key_ref else ""
            probe_sql = (
                f"SELECT COUNT(*) AS n, {key_select}"
                f"SUM(CASE WHEN {end_ref} IS NULL THEN 1 ELSE 0 END) AS n_open, "
                f"SUM(CASE WHEN {end_ref} IS NOT NULL AND {end_ref} < {today} "
                f"THEN 1 ELSE 0 END) AS n_past "
                f"FROM (SELECT * FROM {table_ref} LIMIT {sample_cap}) AS prof_t"
            )
            try:
                rows = await asyncio.to_thread(
                    execute_with_cache,
                    lambda sql: loader_class.execute_sql_query(sql, db_url),
                    probe_sql,
                    db_url=db_url,
                    db_type=db_type,
                )
                hint = _build_temporal_hint(rows, key_ref is not None, start, end, key)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.info(
                    "Temporal profile skipped: table=%s error=%s",
                    sanitize_log_input(table_ref),
                    sanitize_log_input(str(exc))[:240],
                )
                hint = ""
            _TEMPORAL_PROFILE_HINT_CACHE[cache_key] = hint
        profiled += 1
        if hint:
            table_info[1] = f"{table_info[1]}\n[[TEMPORAL]]{hint}"
            logging.info(
                "Temporal profile added: table=%s", sanitize_log_input(table_ref)
            )
    return tables


async def _enrich_tables_with_value_samples(
    tables: list,
    loader_class,
    db_url: str,
    db_type: str,
    user_query: str,
) -> list:
    """Add runtime DISTINCT samples to structured column context."""
    if not tables or not loader_class or not db_url:
        return tables
    if db_type not in {"impala", "postgresql", "postgres", "mysql", "snowflake"}:
        return tables
    if not getattr(Config, "QW_VALUE_SAMPLING_ENABLED", True):
        return await _enrich_tables_with_temporal_profile(
            tables, loader_class, db_url, db_type
        )

    value_limit = int(getattr(Config, "QW_VALUE_SAMPLE_LIMIT", 8))
    column_limit = int(getattr(Config, "QW_VALUE_SAMPLE_MAX_COLUMNS", 12))
    max_sqls_per_column = int(getattr(Config, "QW_VALUE_SAMPLE_MAX_SQLS_PER_COLUMN", 2))
    # Default ON: code-like filter columns get DISTINCT previews even when the
    # question wording doesn't LIKE-match the stored values (e.g. «ссудные
    # счета» vs role code 'ACCOUNT'). Out-of-the-box behaviour must match the
    # verified contour — no install-time tuning flags.
    fallback_enabled = os.getenv(
        "QW_VALUE_SAMPLE_FALLBACK_ENABLED", "true"
    ).strip().lower() in {"1", "true", "yes", "on"}
    if column_limit <= 0:
        return tables
    search_terms = _value_search_terms(user_query)
    sampled_columns = 0

    for table_info in tables:
        if sampled_columns >= column_limit:
            break
        if not isinstance(table_info, list) or len(table_info) < 4:
            continue
        table_ref = _safe_table_ref(table_info[0])
        if not table_ref:
            continue

        for column in table_info[3] or []:
            if sampled_columns >= column_limit:
                break
            if not isinstance(column, dict) or not _is_value_sample_candidate(column):
                continue
            column_ref = _safe_column_ref(column.get("columnName") or column.get("name"))
            if not column_ref:
                continue

            sample_sqls = []
            for term in search_terms:
                safe_term = term.replace("'", "''")
                sample_sqls.append(
                    f"SELECT DISTINCT {column_ref} AS sample_value "
                    f"FROM {table_ref} "
                    f"WHERE {column_ref} IS NOT NULL "
                    f"AND LOWER({column_ref}) LIKE LOWER('%{safe_term}%') "
                    f"LIMIT {value_limit}"
                )
                if len(sample_sqls) >= max(0, max_sqls_per_column - 1):
                    break
            if fallback_enabled and not sample_sqls:
                sample_sqls.append(
                    f"SELECT DISTINCT {column_ref} AS sample_value "
                    f"FROM {table_ref} "
                    f"WHERE {column_ref} IS NOT NULL "
                    f"LIMIT {value_limit}"
                )
            if not sample_sqls:
                continue
            sample_sqls = sample_sqls[:max_sqls_per_column]
            rows = []
            try:
                for sample_sql in sample_sqls:
                    rows.extend(await asyncio.to_thread(
                        execute_with_cache,
                        lambda sql: loader_class.execute_sql_query(sql, db_url),
                        sample_sql,
                        db_url=db_url,
                        db_type=db_type,
                    ))
                    if len(_extract_sample_values(rows, value_limit)) >= value_limit:
                        break
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.info(
                    "Value sampling skipped: table=%s column=%s error=%s",
                    sanitize_log_input(table_ref),
                    sanitize_log_input(column_ref),
                    sanitize_log_input(str(exc))[:240],
                )
                continue

            values = _extract_sample_values(rows, value_limit)
            sampled_columns += 1
            if not values:
                continue

            _merge_column_sample_values(column, values, value_limit)
            logging.info(
                "Value sampling added: table=%s column=%s values=%s",
                sanitize_log_input(table_ref),
                sanitize_log_input(column_ref),
                sanitize_log_input(", ".join(values))[:240],
            )

    return await _enrich_tables_with_temporal_profile(
        tables, loader_class, db_url, db_type
    )


# Date / numeric column profiling --------------------------------------------
# The declared `nullable` flag is supplier metadata and often says a column CAN
# be NULL while the actual data never is. For temporal wording the model needs
# the DATA truth, not the declaration: an execution/close date that is never
# NULL means «ещё не наступило / не закрыто на D» = (col > D), never `col IS
# NULL` (which returns nothing). We probe one bounded aggregate per table and
# attach a compact, schema-agnostic fact to each date/numeric column so the
# planner SEES it. No hardcoded table/column names.
_PROFILE_DATE_RE = re.compile(r"date|timestamp", re.IGNORECASE)
_PROFILE_NUM_RE = re.compile(
    r"int|double|decimal|float|numeric|real|bigint", re.IGNORECASE
)
_PROFILE_SKIP_NAME_RE = re.compile(r"(id|guid|uuid|hash|key)$", re.IGNORECASE)


def _is_profile_candidate(column: dict) -> bool:
    name = str(column.get("columnName") or column.get("name") or "").lower()
    typ = str(column.get("dataType") or column.get("type") or "").lower()
    if not name or not typ or _PROFILE_SKIP_NAME_RE.search(name):
        return False
    return bool(_PROFILE_DATE_RE.search(typ) or _PROFILE_NUM_RE.search(typ))


def _format_data_profile(n: int, nn: int, mn, mx) -> str:
    """Compact RU data fact: NULL-ness (ground truth) + observed range."""
    if not n:
        return ""
    nulls = n - nn
    if nn == 0:
        nullfact = "всегда NULL"
    elif nulls <= 0:
        nullfact = "никогда не NULL (всегда заполнено)"
    else:
        pct = round(nulls / n * 100)
        nullfact = f"~{pct}% NULL" if pct >= 1 else "<1% NULL"
    parts = [nullfact]
    smn, smx = str(mn).strip(), str(mx).strip()
    if smn and smx and smn.lower() != "none" and smx.lower() != "none":
        rng = f"{smn}…{smx}" if smn != smx else smn
        parts.append(f"диапазон {rng}")
    return "; ".join(parts)


async def _enrich_columns_with_data_profile(
    tables: list, loader_class, db_url: str, db_type: str,
) -> list:
    """Attach a data-grounded NULL-ness + range fact to date/numeric columns.

    One bounded aggregate per table (cached per db_url+sql), so repeated
    questions reuse profiles. Schema-agnostic; never raises."""
    if os.getenv("QW_COLUMN_PROFILE_ENABLED", "true").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return tables
    if not tables or not loader_class or not db_url:
        return tables
    if db_type not in {"impala", "postgresql", "postgres", "mysql", "snowflake"}:
        return tables
    cap = int(getattr(Config, "QW_COLUMN_PROFILE_SAMPLE", 200000) or 200000)
    # Cover EVERY candidate table/column so validity dates, measures and codes in
    # low-ranked FK-target tables also get samples + profile. Cached per table.
    max_tables = int(getattr(Config, "QW_COLUMN_PROFILE_MAX_TABLES", 40) or 40)
    max_cols = int(getattr(Config, "QW_COLUMN_PROFILE_MAX_COLS", 60) or 60)
    profiled = 0
    for table_info in tables:
        if profiled >= max_tables:
            break
        if not isinstance(table_info, list) or len(table_info) < 4:
            continue
        table_ref = _safe_table_ref(table_info[0])
        if not table_ref:
            continue
        cands = [
            c for c in (table_info[3] or [])
            if isinstance(c, dict) and _is_profile_candidate(c)
            and not c.get("data_profile")
        ][:max_cols]
        if not cands:
            continue
        # Quote identifiers so reserved-word columns (left, limit, …) parse.
        _q = (lambda r: f"`{r}`") if db_type in {"impala", "mysql"} else (
            lambda r: f'"{r}"')
        selects = ["COUNT(*) AS n"]
        meta = []
        for i, c in enumerate(cands):
            ref = _safe_column_ref(c.get("columnName") or c.get("name"))
            if not ref:
                continue
            qref = _q(ref)
            selects.append(f"COUNT({qref}) AS nn_{i}")
            selects.append(f"MIN({qref}) AS mn_{i}")
            selects.append(f"MAX({qref}) AS mx_{i}")
            meta.append((i, c))
        if not meta:
            continue
        probe_sql = (
            f"SELECT {', '.join(selects)} "
            f"FROM (SELECT * FROM {table_ref} LIMIT {cap}) AS prof_t"
        )
        try:
            rows = await asyncio.to_thread(
                execute_with_cache,
                lambda sql: loader_class.execute_sql_query(sql, db_url),
                probe_sql,
                db_url=db_url,
                db_type=db_type,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.info(
                "Column profile skipped: table=%s error=%s",
                sanitize_log_input(table_ref),
                sanitize_log_input(str(exc))[:200],
            )
            continue
        profiled += 1
        row = rows[0] if rows else None
        if not isinstance(row, dict):
            continue

        def _num(value):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0

        n = _num(row.get("n"))
        for i, c in meta:
            prof = _format_data_profile(
                n, _num(row.get(f"nn_{i}")), row.get(f"mn_{i}"), row.get(f"mx_{i}")
            )
            if prof:
                c["data_profile"] = prof

        # Concrete EXAMPLE VALUES for the same date/numeric columns (a few
        # distinct non-null), so the planner sees real samples — not just the
        # range — exactly as it does for text columns. One extra bounded query.
        try:
            sample_cols = ", ".join(
                _q(_safe_column_ref(c.get("columnName") or c.get("name")))
                for _, c in meta
            )
            srows = await asyncio.to_thread(
                execute_with_cache,
                lambda sql: loader_class.execute_sql_query(sql, db_url),
                f"SELECT {sample_cols} "
                f"FROM (SELECT * FROM {table_ref} LIMIT {cap}) AS prof_s LIMIT 25",
                db_url=db_url,
                db_type=db_type,
            )
        except Exception:  # pylint: disable=broad-exception-caught
            srows = []
        for i, c in meta:
            if c.get("sample_values"):
                continue
            key = _safe_column_ref(c.get("columnName") or c.get("name"))
            seen: list = []
            for r in (srows or []):
                v = r.get(key) if isinstance(r, dict) else None
                if v is None:
                    continue
                sv = str(v)
                if sv not in seen:
                    seen.append(sv)
                if len(seen) >= 5:
                    break
            if seen:
                c["sample_values"] = seen
    return tables


_CLARIFICATION_REQUEST_RE = re.compile(
    r"("
    r"уточните|уточнить|нужно\s+уточн|неоднознач|несколько\s+сопоставимых|"
    r"sql\s+не\s+возвращ|missing\s+information|ambiguities|ambiguous|"
    r"clarify|please\s+specify|which\s+(source|table|object|type)"
    r")",
    re.IGNORECASE,
)
_SQL_START_RE = re.compile(
    r"^\s*(select|with|insert|update|delete|merge|create|alter|drop)\b",
    re.IGNORECASE,
)
_SCHEMA_EXECUTION_ERROR_RE = re.compile(
    # Identifier-resolution failures only. Impala prefixes EVERY error with
    # "AnalysisException", so matching that word would route shape errors
    # (ORDER BY/GROUP BY, casts) away from the healer that can fix them.
    r"(column .*does not exist|relation .*does not exist|table .*does not exist|"
    r"no such column|no such table|unknown column|unknown table|"
    r"operator does not exist|ambiguous column|invalid identifier|"
    r"could not resolve (column|field|table) reference|"
    r"could not resolve column)",
    re.IGNORECASE,
)
_SQL_TABLE_REF_RE = re.compile(
    r"\b(?:from|join)\s+([`\"A-Za-z_][`\"A-Za-z0-9_.]*)",
    re.IGNORECASE,
)
_EXPLICIT_PERIOD_RE = re.compile(
    r"("
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{8}\b|\b\d{1,2}[.]\d{1,2}[.]\d{4}\b|"
    r"\b(?:19|20)\d{2}\b|"
    r"\b(today|current|yesterday|last|previous|month|year|quarter|week|period)\b|"
    r"\b(сегодня|текущ|вчера|последн|прошл|месяц|год|квартал|недел|период)\w*"
    r")",
    re.IGNORECASE,
)
def _csv_env(name: str, default: str) -> frozenset[str]:
    """Read a comma-separated, case-insensitive set from the environment.

    Naming-convention heuristics must stay configurable per installation —
    no datamart-specific names belong in engine code.
    """
    raw = os.getenv(name, "").strip() or default
    return frozenset(
        item.strip().lower() for item in raw.split(",") if item.strip()
    )


# Reporting/as-of date detection is DESCRIPTION-DRIVEN by default: a column is
# a reporting/snapshot date because its comment says so (e.g. "Дата отчета",
# "Дата баланса", "report/as-of/snapshot date"), NOT because of a baked-in
# column-name list or a "v_f_" table-name prefix. The name/prefix lists below
# are empty by default and exist only so a specific installation can opt into a
# naming convention via env — the engine ships with no datamart names hardcoded.
_REPORTING_COLUMN_NAMES = _csv_env("QW_REPORTING_DATE_COLUMNS", "")
_FACT_TABLE_PREFIXES = tuple(_csv_env("QW_FACT_TABLE_PREFIXES", ""))
_FACT_TABLE_MARKERS = tuple(_csv_env("QW_FACT_TABLE_MARKERS", ""))
_REPORTING_DESC_RE = re.compile(
    r"(report|as[-\s]?of|snapshot|balance\s+date|effective\s+date|"
    r"отчёт|отчет|срез|баланс|дата,\s*за\s*котор)",
    re.IGNORECASE,
)
_DATE_LIKE_COLUMN_RE = re.compile(r"(^|_)(date|dt)$|(_date|_dt)$", re.IGNORECASE)
_CHANGE_THRESHOLD_RE = re.compile(
    r"\b(change|changes|changed|delta|difference|diff)\b.{0,80}"
    r"\b(greater\s+than|more\s+than|over|above|exceed)\b|"
    r"\b(измен|разниц|дельт)\w*.{0,80}\b(больш|превыш|свыше|более)\w*",
    re.IGNORECASE | re.DOTALL,
)
_AVERAGE_CHANGE_RE = re.compile(
    r"\b(avg|average|mean)\b.{0,80}\b(change|delta|difference|diff)\b|"
    r"\b(средн)\w*.{0,80}\b(измен|разниц|дельт)\w*",
    re.IGNORECASE | re.DOTALL,
)
_EXPLICIT_AVERAGE_THRESHOLD_RE = re.compile(
    r"\b(avg|average|mean)\b.{0,80}\b(greater\s+than|more\s+than|over|above|exceed)\b|"
    r"\b(средн)\w*.{0,80}\b(больш|превыш|свыше|более)\w*",
    re.IGNORECASE | re.DOTALL,
)
_EXPLICIT_EVENT_THRESHOLD_RE = re.compile(
    r"\b(each|individual|per[-\s]?event|row[-\s]?level)\b.{0,80}"
    r"\b(change|delta|difference|diff)\b|"
    r"\b(кажд|отдельн|построчн|событи)\w*.{0,80}\b(измен|разниц|дельт)\w*",
    re.IGNORECASE | re.DOTALL,
)
_RECENT_CLARIFICATION_TTL_SECONDS = 3 * 60 * 60
_RECENT_CLARIFICATION_CACHE: dict[tuple[str, str, str], tuple[str, float]] = {}
_RECENT_CLARIFICATION_MAX_ENTRIES = 512
_RECENT_BASE_QUERY_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_RECENT_BASE_QUERY_MAX_ENTRIES = 512
_QUERY_CORRECTION_MARKER = "User correction for the same request:"
_QUERY_CORRECTION_INSTRUCTION = (
    "Apply the correction to the previous request. Do not treat the "
    "correction as a standalone database question."
)
_CORRECTION_ONLY_RE = re.compile(
    r"("
    r"\b(нет|не\s+надо|не\s+нужно|не\s+включ|не\s+использ|без|исключ|убер|"
    r"добав|использ|возьми|нужно|надо|лучше|вместо|замени|таблиц|колонк)\w*|"
    r"\b(no|not|don't|dont|do\s+not|without|exclude|remove|use|instead|replace|"
    r"add|prefer|table|column)\b"
    r")",
    re.IGNORECASE,
)
_STANDALONE_REQUEST_RE = re.compile(
    r"("
    r"\b(select|show|list|find|count|sum|avg|analy[sz]e|display)\b|"
    r"\b(найд|вывед|покаж|посчита|сколько|сумм|проанализ|постро)\w*"
    r")",
    re.IGNORECASE,
)


def _normalize_clarification_cache_text(value: str) -> str:
    normalized = str(value or "").lower()
    normalized = re.sub(r"[^\w\s]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


def _clarification_cache_tokens(value: str) -> set[str]:
    return {
        token for token in _normalize_clarification_cache_text(value).split()
        if len(token) >= 3
    }


def _similar_clarification_question(left: str, right: str) -> bool:
    if left == right:
        return True
    left_tokens = _clarification_cache_tokens(left)
    right_tokens = _clarification_cache_tokens(right)
    if len(left_tokens) < 5 or len(right_tokens) < 5:
        return False
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    if union and intersection / union >= 0.88:
        return True
    smaller = min(len(left_tokens), len(right_tokens))
    larger = max(len(left_tokens), len(right_tokens))
    return (
        smaller >= 8
        and intersection / smaller >= 0.92
        and larger / smaller <= 1.25
    )


def _recent_clarification_key(
    user_id: str,
    graph_id: str,
    question: str,
) -> tuple[str, str, str]:
    return (
        str(user_id or ""),
        str(graph_id or ""),
        _normalize_clarification_cache_text(question),
    )


def _remember_recent_clarification(
    user_id: str,
    graph_id: str,
    question: str,
    clarification: str,
) -> None:
    normalized_question = _normalize_clarification_cache_text(question)
    normalized_clarification = " ".join(str(clarification or "").split())
    if not normalized_question or not normalized_clarification:
        return
    expires_at = time.time() + _RECENT_CLARIFICATION_TTL_SECONDS
    _RECENT_CLARIFICATION_CACHE[
        _recent_clarification_key(user_id, graph_id, question)
    ] = (normalized_clarification, expires_at)
    while len(_RECENT_CLARIFICATION_CACHE) > _RECENT_CLARIFICATION_MAX_ENTRIES:
        oldest_key = min(
            _RECENT_CLARIFICATION_CACHE,
            key=lambda key: _RECENT_CLARIFICATION_CACHE[key][1],
        )
        _RECENT_CLARIFICATION_CACHE.pop(oldest_key, None)


def _recent_clarification_for(
    user_id: str,
    graph_id: str,
    question: str,
) -> str:
    now = time.time()
    expired_keys = [
        key for key, (_value, expires_at) in _RECENT_CLARIFICATION_CACHE.items()
        if expires_at <= now
    ]
    for key in expired_keys:
        _RECENT_CLARIFICATION_CACHE.pop(key, None)
    cached = _RECENT_CLARIFICATION_CACHE.get(
        _recent_clarification_key(user_id, graph_id, question)
    )
    if not cached:
        normalized_question = _normalize_clarification_cache_text(question)
        for (cached_user, cached_graph, cached_question), (
            value,
            expires_at,
        ) in _RECENT_CLARIFICATION_CACHE.items():
            if cached_user != str(user_id or "") or cached_graph != str(graph_id or ""):
                continue
            if expires_at <= now:
                continue
            if _similar_clarification_question(normalized_question, cached_question):
                logging.info(
                    "Recent clarification fuzzy cache hit: graph=%s question=%s",
                    sanitize_log_input(graph_id),
                    sanitize_log_input(cached_question)[:160],
                )
                return value
        return ""
    value, expires_at = cached
    if expires_at <= now:
        _RECENT_CLARIFICATION_CACHE.pop(
            _recent_clarification_key(user_id, graph_id, question),
            None,
        )
        return ""
    return value


def _recent_base_query_key(user_id: str, graph_id: str) -> tuple[str, str]:
    return (str(user_id or ""), str(graph_id or ""))


def _remember_recent_base_query(user_id: str, graph_id: str, question: str) -> None:
    normalized_question = " ".join(str(question or "").split())
    if not normalized_question:
        return
    _RECENT_BASE_QUERY_CACHE[
        _recent_base_query_key(user_id, graph_id)
    ] = (normalized_question, time.time() + _RECENT_CLARIFICATION_TTL_SECONDS)
    while len(_RECENT_BASE_QUERY_CACHE) > _RECENT_BASE_QUERY_MAX_ENTRIES:
        oldest_key = min(
            _RECENT_BASE_QUERY_CACHE,
            key=lambda key: _RECENT_BASE_QUERY_CACHE[key][1],
        )
        _RECENT_BASE_QUERY_CACHE.pop(oldest_key, None)


def _recent_base_query_for(user_id: str, graph_id: str) -> str:
    now = time.time()
    expired_keys = [
        key for key, (_value, expires_at) in _RECENT_BASE_QUERY_CACHE.items()
        if expires_at <= now
    ]
    for key in expired_keys:
        _RECENT_BASE_QUERY_CACHE.pop(key, None)
    cached = _RECENT_BASE_QUERY_CACHE.get(_recent_base_query_key(user_id, graph_id))
    if not cached:
        return ""
    value, expires_at = cached
    if expires_at <= now:
        _RECENT_BASE_QUERY_CACHE.pop(_recent_base_query_key(user_id, graph_id), None)
        return ""
    return value


def _looks_like_query_correction(current: str, previous_query: str | None = None) -> bool:
    text = str(current or "").strip()
    if not text or _SQL_START_RE.match(text):
        return False
    if len(text) > 700:
        return False
    if not _CORRECTION_ONLY_RE.search(text):
        return False
    token_count = len(re.findall(r"\S+", text))
    if token_count <= 20:
        return True
    if previous_query and len(text) <= max(180, int(len(str(previous_query)) * 0.50)):
        return True
    return not ("?" in text and _STANDALONE_REQUEST_RE.search(text))


def _merge_query_correction(previous_query: str, correction: str) -> str:
    normalized_previous = _normalize_clarification_cache_text(previous_query)
    normalized_correction = _normalize_clarification_cache_text(correction)
    if normalized_correction and normalized_correction in normalized_previous:
        return str(previous_query or "").strip()
    return (
        f"{previous_query}\n\n"
        f"{_QUERY_CORRECTION_MARKER} {correction}\n"
        f"{_QUERY_CORRECTION_INSTRUCTION}"
    )


def _resolve_query_correction(
    queries_history: list[str],
    user_id: str,
    graph_id: str,
) -> tuple[list[str], bool]:
    if not queries_history:
        return queries_history, False

    current = queries_history[-1]
    previous_query = queries_history[-2] if len(queries_history) >= 2 else ""
    if previous_query and _looks_like_query_correction(current, previous_query):
        merged_history = list(queries_history)
        merged_history[-1] = _merge_query_correction(previous_query, current)
        logging.info(
            "Resolved query correction from chat history: previous_query_chars=%d "
            "correction=%s",
            len(str(previous_query or "")),
            sanitize_log_input(current)[:180],
        )
        return merged_history, True

    # Cross-request "recent query cache" correction. This pulls a query cached
    # per (user_id, graph_id) — NOT per question — so a brand-new standalone
    # question whose wording trips _looks_like_query_correction would be merged
    # with whatever the SAME user last asked on the SAME graph. In a single-user
    # contour (and in a benchmark) every independent question would inherit the
    # previous one. Disabled by default; opt in only for a truly conversational
    # contour. The in-request chat-history correction above is unaffected.
    if (
        len(queries_history) == 1
        and os.getenv("QW_RECENT_QUERY_CORRECTION_ENABLED", "").strip().lower()
        in {"1", "true", "yes", "on"}
        and _looks_like_query_correction(current)
    ):
        cached_query = _recent_base_query_for(user_id, graph_id)
        if cached_query:
            merged_history = list(queries_history)
            merged_history[-1] = _merge_query_correction(cached_query, current)
            logging.info(
                "Resolved query correction from recent query cache: "
                "cached_query_chars=%d correction=%s",
                len(cached_query),
                sanitize_log_input(current)[:180],
            )
            return merged_history, True

    return queries_history, False


def _assistant_requested_clarification(message: str | None) -> bool:
    """Detect that the previous assistant turn asked the user to disambiguate."""
    return bool(_CLARIFICATION_REQUEST_RE.search(str(message or "")))


def _looks_like_short_clarification(current: str, previous_query: str) -> bool:
    """Return true when the current user turn is likely a clarification answer."""
    text = str(current or "").strip()
    if not text or _SQL_START_RE.match(text):
        return False
    if "?" in text and len(text) > 120:
        return False
    token_count = len(re.findall(r"\S+", text))
    if token_count <= 8 and len(text) <= 100:
        return True
    if token_count <= 48 and len(text) <= 420:
        return True
    return (
        token_count <= 24
        and len(text) <= 220
        and len(text) <= max(100, int(len(str(previous_query or "")) * 0.45))
    )


def _resolve_followup_clarification(
    queries_history: list[str],
    result_history: list[str] | None,
) -> list[str]:
    """Merge a short clarification answer with the unresolved previous request."""
    if len(queries_history or []) < 2 or not result_history:
        return queries_history

    previous_answer = result_history[-1] if result_history else ""
    if not _assistant_requested_clarification(previous_answer):
        return queries_history

    current = queries_history[-1]
    previous_query = queries_history[-2]
    if not _looks_like_short_clarification(current, previous_query):
        return queries_history

    merged_history = list(queries_history)
    merged_history[-1] = (
        f"{previous_query}\n\n"
        f"User clarification for the unresolved request: {current}"
    )
    logging.info(
        "Resolved follow-up clarification: previous_query_chars=%d "
        "clarification=%s",
        len(str(previous_query or "")),
        sanitize_log_input(current)[:160],
    )
    return merged_history


def _apply_recent_clarification(
    queries_history: list[str],
    user_id: str,
    graph_id: str,
) -> list[str]:
    if len(queries_history or []) != 1:
        return queries_history
    current = queries_history[-1]
    clarification = _recent_clarification_for(user_id, graph_id, current)
    if not clarification:
        return queries_history
    merged_history = list(queries_history)
    merged_history[-1] = (
        f"{current}\n\n"
        f"User clarification restored from a recent resolved conversation: "
        f"{clarification}"
    )
    logging.info(
        "Applied recent clarification cache: graph=%s clarification=%s",
        sanitize_log_input(graph_id),
        sanitize_log_input(clarification)[:160],
    )
    return merged_history


def _is_schema_execution_error(error_message: str) -> bool:
    return bool(_SCHEMA_EXECUTION_ERROR_RE.search(str(error_message or "")))


def _needs_change_threshold_clarification(user_query: str) -> bool:
    """Do not pre-emptively ask about ordinary threshold + average wording.

    In SQL semantics, "find objects where a change exceeds N and output the
    average change" means: filter qualifying change-event rows first, then
    aggregate the requested average. Asking the user here made clear requests
    look ambiguous. The analysis agent still validates that generated SQL keeps
    the change-event grain and does not move event thresholds to HAVING AVG(...)
    unless the user explicitly asks for groups whose average exceeds a threshold.
    """
    return False


def _change_threshold_clarification_message() -> str:
    return (
        "Уточните, как применять порог для изменения: отбирать только "
        "отдельные события/строки изменения, где модуль изменения превышает "
        "порог, и затем считать среднее по ним, или отбирать сделки/группы, "
        "у которых уже среднее изменение превышает порог? Ответ сохраню и "
        "буду использовать для аналогичных запросов."
    )


def _clean_sql_identifier(value: str) -> str:
    return str(value or "").strip().strip("`\"").lower()


def _table_ref_variants(table_name: str) -> set[str]:
    cleaned = _clean_sql_identifier(table_name)
    parts = [part for part in cleaned.split(".") if part]
    variants = {cleaned}
    if parts:
        variants.add(parts[-1])
    if len(parts) >= 2:
        variants.add(".".join(parts[-2:]))
    return {variant for variant in variants if variant}


def _sql_table_refs(sql_query: str) -> set[str]:
    refs: set[str] = set()
    for match in _SQL_TABLE_REF_RE.finditer(sql_query or ""):
        refs.update(_table_ref_variants(match.group(1)))
    return refs


def _column_name_from_context(column: dict) -> str:
    return str(column.get("columnName") or column.get("name") or "").lower()


def _column_description_from_context(column: dict) -> str:
    return str(column.get("description") or column.get("comment") or "")


def _is_reporting_or_period_column(column: dict, table_name: str) -> bool:
    column_name = _column_name_from_context(column)
    description = _column_description_from_context(column)
    if column_name in _REPORTING_COLUMN_NAMES:
        return True
    if _REPORTING_DESC_RE.search(description):
        return True
    table_base = table_name.lower().split(".")[-1]
    fact_like = (
        any(table_base.startswith(prefix) for prefix in _FACT_TABLE_PREFIXES)
        or any(marker in table_base for marker in _FACT_TABLE_MARKERS)
    )
    return bool(fact_like and _DATE_LIKE_COLUMN_RE.search(column_name))


def _question_has_explicit_period(user_query: str) -> bool:
    return bool(_EXPLICIT_PERIOD_RE.search(user_query or ""))


def _period_required_tables(sql_query: str, tables: list) -> list[str]:
    used_refs = _sql_table_refs(sql_query)
    if not used_refs:
        return []
    required: list[str] = []
    for table_info in tables or []:
        if not isinstance(table_info, list) or len(table_info) < 4:
            continue
        table_name = str(table_info[0] or "")
        if not (_table_ref_variants(table_name) & used_refs):
            continue
        columns = table_info[3] or []
        if any(
            isinstance(column, dict)
            and _is_reporting_or_period_column(column, table_name)
            for column in columns
        ):
            required.append(table_name)
    return required


def _decisive_retry_enabled() -> bool:
    return os.getenv(
        "QW_DECISIVE_RETRY_ENABLED", "true"
    ).strip().lower() not in {"0", "false", "no", "off"}


def _period_clarification_enabled() -> bool:
    return os.getenv(
        "QW_PERIOD_CLARIFICATION_ENABLED", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}


_DATE_CONSTRAINT_RE = re.compile(
    r"\b(current_date|current_timestamp|now|today|getdate|sysdate|max\s*\(|"
    r"between|date\s*'|date\s*\"|interval|dateadd|date_sub|date_add|"
    r"_date\b|_dt\b)",
    re.IGNORECASE,
)


def _sql_has_date_constraint(sql_query: str) -> bool:
    """True when the SQL already pins a date/period (literal, function, or range).

    Used to suppress the pre-emptive period-clarification: if the model already
    constrained time, asking the user for a date is a false refusal.
    """
    scrubbed = _scrub_sql_literals_and_comments_keep_dates(sql_query)
    return bool(_DATE_CONSTRAINT_RE.search(scrubbed))


def _scrub_sql_literals_and_comments_keep_dates(sql_query: str) -> str:
    """Strip comments but keep the raw SQL so date literals/functions show."""
    text = sql_query or ""
    # only strip line/block comments; keep string literals so DATE '...' is seen
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return text


def _missing_period_clarification(sql_query: str, user_query: str, tables: list) -> str | None:
    if _question_has_explicit_period(user_query):
        return None
    period_tables = _period_required_tables(sql_query, tables)
    if not period_tables:
        return None
    shown_tables = ", ".join(period_tables[:4])
    if len(period_tables) > 4:
        shown_tables += f" и еще {len(period_tables) - 4}"
    return (
        "Уточните, пожалуйста, отчетную дату или период для запроса. "
        f"Используемые таблицы ({shown_tables}) являются fact/snapshot или "
        "имеют отчетную/периодную дату, поэтому их нельзя агрегировать по всем "
        "датам по умолчанию. Если нужен текущий срез, напишите \"на сегодня\" "
        "или \"текущий отчетный день\"."
    )


def _schema_reanalysis_query(user_query: str, failed_sql: str, error_message: str) -> str:
    return (
        f"{user_query}\n\n"
        "Previous generated SQL failed with a schema/identifier/type error. "
        "Re-run table and column selection from the graph/RAG context instead "
        "of locally healing or guessing column names.\n"
        f"Failed SQL:\n{failed_sql}\n"
        f"Execution error:\n{error_message}\n"
        "Use only real tables/columns and declared relationships from the graph. "
        "If the correct source is still ambiguous or missing, ask for "
        "clarification and return no SQL."
    )


async def _graph_schema_allowlist(namespaced: str, db=None) -> list:
    """Return the FULL graph schema as gate table_info lists.

    The allowlist for the deterministic gate must cover EVERY table/column that
    exists in the database graph, not only the ~16 candidates the table-finder
    surfaced for the prompt. Otherwise the gate falsely rejects a real table the
    model used but recall left out of context. Hallucinations (names absent from
    the whole graph) are still caught. One cheap Cypher read, no LLM.
    """
    try:
        graph = resolve_db(db).select_graph(namespaced)
        result = await graph.query(
            "MATCH (c:Column)-[:BELONGS_TO]->(t:Table) "
            "RETURN t.name AS table, "
            "collect({name: c.name, type: c.type, key: c.key_type, "
            "desc: c.description}) AS columns"
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning(
            "Gate full-schema allowlist unavailable for %s: %s",
            sanitize_log_input(namespaced),
            sanitize_log_input(str(exc))[:200],
        )
        return []
    allowlist = []
    for row in result.result_set or []:
        table_name = row[0]
        columns = []
        for item in row[1] or []:
            if isinstance(item, dict) and item.get("name"):
                columns.append({
                    "columnName": item.get("name"),
                    "dataType": item.get("type") or "",
                    "keyType": item.get("key") or "",
                    "description": item.get("desc") or "",
                })
        allowlist.append([table_name, "", {}, columns])
    return allowlist


# Generic + common business-entity SUBJECT nouns. These identify the entity, not
# the METRIC, so they must not by themselves match a linker output to a resolved
# concept (e.g. "driver name" must NOT match the concept "Driver Age"). Reference
# data, not a per-query/DB hardcode.
# Only GENERIC, domain-independent words. The ENTITY nouns (driver, account,
# patient, ...) are NOT hardcoded — they are derived per-query from the schema's
# own table names, so this works for any database without per-DB literals.
_RECONCILE_STOPWORDS = {
    "the", "of", "a", "an", "and", "or", "per", "each", "all", "their", "its",
    "to", "by", "for", "with", "average", "avg", "mean", "total", "number",
    "count", "sum", "list", "show", "give", "value", "values", "overall",
    "score", "amount", "result", "results", "name", "names", "id", "ids",
}


def _entity_stopwords(tables: list) -> set:
    """Entity tokens derived from the candidate TABLE NAMES (singular+plural), so
    a subject noun like 'driver'/'account'/'patient' is non-distinctive for
    concept matching — general, schema-driven, no hardcoded domain words."""
    out: set = set()
    for t in tables or []:
        if not isinstance(t, (list, tuple)) or not t or not t[0]:
            continue
        for tok in re.findall(r"[a-z]+", str(t[0]).lower()):
            if len(tok) > 2:
                out.add(tok)
                out.add(tok[:-1] if tok.endswith("s") else tok + "s")
    return out


def _reconcile_tokens(text: str, extra_stop: set | None = None) -> set:
    stop = _RECONCILE_STOPWORDS | (extra_stop or set())
    return {w for w in re.findall(r"[a-z]+", (text or "").lower())
            if len(w) > 2 and w not in stop}


def _stage_debug_on() -> bool:
    """Read the toggle LIVE so the Settings switch takes effect without restart
    (apply_runtime_overrides writes the chosen value to os.environ)."""
    return os.getenv("STAGE_DEBUG", "true").strip().lower() in {
        "1", "true", "yes", "on"}


def _stage_log(label: str, content) -> None:
    """Log a pipeline stage's INPUT/OUTPUT verbatim (truncated) so the per-agent
    context flow is inspectable in the in-app debug panel (/settings/debug-logs).
    Toggled by the STAGE_DEBUG setting (Settings page), read live per call."""
    if not _stage_debug_on():
        return
    s = content if isinstance(content, str) else str(content)
    try:
        _cap = int(os.getenv("STAGE_LOG_MAXLEN", "3000"))
    except ValueError:
        _cap = 3000
    logging.info("STAGE %s [%d chars]\n%s", label, len(s),
                 s[:_cap] + (" …[truncated]" if len(s) > _cap else ""))


def _embed_rank_concepts(query: str, concepts: list, top_k: int) -> list:
    """Re-rank token-matched KB concepts by EMBEDDING similarity to the question and
    keep the top_k. detect_concepts over-recalls on common words (a question about how
    well something "performs on average in sprint" token-matches 'Average Stops Per
    Car', 'Points Finish', 'Sprint Session'…); semantic ranking surfaces the concept
    that actually fits the intent (the 'Sprint Performance Index') and drops the
    look-alikes, so every downstream agent gets a SMALL, SUFFICIENT concept set rather
    than a diluting dump that pins distractor columns into the resolver schema.
    Graph-backed (same embedding model the KB is indexed with); falls back to the
    token order on any failure. General — no DB/column/question specifics."""
    if not query or len(concepts) <= top_k:
        return concepts
    try:
        import math
        texts = [query] + [f"{n}. {d}" for n, d in concepts]
        vecs = Config.EMBEDDING_MODEL.embed(texts)
        qv = vecs[0]

        def _cos(v) -> float:
            dot = sum(a * b for a, b in zip(qv, v))
            na = math.sqrt(sum(a * a for a in qv))
            nb = math.sqrt(sum(b * b for b in v))
            return dot / (na * nb) if na and nb else 0.0

        order = sorted(range(len(concepts)),
                       key=lambda i: _cos(vecs[i + 1]), reverse=True)
        ranked = [concepts[i] for i in order[:top_k]]
        logging.info("Concept embed-rank: %d -> %d kept=%s",
                     len(concepts), len(ranked), [n for n, _ in ranked])
        return ranked
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("Concept embed-rank failed (%s); token order", str(exc)[:120])
        return concepts[:top_k]


def _parse_concept_chunks(chunks: list) -> list:
    """Parse embedding-retrieved per-concept :Knowledge chunks into ``[(name, def)]``.

    Each chunk is one ``## Title`` section + body (the per-concept node format).
    Returns the same shape as ``detect_concepts`` so the rest of the pipeline is
    unchanged. Deduped by name, order preserved (similarity order)."""
    out: list = []
    seen: set = set()
    for ch in chunks or []:
        ch = (ch or "").strip()
        if not ch:
            continue
        title, body_start = "", 0
        lines = ch.splitlines()
        for i, ln in enumerate(lines):
            m = re.match(r"^\s*#{1,6}\s+(.+?)\s*$", ln)
            if m:
                title, body_start = m.group(1).strip(), i + 1
                break
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((title, "\n".join(lines[body_start:]).strip()))
    return out


def _schema_brief(tables: list) -> str:
    """Compact 'table: col1, col2, ...' lines for stage logging."""
    out = []
    for t in tables or []:
        if isinstance(t, (list, tuple)) and t and t[0]:
            cols = ", ".join(str(c.get("columnName") or c.get("name") or "")
                             for c in (t[3] if len(t) >= 4 and t[3] else [])
                             if isinstance(c, dict))
            out.append(f"{t[0]}: {cols}")
    return "\n".join(out)


def _resolved_known_columns(tables: list) -> set:
    """Lowercased set of real column names across the candidate tables."""
    cols: set = set()
    for t in tables or []:
        if not isinstance(t, (list, tuple)) or len(t) < 4 or not t[3]:
            continue
        for c in t[3]:
            if isinstance(c, dict):
                n = c.get("columnName") or c.get("name")
                if n:
                    cols.add(str(n).lower())
    return cols


def _has_nested_aggregate(tree) -> bool:
    """True if an aggregate function nests another aggregate in its argument
    (e.g. ``SUM(POWER(x - SUM(x)/COUNT(x), 2))``) — always invalid SQL."""
    try:
        from sqlglot import exp
        for agg in tree.find_all(exp.AggFunc):
            for sub in agg.find_all(exp.AggFunc):
                if sub is not agg:
                    return True
    except Exception:  # pragma: no cover
        return False
    return False


_DATEISH_RE = re.compile(r"(date|time|birth|dob|deadline|timestamp|\bday\b)", re.IGNORECASE)


def _prune_empty_json_leaves(json_paths: dict, tables: list,
                             loader_class, db_url: str) -> dict:
    """Drop JSON leaf paths that are (almost) always NULL in the actual data.

    A weak binder picks a leaf by NAME match — e.g. for "sprint winners at the time
    they won" it grabs ``event_schedule->'sessions'->'sprint'->>'date'`` (the
    semantically-perfect "sprint date") which is NULL in ~99% of rows, while the real
    populated date lives in a sibling (``date_set``) → query returns 0 rows. The
    metadata describes BOTH as dates, so the model cannot know which is populated.
    Here we ask the DATA (graph/DB) and remove leaves that are essentially empty, so
    the binder is only offered fields it can actually use. Failure-tolerant: any error
    keeps all leaves (no regression); never drops the last surviving leaf of a column.
    No metadata edit, no per-DB hardcode (the JSON ops are the dialect's standard)."""
    if not json_paths or not loader_class or not db_url:
        return json_paths
    col2tbl: dict = {}
    for t in tables or []:
        if not (isinstance(t, (list, tuple)) and len(t) >= 4 and t[3]):
            continue
        tn = str(t[0] or "")
        for c in t[3]:
            if not isinstance(c, dict):
                continue
            cn = str(c.get("columnName") or c.get("name") or "").lower()
            if cn and cn not in col2tbl:
                col2tbl[cn] = tn
    out = dict(json_paths)
    for col, info in list(json_paths.items()):
        tname = col2tbl.get(str(col).lower())
        paths = list((info or {}).get("full") or [])
        if not tname or len(paths) < 2:
            continue
        exprs = []
        for p in paths:
            parts = [str(k) for k in p if str(k) != ""]
            if not parts:
                continue
            e = f'"{col}"' + "".join(f"->'{k}'" for k in parts[:-1]) + f"->>'{parts[-1]}'"
            exprs.append((tuple(parts), e))
        if len(exprs) < 2:
            continue
        sql = ("SELECT " + ", ".join(f"count({e})" for _, e in exprs)
               + f', count(*) FROM "{tname}"')
        try:
            rows = loader_class.execute_sql_query(sql, db_url)
            vals = list(rows[0]) if rows else []
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        if len(vals) != len(exprs) + 1:
            continue
        total = vals[-1] or 0
        if total < 50:
            continue
        keep, dropped = set(), []
        for (parts, _), cnt in zip(exprs, vals[:-1]):
            (keep.add(parts) if (cnt or 0) / total >= 0.02 else dropped.append(".".join(parts)))
        if dropped and keep:
            ni = dict(info)
            ni["full"] = keep
            if isinstance(info.get("leaves"), dict):
                ni["leaves"] = {k: v for k, v in info["leaves"].items()
                                if tuple(v) in keep}
            out[col] = ni
            logging.info("JSON-leaf prune %s.%s: dropped near-empty %s",
                         tname, col, dropped[:6])
    return out


def _json_leaf_index(tables: list, json_paths: dict) -> dict:
    """table_lower -> list of (leaf_lower, json_column_name, path_tuple) for every
    JSON leaf reachable on that table — used to repair a resolver's phantom flat
    column to the real JSON path it meant."""
    idx: dict = {}
    for t in tables or []:
        if not isinstance(t, (list, tuple)) or len(t) < 4 or not t[3]:
            continue
        tn = str(t[0]).lower()
        for c in t[3]:
            if not isinstance(c, dict):
                continue
            cn = c.get("columnName") or c.get("name") or ""
            info = (json_paths or {}).get(str(cn).lower())
            if not info:
                continue
            for leaf, path in (info.get("leaves") or {}).items():
                tup = tuple(path) if isinstance(path, (list, tuple)) else (leaf,)
                idx.setdefault(tn, []).append((str(leaf).lower(), cn, tup))
    return idx


def _find_json_leaf(idx: dict, table: str, name: str):
    """Best JSON leaf for a phantom (table, name): exact name, else substring,
    shortest leaf. Falls back to all tables if the qualifier has no JSON cols."""
    name = (name or "").lower()
    cands = idx.get((table or "").lower()) or [x for v in idx.values() for x in v]
    exact = [x for x in cands if x[0] == name]
    if exact:
        return exact[0]
    sub = sorted((x for x in cands if name and (name in x[0] or x[0] in name)),
                 key=lambda x: len(x[0]))
    return sub[0] if sub else None


def _json_path_expr(table: str, json_col: str, path: tuple, dateish: bool) -> str:
    expr = f"{table}.{json_col}"
    for k in path[:-1]:
        expr += f"->'{k}'"
    expr += f"->>'{path[-1]}'"
    return f"({expr})::date" if dateish else expr


def _repair_phantom_columns(expr_sql: str, tables: list, json_paths: dict,
                            known: set, db_type: str | None):
    """Rewrite a resolver formula's PHANTOM flat columns to the real JSON leaf
    they meant (``races.date`` -> ``(races.event_schedule->>'date_set')::date``),
    deterministically via sqlglot. Returns the repaired SQL if EVERY phantom was
    grounded, else None (caller then drops the formula). Date-ish leaves get a
    ``::date`` cast so date arithmetic still works."""
    if not expr_sql or not json_paths:
        return None
    try:
        import sqlglot
        from sqlglot import exp
        from api.sql_utils.sql_gate import sqlglot_dialect
    except Exception:  # pragma: no cover
        return None
    dialect = sqlglot_dialect(db_type)
    try:
        tree = sqlglot.parse_one(expr_sql, read=dialect)
    except Exception:
        return None
    if tree is None:
        return None
    idx = _json_leaf_index(tables, json_paths)
    if not idx:
        return None
    col_owner = {}
    for t in tables or []:
        if isinstance(t, (list, tuple)) and len(t) >= 4 and t[3]:
            for c in t[3]:
                if isinstance(c, dict):
                    nm = str(c.get("columnName") or c.get("name") or "").lower()
                    if nm:
                        col_owner.setdefault(nm, str(t[0]).lower())
    repaired_any = False
    for col in list(tree.find_all(exp.Column)):
        nm = (col.name or "").lower()
        if not nm or nm in known:
            continue
        leaf = _find_json_leaf(idx, (col.table or ""), nm)
        if not leaf:
            return None  # a phantom we cannot ground -> give up (drop)
        leaf_name, json_col, path = leaf
        owner = (col.table or "").lower() or col_owner.get(json_col.lower(), "")
        if not owner:
            return None
        dateish = bool(_DATEISH_RE.search(nm) or _DATEISH_RE.search(leaf_name))
        try:
            new_node = sqlglot.parse_one(
                _json_path_expr(owner, json_col, path, dateish), read=dialect)
            col.replace(new_node)
            repaired_any = True
        except Exception:
            return None
    return tree.sql(dialect=dialect) if repaired_any else None


def _has_identity_stub_factor(tree) -> bool:
    """True if the expression multiplies/divides a column-bearing term by a PURE
    CONSTANT sub-expression that evaluates to 1 — an identity stub. The resolver
    emits this when it cannot bind one factor of a composite metric and fills it
    with a placeholder (e.g. ``score * (100.0/100)`` where the reliability factor
    was stubbed to 1): the formula has silently dropped a real term, so it is the
    WRONG value and must not be copied. Deterministic, general, names nothing —
    pure numeric-constant evaluation over the sqlglot AST."""
    try:
        from sqlglot import exp  # pylint: disable=import-outside-toplevel
    except Exception:  # pragma: no cover
        return False

    def _ceval(node):
        # Evaluate a sub-tree built ONLY of numeric literals and + - * / to a
        # float; return None if it references any column (i.e. not a pure const).
        if node is None or isinstance(node, exp.Column):
            return None
        if isinstance(node, exp.Paren):
            return _ceval(node.this)
        if isinstance(node, exp.Neg):
            v = _ceval(node.this)
            return -v if v is not None else None
        if isinstance(node, exp.Literal):
            if node.is_number:
                try:
                    return float(node.name)
                except (ValueError, TypeError):
                    return None
            return None
        if isinstance(node, (exp.Mul, exp.Div, exp.Add, exp.Sub)):
            lv, rv = _ceval(node.left), _ceval(node.right)
            if lv is None or rv is None:
                return None
            if isinstance(node, exp.Mul):
                return lv * rv
            if isinstance(node, exp.Div):
                return lv / rv if rv else None
            if isinstance(node, exp.Add):
                return lv + rv
            return lv - rv
        return None

    for node in list(tree.find_all(exp.Mul)) + list(tree.find_all(exp.Div)):
        for operand in (node.left, node.right):
            val = _ceval(operand)
            if val is not None and abs(val - 1.0) < 1e-9:
                other = node.right if operand is node.left else node.left
                # A bare 1.0 written as 100.0/100 (not a plain "1") next to a
                # column-bearing term is the stub signature; a plain integer 1 in
                # a legitimate place is rarer but still suspicious only when it's
                # an arithmetic constant (Div/Mul), so require non-trivial const.
                if other is not None and list(other.find_all(exp.Column)) \
                        and isinstance(operand, (exp.Paren, exp.Div, exp.Mul)):
                    return True
    return False


def _filter_grounded_resolved(resolved: list, tables: list, db_type: str | None,
                              json_paths: dict | None = None) -> list:
    """Keep only resolved formulas whose column references all EXIST in the schema.

    A resolver can hallucinate a flat column (e.g. ``races.race_date`` when the
    date lives in ``event_schedule`` JSON). Injecting such a formula — and dropping
    the linker's real column for it — turns an executable (if imperfect) query into
    a broken one (L3 -> L1). "Do no harm": an ungrounded formula is dropped so the
    linker's executable column stands. General: grounding is checked via sqlglot
    AST against the real schema columns, no hardcodes.
    """
    known = _resolved_known_columns(tables)
    if not known or not resolved:
        return resolved
    try:
        import sqlglot
        from sqlglot import exp
        from api.sql_utils.sql_gate import sqlglot_dialect
    except Exception:  # pragma: no cover
        return resolved
    dialect = sqlglot_dialect(db_type)
    known_tables = {str(t[0]).lower() for t in (tables or []) if t and t[0]}
    kept: list = []
    for r in resolved:
        expr = (r.get("sql_expression") or "").strip()
        if not expr:
            kept.append(r)  # filter-only concept, nothing to ground
            continue
        tree = None
        try:
            tree = sqlglot.parse_one(expr, read=dialect)
        except Exception:
            tree = None
        if tree is None:
            kept.append(r)  # can't parse -> don't penalize
            continue
        # Phantom TABLE QUALIFIER on a column ref: a resolver-invented table like
        # `race_results.status_id` parses as a Column whose `.table` is the bogus
        # qualifier — `find_all(exp.Table)` (FROM/JOIN only) misses it, and the
        # name check below sees only `status_id` (which may exist elsewhere), so
        # the phantom slips through to the generator → unrunnable join → retry
        # storm. A phantom table can't be repaired (no such table), so drop the
        # formula now and let the generator fall back to an executable column.
        if known_tables:
            qual_bad = sorted({
                (c.table or "").lower() for c in tree.find_all(exp.Column)
                if (c.table or "").lower() and (c.table or "").lower() not in known_tables
            })
            if qual_bad:
                logging.info(
                    "Resolved formula dropped (column on unknown table %s): %s",
                    qual_bad[:3], r.get("name"),
                )
                continue
        phantom = [
            (c.name or "").lower() for c in tree.find_all(exp.Column)
            if (c.name or "").lower() and (c.name or "").lower() not in known
        ]
        if phantom:
            # Try to GROUND a phantom flat column to the real JSON leaf it meant
            # (races.date -> (races.event_schedule->>'date_set')::date) before
            # giving up. Deterministic, so it works regardless of the weak model.
            repaired = _repair_phantom_columns(expr, tables, json_paths, known, db_type)
            if repaired:
                r = dict(r)
                r["sql_expression"] = repaired
                fexpr = (r.get("filter") or "").strip()
                if fexpr:
                    rep_f = _repair_phantom_columns(fexpr, tables, json_paths, known, db_type)
                    if rep_f:
                        r["filter"] = rep_f
                logging.info("Resolved formula grounded by repair: %s -> %s",
                             r.get("name"), repaired[:120])
                tree = sqlglot.parse_one(repaired, read=dialect)  # re-check below
            else:
                logging.info(
                    "Resolved formula dropped (ungrounded columns %s): %s",
                    sorted(set(phantom))[:5], r.get("name"),
                )
                continue
        # Nested aggregate (an aggregate inside another aggregate's argument) is
        # invalid SQL — drop it so the model doesn't copy a non-executing formula
        # (e.g. a hand-rolled SQRT(SUM(POWER(x - SUM(x)/COUNT(x))...)) stddev).
        if _has_nested_aggregate(tree):
            logging.info("Resolved formula dropped (nested aggregate): %s", r.get("name"))
            continue
        # Identity-stub factor (×1 written as a constant like 100.0/100): the
        # resolver dropped a real factor of a composite metric and placeheld it
        # with 1 — the value is incomplete/wrong. Drop it so the generator builds
        # the metric from the delivered concept chain instead of copying a
        # half-formula. Deterministic AST check, general.
        if _has_identity_stub_factor(tree):
            logging.info("Resolved formula dropped (identity-stub ×1 factor — dropped term): %s",
                         r.get("name"))
            continue
        # Phantom TABLE: a formula referencing a table not in the schema (e.g. a
        # resolver-invented race_results) can't execute — column repair can't fix
        # a missing table, so drop it (general; the generator then falls back).
        if known_tables:
            bad = sorted({(tt.name or "").lower() for tt in tree.find_all(exp.Table)
                          if (tt.name or "").lower() and (tt.name or "").lower() not in known_tables})
            if bad:
                logging.info("Resolved formula dropped (unknown table %s): %s",
                             bad[:3], r.get("name"))
                continue
        kept.append(r)
    return kept


def _reconcile_plan_with_resolved(plan: dict, resolved: list,
                                  tables: list | None = None) -> dict:
    """Drop linker SELECT/FILTER entries that name a *decoy* column for a value a
    RESOLVED METRIC formula already defines, so the generator computes the formula
    instead of copying a similarly-named single column. An entry is a decoy when
    its ``asks_for`` shares a distinctive (non-stopword) token with a resolved
    concept name AND the entry's column is NOT referenced by that concept's
    formula (if it were, they agree — keep it). General: matching is on shared
    tokens + formula text, never on hardcoded names.
    """
    entity_stop = _entity_stopwords(tables)
    concepts = []
    for r in resolved or []:
        toks = _reconcile_tokens(r.get("name"), entity_stop)
        expr = (r.get("sql_expression") or "").lower()
        if toks and expr:
            concepts.append((toks, expr))
    if not concepts:
        return plan

    def _refs(expr: str) -> set:
        """Distinct column / JSON-leaf names a formula references."""
        keys = set(re.findall(r"->>?\s*'([^']+)'", expr))          # JSON leaf keys
        keys |= {m[1] for m in
                 re.findall(r"\b([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)", expr)}  # table.col
        return {k for k in keys if k}

    def _is_decoy(entry: dict) -> bool:
        col = (entry.get("column") or "").lower()
        if not col:
            return False
        leaf = col.split(".")[-1].strip("'\"`[]")
        a_toks = _reconcile_tokens(entry.get("asks_for"), entity_stop)
        if not a_toks:
            return False
        for toks, expr in concepts:
            if not (a_toks & toks):
                continue
            # The output phrase names this resolved concept. Keep the linker
            # binding ONLY when the concept's formula is essentially that single
            # column (they agree). When the formula is MULTI-TERM (references >1
            # distinct column/leaf, e.g. SPI = (9-final_position)+points), the
            # linker bound the output to just ONE term — a decoy: drop it so the
            # generator emits the FULL resolved formula, not the single column.
            multi_term = len(_refs(expr)) >= 2
            if multi_term or (leaf and leaf not in expr and col not in expr):
                return True
        return False

    new = dict(plan)
    sel0, flt0 = plan.get("select") or [], plan.get("filters") or []
    new["select"] = [e for e in sel0 if not _is_decoy(e)]
    new["filters"] = [e for e in flt0 if not _is_decoy(e)]
    dropped = (len(sel0) - len(new["select"])) + (len(flt0) - len(new["filters"]))
    if dropped:
        logging.info(
            "Reconcile: dropped %d decoy linker entr%s (covered by resolved formula)",
            dropped, "y" if dropped == 1 else "ies",
        )
    return new


    refed: set = set()
    for b in (plan.get("select") or []) + (plan.get("filters") or []):
        t = _orphan_join_table(b.get("column"))
        if t:
            refed.add(t)
    for r in (resolved or []):
        for s in (r.get("sql_expression"), r.get("filter")):
            for m in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.", str(s or "")):
                refed.add(m.lower())

    def _sides(j):
        js = j.get("join") if isinstance(j, dict) else j
        return [t for t in (_orphan_join_table(s)
                            for s in re.split(r"\s*=\s*", str(js))) if t]

    counts: dict = {}
    for j in joins:
        for t in set(_sides(j)):
            counts[t] = counts.get(t, 0) + 1
    kept, removed = [], 0
    for j in joins:
        sides = _sides(j)
        if any((t not in refed) and counts.get(t, 0) <= 1 for t in sides):
            removed += 1
            continue
        kept.append(j)
    if removed:
        logging.info("Reconcile: pruned %d orphan leaf join(s) (unreferenced decoy table)", removed)
        plan = dict(plan)
        plan["joins"] = kept
    return plan


def _plan_pinned_tables(link_plan: dict, skeleton: list, known: set) -> set:
    """Table names the generator MUST keep: only the tables the LINKER actually
    bound a column to (select/filter) or chose a join for. The full join-skeleton
    is deliberately NOT pinned — it connects nearly every candidate (FK web), so
    pinning it would keep ~all tables and defeat the prune. Join partners the
    linker needs are already its select/filter tables; the skeleton is still
    injected as join hints, and 1-hop partners are added by ``skeleton`` widening
    in the caller. Only names present in the candidate set are returned."""
    pinned: set = set()

    def _add(ref: str):
        head = str(ref or "").split(".")[0].strip().strip('"`[]').lower()
        if head and head in known:
            pinned.add(head)

    for b in ((link_plan.get("select") or []) + (link_plan.get("filters") or [])
              + (link_plan.get("group_by") or [])):
        _add(b.get("column"))
    for j in (link_plan.get("joins") or []):
        _js = j.get("join") if isinstance(j, dict) else j  # joins are {join,evidence} now
        for side in re.split(r"\s*=\s*", str(_js)):
            _add(side)
    return pinned


def _skeleton_one_hop(skeleton: list, anchor: set, known: set) -> set:
    """Tables reachable in ONE join hop from the linker-pinned ``anchor`` tables
    via the verified skeleton — the direct join partners the generator may need,
    without pulling the entire FK web."""
    out: set = set()
    for j in (skeleton or []):
        sides = [s.strip().strip('"`[]').lower().split(".")[0]
                 for s in re.split(r"\s*=\s*", str(j))]
        sides = [s for s in sides if s in known]
        if len(sides) == 2 and (sides[0] in anchor or sides[1] in anchor):
            out.update(sides)
    return out - anchor


def _dedup_table_columns(tables: list) -> list:
    """Drop duplicate column entries within each table (by lowercased name),
    keeping the first occurrence. Repeated schema enrichment/loading can append a
    table's columns more than once, which triples (or worse) every agent's schema
    — bloating the prompt and making the model see the same column several times.
    This collapses each table's column list to unique columns. General: by name
    only, no DB/table specifics."""
    if not tables:
        return tables
    out: list = []
    for t in tables:
        if not isinstance(t, (list, tuple)) or len(t) < 4 or not t[3]:
            out.append(t)
            continue
        seen: set = set()
        cols: list = []
        for c in t[3]:
            if not isinstance(c, dict):
                cols.append(c)
                continue
            cn = str(c.get("columnName") or c.get("name") or "").lower()
            if cn and cn in seen:
                continue
            if cn:
                seen.add(cn)
            cols.append(c)
        if len(cols) != len(t[3]):
            nt = list(t)
            nt[3] = cols
            out.append(nt)
        else:
            out.append(t)
    return out


def _resolved_metric_tables(resolved: list, tables: list) -> set:
    """Tables the RESOLVER bound its formulas to, found by mapping the DISTINCTIVE
    column names in each resolved expression/filter to the tables that own them.

    A composite metric is often decomposed by the resolver onto DETAIL columns
    (e.g. a per-event base measure used to compute a rate) that the LINKER did
    not pin — the linker tends to bind the whole metric to one pre-computed
    look-alike column on a snapshot table. If the detail tables are pruned, the
    generator cannot use the resolved formula and silently falls back to the
    look-alike. This keeps them. Alias-agnostic (matches real column names, not
    the resolver's aliases); generic columns owned by >2 tables are ignored so
    we don't over-keep. Fully general — names nothing.
    """
    if not resolved or not tables:
        return set()
    col_owners: dict = {}
    for t in tables:
        if not isinstance(t, (list, tuple)) or len(t) < 4 or not t[3]:
            continue
        tnl = str(t[0] or "").lower()
        for c in t[3]:
            if isinstance(c, dict):
                cn = str(c.get("columnName") or c.get("name") or "").lower()
                if cn:
                    col_owners.setdefault(cn, set()).add(tnl)
    # Generic column names appear in many tables; mapping them would pull in
    # unrelated tables (a metric's expr also contains the resolver's own aliases
    # and SQL keywords). Only a DISTINCTIVE column (owned by exactly one table,
    # not a common name) reliably identifies the detail table the formula needs.
    _generic = {"status", "name", "type", "code", "value", "label", "flag",
                "kind", "group", "class", "state", "amount", "number", "total",
                "count", "date", "year", "month", "season", "title", "result",
                "score", "rank", "position", "points", "time"}
    ref_cols: set = set()
    for r in (resolved or []):
        blob = f"{r.get('sql_expression') or ''} {r.get('filter') or ''}"
        for tok in re.findall(r"[A-Za-z_]\w*", blob):
            ref_cols.add(tok.lower())
    keep: set = set()
    for cn in ref_cols:
        if len(cn) < 5 or cn in _generic:
            continue
        owners = col_owners.get(cn)
        if owners and len(owners) == 1:  # distinctive: one owner only
            keep |= owners
    return keep


def _json_output_tables(question: str, json_paths: dict, tables: list) -> set:
    """Tables that OWN a JSON column whose leaf key is NAMED in the question.

    A requested OUTPUT value is often stored inside a JSON column (e.g. an
    event's name inside a schedule JSON). The weak linker frequently fails to
    bind such a value to its JSON path — it picks a flat look-alike or drops it —
    so the owning table is never pinned and gets pruned, and the generator then
    cannot emit that column (it hallucinates a flat name → gate reject → empty,
    or silently omits the column). This pins the owning table directly from the
    graph's JSON-leaf registry whenever the question names the leaf, independent
    of the linker's binding. Targeted: only a MULTI-token leaf key whose tokens
    ALL appear in the question (specific, not a single generic word). General,
    graph-driven; names nothing.
    """
    if not question or not json_paths or not tables:
        return set()
    qtokens = set(re.findall(r"[a-z]+", question.lower()))
    owners: dict = {}
    for t in tables:
        if not isinstance(t, (list, tuple)) or len(t) < 4 or not t[3]:
            continue
        tnl = str(t[0] or "").lower()
        for c in t[3]:
            if isinstance(c, dict):
                cn = str(c.get("columnName") or c.get("name") or "").lower()
                if cn:
                    owners.setdefault(cn, set()).add(tnl)
    keep: set = set()
    for jcol, info in (json_paths or {}).items():
        owner = owners.get(str(jcol).lower())
        if not owner:
            continue
        leaves = (info or {}).get("leaves") or {}
        for leaf in leaves:
            toks = [x for x in re.findall(r"[a-z]+", str(leaf).lower()) if len(x) > 2]
            if len(toks) >= 2 and all(x in qtokens for x in toks):
                keep |= owner
                break
    return keep


def _prune_generator_tables(tables: list, link_plan: dict, skeleton: list,
                            cap: int, resolved: list | None = None,
                            question: str | None = None,
                            json_paths: dict | None = None) -> list:
    """Shrink the GENERATOR's table set to keep prefill fast and noise low.

    Keeps (pinned) linker + join-skeleton tables, the tables the RESOLVER bound
    its formulas to, plus the top finder-ranked tables up to ``cap`` (pinned may
    overflow the cap so a needed table is never dropped). ``tables`` is already
    in finder-rank order. The full set is left untouched for the gates/FK
    validation — this prunes only the generator input. Returns the original list
    unchanged when cap<=0 or nothing would be dropped.
    """
    if not cap or cap <= 0 or not tables:
        return tables
    known = {str(t[0]).lower() for t in tables if t and t[0]}
    pinned = _plan_pinned_tables(link_plan or {}, skeleton or [], known)
    # Union in the resolver's bound tables so a decomposed formula's detail
    # columns survive (otherwise the generator loses them and falls back to the
    # linker's pre-computed look-alike — the snapshot-vs-detail failure).
    pinned = pinned | _resolved_metric_tables(resolved or [], tables)
    # Union in tables owning a JSON leaf the question names (a requested output
    # value stored in JSON that the linker failed to bind to its table).
    _json_pins = _json_output_tables(question or "", json_paths or {}, tables)
    if _json_pins:
        logging.info("JSON-output-table pins added: %s", sorted(_json_pins))
    pinned = pinned | _json_pins
    # MINIMAL CONTEXT (architecture principle): the LINKER already chose the exact
    # tables the query needs (its select/filter/join columns). Trust it — give the
    # generator ONLY those tables, in finder-rank order. Do NOT add speculative
    # 1-hop join partners: for a hub table that pulls ~10+ extra tables, blowing
    # the prompt to ~40k chars and ~5 min prefill on a weak model (the timeouts).
    # Fall back to the top finder-ranked tables ONLY when the linker produced no
    # plan at all (so we never send an empty schema).
    if pinned:
        keep = [t for t in tables if str(t[0]).lower() in pinned]
    else:
        keep = tables[:cap]
    # GRAPH-DRIVEN variance guard: drop a kept table that is FK-ISOLATED — named in
    # NO join of the verified FK skeleton — while other kept tables ARE joined. The
    # weak linker intermittently pins a spurious table the query cannot reach (only
    # via a hallucinated join key); the generator then emits an invalid join →
    # gate-reject → retry storm / 0 rows (seen: a tangential lookup table pinned
    # beside the real ones). The graph's FK skeleton is the TRUTH of what can join,
    # so a pin absent from every skeleton edge is noise. Deterministic, general, no
    # model, no prompt. Never fires for a single-table query (empty skeleton) and
    # never empties keep. This is the graph minimising the agents' variance.
    if keep and len(keep) > 1 and skeleton:
        _joined = set()
        for _j in skeleton:
            for _m in re.findall(r"([A-Za-z_]\w*)\s*\.", str(_j)):
                _joined.add(_m.lower())
        if _joined:
            _conn = [t for t in keep if str(t[0]).lower() in _joined]
            if _conn and len(_conn) < len(keep):
                logging.info(
                    "FK-isolated generator tables dropped (graph): %s",
                    [str(t[0]) for t in keep if t not in _conn],
                )
                keep = _conn
    if not keep or len(keep) >= len(tables):
        return tables
    logging.info(
        "Generator schema pruned: %d -> %d tables (linker-pinned=%d) kept=%s",
        len(tables), len(keep), len(pinned), [str(t[0]) for t in keep],
    )
    return keep


def _column_ref_head(col_ref: str) -> str:
    """'drivers.driver_identity->>\\'name\\'' -> 'drivers.driver_identity' (strip
    JSON-path / cast / call suffix), lowercased."""
    s = re.split(r"->|::|\s|\(", str(col_ref or "").strip())[0]
    return s.strip().strip('"`[]').lower()


def _prune_generator_columns(tables: list, query: str, link_plan: dict,
                             skeleton: list, cap: int,
                             extra_pins: set | None = None) -> list:
    """Column-level prune of the generator schema (chunk=field): keep only the
    relevance-ranked top columns plus the linker-selected and join-key columns;
    drop the rest and any table left with no kept columns. Ranking uses the
    field description (obfuscation-robust). This is the precise version of "send
    only the fields we actually need" — far tighter than per-table token caps.
    ``extra_pins`` (e.g. the resolved formulas' columns) are always kept.
    """
    if not cap or cap <= 0 or not tables:
        return tables
    pinned: set = set(extra_pins or set())
    for b in ((link_plan.get("select") or []) + (link_plan.get("filters") or [])
              + (link_plan.get("group_by") or [])):
        ref = _column_ref_head(b.get("column"))
        if "." in ref:
            pinned.add(ref)
    for j in (link_plan.get("joins") or []) + (skeleton or []):
        _js = j.get("join") if isinstance(j, dict) else j  # link joins are {join,evidence}; skeleton are str
        for side in re.split(r"\s*=\s*", str(_js)):
            ref = _column_ref_head(side)
            if "." in ref:
                pinned.add(ref)
    keep = set(rank_columns_by_relevance(tables, query, top_k=cap, pinned=pinned))
    out: list = []
    before = after = 0
    for t in tables:
        if not isinstance(t, (list, tuple)) or len(t) < 4 or not t[3]:
            out.append(t)
            continue
        tn = str(t[0] or "").lower()
        before += len(t[3])
        kept = [c for c in t[3] if isinstance(c, dict)
                and f"{tn}.{str(c.get('columnName') or c.get('name') or '').lower()}" in keep]
        if kept:
            nt = list(t)
            nt[3] = kept
            out.append(nt)
            after += len(kept)
    if not out:
        return tables
    if after < before:
        logging.info(
            "Generator columns pruned: %d -> %d cols, %d -> %d tables (cap=%d pinned=%d)",
            before, after, len(tables), len(out), cap, len(pinned),
        )
    return out


def _sql_referenced_col_names(sql: str, db_type: str | None) -> set:
    """Bare column names the SQL references (lowercased), via sqlglot AST."""
    if not sql:
        return set()
    try:
        import sqlglot
        from sqlglot import exp
        from api.sql_utils.sql_gate import sqlglot_dialect
        tree = sqlglot.parse_one(sql, read=sqlglot_dialect(db_type))
    except Exception:
        return set()
    if tree is None:
        return set()
    out: set = set()
    try:
        for c in tree.find_all(exp.Column):
            n = (c.name or "").lower()
            if n and n != "*":
                out.add(n)
    except Exception:
        return set()
    return out


def _table_col_name_set(t) -> set:
    if not isinstance(t, (list, tuple)) or len(t) < 4 or not t[3]:
        return set()
    return {str(c.get("columnName") or c.get("name") or "").lower()
            for c in t[3] if isinstance(c, dict)}


def _readd_tables_for_missing_fields(sql: str, gen_tables: list,
                                     full_tables: list, db_type: str | None) -> list:
    """RAG top-up: if the SQL references a field absent from the (pruned)
    ``gen_tables`` but present in the full candidate set, return ``gen_tables``
    widened with the owning table(s). Returns the same list when nothing to add
    (either no miss, or the missing field is a phantom not in the schema at all —
    that case is left to the generator's clarify/best-effort path)."""
    refs = _sql_referenced_col_names(sql, db_type)
    if not refs:
        return gen_tables
    gen_cols: set = set()
    for t in gen_tables:
        gen_cols |= _table_col_name_set(t)
    missing = refs - gen_cols
    if not missing:
        return gen_tables
    gen_names = {str(t[0]).lower() for t in gen_tables}
    add_names: set = set()
    for t in full_tables:
        if str(t[0]).lower() in gen_names:
            continue
        if _table_col_name_set(t) & missing:
            add_names.add(str(t[0]).lower())
    if not add_names:
        return gen_tables
    return list(gen_tables) + [t for t in full_tables
                              if str(t[0]).lower() in add_names]


_SINGLE_VALUE_RE = re.compile(
    r"\bhow many\b"
    r"|\b(just\s+)?(show me\s+|give me\s+|return\s+|display\s+)?(the\s+)?(total|overall)\s+(count|number|sum)\b"
    r"|\bthe\s+(count|number)\s+of\b"
    r"|\bthe\s+(highest|lowest|largest|smallest|maximum|minimum|greatest|max|min|single|overall)\b"
    r"|\breturn\s+the\s+(highest|lowest|maximum|minimum|largest|smallest|greatest|top|single)\b",
    re.IGNORECASE,
)


def _single_value_intent(query: str) -> bool:
    """Heuristic: the question's FINAL ask is a single scalar (a total/count, a
    superlative max/min, or 'how many') — so the result should be one value, not
    per-group rows. General signal words only, no per-DB literals."""
    return bool(_SINGLE_VALUE_RE.search(query or ""))


_OUTPUT_GRAIN_DIRECTIVE = (
    "OUTPUT GRAIN: the question asks for a SINGLE value. The final SELECT must "
    "return exactly ONE row with that one aggregate value — do NOT output "
    "per-group rows or extra grouping/dimension columns. A per-group calculation "
    "may appear in a CTE as an INTERMEDIATE step, but the final result is the "
    "single value the question asks for."
)

def _gate_reanalysis_query(user_query: str, rejected_sql: str, gate_report: str) -> str:
    return (
        f"{user_query}\n\n"
        "Previous generated SQL referenced schema objects that do not exist "
        "in the database graph, or failed deterministic validation. Re-run "
        "table and column selection from the graph/RAG context instead of "
        "guessing or locally patching identifiers.\n"
        f"Rejected SQL:\n{rejected_sql}\n"
        f"Validation errors:\n{gate_report}\n"
        "Use only real tables/columns and declared relationships from the "
        "graph. If the request cannot be answered from the real schema, ask "
        "for clarification and return no SQL."
    )


def _gate_grain_repair_query(user_query: str, rejected_sql: str, gate_report: str) -> str:
    return (
        f"{user_query}\n\n"
        "The SQL below is valid against the real schema but failed a correctness "
        "check (see below). Fix it MINIMALLY and return corrected SQL, keeping "
        "the same output columns and intent. If a needed output field is missing "
        "from your chosen tables (returned as NULL), follow the declared FOREIGN "
        "KEY relationships from the main object to a table that actually exposes "
        "it instead of padding with NULL. If a period/snapshot table is "
        "unconstrained, use its snapshot/period date column — the date column "
        "whose description marks it as the reporting/balance/as-of/snapshot "
        "date — to BOTH (a) slice that snapshot to the date or period from the "
        "question (or the latest/current snapshot date per the rules) AND "
        "(b) equate that snapshot date across every joined snapshot alias, so "
        "rows do not multiply across stored dates. Do NOT ask for clarification.\n"
        f"SQL to fix:\n{rejected_sql}\n"
        f"What to fix:\n{gate_report}"
    )


def _is_semantic_validation_failure(analysis: dict) -> bool:
    return bool(
        isinstance(analysis, dict)
        and analysis.get("__semantic_validation_failure")
        and not analysis.get("is_sql_translatable")
    )


_SQL_KEYWORDS = {
    "select", "from", "where", "group", "order", "having", "count", "sum",
    "avg", "min", "max", "case", "when", "then", "else", "end", "extract",
    "year", "month", "cast", "coalesce", "nullif", "over", "partition",
    "distinct", "integer", "real", "numeric", "date", "null", "true", "false",
    "and", "or", "not", "join", "left", "right", "inner", "outer", "desc",
    "asc", "lower", "trim", "round", "floor", "with", "union", "between",
}


def _resolved_base_columns(resolved: list | None) -> set:
    """Distinctive column-name tokens the RESOLVER bound its (grounded) formulas
    to. Used to PREFER a generated candidate that actually computes the metric
    from those columns over one that binds a snapshot/decoy look-alike. The
    resolver's decomposition is validated upstream, so this is a correctness
    signal, not a guess. General — driven only by the resolved expressions."""
    cols: set = set()
    for r in (resolved or []):
        blob = f"{r.get('sql_expression') or ''} {r.get('filter') or ''}".lower()
        for tok in re.findall(r"[a-z_][a-z0-9_]*", blob):
            if len(tok) >= 5 and tok not in _SQL_KEYWORDS:
                cols.add(tok)
    return cols


def _pick_by_execution(candidates: list, execfn, dialect,
                       resolved: list | None = None) -> dict:
    """Self-consistency by EXECUTION RESULT (not by SQL text).

    The generator is non-deterministic on a real prompt (same input -> different
    SQL across runs: right binding vs scoreval, right polarity vs COUNT(col), a
    join that explodes, a subquery that errors). Picking the modal SQL is a
    coin-flip when the samples disagree. Instead: EXECUTE each candidate, drop the
    ones that error or return an empty result, and keep the result-set the MOST
    candidates AGREE on (different-but-equivalent correct SQL converges to the
    same rows; the diverse wrong answers scatter, so the single most common result
    is the consensus-correct one). Falls back to SQL-modal selection when nothing
    executes. General — DB-agnostic, no per-case logic; the graph/connector does
    the executing (the goal's "use the DB + tools to minimise errors")."""
    import hashlib as _hl  # pylint: disable=import-outside-toplevel
    from collections import Counter as _Counter  # pylint: disable=import-outside-toplevel

    def _fingerprint(_rows) -> str | None:
        try:
            _norm = sorted(
                tuple(str(v) for v in (r.values() if isinstance(r, dict) else (r,)))
                for r in _rows)
            return _hl.sha256(repr(_norm).encode("utf-8")).hexdigest()
        except Exception:  # pylint: disable=broad-exception-caught
            return None

    scored: list = []
    for _c in candidates or []:
        _sql = str(_c.get("sql_query") or "").strip()
        if not _sql or _c.get("is_sql_translatable") is False:
            continue
        try:
            _rows = execfn(_sql)
        except Exception:  # pylint: disable=broad-exception-caught
            continue  # errored -> drop (filters L1)
        if not _rows:
            continue  # empty -> drop (filters degenerate 0-row)
        scored.append((_c, _fingerprint(_rows)))
    if not scored:
        return _pick_modal_analysis(candidates, dialect, resolved)
    _fps = _Counter(fp for _, fp in scored if fp)
    if _fps:
        _top, _n = _fps.most_common(1)[0]
        _winners = [c for c, fp in scored if fp == _top]
        logging.info("Generator result-consensus: %d valid/%d cands, top result x%d",
                     len(scored), len(candidates or []), _n)
        # Among candidates that agree on the modal result, prefer a
        # resolver-adherent one (reuse the SQL-modal tie-break).
        return _pick_modal_analysis(_winners, dialect, resolved) if len(_winners) > 1 else _winners[0]
    return scored[0][0]


def _pick_modal_analysis(candidates: list, dialect, resolved: list | None = None) -> dict:
    """Pick the best generation among N self-consistency candidates.

    The weak generator is non-deterministic (same prompt -> different SQL across
    runs). Selection order: (1) prefer candidates that ADHERE to the resolver's
    validated formula — i.e. reference the most of its base columns — so a
    candidate that computes a metric from the right detail columns beats one that
    falls back to a snapshot look-alike (the proven case-5 failure mode); then
    (2) the MODAL sqlglot-canonical SQL among those; then (3) highest confidence.
    General — no DB/query specifics; the resolved formula + structural agreement
    drive it. Reduces to pure modal voting when no resolved columns discriminate.
    """
    cands = [c for c in candidates if isinstance(c, dict)]
    if len(cands) <= 1:
        return cands[0] if cands else {}

    def _canon(sql: str) -> str:
        try:
            import sqlglot  # pylint: disable=import-outside-toplevel
            return sqlglot.parse_one(sql, read=dialect).sql(
                dialect=dialect, normalize=True, comments=False)
        except Exception:  # pylint: disable=broad-exception-caught
            return " ".join((sql or "").split()).lower()

    pool = [c for c in cands
            if (c.get("sql_query") or "").strip() and c.get("is_sql_translatable")] or cands

    # (1) Resolved-formula adherence filter: keep the candidates that use the
    # most of the resolver's base columns (only when it actually discriminates).
    ref_cols = _resolved_base_columns(resolved)
    if ref_cols and len(pool) > 1:
        def _adherence(c):
            sql = (c.get("sql_query") or "").lower()
            return sum(1 for col in ref_cols if col in sql)
        scores = [(_adherence(c), c) for c in pool]
        best = max(s for s, _ in scores)
        if best > 0 and any(s < best for s, _ in scores):
            kept = [c for s, c in scores if s == best]
            if kept:
                pool = kept

    keys = [_canon(c.get("sql_query") or "") for c in pool]
    from collections import Counter  # pylint: disable=import-outside-toplevel
    top_key, top_n = Counter(keys).most_common(1)[0]
    if top_n >= 2:
        matching = [c for c, k in zip(pool, keys) if k == top_key]
        return max(matching, key=lambda c: c.get("confidence", 0) or 0)
    return max(pool, key=lambda c: c.get("confidence", 0) or 0)


def _semantic_reanalysis_query(
    user_query: str,
    rejected_sql: str,
    semantic_issues: str,
) -> str:
    return (
        f"{user_query}\n\n"
        "Previous generated SQL was rejected by internal semantic validation. "
        "Re-run table and column selection from the graph/RAG context instead "
        "of guessing or locally patching the SQL.\n"
        f"Rejected SQL:\n{rejected_sql}\n"
        f"Semantic issue(s):\n{semantic_issues}\n"
        "Use only real tables/columns and declared relationships from the graph. "
        "If the request can be answered, return executable SQL that preserves "
        "the requested row grain, measure grain, temporal/change grain, and "
        "database dialect. If the correct source or metric is still genuinely "
        "ambiguous, ask for clarification and return no SQL."
    )


async def run_query(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    user_id: str,
    graph_id: str,
    chat_data: Any,
    db: Optional["FalkorDB"] = None,
) -> AsyncGenerator[Union[dict, _Final], None]:
    """Run the full text2sql pipeline.

    Yields wire-format progress dicts (matching the streaming JSON shapes the
    React frontend already parses) and ends with a ``_Final(QueryResult)``
    sentinel carrying the structured result for SDK callers.

    Args:
        user_id: Namespacing identifier.
        graph_id: Un-prefixed graph id; namespacing is applied internally.
        chat_data: Anything with ``chat`` / ``result`` / ``instructions`` /
            ``custom_api_key`` / ``custom_model`` / ``use_user_rules`` /
            ``use_memory`` attributes (Pydantic ``ChatRequest`` works).
        db: Optional FalkorDB handle; resolves to the server singleton when
            ``None``.
    """
    overall_start = time.perf_counter()
    namespaced = graph_name(user_id, graph_id)
    queries_history, result_history, instructions, use_user_rules = (
        validate_and_truncate_chat(chat_data)
    )
    raw_queries_history = list(queries_history)
    resolved_queries_history = _resolve_followup_clarification(
        queries_history, result_history,
    )
    if resolved_queries_history is not queries_history and len(queries_history) >= 2:
        _remember_recent_clarification(
            user_id,
            graph_id,
            queries_history[-2],
            queries_history[-1],
        )
    resolved_queries_history, correction_applied = _resolve_query_correction(
        resolved_queries_history,
        user_id,
        graph_id,
    )
    queries_history = _apply_recent_clarification(
        resolved_queries_history,
        user_id,
        graph_id,
    )
    if (
        not correction_applied
        and not _looks_like_query_correction(queries_history[-1])
    ):
        _remember_recent_base_query(user_id, graph_id, queries_history[-1])
    elif correction_applied:
        _remember_recent_base_query(user_id, graph_id, queries_history[-1])
    custom_api_key = getattr(chat_data, "custom_api_key", None)
    custom_model = getattr(chat_data, "custom_model", None)
    use_memory = getattr(chat_data, "use_memory", False)
    use_knowledge = getattr(chat_data, "use_knowledge", True)
    validate_custom_model(custom_model)

    # Session continuity (client-echo): if the browser sent the prior turn's
    # plan and THIS question is a related follow-up, inject it as a labelled
    # prompt block so generation REFINES the same plan instead of rebuilding.
    # A deterministic guard (+ db match) decides; the block is advisory context
    # only — it never pins columns through retrieval/pruning. Uses the raw
    # (pre-merge) current question for the relatedness check.
    prior_turn_block = build_prior_turn_block(
        getattr(chat_data, "session_context", None),
        raw_queries_history[-1] if raw_queries_history else queries_history[-1],
        graph_id,
    )
    if prior_turn_block:
        instructions = (
            f"{instructions}\n\n{prior_turn_block}"
            if instructions else prior_turn_block
        )
        logging.info(
            "Session continuity: applied prior-turn context (%d chars) for follow-up",
            len(prior_turn_block),
        )

    logging.info("User Query: %s", sanitize_query(queries_history[-1]))

    # Memory tool created concurrently with relevancy/find work — small perf
    # win for streaming, harmless for SDK. Lazy-imported via _create_memory_tool.
    memory_tool_task = (
        asyncio.create_task(_create_memory_tool(user_id, namespaced, db=db))
        if use_memory else None
    )

    yield {
        "type": "reasoning_step",
        "final_response": False,
        "message": "Step 1: Analyzing user query and generating SQL...",
    }

    logging.info("Text2SQL stage started: loading graph context graph=%s", namespaced)
    yield {
        "type": "reasoning_step",
        "final_response": False,
        "message": "Text2SQL: loading graph context and database metadata...",
    }
    db_description, db_url = await get_db_description(namespaced, db=db)
    raw_knowledge_spec = (
        await get_knowledge(namespaced, db=db) if use_knowledge else None
    )
    knowledge_spec = None
    if raw_knowledge_spec:
        # Lexical focus: trims the blob to query-matching concepts WHEN the KB is
        # in the structured concept format; for an unstructured KB it returns the
        # whole blob unchanged (no reduction).
        focused = focus_knowledge_for_query(raw_knowledge_spec, queries_history[-1])
        # Vector-retrieved relevant chunks from embedded :Knowledge/:Document
        # (recall-generous top_k so a needed concept is not dropped). This is also
        # the only path by which uploaded :Document text reaches the prompt.
        retrieved_context = ""
        try:
            retrieved_context = await retrieve_indexed_context(
                namespaced, queries_history[-1],
                labels=("Document", "Knowledge"),
                top_k=int(getattr(Config, "KNOWLEDGE_RETRIEVAL_TOP_K", 8)),
                db=db,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("Indexed-context retrieval skipped: %s", str(exc)[:200])
        retrieved_block = (
            "Retrieved database context (semantic match to the current question; "
            "the schema remains authoritative for table/column names):\n"
            f"{retrieved_context}"
        ) if retrieved_context else ""
        # If the lexical focus ACTUALLY trimmed (structured KB), keep it + augment
        # with the retrieved chunks. If it could NOT trim (returned ~the whole
        # blob), prefer the RAG-retrieved relevant subset over dumping the full
        # blob — but fall back to the full blob if retrieval found nothing, so a
        # needed concept is never silently dropped.
        trimmed = bool(focused) and len(focused) < 0.8 * len(raw_knowledge_spec)
        if trimmed:
            knowledge_spec = f"{focused}\n\n{retrieved_block}".strip() if retrieved_block else focused
        elif retrieved_block:
            knowledge_spec = retrieved_block
        else:
            knowledge_spec = raw_knowledge_spec
    user_rules_spec = (
        await get_user_rules(namespaced, db=db) if use_user_rules else None
    )
    # user_rules now carry GENERAL BEHAVIORAL guidance (JSON navigation, formula
    # fidelity, question-overrides-defaults, output grain) that must shape
    # GENERATION -- so they go to the generator. A post-hoc rule-gate re-applying
    # them was net-negative on the weak local model (it mangled correct SQL), so
    # the LLM gate is disabled (RULE_GATE_ENABLED=false) and the deterministic
    # sqlglot gate remains the safety net. rule_gate_user_rules_spec is kept for
    # when a genuine post-hoc preference pass is re-enabled later.
    generation_user_rules_spec = user_rules_spec or ""
    rule_gate_user_rules_spec = user_rules_spec or ""
    logging.info(
        "Knowledge pipeline: graph=%s use_knowledge=%s raw_knowledge_chars=%d "
        "focused_knowledge_chars=%d primary=%s use_user_rules=%s "
        "user_rules_chars=%d use_memory=%s",
        sanitize_log_input(namespaced),
        use_knowledge,
        len(raw_knowledge_spec or ""),
        len(knowledge_spec or ""),
        _knowledge_primary_for_log(knowledge_spec),
        use_user_rules,
        len(user_rules_spec or ""),
        use_memory,
    )
    db_type, loader_class = get_database_type_and_loader(db_url)
    generation_db_type = db_type

    if not loader_class:
        yield {"type": "error", "final_response": True,
               "message": "Unable to determine database type"}
        yield _Final(_build_query_result(
            sql_query="", results=[],
            ai_response="Unable to determine database type",
            is_valid=False,
            execution_time=time.perf_counter() - overall_start,
            error_message="Unable to determine database type",
        ))
        return

    if _needs_change_threshold_clarification(queries_history[-1]):
        msg = _change_threshold_clarification_message()
        yield {
            "type": "sql_query",
            "data": "",
            "conf": 0,
            "miss": msg,
            "amb": msg,
            "exp": (
                "The request combines a change threshold with an average change "
                "output, but does not state whether the threshold applies to "
                "individual change events or to grouped average changes."
            ),
            "is_valid": False,
            "final_response": False,
        }
        yield {
            "type": "followup_questions",
            "final_response": True,
            "message": msg,
            "missing_information": msg,
            "ambiguities": msg,
        }
        yield _Final(_build_query_result(
            sql_query="",
            results=[],
            ai_response=msg,
            confidence=0.0,
            is_valid=False,
            execution_time=time.perf_counter() - overall_start,
            missing_information=msg,
            ambiguities=msg,
            explanation=(
                "Asked for clarification instead of choosing one possible "
                "change-threshold interpretation."
            ),
        ))
        return

    # Concurrent: relevancy check + table-finding
    logging.info("Text2SQL stage started: RAG table search and relevancy check")
    yield {
        "type": "reasoning_step",
        "final_response": False,
        "message": "Text2SQL: running RAG table search and relevancy check...",
    }
    find_task = asyncio.create_task(
        find(
            namespaced,
            queries_history,
            db_description,
            knowledge_spec=knowledge_spec,
            user_rules_spec=generation_user_rules_spec,
            db=db,
        )
    )
    if getattr(Config, "TEXT2SQL_RELEVANCY_ENABLED", True):
        agent_rel = RelevancyAgent(
            queries_history, result_history, custom_api_key, custom_model,
        )
        relevancy_task = asyncio.create_task(
            agent_rel.get_answer(queries_history[-1], db_description)
        )
        raw_answer_rel = await relevancy_task
        if isinstance(raw_answer_rel, dict):
            answer_rel = raw_answer_rel
        else:
            logging.warning(
                "Text2SQL relevancy returned non-dict response: %s",
                sanitize_log_input(str(raw_answer_rel))[:300],
            )
            answer_rel = {}
        relevancy_status = str(answer_rel.get("status") or "On-topic").strip()
        relevancy_reason = str(answer_rel.get("reason") or "").strip()
        if relevancy_status not in {"On-topic", "Off-topic", "Inappropriate"}:
            logging.warning(
                "Text2SQL relevancy returned unexpected status=%s; continuing as on-topic",
                sanitize_log_input(relevancy_status)[:120],
            )
            relevancy_status = "On-topic"
        logging.info(
            "Text2SQL relevancy completed: status=%s reason=%s",
            sanitize_log_input(relevancy_status),
            sanitize_log_input(relevancy_reason)[:200],
        )
    else:
        relevancy_status = "On-topic"
        relevancy_reason = "relevancy check disabled"
        logging.info("Text2SQL relevancy skipped by configuration")

    if relevancy_status != "On-topic":
        find_task.cancel()
        try:
            await find_task
        except asyncio.CancelledError:
            logging.debug("Find task cancelled (off-topic query)")
        # Clear, user-facing explanation (the old "Off topic question: <raw reason>"
        # was opaque). State plainly that the DB lacks the data, give the reason,
        # and say what this database DOES contain so the user knows what to ask.
        _reason = (relevancy_reason or "").strip().rstrip(".")
        _scope = ""
        _dd = (db_description or "").strip()
        if _dd:
            _scope = " This database contains: " + (
                _dd[:240].rstrip() + ("…" if len(_dd) > 240 else "")
            )
        if relevancy_status == "Inappropriate":
            msg = ("This request can’t be handled by this assistant"
                   + ((" — " + _reason + ".") if _reason else ".") + _scope)
        else:  # Off-topic
            msg = ("This database does not contain information that can answer your "
                   "question"
                   + ((" — " + _reason + ".") if _reason else ".")
                   + _scope
                   + " Please ask about the data this database actually stores.")
        yield {"type": "followup_questions", "final_response": True, "message": msg}
        yield _Final(_build_query_result(
            sql_query="", results=[], ai_response=msg,
            is_valid=False,
            execution_time=time.perf_counter() - overall_start,
        ))
        return

    yield {
        "type": "reasoning_step",
        "final_response": False,
        "message": "Text2SQL: query is on-topic; waiting for RAG table candidates...",
    }
    tables = await find_task
    tables = _dedup_table_columns(tables)
    # Deterministic retrieval robustness (graph-driven): the LLM finder sometimes
    # OMITS a table that holds a value the QUESTION explicitly names inside a JSON
    # column (e.g. an event's name in a schedule JSON) while returning unrelated
    # tables. Add back any table that OWNS a question-named JSON leaf — straight
    # from the graph, independent of finder variance — so a requested output's
    # table is never lost. General, names nothing, no metadata change.
    try:
        _have_aug = {str(t[0]).lower() for t in (tables or []) if t and t[0]}
        _g_aug = resolve_db(db).select_graph(namespaced)
        _owner_names = [n for n in await json_leaf_owner_tables(_g_aug, queries_history[-1])
                        if n.lower() not in _have_aug]
        if _owner_names:
            _added_tabs = await fetch_table_entries(_g_aug, _owner_names)
            if _added_tabs:
                tables = tables + _added_tabs
                logging.info("Retrieval augmented with JSON-output owner table(s): %s",
                             [t[0] for t in _added_tabs])
    except Exception as _exc:  # pylint: disable=broad-exception-caught
        logging.warning("JSON-output retrieval augmentation skipped: %s", str(_exc)[:160])
    logging.info("Text2SQL RAG table search completed: tables=%d", len(tables or []))
    yield {
        "type": "reasoning_step",
        "final_response": False,
        "message": f"Text2SQL: selected {len(tables or [])} candidate tables; enriching context...",
    }
    tables = await _enrich_tables_with_value_samples(
        tables, loader_class, db_url, db_type, queries_history[-1],
    )
    # Data-grounded NULL-ness + range fact for date/numeric columns, so the
    # planner can choose `col > D` vs `col IS NULL` from the actual data rather
    # than the (often misleading) declared nullability.
    tables = await _enrich_columns_with_data_profile(
        tables, loader_class, db_url, db_type,
    )
    # Canonical found set. Retries REFINE this — they never re-search: a re-find
    # on an error/reanalysis query degrades the ranking and evicts the correctly
    # retrieved domain tables. We only refine what was found.
    base_tables = tables

    # (graph-native joins) Traverse the FK :REFERENCES edges to compute the EXACT
    # verified join conditions among the candidate tables, and hand them to the
    # generator. The model copies real joins instead of inventing them — the
    # single biggest correctness + weak-model win, and the deterministic join
    # gate can later reject any join not in this set.
    _gate_json_paths: dict = {}
    _gate_join_set: set = set()
    try:
        _g = resolve_db(db).select_graph(namespaced)
        # Self-heal FK edges from the loader-populated Table.foreign_keys property
        # (no-op when edges already exist; repairs after a fresh build / re-index).
        await materialize_fk_edges(_g)
        _tbl_names = [t[0] for t in tables if t and t[0]]
        _skeleton = await compute_join_skeleton(_g, _tbl_names)
        # Truth sources for the deterministic gate registry (graph-derived, no
        # hardcodes): valid JSON key paths per column + the verified join edges.
        _gate_json_paths = await column_json_paths(_g, _tbl_names)
        # Drop JSON leaves that are essentially empty in the data, so the binder is not
        # offered a perfectly-named-but-NULL field (the case-1 sprint.date vs date_set
        # trap). Data-driven, failure-tolerant, no metadata edit.
        _gate_json_paths = await asyncio.to_thread(
            _prune_empty_json_leaves, _gate_json_paths, tables, loader_class, db_url)
        _gate_join_set = {frozenset(s.split(" = ")) for s in _skeleton if " = " in s}
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("Join-skeleton computation skipped: %s", str(exc)[:200])
        _skeleton = []
    # LINKER (focused agent, one job): from the candidate columns + verified joins
    # it picks the EXACT columns/joins/filters (+ evidences) the query needs and
    # hands the generator a tight PLAN — instead of the generator choosing among
    # 150 columns + 17 joins (the noise that made the weak model pick wrong
    # sources). Strong source ambiguity is flagged for human clarification.
    # Detect the NAMED business concepts the question invokes BEFORE the linker
    # runs, so the binding agent sees each metric's DEFINITION (deterministic KB
    # name match; reused by the resolver below — no double work). Without it the
    # linker binds a metric phrase ("consistency score", "average lap time")
    # blind and can map it to a look-alike column on the WRONG grain (a
    # per-sprint aggregate instead of the per-lap measure the concept is defined
    # over). General: only concept TEXT travels into the linker — no table or
    # column is ever named here.
    _concepts: list = []
    if use_knowledge and raw_knowledge_spec:
        try:
            # Delivery = recall THEN precision: token-match RECALLS candidate concepts
            # (catches a concept the question implies by intent, e.g. "age"->Driver Age,
            # which pure embedding-KNN ranks too low and drops), then embedding RE-RANK
            # orders them by semantic closeness and keeps the few most relevant — a
            # focused, only-needed-but-sufficient set instead of the full token dump
            # (the over-recall that diluted the resolver). General; graph-embedding
            # backed; falls back to token order if embeddings are unavailable.
            _concepts = detect_concepts(queries_history[-1], raw_knowledge_spec,
                                        max_concepts=8)
            # SEMANTIC concept recall (codex): token-recall above only finds a
            # concept named by its NAME tokens. A concept the question names by
            # MEANING — "keyword-hitting values" => "Suspicion Signal Density"
            # ("keyword hits per message") — is never recalled, and the embed-rank
            # below can only re-order what recall already found. KNN over the
            # per-concept :Knowledge embeddings recalls by meaning; UNION (not
            # replace) so name- and meaning-named concepts both reach embed-rank,
            # which then ranks by similarity and caps small (junk on simple
            # queries falls out + the resolver/do-no-harm guards drop ungrounded).
            try:
                _kc = _parse_concept_chunks(await retrieve_concept_chunks(
                    namespaced, queries_history[-1], top_k=5, db=db))
                _seen_c = {n.lower() for n, _ in _concepts}
                for _n, _d in _kc:
                    if _n.lower() not in _seen_c:
                        _concepts.append((_n, _d))
                        _seen_c.add(_n.lower())
            except Exception:  # pylint: disable=broad-exception-caught
                pass
            _concepts = _embed_rank_concepts(
                queries_history[-1], _concepts,
                int(getattr(Config, "CONCEPT_RETRIEVAL_TOPK", 6) or 6))
            # Distinctive-term gate (codex): KNN/token recall over-recalls look-
            # alikes that match only on GENERIC words (risk/pattern/value) and
            # distract the weak resolver/generator (it wandered to a vendor
            # "comm trust" decoy). Keep only concepts that share a DISTINCTIVE
            # (>=6-char, non-generic) term with the question — the operative term
            # ("keyword") isolates the right metric. Fallback to the ranked set if
            # the gate would empty it (question named the concept only generically).
            _GEN = {"pattern", "patterns", "value", "values", "average", "count",
                    "counts", "total", "number", "numbers", "score", "scores",
                    "rate", "rates", "metric", "metrics", "level", "levels",
                    "amount", "amounts", "identify", "customer", "internal"}
            _qd = {w for w in re.findall(r"[a-z]{6,}", queries_history[-1].lower())
                   if w not in _GEN}
            if _qd and _concepts:
                _kept = [(n, d) for n, d in _concepts
                         if {w for w in re.findall(r"[a-z]{6,}", f"{n} {d}".lower())
                             if w not in _GEN} & _qd]
                if _kept:
                    _concepts = _kept
        except Exception:  # pylint: disable=broad-exception-caught
            _concepts = []
    # NOTE: the chain-expansion (composite-metric sub-concepts) is applied LATER,
    # AFTER the linker, so it reaches only the resolver/generator (which decompose
    # the formula) and does NOT perturb the linker's table choice with concepts it
    # doesn't need (minimal-context principle). See `_concepts` expansion below.
    _concepts_block = "\n".join(f"## {n}\n{d[:300]}" for n, d in _concepts)

    _link_plan = {"select": [], "filters": [], "joins": [], "group_by": [], "ambiguous": []}
    if tables:
        try:
            # Candidate columns for the linker, RELEVANCE-RANKED by the query vs
            # each field's name+description+samples (chunk=field). Ranking by
            # description means obfuscated names still surface (msec_val ~ "lap
            # time") and irrelevant decoys (a social "avg engagement rate" for a
            # "consistency" ask) rank low and fall outside the cap — so the linker
            # never sees them. Replaces the old first-90 dump.
            _colmap: dict = {}
            for _t in tables:
                _tn = str(_t[0] or "") if _t else ""
                for _c in (_t[3] if _t and len(_t) >= 4 and _t[3] else []):
                    if isinstance(_c, dict):
                        _n = _c.get("columnName") or _c.get("name")
                        if _n:
                            _colmap[f"{_tn}.{_n}".lower()] = (_tn, _n, _c)
            _ranked = rank_columns_by_relevance(
                tables, queries_history[-1],
                top_k=int(getattr(Config, "LINKER_CANDIDATE_COLUMNS", 50)),
            )
            # Relevance tokens for capping a JSON column's leaf list: question +
            # concept text. Without a cap a JSON column dumps ALL leaves (the
            # linker prompt ballooned to ~27K chars / 166 lines = the dominant
            # latency AND noise that let it grab look-alike leaves). Keep the
            # leaves most relevant to the question/concepts; date/keys still rank
            # in for as-of questions.
            _link_rel = set(re.findall(
                r"[a-z_]{3,}",
                (str(queries_history[-1] or "") + " " + " ".join(
                    f"{_n} {_d}" for _n, _d in (_concepts or []))).lower()))
            _link_leaf_cap = int(getattr(Config, "LINKER_JSON_LEAF_CAP", 6) or 6)
            _lc: list[str] = []
            _rel_tnames: set = set()
            for _ref in _ranked:
                _tup = _colmap.get(_ref)
                if not _tup:
                    continue
                _tn, _n, _c = _tup
                _rel_tnames.add(_tn)
                _ty = _c.get("type") or _c.get("dataType") or ""
                _d = " ".join(str(_c.get("description") or "").split())[:110]
                _lc.append(f"{_tn}.{_n}" + (f" ({_ty})" if _ty else "")
                           + (f": {_d}" if _d else ""))
                # For a JSON column, also list its bindable leaf paths (with each
                # leaf's meaning) so the linker can pick a SPECIFIC leaf — e.g. an
                # entity's readable 'reference'/'code' identifier inside an identity
                # JSON — instead of only the opaque JSON container or a numeric
                # surrogate key. The linker is otherwise blind to JSON leaves.
                if _gate_json_paths and str(_n).lower() in _gate_json_paths:
                    for _leaf in _render_json_leaf_paths(
                            _tn, _n, _gate_json_paths, str(_c.get("description") or ""),
                            rel_tokens=_link_rel, max_leaves=_link_leaf_cap):
                        _lc.append("    " + _leaf)
            if _lc:
                # Row-grain ONLY for the relevant (ranked-into) tables — lean, no
                # tangential-table noise — so the linker can tell a per-event
                # detail table from a coarser snapshot/summary and pick the source
                # whose grain matches the request. Graph-driven (PK), general.
                _cols_block = "\n".join(_lc)
                _grain = table_grain_lines([
                    _t for _t in (tables or [])
                    if isinstance(_t, (list, tuple)) and _t and str(_t[0]) in _rel_tnames
                ])
                if len(_grain) >= 2:
                    _cols_block = (
                        "TABLE ROW GRAINS (pick the source whose grain matches the "
                        "request — a per-event / 'after each' value comes from the "
                        "detail table, not a coarser snapshot/summary):\n"
                        + "\n".join(_grain) + "\n\nCOLUMNS:\n" + _cols_block
                    )
                logging.info("Linker candidates: %d ranked columns; top: %s",
                             len(_lc), " | ".join(c.split(":")[0].strip() for c in _lc[:12]))
                _stage_log("LINKER<-candidates", _cols_block)
                _stage_log("LINKER<-joins", "\n".join(_skeleton) or "(none)")
                _link_plan = await LinkerAgent(
                    queries_history, result_history, custom_api_key, custom_model,
                ).link(queries_history[-1], _cols_block,
                       "\n".join(_skeleton), generation_db_type,
                       _concepts_block)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("Linker skipped: %s", str(exc)[:200])

    # (metric/concept resolver) — runs BEFORE the plan is rendered so its
    # AUTHORITATIVE formulas can reconcile the linker plan (drop decoy columns).
    # When the question invokes a NAMED business concept (deterministic KB name
    # match), resolve its formula/filter to an exact column-bound SQL expression
    # so the weak model COPIES it instead of re-deriving it (the SPI 9-vs-21
    # flakiness). Skips the LLM entirely when no concept is referenced.
    _resolved: list = []
    # _concepts already detected above (before the linker) — reuse, no re-run.
    # CHAIN EXPANSION (resolver/generator only, AFTER the linker): a matched
    # concept whose formula NAMES other concepts is composite. Pull the
    # referenced concepts in now so the RESOLVER can decompose the metric down to
    # its base inputs (e.g. a rate -> its finishes/starts inputs) and bind those
    # to the DETAIL columns — instead of stubbing a factor or binding a
    # pre-computed look-alike. Done here (not before the linker) so it never
    # enlarges the linker's table-choice context with concepts it doesn't need.
    if use_knowledge and raw_knowledge_spec and _concepts:
        try:
            _support = expand_concept_references(_concepts, raw_knowledge_spec)
            if _support:
                _have = {n for n, _ in _concepts}
                _concepts = _concepts + [(n, d) for n, d in _support if n not in _have]
                logging.info("Concept chain expanded (resolver): +%d supporting (%s)",
                             len(_support), ", ".join(n for n, _ in _support))
        except Exception as _exc:  # pylint: disable=broad-exception-caught
            logging.warning("Concept-chain expansion skipped: %s", str(_exc)[:160])
    if use_knowledge and raw_knowledge_spec:
        try:
            _stage_log("RESOLVER<-concepts", "\n".join(
                f"## {n}\n{d[:300]}" for n, d in _concepts))
            if _concepts:
                # Rank the resolver's candidate columns by relevance to the
                # QUESTION *plus the matched concept DEFINITIONS* — not by table
                # order. A concept formula introduces terms absent from the bare
                # question (PPR -> "cumulative points" -> driver_standings.acc_pt;
                # Age -> "birth date"), so a plain table-order cap drops the very
                # column the resolver must bind, forcing it to hallucinate a
                # phantom. Ranking on the concept vocabulary surfaces it. General:
                # no table/column is named here — only the concept text drives it.
                # Rank candidate columns PER concept (each concept's own
                # name + definition + the question), then UNION under a hard
                # budget. Blending ALL concept defs into one query lets sibling
                # concepts' vocabulary outvote the columns the TARGET concept
                # needs: e.g. "sprint/result" boosts a decoy ("Sprint result
                # number") above the JSON date/birth path the Age concept must
                # bind, so the resolver grounds to a phantom and its formula is
                # dropped. Concept-local ranking keeps each concept's own
                # columns; an adaptive per-concept cap + a question-only
                # fallback keep the total bounded (a big dump made the weak
                # model mangle deep JSON paths). General + deterministic:
                # driven only by concept text, names nothing.
                _q = queries_history[-1] or ""
                _budget = 55
                _per_k = max(4, min(12, _budget // max(1, len(_concepts))))
                _pinned: list[str] = []
                _seen_ref: set[str] = set()
                for _cn, _cd in _concepts:
                    # Rank each concept's candidates by the CONCEPT TEXT ONLY (name +
                    # definition) — NOT the full question. The question carries
                    # unrelated tokens (output columns, filters) that bury the column
                    # the concept's own definition names: e.g. "Lap Time in Seconds"
                    # (def: lap time in milliseconds → seconds) must surface
                    # lap_times.msec_val ("lap time in milliseconds"), but the full
                    # question's surname/first_name/JSON/races tokens diluted it out.
                    for _ref in rank_columns_by_relevance(
                            tables, f"{_cn} {_cd}", top_k=_per_k, boost_tname=True):
                        if _ref not in _seen_ref:
                            _seen_ref.add(_ref)
                            _pinned.append(_ref)
                # Also PIN the columns the linker already chose (select / filters /
                # joins / group_by). The linker is concept-aware, so its plan IS the
                # focused, sufficient column set; pinning it lets us cut the budget
                # without ever dropping a column the query needs (the earlier failure
                # mode where a smaller budget silently lost the metric's base column).
                # Reusing the prior agent's focus = small AND complete context, no
                # fragile budget guesswork. Base "table.column" only (strip JSON path).
                for _sect in ("select", "filters", "joins", "group_by"):
                    for _it in (_link_plan.get(_sect) or []):
                        _cv = str(_it.get("column") or _it.get("join") or "")
                        for _bc in re.findall(r"[A-Za-z_]\w*\.[A-Za-z_]\w*", _cv):
                            _bcl = _bc.lower()
                            if _bcl not in _seen_ref:
                                _seen_ref.add(_bcl)
                                _pinned.append(_bcl)
                _keep = set(rank_columns_by_relevance(
                    tables, _q, top_k=_budget, pinned=_pinned))
                _rtables = []
                for _t in tables:
                    if not isinstance(_t, (list, tuple)) or len(_t) < 4 or not _t[3]:
                        _rtables.append(_t)
                        continue
                    _tn = str(_t[0] or "").lower()
                    _kc = [c for c in _t[3] if isinstance(c, dict) and
                           f"{_tn}.{str(c.get('columnName') or c.get('name') or '').lower()}"
                           in _keep]
                    if _kc:
                        _nt = list(_t)
                        _nt[3] = _kc
                        _rtables.append(_nt)
                # Render order MUST equal relevance order: the renderer caps at
                # max_cols in LIST order, so the concept-pinned columns have to
                # come FIRST. Otherwise a noise table that merely shares the
                # question's entity word (matched by the bare question, never by
                # any concept's formula — e.g. a profile/metadata table) can
                # exhaust the cap before the detail columns a composite metric
                # decomposes into are ever emitted, and the resolver then stubs
                # the missing factor. _pinned is already in concept-relevance
                # order; sort columns and tables by it. General, no names here.
                _prio = {_r: _i for _i, _r in enumerate(_pinned)}
                _BIG = 10 ** 6

                def _cref(_tnl, _c):
                    return f"{_tnl}.{str(_c.get('columnName') or _c.get('name') or '').lower()}"

                def _tbl_prio(_t):
                    if not (isinstance(_t, (list, tuple)) and len(_t) >= 4 and _t[3]):
                        return _BIG
                    _tnl = str(_t[0] or "").lower()
                    return min((_prio.get(_cref(_tnl, _c), _BIG) for _c in _t[3]
                                if isinstance(_c, dict)), default=_BIG)

                for _t in _rtables:
                    if isinstance(_t, list) and len(_t) >= 4 and _t[3]:
                        _tnl = str(_t[0] or "").lower()
                        _t[3] = sorted(
                            _t[3],
                            key=lambda _c, _tnl=_tnl: _prio.get(_cref(_tnl, _c), _BIG)
                            if isinstance(_c, dict) else _BIG)
                _rtables.sort(key=_tbl_prio)
                # Focused, concept-ranked context: enough to surface the columns
                # a concept binds to, but small enough not to drown/confuse the
                # resolver (a 35k-char dump made it mangle deep JSON paths).
                # Cap the RENDER size (lines = base cols + JSON leaves) so the resolver
                # gets a focused schema, not a 20k-char JSON-leaf dump that drowns the
                # concept signal (it then returned a degenerate proxy metric). Tables
                # render in relevance order, so the question's primary table + its leaves
                # survive the cap; irrelevant tables' leaves are trimmed.
                # Keep only the JSON leaves relevant to the question + matched
                # concepts (cap per column); a JSON column otherwise dumps ALL its
                # nested leaves and dilutes the binder into dropping a real formula
                # term. rel_query = question + concept definitions (which name the
                # leaves a metric binds, e.g. final_position/points for SPI).
                _ctx = candidate_columns_retrieval_text(
                    _rtables or tables,
                    max_cols=int(getattr(Config, "RESOLVER_MAX_COLS", 22) or 22),
                    json_paths=_gate_json_paths,
                    rel_query=(queries_history[-1] or "") + " " + " ".join(
                        f"{_n} {_d}" for _n, _d in _concepts),
                    max_json_leaves=int(getattr(Config, "RESOLVER_JSON_LEAF_CAP", 4) or 4))
                _stage_log("RESOLVER<-schema_ctx", _ctx)
                _resolved = await MetricResolverAgent(
                    queries_history, result_history, custom_api_key, custom_model,
                ).resolve(queries_history[-1], _concepts, _ctx, generation_db_type,
                          user_rules=generation_user_rules_spec)
                # Narrow critic (specialized agent): re-check each resolved
                # formula's column bindings against the concept DEFINITION + the
                # column descriptions and rebind a MIS-bound column (e.g. a count
                # keyed to a points column when the definition says the state is a
                # status/mark column). Targets the weak model's formula-construction
                # variance that the deterministic guards can't (valid-but-wrong
                # column). Tiny focused context; math left identical; flag-gated.
                if _resolved and getattr(Config, "FORMULA_VALIDATOR_ENABLED", True):
                    try:
                        _cdefs = {str(_n): _d for _n, _d in (_concepts or [])}
                        _resolved = await FormulaValidatorAgent(
                            queries_history, result_history, custom_api_key, custom_model,
                        ).validate(_resolved, _cdefs, _ctx, generation_db_type)
                    except Exception as _vexc:  # pylint: disable=broad-exception-caught
                        logging.warning("Formula validator skipped: %s", str(_vexc)[:160])
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("Metric resolver skipped: %s", str(exc)[:200])

    # Do-no-harm: drop any resolved formula that references columns not in the
    # real schema, so an ungrounded formula never replaces the linker's executable
    # column (would turn L3 into a non-executing L1).
    if _resolved:
        _resolved = _filter_grounded_resolved(
            _resolved, tables, generation_db_type, _gate_json_paths)

    # A resolved formula is authoritative: drop linker decoy columns it covers so
    # the generator computes the formula instead of copying a similar column
    # (e.g. "performance index score" -> biometric.sprintscore when SPI is a
    # formula over sprint_results.sprint_performance).
    if _resolved:
        _link_plan = _reconcile_plan_with_resolved(_link_plan, _resolved, tables)

    # Decide the GENERATOR's table set NOW (before assembling instructions) so the
    # join skeleton handed to the generator can be filtered to exactly those tables.
    # The full `tables`/`_skeleton`/`_gate_join_set` stay intact for the gate; only
    # the generator's view shrinks. (Column-level prune still happens further below.)
    base_tables = _prune_generator_tables(
        tables, _link_plan, _skeleton, getattr(Config, "GENERATOR_TABLE_CAP", 8),
        resolved=_resolved, question=queries_history[-1], json_paths=_gate_json_paths,
    )
    _gen_tbl_names = {t[0] for t in base_tables if t and t[0]}
    # Join skeleton scoped to the generator's tables only — joins to tables the
    # generator can't see are noise that legitimizes decoy/snapshot tables
    # (e.g. a snapshot "cumulative points" table joined in by a stray edge).
    _gen_skeleton = [
        s for s in _skeleton
        if all(
            part.split(".")[0] in _gen_tbl_names
            for part in s.split(" = ")
            if "." in part
        )
    ] if _skeleton else []

    # DIRECT-GEN mode (default ON for a capable model): the resolver's pre-bound
    # formula and the linker's column/filter PLAN, injected as "authoritative",
    # make the generator FOLLOW their (sometimes wrong) column bindings instead of
    # reasoning holistically from the KB definitions + schema — which a direct
    # clean prompt shows it does CORRECTLY (picks the right car/status column,
    # keeps the classification filter). So in direct-gen we DROP the resolved +
    # plan pre-binding and let the generator reason from KB + schema, but KEEP the
    # factual FK JOINS (skeleton). Toggle off with T2S_DIRECT_GEN=0.
    _direct_gen = os.getenv("T2S_DIRECT_GEN", "1") == "1"
    # Inject (final prompt order top->bottom): RESOLVED METRICS (authoritative),
    # clarification, SCHEMA-LINK PLAN, then the original instructions.
    _plan_block = render_plan_block(_link_plan)
    if _plan_block and not _direct_gen:
        instructions = f"{_plan_block}\n\n{instructions or ''}".strip()
        logging.info(
            "Linker plan injected: select=%d filters=%d joins=%d ambiguous=%d",
            len(_link_plan["select"]), len(_link_plan["filters"]),
            len(_link_plan["joins"]), len(_link_plan["ambiguous"]),
        )
    elif _gen_skeleton:  # linker produced nothing (or direct-gen) -> raw verified joins
        instructions = (
            "VERIFIED JOIN CONDITIONS (use these EXACT ON conditions; do not "
            "invent a join):\n- " + "\n- ".join(_gen_skeleton)
            + f"\n\n{instructions or ''}"
        ).strip()
    # In DIRECT-GEN we deliberately do NOT inject the linker's ambiguous-source
    # clarification: it leaks the linker's (sometimes wrong) preferred binding —
    # e.g. "track names" -> location->>'city' while omitting the obvious ->>'name'
    # leaf entirely — and biases the generator onto it. A clean prompt lets the
    # generator pick the best-described leaf itself (probe: 3/3 picks 'name').
    if _link_plan.get("ambiguous") and not _direct_gen:
        _clar = render_clarification(_link_plan["ambiguous"])
        if _clar:
            instructions = (
                f"{_clar}\n(If you cannot tell from the schema, pick the column "
                f"whose description best fits the question's domain.)\n\n"
                f"{instructions or ''}"
            ).strip()
            logging.info("Linker flagged %d ambiguous source(s)", len(_link_plan["ambiguous"]))

    _rblock = render_resolved_block(_resolved)
    # Direct-gen normally drops the resolved block (clean prompt beats pre-binding
    # for single-column metrics). But a RATIO/composite metric — its bound formula
    # divides or combines terms — is exactly what a weak generator gets wrong (it
    # uses the numerator column alone, e.g. AVG(keyword_match_count) instead of
    # AVG(keyword_match_count / msg_count_total)). So inject the resolved block
    # when any resolved metric's formula is a multi-term ratio, even on direct-gen. (codex)
    _has_ratio = any("/" in str((r or {}).get("sql_expression") or "")
                     for r in (_resolved or []))
    if _rblock and (not _direct_gen or _has_ratio):
        instructions = f"{_rblock}\n\n{instructions or ''}".strip()
        logging.info("Injected %d resolved metric/concept(s)%s", len(_resolved),
                     " (ratio formula on direct-gen)" if (_direct_gen and _has_ratio) else "")

    # DIRECT-GEN — narrow nested-computation plan for DISPERSION metrics only.
    # The resolver's `grain` field over-fires (it tags row-level CASE/arithmetic
    # metrics "one event" too), so gate on the SQL itself: only a metric whose
    # expression uses a DISPERSION aggregate (STDDEV/VARIANCE over an event's
    # sub-rows) GENUINELY must be computed per-event-then-aggregated — pooling a
    # spread across events is meaningless. This fires for at most the spread/
    # consistency metric of a query (e.g. lap-time STDDEV) and NOTHING else, so
    # row-level cases (per-row index, CASE points table, plain sum) are untouched.
    # General, dialect-agnostic. (Kept standalone: it fixes the per-event grain so
    # the spread is computed correctly; the separate G8-rework needed to ALSO
    # retain NULL-aggregate rows was reverted — it perturbed the lean prompt and
    # regressed unrelated cases. So this makes the VALUE correct; whole-case L4
    # additionally needs that count fix, which the bloated prompt can't absorb.)
    if _direct_gen and _resolved:
        _disp = re.compile(r"\b(?:STDDEV|STDDEV_POP|STDDEV_SAMP|VARIANCE|VAR_POP|VAR_SAMP)\s*\(", re.I)
        _grain_plan = [
            f'- "{r.get("name", "")}" (grain = {r["grain"]}): build it in TWO '
            "levels — (1) a CTE that GROUPs BY that event key and computes the "
            "spread for ONE event; (2) then aggregate those per-event values to "
            "the level the question asks (e.g. AVG per entity). NEVER compute the "
            "spread directly over all rows pooled from different events."
            for r in _resolved
            if isinstance(r, dict) and str(r.get("grain") or "").strip()
            and _disp.search(str(r.get("sql_expression") or ""))
        ]
        if _grain_plan:
            instructions = (
                "NESTED METRIC COMPUTATION (mandatory — a spread/dispersion metric "
                "defined within an event MUST be nested, never pooled):\n"
                + "\n".join(_grain_plan) + f"\n\n{instructions or ''}"
            ).strip()
            logging.info("Injected %d dispersion-grain plan(s) (direct-gen)", len(_grain_plan))

    # Output-grain directive (general): when the question's final ask is a single
    # value (a total/count, a superlative, or "how many"), tell the generator to
    # return one aggregate value, not per-group rows.
    if _single_value_intent(queries_history[-1]):
        instructions = f"{_OUTPUT_GRAIN_DIRECTIVE}\n\n{instructions or ''}".strip()
        logging.info("Output-grain directive injected (single-value intent)")

    # Prune the GENERATOR's schema to the linker/skeleton-pinned tables + top
    # finder-ranked ones (cap). find() can surface ~20 tables (~45K schema chars)
    # → ~200s prefill per call on a weak model → correction retries blow the
    # per-query budget (the stability/veterans timeouts). The gates keep the full
    # `tables` set; only the generator input shrinks. Pinned tables never dropped.
    # (base_tables already computed above, before the instruction assembly, so the
    # join skeleton could be scoped to it — reuse it here.)
    # Column-level prune (chunk=field): keep only the relevance-ranked + linker +
    # join-key columns. This is the precise "send only needed fields" the schema
    # was retrieved by — kills the per-table token-cap bloat that name-obfuscation
    # defeats, and shrinks prefill far more than table pruning alone.
    # Rank the kept columns by the question PLUS the matched concept definitions,
    # not the bare question. A value the question names by INTENT ("age") lives in
    # a column described by a different word ("birth_date" in driver_identity), so
    # ranking on the bare question scores that column low and the prune drops it —
    # leaving the model without the value and forcing it onto a decoy (the biometric
    # driver_profiles). The concept definition ("Driver Age = ... birth date ...")
    # bridges intent->column, exactly as the resolver's candidate ranking does.
    _gen_rank_query = queries_history[-1] or ""
    if _concepts:
        _gen_rank_query += " " + " ".join(d for _, d in _concepts)
    base_tables = _prune_generator_columns(
        base_tables, _gen_rank_query, _link_plan, _skeleton,
        getattr(Config, "GENERATOR_COLUMN_CAP", 45),
    )

    # (#4) Column-aware business-knowledge for the GENERATOR: retrieve using the
    # question PLUS the candidate columns' descriptions, so a concept keyed to a
    # specific column is pulled even when the bare question never names it. The
    # finder's query-only knowledge_spec is left untouched to preserve table
    # recall (find() already ran above). Pure embedding work, no extra LLM call.
    generation_knowledge_spec = knowledge_spec
    # When the question invokes named KB concepts, the generator's knowledge IS the
    # focused concept chain (the exact definitions the requested metric is built from)
    # — not the noisy semantic-retrieval blob, which buried the formula among ~15
    # unrelated concepts and ballooned the prompt to ~42k chars (a single 300s+ LLM
    # call -> timeout). Same focused chain the resolver already used; small + salient.
    if _concepts:
        # For any concept the resolver ALREADY distilled into a column-bound formula,
        # drop its raw KB text from the generator: the resolved-metrics block delivers
        # it cleanly, while the raw definition's textbook denominator / threshold /
        # grain wording only contradicts and dilutes (probes: a focused prompt builds
        # the correct query; the raw defs make the weak generator drop a metric or
        # invent a spurious filter). Keep raw text only for UNresolved concepts so the
        # generator still has their definition. General — no DB/column specifics.
        _resolved_token_sets = [
            set(re.findall(r"[a-z]+", str(r.get("name") or "").lower()))
            for r in (_resolved or [])
        ]
        # Common domain words that must NOT, on their own, mark a concept resolved
        # (else a resolved metric named "... Sprint Finishers" would falsely drop the
        # "Sprint Performance Index" definition just because both contain "sprint").
        _COMMON_CONCEPT_TOKENS = {
            "sprint", "driver", "drivers", "race", "races", "season", "total",
            "average", "points", "point", "score", "value", "time", "rate",
        }

        def _resolved_already(_cn: str) -> bool:
            # A concept counts as resolved only if some resolved metric name carries
            # its FULL distinctive signature (all its non-common tokens) — so its raw
            # KB text is dropped only when the resolver truly produced that metric.
            _toks = {t for t in re.findall(r"[a-z]+", _cn.lower())
                     if len(t) >= 5 and t not in _COMMON_CONCEPT_TOKENS}
            if not _toks:
                return False
            return any(_toks <= rset for rset in _resolved_token_sets)

        # In DIRECT-GEN the resolved-metrics block is NOT injected into the
        # generator, so we must KEEP every matched concept's raw KB definition —
        # dropping a "resolved" concept here would otherwise leave the generator
        # with NO definition of it (e.g. it lost "reliability = finishes/starts,
        # finished if the status mark is null" and fell back to a wrong column).
        # Only drop resolved concepts when their column-bound formula IS injected.
        if os.getenv("T2S_DIRECT_GEN", "1") == "1":
            # FOCUS the generator's knowledge to the question's PRIMARY metric(s)
            # + their transitive chain ONLY. Unrelated embed-rank look-alikes
            # (other indices/scores the question doesn't ask for) DILUTE a chained
            # formula and make the weak model mis-compose it — e.g. case5 CPS kept
            # the rate's *100 but dropped the metric's /100 (=> a 100x error) only
            # when 8 metrics were present; with the 3-metric chain it computes the
            # cancelled form correctly. Primary = a concept whose distinctive
            # (>=5-char, non-common) name tokens ALL appear in the question; fall
            # back to the top-ranked concept. General — no DB/column specifics.
            _ql = (queries_history[-1] or "").lower()

            def _named_in_q(_cn: str) -> bool:
                _toks = [t for t in re.findall(r"[a-z]+", _cn.lower())
                         if len(t) >= 5 and t not in _COMMON_CONCEPT_TOKENS]
                return bool(_toks) and all(t in _ql for t in _toks)

            # Primaries = the top embed-ranked concepts (highest semantic
            # similarity to the question — these ARE the metric/classification it
            # asks about) UNION any concept whose distinctive name tokens appear
            # in the question. The embed-rank covers concepts referenced by a
            # SYNONYM (e.g. the question says "pit strategy classification" but the
            # concept is named "Pit Strategy Cluster" — name-token match alone
            # drops it; similarity keeps it). Then add their transitive chain.
            _topk = int(getattr(Config, "CONCEPT_RETRIEVAL_TOPK", 6) or 6)
            _primary_n = max(2, min(3, _topk))
            _primaries = list(_concepts[:_primary_n])
            _have0 = {n for n, _ in _primaries}
            _primaries += [(n, d) for n, d in _concepts[_primary_n:]
                           if _named_in_q(n) and n not in _have0]
            try:
                _refs = expand_concept_references(_primaries, raw_knowledge_spec)
            except Exception:  # pylint: disable=broad-exception-caught
                _refs = []
            _focus_names = {n for n, _ in _primaries} | {n for n, _ in _refs}
            _keep_concepts = [(n, d) for n, d in _concepts if n in _focus_names] or list(_concepts)
            if len(_keep_concepts) < len(_concepts):
                logging.info("Generator KB focused to chain: %d/%d concepts (%s)",
                             len(_keep_concepts), len(_concepts),
                             ", ".join(n for n, _ in _keep_concepts))
        else:
            _keep_concepts = [(n, d) for n, d in _concepts if not _resolved_already(n)]
        generation_knowledge_spec = "\n\n".join(
            f"## {n}\n{d}" for n, d in _keep_concepts
        ).strip()
    elif (use_knowledge and raw_knowledge_spec
            and getattr(Config, "KNOWLEDGE_BY_COLUMNS_ENABLED", True)):
        col_text = candidate_columns_retrieval_text(tables, max_cols=80)
        if col_text:
            col_key = f"{queries_history[-1]}\n\nCandidate columns:\n{col_text}"
            col_ctx = ""
            try:
                col_ctx = await retrieve_indexed_context(
                    namespaced, col_key,
                    labels=("Document", "Knowledge"),
                    top_k=int(getattr(Config, "KNOWLEDGE_RETRIEVAL_TOP_K", 8)),
                    db=db,
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning("Column-aware knowledge retrieval skipped: %s", str(exc)[:200])
            if col_ctx:
                col_block = (
                    "Retrieved database context (semantic match to the question "
                    "and the candidate columns; the schema remains authoritative "
                    "for table/column names):\n" + col_ctx
                )
                focused_local = focus_knowledge_for_query(
                    raw_knowledge_spec, queries_history[-1]
                )
                if focused_local and len(focused_local) < 0.8 * len(raw_knowledge_spec):
                    generation_knowledge_spec = f"{focused_local}\n\n{col_block}".strip()
                else:
                    generation_knowledge_spec = col_block
                logging.info(
                    "Column-aware generator knowledge: chars=%d (finder knowledge=%d)",
                    len(generation_knowledge_spec or ""), len(knowledge_spec or ""),
                )

    memory_tool = None
    memory_context = None
    # Advisory prior-example recall (the MemoryAgent vector-matches similar past
    # questions, judges intent, and returns example SQL + a recommendation). It
    # runs whenever memory is ON — even for a standalone one-shot question — and
    # is suggestions only; the generator decides. Relevance is the agent's job,
    # so there is no separate history gate here.
    memory_tool, memory_context = await _memory_context_or_none(
        memory_tool_task,
        queries_history[-1],
        custom_model=custom_model,
        custom_api_key=custom_api_key,
    )

    logging.info(
        "Analysis context injection: graph=%s tables=%d knowledge_chars=%d "
        "user_rules_chars=%d memory_chars=%d",
        sanitize_log_input(namespaced),
        len(tables or []),
        len(knowledge_spec or ""),
        len(user_rules_spec or ""),
        len(memory_context or ""),
    )

    agent_an = AnalysisAgent(
        queries_history, result_history, custom_api_key, custom_model,
    )
    logging.info("Text2SQL stage started: generating SQL with analysis LLM")
    yield {
        "type": "reasoning_step",
        "final_response": False,
        "message": "Text2SQL: generating SQL with LLM...",
    }
    def _bb_run_analysis(an_tables, an_rules):
        # Fresh agent per attempt so the top-up loop does not accumulate
        # message history across retries.
        a = AnalysisAgent(
            queries_history, result_history, custom_api_key, custom_model,
        )
        return a.get_analysis(
            queries_history[-1], an_tables, db_description, instructions,
            memory_context, generation_db_type, an_rules, knowledge_spec,
        )

    tool_path_used = False
    if getattr(Config, "BLACKBOARD_PIPELINE_ENABLED", False):
        # Blackboard pipeline: build a shared JSON state from the ranked find()
        # result (initial N tables), let a rule-RAG agent select the relevant
        # business rules into it, generate, and -- if generation cannot map a
        # requested element -- let the SchemaTopUp agent ADD tables/columns
        # (missing_tables_request) by re-querying retrieval, then regenerate.
        logging.warning(
            "BB-BRANCH ENTER pipe=%s tool=%s tables=%d",
            getattr(Config, "BLACKBOARD_PIPELINE_ENABLED", False),
            getattr(Config, "BLACKBOARD_TOOL_AGENT_ENABLED", False),
            len(tables or []),
        )
        from api.core import blackboard as _bb
        from api.agents.schema_topup_agent import SchemaTopUpAgent
        bb = _bb.from_find_tables(
            queries_history[-1], tables, generation_db_type, namespaced,
            initial_limit=int(getattr(Config, "BLACKBOARD_INITIAL_TABLE_LIMIT", 12)),
            max_topups=int(getattr(Config, "BLACKBOARD_MAX_TOPUPS", 2)),
        )
        # Record prior-turn user remarks into the blackboard so later
        # corrections refine from accumulated state.
        _bb.seed_user_feedback(bb, queries_history, result_history)
        # Database-specific business knowledge (general, no table names) carries
        # the domain conventions (authoritative measure source, etc.) the planner
        # needs to disambiguate near-synonym columns.
        bb["knowledge"] = knowledge_spec or ""
        effective_rules = user_rules_spec
        if getattr(Config, "RULE_RAG_ENABLED", False) and user_rules_spec:
            try:
                from api.agents.business_rule_rag_agent import BusinessRuleRagAgent
                BusinessRuleRagAgent(
                    user_rules_spec,
                    max_chars=int(getattr(Config, "RULE_RAG_MAX_CHARS", 3500)),
                ).select(bb)
                effective_rules = _bb.selected_rules_as_text(bb) or user_rules_spec
            except Exception as exc:  # noqa: BLE001
                logging.warning(
                    "Rule-RAG selection failed, using full rules: %s", exc
                )
        topup_agent = SchemaTopUpAgent(
            find, namespaced, db_description, knowledge_spec, db,
            max_new_tables=int(getattr(Config, "BLACKBOARD_TOPUP_MAX_TABLES", 6)),
            user_rules_spec=user_rules_spec or "",
        )
        answer_an = None
        if getattr(Config, "BLACKBOARD_TOOL_AGENT_ENABLED", False):
            # Tool-based two-phase agent: rule-aware planner (field selection via
            # validated tool calls) -> SQL writer. Avoids free-text JSON format
            # errors and reads the selected business rules when choosing columns.
            try:
                from api.agents.business_rule_rag_agent import parse_rules
                if not bb.get("selected_business_rules") and user_rules_spec:
                    _bb.set_selected_rules(bb, parse_rules(user_rules_spec))
            except Exception as exc:  # noqa: BLE001
                logging.warning("Rule parse for tool planner failed: %s", exc)
            from api.agents.tool_blackboard_agent import ToolBlackboardPipeline

            def _sync_topup(_board):
                import asyncio as _aio
                return _aio.run(topup_agent.topup(_board))

            pipeline = ToolBlackboardPipeline(custom_model, custom_api_key)
            answer_an = await asyncio.to_thread(pipeline.run, bb, _sync_topup)
            logging.warning(
                "BB-TOOL done: translatable=%s topups=%d "
                "selected_rules=%d sql_len=%d",
                answer_an.get("is_sql_translatable"),
                bb["retrieval"]["topup_count"],
                len(bb.get("selected_business_rules") or []),
                len(str(answer_an.get("sql_query") or "")),
            )
            tool_path_used = True
            # ROUTER: the tool agent did not produce SQL (need_clarification).
            # Decide what to do instead of blindly asking the user:
            #   delegate -> RAG schema top-up + re-plan (missing schema);
            #   self-resolve -> decisive re-plan (commit from metadata);
            #   ask -> fall through to the followup (genuine ambiguity).
            _router_rounds = 0
            while (not answer_an.get("is_sql_translatable")) and _router_rounds < 2:
                _router_rounds += 1
                _has_missing = bool(
                    bb.get("missing_tables_request")
                    or bb.get("missing_columns_request")
                )
                if _has_missing and _bb.can_topup(bb):
                    bb.setdefault("trace", []).append(
                        {"agent": "router", "decision": "delegate_topup"})
                    yield {
                        "type": "reasoning_step", "final_response": False,
                        "message": "Router: schema insufficient -> delegating to "
                                   "RAG top-up and re-planning...",
                    }
                    bb = await topup_agent.topup(bb)
                    answer_an = await asyncio.to_thread(
                        pipeline.run, bb, _sync_topup)
                elif not bb.get("_decisive_tried"):
                    bb["_decisive_mode"] = True
                    bb["_decisive_tried"] = True
                    bb.setdefault("trace", []).append(
                        {"agent": "router", "decision": "self_resolve"})
                    yield {
                        "type": "reasoning_step", "final_response": False,
                        "message": "Router: resolving from metadata "
                                   "(decisive re-plan)...",
                    }
                    answer_an = await asyncio.to_thread(
                        pipeline.run, bb, _sync_topup)
                else:
                    bb.setdefault("trace", []).append(
                        {"agent": "router", "decision": "ask_user"})
                    break
            logging.warning(
                "BB-ROUTER done: translatable=%s rounds=%d topups=%d",
                answer_an.get("is_sql_translatable"), _router_rounds,
                bb["retrieval"]["topup_count"],
            )
        max_attempts = int(getattr(Config, "BLACKBOARD_MAX_TOPUPS", 2)) + 1
        for _attempt in range(max_attempts):
            if answer_an is not None:
                break
            bb_tables = _bb.selected_legacy_tables(bb)
            answer_an = await asyncio.to_thread(
                _bb_run_analysis, bb_tables, effective_rules
            )
            if answer_an.get("is_sql_translatable"):
                break
            if not _bb.can_topup(bb):
                break
            hint = (
                str(answer_an.get("missing_information") or "").strip()
                or str(answer_an.get("ambiguities") or "").strip()
            )
            if not hint:
                break
            _bb.add_missing_tables_request(
                bb, "analysis_sql_agent", hint,
                reason="generation could not map a requested element",
            )
            before = len(bb["tables"])
            yield {
                "type": "reasoning_step",
                "final_response": False,
                "message": (
                    "Text2SQL: schema insufficient; RAG agent topping up "
                    "tables/columns..."
                ),
            }
            bb = await topup_agent.topup(bb)
            if len(bb["tables"]) <= before:
                break  # nothing new added -> stop looping
            logging.info(
                "Blackboard top-up applied: attempt=%d tables_now=%d",
                _attempt + 1, len(bb["tables"]),
            )
        # Persist the generated SQL into the blackboard (draft == committed
        # final here; the post-gate SQL is recorded again after the gate).
        if answer_an is not None:
            _bb.set_sql_draft(bb, answer_an.get("sql_query"))
            _bb.set_sql_final(bb, answer_an.get("sql_query"))
        tables = _bb.selected_legacy_tables(bb)
        base_tables = tables
        logging.info(
            "Blackboard pipeline done: final_tables=%d topups=%d "
            "rules_chars=%d (was %d)",
            len(tables), bb["retrieval"]["topup_count"],
            len(effective_rules or ""), len(user_rules_spec or ""),
        )
    else:
        # Generate on the PRUNED schema (base_tables), with a RAG top-up loop: if
        # the SQL references a real field that pruning dropped, re-add it from the
        # full candidate set and regenerate (≤ GENERATOR_MAX_TOPUPS). After that
        # the generator's own missing_information surfaces a clarification to the
        # user. A phantom field (not anywhere in the schema) ends the loop and is
        # handled by the generator's best-effort/clarify path.
        _gen_tables = base_tables
        _max_topups = int(getattr(Config, "GENERATOR_MAX_TOPUPS", 5))
        answer_an = None
        _stage_log("GENERATOR<-instructions", instructions or "(none)")
        _stage_log("GENERATOR<-knowledge", generation_knowledge_spec or "(none)")
        _stage_log("GENERATOR<-user_rules", generation_user_rules_spec or "(none)")
        _stage_log("GENERATOR<-schema", _schema_brief(_gen_tables))
        _sc_n = int(getattr(Config, "GENERATOR_SELF_CONSISTENCY", 3) or 3)
        for _gtry in range(1, _max_topups + 1):
            _gen_cands = []
            for _si in range(max(1, _sc_n)):
                _gen_agent = AnalysisAgent(
                    queries_history, result_history, custom_api_key, custom_model,
                )
                _gen_cands.append(await asyncio.to_thread(
                    _gen_agent.get_analysis,
                    queries_history[-1], _gen_tables, db_description, instructions,
                    memory_context, generation_db_type, generation_user_rules_spec,
                    generation_knowledge_spec,
                ))
            # Gate-clean EACH candidate BEFORE execution + consensus selection.
            # Self-consistency executes every candidate and picks by result-
            # consensus; a mechanically-fixable defect (e.g. asymmetric
            # `col = LOWER('X')` → folds only the literal → 0 rows) would otherwise
            # execute wrong, poison the consensus, and/or be the chosen SQL. The
            # deterministic sqlglot gates run here so all candidates are clean first.
            _cand_ctx = GateContext(db_type=generation_db_type)
            for _cand in _gen_cands:
                try:
                    _cs = _cand.get("sql_query")
                    if not _cs:
                        continue
                    _cg, _ci, _cr = run_gates(_cs, _cand_ctx)
                    if _cr and _cg:
                        _cand["sql_query"] = _cg
                        logging.info("Gate registry (candidate): %s", "; ".join(_ci)[:200])
                except Exception:  # pylint: disable=broad-exception-caught
                    continue
            if _sc_n > 1:
                def _sc_execfn(_sql, _du=db_url, _dt=generation_db_type, _lc=loader_class):
                    _ro, _ro_err = validate_read_only_sql(_sql)
                    if not _ro:
                        raise InvalidArgumentError(_ro_err)
                    return execute_with_cache(
                        lambda _q: _lc.execute_sql_query(_q, _du),
                        _sql, db_url=_du, db_type=_dt)
                answer_an = _pick_by_execution(
                    _gen_cands, _sc_execfn, generation_db_type, _resolved)
                logging.info(
                    "Generator self-consistency: %d samples -> result-consensus SQL chosen",
                    _sc_n,
                )
            else:
                answer_an = _gen_cands[0]
            _stage_log(f"GENERATOR->sql (try {_gtry})", answer_an.get("sql_query") or "(empty)")
            _widened = _readd_tables_for_missing_fields(
                answer_an.get("sql_query"), _gen_tables, tables, generation_db_type,
            )
            if len(_widened) <= len(_gen_tables):
                break  # no real-but-pruned field missing -> done
            logging.info(
                "Generator RAG top-up: attempt=%d/%d re-added %d table(s) for "
                "missing field(s); regenerating",
                _gtry, _max_topups, len(_widened) - len(_gen_tables),
            )
            _gen_tables = _widened
        base_tables = _gen_tables
    logging.info(
        "Text2SQL analysis completed: translatable=%s confidence=%s sql_chars=%d",
        answer_an.get("is_sql_translatable"),
        answer_an.get("confidence"),
        len(answer_an.get("sql_query", "") or ""),
    )

    if not tool_path_used and _is_semantic_validation_failure(answer_an):
        yield {
            "type": "reasoning_step",
            "final_response": False,
            "message": (
                "Step 1a: Semantic validation failed; re-running RAG "
                "table/column selection..."
            ),
        }
        semantic_query = _semantic_reanalysis_query(
            queries_history[-1],
            str(answer_an.get("__semantic_rejected_sql") or ""),
            str(answer_an.get("ambiguities") or answer_an.get("missing_information") or ""),
        )
        semantic_tables = base_tables
        semantic_agent = AnalysisAgent(
            [semantic_query], [], custom_api_key, custom_model,
        )
        semantic_answer = await asyncio.to_thread(
            semantic_agent.get_analysis,
            semantic_query,
            semantic_tables,
            db_description,
            instructions,
            memory_context,
            generation_db_type,
            generation_user_rules_spec,
            generation_knowledge_spec,
        )
        if semantic_answer.get("is_sql_translatable"):
            answer_an = semantic_answer
            tables = semantic_tables
            yield {
                "type": "reasoning_step",
                "final_response": False,
                "message": "Step 1a: RAG re-analysis produced semantic-valid SQL.",
            }
        else:
            logging.warning(
                "Semantic RAG re-analysis did not produce executable SQL: graph=%s "
                "missing=%s ambiguities=%s",
                sanitize_log_input(namespaced),
                sanitize_log_input(str(semantic_answer.get("missing_information", "")))[:300],
                sanitize_log_input(str(semantic_answer.get("ambiguities", "")))[:300],
            )

    # Clarify-or-execute is ATOMIC: decide BEFORE surfacing anything. First
    # the decisive pass, then either ASK (followup + stop, no execution) or
    # COMMIT and show the executed SQL below. A pre-decisive sql_query event
    # leaked the source-ambiguity to the user even when the request was then
    # answered — producing a contradictory "asked AND returned results".
    if not tool_path_used and not answer_an["is_sql_translatable"] and _decisive_retry_enabled():
        # One decisive attempt before surfacing a clarification: when schema
        # descriptions make the metric/source mapping resolvable, prefer an
        # executable answer over a question. The result still passes the full
        # deterministic gate and execution, so a wrong decisive guess cannot
        # silently return garbage identifiers.
        yield {
            "type": "reasoning_step",
            "final_response": False,
            "message": (
                "Step 1c: clarification suggested; trying one decisive "
                "generation pass before asking..."
            ),
        }
        decisive_query = (
            f"{queries_history[-1]}\n\n"
            "Decisive mode: a clarification was suggested for this request — "
            f"{' '.join(str(answer_an.get('missing_information') or answer_an.get('ambiguities') or '').split())[:500]}\n"
            "Re-read the table and column descriptions in the schema context. "
            "If the requested metric or source can be mapped to a described "
            "column of one visible table (the best description match for the "
            "requested business object), return executable SQL using it. Only "
            "keep is_sql_translatable=false if the mapping is genuinely "
            "impossible from the visible schema.\n"
            "Do NOT invent literal code/status/type values: use a text literal "
            "in a filter only when it appears in the question, the rules, or "
            "the listed sample values. Do not add subtype/type filters the "
            "question does not ask for — if the table description already "
            "matches the requested business object, take its rows as-is."
        )
        decisive_agent = AnalysisAgent(
            [decisive_query], [], custom_api_key, custom_model,
        )
        decisive_answer = await asyncio.to_thread(
            decisive_agent.get_analysis,
            decisive_query,
            tables,
            db_description,
            instructions,
            memory_context,
            generation_db_type,
            generation_user_rules_spec,
            generation_knowledge_spec,
        )
        if decisive_answer.get("is_sql_translatable") and (
            decisive_answer.get("sql_query") or ""
        ).strip():
            logging.info(
                "Decisive retry produced SQL after clarification suggestion: "
                "conf=%s",
                decisive_answer.get("confidence"),
            )
            answer_an = decisive_answer
        else:
            logging.info("Decisive retry kept the clarification outcome")

    if not answer_an["is_sql_translatable"]:
        follow_up = _direct_follow_up_from_analysis(answer_an)
        if not follow_up:
            follow_up_agent = FollowUpAgent(
                queries_history, result_history, custom_api_key, custom_model,
            )
            follow_up = await asyncio.to_thread(
                follow_up_agent.generate_follow_up_question,
                user_question=queries_history[-1],
                analysis_result=answer_an,
            )
        yield {
            "type": "followup_questions",
            "final_response": True,
            "message": follow_up,
            "missing_information": answer_an.get("missing_information", ""),
            "ambiguities": answer_an.get("ambiguities", ""),
        }
        yield _Final(_build_query_result(
            sql_query=answer_an.get("sql_query", ""),
            results=[],
            ai_response=follow_up,
            confidence=answer_an.get("confidence", 0.0),
            is_valid=False,
            execution_time=time.perf_counter() - overall_start,
            missing_information=answer_an.get("missing_information", ""),
            ambiguities=answer_an.get("ambiguities", ""),
            explanation=answer_an.get("explanation", ""),
        ))
        return

    # Auto-quote identifiers using the table set we already loaded.
    known_tables = {t[0] for t in tables} if tables else set()
    sanitized_sql, was_modified = auto_quote_sql_identifiers(
        answer_an["sql_query"], known_tables, generation_db_type,
    )
    if was_modified:
        logging.info(
            "SQL query auto-sanitized: quoted table names with special characters"
        )
        answer_an["sql_query"] = sanitized_sql

    # Deterministic parenthesize: JSON ->/->> bind looser than || and arithmetic
    # in PostgreSQL, so an unparenthesized JSON-concat raises "operator does not
    # exist: text -> unknown". The model/healer can't reliably self-parenthesize;
    # this AST pass fixes the whole class before the gate/EXPLAIN.
    _paren_sql, _paren_mod = parenthesize_json_in_operators(
        answer_an["sql_query"], generation_db_type,
    )
    if _paren_mod:
        logging.info("SQL parenthesized JSON extractions inside ||/arithmetic")
        answer_an["sql_query"] = _paren_sql

    # Deterministic JSON-leaf promotion: the linker/generator sometimes binds a
    # JSON-stored field to a flat column name (`event_name` for
    # `event_schedule->>'event_name'`). The schema has no such flat column, so the
    # gate rejects it and the repair loop can return empty SQL (0 rows). Promote a
    # bare/qualified name that is really a UNIQUE JSON leaf to its path, using the
    # schema's leaf registry — before the gate sees it.
    _flat_cols = {
        (_c.get("name") or "").lower()
        for _t in (tables or []) if isinstance(_t, (list, tuple)) and len(_t) >= 4
        for _c in (_t[3] or []) if isinstance(_c, dict)
    }
    _promo_sql, _promo_mod = promote_bare_to_json_leaf(
        answer_an["sql_query"], generation_db_type, _gate_json_paths, _flat_cols,
    )
    if _promo_mod:
        logging.info("SQL promoted bare identifier(s) to JSON leaf path(s)")
        answer_an["sql_query"] = _promo_sql

    # Deterministic GATE REGISTRY (ordered, sqlglot AST, dialect-aware, general):
    # repairs JSON key paths/operators against the schema's known keys and flags
    # joins that are not real FK edges. /sql is generate-only (no execute/heal),
    # so the deterministic repairs run here before the SQL is returned.
    _known_tables_rc = {str(t[0]) for t in (tables or []) if t and t[0]}
    _known_cols_rc = {
        str(_c.get("name")) for _t in (tables or [])
        if isinstance(_t, (list, tuple)) and len(_t) >= 4
        for _c in (_t[3] or []) if isinstance(_c, dict) and _c.get("name")
    }
    _gate_ctx = GateContext(
        db_type=generation_db_type,
        json_paths=_gate_json_paths,
        join_set=_gate_join_set,
        known_tables=_known_tables_rc,
        known_columns=_known_cols_rc,
    )
    _gated_sql, _gate_issues, _gate_repaired = run_gates(
        answer_an["sql_query"], _gate_ctx,
    )
    if _gate_repaired:
        answer_an["sql_query"] = _gated_sql
    if _gate_issues:
        logging.info("Gate registry: %s", "; ".join(_gate_issues)[:400])

    sql_query = answer_an["sql_query"]

    # (#3) Rule-gate: a focused LLM pass that applies the user's generation
    # preferences + the RAG'd business rules to the generated SQL, editing as
    # little as possible. Runs BEFORE the deterministic sqlglot gate so its
    # output is still identifier-checked. Skips itself when there is no rule text
    # to apply, and is fail-safe (returns the SQL unchanged on any error). The
    # generator no longer carries user_rules, so this is where they take effect.
    async def _run_rule_gate(current_sql: str) -> dict:
        out = {"sql_query": current_sql, "ran": False, "changed": False,
               "applied_rules": [], "unapplied_rules": []}
        if not getattr(Config, "RULE_GATE_ENABLED", True):
            return out
        if not (current_sql or "").strip():
            return out
        gate_ur = rule_gate_user_rules_spec
        # (#4) Business rules retrieved by the SELECTED columns this SQL relies
        # on (owner: "RAG business data knowing the selected columns").
        gate_br = ""
        if (getattr(Config, "RULE_GATE_ON_BUSINESS_RULES", True)
                and use_knowledge and raw_knowledge_spec):
            sel_text = selected_columns_retrieval_text(answer_an.get("schema_json"))
            sel_key = (
                f"{queries_history[-1]}\n\nSelected columns:\n{sel_text}"
                if sel_text else queries_history[-1]
            )
            try:
                gate_br = await retrieve_indexed_context(
                    namespaced, sel_key, labels=("Knowledge", "Document"),
                    top_k=int(getattr(Config, "KNOWLEDGE_RETRIEVAL_TOP_K", 8)),
                    db=db,
                ) or ""
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning("Gate business-rule retrieval skipped: %s", str(exc)[:200])
            if not gate_br:
                gate_br = generation_knowledge_spec or ""
        if not SqlGateAgent.should_run(gate_ur, gate_br):
            return out
        schema_ctx = selected_schema_compact(answer_an.get("schema_json"))
        gate = SqlGateAgent(
            queries_history, result_history, custom_api_key, custom_model,
        )
        try:
            res = await gate.apply(
                queries_history[-1], current_sql,
                user_rules=gate_ur, business_rules=gate_br,
                schema_context=schema_ctx, database_type=generation_db_type,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("Rule-gate failed (%s); keeping SQL", str(exc)[:200])
            return out
        new_sql = (res.get("sql_query") or "").strip() or current_sql
        if res.get("changed") and new_sql != (current_sql or "").strip():
            sanitized, mod = auto_quote_sql_identifiers(
                new_sql, known_tables, generation_db_type,
            )
            if mod:
                new_sql = sanitized
            out["changed"] = True
        out["sql_query"] = new_sql
        out["ran"] = True
        out["applied_rules"] = res.get("applied_rules") or []
        out["unapplied_rules"] = res.get("unapplied_rules") or []
        return out

    rule_gate_result = await _run_rule_gate(sql_query)
    if rule_gate_result["ran"]:
        sql_query = rule_gate_result["sql_query"]
        answer_an["sql_query"] = sql_query
        answer_an["rule_gate_applied"] = rule_gate_result["applied_rules"]
        answer_an["rule_gate_unapplied"] = rule_gate_result["unapplied_rules"]
        # The rule-gate is an LLM pass and can RE-INTRODUCE deterministically
        # fixable defects (e.g. case-insensitive intent rendered as asymmetric
        # `col = LOWER('X')` — folds only the literal → zero rows). Re-run the
        # sqlglot gates so the deterministic repairs are always the LAST word.
        _rg2_sql, _rg2_issues, _rg2_repaired = run_gates(sql_query, _gate_ctx)
        if _rg2_repaired:
            sql_query = _rg2_sql
            answer_an["sql_query"] = _rg2_sql
        if _rg2_issues:
            logging.info("Gate registry (post rule-gate): %s", "; ".join(_rg2_issues)[:400])
        if rule_gate_result["changed"]:
            yield {
                "type": "reasoning_step",
                "final_response": False,
                "message": "Rule-gate: applied user/business rules to the SQL.",
            }

    # Filter validation (focused agent): every WHERE/HAVING predicate must be
    # justified by the question or the plan's authorized filters; invented ones
    # (e.g. a threshold lifted from a metric-formula constant like "position<=9"
    # from the "9" in "9-position") are dropped. Filters live separately in the
    # plan JSON; this agent validates the SQL's predicates against them.
    # NOT in direct-gen: this agent validates the generator's predicates against
    # the LINKER's authorized-filter plan and drops any not in it. In direct-gen
    # we deliberately don't trust the linker plan (it can mis-locate a value —
    # e.g. it put "Hamilton" at name->>'first_name' and this agent then deleted
    # the generator's CORRECT driver_identity->>'reference' = 'hamilton', leaving
    # an unfiltered/empty result). The clean-prompt generator justifies its own
    # filters from the question + schema; let them stand.
    if (getattr(Config, "FILTER_VALIDATOR_ENABLED", True) and not _direct_gen
            and FilterValidatorAgent.should_run(sql_query)):
        _auth_lines: list[str] = []
        for _b in (_link_plan.get("filters") or []):
            if isinstance(_b, dict) and _b.get("column"):
                _auth_lines.append(
                    f"- {(_b.get('asks_for') or '').strip()}: {_b['column']}"
                    + (f" — {(_b.get('evidence') or '').strip()}" if _b.get('evidence') else "")
                )
        for _r in (_resolved or []):
            if isinstance(_r, dict) and str(_r.get("filter") or "").strip():
                _auth_lines.append(f"- {_r.get('name', '')} filter: {_r['filter']}")
        try:
            _fv = await FilterValidatorAgent(
                queries_history, result_history, custom_api_key, custom_model,
            ).validate(
                queries_history[-1], sql_query, "\n".join(_auth_lines),
                generation_db_type,
            )
            if _fv.get("changed") and (_fv.get("sql_query") or "").strip():
                sql_query = _fv["sql_query"].strip()
                answer_an["sql_query"] = sql_query
                answer_an["filter_validator_removed"] = _fv.get("removed") or []
                yield {
                    "type": "reasoning_step",
                    "final_response": False,
                    "message": "Filter validation: removed unjustified filter(s).",
                }
        except Exception as _exc:  # pylint: disable=broad-exception-caught
            logging.warning("Filter validator skipped: %s", str(_exc)[:160])

    # Deterministic sqlglot gate: parse + read-only + schema allowlist.
    # Hallucinated identifiers are rejected BEFORE execution; the precise
    # diff drives one targeted re-link + regeneration instead of guessing.
    # The allowlist is the FULL graph schema (not just the prompt candidates)
    # so a real table left out of context by recall is not falsely rejected.
    gate_allowlist = await _graph_schema_allowlist(namespaced, db=db) or tables

    def _deterministic_report(current_sql: str) -> tuple[bool, str, bool, bool]:
        """Gate (identifiers/read-only) + snapshot-grain lint, one report.

        Third element is True when the gate failure was a pure SYNTAX (parse)
        error — the healer can fix that by the error text, so the caller may
        escalate to the executor+healer instead of refusing.
        """
        problems: list[str] = []
        result = sql_gate.validate_sql(
            current_sql, generation_db_type, gate_allowlist,
        )
        had_parse_error = bool(getattr(result, "parse_error", None))
        if not result.ok:
            problems.append(result.report())
        if result.ok and sql_gate.grain_lint_enabled():
            grain_issues = sql_gate.snapshot_grain_issues(
                current_sql, generation_db_type, gate_allowlist,
            )
            if grain_issues:
                logging.warning(
                    "Snapshot-grain lint issues: %s sql=%s",
                    "; ".join(grain_issues)[:500],
                    sanitize_log_input(current_sql)[:300],
                )
                problems.extend(grain_issues)
        if result.ok:
            null_issues = sql_gate.null_output_placeholder_issues(
                current_sql, generation_db_type,
            )
            if null_issues:
                logging.warning(
                    "NULL-placeholder output issues: %s sql=%s",
                    "; ".join(null_issues)[:300],
                    sanitize_log_input(current_sql)[:300],
                )
                problems.extend(null_issues)
        if result.ok:
            # Formula fidelity: the generator must not invert a NULL-test that a
            # resolved metric fixed (weak model sometimes flips IS NULL<->IS NOT NULL,
            # silently negating the metric). Structural sqlglot-AST check; bounces a
            # targeted hint to the model rather than executing a wrong result.
            fidelity_issues = sql_gate.resolved_null_polarity_issues(
                current_sql, _resolved, generation_db_type,
            )
            if fidelity_issues:
                logging.warning(
                    "Resolved-formula fidelity issues: %s sql=%s",
                    "; ".join(fidelity_issues)[:300],
                    sanitize_log_input(current_sql)[:200],
                )
                problems.extend(fidelity_issues)
        # grain_only: schema is valid, the only problems are semantic
        # (missing date pin / NULL-padded outputs) -> a targeted repair
        # that bounces to the model, not a re-selection or a refusal.
        grain_only = bool(result.ok and problems)
        return (not problems), "\n".join(problems), had_parse_error, grain_only

    if sql_gate.gate_enabled():
        gate_ok, gate_report, gate_parse_error, gate_grain_only = _deterministic_report(sql_query)
        gate_repairs_left = sql_gate.gate_max_repairs()
        while not gate_ok and gate_repairs_left > 0:
            gate_repairs_left -= 1
            yield {
                "type": "reasoning_step",
                "final_response": False,
                "message": (
                    "Step 1b: SQL failed deterministic schema/grain validation; "
                    "re-running table/column selection..."
                ),
            }
            gate_query = (
                _gate_grain_repair_query(
                    queries_history[-1], sql_query, gate_report,
                )
                if gate_grain_only
                else _gate_reanalysis_query(
                    queries_history[-1], sql_query, gate_report,
                )
            )
            gate_tables = base_tables
            gate_agent = AnalysisAgent(
                [gate_query], [], custom_api_key, custom_model,
            )
            gate_answer = await asyncio.to_thread(
                gate_agent.get_analysis,
                gate_query,
                gate_tables,
                db_description,
                instructions,
                memory_context,
                generation_db_type,
                generation_user_rules_spec,
                generation_knowledge_spec,
            )
            if not gate_answer.get("is_sql_translatable"):
                answer_an = gate_answer
                sql_query = ""
                break
            gate_sql = gate_answer.get("sql_query", "")
            if not gate_sql.strip():
                # Model said translatable but returned no SQL — a malformed
                # repair. Burn the attempt, keep the previous SQL and report;
                # exhausted attempts end in an honest refusal, not an empty
                # statement reaching the executor.
                logging.warning(
                    "Gate repair returned translatable but empty SQL; retrying",
                )
                continue
            sanitized_gate_sql, gate_was_modified = auto_quote_sql_identifiers(
                gate_sql,
                {t[0] for t in gate_tables} if gate_tables else set(),
                generation_db_type,
            )
            if gate_was_modified:
                gate_sql = sanitized_gate_sql
                gate_answer["sql_query"] = gate_sql
            answer_an = gate_answer
            tables = gate_tables
            sql_query = gate_sql
            # Re-apply the rule-gate to the regenerated SQL so a deterministic
            # repair cannot silently drop the user/business rules (codex).
            _rg = await _run_rule_gate(sql_query)
            if _rg["ran"] and _rg["changed"]:
                sql_query = _rg["sql_query"]
                answer_an["sql_query"] = sql_query
                answer_an["rule_gate_applied"] = _rg["applied_rules"]
                answer_an["rule_gate_unapplied"] = _rg["unapplied_rules"]
            gate_ok, gate_report, gate_parse_error, gate_grain_only = _deterministic_report(sql_query)
            logging.info(
                "Post-gate-repair deterministic check: ok=%s", gate_ok,
            )

        if (not gate_ok) and sql_query and gate_parse_error:
            # Syntax-only gate failure after re-generation: escalate to the
            # executor + SQL healer instead of refusing. The EXPLAIN preflight
            # and the healer fix the specific syntax error by its message over
            # several attempts (Config.SQL_HEALING_MAX_ATTEMPTS) — more reliable
            # than from-scratch re-generation for a local mistake such as a
            # WHERE clause inside OVER(). Identifier hallucinations and grain
            # issues still refuse below; the healer's _run_sql re-validates each
            # attempt through the gate, so it cannot reach the database with
            # invented identifiers.
            logging.info(
                "Gate syntax-only failure after %d repair(s) — escalating to "
                "executor+healer instead of refusing",
                sql_gate.gate_max_repairs(),
            )
        elif not gate_ok or not sql_query:
            gate_message = (
                "Не удалось построить корректный SQL: запрос ссылается на "
                "объекты, которых нет в выбранной схеме, или не проходит "
                "детерминированную проверку.\n"
                f"{gate_report}"
                if not gate_ok
                else _direct_follow_up_from_analysis(answer_an)
                or "Не удалось построить корректный SQL по доступной схеме."
            )
            yield {
                "type": "sql_query",
                "data": "",
                "conf": 0,
                "miss": gate_message,
                "amb": answer_an.get("ambiguities", ""),
                "exp": answer_an.get("explanation", ""),
                "is_valid": False,
                "final_response": False,
            }
            yield {
                "type": "followup_questions",
                "final_response": True,
                "message": gate_message,
                "missing_information": gate_message,
                "ambiguities": answer_an.get("ambiguities", ""),
            }
            yield _Final(_build_query_result(
                sql_query=sql_query,
                results=[],
                ai_response=gate_message,
                confidence=answer_an.get("confidence", 0.0),
                is_valid=False,
                execution_time=time.perf_counter() - overall_start,
                missing_information=gate_message,
                ambiguities=answer_an.get("ambiguities", ""),
                explanation=answer_an.get("explanation", ""),
            ))
            return

    # Feature B — evidence grounding. The analysis agent justifies every
    # filter/metric column in answer_an["column_evidence"]; verify (pure sqlglot
    # AST) that each filter/metric column actually carries a justification. This
    # is advisory by default (logged + surfaced); enabling the repair flag lets
    # ONE re-grounding pass fix ungrounded filters/metrics before execution.
    evidence_issues: list = []
    if getattr(Config, "EVIDENCE_GROUNDING_ENABLED", True) and sql_query:
        evidence_issues = check_evidence_grounding(
            sql_query, answer_an.get("column_evidence"), generation_db_type,
        )
        if evidence_issues:
            logging.info(
                "Evidence grounding: %d issue(s) [%s] sql=%s",
                len(evidence_issues),
                ", ".join(sorted({i["check"] for i in evidence_issues})),
                sanitize_log_input(sql_query)[:200],
            )
        hard_hint = evidence_repair_hint(evidence_issues)
        if hard_hint and getattr(Config, "EVIDENCE_GROUNDING_REPAIR_ENABLED", False):
            yield {
                "type": "reasoning_step",
                "final_response": False,
                "message": (
                    "Step 1c: some filters/metrics are not justified by "
                    "evidence; re-grounding the query..."
                ),
            }
            ev_agent = AnalysisAgent(
                queries_history, result_history, custom_api_key, custom_model,
            )
            ev_answer = await asyncio.to_thread(
                ev_agent.get_analysis,
                queries_history[-1], base_tables, db_description,
                (instructions or "") + "\n\n" + hard_hint,
                memory_context, generation_db_type, generation_user_rules_spec,
                generation_knowledge_spec,
            )
            ev_sql = (ev_answer.get("sql_query") or "").strip()
            if ev_answer.get("is_sql_translatable") and ev_sql:
                ev_sql_q, ev_mod = auto_quote_sql_identifiers(
                    ev_sql, known_tables, generation_db_type,
                )
                if ev_mod:
                    ev_answer["sql_query"] = ev_sql_q
                    ev_sql = ev_sql_q
                ev_gate_ok = True
                if sql_gate.gate_enabled():
                    ev_gate_ok, _r, _pe, _go = _deterministic_report(ev_sql)
                if ev_gate_ok:
                    new_issues = check_evidence_grounding(
                        ev_sql, ev_answer.get("column_evidence"),
                        generation_db_type,
                    )
                    # Adopt only when the repair did not make grounding worse.
                    if len(evidence_repair_hint(new_issues)) <= len(hard_hint):
                        answer_an = ev_answer
                        sql_query = ev_sql
                        evidence_issues = new_issues
        answer_an["evidence_issues"] = evidence_issues

    # Pre-emptive "ask for a reporting date" heuristic is OFF by default: it
    # fired even when the generated SQL already constrained the date (MAX(...),
    # BETWEEN start/final, CURRENT_DATE) or used a non-perioded reference table,
    # producing false refusals. Date discipline is handled by the business
    # rules at generation time and by execution. Opt back in with
    # QW_PERIOD_CLARIFICATION_ENABLED=true.
    period_clarification = None
    if _period_clarification_enabled() and not _sql_has_date_constraint(sql_query):
        period_clarification = _missing_period_clarification(
            sql_query, queries_history[-1], tables
        )
    if period_clarification:
        yield {
            "type": "sql_query",
            "data": "",
            "conf": 0,
            "miss": period_clarification,
            "amb": period_clarification,
            "exp": "A required reporting period/date is missing for fact/snapshot tables.",
            "is_valid": False,
            "final_response": False,
        }
        yield {
            "type": "followup_questions",
            "final_response": True,
            "message": period_clarification,
            "missing_information": period_clarification,
            "ambiguities": period_clarification,
        }
        yield _Final(_build_query_result(
            sql_query="",
            results=[],
            ai_response=period_clarification,
            confidence=0.0,
            is_valid=False,
            execution_time=time.perf_counter() - overall_start,
            missing_information=period_clarification,
            ambiguities=period_clarification,
            explanation=(
                "Asked for a reporting date/period instead of aggregating "
                "fact/snapshot tables across all dates."
            ),
        ))
        return

    is_read_only, read_only_error = validate_read_only_sql(sql_query)
    if not is_read_only:
        logging.warning(
            "Blocked non-read-only generated SQL: graph=%s reason=%s sql=%s",
            sanitize_log_input(namespaced),
            sanitize_log_input(read_only_error),
            sanitize_log_input(sql_query)[:300],
        )
        yield {
            "type": "error",
            "final_response": True,
            "message": read_only_error,
        }
        yield _Final(_build_query_result(
            sql_query=sql_query,
            results=[],
            ai_response=read_only_error,
            confidence=answer_an.get("confidence", 0.0),
            is_valid=False,
            execution_time=time.perf_counter() - overall_start,
            error_message=read_only_error,
        ))
        return

    sql_type, is_destructive = detect_destructive_operation(sql_query)
    on_demo = is_general_graph(namespaced)

    # Surface the SQL that is actually about to execute (final, post-gate and
    # post-quote-normalization). The user sees the executed query — with no
    # clarification questions, because we only reach here after committing to
    # run, never on the ambiguity path (which returned above).
    # The EXECUTABLE SQL stays comment-free (what the bench extracts/runs); the
    # commented copy is rendered separately from the per-column evidence.
    # Generate-path preflight + heal. The /sql endpoint returns SQL WITHOUT
    # executing it, so a column-resolution error the static sqlglot gate cannot
    # see — a column that is valid in the schema allowlist but absent from an
    # inner CTE's own projection (e.g. a CTE that selects `drv_main` while a
    # later CTE references `drive_link`) — would otherwise leave here as
    # un-runnable SQL. A cheap EXPLAIN validates runnability without scanning;
    # on failure ONE healer loop repairs by the database's own error message,
    # each attempt re-checked read-only + through the static gate, and
    # EXPLAIN-only so nothing actually executes on this path. The downstream
    # executor's own preflight (below) then simply re-confirms the healed SQL.
    if (getattr(Config, "GENERATE_PREFLIGHT_HEAL_ENABLED", True)
            and sql_query and sql_gate.explain_preflight_enabled(db_type)):
        def _run_explain(candidate_sql: str):
            pf_read_only, pf_read_only_error = validate_read_only_sql(candidate_sql)
            if not pf_read_only:
                raise InvalidArgumentError(pf_read_only_error)
            if sql_gate.gate_enabled():
                pf_ok, pf_report, _, _ = _deterministic_report(candidate_sql)
                if not pf_ok:
                    raise InvalidArgumentError(
                        "SQL validation failed:\n" + pf_report
                    )
            return loader_class.execute_sql_query(
                f"EXPLAIN {candidate_sql.rstrip().rstrip(';')}", db_url,
            )
        try:
            await asyncio.to_thread(_run_explain, sql_query)
        except Exception as _pf_err:  # pylint: disable=broad-exception-caught
            logging.info(
                "Generate-path preflight EXPLAIN failed; healing: %s",
                sanitize_log_input(str(_pf_err))[:200],
            )
            try:
                _pf_heal = await asyncio.to_thread(
                    HealerAgent(
                        max_healing_attempts=Config.SQL_HEALING_MAX_ATTEMPTS,
                    ).heal_and_execute,
                    initial_sql=sql_query,
                    initial_error=str(_pf_err),
                    execute_sql_func=_run_explain,
                    db_description=(
                        f"{db_description}\n\n"
                        f"SCHEMA CONTEXT FOR THE FAILED QUERY:\n"
                        f"{agent_an._format_schema(tables)}"
                    ),
                    question=queries_history[-1],
                    database_type=generation_db_type,
                )
                _pf_sql = (_pf_heal.get("sql_query") or "").strip()
                if _pf_heal.get("success") and _pf_sql and _pf_sql != sql_query:
                    sql_query = _pf_sql
                    answer_an["sql_query"] = sql_query
                    yield {
                        "type": "reasoning_step",
                        "final_response": False,
                        "message": (
                            "Preflight: repaired SQL to a runnable form via the "
                            "database error message (no execution)."
                        ),
                    }
            except Exception as _pf_h_err:  # pylint: disable=broad-exception-caught
                logging.warning(
                    "Generate-path preflight heal skipped: %s",
                    sanitize_log_input(str(_pf_h_err))[:160],
                )

    # Filter-value validator + repair (codex #3) — placed at the single pre-emit
    # point so it fixes the DISPLAYED + EXECUTED SQL on every generation path
    # (incl. the blackboard path). When the SQL filters a literal on the wrong
    # column while a reachable, domain-matching column actually holds it
    # (event_name LIKE '%Italian%' vs circuits.location.country = 'Italy'), it is
    # rewritten. Deterministic detection; one repair call, only on a real mismatch.
    try:
        from api.core.filter_value_validator import (  # pylint: disable=import-outside-toplevel
            validate_and_repair_filter_values)
        _fv_graph = resolve_db(db).select_graph(namespaced)
        _fv_sql, _fv_rep = await validate_and_repair_filter_values(
            sql_query, queries_history[-1], _fv_graph, generation_db_type)
        if _fv_rep and _fv_sql:
            sql_query = _fv_sql
            answer_an["sql_query"] = _fv_sql
            logging.info("SQL filter-value corrected to the domain-matching column")
    except Exception:  # pylint: disable=broad-exception-caught
        logging.warning("filter-value validation skipped", exc_info=False)

    # Resolved-ratio enforcement (codex #3): if the resolver bound a RATIO metric
    # but the SQL aggregates only ONE component (AVG(numerator) — or even
    # AVG(denominator)), rewrite that aggregate to wrap the full bound ratio.
    # Deterministic — guarantees the formula even when the weak generator varies
    # run-to-run (observed 9.885 / 50.647 / 0.084 for the same question). Runs at
    # the single pre-emit point so it fixes the displayed + executed SQL on every
    # path.
    try:
        from api.core.ratio_formula_gate import enforce_resolved_ratio  # pylint: disable=import-outside-toplevel
        _rf_sql, _rf_rep = enforce_resolved_ratio(sql_query, _resolved, generation_db_type)
        if _rf_rep and _rf_sql:
            sql_query = _rf_sql
            answer_an["sql_query"] = _rf_sql
            logging.info("SQL rewritten to compute the resolved ratio formula")
    except Exception:  # pylint: disable=broad-exception-caught
        logging.warning("ratio-formula gate skipped", exc_info=False)

    # Deterministic gate on the SQL JUST BEFORE it is rendered + emitted to the
    # client AND executed, so the DISPLAYED and EXECUTED SQL are both the repaired
    # form (e.g. asymmetric `col = LOWER('X')` -> `LOWER(col) = LOWER('X')`).
    # This emission point precedes execution, so it is the single place that makes
    # the shown SQL and the answer consistent across every generation path.
    try:
        _eg_sql, _eg_issues, _eg_rep = run_gates(sql_query, _gate_ctx)
        if _eg_rep and _eg_sql:
            sql_query = _eg_sql
            answer_an["sql_query"] = _eg_sql
            if _eg_issues:
                logging.info("Gate registry (pre-emit): %s", "; ".join(_eg_issues)[:300])
    except Exception as _eg_exc:  # pylint: disable=broad-exception-caught
        logging.warning("pre-emit gate skipped: %s", str(_eg_exc)[:160])

    column_evidence = answer_an.get("column_evidence") or []
    sql_commented = render_commented_sql(
        sql_query, column_evidence, generation_db_type,
    )
    yield {
        "type": "sql_query",
        "data": sql_query,
        "sql_commented": sql_commented,
        "column_evidence": column_evidence,
        "evidence_issues": answer_an.get("evidence_issues") or [],
        "schema_json": answer_an.get("schema_json") or {},
        "conf": answer_an.get("confidence", 0),
        "miss": "",
        "amb": "",
        "exp": answer_an.get("explanation", ""),
        "is_valid": True,
        "final_response": False,
    }

    if is_destructive and on_demo:
        yield {
            "type": "error",
            "final_response": True,
            "message": "Destructive operation not allowed on demo graphs",
        }
        yield _Final(_build_query_result(
            sql_query=sql_query, results=[],
            ai_response="Destructive operation not allowed on demo graphs",
            confidence=answer_an.get("confidence", 0.0),
            is_valid=True, is_destructive=True,
            execution_time=time.perf_counter() - overall_start,
            error_message="Destructive operation not allowed on demo graphs",
        ))
        return

    if is_destructive:
        confirmation_msg = build_destructive_confirmation_message(sql_type, sql_query)
        yield {
            "type": "destructive_confirmation",
            "message": confirmation_msg,
            "sql_query": sql_query,
            "operation_type": sql_type,
            "final_response": False,
        }
        yield _Final(_build_query_result(
            sql_query=sql_query, results=[], ai_response=confirmation_msg,
            confidence=answer_an.get("confidence", 0.0),
            is_valid=True, is_destructive=True, requires_confirmation=True,
            execution_time=time.perf_counter() - overall_start,
        ))
        return

    yield {
        "type": "reasoning_step",
        "final_response": False,
        "message": "Step 2: Executing SQL query against database...",
    }
    logging.info("Text2SQL stage started: executing SQL against %s", db_type)

    # FINAL deterministic gate pass — the LAST word on the SQL before execution.
    # Steps after the first gate (rule-gate, candidate selection, knowledge-driven
    # rewrites) can re-introduce deterministically-fixable defects, e.g. asymmetric
    # case-folding `col = LOWER('X')` (folds only the literal → zero rows). Re-run
    # the sqlglot gates so the EXECUTED and RETURNED SQL is always gate-clean.
    try:
        _fg_sql, _fg_issues, _fg_repaired = run_gates(sql_query, _gate_ctx)
        if _fg_repaired:
            sql_query = _fg_sql
            answer_an["sql_query"] = _fg_sql
        if _fg_issues:
            logging.info("Gate registry (final pre-exec): %s", "; ".join(_fg_issues)[:400])
    except Exception as _fg_exc:  # pylint: disable=broad-exception-caught
        logging.warning("final pre-exec gate skipped: %s", str(_fg_exc)[:160])

    is_schema_modifying, operation_type = check_schema_modification(sql_query, loader_class)

    execution_error_msg = None
    query_results: list = []
    user_readable_response = ""

    try:
        try:
            # Cheap EXPLAIN preflight: dialect/identifier errors surface here
            # and flow into the standard repair path without a heavy scan.
            if sql_gate.explain_preflight_enabled(db_type):
                await asyncio.to_thread(
                    loader_class.execute_sql_query,
                    f"EXPLAIN {sql_query}",
                    db_url,
                )
            query_results = await asyncio.to_thread(
                execute_with_cache,
                lambda sql: loader_class.execute_sql_query(sql, db_url),
                sql_query,
                db_url=db_url,
                db_type=db_type,
            )
        except Exception as exec_error:  # pylint: disable=broad-exception-caught
            schema_reanalysis_succeeded = False
            healer_initial_sql = sql_query
            healer_initial_error = str(exec_error)

            if _is_schema_execution_error(str(exec_error)):
                yield {
                    "type": "reasoning_step",
                    "final_response": False,
                    "message": (
                        "Step 2a: SQL failed on schema context; re-running "
                        "RAG table/column selection..."
                    ),
                }
                repair_query = _schema_reanalysis_query(
                    queries_history[-1], sql_query, str(exec_error)
                )
                repair_tables = base_tables
                repair_agent = AnalysisAgent(
                    [repair_query], [], custom_api_key, custom_model,
                )
                repair_answer = await asyncio.to_thread(
                    repair_agent.get_analysis,
                    repair_query,
                    repair_tables,
                    db_description,
                    instructions,
                    memory_context,
                    generation_db_type,
                    generation_user_rules_spec,
                    generation_knowledge_spec,
                )
                yield {
                    "type": "sql_query",
                    "data": repair_answer.get("sql_query", ""),
                    "conf": repair_answer.get("confidence", 0),
                    "miss": repair_answer.get("missing_information", ""),
                    "amb": repair_answer.get("ambiguities", ""),
                    "exp": repair_answer.get("explanation", ""),
                    "is_valid": repair_answer.get("is_sql_translatable", False),
                    "final_response": False,
                }

                if not repair_answer.get("is_sql_translatable"):
                    follow_up = _direct_follow_up_from_analysis(repair_answer)
                    if not follow_up:
                        follow_up_agent = FollowUpAgent(
                            [repair_query], [], custom_api_key, custom_model,
                        )
                        follow_up = await asyncio.to_thread(
                            follow_up_agent.generate_follow_up_question,
                            user_question=queries_history[-1],
                            analysis_result=repair_answer,
                        )
                    yield {
                        "type": "followup_questions",
                        "final_response": True,
                        "message": follow_up,
                        "missing_information": repair_answer.get("missing_information", ""),
                        "ambiguities": repair_answer.get("ambiguities", ""),
                    }
                    yield _Final(_build_query_result(
                        sql_query=repair_answer.get("sql_query", ""),
                        results=[],
                        ai_response=follow_up,
                        confidence=repair_answer.get("confidence", 0.0),
                        is_valid=False,
                        execution_time=time.perf_counter() - overall_start,
                        missing_information=repair_answer.get("missing_information", ""),
                        ambiguities=repair_answer.get("ambiguities", ""),
                        explanation=repair_answer.get("explanation", ""),
                    ))
                    return

                repair_sql = repair_answer.get("sql_query", "")
                sanitized_repair_sql, repair_was_modified = auto_quote_sql_identifiers(
                    repair_sql,
                    {t[0] for t in repair_tables} if repair_tables else set(),
                    generation_db_type,
                )
                if repair_was_modified:
                    repair_sql = sanitized_repair_sql
                    repair_answer["sql_query"] = repair_sql
                repair_read_only, repair_read_only_error = validate_read_only_sql(repair_sql)
                if not repair_read_only:
                    raise InvalidArgumentError(repair_read_only_error)
                if sql_gate.gate_enabled():
                    repair_gate = sql_gate.validate_sql(
                        repair_sql, generation_db_type,
                        gate_allowlist or repair_tables,
                    )
                    sql_gate.log_gate_result(
                        "schema-repair", repair_gate, repair_sql,
                    )
                    if not repair_gate.ok:
                        # The report wording matches the schema-error regex, so
                        # control flows to the no-guessing refusal branch below.
                        raise RuntimeError(
                            "SQL validation failed:\n" + repair_gate.report()
                        )

                try:
                    query_results = await asyncio.to_thread(
                        execute_with_cache,
                        lambda sql: loader_class.execute_sql_query(sql, db_url),
                        repair_sql,
                        db_url=db_url,
                        db_type=db_type,
                    )
                    answer_an = repair_answer
                    tables = repair_tables
                    sql_query = repair_sql
                    is_schema_modifying, operation_type = check_schema_modification(
                        sql_query, loader_class
                    )
                    schema_reanalysis_succeeded = True
                    yield {
                        "type": "reasoning_step",
                        "final_response": False,
                        "message": (
                            "Step 2a: RAG re-analysis produced executable SQL."
                        ),
                    }
                except Exception as repair_exec_error:  # pylint: disable=broad-exception-caught
                    if _is_schema_execution_error(str(repair_exec_error)):
                        yield {
                            "type": "healing_failed",
                            "final_response": False,
                            "message": (
                                "❌ RAG re-analysis still produced a schema-invalid "
                                "query; SQL healer was skipped to avoid guessing "
                                "columns or tables."
                            ),
                            "final_error": str(repair_exec_error),
                        }
                        raise repair_exec_error
                    healer_initial_sql = repair_sql
                    healer_initial_error = str(repair_exec_error)

            if not schema_reanalysis_succeeded:
                yield {
                    "type": "reasoning_step",
                    "final_response": False,
                    "message": "Step 2a: SQL execution failed, attempting to heal query...",
                }
                healer = HealerAgent(max_healing_attempts=Config.SQL_HEALING_MAX_ATTEMPTS)

                def _run_sql(sql: str):
                    healed_read_only, healed_read_only_error = validate_read_only_sql(sql)
                    if not healed_read_only:
                        raise InvalidArgumentError(healed_read_only_error)
                    if sql_gate.gate_enabled():
                        healed_gate = sql_gate.validate_sql(
                            sql, generation_db_type, gate_allowlist or tables,
                        )
                        if not healed_gate.ok:
                            # Precise identifier feedback for the next healing
                            # attempt without burning a database round-trip.
                            raise InvalidArgumentError(
                                "SQL validation failed:\n" + healed_gate.report()
                            )
                    return execute_with_cache(
                        lambda query: loader_class.execute_sql_query(query, db_url),
                        sql,
                        db_url=db_url,
                        db_type=db_type,
                    )

                healing_result = await asyncio.to_thread(
                    healer.heal_and_execute,
                    initial_sql=healer_initial_sql,
                    initial_error=healer_initial_error,
                    execute_sql_func=_run_sql,
                    db_description=(
                        f"{db_description}\n\n"
                        f"USER RULES & SPECIFICATIONS:\n"
                        f"{user_rules_spec or 'No user rules provided.'}\n\n"
                        f"SCHEMA CONTEXT FOR THE FAILED QUERY:\n"
                        f"{agent_an._format_schema(tables)}"
                    ),
                    question=queries_history[-1],
                    database_type=generation_db_type,
                )

                if not healing_result.get("success"):
                    yield {
                        "type": "healing_failed",
                        "final_response": False,
                        "message": (
                            f"❌ Failed to heal query after "
                            f"{healing_result.get('attempts', 0)} attempt(s)"
                        ),
                        "final_error": healing_result.get("final_error", str(exec_error)),
                    }
                    raise exec_error

                sql_query = healing_result["sql_query"]
                healed_read_only, healed_read_only_error = validate_read_only_sql(sql_query)
                if not healed_read_only:
                    raise InvalidArgumentError(healed_read_only_error)
                answer_an["sql_query"] = sql_query
                query_results = healing_result["query_results"]

                yield {
                    "type": "healing_success",
                    "final_response": False,
                    "message": (
                        f"✅ Query healed and executed successfully after "
                        f"{healing_result.get('attempts', 0)} attempt(s)"
                    ),
                    "healed_sql": sql_query,
                    "attempts": healing_result.get("attempts", 0),
                }

        yield {
            "type": "query_result",
            "data": query_results,
            "final_response": False,
        }

        if is_schema_modifying:
            async for ev in _emit_schema_refresh(
                loader_class, namespaced, db_url, operation_type,
                db=db, mark_final_response=True,
            ):
                yield ev

        step_num = "4" if is_schema_modifying else "3"
        yield {
            "type": "reasoning_step",
            "final_response": False,
            "message": f"Step {step_num}: Generating user-friendly response",
        }
        logging.info("Text2SQL stage started: formatting user response")

        if getattr(Config, "RESPONSE_FORMATTER_ENABLED", True):
            user_readable_response = await asyncio.to_thread(
                format_ai_response,
                queries_history=queries_history,
                result_history=result_history,
                sql_query=sql_query,
                query_results=query_results,
                db_description=db_description,
                custom_api_key=custom_api_key,
                custom_model=custom_model,
            )
        else:
            logging.info("Text2SQL response formatter skipped by configuration")
            user_readable_response = _fast_response(sql_query, query_results)

        yield {
            "type": "ai_response",
            "final_response": True,
            "message": user_readable_response,
        }
    except Exception as e:  # pylint: disable=broad-exception-caught
        execution_error_msg = str(e)
        logging.exception("Error executing SQL query")  # nosemgrep
        yield {
            "type": "error",
            "final_response": True,
            "message": "Error executing SQL query",
            "error_detail": execution_error_msg,
            "error_class": e.__class__.__name__,
            "sql_query": sql_query,
            "stage": "execute_sql",
            "database_type": db_type,
        }
        if not user_readable_response:
            user_readable_response = f"Error executing SQL query: {execution_error_msg}"

    if memory_tool is not None:
        full_response = {
            "question": queries_history[-1],
            "generated_sql": answer_an.get("sql_query", ""),
            "query_results_preview": _query_results_preview(query_results),
            "answer": user_readable_response,
            "success": execution_error_msg is None,
        }
        if execution_error_msg:
            full_response["error"] = execution_error_msg
        save_memory_background(
            memory_tool=memory_tool,
            question=queries_history[-1],
            sql_query=answer_an.get("sql_query", ""),
            success=execution_error_msg is None,
            error=execution_error_msg or "",
            full_response=full_response,
            chat_histories=[raw_queries_history, result_history],
        )

    yield _Final(_build_query_result(
        sql_query=answer_an.get("sql_query", ""),
        results=query_results if execution_error_msg is None else [],
        ai_response=user_readable_response,
        confidence=answer_an.get("confidence", 0.0),
        is_valid=True,
        is_destructive=is_destructive,
        execution_time=time.perf_counter() - overall_start,
        missing_information=answer_an.get("missing_information", ""),
        ambiguities=answer_an.get("ambiguities", ""),
        explanation=answer_an.get("explanation", ""),
        error_message=execution_error_msg,
        sql_commented=render_commented_sql(
            answer_an.get("sql_query", ""),
            answer_an.get("column_evidence"),
            generation_db_type,
        ),
        column_evidence=answer_an.get("column_evidence") or [],
        evidence_issues=answer_an.get("evidence_issues") or [],
        schema_json=answer_an.get("schema_json") or {},
    ))


async def run_confirmed(  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    user_id: str,
    graph_id: str,
    confirm_data: Any,
    db: Optional["FalkorDB"] = None,
) -> AsyncGenerator[Union[dict, _Final], None]:
    """Execute a user-confirmed destructive SQL operation.

    Same wire-format-+-_Final shape as ``run_query``. Confirmed destructive
    queries are NOT auto-healed (we just confirmed *this* SQL, not a healed
    variant), so this path skips the healer entirely.
    """
    overall_start = time.perf_counter()
    namespaced = graph_name(user_id, graph_id)

    if is_general_graph(namespaced):
        # Match streaming refusal: even an explicit CONFIRM cannot run writes
        # on a demo graph.
        raise InvalidArgumentError(
            "Destructive operations are not allowed on demo graphs"
        )

    confirmation = (getattr(confirm_data, "confirmation", "") or "").strip().upper()
    sql_query = getattr(confirm_data, "sql_query", "") or ""
    queries_history = getattr(confirm_data, "chat", []) or []
    custom_api_key = getattr(confirm_data, "custom_api_key", None)
    custom_model = getattr(confirm_data, "custom_model", None)
    validate_custom_model(custom_model)

    if not sql_query:
        raise InvalidArgumentError("No SQL query provided")

    is_read_only, read_only_error = validate_read_only_sql(sql_query)
    if not is_read_only:
        logging.warning(
            "Blocked non-read-only confirmed SQL before confirmation: graph=%s reason=%s sql=%s",
            sanitize_log_input(namespaced),
            sanitize_log_input(read_only_error),
            sanitize_log_input(sql_query)[:300],
        )
        yield {
            "type": "error",
            "final_response": True,
            "message": read_only_error,
        }
        yield _Final(_build_query_result(
            sql_query=sql_query,
            results=[],
            ai_response=read_only_error,
            is_valid=False,
            is_destructive=True,
            execution_time=time.perf_counter() - overall_start,
            error_message=read_only_error,
        ))
        return

    question = (
        queries_history[-1] if queries_history else "Destructive operation confirmation"
    )

    if confirmation != "CONFIRM":
        yield {
            "type": "operation_cancelled",
            "message": (
                "Operation cancelled. The destructive SQL query was not executed."
            ),
        }
        yield _Final(_build_query_result(
            sql_query=sql_query, results=[],
            ai_response="Operation cancelled. The destructive SQL query was not executed.",
            is_valid=True, is_destructive=True,
            execution_time=time.perf_counter() - overall_start,
        ))
        return

    use_memory = bool(getattr(confirm_data, "use_memory", False))
    memory_tool = None
    execution_error_msg = None
    user_readable_response = ""
    query_results: list = []

    try:
        # Only create the MemoryTool when the caller asks for it. graphiti_core
        # is in the [server] extra; SDK installs without it would otherwise
        # ImportError here at runtime.
        if use_memory:
            memory_tool = await _create_memory_tool(user_id, namespaced, db=db)
        db_description, db_url = await get_db_description(namespaced, db=db)
        db_type, loader_class = get_database_type_and_loader(db_url)

        if not loader_class:
            yield {"type": "error", "message": "Unable to determine database type"}
            yield _Final(_build_query_result(
                sql_query=sql_query, results=[],
                ai_response="Unable to determine database type",
                is_valid=False, is_destructive=True,
                execution_time=time.perf_counter() - overall_start,
                error_message="Unable to determine database type",
            ))
            return

        yield {"type": "reasoning_step",
               "message": "Step 2: Executing confirmed SQL query"}

        sql_query, was_modified = await quote_identifiers_from_graph(
            sql_query=sql_query, graph_id=namespaced, db_type=db_type, db=db,
        )
        if was_modified:
            logging.info("Confirmed SQL query auto-sanitized")

        is_read_only, read_only_error = validate_read_only_sql(sql_query)
        if not is_read_only:
            raise InvalidArgumentError(read_only_error)

        is_schema_modifying, operation_type = check_schema_modification(
            sql_query, loader_class,
        )
        query_results = await asyncio.to_thread(
            execute_with_cache,
            lambda sql: loader_class.execute_sql_query(sql, db_url),
            sql_query,
            db_url=db_url,
            db_type=db_type,
        )
        yield {"type": "query_result", "data": query_results}

        if is_schema_modifying:
            async for ev in _emit_schema_refresh(
                loader_class, namespaced, db_url, operation_type, db=db,
            ):
                yield ev

        step_num = "4" if is_schema_modifying else "3"
        yield {"type": "reasoning_step",
               "message": f"Step {step_num}: Generating user-friendly response"}

        user_readable_response = await asyncio.to_thread(
            format_ai_response,
            queries_history=queries_history or [question],
            result_history=None,
            sql_query=sql_query,
            query_results=query_results,
            db_description=db_description,
            custom_api_key=custom_api_key,
            custom_model=custom_model,
        )

        yield {"type": "ai_response", "message": user_readable_response}

    except Exception as e:  # pylint: disable=broad-exception-caught
        # Wraps both MemoryTool.create failures and driver-specific execution errors.
        execution_error_msg = str(e) or "Error executing query"
        logging.exception("Error executing confirmed SQL query")  # nosemgrep
        yield {
            "type": "error",
            "message": "Error executing SQL query",
            "error_detail": execution_error_msg,
            "error_class": e.__class__.__name__,
            "sql_query": sql_query,
            "stage": "execute_confirmed_sql",
            "database_type": db_type,
        }
        if not user_readable_response:
            user_readable_response = execution_error_msg

    if memory_tool is not None:
        full_response = {
            "question": question,
            "generated_sql": sql_query,
            "query_results_preview": _query_results_preview(query_results),
            "answer": user_readable_response,
            "success": execution_error_msg is None,
        }
        if execution_error_msg:
            full_response["error"] = execution_error_msg
        save_memory_background(
            memory_tool=memory_tool,
            question=question,
            sql_query=sql_query,
            success=execution_error_msg is None,
            error=execution_error_msg or "",
            full_response=full_response,
            chat_histories=[queries_history or [question], []],
        )

    yield _Final(_build_query_result(
        sql_query=sql_query,
        results=query_results if execution_error_msg is None else [],
        ai_response=user_readable_response,
        is_valid=True, is_destructive=True,
        execution_time=time.perf_counter() - overall_start,
        error_message=execution_error_msg,
    ))



async def _resolve_refresh_target(
    user_id: str, graph_id: str, db: Optional["FalkorDB"] = None,
) -> tuple[str, str]:
    """Validate refresh prerequisites and return ``(namespaced, db_url)``.

    Raises:
        InvalidArgumentError: For demo graphs, which are read-only.
        InternalError: When no source URL is on record for the graph.
    """
    namespaced = graph_name(user_id, graph_id)
    if is_general_graph(namespaced):
        raise InvalidArgumentError("Demo graphs cannot be refreshed")

    _, db_url = await get_db_description(namespaced, db=db)
    if not db_url or db_url == "No URL available for this database.":
        raise InternalError("No database URL found for this graph")

    return namespaced, db_url


# In-flight re-index jobs keyed by namespaced graph. The rebuild runs as a
# DETACHED task so a client disconnect (which cancels only the streaming
# generator) can never abort the mutation mid-way. The previous graph is
# GRAPH.COPY-backed-up first and restored on any failure, so an interrupted or
# failed rebuild never leaves a corrupted/partial graph.
_REINDEX_JOBS: "dict[str, asyncio.Task]" = {}


def _reindex_event(event_type: str, message: str) -> str:
    return json.dumps({"type": event_type, "message": message}) + MESSAGE_DELIMITER


def _reindex_backup_name(namespaced: str) -> str:
    return f"{namespaced}__reindex_backup"


async def _reindex_marker_set(db, namespaced: str) -> None:
    try:
        await resolve_db(db).connection.execute_command(
            "SET", f"t2s:reindex:{namespaced}", "running")
    except Exception:  # pylint: disable=broad-exception-caught
        pass


async def _reindex_marker_clear(db, namespaced: str) -> None:
    try:
        await resolve_db(db).connection.execute_command(
            "DEL", f"t2s:reindex:{namespaced}")
    except Exception:  # pylint: disable=broad-exception-caught
        pass


async def _reindex_marker_present(db, namespaced: str) -> bool:
    try:
        return bool(await resolve_db(db).connection.execute_command(
            "EXISTS", f"t2s:reindex:{namespaced}"))
    except Exception:  # pylint: disable=broad-exception-caught
        return False


async def _reindex_worker(
    namespaced, db_url, user_id, db,
    saved_knowledge, saved_user_rules, saved_documents, queue,
):
    """Detached rebuild: backup -> drop -> re-pull -> restore. On ANY failure,
    roll back to the backup so the live graph is never left partial. Progress is
    pushed to ``queue``; a ``None`` sentinel signals completion. This is NOT the
    request generator, so a client disconnect cannot cancel it."""
    backup = _reindex_backup_name(namespaced)
    try:
        # 1. Back up the live graph so an interrupted/failed rebuild can restore.
        await queue.put(_reindex_event(
            "reasoning_step", "Backing up the current graph before re-index..."))
        await copy_graph(namespaced, backup, db=db)
        await _reindex_marker_set(db, namespaced)  # live graph now untrustworthy
        # 2. Drop + re-pull the schema from the source DB (re-embeds it).
        await drop_graph(namespaced, db=db)
        async for chunk in await load_database(db_url, user_id, db=db):
            await queue.put(chunk)
        # 3. Restore knowledge / rules / uploaded :Document schemas (re-embedded
        #    with the CURRENT model).
        if (saved_knowledge or "").strip():
            try:
                await set_knowledge(namespaced, saved_knowledge, db=db, append=False)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning("Re-index: could not restore knowledge: %s", str(exc)[:200])
        if (saved_user_rules or "").strip():
            try:
                await set_user_rules(namespaced, saved_user_rules, db=db)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning("Re-index: could not restore user rules: %s", str(exc)[:200])
        restored_docs = 0
        for _src, _text in (saved_documents or {}).items():
            if not (_text or "").strip():
                continue
            try:
                await index_text_chunks(namespaced, "Document", _text, _src, db=db)
                restored_docs += 1
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning("Re-index: could not restore document (source=%s): %s",
                                _src, str(exc)[:200])
        # 4. Success: the live graph is good again -> clear marker, drop backup.
        await _reindex_marker_clear(db, namespaced)
        await drop_graph(backup, db=db)
        await queue.put(_reindex_event(
            "info",
            "Re-index complete; schema, knowledge, rules"
            + (", and uploaded schemas" if restored_docs else "")
            + " re-embedded with the current model."))
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.error("Re-index failed; rolling back from backup: %s", str(exc)[:300])
        try:
            await drop_graph(namespaced, db=db)
            await copy_graph(backup, namespaced, db=db)
            await drop_graph(backup, db=db)
            await _reindex_marker_clear(db, namespaced)
        except Exception as rb:  # pylint: disable=broad-exception-caught
            logging.error("Re-index rollback failed: %s", str(rb)[:300])
        await queue.put(_reindex_event(
            "error", "Re-index failed; the previous graph was restored."))
    finally:
        await queue.put(None)
        _REINDEX_JOBS.pop(namespaced, None)


async def refresh_database_schema(user_id: str, graph_id: str, db=None):
    """Full, crash-safe re-index of a database graph.

    Runs as a DETACHED background task that GRAPH.COPY-backs-up the live graph,
    then DROPs + re-pulls Table/Column/Database from the source DB via
    ``load_database`` and re-applies the stored business knowledge / user-rules /
    uploaded ``:Document`` schemas — all re-embedded with the CURRENT embedding
    model (so a changed model/dimension is fixed too). Because the rebuild is
    detached, a client disconnect cannot abort it; on ANY failure it rolls back
    from the backup, so the graph is never left partial. One rebuild per graph
    at a time. To wipe a DB entirely instead, delete the graph.

    Returns a streaming progress generator (wire-format), same as before.
    """
    try:
        # Validate prerequisites BEFORE any action (raises for demo graphs and
        # for graphs with no source URL on record).
        namespaced, db_url = await _resolve_refresh_target(user_id, graph_id, db=db)
        backup = _reindex_backup_name(namespaced)

        # Crash recovery: a leftover backup + marker (with no running job) means a
        # prior rebuild died mid-way -> the live graph may be partial; restore it.
        if (namespaced not in _REINDEX_JOBS
                and await _reindex_marker_present(db, namespaced)
                and await graph_exists(backup, db=db)):
            logging.warning(
                "Re-index: detected an interrupted rebuild; restoring %s from backup",
                sanitize_log_input(namespaced))
            await drop_graph(namespaced, db=db)
            await copy_graph(backup, namespaced, db=db)
            await drop_graph(backup, db=db)
            await _reindex_marker_clear(db, namespaced)
        elif (namespaced not in _REINDEX_JOBS
                and await graph_exists(backup, db=db)):
            # Stale backup (a prior success that failed to clean up) -> drop it.
            await drop_graph(backup, db=db)

        # Concurrency guard: one rebuild per graph at a time.
        existing = _REINDEX_JOBS.get(namespaced)
        if existing is not None and not existing.done():
            async def _busy():
                yield _reindex_event(
                    "error",
                    "A re-index is already running for this database; please wait.")
            return _busy()

        # Snapshot knowledge / rules / uploaded :Document schemas before the drop.
        try:
            saved_knowledge = await get_knowledge(namespaced, db=db)
        except Exception:  # pylint: disable=broad-exception-caught
            saved_knowledge = ""
        try:
            saved_user_rules = await get_user_rules(namespaced, db=db)
        except Exception:  # pylint: disable=broad-exception-caught
            saved_user_rules = ""
        try:
            saved_documents = await get_document_sources(namespaced, db=db)
        except Exception:  # pylint: disable=broad-exception-caught
            saved_documents = {}

        # Kick off the DETACHED rebuild and stream its progress from a queue. A
        # client disconnect cancels only this generator, never the rebuild task.
        queue: "asyncio.Queue" = asyncio.Queue()
        task = asyncio.create_task(_reindex_worker(
            namespaced, db_url, user_id, db,
            saved_knowledge, saved_user_rules, saved_documents, queue))
        _REINDEX_JOBS[namespaced] = task

        async def generate():
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield item

        return generate()
    except (InvalidArgumentError, InternalError):
        raise
    except Exception as e:
        logging.error("Error in refresh_graph_schema: %s", str(e))
        raise InternalError("Internal server error while refreshing schema") from e


async def refresh_schema_for_sdk(
    user_id: str, graph_id: str, db: Optional["FalkorDB"] = None,
) -> RefreshResult:
    """SDK-facing schema refresh that returns a structured ``RefreshResult``.

    The streaming ``refresh_database_schema`` returns a wire-format generator;
    SDK callers want a single dataclass back. Both share the same underlying
    reload via ``load_database_sync``.
    """
    # Lazy import to break the circular dep with schema_loader.
    from api.core.schema_loader import load_database_sync  # pylint: disable=import-outside-toplevel

    try:
        _, db_url = await _resolve_refresh_target(user_id, graph_id, db=db)
    except InternalError as e:
        # SDK contract is to return a RefreshResult, not raise, when the URL
        # is missing. InvalidArgumentError (demo graph) still propagates.
        return RefreshResult(success=False, message=str(e))

    try:
        connection_result = await load_database_sync(db_url, user_id, db=db)
        return RefreshResult(
            success=connection_result.success,
            message=connection_result.message,
        )
    except (RedisError, ConnectionError, OSError) as e:
        logging.error("Error refreshing schema: %s", str(e))
        return RefreshResult(
            success=False,
            message=f"Failed to refresh schema: {str(e)}",
        )


async def delete_database(user_id: str, graph_id: str, db=None):
    """Delete the specified graph (namespaced to the user).

    This will attempt to delete the FalkorDB graph belonging to the
    authenticated user. The graph id used by the client is stripped of
    namespace and will be namespaced using the user's id from the request
    state.

    Wipes ALL data bound to the DB (R4): one DB == one FalkorDB graph, and
    ``graph.delete()`` issues ``GRAPH.DELETE`` which drops the entire graph
    keyspace in a single operation — every label (Table, Column, Database,
    BusinessRules, and the retrieval nodes Knowledge / UserRuleChunk / Document)
    and all relationships go with it. No label survives, so no partial-delete
    cleanup is required.
    """
    namespaced = graph_name(user_id, graph_id)
    if is_general_graph(graph_id):
        raise InvalidArgumentError("Demo graphs cannot be deleted")

    try:
        # Select and delete the graph using the FalkorDB client API.
        # GRAPH.DELETE removes the whole graph (all labels + relationships),
        # including the additive retrieval nodes (Knowledge/UserRuleChunk/Document).
        graph = resolve_db(db).select_graph(namespaced)
        await graph.delete()
        return {"success": True, "graph": graph_id}
    except ResponseError as re:
        raise GraphNotFoundError("Failed to delete graph, Graph not found") from re
    except (RedisError, ConnectionError) as e:
        logging.exception("Failed to delete graph %s: %s", sanitize_log_input(namespaced), e)
        raise InternalError("Failed to delete graph") from e
    except Exception as e:  # pylint: disable=broad-exception-caught
        # Catch-all so any future driver-specific exception is wrapped into
        # a consistent API/SDK error contract instead of leaking as a 500.
        logging.exception(
            "Unexpected error deleting graph %s: %s", sanitize_log_input(namespaced), e,
        )
        raise InternalError("Failed to delete graph") from e
