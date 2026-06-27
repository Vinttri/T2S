"""Ordered registry of deterministic, sqlglot-based SQL gates.

Design rules (owner):
- EVERYTHING goes through sqlglot (AST), across the supported dialects.
- Every gate is GENERAL — no table/column/query/DB hardcodes. A gate's truth
  comes from the schema/graph passed in ``GateContext``, never from literals.
- Gates that only make sense for a dialect family declare it in ``dialects``;
  dialect-agnostic gates leave it ``None`` and run everywhere.
- There is an explicit ORDER and a single registry (:data:`GATE_REGISTRY`).
- A gate may REPAIR (mutate the AST → corrected SQL) or only FLAG (add issues
  that drive the existing regenerate/repair loop). Repairs are deterministic.

The identifier / read-only / single-statement checks already live in
``sql_gate.validate_sql`` (also sqlglot) and run as registry stage 0 via
:func:`run_gates`; the gates here add JSON-path and join-integrity coverage.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

try:  # sqlglot is a hard dependency; degrade gracefully
    import sqlglot
    from sqlglot import exp
    _SQLGLOT = True
except Exception:  # pragma: no cover  # pylint: disable=broad-exception-caught
    sqlglot = None
    exp = None
    _SQLGLOT = False

from .sql_gate import sqlglot_dialect

# Dialect families that express JSON access with -> / ->> arrows (so the
# operator/path gates apply). Impala/Hive use get_json_object — a different
# shape handled by their own (future) group, not these gates.
_ARROW_JSON_DIALECTS = {"postgres", "mysql"}


@dataclass
class GateContext:
    """Everything a gate needs — all derived from the live schema/graph."""
    db_type: Optional[str] = None
    json_paths: dict = field(default_factory=dict)   # {col_lower: {"leaves":{leaf:(k..)}, "full":set}}
    join_set: set = field(default_factory=set)        # {frozenset({"t.c", "t2.c2"})}
    known_tables: set = field(default_factory=set)    # real-case table names from the schema
    known_columns: set = field(default_factory=set)   # real-case column names from the schema
    dialect: Optional[str] = None
    db_family: Optional[str] = None                   # 'postgres' | 'mysql' | 'impala' | ...


@dataclass
class GateOutcome:
    name: str
    ok: bool
    issues: list = field(default_factory=list)
    repaired: bool = False


# --------------------------------------------------------------------------- #
# Shared AST helpers (dialect-aware, general)
# --------------------------------------------------------------------------- #
def _json_path_sql(col_sql: str, keys: list[str], db_family: Optional[str]) -> str:
    """Render a JSON path correctly for the dialect: arrows for postgres/mysql
    (``->`` for every intermediate key, ``->>`` for the leaf), get_json_object
    for impala/hive. ``col_sql`` is the already-rendered root column."""
    fam = (db_family or "postgres").lower()
    if fam in {"impala", "hive", "spark", "trino", "presto"}:
        return f"get_json_object({col_sql}, '$.{'.'.join(keys)}')"
    expr = col_sql
    for key in keys[:-1]:
        expr += f"->'{key}'"
    expr += f"->>'{keys[-1]}'"
    return expr


def _extract_key(expr_node) -> Optional[str]:
    """Bare JSON key from a JSONExtract's expression, across sqlglot shapes:
    a ``JSONPath`` (``JSONPathRoot`` + ``JSONPathKey('k')``) or a plain literal.
    """
    if expr_node is None:
        return None
    if isinstance(expr_node, exp.JSONPath):
        for part in expr_node.expressions:
            if isinstance(part, exp.JSONPathKey):
                this = part.this
                return this if isinstance(this, str) else getattr(this, "name", str(this))
        return None
    name = getattr(expr_node, "name", "") or str(expr_node)
    return name.strip("'\"$.")


def _chain_root_and_keys(node):
    """From a JSON-extract chain node, return (root_expr, [keys...])."""
    keys: list[str] = []
    cur = node
    while exp is not None and isinstance(cur, (exp.JSONExtract, exp.JSONExtractScalar)):
        key = _extract_key(cur.expression)
        if key is None:
            return cur, []
        keys.append(key)
        cur = cur.this
    keys.reverse()
    return cur, keys


def _is_outermost_json(node) -> bool:
    parent = node.parent
    return not (isinstance(parent, (exp.JSONExtract, exp.JSONExtractScalar))
                and parent.this is node)


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def gate_json_paths(expr, ctx: GateContext) -> GateOutcome:
    """Repair JSON access so every intermediate key uses ``->`` (jsonb) and only
    the leaf uses ``->>`` (text), and so the key path actually EXISTS for that
    column. Wrong/short paths whose leaf maps to exactly one real path are
    rebuilt; unknown columns get operator-only correction. General: the valid
    paths come from ``ctx.json_paths`` (the schema), nothing hardcoded.
    """
    issues: list[str] = []
    changed = False
    for node in list(expr.find_all(exp.JSONExtract, exp.JSONExtractScalar)):
        if not _is_outermost_json(node):
            continue
        root, keys = _chain_root_and_keys(node)
        if not keys or not isinstance(root, exp.Column):
            continue
        col = (root.name or "").lower()
        correct = list(keys)
        info = ctx.json_paths.get(col)
        if info:
            full = info.get("full") or set()
            leaves = info.get("leaves") or {}
            if tuple(keys) not in full:
                leaf = keys[-1]
                if leaf in leaves:
                    correct = list(leaves[leaf])  # repair short/wrong path via unique leaf
                # else: unknown leaf — keep keys, only operators get fixed
        rebuilt = _json_path_sql(root.sql(dialect=ctx.dialect), correct, ctx.db_family)
        if rebuilt != node.sql(dialect=ctx.dialect):
            try:
                node.replace(sqlglot.parse_one(rebuilt, read=ctx.dialect))
                changed = True
                issues.append(f"{col}: {'/'.join(keys)} -> {'.'.join(correct)}")
            except Exception:  # pylint: disable=broad-exception-caught
                continue
    return GateOutcome("json_paths", ok=not issues, issues=issues, repaired=changed)


def gate_join_integrity(expr, ctx: GateContext) -> GateOutcome:
    """FLAG equality joins between two qualified columns that are NOT a declared
    FK edge (``ctx.join_set`` from the graph). Catches invented joins; flag-only
    (a wrong join can't be auto-fixed safely) so the regenerate loop handles it.
    General: the valid joins come from the graph, not literals.
    """
    if not ctx.join_set:
        return GateOutcome("join_integrity", ok=True)
    issues: list[str] = []
    for eq in expr.find_all(exp.EQ):
        left, right = eq.left, eq.right
        if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
            continue
        lt, rt = (left.table or "").lower(), (right.table or "").lower()
        if not lt or not rt or lt == rt:
            continue  # only cross-alias equalities look like joins
        lc, rc = f"{lt}.{(left.name or '').lower()}", f"{rt}.{(right.name or '').lower()}"
        # join_set is keyed by real table.column; alias may differ, so match on
        # the column-name pair regardless of alias prefix as a generous check.
        pair_names = frozenset({(left.name or "").lower(), (right.name or "").lower()})
        ok = any(pair_names == frozenset(p.split(".")[-1] for p in edge)
                 for edge in ctx.join_set)
        if not ok:
            issues.append(f"join not in FK graph: {lc} = {rc}")
    return GateOutcome("join_integrity", ok=not issues, issues=issues)


# Reserved words that are NOT valid as a BARE identifier in one or more supported
# dialects (postgres / mysql / tsql / oracle / hive). Reference data — like a
# stop-word list — NOT a per-query or per-DB hardcode: any column/table/alias
# whose name is here must be quoted for the target dialect. Quoting a word that
# happens to be harmless in a given dialect is still valid SQL, so applying this
# uniformly is safe, and sqlglot renders the correct quote char per dialect
# (postgres "x", hive/mysql `x`, tsql [x]). sqlglot's own keyword introspection
# is incomplete per dialect (e.g. it does not flag postgres ``order``/``value``),
# which is why this curated set exists.
_RESERVED_IDENTIFIERS = {
    "all", "alter", "analyse", "analyze", "and", "any", "array", "as", "asc",
    "authorization", "begin", "between", "both", "by", "case", "cast", "check",
    "collate", "column", "comment", "commit", "constraint", "create", "cross",
    "current", "current_date", "current_role", "current_time", "current_timestamp",
    "current_user", "cursor", "database", "default", "deferrable", "delete", "desc",
    "describe", "distinct", "do", "drop", "else", "end", "escape", "except",
    "exists", "false", "fetch", "filter", "for", "foreign", "from", "full",
    "function", "grant", "group", "groups", "having", "if", "in", "index",
    "initial", "inner", "insert", "intersect", "interval", "into", "is", "join",
    "key", "language", "leading", "left", "level", "like", "limit", "lock",
    "natural", "not", "null", "of", "offset", "on", "only", "or", "order", "outer",
    "over", "overlaps", "partition", "position", "primary", "range", "rank",
    "references", "rename", "replace", "reset", "returning", "revoke", "right",
    "role", "rollback", "row", "rows", "schema", "select", "session", "session_user",
    "set", "show", "some", "start", "system", "table", "then", "time", "timestamp",
    "to", "trailing", "trigger", "true", "truncate", "union", "unique", "update",
    "usage", "use", "user", "using", "value", "values", "view", "when", "where",
    "window", "with",
}


def gate_reserved_identifiers(expr, ctx: GateContext) -> GateOutcome:
    """Quote identifiers (columns / tables / aliases) whose name collides with a
    SQL reserved word, using the TARGET DIALECT's quote char. A column literally
    named ``order`` / ``value`` / ``level`` / ``user`` is invalid as a bare
    identifier in most dialects; quoting makes the SQL valid everywhere. General
    and dialect-aware: the reserved set is dialect reference data (not per-query),
    and sqlglot emits the right quote char on render. Repairs the AST in place;
    function names are untouched (they are not ``exp.Identifier`` nodes).
    """
    changed = False
    issues: list[str] = []
    for ident in expr.find_all(exp.Identifier):
        if ident.quoted:
            continue
        name = ident.this if isinstance(ident.this, str) else None
        if not name or name.lower() not in _RESERVED_IDENTIFIERS:
            continue
        ident.set("quoted", True)
        changed = True
        if name.lower() not in issues:
            issues.append(name.lower())
    return GateOutcome("reserved_identifiers", ok=not issues, issues=issues,
                       repaired=changed)


_FOLD_LOWER_DIALECTS = {"postgres"}


def gate_quote_case_sensitive_identifiers(expr, ctx: GateContext) -> GateOutcome:
    """Quote table/column identifiers that name a mixed-case schema object.

    PostgreSQL folds an UNQUOTED identifier to lower-case, so a real table/column
    like ``PaymentProcessingEvents`` referenced without quotes resolves to a
    non-existent ``paymentprocessingevents`` ("relation does not exist").
    sqlglot does NOT auto-quote on render, and the model only sometimes quotes,
    so this normalises every reference. Schema-driven: it quotes ONLY identifiers
    whose lower-cased form matches a known schema object whose real name is not
    all-lower-case — a genuinely lower-case object is left unquoted (quoting it
    with the wrong case would itself break it). Restores the real casing too, so
    a model that wrote the name lower-case is repaired. Targets table references
    and column references (name + table qualifier), not alias definitions.
    """
    lookup: dict[str, str] = {}
    for nm in set(ctx.known_tables or ()) | set(ctx.known_columns or ()):
        s = str(nm)
        if s and s != s.lower():
            lookup[s.lower()] = s
    if not lookup:
        return GateOutcome("quote_case_sensitive_identifiers", ok=True)
    changed = False
    issues: list[str] = []
    for ident in expr.find_all(exp.Identifier):
        if ident.quoted or not isinstance(ident.this, str):
            continue
        real = lookup.get(ident.this.lower())
        if not real:
            continue
        parent = ident.parent
        is_ref = (
            (isinstance(parent, exp.Table) and parent.this is ident)
            or (isinstance(parent, exp.Column)
                and (parent.this is ident or parent.args.get("table") is ident))
        )
        if not is_ref:
            continue
        ident.set("this", real)
        ident.set("quoted", True)
        changed = True
        if real not in issues:
            issues.append(real)
    return GateOutcome("quote_case_sensitive_identifiers", ok=not issues,
                       issues=issues, repaired=changed)


_NUM_AGG = (exp.Avg, exp.Sum, exp.Min, exp.Max) if exp else ()
_ARITH = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod) if exp else ()
_CMP = (exp.LT, exp.LTE, exp.GT, exp.GTE, exp.EQ, exp.NEQ) if exp else ()


def _effective_parent(node):
    """Parent of *node*, transparently skipping wrapping parentheses."""
    p = node.parent
    while isinstance(p, exp.Paren):
        p = p.parent
    return p


def gate_json_numeric_cast(expr, ctx: GateContext) -> GateOutcome:
    """Cast a JSON text-extraction (``->>``) to numeric when it is used in a
    NUMERIC context (arithmetic operand, numeric aggregate arg, or compared to a
    number). In postgres ``->>`` yields TEXT, so ``9 - col->>'k'`` or
    ``AVG(col->>'k')`` fails to execute; the model often casts the *outer*
    expression (too late) or forgets entirely. This places the cast on the
    extraction itself. Deterministic and general — needed because the native /sql
    path is generate-only (no execution-repair/healer to catch the type error).
    """
    changed = False
    count = 0
    for node in list(expr.find_all(exp.JSONExtractScalar)):
        parent = _effective_parent(node)
        if isinstance(parent, exp.Cast):
            continue  # already cast
        numeric_ctx = isinstance(parent, _ARITH) or isinstance(parent, _NUM_AGG)
        if not numeric_ctx and isinstance(parent, _CMP):
            other = parent.right if node is parent.left or (
                isinstance(parent.left, exp.Paren) and node in parent.left.flatten()
            ) else parent.left
            numeric_ctx = isinstance(other, exp.Literal) and other.is_number
        if not numeric_ctx:
            continue
        node.replace(exp.Cast(this=node.copy(),
                              to=exp.DataType.build("DOUBLE PRECISION")))
        changed = True
        count += 1
    issues = [f"cast {count} json text-extraction(s) to numeric"] if count else []
    return GateOutcome("json_numeric_cast", ok=not issues, issues=issues,
                       repaired=changed)


def _div_has_float(node) -> bool:
    """True if a division subtree already forces float math (a cast or a decimal
    literal anywhere inside it)."""
    for n in node.walk():
        if isinstance(n, exp.Cast):
            return True
        if isinstance(n, exp.Literal) and n.is_number and "." in (n.name or ""):
            return True
    return False


def gate_integer_division(expr, ctx: GateContext) -> GateOutcome:
    """Force float division where the model wrote integer/integer (truncates).

    Weak models routinely emit ``msec_val / 1000`` (→ truncated seconds) or
    ``COUNT(*) / COUNT(DISTINCT x)`` (→ integer 0/1) when a fractional result is
    meant. Deterministic + conservative: only (a) turn an INTEGER LITERAL divisor
    into a float (``/ 1000`` → ``/ 1000.0``), or (b) when a COUNT is involved and
    nothing already forces float, cast the numerator. Ambiguous ``a / b`` over
    unknown-typed columns is left untouched. Runs on every dialect (integer
    division truncates everywhere); needed because /sql is generate-only.
    """
    changed = False
    count = 0
    for div in list(expr.find_all(exp.Div)):
        right = div.expression
        if isinstance(right, exp.Literal) and right.is_number and "." not in (right.name or ""):
            div.set("expression", exp.Literal.number(right.name + ".0"))
            changed = True
            count += 1
            continue
        if not _div_has_float(div) and (
            isinstance(div.this, exp.Count) or isinstance(div.expression, exp.Count)
        ):
            div.set("this", exp.Cast(this=div.this.copy(),
                                     to=exp.DataType.build("DOUBLE PRECISION")))
            changed = True
            count += 1
    issues = [f"forced float division in {count} place(s)"] if count else []
    return GateOutcome("integer_division", ok=not issues, issues=issues, repaired=changed)


# Dialect families whose DESC ordering defaults to NULLS FIRST — so a NULL key
# silently steals the top of a ranking. Postgres and Oracle behave this way;
# MySQL/SQLite already sort NULLs last on DESC, so they need no repair (and some
# lack the NULLS LAST clause). Reference data, not a per-query/DB hardcode.
_NULLS_FIRST_ON_DESC_DIALECTS = {"postgres"}


def gate_ranking_nulls_last(expr, ctx: GateContext) -> GateOutcome:
    """In a top-N ranking (``ORDER BY <key> DESC`` + ``LIMIT``), force a NULL
    ordering key to sort LAST so an undefined / missing metric value cannot
    occupy the top slot.

    Postgres defaults a DESC ordering to NULLS FIRST, so
    ``ORDER BY MAX(points) DESC LIMIT 1`` returns a row whose metric is NULL
    (no data) instead of the real maximum — the LIMIT then truncates to exactly
    that wrong row. For a ranking the intent is always the opposite: missing
    values belong at the bottom. Deterministic + conservative: fires ONLY when a
    LIMIT is present (the truncation that makes a leading NULL actually steal the
    answer) and only on DESC keys not already marked NULLS LAST. Scoped to
    dialects whose DESC default is NULLS FIRST. General — no table/column/query
    knowledge, pure ORDER-BY structure.
    """
    changed = False
    count = 0
    for select in expr.find_all(exp.Select):
        if select.args.get("limit") is None:
            continue
        order = select.args.get("order")
        if order is None:
            continue
        for ordered in order.find_all(exp.Ordered):
            # attribute each ORDER key to THIS select (skip a nested subquery's)
            if ordered.find_ancestor(exp.Select) is not select:
                continue
            if not ordered.args.get("desc"):
                continue  # ASC already sorts NULLs last in these dialects
            if ordered.args.get("nulls_first") is False:
                continue  # already explicit NULLS LAST
            ordered.set("nulls_first", False)
            changed = True
            count += 1
    issues = [f"forced NULLS LAST on {count} DESC ranking key(s)"] if count else []
    return GateOutcome("ranking_nulls_last", ok=not issues, issues=issues,
                       repaired=changed)


def gate_symmetric_case_fold(expr, ctx: GateContext) -> GateOutcome:
    """Make case-folding SYMMETRIC: when an equality compares a case-folded string
    literal (``LOWER('Advanced')`` / ``UPPER(...)``) against a BARE column, wrap the
    column in the same function so the comparison is genuinely case-insensitive.

    Weak models often write ``col = LOWER('Advanced')`` intending a case-insensitive
    match, but that lowercases only the literal (→ ``col = 'advanced'``) and returns
    ZERO rows whenever the stored value has any other case (``'Advanced'``). Folding
    the column too — ``LOWER(col) = LOWER('Advanced')`` — fixes it. Safe in both
    directions: if the column is already lower-case the extra LOWER is a no-op.
    Deterministic, dialect-agnostic (LOWER/UPPER exist everywhere). Only fires when
    the folded side wraps a LITERAL (so column-vs-column comparisons are untouched).
    """
    if exp is None:
        return GateOutcome("symmetric_case_fold", ok=True)
    cf = (exp.Lower, exp.Upper)
    changed = False
    count = 0
    for cmp in list(expr.find_all(exp.EQ, exp.NEQ)):
        left, right = cmp.left, cmp.right
        if left is None or right is None:
            continue
        if isinstance(right, cf) and isinstance(right.this, exp.Literal) and isinstance(left, exp.Column):
            cmp.set("this", type(right)(this=left.copy()))
            changed = True
            count += 1
        elif isinstance(left, cf) and isinstance(left.this, exp.Literal) and isinstance(right, exp.Column):
            cmp.set("expression", type(left)(this=right.copy()))
            changed = True
            count += 1
    issues = [f"made case-folding symmetric on {count} comparison(s)"] if count else []
    return GateOutcome("symmetric_case_fold", ok=not issues, issues=issues, repaired=changed)


# --------------------------------------------------------------------------- #
# Registry (ORDER matters): (name, fn, applicable_db_families_or_None)
# --------------------------------------------------------------------------- #
GATE_REGISTRY: list[tuple[str, Callable, Optional[set]]] = [
    ("json_paths", gate_json_paths, _ARROW_JSON_DIALECTS),
    ("json_numeric_cast", gate_json_numeric_cast, _ARROW_JSON_DIALECTS),
    ("integer_division", gate_integer_division, None),
    ("ranking_nulls_last", gate_ranking_nulls_last, _NULLS_FIRST_ON_DESC_DIALECTS),
    ("symmetric_case_fold", gate_symmetric_case_fold, None),
    ("join_integrity", gate_join_integrity, None),
    ("reserved_identifiers", gate_reserved_identifiers, None),
    ("quote_case_sensitive_identifiers", gate_quote_case_sensitive_identifiers,
     _FOLD_LOWER_DIALECTS),
]


def _db_family(db_type: Optional[str]) -> str:
    dt = (db_type or "").lower()
    if dt.startswith("postg"):
        return "postgres"
    if "mysql" in dt or "maria" in dt:
        return "mysql"
    if "impala" in dt or "hive" in dt:
        return "impala"
    return dt or "postgres"


def run_gates(sql: str, ctx: GateContext) -> tuple[str, list[str], bool]:
    """Run the registry in order. Returns ``(final_sql, issues, repaired)``.

    Parses once with the dialect, applies each applicable gate's repairs to the
    shared AST, re-renders if anything changed. Fail-safe: returns the input on
    any parse error.
    """
    if not _SQLGLOT or not sql:
        return sql, [], False
    ctx.dialect = sqlglot_dialect(ctx.db_type)
    ctx.db_family = _db_family(ctx.db_type)
    try:
        expr = sqlglot.parse_one(sql, read=ctx.dialect)
    except Exception:  # pylint: disable=broad-exception-caught
        return sql, [], False
    all_issues: list[str] = []
    repaired = False
    for name, fn, families in GATE_REGISTRY:
        if families is not None and ctx.db_family not in families:
            continue
        try:
            outcome = fn(expr, ctx)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("gate %s failed: %s", name, str(exc)[:160])
            continue
        if outcome.issues:
            all_issues.extend(f"{name}: {i}" for i in outcome.issues)
        repaired = repaired or outcome.repaired
    final_sql = expr.sql(dialect=ctx.dialect) if repaired else sql
    return final_sql, all_issues, repaired
