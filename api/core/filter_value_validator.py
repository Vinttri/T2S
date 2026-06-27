"""Deterministic filter-value validator + one compact repair (codex #3).

Detects when generated SQL filters a literal value on the WRONG column while a
domain-matching, FK-reachable column actually holds it — e.g. it filters a race
event_name for a COUNTRY literal while ``circuits.location.country`` holds that
value — and fires ONE compact repair so the join/filter is corrected.

Detection is deterministic: a country gazetteer recovers the literal from the
question (any language/declension), sqlglot reads the SQL's tables, and the
GRAPH (source of truth — independent of whatever narrowed table set the
generator kept) is queried for the column whose grounded description lists that
value, plus FK reachability from a table already in the query. The repair is a
single LLM call, fired ONLY on a detected mismatch — clean queries pay nothing.
General: nothing DB-specific is hardcoded; the value's home column is discovered
from the data-grounded descriptions in the graph.
"""
from __future__ import annotations

import asyncio
import logging
import re

try:
    import sqlglot
    from sqlglot import exp
except Exception:  # pylint: disable=broad-exception-caught
    sqlglot = None
    exp = None


def _json_path_for_value(description: str, value: str):
    m = re.search(r"JSON fields:\s*(.+)$", description or "", re.S)
    if not m:
        return None
    for part in m.group(1).split(";"):
        pm = re.match(r"\s*([\w.]+)\s*\((.+?)\)", part.strip(), re.S)
        if not pm:
            continue
        path, vals = pm.group(1), pm.group(2)
        if value.lower() in [v.strip().lower() for v in vals.split(",")]:
            return path
    return None


async def _reachable_from_used(graph, cand: str, used: set) -> bool:
    """True if ``cand`` shares a direct FK (either direction) with a used table."""
    if not used:
        return False
    q = ("MATCH (a:Table)<-[:BELONGS_TO]-(:Column)-[:REFERENCES]-(:Column)-[:BELONGS_TO]->(b:Table) "
         "WHERE toLower(a.name) = toLower($cand) AND toLower(b.name) IN $used "
         "RETURN count(*) > 0")
    try:
        rows = (await graph.query(q, {"cand": cand, "used": list(used)})).result_set or []
        return bool(rows and rows[0][0])
    except Exception:  # pylint: disable=broad-exception-caught
        return False


async def detect_filter_value_mismatch(sql, question, graph, db_type):
    """Return ``{value, table, column, path}`` when a country literal is filtered
    on the wrong column while a reachable country-domain column holds it; else
    None. Deterministic; queries the graph for the value's home column."""
    if not sqlglot or not sql or graph is None:
        return None
    try:
        from api.core.gazetteer import extract_country_literals  # pylint: disable=import-outside-toplevel
        from api.sql_utils.sql_gate import sqlglot_dialect  # pylint: disable=import-outside-toplevel
        literals = extract_country_literals(question)
        if not literals:
            return None
        expr = sqlglot.parse_one(sql, read=sqlglot_dialect(db_type))
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    used = {(t.name or "").lower() for t in expr.find_all(exp.Table)}
    if not used:
        return None
    for value in literals:
        try:
            rows = (await graph.query(
                "MATCH (t:Table)<-[:BELONGS_TO]-(c:Column) "
                "WHERE toLower(c.description) CONTAINS toLower($v) "
                "RETURN t.name, c.name, c.description LIMIT 12",
                {"v": value})).result_set or []
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        if not rows:
            continue
        # already filtering a table that holds this value -> assume SQL is fine
        if any(str(r[0]).lower() in used for r in rows):
            continue
        for r in rows:
            tname, cname, desc = str(r[0]), str(r[1]), str(r[2] or "")
            if await _reachable_from_used(graph, tname, used):
                return {"value": value, "table": tname, "column": cname,
                        "path": _json_path_for_value(desc, value)}
    return None


async def _fetch_schema_text(graph, names) -> str:
    try:
        rows = (await graph.query(
            "MATCH (t:Table)<-[:BELONGS_TO]-(c:Column) "
            "WHERE toLower(t.name) IN $names "
            "RETURN t.name, t.foreign_keys, "
            "collect(c.name + ' [' + coalesce(substring(c.description,0,90),'') + ']')",
            {"names": [n.lower() for n in names]})).result_set or []
    except Exception:  # pylint: disable=broad-exception-caught
        return ""
    lines = []
    for r in rows:
        cols = ", ".join((r[2] or [])[:24])
        fk = str(r[1] or "")
        lines.append(f"TABLE {r[0]} ({cols})" + (f" FK: {fk}" if fk and fk != "[]" else ""))
    return "\n".join(lines)


async def _repair(sql, question, hint, graph, used):
    from api.config import Config  # pylint: disable=import-outside-toplevel
    from litellm import completion  # pylint: disable=import-outside-toplevel
    col, path = hint["column"], hint.get("path")
    if path:
        parts = path.split(".")
        expr_s = col
        for k in parts[:-1]:
            expr_s += f"->'{k}'"
        expr_s += f"->>'{parts[-1]}'"
    else:
        expr_s = col
    target = f'{hint["table"]}.{expr_s}'
    schema_txt = await _fetch_schema_text(graph, list(used) + [hint["table"]])
    directive = (
        f"The literal '{hint['value']}' is NOT in the column the current query "
        f"filters; it is a value of {target}. Rewrite the query to filter "
        f"{target} = '{hint['value']}', joining {hint['table']} to the existing "
        f"tables via their foreign-key relationship. Keep the rest of the "
        f"question's intent (aggregation, grouping, other filters)."
    )
    prompt = (
        "You correct a single mis-targeted filter in a SQL query.\n\n"
        f"Schema:\n{schema_txt}\n\n"
        f"Question: {question}\n\nCurrent SQL:\n{sql}\n\n"
        f"Problem: {directive}\n\n"
        "Return ONLY the corrected SQL — no markdown, no commentary."
    )
    try:
        resp = await asyncio.to_thread(
            completion,
            **Config.completion_kwargs(
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=700,
                extra_body=Config.reasoning_extra_body(
                    getattr(Config, "COMPLETION_REASONING", None)),
            ))
        txt = (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("filter-value repair call failed: %s", str(exc)[:160])
        return None
    txt = re.sub(r"```(?:sql)?", "", txt).replace("```", "").strip()
    m = re.search(r"(?is)\b(SELECT|WITH)\b.*", txt)
    new_sql = (m.group(0) if m else txt).strip().rstrip(";").strip()
    return new_sql or None


async def validate_and_repair_filter_values(sql, question, graph, db_type):
    """Return ``(sql, repaired)``. Repairs only on a detected mismatch; otherwise
    returns the input untouched. Never raises."""
    try:
        hint = await detect_filter_value_mismatch(sql, question, graph, db_type)
    except Exception:  # pylint: disable=broad-exception-caught
        return sql, False
    if not hint:
        return sql, False
    used = set()
    try:
        from api.sql_utils.sql_gate import sqlglot_dialect  # pylint: disable=import-outside-toplevel
        expr = sqlglot.parse_one(sql, read=sqlglot_dialect(db_type))
        used = {(t.name or "").lower() for t in expr.find_all(exp.Table)}
    except Exception:  # pylint: disable=broad-exception-caught
        pass
    logging.info(
        "filter-value mismatch: value=%s -> should filter %s.%s (path=%s)",
        hint["value"], hint["table"], hint["column"], hint.get("path"))
    new_sql = await _repair(sql, question, hint, graph, used)
    if new_sql and new_sql.strip().lower() != (sql or "").strip().lower():
        logging.info("filter-value repair applied")
        return new_sql, True
    return sql, False
