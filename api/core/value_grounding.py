"""Data-grounded column metadata correction at INDEX time.

TWO corrections, both so the generator trusts the DATABASE, not prose:

1. TYPE FROM DB. Column descriptions (from the dataset's column_meaning) often
   begin with a TYPE word ("BOOLEAN.", "TEXT.", "BIGINT.") that can be WRONG or
   merely logical — a 0/1 ``bigint`` flag described as "BOOLEAN". The physical type
   already lives on the graph Column node (read from ``information_schema`` by the
   loader). So we STRIP the leading scalar type word from the description: the DB
   type is authoritative, the description only carries meaning / PK-FK-constraint
   hints. (JSONB structure descriptions are kept — they are metadata, not a type.)

2. VALUES FROM DB. For LOW-CARDINALITY columns we read the ACTUAL distinct values
   and rewrite the description's "Possible values:" to match, so value filters use
   real literals (e.g. ``CrossBorder = 1`` not ``= 'Yes'``).

Runs at load + re-index. Idempotent, bounded, dialect-aware, never raises.
"""
from __future__ import annotations

import asyncio
import logging
import re

_SKIP_NAME = re.compile(r"_id$|ref$|code$|key$|uuid|hash|link", re.I)
# Leading SCALAR type words to drop (type comes from the DB). JSON/JSONB kept.
_LEAD_TYPE = re.compile(
    r"^\s*(?:BOOLEAN|BOOL|TEXT|VARCHAR|CHARACTER VARYING|CHARACTER|CHAR|"
    r"BIGINT|INTEGER|INT|SMALLINT|TINYINT|REAL|DOUBLE PRECISION|DOUBLE|"
    r"NUMERIC|DECIMAL|FLOAT|MONEY|TIMESTAMP(?: WITH(?:OUT)? TIME ZONE)?|"
    r"TIMESTAMPTZ|DATETIME|DATE|TIME|UUID|BYTEA|SERIAL|BIGSERIAL)\b\.?\s*",
    re.I,
)


def _cast_text(db_type: str | None, col_q: str) -> str:
    dt = (db_type or "").lower()
    if "mysql" in dt or "maria" in dt:
        return f"CAST({col_q} AS CHAR)"
    if "impala" in dt or "hive" in dt or "spark" in dt:
        return f"CAST({col_q} AS STRING)"
    return f"{col_q}::text"  # postgres / snowflake


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s).strip().lower())


def _strip_leading_type(desc: str) -> str:
    """Drop a leading scalar type word so the DB type stays authoritative."""
    return _LEAD_TYPE.sub("", desc or "", count=1).lstrip()


_DOC_STOP = {
    "table", "column", "value", "values", "field", "data", "number", "count",
    "total", "record", "records", "which", "where", "there", "their", "about",
    "based", "whether", "means", "example", "possible", "stored", "store",
}


def _doc_tokens(s) -> set:
    return {w for w in re.findall(r"[A-Za-z]{5,}", str(s or "").lower())
            if w not in _DOC_STOP}


async def reembed_rich_column_docs(graph, emb) -> int:
    """Re-embed every Column with a RICH document so retrieval matches the FULL
    context — not just the bare column description. Each document carries: the
    column + its description, its TABLE + table description, the data type, key/
    constraint info, FK in/out (who references it / what it references), actual
    values, a derived role (id/measure/date/flag/dimension), and the text of any
    business-KB concept that relates to it. The column's ``description`` (what the
    generator reads) is left as the grounded one; only the EMBEDDING is enriched.
    Returns the number of columns re-embedded. Never raises.
    """
    # business-KB concepts (whole text per concept)
    try:
        kb = [str(r[0] or "") for r in
              (await graph.query("MATCH (k:Knowledge) RETURN k.content")).result_set or []]
    except Exception:  # pylint: disable=broad-exception-caught
        kb = []
    kb = [(c, _doc_tokens(c)) for c in kb if c.strip()]
    # all columns + table + FK in/out, in one pass
    q = (
        "MATCH (t:Table)<-[:BELONGS_TO]-(c:Column) "
        "OPTIONAL MATCH (c)-[:REFERENCES]->(fo:Column)-[:BELONGS_TO]->(fot:Table) "
        "WITH t, c, collect(DISTINCT fot.name + '.' + fo.name) AS fk_out "
        "OPTIONAL MATCH (fi:Column)-[:REFERENCES]->(c) "
        "OPTIONAL MATCH (fi)-[:BELONGS_TO]->(fit:Table) "
        "RETURN t.name, t.description, c.name, c.type, c.nullable, c.key_type, "
        "c.sample_values, c.description, fk_out, "
        "collect(DISTINCT fit.name + '.' + fi.name) AS fk_in"
    )
    try:
        rows = (await graph.query(q)).result_set or []
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("rich-column docs: column scan failed: %s", str(exc)[:160])
        return 0

    def _clean_refs(refs):
        return [x for x in (refs or []) if x and not x.startswith(".") and not x.endswith(".")][:6]

    docs: list[str] = []
    keys: list[tuple] = []
    for r in rows:
        try:
            tn, td, cn, ctype, nullable, ktype, samples, cdesc, fk_out, fk_in = (
                r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9])
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        if not tn or not cn:
            continue
        nm, ty = str(cn).lower(), str(ctype or "").lower()
        if ktype or re.search(r"_id$|ref$|code$|key$|uuid", nm):
            role = "identifier / key"
        elif re.search(r"date|time", ty):
            role = "date"
        elif re.search(r"bool", ty):
            role = "boolean flag"
        elif re.search(r"int|real|numer|double|decimal|float|money|serial", ty):
            role = "numeric measure"
        else:
            role = "category / dimension"
        ctoks = _doc_tokens(cn) | _doc_tokens(cdesc)
        rel = []
        for content, ktoks in kb:
            if len(ktoks & ctoks) >= 2:
                rel.append(re.sub(r"\s+", " ", content).strip()[:220])
            if len(rel) >= 2:
                break
        sval = samples if isinstance(samples, str) else (
            ", ".join(str(x) for x in samples) if isinstance(samples, (list, tuple)) else "")
        parts = [
            f"{tn}.{cn}: {(cdesc or cn)}",
            (f"Table «{tn}»: {td}" if td else f"Table «{tn}»")[:300],
            f"Type {ctype or '?'}; "
            + ("primary key; " if str(ktype or "").upper() in {"PRI", "PK"} else "")
            + ("not null; " if str(nullable).upper() in {"NO", "FALSE"} else "")
            + f"role: {role}",
        ]
        if sval:
            parts.append(f"Values: {sval[:200]}")
        # NOTE: FK references (who this column references / is referenced by) are
        # deliberately NOT embedded. Naming OTHER tables in a column's document
        # makes every table with an FK to e.g. `circuits` match a "circuit"
        # query, destroying retrieval precision on large schemas. FK context
        # stays in the graph for the generator + drives expansion via the
        # REFERENCES edges; the EMBEDDING is the column's own identity only.
        if rel:
            parts.append("Business knowledge: " + " | ".join(rel))
        docs.append(". ".join(p for p in parts if p)[:1400])
        keys.append((tn, cn))

    reembedded = 0
    batch = 100
    for i in range(0, len(docs), batch):
        chunk, kchunk = docs[i:i + batch], keys[i:i + batch]
        try:
            vecs = await asyncio.to_thread(emb.embed, chunk)
        except Exception:  # pylint: disable=broad-exception-caught
            # fall back to per-item if the model doesn't batch
            vecs = []
            for d in chunk:
                try:
                    vecs.append((await asyncio.to_thread(emb.embed, d))[0])
                except Exception:  # pylint: disable=broad-exception-caught
                    vecs.append(None)
        ups = [{"t": kchunk[j][0], "c": kchunk[j][1], "e": vecs[j]}
               for j in range(min(len(chunk), len(vecs))) if vecs[j] is not None]
        if not ups:
            continue
        try:
            await graph.query(
                "UNWIND $rows AS row "
                "MATCH (t:Table {name:row.t})<-[:BELONGS_TO]-(c:Column {name:row.c}) "
                "SET c.embedding = vecf32(row.e)", {"rows": ups})
            reembedded += len(ups)
        except Exception:  # pylint: disable=broad-exception-caught
            continue
    if reembedded:
        logging.info("rich-column docs: re-embedded %d column(s) with full context", reembedded)
    return reembedded


def _json_leaf_paths(obj, prefix=(), out=None, depth=3):
    """Collect scalar-leaf key paths in a nested JSON object (arrays sampled by
    first element). Bounded depth. Returns a list of tuples (path components)."""
    if out is None:
        out = []
    if len(prefix) >= depth:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _json_leaf_paths(v, prefix + (str(k),), out, depth)
    elif isinstance(obj, list):
        if obj:
            _json_leaf_paths(obj[0], prefix, out, depth)
    elif prefix:
        out.append(prefix)
    return out


async def ground_json_leaves(loader_class, db_url, db_type, tref_fn, qi_fn,
                             tname, cname, sample_rows, maxcard) -> str:
    """For a JSON/JSONB column, discover low-cardinality leaf paths and their
    distinct values, and return a description fragment like
    " JSON fields: location.country (Italy, Germany, …); location.city (…).".

    This makes the buried values searchable: folded into the column description
    they reach the rich-doc embedding (so a "country" question surfaces the table
    holding `location.country`) AND tell the generator the exact extraction path.
    Postgres/JSONB only (arrow syntax). Never raises.
    """
    if (db_type or "").lower() not in {"postgresql", "postgres"}:
        return ""
    col_q, tbl_q = qi_fn(cname), tref_fn(tname)
    try:
        srows = await asyncio.to_thread(
            loader_class.execute_sql_query,
            f"SELECT {col_q} AS v FROM {tbl_q} WHERE {col_q} IS NOT NULL LIMIT 8",
            db_url)
    except Exception:  # pylint: disable=broad-exception-caught
        return ""
    paths: set = set()
    for r in (srows or []):
        v = r.get("v") if isinstance(r, dict) else r
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except Exception:  # pylint: disable=broad-exception-caught
                continue
        if isinstance(v, (dict, list)):
            for p in _json_leaf_paths(v):
                paths.add(p)
    if not paths:
        return ""
    frags: list[str] = []
    for p in sorted(paths)[:12]:
        expr = col_q
        for k in p[:-1]:
            expr += f"->'{k}'"
        expr += f"->>'{p[-1]}'"
        dsql = (f"SELECT DISTINCT {expr} AS lv "
                f"FROM (SELECT {col_q} FROM {tbl_q} LIMIT {sample_rows}) s "
                f"WHERE {expr} IS NOT NULL LIMIT {maxcard * 2 + 6}")
        try:
            vr = await asyncio.to_thread(loader_class.execute_sql_query, dsql, db_url)
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        vals = []
        for x in (vr or []):
            lv = x.get("lv") if isinstance(x, dict) else x
            if lv is not None:
                vals.append(str(lv))
        if not vals or len(vals) > maxcard * 2:
            continue  # high-card leaf (free text / ids) — skip

        def _categorical(v: str) -> bool:
            v = v.strip()
            if not v or len(v) > 40:
                return False
            if v.replace(".", "", 1).replace("-", "", 1).isdigit():
                return False  # numeric (coordinates, ids, amounts)
            if "://" in v or v.lower().startswith(("http", "www")):
                return False  # URLs / links
            return True

        cat_vals = [v for v in vals if _categorical(v)]
        # keep the leaf only if it is genuinely categorical (a routable domain:
        # country / city / status / name) — not numbers, URLs or free text.
        if len(cat_vals) < max(1, int(len(vals) * 0.6)):
            continue
        shown = ", ".join(cat_vals[:15]) + (", …" if len(cat_vals) > 15 else "")
        frags.append(f"{'.'.join(p)} ({shown})")
    if not frags:
        return ""
    return " JSON fields: " + "; ".join(frags) + "."


async def augment_db_description(graph) -> bool:
    """Ground the database description in the ACTUAL table names so the entity
    extractor (table-finder) has a domain anchor.

    Without it, a weak / prefill-bound model facing an unfamiliar or non-English
    question has nothing to anchor on and hallucinates the most common schema in
    its training data (generic e-commerce: orders, products, customers) — and
    retrieval then faithfully matches the WRONG tables. Listing the real table
    names lets the model infer the true domain (e.g. races/circuits/drivers ->
    motorsport). Deterministic: lists what exists, invents nothing. Idempotent
    (strips a prior "Tables include:" before re-appending). Never raises.
    """
    try:
        trows = (await graph.query(
            "MATCH (t:Table) RETURN t.name ORDER BY t.name")).result_set or []
        names = [str(r[0]) for r in trows if r and r[0]]
        if not names:
            return False
        drows = (await graph.query(
            "MATCH (d:Database) RETURN d.description")).result_set or []
        existing = str(drows[0][0]) if drows and drows[0] and drows[0][0] else ""
        base = re.sub(r"\s*Tables include:.*$", "", existing, flags=re.S).strip()
        if not base:
            base = f"Database with {len(names)} tables."
        cap = 60
        listed = ", ".join(names[:cap])
        tail = "." if len(names) <= cap else f", and {len(names) - cap} more."
        new_desc = base.rstrip(". ") + ". Tables include: " + listed + tail
        await graph.query(
            "MATCH (d:Database) SET d.description = $d", {"d": new_desc})
        logging.info("value_grounding: grounded db_description with %d table names",
                     len(names))
        return True
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("value_grounding: db_description augment skipped: %s",
                        str(exc)[:160])
        return False


async def ground_column_values(
    graph_id: str,
    loader_class,
    db_url: str,
    db_type: str,
    db=None,
    maxcard: int = 20,
    sample_rows: int = 50000,
) -> int:
    """Correct column descriptions (type-word strip + actual value grounding).

    Returns the number of descriptions changed. Never raises.
    """
    try:
        from api.graph import resolve_db  # pylint: disable=import-outside-toplevel
        graph = resolve_db(db).select_graph(graph_id)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("value_grounding: graph '%s' unavailable: %s", graph_id, str(exc)[:160])
        return 0
    if not db_url or loader_class is None:
        return 0
    _bt = (db_type or "").lower() in {"mysql", "maria", "impala", "hive"}
    qi = (lambda r: f"`{r}`") if _bt else (lambda r: f'"{r}"')
    # table names may be schema-qualified (e.g. "public.transactions" after a merge
    # refresh) — quote EACH dotted part so the SQL is `"public"."transactions"`.
    tref = lambda name: ".".join(qi(p) for p in str(name).split("."))
    try:
        res = await graph.query(
            "MATCH (t:Table)<-[:BELONGS_TO]-(c:Column) "
            "RETURN t.name, c.name, c.description, c.type")
        rows = res.result_set or []
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("value_grounding: column scan failed for '%s': %s", graph_id, str(exc)[:160])
        return 0

    changed = 0
    for record in rows:
        try:
            tname, cname, desc = record[0], record[1], (record[2] if len(record) > 2 else "")
            ctype = record[3] if len(record) > 3 else ""
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        if not tname or not cname:
            continue
        desc = desc or ""
        # (1) TYPE FROM DB — strip a leading scalar type word from the prose.
        new_desc = _strip_leading_type(desc)

        # (2) VALUES FROM DB — only for low-cardinality, non-identifier columns.
        if not _SKIP_NAME.search(str(cname)):
            col_q, tbl_q = qi(cname), tref(tname)
            sql = (
                f"SELECT DISTINCT {_cast_text(db_type, col_q)} AS v "
                f"FROM (SELECT {col_q} FROM {tbl_q} LIMIT {sample_rows}) s "
                f"WHERE {col_q} IS NOT NULL LIMIT {maxcard + 6}"
            )
            try:
                vrows = await asyncio.to_thread(loader_class.execute_sql_query, sql, db_url)
            except Exception:  # pylint: disable=broad-exception-caught
                vrows = None
            vals: list[str] = []
            for r in (vrows or []):
                if isinstance(r, dict):
                    v = r.get("v")
                    if v is None and r:
                        v = next(iter(r.values()))
                else:
                    v = r
                if v is not None:
                    vals.append(str(v))
            if vals and len(vals) <= maxcard:
                actual = {_norm(v) for v in vals}
                m = re.search(r"Possible values:\s*([^.\n]+)", new_desc)
                already = bool(m) and {_norm(x) for x in re.split(r"[;,]", m.group(1))} == actual
                if not already:
                    base = re.sub(r"\s*Possible values:\s*[^.\n]*\.?", "", new_desc)
                    base = re.sub(r"\s*Example:\s*[^.\n]*\.?", "", base).strip()
                    if actual <= {"0", "1"} and all(v.strip().lstrip("-").isdigit() for v in vals):
                        tail = (" Integer flag. Possible values: " + ", ".join(vals)
                                + " (1 = yes/true, 0 = no/false). Filter with the integer literal, e.g. = 1.")
                    else:
                        tail = " Possible values: " + ", ".join(vals) + "."
                    new_desc = (base + tail).strip()

        # (2b) JSON LEAVES — index low-card nested leaf values for JSONB columns,
        # so buried fields (e.g. location.country = Italy, Germany, …) become
        # searchable via the embedding AND the generator learns the exact path.
        if "json" in str(ctype).lower():
            try:
                leaf_frag = await ground_json_leaves(
                    loader_class, db_url, db_type, tref, qi, tname, cname,
                    sample_rows, maxcard)
            except Exception:  # pylint: disable=broad-exception-caught
                leaf_frag = ""
            if leaf_frag:
                base = re.sub(r"\s*JSON fields:.*$", "", new_desc, flags=re.S).strip()
                new_desc = (base + leaf_frag).strip()

        if new_desc and new_desc != desc:
            try:
                await graph.query(
                    "MATCH (t:Table {name:$t})<-[:BELONGS_TO]-(c:Column {name:$c}) "
                    "SET c.description=$d", {"t": tname, "c": cname, "d": new_desc})
                changed += 1
            except Exception:  # pylint: disable=broad-exception-caught
                continue
    if changed:
        logging.info("value_grounding: corrected %d column description(s) "
                     "(type-from-DB + actual values) in %s", changed, graph_id)

    # --- TABLE discoverability: backfill placeholder table descriptions from their
    # columns and RE-EMBED Table.embedding, so the finder's table-level vector
    # retrieval can surface a table by its COLUMN CONTENT (e.g. a "fraud" table
    # named `risk_analytics` whose only "fraud" signal lives in a column). Without
    # this, a placeholder table description ("Table: X") embeds to nothing useful
    # and the table is never retrieved. General; runs at load/refresh.
    try:
        from api.config import Config  # pylint: disable=import-outside-toplevel
        emb = getattr(Config, "EMBEDDING_MODEL", None)
    except Exception:  # pylint: disable=broad-exception-caught
        emb = None
    if emb is not None:
        # rich per-column documents → the finder retrieves on the FULL column
        # context (table+desc, type, keys, FK in/out, values, role, related KB),
        # not just the bare description. Runs after column grounding so it reads
        # the grounded descriptions.
        try:
            await reembed_rich_column_docs(graph, emb)
        except Exception:  # pylint: disable=broad-exception-caught
            logging.warning("value_grounding: rich-column re-embed skipped", exc_info=False)
        try:
            crows = (await graph.query(
                "MATCH (t:Table)<-[:BELONGS_TO]-(c:Column) "
                "RETURN t.name, t.description, c.name, c.description")).result_set or []
        except Exception:  # pylint: disable=broad-exception-caught
            crows = []
        by_table: dict = {}
        for r in crows:
            try:
                tn, td, cn, cd = r[0], r[1], r[2], r[3]
            except Exception:  # pylint: disable=broad-exception-caught
                continue
            if not tn:
                continue
            slot = by_table.setdefault(tn, {"desc": td or "", "cols": []})
            slot["cols"].append((cn, cd or ""))
        reembedded = 0
        for tn, info in by_table.items():
            td = (info["desc"] or "").strip()
            placeholder = (not td) or td.lower() == tn.lower() \
                or td.lower().startswith("table:")
            bits = []
            for cn, cd in info["cols"][:40]:
                meaning = re.split(r"\.|Possible values:|Example:", cd)[0].strip()
                bits.append(f"{cn}" + (f" ({meaning[:60]})" if meaning else ""))
            col_summary = "; ".join(bits)
            base = tn.replace("_", " ") if placeholder else td
            new_td = (base.rstrip(". ") + ". Columns: " + col_summary)[:1500]
            try:
                vec = (await asyncio.to_thread(emb.embed, new_td))[0]
                await graph.query(
                    "MATCH (t:Table {name:$t}) SET t.description=$d, t.embedding=vecf32($e)",
                    {"t": tn, "d": new_td, "e": vec})
                reembedded += 1
            except Exception:  # pylint: disable=broad-exception-caught
                continue
        if reembedded:
            logging.info("value_grounding: backfilled + re-embedded %d table "
                         "description(s) in %s", reembedded, graph_id)
    # Ground the DB description in real table names (domain anchor for the
    # entity extractor). Runs regardless of the embedding model.
    await augment_db_description(graph)
    return changed
