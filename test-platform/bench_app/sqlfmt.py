"""Small SQL pretty-printer used for API/display output.

It is intentionally dependency-free: the running environment does not currently
ship sqlparse, and benchmark execution must not depend on another package just
to make downloaded/displayed SQL readable.
"""
from __future__ import annotations

import re
from collections.abc import Iterable


_MAJOR = re.compile(
    r"\b(WITH|SELECT|FROM|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|OFFSET|"
    r"UNION(?:\s+ALL)?|EXCEPT|INTERSECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|"
    r"CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE|RETURNING|VALUES|SET)\b",
    re.IGNORECASE,
)
_JOINS = re.compile(r"\b((?:LEFT|RIGHT|FULL|INNER|CROSS)\s+(?:OUTER\s+)?JOIN|JOIN)\b", re.IGNORECASE)
_MINOR = re.compile(r"\b(AND|OR|ON|WHEN|ELSE)\b", re.IGNORECASE)
_KEYWORDS = re.compile(
    r"\b(select|from|where|group\s+by|order\s+by|having|limit|offset|"
    r"union(?:\s+all)?|except|intersect|with|join|left\s+(?:outer\s+)?join|"
    r"right\s+(?:outer\s+)?join|full\s+(?:outer\s+)?join|inner\s+join|"
    r"cross\s+join|on|and|or|as|case|when|then|else|end|insert\s+into|"
    r"update|delete\s+from|create\s+table|alter\s+table|drop\s+table|"
    r"values|returning|set|distinct|over|partition\s+by)\b",
    re.IGNORECASE,
)
_SQL_START = re.compile(r"^\s*(WITH|SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP)\b", re.IGNORECASE)


def _split_quoted(sql: str):
    """Yield (text, is_quoted) segments, preserving SQL string/identifier text."""
    buf: list[str] = []
    quote = None
    dollar = None
    i = 0

    def flush(is_quoted: bool):
        nonlocal buf
        if buf:
            yield "".join(buf), is_quoted
            buf = []

    while i < len(sql):
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < len(sql) else ""
        if dollar:
            if sql.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                yield from flush(True)
                dollar = None
                continue
            buf.append(ch)
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                if nxt == quote:
                    buf.append(nxt)
                    i += 2
                    continue
                yield from flush(True)
                quote = None
            i += 1
            continue
        m = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[i:])
        if m:
            yield from flush(False)
            dollar = m.group(0)
            buf.append(dollar)
            i += len(dollar)
            continue
        if ch in ("'", '"', "`"):
            yield from flush(False)
            quote = ch
            buf.append(ch)
            i += 1
            continue
        buf.append(ch)
        i += 1
    yield from flush(bool(quote or dollar))


def _transform_unquoted(sql: str, fn):
    return "".join(text if quoted else fn(text) for text, quoted in _split_quoted(sql))


def _normalise_unquoted(text: str) -> str:
    return (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\t", " ")
    )


def _split_top_level_commas(text: str) -> list[str]:
    parts, buf = [], []
    quote = None
    dollar = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if dollar:
            if text.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
            buf.append(ch)
            i += 1
            continue
        if quote:
            buf.append(ch)
            if ch == quote:
                if nxt == quote:
                    buf.append(nxt)
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        m = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", text[i:])
        if m:
            dollar = m.group(0)
            buf.append(dollar)
            i += len(dollar)
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _format_select_line(line: str) -> list[str]:
    m = re.match(r"^(SELECT(?:\s+DISTINCT)?)\s+(.+)$", line, re.IGNORECASE)
    if not m:
        return [line]
    head, body = m.group(1).upper(), m.group(2).strip()
    parts = _split_top_level_commas(body)
    if len(parts) <= 1:
        return [f"{head} {body}"]
    lines = [head]
    for idx, part in enumerate(parts):
        suffix = "," if idx < len(parts) - 1 else ""
        lines.append(f"  {part}{suffix}")
    return lines


def format_sql(sql: str | None) -> str | None:
    """Return readable SQL without changing semantics; None stays None."""
    if sql is None:
        return None
    raw = str(sql).strip()
    if not raw or not _SQL_START.search(raw):
        return raw

    def pass1(text: str) -> str:
        text = _normalise_unquoted(text)
        text = re.sub(r"\s+", " ", text)
        text = re.sub(r"\s*,\s*", ", ", text)
        text = re.sub(r"\s*;\s*", ";", text)
        text = _KEYWORDS.sub(lambda m: m.group(0).upper().replace("  ", " "), text)
        text = _MAJOR.sub(r"\n\1", text)
        text = _JOINS.sub(r"\n\1", text)
        text = _MINOR.sub(r"\n  \1", text)
        text = re.sub(r"\s+\)", ")", text)
        text = re.sub(r"\(\s+", "(", text)
        return text

    formatted = _transform_unquoted(raw, pass1)
    formatted = re.sub(r"^\s*\n", "", formatted)
    formatted = re.sub(r"\n\s*\n+", "\n", formatted).strip()
    lines = [line.strip() for line in formatted.splitlines() if line.strip()]

    out: list[str] = []
    for line in lines:
        if re.match(r"^(SELECT|SELECT DISTINCT)\b", line, re.IGNORECASE):
            out.extend(_format_select_line(line))
        elif re.match(r"^(AND|OR|ON|WHEN|ELSE)\b", line, re.IGNORECASE):
            out.append("  " + line)
        else:
            out.append(line)
    return "\n".join(out)


def format_sql_fields(row: dict, fields: Iterable[str] = ("gold_sql", "predicted_sql", "gt_sql", "agent_sql")) -> dict:
    """Return a shallow copy with known SQL fields pretty-printed."""
    out = dict(row)
    for field in fields:
        if field in out:
            out[field] = format_sql(out.get(field))
    return out


def format_case_sql(case: dict) -> dict:
    return format_sql_fields(case, ("gold_sql", "predicted_sql"))

