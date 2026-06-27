"""Shared graph-merge engine for T2S schema loaders.

This module hosts ``merge_graph_data`` — the single Cypher mutation engine that
non-destructively merges supplemental schema metadata (descriptions, sample
values, key/nullable gap-fills, and declared FK relationships) into a graph that
was already built from an executable database via ``graph_loader.load_to_graph``.

It is a pure lift of what previously lived as
``YamlSchemaLoader._merge_graph_data`` so that BOTH the YAML loader and the new
agent loader can import one canonical writer instead of reaching for a
``@staticmethod`` on another loader class. Behaviour is byte-for-byte identical
to the original; ``YamlSchemaLoader._merge_graph_data`` now delegates here.

The contract it enforces (the DATABASE IS THE FACT):
  * Table/column existence and data ``type`` are never created or overwritten
    when the graph is backed by a real database — objects absent from the live
    schema are skipped with a warning, not materialized.
  * ``key_type`` and ``nullable`` are only FILLED into gaps (when the live value
    is empty / NULL / 'NONE' / 'unknown'); a DB-asserted fact is never replaced.
  * Descriptions, embeddings, sample values, and ``REFERENCES`` (FK) edges are
    supplemented idempotently.
"""

import json
import logging
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


async def merge_graph_data(  # pylint: disable=too-many-arguments,too-many-locals,too-many-branches,too-many-statements
    graph_id: str,
    entities: dict[str, Any],
    relationships: dict[str, list[dict[str, str]]],
    db_name: str,
    execute_url: str,
    db=None,
    existing_entities: dict[str, Any] | None = None,
) -> None:
    """Merge supplemental metadata into an existing graph without duplicating nodes.

    When the graph is backed by a real database (``execute_url`` points at an
    executable engine), the DATABASE IS THE FACT: structural metadata loaded from
    it (table/column existence, data types, nullability, key flags) is never
    overwritten. Supplemental sources (YAML files, enrichment documents) may only
    supplement business meaning: descriptions, sample values, declared
    relationships, and gap-only PK / NOT NULL flags. Tables/columns present only
    in the supplement are skipped with a warning instead of materializing phantom
    schema objects that would poison retrieval and the identifier gate. Pure
    (no executable database) graphs keep the historical full-write behaviour.
    """
    from api.config import Config  # pylint: disable=import-outside-toplevel
    from api.core.db_resolver import resolve_db  # pylint: disable=import-outside-toplevel
    from api.utils import generate_db_description  # pylint: disable=import-outside-toplevel

    graph = resolve_db(db).select_graph(graph_id)
    embedding_model = Config.EMBEDDING_MODEL
    vec_len = embedding_model.get_vector_size()

    db_backed = bool(
        execute_url and not execute_url.lower().startswith("yaml://")
    )
    known = existing_entities or {}
    skipped_tables: list[str] = []
    skipped_columns: list[str] = []

    try:
        await graph.query(
            """
            CREATE VECTOR INDEX FOR (t:Table) ON (t.embedding)
            OPTIONS {dimension:$size, similarityFunction:'euclidean'}
            """,
            {"size": vec_len},
        )
        await graph.query(
            """
            CREATE VECTOR INDEX FOR (c:Column) ON (c.embedding)
            OPTIONS {dimension:$size, similarityFunction:'euclidean'}
            """,
            {"size": vec_len},
        )
        await graph.query("CREATE INDEX FOR (p:Table) ON (p.name)")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.info("Graph merge skipped index creation: %s", exc)

    all_table_names = sorted(set(known.keys()) | set(entities.keys())) \
        if db_backed else list(entities.keys())
    db_description = generate_db_description(
        db_name=db_name,
        table_names=all_table_names,
    )
    await graph.query(
        """
        MERGE (d:Database {name: $db_name})
        SET d.description = $description,
            d.url = $url
        """,
        {"db_name": db_name, "description": db_description, "url": execute_url or ""},
    )

    for table_name, table_info in entities.items():
        if db_backed and table_name not in known:
            # The database is the fact: don't materialize supplement-only tables.
            skipped_tables.append(table_name)
            continue
        table_description = table_info["description"]
        table_embedding = embedding_model.embed(table_description)[0]
        foreign_keys = json.dumps(table_info.get("foreign_keys", []))
        await graph.query(
            """
            MERGE (t:Table {name: $table_name})
            SET t.description = $description,
                t.embedding = vecf32($embedding),
                t.foreign_keys = $foreign_keys
            """,
            {
                "table_name": table_name,
                "description": table_description,
                "embedding": table_embedding,
                "foreign_keys": foreign_keys,
            },
        )

        columns = table_info.get("columns", {})
        col_descriptions = table_info.get("col_descriptions") or [
            column_info["description"] for column_info in columns.values()
        ]
        try:
            column_embeddings = embedding_model.embed(col_descriptions) if col_descriptions else []
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning(
                "Graph merge batch column embedding failed for %s: %s",
                table_name,
                exc,
            )
            column_embeddings = []

        known_columns = (known.get(table_name) or {}).get("columns", {}) \
            if db_backed else {}
        for index, (column_name, column_info) in enumerate(columns.items()):
            if db_backed and column_name not in known_columns:
                # The database is the fact: don't materialize supplement-only
                # columns the executable schema does not have.
                skipped_columns.append(f"{table_name}.{column_name}")
                continue
            if index < len(column_embeddings):
                column_embedding = column_embeddings[index]
            else:
                column_embedding = embedding_model.embed(column_info["description"])[0]

            sample_values = [
                str(value)[:160]
                for value in (column_info.get("sample_values", []) or [])[:5]
                if value is not None and str(value) != ""
            ]
            params = {
                "table_name": table_name,
                "col_name": column_name,
                "type": column_info.get("type", "unknown"),
                "nullable": column_info.get("null", "unknown"),
                "key": column_info.get("key", "unknown"),
                "sample_values": json.dumps(sample_values, ensure_ascii=False),
                "description": column_info["description"],
                "embedding": column_embedding,
            }
            existing = await graph.query(
                """
                MATCH (c:Column {name: $col_name})-[:BELONGS_TO]->(:Table {name: $table_name})
                RETURN count(c)
                """,
                {"col_name": column_name, "table_name": table_name},
            )
            if existing.result_set and int(existing.result_set[0][0]) > 0:
                if db_backed:
                    # Database metadata stays authoritative for what the
                    # database actually KNOWS (data types). Key flags and
                    # nullability are facts engines like Impala don't store —
                    # the supplement legitimately SUPPLEMENTS them when the
                    # graph has none (grain lint and key heuristics depend on
                    # PK marks). Descriptions/samples supplement too.
                    update_query = """
                    MATCH (c:Column {name: $col_name})-[:BELONGS_TO]->(:Table {name: $table_name})
                    SET c.description = $description,
                        c.embedding = vecf32($embedding),
                        c.key_type = CASE
                            WHEN c.key_type IS NULL OR c.key_type = ''
                                 OR c.key_type = 'NONE' OR c.key_type = 'unknown'
                            THEN $key ELSE c.key_type END,
                        c.nullable = CASE
                            WHEN c.nullable IS NULL OR c.nullable = ''
                                 OR c.nullable = 'unknown'
                            THEN $nullable ELSE c.nullable END
                    """
                    if sample_values:
                        update_query += ", c.sample_values = $sample_values"
                    await graph.query(update_query, params)
                else:
                    await graph.query(
                        """
                        MATCH (c:Column {name: $col_name})-[:BELONGS_TO]->(:Table {name: $table_name})
                        SET c.type = $type,
                            c.nullable = $nullable,
                            c.key_type = $key,
                            c.sample_values = $sample_values,
                            c.description = $description,
                            c.embedding = vecf32($embedding)
                        """,
                        params,
                    )
            else:
                await graph.query(
                    """
                    MATCH (t:Table {name: $table_name})
                    CREATE (c:Column {
                        name: $col_name,
                        type: $type,
                        nullable: $nullable,
                        key_type: $key,
                        sample_values: $sample_values,
                        description: $description,
                        embedding: vecf32($embedding)
                    })-[:BELONGS_TO]->(t)
                    """,
                    params,
                )

    if skipped_tables or skipped_columns:
        logging.warning(
            "Graph merge skipped objects absent from the executable database "
            "(database is the fact): tables=%s columns=%s",
            skipped_tables[:20],
            skipped_columns[:30],
        )

    for relationship_name, items in relationships.items():
        for rel in items:
            try:
                await graph.query(
                    """
                    MATCH (src:Column {name: $source_col})
                        -[:BELONGS_TO]->(:Table {name: $source_table})
                    MATCH (tgt:Column {name: $target_col})
                        -[:BELONGS_TO]->(:Table {name: $target_table})
                    MERGE (src)-[r:REFERENCES {rel_name: $rel_name}]->(tgt)
                    SET r.note = $note
                    """,
                    {
                        "source_col": rel["source_column"],
                        "target_col": rel["target_column"],
                        "source_table": rel["from"],
                        "target_table": rel["to"],
                        "rel_name": relationship_name,
                        "note": rel.get("note", ""),
                    },
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning(
                    "Could not merge relationship %s: %s",
                    relationship_name,
                    exc,
                )
