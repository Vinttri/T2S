"""User-defined templated connector.

Connector transport is intentionally simple: one plain HTTP(S) request, one
HTTP response, then SQL extraction from that response. WebSocket, SSE, and
streaming connector contracts are rejected; wrap those upstreams outside the app
behind a synchronous HTTP endpoint if needed.
"""
from __future__ import annotations

import json
import re
import shlex
from typing import Any
from urllib.parse import urlsplit

import httpx

from leaderboard.redaction import redact_obj, redact_text, safe_exception

# database is NOT a runtime placeholder anymore — each connector is bound to one DB
# and its db_id is baked into the body as a literal at save time (see _bake_db).
PLACEHOLDER_RE = re.compile(r"\{\{\s*(question|dialect)\s*\}\}")
PLAIN_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH"}
EVENT_STREAM_MIME = "text/event-stream"


def validate_plain_http_connector(c: dict, *, rendered_url: str | None = None):
    """Validate the connector transport contract: one plain HTTP request/response."""
    method = (c.get("method") or "POST").upper()
    if method not in PLAIN_HTTP_METHODS:
        raise ValueError("Коннекторы поддерживают только обычные HTTP(S) методы GET/POST/PUT/PATCH.")
    url = rendered_url or c.get("url") or ""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Коннектор должен быть обычным HTTP(S) endpoint: http://... или https://...")
    headers = {str(k).lower(): str(v).lower() for k, v in (c.get("headers") or {}).items()}
    if EVENT_STREAM_MIME in headers.get("accept", ""):
        raise ValueError("SSE/text-event-stream для коннекторов не поддерживается: нужен один HTTP request/response.")


def _fill(text: str, vals: dict, *, json_escape: bool) -> str:
    if not text:
        return text
    def repl(m):
        val = vals.get(m.group(1), "")
        if json_escape:
            return json.dumps(val, ensure_ascii=False)[1:-1]  # escaped, no surrounding quotes
        return val
    return PLACEHOLDER_RE.sub(repl, text)


def _dig(obj: Any, path: str):
    """Navigate a dotted path into nested dict/list (e.g. 'data.0.sql')."""
    cur = obj
    for part in (path or "").split("."):
        if part == "":
            continue
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _deep_collect(obj: Any, path: str) -> list:
    """Find every value reachable via dotted `path` from ANY dict node in the tree
    (handles SQL buried in a variable-index chunk list, e.g. Vanna's
    chunks[].rich.data.metadata.sql). Returns matches in document order."""
    found = []
    def walk(o):
        if isinstance(o, dict):
            v = _dig(o, path)
            if isinstance(v, str) and v.strip():
                found.append(v.strip())
            for vv in o.values():
                walk(vv)
        elif isinstance(o, list):
            for vv in o:
                walk(vv)
    walk(obj)
    return found


def extract_sql(payload: Any, raw_text: str, spec: dict) -> str | None:
    """spec = {field?: dotted path into JSON, mode: raw|sql_block|regex|json,
    pattern?: regex with group 1, deep?: search the path anywhere in the tree
    and take the LAST match}. Returns SQL string or None."""
    mode = (spec or {}).get("mode", "sql_block")
    field = (spec or {}).get("field")
    if field and (spec or {}).get("deep"):
        hits = _deep_collect(payload if isinstance(payload, (dict, list)) else {}, field)
        return hits[-1] if hits else None
    # pick source text
    if field:
        src = _dig(payload if isinstance(payload, (dict, list)) else {}, field)
        src = "" if src is None else (src if isinstance(src, str) else json.dumps(src, ensure_ascii=False))
    else:
        src = raw_text or ""
    if mode == "raw" or mode == "json":
        return src.strip() or None
    if mode == "regex":
        m = re.search((spec or {}).get("pattern", r"```sql\s*(.*?)```"), src, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else None
    # default: fenced ```sql block, fallback to first ```...``` SELECT/WITH
    m = (re.search(r"```sql\s*(.*?)```", src, re.DOTALL | re.IGNORECASE)
         or re.search(r"```\s*((?:SELECT|WITH)\b.*?)```", src, re.DOTALL | re.IGNORECASE))
    if m:
        return m.group(1).strip()
    # last resort: the whole source if it looks like SQL
    s = src.strip()
    if re.match(r"^\s*(SELECT|WITH)\b", s, re.IGNORECASE):
        return s
    return None


class TemplatedConnector:
    def __init__(self, c: dict):
        self.c = c

    async def generate(self, client: httpx.AsyncClient, question: str, dialect: str, timeout: float, database: str = ""):
        c = self.c
        vals = {"question": question, "dialect": dialect, "database": database}
        url = _fill(c["url"], vals, json_escape=False)
        headers = {k: _fill(str(v), vals, json_escape=False) for k, v in (c.get("headers") or {}).items()}
        method = (c.get("method") or "POST").upper()
        try:
            validate_plain_http_connector({**c, "headers": headers, "method": method}, rendered_url=url)
        except ValueError as exc:
            return None, {}, redact_text(str(exc))
        body_str = _fill(c.get("body_template") or "", vals, json_escape=True)
        kwargs = {"headers": headers, "timeout": timeout}
        if method in ("POST", "PUT", "PATCH") and body_str.strip():
            headers.setdefault("Content-Type", "application/json")
            kwargs["content"] = body_str.encode("utf-8")
        try:
            resp = await client.request(method, url, **kwargs)
        except Exception as exc:
            return None, {}, safe_exception(exc, extra_secrets=[url], limit=200)
        raw = resp.text
        if EVENT_STREAM_MIME in (resp.headers.get("content-type") or "").lower():
            return None, {"status": resp.status_code, "body": redact_text(raw[:8000])}, (
                "SSE/text-event-stream response is not supported; use a synchronous HTTP endpoint "
                "that returns one response containing SQL."
            )
        if resp.status_code != 200:
            # keep the full raw body in the payload (for the "сырой ответ" view);
            # only the short error string is truncated.
            safe_raw = redact_text(raw)
            return None, {"status": resp.status_code, "body": safe_raw}, f"HTTP {resp.status_code}: {safe_raw[:300]}"
        try:
            payload = resp.json()
        except Exception:
            payload = None
        sql = extract_sql(payload, raw, c.get("sql_extract") or {})
        return sql, redact_obj(payload if payload is not None else {"text": raw}), (None if sql else "no SQL extracted from response")


def preview_request(c: dict, question: str, dialect: str, database: str = "") -> dict:
    """Render the request without sending — for the UI 'preview' button."""
    vals = {"question": question, "dialect": dialect, "database": database}
    url = _fill(c["url"], vals, json_escape=False)
    headers = {k: _fill(str(v), vals, json_escape=False) for k, v in (c.get("headers") or {}).items()}
    method = (c.get("method") or "POST").upper()
    validate_plain_http_connector({**c, "headers": headers, "method": method}, rendered_url=url)
    body = _fill(c.get("body_template") or "", vals, json_escape=True)
    return {
        "method": method,
        "url": redact_text(url),
        "headers": redact_obj(headers),
        "body": redact_text(body),
    }


def preview_to_curl(preview: dict) -> str:
    """Render a shell-safe curl command from a rendered request preview."""
    method = (preview.get("method") or "POST").upper()
    parts = ["curl", "-X", method, shlex.quote(preview.get("url") or "")]
    for key, value in (preview.get("headers") or {}).items():
        parts.extend(["-H", shlex.quote(f"{key}: {value}")])
    body = preview.get("body")
    if body:
        parts.extend(["--data-raw", shlex.quote(body)])
    if len(parts) <= 6:
        return " ".join(parts)
    lines = [" ".join(parts[:4])]
    for i in range(4, len(parts), 2):
        lines.append("  " + " ".join(parts[i:i + 2]))
    return " \\\n".join(lines)
