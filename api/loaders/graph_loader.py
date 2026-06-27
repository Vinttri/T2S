"""Graph loader module for loading data into graph databases."""

import inspect
import json
import logging
import os
from typing import Awaitable, Callable


from api.config import Config
from api.utils import generate_db_description, create_combined_description

ProgressCallback = Callable[[str], Awaitable[None] | None]


async def _report_progress(progress_callback: ProgressCallback | None, message: str) -> None:
    """Log graph loading progress and optionally stream it to the caller."""
    logging.info(message)
    if progress_callback is None:
        return
    result = progress_callback(message)
    if inspect.isawaitable(result):
        await result


def _progress_every() -> int:
    raw = os.getenv("GRAPH_LOAD_PROGRESS_EVERY_TABLES", "1").strip()
    try:
        value = int(raw)
    except ValueError:
        return 1
    return max(1, value)


def _should_report(index: int, total: int, every: int) -> bool:
    return index == 1 or index == total or index % every == 0


async def load_to_graph(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    graph_id: str,
    entities: dict,
    relationships: dict,
    batch_size: int = 100,
    db_name: str = "TBD",
    db_url: str = "",
    db=None,
    generate_descriptions: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> None:
    """
    Load the graph data into the database.
    It gets the Graph name as an argument and expects

    Input:
    - entities: A dictionary containing the entities and their attributes.
    - relationships: A dictionary containing the relationships between entities.
    - batch_size: The size of the batch for embedding.
    - db_name: The name of the database.
    - db: Optional FalkorDB handle; falls back to the server singleton.
    """
    from api.core.db_resolver import resolve_db  # pylint: disable=import-outside-toplevel

    table_count = len(entities)
    column_count = sum(
        len(table_info.get("columns", {})) for table_info in entities.values()
    )
    relationship_count = sum(len(items) for items in relationships.values())
    await _report_progress(
        progress_callback,
        (
            f"Building RAG graph '{graph_id}': preparing {table_count} tables, "
            f"{column_count} columns, {relationship_count} relationships."
        ),
    )

    graph = resolve_db(db).select_graph(graph_id)
    embedding_model = Config.EMBEDDING_MODEL
    await _report_progress(
        progress_callback,
        "Building RAG graph: checking embedding model and vector size...",
    )
    vec_len = embedding_model.get_vector_size()
    await _report_progress(
        progress_callback,
        f"Building RAG graph: embedding vector size is {vec_len}.",
    )

    if generate_descriptions:
        await _report_progress(
            progress_callback,
            "Building RAG graph: generating combined table and column descriptions...",
        )
        create_combined_description(entities)

    try:
        await _report_progress(
            progress_callback,
            "Building RAG graph: creating vector indexes in FalkorDB...",
        )
        # Create vector indices
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
        # Vector indexes for additive, retrievable free-text nodes that may be
        # added later in this SAME DB graph: appended knowledge (:Knowledge),
        # chunked user rules (:UserRuleChunk), and uploaded schema documents
        # (:Document). Creating them up front (alongside Table/Column) mirrors the
        # existing pattern; they stay empty until knowledge/rules/docs are loaded.
        for _text_label in ("Knowledge", "UserRuleChunk", "Document"):
            await graph.query(
                f"""
                CREATE VECTOR INDEX FOR (n:{_text_label}) ON (n.embedding)
                OPTIONS {{dimension:$size, similarityFunction:'euclidean'}}
                """,
                {"size": vec_len},
            )
    except Exception as e:  # pylint: disable=broad-exception-caught
        logging.warning("Error creating vector indices: %s", str(e))

    await _report_progress(
        progress_callback,
        f"Building RAG graph: creating database node for '{db_name}'...",
    )
    # Only use the LLM for the DB description when descriptions are enabled, and
    # never let an LLM outage abort a DB-only index (the database is the fact).
    if generate_descriptions:
        try:
            db_des = generate_db_description(db_name=db_name, table_names=list(entities.keys()))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("DB description generation failed (%s); using a stub.", exc)
            db_des = f"{db_name} database with {len(entities)} tables."
    else:
        db_des = f"{db_name} database with {len(entities)} tables."
    await graph.query(
        """
        CREATE (d:Database {
            name: $db_name,
            description: $description,
            url: $url
        })
        """,
        {"db_name": db_name, "description": db_des, "url": db_url},
    )

    progress_every = _progress_every()
    entity_items = list(entities.items())
    for table_index, (table_name, table_info) in enumerate(entity_items, start=1):
        columns = table_info.get("columns", {})
        if _should_report(table_index, table_count, progress_every):
            await _report_progress(
                progress_callback,
                (
                    f"Building RAG graph: table {table_index}/{table_count} "
                    f"'{table_name}' with {len(columns)} columns."
                ),
            )

        table_desc = table_info["description"]
        await _report_progress(
            progress_callback,
            f"Building RAG graph: embedding table '{table_name}' description...",
        )
        embedding_result = embedding_model.embed(table_desc)
        fk = json.dumps(table_info.get("foreign_keys", []))

        # Create table node
        await graph.query(
            """
            CREATE (t:Table {
                name: $table_name,
                description: $description,
                embedding: vecf32($embedding),
                foreign_keys: $foreign_keys
            })
            """,
            {
                "table_name": table_name,
                "description": table_desc,
                "embedding": embedding_result[0],
                "foreign_keys": fk,
            },
        )

        # Batch embeddings for table columns
        # TODO: Check if the embedding model and description are correct  # pylint: disable=fixme
        # (without 2 sources of truth)
        batch_flag = True
        col_descriptions = table_info.get("col_descriptions")
        if col_descriptions is None:
            batch_flag = False
        else:
            try:
                embed_columns = []
                batches = [
                    col_descriptions[i : i + batch_size]
                    for i in range(0, len(col_descriptions), batch_size)
                ]
                await _report_progress(
                    progress_callback,
                    (
                        f"Building RAG graph: embedding {len(col_descriptions)} "
                        f"columns for '{table_name}' in {len(batches)} batches..."
                    ),
                )
                for batch_index, batch in enumerate(batches, start=1):
                    if _should_report(batch_index, len(batches), 5):
                        await _report_progress(
                            progress_callback,
                            (
                                f"Building RAG graph: embedding column batch "
                                f"{batch_index}/{len(batches)} for '{table_name}'."
                            ),
                        )

                    embedding_result = embedding_model.embed(batch)
                    embed_columns.extend(embedding_result)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logging.warning("Error creating embeddings for %s: %s", table_name, str(e))
                batch_flag = False

        # Create column nodes
        await _report_progress(
            progress_callback,
            f"Building RAG graph: writing {len(columns)} columns for '{table_name}'...",
        )
        for idx, (col_name, col_info) in enumerate(table_info["columns"].items()):
            if not batch_flag:
                embed_columns = []
                await _report_progress(
                    progress_callback,
                    (
                        f"Building RAG graph: embedding column '{table_name}.{col_name}' "
                        "individually..."
                    ),
                )
                embedding_result = embedding_model.embed(col_info["description"])
                embed_columns.extend(embedding_result)
                idx = 0

            final_description = col_info["description"]
            sample_values = [
                str(value)[:160]
                for value in (col_info.get("sample_values", []) or [])[:5]
                if value is not None and str(value) != ""
            ]

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
                {
                    "table_name": table_name,
                    "col_name": col_name,
                    "type": col_info.get("type", "unknown"),
                    "nullable": col_info.get("null", "unknown"),
                    "key": col_info.get("key", "unknown"),
                    "sample_values": json.dumps(sample_values, ensure_ascii=False),
                    "description": final_description,
                    "embedding": embed_columns[idx],
                },
            )

        if _should_report(table_index, table_count, progress_every):
            await _report_progress(
                progress_callback,
                f"Building RAG graph: completed table {table_index}/{table_count} '{table_name}'.",
            )

    # Create relationships
    await _report_progress(
        progress_callback,
        f"Building RAG graph: writing {relationship_count} relationships...",
    )
    for rel_name, table_info in relationships.items():
        for rel in table_info:
            source_table = rel["from"]
            source_field = rel["source_column"]
            target_table = rel["to"]
            target_field = rel["target_column"]
            note = rel.get("note", "")

            # Create relationship if both tables and columns exist
            try:
                await graph.query(
                    """
                    MATCH (src:Column {name: $source_col})
                        -[:BELONGS_TO]->(source:Table {name: $source_table})
                    MATCH (tgt:Column {name: $target_col})
                        -[:BELONGS_TO]->(target:Table {name: $target_table})
                    CREATE (src)-[:REFERENCES {
                        rel_name: $rel_name,
                        note: $note
                    }]->(tgt)
                    """,
                    {
                        "source_col": source_field,
                        "target_col": target_field,
                        "source_table": source_table,
                        "target_table": target_table,
                        "rel_name": rel_name,
                        "note": note,
                    },
                )
            except Exception as e:  # pylint: disable=broad-exception-caught
                logging.warning("Could not create relationship %s: %s", rel_name, str(e))
                continue

    await _report_progress(
        progress_callback,
        f"Building RAG graph '{graph_id}' completed.",
    )
