"""Unit tests for the deterministic sqlglot validation gate."""

import pytest

from api.sql_utils import sql_gate


def _tables():
    """Schema card in find()'s table_info format: [name, desc, fks, columns]."""
    return [
        [
            "dm_mis.v_f_account_rest",
            "Account balance snapshots",
            {},
            [
                {"columnName": "ACCOUNT_NBR", "dataType": "string"},
                {"columnName": "REPORT_DATE", "dataType": "date"},
                {"columnName": "BALANCE_OUT", "dataType": "decimal"},
                {"columnName": "CRCY_ISO_ALPHA_CD", "dataType": "string"},
            ],
        ],
        [
            "dm_mis.v_d_entity_org",
            "Organizations",
            {},
            [
                {"columnName": "ENTITY_CORE_ID", "dataType": "bigint"},
                {"columnName": "FULL_NAME", "dataType": "string"},
                {"columnName": "TAX_NUMBER", "dataType": "string"},
                {"columnName": "ENTITY_TYPE", "dataType": "int"},
            ],
        ],
    ]


class TestSchemaCard:
    def test_card_registers_full_and_short_names(self):
        card = sql_gate.build_schema_card(_tables())
        assert "dm_mis.v_f_account_rest" in card
        assert "v_f_account_rest" in card
        assert "balance_out" in card["v_f_account_rest"]


class TestValidSQL:
    def test_simple_select_passes(self):
        result = sql_gate.validate_sql(
            "SELECT balance_out FROM dm_mis.v_f_account_rest "
            "WHERE report_date = '2026-05-24' AND account_nbr = '42'",
            "impala",
            _tables(),
        )
        assert result.ok, result.report()

    def test_join_with_aliases_passes(self):
        result = sql_gate.validate_sql(
            "SELECT o.full_name, r.balance_out "
            "FROM dm_mis.v_f_account_rest r "
            "JOIN dm_mis.v_d_entity_org o ON o.entity_core_id = r.account_nbr",
            "impala",
            _tables(),
        )
        assert result.ok, result.report()

    def test_cte_and_window_passes(self):
        result = sql_gate.validate_sql(
            "WITH t AS (SELECT crcy_iso_alpha_cd AS code, "
            "SUM(balance_out) AS total FROM v_f_account_rest "
            "GROUP BY crcy_iso_alpha_cd) "
            "SELECT code, total, RANK() OVER (ORDER BY total DESC) AS rnk FROM t",
            "impala",
            _tables(),
        )
        assert result.ok, result.report()

    def test_postgres_cast_passes(self):
        result = sql_gate.validate_sql(
            "SELECT AVG(CAST(balance_out AS DECIMAL(20,2))) "
            "FROM v_f_account_rest",
            "postgresql",
            _tables(),
        )
        assert result.ok, result.report()


class TestHallucinations:
    def test_unknown_table_is_caught_with_suggestion(self):
        result = sql_gate.validate_sql(
            "SELECT balance_out FROM dm_mis.v_f_account_rests",
            "impala",
            _tables(),
        )
        assert not result.ok
        assert result.unknown_tables == ["dm_mis.v_f_account_rests"]
        assert "v_f_account_rest" in " ".join(
            result.suggestions["dm_mis.v_f_account_rests"]
        )

    def test_unknown_qualified_column_is_caught(self):
        result = sql_gate.validate_sql(
            "SELECT r.account_balance FROM dm_mis.v_f_account_rest r",
            "impala",
            _tables(),
        )
        assert not result.ok
        assert result.unknown_columns
        assert "balance_out" in " ".join(
            result.suggestions[result.unknown_columns[0]]
        )

    def test_unknown_bare_column_is_caught(self):
        result = sql_gate.validate_sql(
            "SELECT made_up_column FROM v_f_account_rest",
            "impala",
            _tables(),
        )
        assert not result.ok
        assert result.unknown_columns == ["made_up_column"]

    def test_output_alias_not_false_positive(self):
        result = sql_gate.validate_sql(
            "SELECT SUM(balance_out) AS total_balance "
            "FROM v_f_account_rest ORDER BY total_balance DESC",
            "impala",
            _tables(),
        )
        assert result.ok, result.report()


class TestReadOnly:
    @pytest.mark.parametrize(
        "sql",
        [
            "DELETE FROM v_f_account_rest",
            "INSERT INTO v_f_account_rest VALUES (1)",
            "UPDATE v_f_account_rest SET balance_out = 0",
            "DROP TABLE v_f_account_rest",
            "CREATE TABLE x AS SELECT 1",
        ],
    )
    def test_write_statements_blocked(self, sql):
        result = sql_gate.validate_sql(sql, "impala", _tables())
        assert not result.ok
        assert result.read_only_violation or result.parse_error

    def test_ast_read_only_helper(self):
        ok, _ = sql_gate.validate_read_only_ast(
            "SELECT 1 FROM v_f_account_rest", "impala",
        )
        assert ok
        bad, reason = sql_gate.validate_read_only_ast(
            "DROP TABLE v_f_account_rest", "impala",
        )
        assert not bad
        assert "DROP" in reason.upper()


class TestParseErrors:
    def test_garbage_sql_fails_parse(self):
        result = sql_gate.validate_sql(
            "SELEC balance FROMM table WHERE", "impala", _tables(),
        )
        assert not result.ok
        assert result.parse_error

    def test_empty_sql_fails(self):
        result = sql_gate.validate_sql("", "impala", _tables())
        assert not result.ok


class TestFullSchemaAllowlist:
    """Gate must accept a real table even when recall left it out of context."""

    def test_real_table_in_full_allowlist_passes(self):
        # full graph allowlist contains v_f_repo_acc_rest, the prompt did not
        full = [
            ["dm_mis.v_f_repo_acc_rest", "", {},
             [{"columnName": "leg1_deal_security_id"},
              {"columnName": "account_id"}, {"columnName": "balance_out"},
              {"columnName": "balance_date"}, {"columnName": "final_date"}]],
            ["dm_mis.v_f_repo_acc_asn", "", {},
             [{"columnName": "leg1_deal_security_id"},
              {"columnName": "account_id"}, {"columnName": "balance_date"}]],
        ]
        result = sql_gate.validate_sql(
            "SELECT r.balance_out FROM dm_mis.v_f_repo_acc_rest r "
            "JOIN dm_mis.v_f_repo_acc_asn a "
            "ON r.leg1_deal_security_id = a.leg1_deal_security_id",
            "impala", full,
        )
        assert result.ok, result.report()

    def test_hallucination_still_caught_against_full_allowlist(self):
        full = [["dm_mis.v_f_account_rest", "", {},
                 [{"columnName": "balance_out"}]]]
        result = sql_gate.validate_sql(
            "SELECT balance_out FROM dm_mis.starships", "impala", full,
        )
        assert not result.ok
        assert "starships" in " ".join(result.unknown_tables)


class TestNoSchemaContext:
    def test_no_card_means_no_identifier_check(self):
        result = sql_gate.validate_sql(
            "SELECT anything FROM anywhere", "impala", [],
        )
        assert result.ok


def _snap_tables():
    """Two snapshot tables + one plain dimension, with PK reporting dates."""
    def col(name, key="", desc="", ctype="string"):
        return {"columnName": name, "keyType": key, "description": desc,
                "dataType": ctype}
    return [
        ["dm_mis.v_f_repo_deal", "", {}, [
            col("balance_date", "PRI", "Дата отчета (PRIMARY KEY)", "date"),
            col("leg1_deal_security_id", "PRI", "ID первой ноги"),
            col("leg2_exec_dt", "", "Дата исполнения второй ноги", "date"),
            col("deal_security_nbr", "", "Номер сделки"),
        ]],
        ["dm_mis.v_d_repo_contract", "", {}, [
            col("balance_date", "PRI", "Дата отчета (PRIMARY KEY)", "date"),
            col("agreement_id", "PRI", "ID договора"),
            col("agreement_nbr", "", "Номер договора"),
            col("fact_close_dt", "", "Дата закрытия", "date"),
        ]],
        ["dm_mis.vfund", "", {}, [
            col("id", "PRI", "Идентификатор валюты"),
            col("code", "", "Код валюты"),
        ]],
    ]


class TestSnapshotGrainLint:
    def test_unpinned_snapshot_join_flagged(self):
        # H7 shape: snapshot tables joined only on key + snapshot equality
        issues = sql_gate.snapshot_grain_issues(
            "SELECT c.agreement_nbr, COUNT(d.leg1_deal_security_id) "
            "FROM dm_mis.v_f_repo_deal d "
            "JOIN dm_mis.v_d_repo_contract c ON d.agreement_id = c.agreement_id "
            "AND c.balance_date = d.balance_date "
            "WHERE d.leg2_exec_dt < CURRENT_DATE() "
            "GROUP BY c.agreement_nbr",
            "impala", _snap_tables(),
        )
        assert issues, "unpinned snapshot join must be flagged"

    def test_literal_pin_passes(self):
        issues = sql_gate.snapshot_grain_issues(
            "SELECT c.agreement_nbr FROM dm_mis.v_d_repo_contract c "
            "JOIN dm_mis.v_f_repo_deal d ON d.agreement_id = c.agreement_id "
            "AND c.balance_date = d.balance_date "
            "WHERE c.balance_date = DATE '2026-05-04'",
            "impala", _snap_tables(),
        )
        assert not issues, issues

    def test_transitive_pin_through_equality(self):
        issues = sql_gate.snapshot_grain_issues(
            "SELECT 1 FROM dm_mis.v_f_repo_deal d "
            "JOIN dm_mis.v_d_repo_contract c "
            "ON c.balance_date = d.balance_date "
            "WHERE d.balance_date >= DATE '2026-01-01'",
            "impala", _snap_tables(),
        )
        assert not issues, issues

    def test_max_subquery_pin_passes(self):
        issues = sql_gate.snapshot_grain_issues(
            "SELECT 1 FROM dm_mis.v_f_repo_deal d "
            "WHERE d.balance_date = "
            "(SELECT MAX(balance_date) FROM dm_mis.v_f_repo_deal)",
            "impala", _snap_tables(),
        )
        assert not issues, issues

    def test_window_scope_exempt(self):
        issues = sql_gate.snapshot_grain_issues(
            "WITH t AS (SELECT balance_date, "
            "LAG(deal_security_nbr) OVER (ORDER BY balance_date) p "
            "FROM dm_mis.v_f_repo_deal) "
            "SELECT * FROM t WHERE balance_date = DATE '2026-06-08'",
            "impala", _snap_tables(),
        )
        assert not issues, issues

    def test_function_over_snapshot_col_pins(self):
        issues = sql_gate.snapshot_grain_issues(
            "SELECT 1 FROM dm_mis.v_d_repo_contract c "
            "WHERE YEAR(c.balance_date) = 2025",
            "impala", _snap_tables(),
        )
        assert not issues, issues

    def test_non_snapshot_table_ignored(self):
        issues = sql_gate.snapshot_grain_issues(
            "SELECT code FROM dm_mis.vfund", "impala", _snap_tables(),
        )
        assert not issues


class TestReport:
    def test_report_mentions_all_violations(self):
        result = sql_gate.validate_sql(
            "SELECT r.no_such_col FROM dm_mis.no_such_table r",
            "impala",
            _tables(),
        )
        text = result.report()
        assert "no_such_table" in text
        assert "does not exist" in text
