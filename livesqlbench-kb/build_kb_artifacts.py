#!/usr/bin/env python3
"""Build T2S delivery artifacts from the OFFICIAL LiveSQLBench sports_events_large KB.

Source: Hugging Face `birdsql/livesqlbench-large-v1`, folder `sports_events_large`:
  - sports_events_large_column_meaning_base.json  (every column's authoritative meaning)
  - sports_events_large_kb.jsonl                  (named business metrics / HKB)

Produces:
  - db-init/02_sports_column_comments.sql   COMMENT ON COLUMN ... (read by the index
        into the graph as column descriptions; auto-applied on a fresh Postgres seed)
  - business-rules/sports_business_knowledge.md   the HKB metrics, loaded as DB knowledge
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent  # /Applications/Python/T2S
KB = ROOT / "livesqlbench-kb"

# ---- column comments SQL (mirrors the official apply_column_comments.py) ----
cm = json.load((KB / "sports_events_large_column_meaning_base.json").open())
stmts = []
for key, desc in cm.items():
    parts = key.split("|")
    if len(parts) != 3:
        continue
    _db, table, column = parts
    if isinstance(desc, dict):  # JSONB nested-schema descriptions
        text = (desc.get("column_meaning", "") + "  Nested fields: "
                + json.dumps(desc.get("fields_meaning", {}), ensure_ascii=False))
    else:
        text = str(desc)
    d = text.replace("'", "''")
    # The dump folds identifiers to lower case; the meaning keys are sometimes
    # mixed-case (e.g. CIRCUIT_DETAIL_CODE, cctRef). Lower-case both so the
    # quoted identifier matches the actual column.
    stmts.append(f'COMMENT ON COLUMN "{table.lower()}"."{column.lower()}" IS \'{d}\';')

sql = (
    "-- T2S: official LiveSQLBench column meanings for sports_events_large.\n"
    "-- Auto-applied on a fresh Postgres seed; the schema index reads these via\n"
    "-- pg_description into the graph's column descriptions. No transaction wrapper:\n"
    "-- each COMMENT autocommits, so one column absent from a given dump can't roll\n"
    "-- back the rest.\n"
    + "\n".join(stmts) + "\n"
)
(ROOT / "db-init" / "02_sports_column_comments.sql").write_text(sql)
print("column comments:", len(stmts))

# ---- KB metrics -> markdown (loaded as DB-specific business knowledge) ----
rows = [json.loads(l) for l in (KB / "sports_events_large_kb.jsonl").open() if l.strip()]
md = [
    "# Sports (Formula 1) — Business Knowledge",
    "",
    "Official LiveSQLBench knowledge base for the `sports_events_large` database:",
    "named concepts and metric definitions used to interpret questions and write SQL.",
    "",
]
for r in rows:
    name = (r.get("knowledge") or "").strip()
    if not name:
        continue
    md.append(f"## {name}")
    if r.get("description"):
        md.append(str(r["description"]).strip())
    if r.get("definition"):
        md.append("")
        md.append(f"Definition: {str(r['definition']).strip()}")
    md.append("")
(ROOT / "business-rules" / "sports_business_knowledge.md").write_text("\n".join(md))
print("kb metrics:", len(rows))
