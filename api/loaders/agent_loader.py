"""Agent-driven schema loader for T2S.

This loader REPLACES the YAML-merge enrichment step (``POST /database/yaml``)
with a document- and LLM-driven enrichment step (``POST /database/enrich``),
while keeping the rest of the pipeline byte-identical.

Principle (see the T2S Agent-Loader Spec):
  * The initial graph is ALWAYS built from the executable database via the
    unchanged ``POST /database`` → ``graph_loader.load_to_graph`` path. This
    loader NEVER creates schema; it only ENRICHES an existing DB-built graph.
  * It reads arbitrary uploaded documents (any type) + user_rules + business
    knowledge, asks ``SchemaEnrichmentAgent`` for a grounded proposal, runs the
    deterministic ``_validate_proposal`` gate against the LIVE graph snapshot,
    converts the validated proposal into the ``entities``/``relationships`` shape,
    and APPLIES it through the EXISTING mutation engine
    :func:`api.loaders.graph_merge.merge_graph_data` — the same writer the YAML
    path uses. The database stays authoritative: ``type`` is never changed; PK /
    NOT NULL / FK / descriptions are only ADDED.

Reused from the YAML loader (no behaviour change): ``_graph_exists``,
``_existing_database_url``, ``_existing_graph_entities``,
``_filter_invalid_relationships``, ``_finalize_entity_descriptions``,
``_column_key_type``, ``_column_nullable``, ``_column_description``.
"""

import logging
from typing import Any, AsyncGenerator, Dict, Iterable, List, Tuple

from api.agents.schema_enrichment_agent import SchemaEnrichmentAgent, _validate_proposal
from api.loaders.doc_text import extract_documents
from api.loaders.graph_merge import merge_graph_data
from api.loaders.yaml_loader import (
    YamlSchemaLoader,
    _column_description,
    _column_key_type,
    _column_nullable,
    _filter_invalid_relationships,
    _finalize_entity_descriptions,
    _normalize_identifier,
    _safe_error_message,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class AgentSchemaLoader:
    """Enrich a DB-built graph from arbitrary documents via an LLM agent."""

    @staticmethod
    async def _snapshot_live_graph(graph_id: str, db=None) -> Dict[str, Any]:
        """Read the live graph richly: columns with type/key/nullable/samples + FK edges.

        Extends ``YamlSchemaLoader._existing_graph_entities`` (which returns
        ``type``/``description``/``key`` only) with ``nullable``, ``sample_values``,
        and existing ``REFERENCES`` edges, so the enrichment agent can see exactly
        which PK / NOT NULL / FK facts the database is MISSING. The structure is a
        superset of ``_existing_graph_entities`` output, so it can be passed
        straight to ``merge_graph_data(existing_entities=...)`` and the validation
        gate unchanged.
        """
        from api.core.db_resolver import resolve_db  # pylint: disable=import-outside-toplevel

        graph = resolve_db(db).select_graph(graph_id)
        snapshot: Dict[str, Any] = {}

        try:
            result = await graph.query(
                """
                MATCH (c:Column)-[:BELONGS_TO]->(t:Table)
                RETURN t.name, t.description, c.name, c.type,
                       c.description, c.key_type, c.nullable, c.sample_values
                """
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logging.info("Agent loader could not read live graph schema: graph=%s", graph_id)
            return {}

        for row in result.result_set or []:
            if len(row) < 3 or not row[0] or not row[2]:
                continue
            table_name = str(row[0])
            column_name = str(row[2])
            entry = snapshot.setdefault(
                table_name,
                {"description": row[1] if len(row) > 1 else "", "columns": {},
                 "foreign_keys": [], "references": []},
            )
            sample_values: List[str] = []
            raw_samples = row[7] if len(row) > 7 else None
            if isinstance(raw_samples, str) and raw_samples:
                try:
                    import json  # pylint: disable=import-outside-toplevel
                    parsed = json.loads(raw_samples)
                    if isinstance(parsed, list):
                        sample_values = [str(value) for value in parsed]
                except Exception:  # pylint: disable=broad-exception-caught
                    sample_values = []
            elif isinstance(raw_samples, list):
                sample_values = [str(value) for value in raw_samples]
            entry["columns"][column_name] = {
                "type": row[3] if len(row) > 3 else "unknown",
                "description": row[4] if len(row) > 4 else "",
                "key": row[5] if len(row) > 5 else "unknown",
                "nullable": row[6] if len(row) > 6 else "unknown",
                "sample_values": sample_values,
            }

        # Existing FK edges, so the agent does not re-propose links already present.
        try:
            edges = await graph.query(
                """
                MATCH (src:Column)-[r:REFERENCES]->(tgt:Column)
                MATCH (src)-[:BELONGS_TO]->(st:Table)
                MATCH (tgt)-[:BELONGS_TO]->(tt:Table)
                RETURN st.name, src.name, tt.name, tgt.name, r.rel_name
                """
            )
            for row in edges.result_set or []:
                if len(row) < 4 or not row[0]:
                    continue
                src_table = str(row[0])
                if src_table in snapshot:
                    snapshot[src_table]["references"].append({
                        "from_column": str(row[1]),
                        "to_table": str(row[2]),
                        "to_column": str(row[3]),
                        "rel_name": str(row[4]) if len(row) > 4 else "",
                    })
        except Exception:  # pylint: disable=broad-exception-caught
            logging.info("Agent loader could not read live FK edges: graph=%s", graph_id)

        return snapshot

    @staticmethod
    def _proposal_to_graph_data(
        validated: Dict[str, Any],
        snapshot: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, list]]:
        """Convert a VALIDATED proposal into ``(entities, relationships)``.

        Only tables/columns touched by the proposal are included, so the merge is
        minimal and idempotent. Each column keeps its DB ``type`` and starts from
        its live ``key``/``nullable``; the proposal then FILLS gaps (validation
        already ensured these are gaps only). Descriptions and FK fragments are
        rebuilt by reusing ``_finalize_entity_descriptions`` so the column text and
        the ``REFERENCES`` edges stay consistent, exactly like the YAML path.
        """
        table_desc = {
            item["table"]: item["description"]
            for item in validated.get("table_descriptions", [])
        }
        col_desc: Dict[Tuple[str, str], str] = {
            (item["table"], item["column"]): item["description"]
            for item in validated.get("column_descriptions", [])
        }
        pk_set = {(item["table"], item["column"]) for item in validated.get("primary_keys", [])}
        nn_set = {(item["table"], item["column"]) for item in validated.get("not_null", [])}
        foreign_keys = validated.get("foreign_keys", [])

        # Every table that any enrichment item references.
        touched_tables: set[str] = set(table_desc)
        for table, _column in col_desc:
            touched_tables.add(table)
        for table, _column in pk_set | nn_set:
            touched_tables.add(table)
        for fk in foreign_keys:
            touched_tables.add(fk["from_table"])
            touched_tables.add(fk["to_table"])

        fks_by_table: Dict[str, list] = {}
        relationships: Dict[str, list] = {}
        fk_source_columns: set[Tuple[str, str]] = set()
        for index, fk in enumerate(foreign_keys, start=1):
            from_table = fk["from_table"]
            from_column = fk["from_column"]
            to_table = fk["to_table"]
            to_column = fk["to_column"]
            fk_source_columns.add((from_table, from_column))
            constraint_name = (
                f"fk_{from_table.replace('.', '_')}_{from_column}_"
                f"{to_table.replace('.', '_')}_{to_column}_{index}"
            )
            fks_by_table.setdefault(from_table, []).append({
                "constraint_name": constraint_name,
                "column": from_column,
                "referenced_table": to_table,
                "referenced_column": to_column,
            })
            relationships.setdefault(constraint_name, []).append({
                "from": from_table,
                "to": to_table,
                "source_column": from_column,
                "target_column": to_column,
                "note": fk.get("note") or f"Foreign key proposed by enrichment agent: {constraint_name}",
            })

        # Columns the proposal actually touches — only these are written. Live
        # columns NOT mentioned by any enrichment item are deliberately excluded
        # from the entities dict so the merge engine never re-writes their
        # description/embedding/key with a generated stub.
        touched_columns: set[Tuple[str, str]] = set(col_desc)
        touched_columns |= pk_set | nn_set | fk_source_columns

        entities: Dict[str, Any] = {}
        for table_name in touched_tables:
            live_table = snapshot.get(table_name) or {}
            live_columns = live_table.get("columns", {})
            columns_info: Dict[str, Any] = {}
            for column_name, live_col in live_columns.items():
                if (table_name, column_name) not in touched_columns:
                    continue  # untouched live column: leave it exactly as the DB built it
                proposed_description = col_desc.get((table_name, column_name))
                base_description = proposed_description if proposed_description is not None \
                    else live_col.get("description", "")
                constraint_types: set[str] = set()
                if (table_name, column_name) in pk_set:
                    constraint_types.add("primary_key")
                if (table_name, column_name) in nn_set:
                    constraint_types.add("not_null")
                if (table_name, column_name) in fk_source_columns:
                    # Mark FK source columns so _finalize_entity_descriptions
                    # resolves key_type to FOREIGN KEY (gap-fill only) and the
                    # column text/edge stay consistent, exactly as the YAML path.
                    constraint_types.add("foreign_key")
                columns_info[column_name] = {
                    "type": live_col.get("type", "unknown"),
                    "null": _column_nullable(constraint_types)
                    if constraint_types else live_col.get("nullable", "unknown"),
                    "key": _column_key_type(constraint_types, False),
                    "description": base_description,
                    "default": None,
                    "sample_values": live_col.get("sample_values", []) or [],
                    "_constraint_types": sorted(constraint_types),
                    "_base_description": base_description,
                }

            # Only emit a table when it carries enriched columns and/or its own
            # description was proposed; skip pure-FK-target tables that have no
            # touched columns and no new description (the FK edge still merges via
            # relationships, and the target table node is untouched).
            has_table_description = table_name in table_desc
            if not columns_info and not has_table_description and table_name not in fks_by_table:
                continue
            table_description = table_desc.get(table_name) or live_table.get("description") \
                or f"Table {table_name}"
            entities[table_name] = {
                "description": table_description,
                "columns": columns_info,
                "foreign_keys": fks_by_table.get(table_name, []),
                "col_descriptions": [
                    column_info["description"] for column_info in columns_info.values()
                ],
            }

        return entities, relationships

    @staticmethod
    async def enrich_documents(  # pylint: disable=too-many-arguments,too-many-locals
        prefix: str,
        documents: Iterable[tuple[str, bytes | str]],
        database: str,
        execute_url: str | None = None,
        user_rules: str | None = None,
        knowledge: str | None = None,
        db=None,
    ) -> AsyncGenerator[Tuple[bool, str], None]:
        """Enrich an existing DB-built graph from arbitrary documents via an agent.

        Async generator yielding ``(success, message)``, structured like
        ``YamlSchemaLoader.load_documents`` so the streaming wire format is
        unchanged.
        """
        try:
            db_name = _normalize_identifier(database)
            if not db_name:
                yield False, "A database (graph) name is required for enrichment."
                return
            graph_id = f"{prefix}_{db_name}"

            # 1. Guard: the graph must already be DB-built. Never create schema.
            yield True, "Checking that the database graph exists..."
            if not await YamlSchemaLoader._graph_exists(graph_id, db=db):
                yield False, (
                    f"Graph {db_name} does not exist yet. Build it from the database "
                    f"first via POST /database, then enrich it."
                )
                return

            existing_url = await YamlSchemaLoader._existing_database_url(graph_id, db=db)
            effective_execute_url = execute_url or existing_url

            # 2. Snapshot the live graph — the only source of truth for what may be touched.
            yield True, "Reading the live database-built schema snapshot..."
            snapshot = await AgentSchemaLoader._snapshot_live_graph(graph_id, db=db)
            if not snapshot:
                yield False, (
                    f"Graph {db_name} exists but exposes no columns to enrich."
                )
                return

            # 3. Ingest arbitrary documents → plain text (any file type).
            yield True, "Extracting text from uploaded documents..."
            document_text = extract_documents(list(documents or []))

            # 3b. Additively embed the RAW extracted document text into the DB
            #     graph as retrievable (:Document {content, embedding}) nodes
            #     (R6). This is purely ADDITIVE: it never wipes the graph and
            #     accumulates across uploads (chunks are MERGEd by content hash,
            #     so re-uploading the same text does not duplicate). It is
            #     failure-tolerant (logs + continues) so an embedding outage
            #     never aborts the structured enrichment below.
            if (document_text or "").strip():
                yield True, "Embedding uploaded document text for retrieval..."
                from api.graph import index_text_chunks  # pylint: disable=import-outside-toplevel

                doc_chunks = await index_text_chunks(
                    graph_id,
                    "Document",
                    document_text,
                    "uploaded_schema_docs",
                    replace_source=False,
                    db=db,
                )
                yield True, (
                    f"Embedded {doc_chunks} document chunk(s) for retrieval."
                )

            # 4. LLM enrichment proposal grounded against the live snapshot.
            yield True, "Asking the enrichment agent to propose schema enrichments..."
            agent = SchemaEnrichmentAgent()
            raw_proposal = await agent.propose(
                snapshot,
                document_text,
                user_rules=user_rules or "",
                knowledge=knowledge or "",
            )

            # 5. Deterministic validation gate (drops anything not in the snapshot,
            #    fills PK/NOT NULL only into gaps, requires both FK endpoints).
            yield True, "Validating proposed enrichments against the live schema..."
            validated = _validate_proposal(raw_proposal, snapshot)

            # 6. Convert validated proposal → entities/relationships, then reuse the
            #    YAML loader's FK filter + description finalizer for consistency.
            entities, relationships = AgentSchemaLoader._proposal_to_graph_data(
                validated, snapshot
            )
            validation_entities = {**snapshot, **entities}
            relationships, skipped_relationships = _filter_invalid_relationships(
                validation_entities, relationships
            )
            _finalize_entity_descriptions(entities)

            # 7. Apply via the EXISTING mutation engine (the single graph writer).
            if entities or relationships:
                yield True, "Merging validated enrichments into the graph..."
                await merge_graph_data(
                    graph_id,
                    entities,
                    relationships,
                    db_name=db_name,
                    execute_url=effective_execute_url,
                    db=db,
                    existing_entities=snapshot,
                )
            else:
                yield True, "No grounded enrichments to apply."

            # 8. Persist rules / knowledge if supplied.
            if user_rules or knowledge:
                from api.graph import set_knowledge, set_user_rules  # pylint: disable=import-outside-toplevel

                if user_rules:
                    await set_user_rules(graph_id, user_rules, db=db)
                if knowledge:
                    await set_knowledge(graph_id, knowledge, db=db)

            # 9. Final result with counts.
            fk_count = sum(len(items) for items in relationships.values())
            skipped = validated.get("skipped", {}) if isinstance(validated, dict) else {}
            skipped_total = sum(int(value) for value in (skipped or {}).values())
            yield True, (
                f"Schema enriched successfully. "
                f"Tables described: {len(validated.get('table_descriptions', []))}; "
                f"columns described: {len(validated.get('column_descriptions', []))}; "
                f"primary keys filled: {len(validated.get('primary_keys', []))}; "
                f"NOT NULL filled: {len(validated.get('not_null', []))}; "
                f"foreign-key edges added: {fk_count}; "
                f"items skipped as not-in-schema or non-gap: {skipped_total}."
                + (
                    f" Dropped {len(skipped_relationships)} invalid FK references."
                    if skipped_relationships else ""
                )
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.exception("Unexpected agent enrichment error: %s", exc)
            yield False, f"Failed to enrich schema: {_safe_error_message(exc)}"
