"""Dedicated QueryWeaver (Dify workflow) adapter.

Direct blocking calls 504 (nginx cuts at 60s; the Dify workflow runs minutes), so
we call it in STREAMING mode. Key point vs the old generic middleware: we RETURN
AS SOON AS the final answer is in — break on `message_end` — instead of draining
the whole SSE stream until the upstream closes it. The stream can linger open
after the answer, which made cases hang and trip the per-case timeout even though
the SQL had already arrived.

SQL is pulled from the accumulated `message` answer (```sql block), with a fallback
to any `workflow_finished`/node output that looks like SQL.
"""
from __future__ import annotations

import json
import re

import httpx

from bench_app.http_client import httpx_verify
from leaderboard.redaction import redact_text, safe_exception


def _structured_sql(text: str) -> str | None:
    """The Dify workflow now appends an explicit final SQL as `sql: {"query": "..."}`.
    Prefer it — it's the итоговый verified query, cleaner than scraping markdown."""
    m = re.search(r'\bsql\s*:\s*(\{[^{}]*?"query"[^{}]*\})', text, re.IGNORECASE | re.DOTALL)
    if m:
        try:
            q = json.loads(m.group(1)).get("query")
        except Exception:
            q = None
        if q and q.strip():
            return q.strip()
    return None


def _sql_from_text(text: str) -> str | None:
    """The FINAL SQL in `text`: prefer the explicit `sql: {"query": ...}` trailer the
    workflow emits, else the LAST complete ```sql``` fenced block (if a draft was
    streamed before the final answer, the last one is the итоговый), then the last
    bare ```…SELECT…``` block, then a raw leading SELECT/WITH. Returns a complete
    block only — a half-streamed (unclosed) fence won't match, so the early-exit
    fires exactly when the SQL is fully present."""
    if not text:
        return None
    structured = _structured_sql(text)
    if structured:
        return structured
    blocks = re.findall(r"```sql\s*(.*?)```", text, re.DOTALL | re.IGNORECASE) \
        or re.findall(r"```\s*((?:SELECT|WITH)\b.*?)```", text, re.DOTALL | re.IGNORECASE)
    if blocks:
        return blocks[-1].strip()
    s = text.strip()
    return s if re.match(r"^\s*(SELECT|WITH)\b", s, re.IGNORECASE) else None


def _scan_outputs(data: dict, prev):
    outs = data.get("outputs") or {}
    for v in (outs.values() if isinstance(outs, dict) else []):
        if isinstance(v, str) and "psycopg2" not in v:
            cand = _sql_from_text(v)
            if cand:
                prev = cand
    return prev


async def query_weaver_sql_ctx(url: str, api_key: str, question: str, database: str,
                               timeout: float = 600.0):
    """Returns (sql | None, error | None, ctx: dict).

    `ctx` keeps the MAXIMUM error context for a failed/odd case so a run can be
    debugged after the fact: the full natural-language `answer`, an event-type
    histogram, every `error` event, every node that did NOT succeed (with its
    error), the workflow_finished outputs, and the HTTP status. The short `error`
    string is also enriched (answer snippet + the first failed node)."""
    body = {"inputs": {"database": database or "dm_mis", "sql_dialect": "postgres"},
            "query": question, "response_mode": "streaming", "user": "bench"}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    answer, node_sql, msg_sql = "", None, None
    ctx = {"http_status": None, "events": {}, "error_events": [], "failed_nodes": [],
           "workflow_outputs": None, "answer": "", "early_exit": None}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0),
                                     verify=httpx_verify("QW_SSL_VERIFY", "CONNECTOR_SSL_VERIFY")) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                ctx["http_status"] = resp.status_code
                if resp.status_code != 200:
                    txt = redact_text((await resp.aread())[:1000].decode("utf-8", "replace"))
                    ctx["body"] = txt
                    return None, f"HTTP {resp.status_code}: {txt[:200]}", ctx
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        ev = json.loads(line[5:].strip())
                    except Exception:
                        continue
                    et = ev.get("event")
                    data = ev.get("data") or {}
                    ctx["events"][et] = ctx["events"].get(et, 0) + 1
                    if et == "message":
                        answer += ev.get("answer", "")
                        cand = _sql_from_text(answer)   # complete ```sql block already in?
                        if cand:
                            msg_sql = cand
                            ctx["early_exit"] = "sql in message"
                            break                       # grab it and bail — skip the tail
                    elif et == "error":
                        ctx["error_events"].append({k: ev.get(k) for k in ("message", "code", "status") if ev.get(k)} or ev)
                        ctx["answer"] = answer
                        return None, ("error event: " + redact_text(str(ev.get("message") or ev))[:300]), ctx
                    elif et == "workflow_finished":
                        node_sql = _scan_outputs(data, node_sql)
                        ctx["workflow_outputs"] = data.get("outputs")
                        if data.get("error"):
                            ctx["error_events"].append({"workflow_error": data.get("error")})
                    elif et == "node_finished":
                        node_sql = _scan_outputs(data, node_sql)
                        if (data.get("status") or "succeeded") != "succeeded":
                            ctx["failed_nodes"].append({
                                "node": data.get("title") or data.get("node_type"),
                                "status": data.get("status"),
                                "error": (redact_text(str(data.get("error")))[:400] if data.get("error") else None)})
                    elif et == "message_end":
                        break   # answer complete — stop here, don't drain the stream tail
    except Exception as exc:  # noqa: BLE001
        ctx["answer"] = answer
        return None, safe_exception(exc, extra_secrets=[api_key, url], limit=200), ctx
    ctx["answer"] = answer
    sql = (msg_sql or _sql_from_text(answer) or node_sql or "").strip() or None
    if sql:
        return sql, None, ctx
    # enrich the short error with the most useful context we have
    bits = ["no SQL in stream"]
    fn = next((n for n in ctx["failed_nodes"] if n.get("error")), None)
    if fn:
        bits.append(f"failed node «{fn['node']}»: {redact_text(fn['error'])[:160]}")
    if answer.strip():
        bits.append("answer: " + redact_text(answer.strip().replace("\n", " "))[:200])
    return None, " | ".join(bits), ctx


async def query_weaver_sql(url: str, api_key: str, question: str, database: str,
                           timeout: float = 600.0):
    """Back-compat 2-tuple wrapper (sql, error) — used by the middleware endpoint."""
    sql, err, _ctx = await query_weaver_sql_ctx(url, api_key, question, database, timeout=timeout)
    return sql, err


# ---- NATIVE QueryWeaver (FalkorDB) — the real product, not the Dify wrapper ----
QWN_BASE = "http://queryweaver.144.91.85.207.nip.io:8080"
QWN_BOUNDARY = "|||FALKORDB_MESSAGE_BOUNDARY|||"


async def queryweaver_native_sql_ctx(question: str, database: str = "dm_mis",
                                     base: str = QWN_BASE, timeout: float = 300.0):
    """Real QueryWeaver: GET /auth-status (dev-auth, sets csrf_token cookie) →
    POST /graphs/{db} with X-CSRF-Token header + body {"chat":[question]}. The
    response is a stream of JSON messages split by FALKORDB_MESSAGE_BOUNDARY; the
    SQL is the `data` of the `sql_query` message. Returns (sql|None, err|None, ctx)."""
    ctx = {"http_status": None, "msg_types": [], "raw": None}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0),
                                     verify=httpx_verify("QW_SSL_VERIFY", "CONNECTOR_SSL_VERIFY")) as client:
            await client.get(f"{base}/auth-status")          # dev-auth + csrf cookie
            csrf = client.cookies.get("csrf_token")
            if not csrf:
                return None, "no csrf_token cookie", ctx
            resp = await client.post(f"{base}/graphs/{database}",
                                     headers={"X-CSRF-Token": csrf, "Content-Type": "application/json"},
                                     json={"chat": [question]})
            ctx["http_status"] = resp.status_code
            text = resp.text
            ctx["raw"] = redact_text(text[:8000])
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}: {redact_text(text)[:200]}", ctx
            sql = None
            for part in text.split(QWN_BOUNDARY):
                part = part.strip()
                if not part:
                    continue
                try:
                    msg = json.loads(part)
                except Exception:
                    continue
                ctx["msg_types"].append(msg.get("type"))
                if msg.get("type") == "sql_query" and msg.get("data"):
                    sql = str(msg["data"]).strip()
                elif msg.get("type") == "error":
                    return None, "error: " + redact_text(str(msg.get("message") or msg.get("data") or msg))[:200], ctx
            return (sql or None), (None if sql else "no sql_query in response"), ctx
    except Exception as exc:  # noqa: BLE001
        return None, safe_exception(exc, extra_secrets=[base], limit=200), ctx
