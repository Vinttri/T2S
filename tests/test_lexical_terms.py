"""Regression tests for lexical term extraction and IDF table ranking.

Born from a real failure: the question «...идентификатор сделки (первая нога),
балансовую дату...» must surface dm_mis.v_f_repo_rate whose column description
says «ID первой ноги сделки». The old extractor sorted terms by length and
capped at 24, dropping the short high-signal words entirely.
"""

from api.graph import (
    _aggressive_cyrillic_stem,
    _combined_lexical_search_terms,
    _lexical_search_terms,
    _table_relevance_score,
    _token_idf_weights,
)

RATE_QUESTION = (
    "Найдите сделки, в которых произошло значительное изменение ставки "
    "финансирования (рост или падение более чем на 20%) в сравнении с "
    "предыдущим периодом на балансовую дату 2026-06-08. Мне необходимо "
    "получить 3 атрибута для каждой такой сделки: идентификатор сделки "
    "(первая нога), балансовую дату и рассчитанный процент изменения."
)


class TestLexicalTerms:
    def test_short_domain_words_survive(self):
        terms = _lexical_search_terms(RATE_QUESTION)
        assert any(term.startswith("став") for term in terms), terms
        assert any(term.startswith("сдел") for term in terms), terms
        assert "ног" in terms or "нога" in terms, terms

    def test_stems_bridge_russian_inflection(self):
        # «первая» in the question must match «первой» in a column description
        assert _aggressive_cyrillic_stem("первая") == "перва"[:5] or \
            _aggressive_cyrillic_stem("первая").startswith("перв")
        haystack = "id первой ноги сделки в core"
        terms = _lexical_search_terms("первая нога сделки")
        assert any(term in haystack for term in terms), terms

    def test_appearance_order_not_length_order(self):
        terms = _lexical_search_terms("нога ставки значительное необходимо")
        first_domain = min(
            index for index, term in enumerate(terms)
            if term.startswith(("ног", "став"))
        )
        generic = [
            index for index, term in enumerate(terms)
            if term.startswith(("значительн", "необходим"))
        ]
        if generic:
            assert first_domain < min(generic)


def _make_table(name, description, columns):
    return [
        name,
        description,
        {},
        [{"columnName": col, "description": desc} for col, desc in columns],
    ]


class TestSourceAmbiguity:
    """Key-only output columns must NOT trigger a source clarification.

    Real failure: «...идентификатор сделки (первая нога), балансовую дату...»
    projects only leg1_deal_security_id + balance_date — both join keys that
    every repo fact table carries — so the disambiguation prompt fired even
    though the model correctly picked v_f_repo_rate.
    """

    def test_key_only_outputs_skip_ambiguity(self):
        from api.agents.analysis_agent import _source_ambiguity_retry_context

        def repo_table(name, extra_cols):
            cols = [
                {"columnName": "leg1_deal_security_id", "key_type": "PK",
                 "description": "ID первой ноги сделки"},
                {"columnName": "balance_date", "key_type": "PK",
                 "description": "Дата отчета"},
            ] + [{"columnName": c, "description": d} for c, d in extra_cols]
            return [name, f"repo table {name}", {}, cols]

        pruned = [
            repo_table("dm_mis.v_f_repo_rate", [("rate", "Значение ставки")]),
            repo_table("dm_mis.v_f_repo_deal", [("type_repo", "Тип РЕПО")]),
            repo_table("dm_mis.v_f_repo_acc_asn",
                       [("account_core_role_nm", "Роль счета")]),
        ]
        analysis = {
            "is_sql_translatable": True,
            "sql_query": (
                "SELECT leg1_deal_security_id, balance_date "
                "FROM dm_mis.v_f_repo_rate WHERE balance_date = '2026-06-08'"
            ),
        }
        result = _source_ambiguity_retry_context(
            analysis, pruned, "ставка финансирования первая нога", False, "",
        )
        assert result is None, result


class TestIdfRanking:
    def test_rare_token_beats_many_generic_tokens(self):
        verbose = _make_table(
            "dm_mis.v_f_contract_rate_ref",
            "Ставки по кредитным договорам, процентная ставка, дата баланса, "
            "изменение ставки, маржа, базовая ставка, предыдущая ставка",
            [
                ("percent_rate", "Размер процентной ставки"),
                ("previos_percent_rate", "Предыдущая процентная ставка по договору"),
                ("report_date", "Дата баланса"),
                ("margin", "Маржа по ставке"),
            ],
        )
        terse = _make_table(
            "dm_mis.v_f_repo_rate",
            "Ставки сделок РЕПО",
            [
                ("rate", "Значение ставки"),
                ("balance_date", "Дата отчета"),
                ("leg1_deal_security_id", "ID первой ноги сделки в CORE"),
            ],
        )
        filler = [
            _make_table(
                f"dm_mis.filler_{index}",
                "Дата ставка договор изменение баланс процент",
                [("col", "дата ставка договор изменение")],
            )
            for index in range(8)
        ]
        pool = [verbose, terse, *filler]
        tokens = set(_combined_lexical_search_terms(RATE_QUESTION, []))
        weights = _token_idf_weights(pool, tokens)

        # «ноги»/«перв» live only in the terse table → high IDF weight
        terse_score = _table_relevance_score(terse, tokens, token_weights=weights)
        filler_score = max(
            _table_relevance_score(table, tokens, token_weights=weights)
            for table in filler
        )
        assert terse_score > filler_score, (terse_score, filler_score)
