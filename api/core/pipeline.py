"""Shared logic for text2sql streaming and SDK (sync) paths.

This module contains pure functions and constants extracted from
``text2sql.py`` (canonical source) so that both the streaming API and the
SDK non-streaming path stay in sync.
"""

import asyncio
import contextvars
import logging
import os
import re
from typing import Any, Optional, Type

from api.agents import ResponseFormatterAgent
from api.config import Config
from api.core.db_resolver import resolve_db
from api.core.errors import InvalidArgumentError
from api.loaders.postgres_loader import PostgresLoader
from api.loaders.mysql_loader import MySQLLoader
from api.loaders.impala_loader import ImpalaLoader
from api.loaders.yaml_loader import YamlSchemaLoader
from api.loaders.base_loader import BaseLoader
from api.sql_utils import SQLIdentifierQuoter, DatabaseSpecificQuoter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Delimiter used by the streaming route to frame JSON messages on the wire.
# Kept here so any caller that composes streaming payloads pulls the single
# source of truth rather than redefining it.
MESSAGE_DELIMITER = "|||FALKORDB_MESSAGE_BOUNDARY|||"

GENERAL_PREFIX = os.getenv("GENERAL_PREFIX")

# Verb → user-facing description for destructive operations. Single source of
# truth for both ``DESTRUCTIVE_OPS`` (membership test) and the confirmation
# message builder, so adding a verb in one place can't drift from the other.
_DESTRUCTIVE_VERBS = {
    'INSERT': 'Add new data to the database',
    'UPDATE': 'Modify existing data in the database',
    'DELETE': '**PERMANENTLY DELETE** data from the database',
    'DROP': '**PERMANENTLY DELETE** entire tables or database objects',
    'CREATE': 'Create new tables or database objects',
    'ALTER': 'Modify the structure of existing tables',
    'TRUNCATE': '**PERMANENTLY DELETE ALL DATA** from specified tables',
}

DESTRUCTIVE_OPS = frozenset(_DESTRUCTIVE_VERBS)
READ_ONLY_START_OPS = frozenset({"SELECT", "WITH"})
WRITE_OR_ADMIN_OPS = frozenset({
    "INSERT", "UPDATE", "DELETE", "MERGE", "DROP", "CREATE", "ALTER",
    "TRUNCATE", "REPLACE", "GRANT", "REVOKE", "COPY", "LOAD", "UNLOAD",
    "REFRESH", "INVALIDATE", "MSCK", "ANALYZE", "VACUUM", "SET", "RESET",
    "CALL",
})
WRITE_OR_ADMIN_RE = re.compile(
    r"(^|[;(]\s*)("
    + "|".join(sorted(WRITE_OR_ADMIN_OPS))
    + r")\b",
    re.IGNORECASE,
)

# Contextvar-scoped task sink. SDK code sets this for the duration of a
# query/execute call so ``save_memory_background`` (fire-and-forget) can
# be awaited at ``T2SClient.close()`` time. Unset in server contexts,
# where the event loop outlives the query and tasks drain naturally.
background_tasks_var: contextvars.ContextVar[Optional[set]] = (
    contextvars.ContextVar("t2s_background_tasks", default=None)
)

# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


def graph_name(user_id: str, graph_id: str) -> str:
    """Return the namespaced graph name.

    Applies validation identical to the original ``_graph_name`` in
    ``text2sql.py``: strip, truncate to 200 chars, reject empty, bypass
    prefix for general/demo graphs.

    Raises:
        InvalidArgumentError: If *graph_id* is empty after stripping.
    """
    graph_id = graph_id.strip()[:200]
    if not graph_id:
        # Bad input is a 400, not a 404 — several callers map
        # InvalidArgumentError → 400 in the HTTP layer.
        raise InvalidArgumentError(
            "Invalid graph_id, must be a non-empty string up to 200 characters."
        )

    if GENERAL_PREFIX and graph_id.startswith(GENERAL_PREFIX):
        return graph_id

    return f"{user_id}_{graph_id}"


def is_general_graph(graph_id: str) -> bool:
    """Return ``True`` when *graph_id* belongs to a demo/general graph."""
    return bool(GENERAL_PREFIX and graph_id.startswith(GENERAL_PREFIX))


# ---------------------------------------------------------------------------
# Database type detection
# ---------------------------------------------------------------------------


def get_database_type_and_loader(
    db_url: str,
    *,
    sdk_only: bool = False,
) -> tuple[Optional[str], Optional[Type[BaseLoader]]]:
    """Determine database type from *db_url* and return the loader class.

    Performs null/empty check, case-insensitive matching and defaults to
    PostgreSQL for backward compatibility on the server path.

    When ``sdk_only`` is True, raises ``InvalidArgumentError`` for vendors
    that need the ``[server]`` extra (snowflake) or for unknown URL schemes,
    so SDK callers get a clean error instead of a deferred ``ImportError``.
    """
    if not db_url or db_url == "No URL available for this database.":
        return None, None

    db_url_lower = db_url.lower()

    if db_url_lower.startswith('postgresql://') or db_url_lower.startswith('postgres://'):
        return 'postgresql', PostgresLoader
    if db_url_lower.startswith('mysql://'):
        return 'mysql', MySQLLoader
    if db_url_lower.startswith('impala://') or db_url_lower.startswith('impala+http://'):
        return 'impala', ImpalaLoader
    if db_url_lower.startswith('yaml://'):
        return 'yaml', YamlSchemaLoader
    if db_url_lower.startswith('snowflake://'):
        if sdk_only:
            raise InvalidArgumentError(
                "Snowflake requires the [server] extra: "
                "pip install t2s[server]"
            )
        # Lazy-import: snowflake-connector-python is in the [server] extra,
        # not in the core SDK install.
        # pylint: disable=import-outside-toplevel
        from api.loaders.snowflake_loader import SnowflakeLoader
        return 'snowflake', SnowflakeLoader

    if sdk_only:
        raise InvalidArgumentError(
            "Invalid database URL format. Must be PostgreSQL, MySQL, Impala, or YAML."
        )
    # Server path keeps the historical default-to-PostgreSQL fallback.
    return 'postgresql', PostgresLoader


def validate_custom_model(custom_model: Optional[str]) -> None:
    """Validate the ``vendor/model`` format and supported vendor list.

    Raises:
        InvalidArgumentError: If the format is wrong or the vendor is unsupported.
    """
    if not custom_model:
        return
    # Lazy-import: SUPPORTED_VENDORS lives in api.config which pulls server-only
    # symbols. Keeping the import here means the SDK doesn't need it at import time.
    # pylint: disable=import-outside-toplevel
    from api.config import SUPPORTED_VENDORS
    parts = custom_model.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise InvalidArgumentError(
            "Invalid model format. Expected 'vendor/model' (e.g. 'openai/gpt-4.1')"
        )
    if parts[0] not in SUPPORTED_VENDORS:
        raise InvalidArgumentError(
            f"Unsupported vendor '{parts[0]}'. Supported: {', '.join(SUPPORTED_VENDORS)}"
        )


# ---------------------------------------------------------------------------
# Input sanitisation
# ---------------------------------------------------------------------------


def sanitize_query(query: str) -> str:
    """Sanitize *query* for safe usage — strips newlines and truncates to 500 chars."""
    return query.replace('\n', ' ').replace('\r', ' ')[:500]


def sanitize_log_input(value: str) -> str:
    """Sanitize *value* for safe logging — removes newlines, CRs, and tabs."""
    if not isinstance(value, str):
        value = str(value)
    return value.replace('\n', ' ').replace('\r', ' ').replace('\t', ' ')


def truncate_for_log(query: str, max_length: int = 200) -> str:
    """Truncate *query* for compact log messages (SDK path)."""
    if len(query) > max_length:
        return query[:max_length] + "..."
    return query


# ---------------------------------------------------------------------------
# SQL analysis helpers
# ---------------------------------------------------------------------------


def _strip_sql_comments_and_whitespace(sql_query: str) -> str:
    """Strip leading SQL comments (-- line and /* block */) and whitespace.

    A naive ``strip().split()[0]`` lets ``-- evil\\nDROP TABLE x`` masquerade
    as a non-destructive statement, bypassing confirmation.
    """
    text = sql_query.lstrip()
    while text:
        if text.startswith("--"):
            newline = text.find("\n")
            if newline == -1:
                return ""
            text = text[newline + 1:].lstrip()
        elif text.startswith("/*"):
            end = text.find("*/")
            if end == -1:
                return ""
            text = text[end + 2:].lstrip()
        else:
            break
    return text


def _scrub_sql_literals_and_comments(sql_query: str) -> str:
    """Replace literals/comments with spaces while preserving statement shape."""
    text = sql_query or ""
    result: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        nxt = text[index + 1] if index + 1 < length else ""

        if char == "-" and nxt == "-":
            result.extend("  ")
            index += 2
            while index < length and text[index] not in "\r\n":
                result.append(" ")
                index += 1
            continue

        if char == "/" and nxt == "*":
            result.extend("  ")
            index += 2
            while index + 1 < length and not (text[index] == "*" and text[index + 1] == "/"):
                result.append(" ")
                index += 1
            if index + 1 < length:
                result.extend("  ")
                index += 2
            continue

        if char in {"'", '"', "`"}:
            quote = char
            result.append(" ")
            index += 1
            while index < length:
                result.append(" ")
                if text[index] == quote:
                    if quote == "'" and index + 1 < length and text[index + 1] == "'":
                        result.append(" ")
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue

        result.append(char)
        index += 1

    return "".join(result)


def detect_destructive_operation(sql_query: str) -> tuple[str, bool]:
    """Return ``(sql_type, is_destructive)`` for a SQL statement.

    Strips leading SQL comments before classifying so attackers cannot
    bypass destructive-op confirmation by prefixing a comment.
    """
    if not sql_query:
        return "", False
    cleaned = _strip_sql_comments_and_whitespace(sql_query)
    sql_type = cleaned.split()[0].upper() if cleaned else ""
    return sql_type, sql_type in DESTRUCTIVE_OPS


def validate_read_only_sql(sql_query: str) -> tuple[bool, str]:
    """Return whether user-generated SQL is safe to execute in read-only mode."""
    if not sql_query or not sql_query.strip():
        return False, "SQL query is empty"

    cleaned = _strip_sql_comments_and_whitespace(sql_query)
    scrubbed = _scrub_sql_literals_and_comments(cleaned).strip()
    if not scrubbed:
        return False, "SQL query is empty"

    first_token = scrubbed.split(None, 1)[0].upper()
    if first_token not in READ_ONLY_START_OPS:
        return (
            False,
            f"Only read-only SELECT/WITH queries are allowed; got {first_token or 'UNKNOWN'}",
        )

    without_trailing_semicolons = scrubbed.rstrip().rstrip(";").strip()
    if ";" in without_trailing_semicolons:
        return False, "Multiple SQL statements are not allowed"

    match = WRITE_OR_ADMIN_RE.search(scrubbed)
    if match:
        operation = match.group(2).upper()
        return (
            False,
            f"Write/admin operation {operation} is disabled; only read-only SELECT/WITH queries are allowed",
        )

    # Second line of defense: AST-level check via sqlglot. The regex above
    # scans tokens; the AST check understands structure (e.g. write nodes
    # smuggled through dialect quirks the scrubber cannot see).
    try:
        # pylint: disable=import-outside-toplevel
        from api.sql_utils.sql_gate import validate_read_only_ast
        ast_ok, ast_reason = validate_read_only_ast(sql_query, None)
        if not ast_ok:
            return False, ast_reason
    except ImportError:
        pass

    return True, ""


def fix_json_operator_chain(sql_query: str, db_type: Optional[str]) -> tuple[str, bool]:
    """Repair Postgres JSON paths that use ``->>`` for an INTERMEDIATE key.

    ``a->>'b'->>'c'`` is invalid: the first ``->>`` returns ``text`` and the
    second ``->>`` has no ``text -> unknown`` operator ("operator does not
    exist: text ->>"). Only the LAST hop may be ``->>`` (text leaf); every
    earlier hop must be ``->`` (jsonb). This is a deterministic, idempotent heal
    of that exact pattern — it leaves already-correct paths and single ``->>``
    untouched. Returns ``(sql, was_modified)``.
    """
    if not sql_query or "->>" not in sql_query:
        return sql_query, False
    if str(db_type or "postgresql").lower() not in {"postgres", "postgresql"}:
        return sql_query, False
    import re  # pylint: disable=import-outside-toplevel
    pattern = re.compile(r"->>(\s*(?:'[^']*'|\d+)\s*)->>")
    current = sql_query
    while True:
        nxt = pattern.sub(r"->\1->>", current)
        if nxt == current:
            break
        current = nxt
    return (current, True) if current != sql_query else (sql_query, False)


def auto_quote_sql_identifiers(
    sql_query: str,
    known_tables: set,
    db_type: Optional[str],
) -> tuple[str, bool]:
    """Auto-quote table names containing special characters.

    Returns ``(sanitized_sql, was_modified)``.
    """
    # First normalize ANSI double-quoted identifiers to the dialect's
    # quoting: in Impala/Hive a double quote is a STRING literal, so an
    # un-normalized ``ORDER BY "alias"`` silently sorts by a constant.
    from api.sql_utils.sql_gate import (  # pylint: disable=import-outside-toplevel
        normalize_current_date_function,
        normalize_identifier_quoting,
    )
    sql_query, quote_normalized = normalize_identifier_quoting(sql_query, db_type)
    # Impala: bare CURRENT_DATE is a column ref and fails; needs CURRENT_DATE().
    sql_query, date_normalized = normalize_current_date_function(sql_query, db_type)
    quote_normalized = quote_normalized or date_normalized
    quote_char = DatabaseSpecificQuoter.get_quote_char(db_type or 'postgresql')
    sanitized, table_quoted = SQLIdentifierQuoter.auto_quote_identifiers(
        sql_query, known_tables, quote_char
    )
    return sanitized, (quote_normalized or table_quoted)


def parenthesize_json_in_operators(
    sql_query: str,
    db_type: Optional[str],
) -> tuple[str, bool]:
    """Wrap JSON extractions (``->`` / ``->>``) that are DIRECT operands of a
    concat (``||``) or arithmetic operator in parentheses.

    PostgreSQL binds the JSON operators MORE LOOSELY than ``||`` and arithmetic,
    so ``a->>'x' || b->>'y'`` misparses (``operator does not exist: text -> unknown``).
    The weak model ignores the (delivered) user-rule about this and the healer
    cannot self-parenthesize reliably, so a deterministic AST pass fixes the whole
    class. General — any JSON column, any dialect using these operators. Returns
    ``(sql, was_modified)``; on any parse/serialize failure returns input unchanged.
    """
    if not sql_query or "->" not in sql_query:
        return sql_query, False
    dialect = "postgres" if (db_type or "").lower().startswith("postgres") else (db_type or None)
    try:
        import sqlglot  # pylint: disable=import-outside-toplevel
        from sqlglot import exp  # pylint: disable=import-outside-toplevel
        tree = sqlglot.parse_one(sql_query, read=dialect)
    except Exception:  # pylint: disable=broad-exception-caught
        return sql_query, False
    changed = False
    for node in list(tree.walk()):
        if isinstance(node, (exp.DPipe, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
            for key in ("this", "expression"):
                child = node.args.get(key)
                if isinstance(child, (exp.JSONExtract, exp.JSONExtractScalar)):
                    node.set(key, exp.Paren(this=child.copy()))
                    changed = True
    if not changed:
        return sql_query, False
    try:
        return tree.sql(dialect=dialect), True
    except Exception:  # pylint: disable=broad-exception-caught
        return sql_query, False


def promote_bare_to_json_leaf(
    sql_query: str,
    db_type: Optional[str],
    json_paths: Optional[dict],
    flat_columns: Optional[set],
) -> tuple[str, bool]:
    """Rewrite a bare/qualified column that is actually a JSON LEAF KEY into its
    JSON path, using the schema's own leaf registry.

    The weak linker/generator sometimes binds a JSON-stored field to a flat column
    name — e.g. `event_name` or `races.event_name` for a field that lives in
    `event_schedule->>'event_name'`. The schema has no such flat column, so the
    gate rejects it and the repair loop can return empty SQL (0 rows). When a
    referenced name is NOT a real flat column but IS the UNIQUE leaf key of a JSON
    column, promote it to `<jsoncol>->'k1'->>'leaf'` (preserving any table alias).
    Deterministic, general, graph-driven; ambiguous leaves (same key in two JSON
    columns) are left untouched. Returns ``(sql, changed)``.
    """
    if not sql_query or not json_paths:
        return sql_query, False
    leaf_map: dict = {}
    ambiguous: set = set()
    for jcol, info in json_paths.items():
        for leaf, keys in ((info or {}).get("leaves") or {}).items():
            lk = str(leaf).lower()
            if lk in leaf_map and leaf_map[lk][0] != jcol:
                ambiguous.add(lk)
            else:
                leaf_map[lk] = (jcol, list(keys))
    for lk in ambiguous:
        leaf_map.pop(lk, None)
    if not leaf_map:
        return sql_query, False
    flat = {str(c).lower() for c in (flat_columns or set())}
    dialect = "postgres" if (db_type or "").lower().startswith("postgres") else (db_type or None)
    try:
        import sqlglot  # pylint: disable=import-outside-toplevel
        from sqlglot import exp  # pylint: disable=import-outside-toplevel
        tree = sqlglot.parse_one(sql_query, read=dialect)
    except Exception:  # pylint: disable=broad-exception-caught
        return sql_query, False
    changed = False
    for col in list(tree.find_all(exp.Column)):
        name = (col.name or "").lower()
        if not name or name in flat or name not in leaf_map:
            continue
        jcol, keys = leaf_map[name]
        if not keys:
            continue
        base = f"{col.table}.{jcol}" if col.table else jcol
        expr = base
        for k in keys[:-1]:
            expr += f"->'{k}'"
        expr += f"->>'{keys[-1]}'"
        try:
            col.replace(sqlglot.parse_one(expr, read=dialect))
            changed = True
        except Exception:  # pylint: disable=broad-exception-caught
            continue
    if not changed:
        return sql_query, False
    try:
        return tree.sql(dialect=dialect), True
    except Exception:  # pylint: disable=broad-exception-caught
        return sql_query, False


def check_schema_modification(
    sql_query: str,
    loader_class: Type[BaseLoader],
) -> tuple[bool, str]:
    """Thin wrapper around ``loader_class.is_schema_modifying_query()``.

    Returns ``(is_schema_modifying, operation_type)``.
    """
    return loader_class.is_schema_modifying_query(sql_query)


# ---------------------------------------------------------------------------
# Chat data validation & truncation
# ---------------------------------------------------------------------------


def validate_and_truncate_chat(
    chat_data,
) -> tuple[list, Optional[list], Optional[str], bool]:
    """Validate *chat_data* and truncate history to ``Config.SHORT_MEMORY_LENGTH``.

    Uses ``getattr`` for safe attribute access (works with both Pydantic
    models and plain objects).

    Returns:
        ``(queries_history, result_history, instructions, use_user_rules)``

    Raises:
        InvalidArgumentError: If chat data is invalid or empty.
    """
    queries_history = getattr(chat_data, 'chat', None)
    result_history = getattr(chat_data, 'result', None)
    instructions = getattr(chat_data, 'instructions', None)
    use_user_rules = getattr(chat_data, 'use_user_rules', True)

    if not queries_history or not isinstance(queries_history, list):
        raise InvalidArgumentError("Invalid or missing chat history")

    if len(queries_history) == 0:
        raise InvalidArgumentError("Empty chat history")

    # Truncate to configured window
    if len(queries_history) > Config.SHORT_MEMORY_LENGTH:
        queries_history = queries_history[-Config.SHORT_MEMORY_LENGTH:]
        if result_history and len(result_history) > 0:
            max_results = Config.SHORT_MEMORY_LENGTH - 1
            if max_results > 0:
                result_history = result_history[-max_results:]
            else:
                result_history = []

    return queries_history, result_history, instructions, use_user_rules


# ---------------------------------------------------------------------------
# Pipeline helpers used by run_query / run_confirmed
# ---------------------------------------------------------------------------


async def quote_identifiers_from_graph(
    sql_query: str,
    graph_id: str,
    db_type: Optional[str],
    db=None,
    known_tables: Optional[set] = None,
) -> tuple[str, bool]:
    """Auto-quote SQL identifiers using the Table list stored in FalkorDB.

    If *known_tables* is supplied, uses it directly; otherwise queries the
    graph for the current Table names. Returns ``(sql, was_modified)``.
    """
    if known_tables is None:
        graph = resolve_db(db).select_graph(graph_id)
        try:
            tables_res = (
                await graph.query("MATCH (t:Table) RETURN t.name")
            ).result_set
            known_tables = (
                {row[0] for row in tables_res} if tables_res else set()
            )
        except Exception:  # pylint: disable=broad-exception-caught
            known_tables = set()

    return auto_quote_sql_identifiers(sql_query, known_tables, db_type)


def format_ai_response(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    queries_history: list,
    result_history: Optional[list],
    sql_query: str,
    query_results: list,
    db_description: str,
    custom_api_key: Optional[str] = None,
    custom_model: Optional[str] = None,
) -> str:
    """Build a human-readable AI response for *query_results*."""
    agent = ResponseFormatterAgent(
        queries_history, result_history, custom_api_key, custom_model,
    )
    return agent.format_response(
        user_query=queries_history[-1] if queries_history else "",
        sql_query=sql_query,
        query_results=query_results,
        db_description=db_description,
    )


def build_destructive_confirmation_message(sql_type: str, sql_query: str) -> str:
    """Return the rich confirmation prompt shown for destructive operations.

    Used by both the streaming confirmation event and the sync ``QueryResult``
    so users see the same warning wording regardless of transport.
    """
    description = _DESTRUCTIVE_VERBS.get(sql_type, "Modify the database")
    return (
        "⚠️ DESTRUCTIVE OPERATION DETECTED ⚠️\n\n"
        f"The generated SQL query will perform a **{sql_type}** operation:\n\n"
        f"SQL:\n{sql_query}\n\n"
        f"What this will do:\n• {description}\n\n"
        "⚠️ WARNING: This operation will make changes to your database and "
        "may be irreversible."
    )


def save_memory_background(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    memory_tool: Any,
    question: str,
    sql_query: str,
    success: bool,
    error: str,
    full_response: Optional[dict] = None,
    chat_histories: Optional[list] = None,
    task_sink: Optional[set] = None,
) -> None:
    """Schedule fire-and-forget memory persistence for the given query.

    Returns immediately; tasks run in the background with their own
    error-logging callbacks so a failure to save never blocks the response.

    When ``task_sink`` is given, each scheduled task is added to it and
    auto-removed on completion. The SDK uses this so ``T2SClient.close()``
    can await in-flight memory writes before disconnecting the pool.
    """

    sink = task_sink if task_sink is not None else background_tasks_var.get()

    def _track(task):
        if sink is None:
            return
        sink.add(task)
        task.add_done_callback(sink.discard)

    def _log_done(label: str):
        # Done-callbacks must not call ``t.exception()`` on a cancelled task —
        # it raises CancelledError and surfaces as a noisy "exception in callback"
        # log line, which is misleading at shutdown.
        def _cb(task):
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                logging.error("%s failed: %s", label, exc)  # nosemgrep
            else:
                logging.info("%s completed successfully", label)
        return _cb

    save_query_task = asyncio.create_task(
        memory_tool.save_query_memory(
            query=question,
            sql_query=sql_query,
            success=success,
            error=error,
        )
    )
    _track(save_query_task)
    save_query_task.add_done_callback(_log_done("Query memory save"))

    # NOTE: graphiti's conversational entity-graph extraction
    # (``memory_tool.add_new_memory``) is intentionally NOT scheduled here. On
    # local/weak models its structured ExtractedEntities() output is unreliable
    # (non-fatal but noisy), and the value it produced is now served far more
    # robustly by the MemoryAgent recalling saved ``:Query`` examples. Saving the
    # query node above is all the active memory path needs.

    clean_task = asyncio.create_task(memory_tool.clean_memory())
    _track(clean_task)
    clean_task.add_done_callback(_log_done("Memory cleanup"))
