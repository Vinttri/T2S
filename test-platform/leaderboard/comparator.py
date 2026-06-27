from __future__ import annotations

from collections import Counter

from .db import SelectResult


def _normalize_rows(rows):
    return [tuple("" if v is None else str(v) for v in row) for row in rows]


def _as_multiset(rows) -> Counter:
    return Counter(_normalize_rows(rows))


def execution_match(predicted: SelectResult, gold: SelectResult, *, ordered: bool):
    if not predicted.ok:
        return False, f"predicted query failed: {predicted.error}"
    if not gold.ok:
        return False, f"gold query failed: {gold.error}"
    if ordered:
        if _normalize_rows(predicted.rows) == _normalize_rows(gold.rows):
            return True, "ordered rows match"
        return False, "ordered rows differ"
    if _as_multiset(predicted.rows) == _as_multiset(gold.rows):
        return True, "row multiset match"
    return False, "row multiset differ"


def eval_level(*, predicted_sql, predicted, gold_sql, gold, ordered):
    if not predicted_sql:
        return 0, "no predicted SQL"
    if predicted is None:
        return 1, "predicted SQL not executed"
    if not predicted.ok:
        return 1, f"predicted execution failed: {predicted.error}"
    if not gold.ok:
        return 2, f"gold execution failed: {gold.error}"
    matched, reason = execution_match(predicted, gold, ordered=ordered)
    if matched:
        return 4, reason
    return 3, reason
