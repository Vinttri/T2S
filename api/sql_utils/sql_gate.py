"""Deterministic SQL validation gate built on sqlglot.

The gate runs AFTER SQL generation and BEFORE execution. It is the
schema-grounded counterpart to the LLM: everything here is plain code,
no model calls and no domain/table-name hardcode.

Checks:
1. ``parse``      — the SQL must parse for the target dialect.
2. ``read_only``  — the AST must be a single SELECT/UNION/CTE statement
                    with no write/DDL/admin nodes anywhere in the tree.
3. ``allowlist``  — every table and every column referenced in the SQL
                    must exist in the schema card built from the graph/RAG
                    context. Unknown identifiers are reported with
                    closest-match suggestions so a repair prompt can fix
                    them precisely instead of guessing.

All knobs are environment variables (no per-database hardcode):
    QW_SQL_GATE_ENABLED        default "true"   — master switch
    QW_SQL_GATE_MAX_REPAIRS    default "1"      — regeneration attempts on gate failure
    QW_EXPLAIN_PREFLIGHT       default "true"   — run EXPLAIN <sql> before the real query
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

try:  # sqlglot is a hard dependency of the SDK, but degrade gracefully anyway
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import ParseError
    _SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on broken installs
    sqlglot = None
    exp = None
    ParseError = Exception
    _SQLGLOT_AVAILABLE = False

# Map T2S db_type values to sqlglot dialect names. Impala has no
# dedicated sqlglot dialect; hive is the closest compatible grammar.
# Single source of truth: db_type (from the DB connector / provider, i.e.
# get_database_type_and_loader) -> the EXACT dialect name the installed sqlglot
# expects. sqlglot has no native "impala"/"mssql" dialect, so alias them to the
# parser-compatible one (Impala is Hive/HQL-compatible; MS SQL Server is T-SQL).
# Every sqlglot call in the codebase resolves its dialect through here.
_DIALECT_MAP = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
    "impala": "hive",
    "hive": "hive",
    "hive2": "hive",
    "hiveql": "hive",
    "snowflake": "snowflake",
    "mssql": "tsql",
    "sqlserver": "tsql",
    "sql_server": "tsql",
    "sql-server": "tsql",
    "tsql": "tsql",
    "oracle": "oracle",
    "sqlite": "sqlite",
    "bigquery": "bigquery",
    "redshift": "redshift",
    "duckdb": "duckdb",
    "trino": "trino",
    "presto": "presto",
    "clickhouse": "clickhouse",
    "databricks": "databricks",
    "spark": "spark",
}


def _installed_sqlglot_dialects() -> set:
    """Dialect names the currently-installed sqlglot build actually ships."""
    try:
        from sqlglot.dialects.dialect import Dialect
        return set(Dialect.classes.keys())
    except Exception:  # pragma: no cover - defensive
        return set()

_TRUE_VALUES = {"1", "true", "yes", "y", "on"}
_FALSE_VALUES = {"0", "false", "no", "n", "off"}

# AST node types that must never appear in a read-only statement.
_WRITE_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter",
    "AlterTable", "TruncateTable", "Grant", "Revoke", "Command", "Set",
    "Use", "Call", "LoadData", "Copy", "AlterColumn", "Analyze",
)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def gate_enabled() -> bool:
    """Master switch for the deterministic SQL gate."""
    return _SQLGLOT_AVAILABLE and _bool_env("QW_SQL_GATE_ENABLED", True)


def gate_max_repairs() -> int:
    # Default 2: one repair pass is not always enough for grain pinning on a
    # weak model. Out-of-the-box behaviour must match the verified contour —
    # no install-time tuning flags required.
    raw = os.getenv("QW_SQL_GATE_MAX_REPAIRS", "").strip()
    try:
        value = int(raw) if raw else 2
    except ValueError:
        value = 2
    return max(0, min(value, 3))


def explain_preflight_enabled(db_type: Optional[str]) -> bool:
    """EXPLAIN preflight runs only against real engines, never for yaml graphs."""
    if (db_type or "").lower() not in _DIALECT_MAP:
        return False
    return _bool_env("QW_EXPLAIN_PREFLIGHT", True)


def sqlglot_dialect(db_type: Optional[str]) -> Optional[str]:
    """Resolve a connector ``db_type`` to the exact sqlglot dialect name.

    Validates the mapped name against the installed sqlglot build and falls back
    gracefully (``db_type`` may already be a valid sqlglot name; otherwise
    ``None`` = sqlglot's permissive default) so a dialect lookup NEVER raises.
    This is THE canonical resolver — all sqlglot parse/transpile/validate calls
    and the agents' prompt dialect come from here.
    """
    name = (db_type or "").strip().lower()
    if not name:
        return None
    available = _installed_sqlglot_dialects()
    mapped = _DIALECT_MAP.get(name)
    if mapped and (not available or mapped in available):
        return mapped
    if name in available:  # db_type already is a valid sqlglot dialect name
        return name
    return None  # controlled fallback: permissive default, never crash


# Dialects whose identifier quote is the BACKTICK. For these, an ANSI
# double-quoted identifier from the model is silently read as a STRING
# literal, so ORDER BY "alias" sorts by a constant and SELECT "col"
# returns the literal text — wrong results with no error.
_BACKTICK_DIALECTS = {"hive", "spark", "databricks", "mysql"}

# Impala parses bare CURRENT_DATE as a COLUMN reference (fails); it needs
# the function form CURRENT_DATE(). Real Hive/Snowflake accept bare and
# Postgres REJECTS the parens, so this is Impala-only. Negative lookbehind
# avoids touching a qualified column ref (t.current_date); the lookahead
# leaves an already-parenthesised CURRENT_DATE() alone.
_BARE_CURRENT_DATE_RE = re.compile(r"(?i)(?<![\w.])current_date\b(?!\s*\()")


def normalize_identifier_quoting(
    sql: str, db_type: Optional[str]
) -> tuple[str, bool]:
    """Rewrite ANSI double-quoted identifiers to the dialect's quoting.

    The model frequently emits ``\"alias\"`` / ``\"col\"`` (ANSI/Postgres
    identifier quoting). In Impala/Hive/MySQL the double quote denotes a
    STRING literal, so ``ORDER BY \"alias\"`` silently orders by a constant
    and ``SELECT \"col\"`` returns the literal text — wrong results with NO
    error. We reparse with the generic reader (where ``\"...\"`` IS an
    identifier) and re-render in the target dialect, which quotes
    identifiers with backticks. Single-quoted string literals are kept.

    Applied only for backtick dialects and only when a double quote is
    present; on any parse/transpile failure the SQL is returned unchanged.
    Returns ``(sql, was_modified)``.
    """
    if not _SQLGLOT_AVAILABLE or sqlglot is None:
        return sql, False
    dialect = sqlglot_dialect(db_type)
    if dialect not in _BACKTICK_DIALECTS:
        return sql, False
    if not sql or '"' not in sql:
        return sql, False
    try:
        rendered = sqlglot.transpile(sql, read=None, write=dialect)
    except Exception:  # pylint: disable=broad-exception-caught
        return sql, False
    if not rendered or not rendered[0] or rendered[0] == sql:
        return sql, False
    return rendered[0], True


def normalize_current_date_function(
    sql: str, db_type: Optional[str]
) -> tuple[str, bool]:
    """Rewrite bare CURRENT_DATE -> CURRENT_DATE() on Impala only.

    Impala parses bare CURRENT_DATE as a column reference and fails at
    execution; the function form is required. Restricted to Impala because
    Postgres rejects the parens and Hive/Snowflake accept the bare form.
    Returns ``(sql, was_modified)``.
    """
    if str(db_type or "").strip().lower() != "impala":
        return sql, False
    if not sql:
        return sql, False
    new_sql = _BARE_CURRENT_DATE_RE.sub("CURRENT_DATE()", sql)
    return (new_sql, new_sql != sql)


@dataclass
class GateResult:
    """Outcome of the deterministic validation gate."""

    ok: bool = True
    parse_error: Optional[str] = None
    read_only_violation: Optional[str] = None
    unknown_tables: list = field(default_factory=list)
    unknown_columns: list = field(default_factory=list)
    suggestions: dict = field(default_factory=dict)
    checked_tables: int = 0
    checked_columns: int = 0

    def report(self) -> str:
        """Human/LLM-readable description of every violation."""
        lines: list[str] = []
        if self.parse_error:
            lines.append(f"SQL does not parse: {self.parse_error}")
        if self.read_only_violation:
            lines.append(self.read_only_violation)
        for table in self.unknown_tables:
            hint = ", ".join(self.suggestions.get(table, []))
            suffix = f" (closest existing tables: {hint})" if hint else ""
            lines.append(
                f"Table '{table}' does not exist in the selected schema{suffix}."
            )
        for column in self.unknown_columns:
            hint = ", ".join(self.suggestions.get(column, []))
            suffix = f" (closest existing columns: {hint})" if hint else ""
            lines.append(
                f"Column '{column}' does not exist in the selected schema{suffix}."
            )
        return "\n".join(lines)


def _normalize_name(value: Any) -> str:
    return str(value or "").strip().strip('`"').lower()


def _table_card_keys(table_name: str) -> set[str]:
    """All lookup keys a referenced table may match: full, schema.table, table."""
    cleaned = _normalize_name(table_name)
    parts = [part for part in cleaned.split(".") if part]
    keys = {cleaned}
    if parts:
        keys.add(parts[-1])
    if len(parts) >= 2:
        keys.add(".".join(parts[-2:]))
    return {key for key in keys if key}


def build_schema_card(tables: Optional[Iterable]) -> dict[str, set[str]]:
    """Build {table_key -> {column, ...}} from find()'s table_info lists.

    ``table_info`` is ``[name, description, foreign_keys, columns]`` where
    columns are dicts carrying ``columnName``/``name``.
    """
    card: dict[str, set[str]] = {}
    for table_info in tables or []:
        if not isinstance(table_info, (list, tuple)) or not table_info:
            continue
        table_name = table_info[0]
        columns = table_info[3] if len(table_info) > 3 else []
        column_names: set[str] = set()
        for column in columns or []:
            if isinstance(column, dict):
                name = _normalize_name(column.get("columnName") or column.get("name"))
                if name:
                    column_names.add(name)
        for key in _table_card_keys(table_name):
            card.setdefault(key, set()).update(column_names)
    return card


def _invalid_json_chain_refs(expression) -> list:
    """JSON-path chains where a text-returning ``->>`` feeds another JSON op.

    In ``a ->> 'b' ->> 'c'`` the first ``->>`` returns TEXT, so the next operator
    runs on text -> runtime "operator does not exist: text ->> unknown". Only the
    FINAL leaf may use ``->>``; intermediate keys must use ``->``. Flags any JSON
    operator whose left operand (through parentheses) is a ``->>``
    (JSONExtractScalar). Pure sqlglot AST, dialect-general; [] where absent."""
    bad: list = []
    try:
        for node in expression.find_all(exp.JSONExtract, exp.JSONExtractScalar):
            inner = node.this
            while isinstance(inner, exp.Paren):
                inner = inner.this
            if isinstance(inner, exp.JSONExtractScalar):
                bad.append(node.sql()[:80])
    except Exception:  # pylint: disable=broad-exception-caught
        return []
    return bad


def _parse(sql: str, db_type: Optional[str]):
    """Parse with the target dialect, falling back to the generic reader."""
    dialect = sqlglot_dialect(db_type)
    # Catch ANY parser/tokenizer failure (ParseError, TokenError on an
    # unterminated string/regex literal, etc.), not just ParseError — otherwise
    # a malformed model SQL crashes the gate and reaches display/execution
    # instead of being routed to repair as a parse error.
    try:
        return sqlglot.parse_one(sql, read=dialect), None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        generic_error = str(exc)
    try:
        return sqlglot.parse_one(sql), None
    except Exception:  # pylint: disable=broad-exception-caught
        return None, (generic_error or "SQL parse error")[:400]


def _read_only_violation(expression) -> Optional[str]:
    """Return a violation message when the AST contains write/DDL nodes."""
    root = expression
    while isinstance(root, exp.With):
        root = root.this
    if not isinstance(root, (exp.Select, exp.Union, exp.Subquery)):
        return (
            f"Only read-only SELECT statements are allowed; got "
            f"{type(root).__name__.upper()}"
        )
    for node_name in _WRITE_NODE_NAMES:
        node_type = getattr(exp, node_name, None)
        if node_type is None:
            continue
        found = expression.find(node_type)
        if found is not None:
            return (
                f"Write/DDL operation {node_name.upper()} is not allowed "
                "in read-only mode"
            )
    return None


def _collect_cte_names(expression) -> set[str]:
    names: set[str] = set()
    for cte in expression.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            names.add(_normalize_name(alias))
    return names


def _collect_output_aliases(expression) -> set[str]:
    aliases: set[str] = set()
    for alias in expression.find_all(exp.Alias):
        if alias.alias:
            aliases.add(_normalize_name(alias.alias))
    for table_alias in expression.find_all(exp.TableAlias):
        for column_def in table_alias.args.get("columns") or []:
            aliases.add(_normalize_name(getattr(column_def, "name", "")))
    return aliases


def _alias_to_table_map(expression, cte_names: set[str]) -> dict[str, Optional[str]]:
    """Map every FROM/JOIN alias to its real table key (None for CTE/derived)."""
    alias_map: dict[str, Optional[str]] = {}
    for table_node in expression.find_all(exp.Table):
        table_key = _normalize_name(
            ".".join(part for part in (table_node.db, table_node.name) if part)
        )
        base_name = _normalize_name(table_node.name)
        alias = _normalize_name(table_node.alias) if table_node.alias else None
        resolved = None if base_name in cte_names else (table_key or base_name)
        if alias:
            alias_map[alias] = resolved
        if base_name and base_name not in alias_map:
            alias_map[base_name] = resolved
    for subquery in expression.find_all(exp.Subquery):
        alias = _normalize_name(subquery.alias) if subquery.alias else None
        if alias:
            alias_map[alias] = None
    return alias_map


def _card_lookup(card: dict[str, set[str]], table_ref: str) -> Optional[set[str]]:
    for key in _table_card_keys(table_ref):
        if key in card:
            return card[key]
    return None


def _identifier_tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[_.]+", value or "") if len(token) >= 3}


def _suggest(value: str, candidates: Iterable[str], limit: int = 3) -> list[str]:
    """Closest matches by edit distance plus shared identifier tokens."""
    pool = sorted({candidate for candidate in candidates if candidate})
    matches = difflib.get_close_matches(value, pool, n=limit, cutoff=0.55)
    value_tokens = _identifier_tokens(value)
    if value_tokens:
        token_scored = sorted(
            (
                (len(value_tokens & _identifier_tokens(candidate)), candidate)
                for candidate in pool
                if value_tokens & _identifier_tokens(candidate)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        for _score, candidate in token_scored:
            if candidate not in matches:
                matches.append(candidate)
            if len(matches) >= limit + 2:
                break
    return matches[: limit + 2]


def _undefined_alias_refs(expression) -> list:
    """Column qualifiers (table aliases) defined by NO source anywhere in the query.

    A dangling alias — e.g. ``r.date`` with no ``races r`` in any FROM/JOIN — is a
    guaranteed runtime "missing FROM-clause entry for table r". The main column
    check is lenient on unresolved qualifiers (to avoid false positives on
    CTE/derived columns), so it lets these through. This catches them structurally.
    Conservative: a qualifier is flagged only if it is in the GLOBAL union of NO
    defined source (FROM/JOIN/CTE/derived alias or a real table name), which avoids
    false positives on correlated subqueries. Fail-safe: returns [] on any error."""
    try:
        defined: set[str] = set()
        try:
            from sqlglot.optimizer.scope import traverse_scope
            for scope in traverse_scope(expression):
                for alias in scope.sources:
                    defined.add(_normalize_name(alias))
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        for table_node in expression.find_all(exp.Table):
            if table_node.name:
                defined.add(_normalize_name(table_node.name))
            if table_node.alias:
                defined.add(_normalize_name(table_node.alias))
        for ta in expression.find_all(exp.TableAlias):
            if ta.name:
                defined.add(_normalize_name(ta.name))
        bad: list = []
        seen: set[str] = set()
        for column_node in expression.find_all(exp.Column):
            qualifier = _normalize_name(column_node.table) if column_node.table else ""
            if qualifier and qualifier not in defined and qualifier not in seen:
                seen.add(qualifier)
                bad.append(f"{qualifier}.{_normalize_name(column_node.name)}")
        return bad
    except Exception:  # pylint: disable=broad-exception-caught
        return []


def _null_polarity_map(node) -> dict:
    """Map each column's NULL-test polarity used under *node*.

    Returns ``{bare_column_name: {"null", "notnull"}}``. ``x IS NULL`` parses as
    ``Is(this=Column, expression=Null)``; ``x IS NOT NULL`` wraps that ``Is`` in a
    ``Not``. Keyed by the BARE column name so an alias difference (``cr.st_mark``
    vs ``st_mark``) still matches.
    """
    out: dict = {}
    if node is None:
        return out
    for is_node in node.find_all(exp.Is):
        if not isinstance(is_node.expression, exp.Null):
            continue
        col = is_node.this
        name = getattr(col, "name", None) or col.sql()
        name = str(name).lower()
        if not name:
            continue
        polarity = "notnull" if isinstance(is_node.parent, exp.Not) else "null"
        out.setdefault(name, set()).add(polarity)
    return out


def resolved_null_polarity_issues(
    sql: str, resolved: Optional[Iterable], db_type: Optional[str],
) -> list:
    """Flag when the generated SQL INVERTS a NULL-test a resolved metric fixed.

    The resolver hands the generator an exact column-bound formula (e.g.
    ``... st_mark IS NULL ...``). A weak generator sometimes "corrects" that to the
    opposite polarity (``IS NOT NULL``), silently negating the metric. This is a
    pure structural (sqlglot AST) check — it compares the NULL-test polarity per
    column between each resolved expression and the SQL, and reports only a clear
    INVERSION (the SQL uses the opposite polarity and NOT the resolved one). No
    string matching, no DB specifics; returns hints the repair loop bounces back.
    """
    if not _SQLGLOT_AVAILABLE or not resolved:
        return []
    sql_ast, _ = _parse(sql, db_type)
    if sql_ast is None:
        return []
    sql_pol = _null_polarity_map(sql_ast)
    if not sql_pol:
        return []
    dialect = sqlglot_dialect(db_type)
    issues: list = []
    seen: set = set()
    for item in resolved:
        expr = str((item or {}).get("sql_expression") or "").strip()
        if not expr or " is " not in f" {expr.lower()} ":
            continue
        try:
            r_ast = sqlglot.parse_one(expr, read=dialect)
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        for col, want_pols in _null_polarity_map(r_ast).items():
            have = sql_pol.get(col, set())
            for pol in want_pols:
                opposite = "notnull" if pol == "null" else "null"
                if pol not in have and opposite in have and (col, pol) not in seen:
                    seen.add((col, pol))
                    want_sql = "NULL" if pol == "null" else "NOT NULL"
                    issues.append(
                        f"the metric formula tests `{col} IS {want_sql}` but the query "
                        f"uses the OPPOSITE polarity — restore the resolved condition's "
                        f"`IS {want_sql}` exactly (do not invert it)"
                    )
    return issues


def validate_sql(
    sql: str,
    db_type: Optional[str],
    tables: Optional[Iterable],
    check_read_only: bool = True,
) -> GateResult:
    """Run the full deterministic gate over generated SQL."""
    result = GateResult()
    if not _SQLGLOT_AVAILABLE:
        return result
    if not (sql or "").strip():
        result.ok = False
        result.parse_error = "empty SQL"
        return result

    expression, parse_error = _parse(sql, db_type)
    if expression is None:
        result.ok = False
        result.parse_error = parse_error
        return result

    if check_read_only:
        violation = _read_only_violation(expression)
        if violation:
            result.ok = False
            result.read_only_violation = violation
            return result

    # Dangling table-alias references (structural, no schema needed): a column
    # qualified by an alias never defined in any FROM/JOIN/CTE -> guaranteed
    # runtime "missing FROM-clause entry". Reject so the healer adds the missing
    # table/join or fixes the alias instead of executing into an error.
    undefined_aliases = _undefined_alias_refs(expression)
    if undefined_aliases:
        result.ok = False
        for ref in undefined_aliases:
            if ref not in result.suggestions:
                result.unknown_columns.append(ref)
                result.suggestions[ref] = (
                    "table alias is not defined in any FROM/JOIN — add the missing "
                    "table/join or qualify the column with a defined alias"
                )
        return result

    # Invalid JSON-path chain (a text-returning ->> feeding another JSON op).
    bad_json = _invalid_json_chain_refs(expression)
    if bad_json:
        result.ok = False
        for ref in bad_json:
            if ref not in result.suggestions:
                result.unknown_columns.append(ref)
                result.suggestions[ref] = (
                    "invalid JSON path: an intermediate key uses ->> (returns text); "
                    "use -> for every key except the final leaf, which uses ->>"
                )
        return result

    card = build_schema_card(tables)
    if not card:
        # No schema context to validate against — let execution decide.
        return result

    cte_names = _collect_cte_names(expression)
    output_aliases = _collect_output_aliases(expression)
    alias_map = _alias_to_table_map(expression, cte_names)
    all_known_columns: set[str] = set()
    for columns in card.values():
        all_known_columns.update(columns)

    # --- tables ---------------------------------------------------------
    seen_unknown: set[str] = set()
    for table_node in expression.find_all(exp.Table):
        base_name = _normalize_name(table_node.name)
        if not base_name or base_name in cte_names:
            continue
        table_ref = _normalize_name(
            ".".join(part for part in (table_node.db, table_node.name) if part)
        )
        result.checked_tables += 1
        if _card_lookup(card, table_ref) is None and table_ref not in seen_unknown:
            seen_unknown.add(table_ref)
            result.unknown_tables.append(table_ref)
            result.suggestions[table_ref] = _suggest(base_name, card.keys())

    # --- columns --------------------------------------------------------
    for column_node in expression.find_all(exp.Column):
        column_name = _normalize_name(column_node.name)
        if not column_name or column_name == "*":
            continue
        qualifier = _normalize_name(column_node.table) if column_node.table else ""
        result.checked_columns += 1

        if qualifier:
            resolved_table = alias_map.get(qualifier, "__unresolved__")
            if resolved_table is None or resolved_table == "__unresolved__":
                # CTE/derived-table column or unresolvable alias: only verify
                # against the global namespace to avoid false positives.
                if (
                    column_name not in all_known_columns
                    and column_name not in output_aliases
                ):
                    ref = f"{qualifier}.{column_name}"
                    if ref not in result.suggestions:
                        result.unknown_columns.append(ref)
                        result.suggestions[ref] = _suggest(
                            column_name, all_known_columns,
                        )
                continue
            table_columns = _card_lookup(card, resolved_table)
            if table_columns is None:
                continue  # table itself already reported as unknown
            if column_name not in table_columns:
                ref = f"{resolved_table}.{column_name}"
                if ref not in result.suggestions:
                    result.unknown_columns.append(ref)
                    result.suggestions[ref] = _suggest(
                        column_name, table_columns | output_aliases,
                    )
            continue

        if (
            column_name not in all_known_columns
            and column_name not in output_aliases
            and column_name not in cte_names
        ):
            if column_name not in result.suggestions:
                result.unknown_columns.append(column_name)
                result.suggestions[column_name] = _suggest(
                    column_name, all_known_columns,
                )

    if result.unknown_tables or result.unknown_columns:
        result.ok = False

    return result


def validate_read_only_ast(sql: str, db_type: Optional[str]) -> tuple[bool, str]:
    """AST-level read-only verification (used alongside the regex guard)."""
    if not _SQLGLOT_AVAILABLE or not (sql or "").strip():
        return True, ""
    expression, _parse_err = _parse(sql, db_type)
    if expression is None:
        # Unparsable SQL is handled by the main gate / execution layer.
        return True, ""
    violation = _read_only_violation(expression)
    if violation:
        return False, violation
    return True, ""


# ---------------------------------------------------------------------------
# Snapshot-grain lint
# ---------------------------------------------------------------------------

_REPORTING_DESC_HEAD_RE = re.compile(
    r"^\s*(дата\s+(отч[её]та|баланса)|report(ing)?\s+date|as[-\s]?of\s+date|"
    r"snapshot\s+date|балансовая\s+дата|отч[её]тная\s+дата)",
    re.IGNORECASE,
)
_DATE_NAME_RE = re.compile(r"(^|_)(date|dt)$", re.IGNORECASE)


def grain_lint_enabled() -> bool:
    return _SQLGLOT_AVAILABLE and _bool_env("QW_GRAIN_LINT_ENABLED", True)


def _snapshot_columns_from_card_row(columns: list) -> set[str]:
    """Snapshot/reporting date columns, decided from schema metadata only:
    a date-named/typed PRIMARY KEY column whose description STARTS with a
    reporting-date phrase ("Дата отчета", "Дата баланса", "report date", ...).
    No table-name conventions involved."""
    out: set[str] = set()
    for column in columns or []:
        if not isinstance(column, dict):
            continue
        name = _normalize_name(column.get("columnName") or column.get("name"))
        if not name:
            continue
        ctype = str(column.get("dataType") or column.get("type") or "").lower()
        desc = str(column.get("description") or "")
        date_like = bool(_DATE_NAME_RE.search(name)) or "date" in ctype
        # A date-typed column whose description STARTS with a reporting-date
        # phrase is a snapshot key by meaning. PK metadata is corroborating
        # but not required: engines like Impala don't store key flags, so
        # requiring PK silently disabled the lint on freshly indexed graphs.
        if date_like and _REPORTING_DESC_HEAD_RE.search(desc):
            out.add(name)
    return out


def null_output_placeholder_issues(
    sql: str,
    db_type: Optional[str],
) -> list[str]:
    """Flag requested output columns returned as a NULL literal (e.g.
    `NULL AS begin_date`): the field was not sourced, only padded. Pure AST;
    NULLs nested inside CASE/COALESCE/expressions are not flagged."""
    if not _SQLGLOT_AVAILABLE or not (sql or "").strip():
        return []
    expression, _err = _parse(sql, db_type)
    if expression is None:
        return []
    select = expression if isinstance(expression, exp.Select) else expression.find(exp.Select)
    if select is None:
        return []
    placeholders: list[str] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Alias) and isinstance(projection.this, exp.Null):
            placeholders.append(projection.alias_or_name or "<unnamed>")
    if not placeholders:
        return []
    return [
        "Requested output column(s) returned as NULL placeholder(s): "
        + ", ".join(placeholders)
        + ". A NULL literal is not a real value — these fields were not sourced. "
        "Find a table that exposes them and follow the declared FOREIGN KEY "
        "relationships from the question's main object to reach it; do not pad "
        "missing outputs with NULL."
    ]


def snapshot_grain_issues(
    sql: str,
    db_type: Optional[str],
    tables: Optional[Iterable],
) -> list[str]:
    """Detect period-dated sources whose snapshot date is unconstrained.

    Joining snapshot tables only on business keys (or only snapshot=snapshot
    equalities between aliases) multiplies rows across every stored reporting
    date. Per SELECT scope: every alias of a snapshot-dated table must have its
    snapshot column constrained by a literal/function/range/subquery predicate,
    directly or transitively through snapshot-equality links. Scopes computing
    window functions are exempt (LAG/LEAD over the date axis is intentional —
    previous-period semantics; the date filter belongs to the outer scope).
    Pure metadata + AST — no domain hardcode.
    """
    if not _SQLGLOT_AVAILABLE or not (sql or "").strip():
        return []
    try:
        from sqlglot.optimizer.scope import traverse_scope
    except ImportError:  # pragma: no cover
        return []

    expression, _err = _parse(sql, db_type)
    if expression is None:
        return []

    snap_by_table: dict[str, set[str]] = {}
    for table_info in tables or []:
        if not isinstance(table_info, (list, tuple)) or not table_info:
            continue
        columns = table_info[3] if len(table_info) > 3 else []
        snap = _snapshot_columns_from_card_row(columns)
        if snap:
            for key in _table_card_keys(table_info[0]):
                snap_by_table[key] = snap
    if not snap_by_table:
        return []

    try:
        scopes = traverse_scope(expression)
    except Exception:  # pylint: disable=broad-exception-caught
        return []

    issues: list[str] = []
    for scope in scopes:
        select_node = scope.expression
        if not isinstance(select_node, exp.Select):
            continue

        alias2table: dict[str, str] = {}
        for alias, source in scope.sources.items():
            if isinstance(source, exp.Table):
                ref = _normalize_name(
                    ".".join(p for p in (source.db, source.name) if p)
                )
                for key in _table_card_keys(ref):
                    if key in snap_by_table:
                        alias2table[_normalize_name(alias)] = key
                        break

        snap_alias = {
            alias: snap_by_table[table]
            for alias, table in alias2table.items()
        }
        if not snap_alias:
            continue

        def _local(node_types):
            for node in select_node.find_all(node_types):
                if node.find_ancestor(exp.Select) is select_node:
                    yield node

        if any(True for _ in _local(exp.Window)):
            continue

        def _snap_refs(node) -> list[str]:
            refs: list[str] = []
            if node is None:
                return refs
            cols = ([node] if isinstance(node, exp.Column)
                    else list(node.find_all(exp.Column)))
            for col in cols:
                alias = _normalize_name(col.table)
                name = _normalize_name(col.name)
                if alias in snap_alias and name in snap_alias[alias]:
                    refs.append(alias)
                elif not alias:
                    candidates = [
                        a for a, cols_ in snap_alias.items() if name in cols_
                    ]
                    refs.extend(candidates if candidates else [])
            return refs

        pinned: set[str] = set()
        linked: list[tuple[str, str]] = []

        for agg in _local((exp.Max, exp.Min)):
            pinned.update(_snap_refs(agg))

        for node in _local((exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
                            exp.Between, exp.In, exp.NEQ)):
            if isinstance(node, (exp.Between, exp.In)):
                pinned.update(_snap_refs(node.this))
                continue
            left, right = node.this, node.expression
            lrefs, rrefs = _snap_refs(left), _snap_refs(right)
            bare_eq = (isinstance(node, exp.EQ)
                       and isinstance(left, exp.Column)
                       and isinstance(right, exp.Column))
            if bare_eq and lrefs and rrefs:
                linked.append((lrefs[0], rrefs[0]))
            else:
                pinned.update(lrefs)
                pinned.update(rrefs)

        changed = True
        while changed:
            changed = False
            for a, b in linked:
                if a in pinned and b not in pinned:
                    pinned.add(b)
                    changed = True
                if b in pinned and a not in pinned:
                    pinned.add(a)
                    changed = True

        for alias in snap_alias:
            if alias not in pinned:
                issues.append(
                    f"Source '{alias}' ({alias2table[alias]}) is a "
                    f"snapshot/period table, but its reporting date column "
                    f"({', '.join(sorted(snap_alias[alias]))}) is not "
                    f"constrained in this SELECT: rows multiply across every "
                    f"stored reporting date. Pin it to the requested date, a "
                    f"date range from the question, the latest available "
                    f"reporting date, or the current date per the rules."
                )
    return issues


_SQL_GATE_LOG_LIMIT = 600


def log_gate_result(stage: str, result: GateResult, sql: str) -> None:
    """Single structured log line per gate decision."""
    if result.ok:
        logging.info(
            "SQL gate passed: stage=%s tables_checked=%d columns_checked=%d",
            stage, result.checked_tables, result.checked_columns,
        )
        return
    logging.warning(
        "SQL gate failed: stage=%s parse_error=%s read_only=%s "
        "unknown_tables=%s unknown_columns=%s sql=%s",
        stage,
        (result.parse_error or "")[:200],
        (result.read_only_violation or "")[:120],
        result.unknown_tables[:8],
        result.unknown_columns[:8],
        re.sub(r"\s+", " ", sql or "")[:_SQL_GATE_LOG_LIMIT],
    )
