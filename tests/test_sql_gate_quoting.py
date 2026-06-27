"""Regression: ANSI double-quoted identifiers must become backticks on Impala.

In Impala/Hive a double quote is a STRING literal, so ORDER BY "alias" silently
sorts by a constant. normalize_identifier_quoting fixes this deterministically.
"""
import pytest

from api.sql_utils.sql_gate import normalize_identifier_quoting

Q = '"'


def test_double_quoted_alias_becomes_backtick_on_impala():
    sql = (
        f'SELECT c AS {Q}кол{Q} FROM t GROUP BY c '
        f'ORDER BY {Q}кол{Q} DESC LIMIT 5'
    )
    out, modified = normalize_identifier_quoting(sql, "impala")
    assert modified
    assert Q not in out
    assert "`кол`" in out and "ORDER BY `кол`" in out


def test_string_literals_preserved():
    sql = "SELECT a FROM t WHERE name = 'John' AND x != ''"
    out, modified = normalize_identifier_quoting(sql, "impala")
    assert not modified
    assert "'John'" in out


def test_postgres_double_quote_is_identifier_left_untouched():
    sql = f'SELECT c AS {Q}кол{Q} FROM t ORDER BY {Q}кол{Q}'
    _out, modified = normalize_identifier_quoting(sql, "postgres")
    assert not modified


def test_no_double_quote_is_noop():
    sql = "SELECT c AS k FROM t ORDER BY k DESC"
    out, modified = normalize_identifier_quoting(sql, "impala")
    assert not modified and out == sql


def test_bare_current_date_gets_parens_on_impala():
    from api.sql_utils.sql_gate import normalize_current_date_function
    out, mod = normalize_current_date_function(
        "SELECT c FROM t WHERE d <= CURRENT_DATE", "impala")
    assert mod and "CURRENT_DATE()" in out and "<= CURRENT_DATE()" in out


def test_current_date_already_parenthesised_untouched():
    from api.sql_utils.sql_gate import normalize_current_date_function
    out, mod = normalize_current_date_function(
        "SELECT c FROM t WHERE d <= CURRENT_DATE()", "impala")
    assert not mod


def test_qualified_current_date_column_untouched():
    from api.sql_utils.sql_gate import normalize_current_date_function
    out, mod = normalize_current_date_function(
        "SELECT t.current_date FROM t", "impala")
    assert not mod


def test_current_date_not_touched_on_postgres():
    from api.sql_utils.sql_gate import normalize_current_date_function
    _out, mod = normalize_current_date_function(
        "SELECT c FROM t WHERE d <= CURRENT_DATE", "postgres")
    assert not mod
