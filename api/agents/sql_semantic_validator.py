"""Deterministic SQL semantic validator for a Text2SQL system (Impala dialect).

Pure static analysis: parse generated SQL with sqlglot and use schema metadata
(NOT-NULL flags, primary keys, foreign keys) to catch recurring correctness bugs.
Returns structured issues that an LLM SQL-writer can consume for repair.

NO LLM calls here. The single public entry point is :func:`validate_sql`.

The three checks implemented:

* ``null_on_notnull`` (error)  -- IS NULL / IS NOT NULL / = 0 / != 0 on a NOT-NULL column.
* ``non_fk_join``     (warn)   -- a JOIN equality whose two columns are not a declared FK pair.
* ``partial_key_fanout`` (warn) -- a joined table with a composite PK constrained on
  only part of its key while a non-distinct aggregate is present (row fan-out).

All checks are schema-driven (no hardcoded table/column names) and conservative:
anything that cannot be resolved against the schema is skipped silently so the
validator never emits false positives on unknown identifiers, and it never raises.
"""

import logging
import re

import sqlglot
from sqlglot import exp

logger = logging.getLogger(__name__)

def _resolve_dialect(db_type):
    """Return a dialect name sqlglot can parse, or None for the default dialect.

    Delegates to the single canonical resolver ``sql_gate.sqlglot_dialect`` so the
    db_type -> sqlglot-dialect mapping (Impala -> hive, MSSQL -> tsql, Oracle ->
    oracle, ...) lives in exactly one place. Thin wrapper kept for existing callers.
    """
    from api.sql_utils.sql_gate import sqlglot_dialect
    return sqlglot_dialect(db_type)


# --------------------------------------------------------------------------- #
# Schema helpers
# --------------------------------------------------------------------------- #
def _norm(name):
    """Lowercase + strip surrounding quotes/backticks from an identifier."""
    if name is None:
        return None
    return str(name).strip().strip('"').strip("`").lower()


def _qualified_table_name(table_exp):
    """Return the lowercased qualified name of an exp.Table, e.g. 'dm_mis.v_f_contract'.

    Includes the db/catalog prefix when present so it can match schema keys.
    """
    if not isinstance(table_exp, exp.Table):
        return None
    parts = []
    catalog = table_exp.args.get("catalog")
    db = table_exp.args.get("db")
    this = table_exp.this
    if catalog is not None:
        parts.append(_norm(catalog.name if hasattr(catalog, "name") else catalog))
    if db is not None:
        parts.append(_norm(db.name if hasattr(db, "name") else db))
    if this is not None:
        parts.append(_norm(this.name if hasattr(this, "name") else this))
    parts = [p for p in parts if p]
    return ".".join(parts) if parts else None


def _resolve_schema_table(qualified, schema):
    """Resolve a (possibly partially) qualified table name to a schema key.

    Tries the fully-qualified name first, then a suffix/short-name match
    (so 'v_f_contract' matches a schema key 'dm_mis.v_f_contract').
    Returns the schema key or None.
    """
    if not qualified or not schema:
        return None
    if qualified in schema:
        return qualified
    short = qualified.split(".")[-1]
    if short in schema:
        return short
    # suffix match against the short component of every schema key
    matches = [k for k in schema if k.split(".")[-1] == short]
    if len(matches) == 1:
        return matches[0]
    # also try matching when the SQL gave a short name and a schema key is qualified
    matches = [k for k in schema if k == qualified or k.endswith("." + qualified)]
    if len(matches) == 1:
        return matches[0]
    return None


def _col_meta(schema_key, col, schema):
    """Return the column metadata dict for schema[schema_key].columns[col], or None."""
    if not schema_key:
        return None
    tbl = schema.get(schema_key)
    if not tbl:
        return None
    cols = tbl.get("columns") or {}
    return cols.get(_norm(col))


# --------------------------------------------------------------------------- #
# Alias map
# --------------------------------------------------------------------------- #
def _build_alias_map(parsed, schema):
    """Map every alias / table short-name -> resolved schema key.

    Walks the FROM clause and all JOINs. For each table we register:
      * its alias (if any) -> schema key
      * its qualified name -> schema key
      * its short (unqualified) name -> schema key
    Only entries that resolve against the schema are kept, so bare-column
    resolution stays conservative.
    """
    alias_map = {}

    def register(table_exp):
        if not isinstance(table_exp, exp.Table):
            return
        qualified = _qualified_table_name(table_exp)
        schema_key = _resolve_schema_table(qualified, schema)
        if schema_key is None:
            return
        alias = table_exp.alias
        if alias:
            alias_map[_norm(alias)] = schema_key
        if qualified:
            alias_map[qualified] = schema_key
            alias_map[qualified.split(".")[-1]] = schema_key

    # Register FROM-clause tables. Use find(exp.From) for robustness across
    # sqlglot versions (the args key has been both 'from' and 'from_').
    for from_clause in parsed.find_all(exp.From):
        for tbl in from_clause.find_all(exp.Table):
            register(tbl)

    for join in parsed.find_all(exp.Join):
        for tbl in join.find_all(exp.Table):
            register(tbl)

    return alias_map


def _resolve_column(col_exp, alias_map, schema):
    """Resolve an exp.Column to (schema_key, column_lower) or (None, None).

    Uses the column's table-qualifier (alias) via alias_map; for a bare column
    it resolves to the single schema table (among those in alias_map) that has
    that column, else gives up.
    """
    if not isinstance(col_exp, exp.Column):
        return None, None
    col = _norm(col_exp.name)
    if not col:
        return None, None
    qualifier = col_exp.table
    if qualifier:
        schema_key = alias_map.get(_norm(qualifier))
        if schema_key and _col_meta(schema_key, col, schema) is not None:
            return schema_key, col
        return None, None
    # bare column: find the unique table in scope that owns it
    owners = []
    for schema_key in set(alias_map.values()):
        if _col_meta(schema_key, col, schema) is not None:
            owners.append(schema_key)
    if len(owners) == 1:
        return owners[0], col
    return None, None


# --------------------------------------------------------------------------- #
# Check 1: null_on_notnull
# --------------------------------------------------------------------------- #
def _is_zero_literal(node):
    """True if node is the numeric literal 0."""
    if isinstance(node, exp.Literal) and not node.args.get("is_string"):
        try:
            return float(node.name) == 0.0
        except (ValueError, TypeError):
            return False
    return False


def _check_null_on_notnull(parsed, alias_map, schema, issues):
    """Flag IS NULL / IS NOT NULL / = 0 / != 0 predicates on NOT-NULL columns."""
    scopes = []
    where = parsed.find(exp.Where)
    if where is not None:
        scopes.append(where)
    having = parsed.find(exp.Having)
    if having is not None:
        scopes.append(having)

    seen = set()

    def emit(schema_key, col, always_true):
        col_meta = _col_meta(schema_key, col, schema)
        if not col_meta or col_meta.get("nullable") is not False:
            return  # only flag explicitly NOT-NULL columns
        dedup = (schema_key, col, always_true)
        if dedup in seen:
            return
        seen.add(dedup)
        if always_true:
            message = (
                "filter is always true, excludes nothing -- likely the wrong column "
                "was chosen for the intended (active/closed/has-value) condition"
            )
        else:
            message = "filter can never match / treats a real value as absent"
        fix_hint = (
            f"{schema_key}.{col} is NOT NULL; for an 'active/open' condition use the "
            "close/end date with (close_date IS NULL OR close_date > D) on the right "
            "column, or a value/range predicate; do not test IS NULL/=0 on a mandatory "
            "column."
        )
        issues.append({
            "check": "null_on_notnull",
            "severity": "error",
            "table": schema_key,
            "column": col,
            "message": message,
            "fix_hint": fix_hint,
        })

    for scope in scopes:
        # IS NULL / IS NOT NULL
        for is_node in scope.find_all(exp.Is):
            left = is_node.this
            right = is_node.expression
            if not isinstance(left, exp.Column):
                continue
            # IS NOT NULL is an Is wrapped in a Not
            is_not = isinstance(is_node.parent, exp.Not)
            if isinstance(right, exp.Null):
                schema_key, col = _resolve_column(left, alias_map, schema)
                if schema_key is None:
                    continue
                # IS NULL -> never matches (always_true=False); IS NOT NULL -> always true
                emit(schema_key, col, always_true=is_not)

        # = 0  and  != 0 / <> 0
        for eq_node in list(scope.find_all(exp.EQ)) + list(scope.find_all(exp.NEQ)):
            left, right = eq_node.this, eq_node.expression
            col_exp = None
            if isinstance(left, exp.Column) and _is_zero_literal(right):
                col_exp = left
            elif isinstance(right, exp.Column) and _is_zero_literal(left):
                col_exp = right
            if col_exp is None:
                continue
            schema_key, col = _resolve_column(col_exp, alias_map, schema)
            if schema_key is None:
                continue
            # = 0 on NOT-NULL -> treats a real value as absent (never matches the
            #   "missing" semantics); != 0 -> always-true-ish exclusion of nothing.
            emit(schema_key, col, always_true=isinstance(eq_node, exp.NEQ))


# --------------------------------------------------------------------------- #
# JOIN ON extraction (shared by checks 2 & 3)
# --------------------------------------------------------------------------- #
def _join_eq_pairs(join):
    """Return list of (left_col_exp, right_col_exp) for every column=column EQ in ON."""
    pairs = []
    on = join.args.get("on")
    if on is None:
        return pairs
    for eq in on.find_all(exp.EQ):
        left, right = eq.this, eq.expression
        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
            pairs.append((left, right))
    return pairs


# --------------------------------------------------------------------------- #
# Check 2: non_fk_join
# --------------------------------------------------------------------------- #
def _is_fk_link(a_key, a_col, b_key, b_col, schema):
    """True if a.col is a declared FK referencing b.col (ref_table/ref_col)."""
    meta = _col_meta(a_key, a_col, schema)
    if not meta:
        return False
    ref_table = _resolve_schema_table(_norm(meta.get("ref_table")), schema)
    ref_col = _norm(meta.get("ref_col"))
    return ref_table == b_key and ref_col == b_col


def _check_non_fk_join(parsed, alias_map, schema, issues):
    """Flag JOIN equalities whose two columns are not a declared FK pair."""
    for join in parsed.find_all(exp.Join):
        for left, right in _join_eq_pairs(join):
            a_key, a_col = _resolve_column(left, alias_map, schema)
            b_key, b_col = _resolve_column(right, alias_map, schema)
            if a_key is None or b_key is None:
                continue
            if a_key == b_key:
                continue  # self-join column compare; not a cross-table FK question
            # Conservative: never flag when the column NAMES match (shared key/date col).
            if a_col == b_col:
                continue
            fk_ab = _is_fk_link(a_key, a_col, b_key, b_col, schema)
            fk_ba = _is_fk_link(b_key, b_col, a_key, a_col, schema)
            if fk_ab or fk_ba:
                continue
            issues.append({
                "check": "non_fk_join",
                "severity": "warn",
                "table": a_key,
                "column": a_col,
                "message": (
                    f"join equality {a_key}.{a_col} = {b_key}.{b_col} is not a declared "
                    "FK pair (possible wrong join path)"
                ),
                "fix_hint": (
                    f"join via a declared FK relationship; {a_key}.{a_col} and "
                    f"{b_key}.{b_col} are not an FK pair (check the FK links in the schema)."
                ),
            })


# --------------------------------------------------------------------------- #
# Check 3: partial_key_fanout
# --------------------------------------------------------------------------- #
def _has_nondistinct_aggregate(parsed):
    """True if the query has a non-distinct SUM/AVG/MIN/MAX or COUNT(*)/COUNT(col)."""
    for agg in parsed.find_all(exp.Sum, exp.Avg, exp.Min, exp.Max):
        if not agg.args.get("distinct"):
            return True
    for cnt in parsed.find_all(exp.Count):
        if cnt.args.get("distinct"):
            continue
        arg = cnt.this
        # COUNT(*) -> arg is a Star; COUNT(col) -> arg is a Column. Both inflate on fan-out.
        if isinstance(arg, (exp.Star, exp.Column)) or arg is None:
            return True
    return False


def _check_partial_key_fanout(parsed, alias_map, schema, issues):
    """Flag joins to a composite-PK table that constrain only part of its key."""
    if not _has_nondistinct_aggregate(parsed):
        return

    for join in parsed.find_all(exp.Join):
        # The joined (right-side) table(s) of this JOIN.
        for table_exp in join.find_all(exp.Table):
            qualified = _qualified_table_name(table_exp)
            schema_key = _resolve_schema_table(qualified, schema)
            if schema_key is None:
                continue
            pk = [_norm(c) for c in (schema.get(schema_key, {}).get("pk") or [])]
            if len(pk) < 2:
                continue  # only composite PKs fan out this way

            # Which PK columns of this table are equated in the ON clause?
            constrained = set()
            for left, right in _join_eq_pairs(join):
                for col_exp in (left, right):
                    c_key, c_col = _resolve_column(col_exp, alias_map, schema)
                    if c_key == schema_key and c_col in pk:
                        constrained.add(c_col)

            if not constrained:
                continue  # nothing of this table's PK is in this ON; not our case
            if constrained.issuperset(pk):
                continue  # full key constrained -> no fan-out

            used = sorted(constrained)
            issues.append({
                "check": "partial_key_fanout",
                "severity": "warn",
                "table": schema_key,
                "column": ",".join(used),
                "message": (
                    f"join to {schema_key} uses only part of its composite key "
                    f"{used} of PK {pk}; rows fan out and SUM/COUNT/AVG over joined "
                    "columns are inflated."
                ),
                "fix_hint": (
                    f"join on the FULL key of {schema_key} (all of: {pk}) OR aggregate "
                    "with COUNT(DISTINCT <entity key>) / pre-aggregate "
                    f"{schema_key} to the entity grain before joining."
                ),
            })


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
_NUMERIC_TARGET_RE = re.compile(r"double|decimal|float|int|numeric|real|bigint", re.IGNORECASE)


def _is_numeric_str(s) -> bool:
    s = str(s).strip()
    if not s:
        return False
    try:
        float(s)
        return True
    except (ValueError, TypeError):
        return False


def _check_cast_categorical(parsed, alias_map, schema, issues):
    """Flag CAST(col AS <numeric>) where the column's SAMPLE VALUES are all
    non-numeric (letters/labels): the cast returns NULL and empties the metric."""
    for cast in parsed.find_all(exp.Cast):
        to_type = str(cast.args.get("to") or "")
        if not _NUMERIC_TARGET_RE.search(to_type):
            continue
        operand = cast.this
        if not isinstance(operand, exp.Column):
            continue
        schema_key, col = _resolve_column(operand, alias_map, schema)
        if not schema_key or not col:
            continue
        meta = (schema.get(schema_key, {}).get("columns", {}) or {}).get(col, {})
        samples = meta.get("samples") or []
        if not samples:
            continue
        if any(_is_numeric_str(s) for s in samples):
            continue  # at least one numeric sample -> castable
        issues.append({
            "check": "cast_categorical",
            "severity": "error",
            "table": schema_key,
            "column": col,
            "message": (
                f"CAST({col} AS {to_type.strip()}) but its sample values are "
                f"non-numeric ({list(samples)[:4]}); the cast returns NULL and "
                f"empties the metric."
            ),
            "fix_hint": (
                "For a numeric aggregate choose a column whose SAMPLE VALUES are "
                "numeric (digits); do not cast a categorical/letter-coded column "
                "to a number."
            ),
        })


def _predicate_scopes(parsed):
    """Return the AST nodes that hold filtering predicates: the WHERE clause and
    every JOIN ... ON clause. (HAVING is post-aggregate and not a row filter.)"""
    scopes = []
    where = parsed.find(exp.Where)
    if where is not None:
        scopes.append(where)
    for join in parsed.find_all(exp.Join):
        on = join.args.get("on")
        if on is not None:
            scopes.append(on)
    return scopes


def _check_row_effectivity_window_unconstrained(parsed, alias_map, schema, issues):
    """Flag a perioded link/assignment/snapshot table that is queried AS OF a single
    date — its snapshot/report date is pinned with ``=`` — but whose ROW-EFFECTIVITY
    window (a begin-of-effect / end-of-effect column pair) is left unconstrained, so
    rows whose effect period does not cover that date are wrongly returned.

    Pure AST: it only inspects which columns appear in WHERE / JOIN-ON predicates and
    which snapshot column is pinned with ``=``. The schema supplies, per table, the
    ``validity_windows`` (begin/end pairs) and ``snapshot_dates`` — classified upstream
    from column METADATA (names/descriptions), never from the SQL text or the question.
    No row-effectivity window in the metadata => this check never fires (e.g. object
    open/close lifecycle dates are not windows, so they are not enforced here)."""
    scopes = _predicate_scopes(parsed)
    if not scopes:
        return

    constrained = set()       # (schema_key, col) referenced in any predicate
    snapshot_eq = set()       # schema_keys whose snapshot date is pinned with '='
    for scope in scopes:
        for col_exp in scope.find_all(exp.Column):
            sk, col = _resolve_column(col_exp, alias_map, schema)
            if sk and col:
                constrained.add((sk, col))
        for eq in scope.find_all(exp.EQ):
            for side in (eq.left, eq.right):
                if not isinstance(side, exp.Column):
                    continue
                sk, col = _resolve_column(side, alias_map, schema)
                if sk and col and col in (schema.get(sk, {}).get("snapshot_dates") or []):
                    snapshot_eq.add(sk)

    # No as-of anchor => not an as-of-D query; stay silent (period/all-history queries).
    if not snapshot_eq:
        return

    reported = set()
    for schema_key in set(alias_map.values()):
        if schema_key in reported:
            continue
        windows = (schema.get(schema_key) or {}).get("validity_windows") or []
        for window in windows:
            if not isinstance(window, (list, tuple)) or len(window) != 2:
                continue
            start_col, end_col = _norm(window[0]), _norm(window[1])
            start_ok = (schema_key, start_col) in constrained
            end_ok = (schema_key, end_col) in constrained
            if start_ok and end_ok:
                continue
            issues.append({
                "check": "row_effectivity_window_unconstrained",
                "severity": "warn",
                "table": schema_key,
                "column": end_col if not end_ok else start_col,
                "message": (
                    f"{schema_key} is queried as of a single date (its snapshot/report "
                    f"date is pinned with '='), but its row-effectivity window "
                    f"{start_col}..{end_col} is not fully constrained, so rows whose "
                    f"effect period does not cover that date are returned."
                ),
                "fix_hint": (
                    f"Constrain the as-of window on {schema_key} with the SAME as-of "
                    f"date D used for its snapshot/report date: {start_col} <= D AND "
                    f"{end_col} >= D (the end date is inclusive — the last day in "
                    f"effect); use ({end_col} IS NULL OR {end_col} >= D) only if "
                    f"{end_col} is nullable. Do not drop the snapshot-date filter."
                ),
            })
            reported.add(schema_key)
            break


def validate_sql(sql, schema, db_type="impala"):
    """Statically validate generated SQL against schema metadata.

    schema = {table_name_lower: {"columns": {col_lower: {"nullable": bool,
        "key": "PK"|"FK"|"", "ref_table": str|None, "ref_col": str|None}},
        "pk": [col_lower, ...]}}.

    Returns a list of issue dicts:
        {"check": str, "severity": "error"|"warn", "table": str, "column": str,
         "message": str, "fix_hint": str}.

    Never raises. On parse failure (or any internal error) returns [].
    """
    if not sql or not isinstance(sql, str):
        return []
    schema = schema or {}
    dialect = _resolve_dialect(db_type or "impala")

    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.SqlglotError as exc:  # parse/tokenize errors
        logger.debug("sql_semantic_validator parse failed: %s", str(exc)[:200])
        return []
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("sql_semantic_validator unexpected parse error: %s", str(exc)[:200])
        return []

    if parsed is None:
        return []

    issues = []
    try:
        alias_map = _build_alias_map(parsed, schema)
        _check_null_on_notnull(parsed, alias_map, schema, issues)
        _check_non_fk_join(parsed, alias_map, schema, issues)
        _check_partial_key_fanout(parsed, alias_map, schema, issues)
        _check_cast_categorical(parsed, alias_map, schema, issues)
        _check_row_effectivity_window_unconstrained(parsed, alias_map, schema, issues)
    except Exception as exc:  # pylint: disable=broad-except
        # Defensive: never let analysis bubble an exception to the caller.
        logger.debug("sql_semantic_validator analysis error: %s", str(exc)[:200])
        return issues

    return issues


# --------------------------------------------------------------------------- #
# Evidence grounding: every filter / metric column must be justified
# --------------------------------------------------------------------------- #
def _evidence_columns(column_evidence):
    """Set of justified column names (lowercased) — entries with a non-empty reason."""
    justified = set()
    for ev in column_evidence or []:
        if not isinstance(ev, dict):
            continue
        col = _norm(ev.get("column"))
        reason = str(ev.get("reason") or "").strip()
        if col and reason:
            justified.add(col)
    return justified


def check_evidence_grounding(sql, column_evidence, db_type="postgres", schema=None):
    """Validate that the SQL's filters and metrics are *grounded* in agent evidence.

    Pure sqlglot AST traceability check (NOT a business-correctness proof): every
    column used in a WHERE / HAVING predicate or inside a SUM/AVG/MIN/MAX metric
    must have a matching ``column_evidence`` entry (a non-empty ``reason``). JOIN
    keys are only checked when ``schema`` is supplied and are exempt when they are
    a declared FK pair. SELECT/GROUP/ORDER columns are intentionally NOT enforced
    here (advisory only) to avoid false positives.

    ``column_evidence`` is the list produced by the analysis agent. Returns a list
    of issue dicts (same shape as :func:`validate_sql`). Never raises. If the SQL
    cannot be parsed it returns ``[]`` (no false alarms on unparseable input).
    """
    if not sql or not isinstance(sql, str):
        return []
    dialect = _resolve_dialect(db_type or "postgres")
    try:
        parsed = sqlglot.parse_one(sql, read=dialect)
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("check_evidence_grounding parse failed: %s", str(exc)[:200])
        return []
    if parsed is None:
        return []

    justified = _evidence_columns(column_evidence)
    schema = schema or {}
    issues = []
    seen = set()

    def emit(col, kind, where, severity="error"):
        col_l = _norm(col)
        if not col_l or col_l in seen:
            return
        seen.add(col_l)
        issues.append({
            "check": kind,
            "severity": severity,
            "table": "",
            "column": col_l,
            "message": (
                f"column '{col_l}' is used in a {where} but has no agent "
                f"justification (column_evidence) explaining why"
            ),
            "fix_hint": (
                f"add a column_evidence entry "
                f"{{table, column: '{col_l}', role, reason}} citing the schema "
                f"description that justifies using {col_l} in the {where}, or "
                f"remove the {where} if the question does not require it"
            ),
        })

    try:
        # Metric columns first (SUM/AVG/MIN/MAX) so a metric column is labelled as
        # such even if it also appears elsewhere. COUNT is structural -> skipped.
        for agg in parsed.find_all(exp.Sum, exp.Avg, exp.Min, exp.Max):
            for col_node in agg.find_all(exp.Column):
                if _norm(col_node.name) not in justified:
                    emit(col_node.name, "ungrounded_metric", "metric aggregate")
        having = parsed.find(exp.Having)
        if having is not None:
            for col_node in having.find_all(exp.Column):
                if _norm(col_node.name) not in justified:
                    emit(col_node.name, "ungrounded_filter", "HAVING filter")
        where = parsed.find(exp.Where)
        if where is not None:
            for col_node in where.find_all(exp.Column):
                if _norm(col_node.name) not in justified:
                    emit(col_node.name, "ungrounded_filter", "WHERE filter")
        # JOIN keys: only when schema is available (so FK pairs can be exempted).
        if schema:
            alias_map = _build_alias_map(parsed, schema)
            for join in parsed.find_all(exp.Join):
                for left, right in _join_eq_pairs(join):
                    a_key, a_col = _resolve_column(left, alias_map, schema)
                    b_key, b_col = _resolve_column(right, alias_map, schema)
                    is_fk = bool(
                        a_key and b_key and (
                            _is_fk_link(a_key, a_col, b_key, b_col, schema)
                            or _is_fk_link(b_key, b_col, a_key, a_col, schema)
                        )
                    )
                    if is_fk:
                        continue
                    for cexp in (left, right):
                        if _norm(cexp.name) not in justified:
                            emit(cexp.name, "ungrounded_join", "JOIN condition", "warn")
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("check_evidence_grounding analysis error: %s", str(exc)[:200])
        return issues

    return issues


def evidence_repair_hint(issues):
    """Build a one-shot repair instruction from ungrounded-evidence issues.

    Returns "" when there is nothing hard to repair (only warnings)."""
    hard = [i for i in (issues or []) if i.get("severity") == "error"]
    if not hard:
        return ""
    lines = [f"  - {i['message']}; {i['fix_hint']}" for i in hard[:8]]
    return (
        "The previous SQL applies filters/metrics that are NOT justified in "
        "column_evidence. For EACH item below, either (a) add a column_evidence "
        "entry {table, column, role, reason} that cites the schema "
        "description/comment justifying that column, or (b) remove the predicate "
        "if the question does not require it. Return sql_query and column_evidence "
        "fully consistent.\n" + "\n".join(lines)
    )


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import sys

    # Hand-built schema (no sqlglot-schema object needed).
    #   v_f_contract: fact, end_date is NOT NULL, has FK contract_id -> d_party.party_id
    #   d_party: dim, single PK party_id
    #   f_balance: composite PK (acct_id, period_id) -> used for fan-out test
    SCHEMA = {
        "dm_mis.v_f_contract": {
            "columns": {
                "contract_id": {"nullable": False, "key": "FK",
                                "ref_table": "dm_mis.d_party", "ref_col": "party_id"},
                "end_date": {"nullable": False, "key": "", "ref_table": None, "ref_col": None},
                "amount": {"nullable": True, "key": "", "ref_table": None, "ref_col": None},
                "branch_code": {"nullable": True, "key": "", "ref_table": None, "ref_col": None},
            },
            "pk": ["contract_id"],
        },
        "dm_mis.d_party": {
            "columns": {
                "party_id": {"nullable": False, "key": "PK", "ref_table": None, "ref_col": None},
                "branch_code": {"nullable": True, "key": "", "ref_table": None, "ref_col": None},
                "party_name": {"nullable": True, "key": "", "ref_table": None, "ref_col": None},
            },
            "pk": ["party_id"],
        },
        "dm_mis.f_balance": {
            "columns": {
                "acct_id": {"nullable": False, "key": "PK", "ref_table": None, "ref_col": None},
                "period_id": {"nullable": False, "key": "PK", "ref_table": None, "ref_col": None},
                "bal": {"nullable": True, "key": "", "ref_table": None, "ref_col": None},
            },
            "pk": ["acct_id", "period_id"],
        },
        # Perioded link table: snapshot date (report_date) + row-effectivity window
        # (date_from / date_to). Used to exercise the new structural gate.
        "dm_mis.link_acct": {
            "columns": {
                "agreement_id": {"nullable": False, "key": "PK", "ref_table": None, "ref_col": None},
                "report_date": {"nullable": False, "key": "PK", "ref_table": None, "ref_col": None},
                "date_from": {"nullable": False, "key": "PK", "ref_table": None, "ref_col": None},
                "date_to": {"nullable": False, "key": "", "ref_table": None, "ref_col": None},
                "role_code": {"nullable": False, "key": "", "ref_table": None, "ref_col": None},
            },
            "pk": ["agreement_id", "report_date", "date_from"],
            "snapshot_dates": ["report_date"],
            "validity_windows": [["date_from", "date_to"]],
        },
    }

    results = []

    def check(label, condition):
        status = "PASS" if condition else "FAIL"
        print(f"[{status}] {label}")
        results.append(condition)

    # (a) IS NULL on NOT-NULL column -> null_on_notnull
    issues_a = validate_sql(
        "SELECT * FROM dm_mis.v_f_contract c WHERE c.end_date IS NULL", SCHEMA)
    check("(a) WHERE end_date IS NULL flags null_on_notnull",
          any(i["check"] == "null_on_notnull" and i["column"] == "end_date"
              for i in issues_a))

    # (b) IS NOT NULL on NOT-NULL column -> null_on_notnull (always-true)
    issues_b = validate_sql(
        "SELECT * FROM dm_mis.v_f_contract c WHERE c.end_date IS NOT NULL", SCHEMA)
    check("(b) WHERE end_date IS NOT NULL flags null_on_notnull",
          any(i["check"] == "null_on_notnull" and "always true" in i["message"]
              for i in issues_b))

    # (c) join on a non-FK column pair with DIFFERENT names -> non_fk_join
    issues_c = validate_sql(
        "SELECT c.amount FROM dm_mis.v_f_contract c "
        "JOIN dm_mis.d_party p ON c.branch_code = p.party_name", SCHEMA)
    check("(c) non-FK join on differently-named cols flags non_fk_join",
          any(i["check"] == "non_fk_join" for i in issues_c))

    # (d) SUM with a partial composite-key join -> partial_key_fanout
    issues_d = validate_sql(
        "SELECT SUM(b.bal) FROM dm_mis.v_f_contract c "
        "JOIN dm_mis.f_balance b ON c.contract_id = b.acct_id", SCHEMA)
    check("(d) SUM with partial composite-key join flags partial_key_fanout",
          any(i["check"] == "partial_key_fanout" and i["table"] == "dm_mis.f_balance"
              for i in issues_d))

    # (e) a clean query yields NO issues
    #     join via declared FK, full PK not relevant (d_party PK is single), no bad predicate.
    issues_e = validate_sql(
        "SELECT p.party_name, SUM(c.amount) "
        "FROM dm_mis.v_f_contract c "
        "JOIN dm_mis.d_party p ON c.contract_id = p.party_id "
        "WHERE c.amount > 0 GROUP BY p.party_name", SCHEMA)
    check("(e) clean query yields no issues", issues_e == [])

    # (f) join on SAME column name (x == y) does NOT flag non_fk_join
    issues_f = validate_sql(
        "SELECT c.amount FROM dm_mis.v_f_contract c "
        "JOIN dm_mis.d_party p ON c.branch_code = p.branch_code", SCHEMA)
    check("(f) same-name join (x==y) does NOT flag non_fk_join",
          not any(i["check"] == "non_fk_join" for i in issues_f))

    # Bonus: malformed SQL never raises and returns [].
    check("(g) malformed SQL returns [] (no raise)",
          validate_sql("SELECT FROM WHERE )(", SCHEMA) == [])

    # (h) as-of-D query (report_date pinned with '=') but the row-effectivity
    #     window date_from/date_to is NOT constrained -> flags the new check.
    issues_h = validate_sql(
        "SELECT l.role_code, COUNT(*) FROM dm_mis.link_acct l "
        "WHERE l.report_date = DATE '2025-11-01' GROUP BY l.role_code", SCHEMA)
    check("(h) as-of snapshot but unconstrained window flags row_effectivity",
          any(i["check"] == "row_effectivity_window_unconstrained"
              and i["table"] == "dm_mis.link_acct" for i in issues_h))

    # (i) window IS constrained (begin<=D AND end>D) -> NO finding.
    issues_i = validate_sql(
        "SELECT l.role_code FROM dm_mis.link_acct l "
        "WHERE l.report_date = DATE '2025-11-01' "
        "AND l.date_from <= DATE '2025-11-01' AND l.date_to > DATE '2025-11-01'", SCHEMA)
    check("(i) constrained window yields no row_effectivity finding",
          not any(i["check"] == "row_effectivity_window_unconstrained" for i in issues_i))

    # (j) NO as-of anchor (no snapshot '=') -> stay silent even if window absent.
    issues_j = validate_sql(
        "SELECT l.role_code FROM dm_mis.link_acct l "
        "WHERE l.report_date > DATE '2025-01-01'", SCHEMA)
    check("(j) no as-of '=' anchor -> no row_effectivity finding",
          not any(i["check"] == "row_effectivity_window_unconstrained" for i in issues_j))

    # (k) window constrained inside JOIN ON (not WHERE) -> NO finding.
    issues_k = validate_sql(
        "SELECT p.party_name FROM dm_mis.d_party p "
        "JOIN dm_mis.link_acct l ON p.party_id = l.agreement_id "
        "AND l.report_date = DATE '2025-11-01' "
        "AND l.date_from <= DATE '2025-11-01' AND l.date_to > DATE '2025-11-01'", SCHEMA)
    check("(k) window constrained in JOIN ON yields no finding",
          not any(i["check"] == "row_effectivity_window_unconstrained" for i in issues_k))

    # ---- evidence grounding checks ----
    ev_l = check_evidence_grounding(
        "SELECT * FROM dm_mis.v_f_contract c WHERE c.amount > 0", [], "postgres")
    check("(l) unjustified WHERE filter flags ungrounded_filter",
          any(i["check"] == "ungrounded_filter" and i["column"] == "amount"
              for i in ev_l))

    ev_m = check_evidence_grounding(
        "SELECT * FROM dm_mis.v_f_contract c WHERE c.amount > 0",
        [{"table": "v_f_contract", "column": "amount", "role": "filter",
          "reason": "positive amounts only"}], "postgres")
    check("(m) justified WHERE filter yields no ungrounded_filter",
          not any(i["check"] == "ungrounded_filter" for i in ev_m))

    ev_n = check_evidence_grounding(
        "SELECT SUM(c.amount) FROM dm_mis.v_f_contract c", [], "postgres")
    check("(n) unjustified SUM metric flags ungrounded_metric",
          any(i["check"] == "ungrounded_metric" and i["column"] == "amount"
              for i in ev_n))

    ev_o = check_evidence_grounding(
        "SELECT COUNT(*) FROM dm_mis.v_f_contract c WHERE c.amount > 0",
        [{"column": "amount", "role": "filter", "reason": "x"}], "postgres")
    check("(o) COUNT(*) does not require metric evidence",
          not any(i["check"] == "ungrounded_metric" for i in ev_o))

    ev_p = check_evidence_grounding(
        "SELECT p.party_name FROM dm_mis.v_f_contract c "
        "JOIN dm_mis.d_party p ON c.contract_id = p.party_id",
        [{"column": "party_name", "role": "select", "reason": "x"}],
        "postgres", SCHEMA)
    check("(p) declared-FK join key is exempt from ungrounded_join",
          not any(i["check"] == "ungrounded_join" for i in ev_p))

    check("(q) evidence_repair_hint non-empty for hard issues, empty for none",
          bool(evidence_repair_hint(ev_l)) and not evidence_repair_hint([]))

    print()
    if all(results):
        print(f"ALL {len(results)} ASSERTIONS PASSED")
        sys.exit(0)
    else:
        failed = sum(1 for r in results if not r)
        print(f"{failed} ASSERTION(S) FAILED")
        sys.exit(1)
