"""Load-time schema-enrichment agent for the T2S Text2SQL pipeline.

This agent runs at LOAD time (not query time, unlike ``SchemaTopUpAgent``). After
the graph has been built from an executable database via the unchanged
``POST /database`` path, this agent reads the LIVE DB-built graph snapshot
together with arbitrary enrichment documents (data dictionaries, glossaries,
spec PDFs, CSV mappings…), the user_rules, and the business knowledge, and
proposes structured enrichments:

    {
      "table_descriptions":  [{"table","description"}],
      "column_descriptions": [{"table","column","description"}],
      "primary_keys":        [{"table","column"}],
      "not_null":            [{"table","column"}],
      "foreign_keys":        [{"from_table","from_column","to_table","to_column","note"}]
    }

The proposal is NOT trusted. A deterministic :func:`_validate_proposal` gate runs
before anything reaches the graph and enforces the invariants:
  * Drop any table/column not present in the live snapshot (no phantom schema).
  * FK endpoints must both exist in the snapshot.
  * PK / NOT NULL are only kept where the live column has a GAP
    (``key_type`` in {'', None, 'NONE', 'unknown'} / ``nullable`` in
    {'', None, 'unknown'}); a DB-asserted fact is never overridden.
  * Data ``type`` is never proposed or changed — it stays DB-true.

LLM and JSON plumbing reuse the conventions in ``api.graph`` (the
``Config.completion_kwargs`` call shape and ``_completion_message_content``
parser); no new LLM client is introduced.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Live key_type values that mean "the database asserted no key here" — only such
# gaps may be filled by a proposal. Mirrors the CASE guards in graph_merge.
_KEY_GAP_VALUES = {"", "none", "unknown"}
# Live nullable values that mean "the database asserted no nullability here".
_NULLABLE_GAP_VALUES = {"", "unknown"}

_SYSTEM_PROMPT = """\
You are a load-time database schema ENRICHMENT agent for a text-to-SQL system.

You are given:
  * The LIVE schema, already built from the real database. This is the FACT.
  * Free-form business documents (data dictionaries, glossaries, specs).
  * Optional user rules and business knowledge.

Your job: propose ENRICHMENTS that fill gaps the database itself does not record
— human-readable table/column descriptions, primary-key marks, NOT NULL marks,
and foreign-key links — grounded ONLY in the documents and the live schema.

HARD CONSTRAINTS:
  * You may ONLY reference tables and columns that appear in the provided live
    schema. Never invent a table, column, or type. If a document mentions
    something absent from the schema, ignore it.
  * Never change a data type. Never restate facts the database already provides.
  * Propose a primary key / NOT NULL only where the live column currently lacks
    that fact AND a document clearly establishes it.
  * Propose a foreign key only when BOTH endpoint columns exist in the live
    schema and a document (or an obvious key relationship in the schema)
    supports the link.
  * Descriptions must be concise business meaning, in the document's language.

Return STRICT JSON, no prose, with exactly this shape:
{
  "table_descriptions":  [{"table": "...", "description": "..."}],
  "column_descriptions": [{"table": "...", "column": "...", "description": "..."}],
  "primary_keys":        [{"table": "...", "column": "..."}],
  "not_null":            [{"table": "...", "column": "..."}],
  "foreign_keys":        [{"from_table": "...", "from_column": "...",
                           "to_table": "...", "to_column": "...", "note": "..."}]
}
Omit empty arrays' contents but keep the keys. Output the JSON object only.
"""

_EMPTY_PROPOSAL: Dict[str, List[dict]] = {
    "table_descriptions": [],
    "column_descriptions": [],
    "primary_keys": [],
    "not_null": [],
    "foreign_keys": [],
}


def _parse_enrichment_response(response: str) -> Dict[str, List[dict]]:
    """Extract the enrichment proposal JSON object from an LLM response.

    Mirrors ``api.graph._parse_descriptions_response``: scan for the first ``{``
    that ``raw_decode`` can parse into a dict carrying any of our expected keys.
    Returns an empty proposal (never raises) when nothing usable is found.
    """
    if not response:
        return dict(_EMPTY_PROPOSAL)

    expected = set(_EMPTY_PROPOSAL.keys())
    decoder = json.JSONDecoder()
    for index, char in enumerate(response):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(response[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and expected & set(candidate.keys()):
            merged = dict(_EMPTY_PROPOSAL)
            for key in expected:
                value = candidate.get(key)
                merged[key] = value if isinstance(value, list) else []
            return merged
    return dict(_EMPTY_PROPOSAL)


def _merge_proposals(proposals: List[Dict[str, List[dict]]]) -> Dict[str, List[dict]]:
    """Combine per-chunk proposals, de-duplicating identical items per key."""
    merged: Dict[str, List[dict]] = {key: [] for key in _EMPTY_PROPOSAL}
    seen: Dict[str, set] = {key: set() for key in _EMPTY_PROPOSAL}
    for proposal in proposals:
        for key in _EMPTY_PROPOSAL:
            for item in proposal.get(key, []) or []:
                if not isinstance(item, dict):
                    continue
                fingerprint = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if fingerprint in seen[key]:
                    continue
                seen[key].add(fingerprint)
                merged[key].append(item)
    return merged


def _norm(value: Any) -> str:
    """Normalize an identifier to QW's lower-case, unquoted style."""
    return str(value or "").strip().strip('"`[]').lower()


def _short(name: str) -> str:
    return _norm(name).rsplit(".", 1)[-1]


def _resolve_table(name: str, snapshot: Dict[str, Any], by_short: Dict[str, List[str]]) -> str:
    """Map a proposed table name onto a live snapshot name (exact, then short)."""
    raw = _norm(name)
    if not raw:
        return ""
    if raw in snapshot:
        return raw
    candidates = by_short.get(_short(raw)) or []
    if len(candidates) == 1:
        return candidates[0]
    return ""  # ambiguous or absent → drop (the database is the fact)


def _validate_proposal(
    proposal: Dict[str, List[dict]],
    snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """Deterministic gate: keep only grounded, gap-filling enrichments.

    ``snapshot`` is the live-graph read: ``{table: {"columns": {col: {type,
    description, key, nullable, sample_values}}, "foreign_keys": [...],
    "references": [...]}}``.

    Returns a dict with the validated, snapshot-resolved items plus a ``skipped``
    report. Nothing here writes to the graph; the converter consumes this output.
    """
    by_short: Dict[str, List[str]] = {}
    for table_name in snapshot:
        by_short.setdefault(_short(table_name), []).append(table_name)

    def column_info(table: str, column: str) -> Optional[dict]:
        cols = (snapshot.get(table) or {}).get("columns", {})
        return cols.get(_norm(column))

    validated: Dict[str, List[dict]] = {key: [] for key in _EMPTY_PROPOSAL}
    skipped: Dict[str, int] = {key: 0 for key in _EMPTY_PROPOSAL}

    # -- table descriptions --------------------------------------------------
    for item in proposal.get("table_descriptions", []) or []:
        if not isinstance(item, dict):
            skipped["table_descriptions"] += 1
            continue
        table = _resolve_table(item.get("table", ""), snapshot, by_short)
        description = str(item.get("description") or "").strip()
        if not table or not description:
            skipped["table_descriptions"] += 1
            continue
        validated["table_descriptions"].append({"table": table, "description": description})

    # -- column descriptions -------------------------------------------------
    for item in proposal.get("column_descriptions", []) or []:
        if not isinstance(item, dict):
            skipped["column_descriptions"] += 1
            continue
        table = _resolve_table(item.get("table", ""), snapshot, by_short)
        column = _norm(item.get("column"))
        description = str(item.get("description") or "").strip()
        if not table or not column or not description or column_info(table, column) is None:
            skipped["column_descriptions"] += 1
            continue
        validated["column_descriptions"].append(
            {"table": table, "column": column, "description": description}
        )

    # -- primary keys (gap-fill only) ----------------------------------------
    for item in proposal.get("primary_keys", []) or []:
        if not isinstance(item, dict):
            skipped["primary_keys"] += 1
            continue
        table = _resolve_table(item.get("table", ""), snapshot, by_short)
        column = _norm(item.get("column"))
        info = column_info(table, column) if table and column else None
        if info is None:
            skipped["primary_keys"] += 1
            continue
        live_key = _norm(info.get("key"))
        if live_key not in _KEY_GAP_VALUES:
            # The database already asserted a key flag here — never override it.
            skipped["primary_keys"] += 1
            continue
        validated["primary_keys"].append({"table": table, "column": column})

    # -- not null (gap-fill only) --------------------------------------------
    for item in proposal.get("not_null", []) or []:
        if not isinstance(item, dict):
            skipped["not_null"] += 1
            continue
        table = _resolve_table(item.get("table", ""), snapshot, by_short)
        column = _norm(item.get("column"))
        info = column_info(table, column) if table and column else None
        if info is None:
            skipped["not_null"] += 1
            continue
        live_nullable = _norm(info.get("nullable"))
        if live_nullable not in _NULLABLE_GAP_VALUES:
            # The database already asserted nullability — never override it.
            skipped["not_null"] += 1
            continue
        validated["not_null"].append({"table": table, "column": column})

    # -- foreign keys (both endpoints must exist) ----------------------------
    seen_fk: set = set()
    for item in proposal.get("foreign_keys", []) or []:
        if not isinstance(item, dict):
            skipped["foreign_keys"] += 1
            continue
        from_table = _resolve_table(item.get("from_table", ""), snapshot, by_short)
        to_table = _resolve_table(item.get("to_table", ""), snapshot, by_short)
        from_column = _norm(item.get("from_column"))
        to_column = _norm(item.get("to_column"))
        if (
            not from_table or not to_table or not from_column or not to_column
            or column_info(from_table, from_column) is None
            or column_info(to_table, to_column) is None
        ):
            skipped["foreign_keys"] += 1
            continue
        fingerprint = (from_table, from_column, to_table, to_column)
        if fingerprint in seen_fk:
            continue
        seen_fk.add(fingerprint)
        validated["foreign_keys"].append({
            "from_table": from_table,
            "from_column": from_column,
            "to_table": to_table,
            "to_column": to_column,
            "note": str(item.get("note") or "").strip(),
        })

    validated["skipped"] = skipped
    return validated


class SchemaEnrichmentAgent:
    """Propose grounded schema enrichments from documents + a live graph snapshot.

    The agent is intentionally I/O-light: it formats a prompt, runs one or more
    bounded LLM rounds over chunked document text (mirroring the bounded-rounds
    pattern of ``SchemaTopUpAgent``), parses each response, merges, and validates.
    The LLM call shape reuses ``Config.completion_kwargs`` exactly as
    ``api.graph._rerank_tables_with_llm`` does.
    """

    def __init__(
        self,
        max_rounds: int = 4,
        chunk_chars: int = 60_000,
        max_tokens: int = 4000,
    ) -> None:
        self._max_rounds = max(1, int(max_rounds))
        self._chunk_chars = max(2_000, int(chunk_chars))
        self._max_tokens = max(256, int(max_tokens))

    @staticmethod
    def _snapshot_for_prompt(snapshot: Dict[str, Any], max_sample_values: int = 5) -> list[dict]:
        """Render the live snapshot compactly for the LLM (facts it must respect)."""
        tables: list[dict] = []
        for table_name, table_info in snapshot.items():
            columns = []
            for column_name, column in (table_info.get("columns") or {}).items():
                samples = (column.get("sample_values") or [])[:max_sample_values]
                columns.append({
                    "name": column_name,
                    "type": column.get("type", ""),
                    "key_type": column.get("key", ""),
                    "nullable": column.get("nullable", ""),
                    "current_description": column.get("description", ""),
                    "sample_values": samples,
                })
            tables.append({
                "table": table_name,
                "current_description": table_info.get("description", ""),
                "existing_foreign_keys": table_info.get("references", []),
                "columns": columns,
            })
        return tables

    def _chunk_documents(self, document_text: str) -> List[str]:
        """Split concatenated document text into bounded chunks on line edges."""
        text = (document_text or "").strip()
        if not text:
            return []
        if len(text) <= self._chunk_chars:
            return [text]
        chunks: List[str] = []
        buffer: list[str] = []
        size = 0
        for line in text.splitlines(keepends=True):
            if size + len(line) > self._chunk_chars and buffer:
                chunks.append("".join(buffer))
                buffer, size = [], 0
            buffer.append(line)
            size += len(line)
        if buffer:
            chunks.append("".join(buffer))
        return chunks[: self._max_rounds]

    async def _run_llm(self, system_prompt: str, user_payload: dict) -> Dict[str, List[dict]]:
        """One LLM round → parsed proposal (never raises; empty on failure)."""
        from litellm import completion  # pylint: disable=import-outside-toplevel

        from api.config import Config  # pylint: disable=import-outside-toplevel
        from api.graph import _completion_message_content  # pylint: disable=import-outside-toplevel

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        completion_kwargs = {
            "messages": messages,
            "temperature": 0,
            "max_tokens": self._max_tokens,
        }
        try:
            completion_result = await asyncio.to_thread(
                completion,
                **Config.completion_kwargs(**completion_kwargs),
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.warning("SchemaEnrichmentAgent: LLM call failed: %s", exc)
            return dict(_EMPTY_PROPOSAL)

        raw_response = _completion_message_content(completion_result)
        return _parse_enrichment_response(raw_response)

    async def propose(
        self,
        snapshot: Dict[str, Any],
        document_text: str,
        user_rules: str = "",
        knowledge: str = "",
    ) -> Dict[str, List[dict]]:
        """Produce a MERGED, RAW (not-yet-validated) enrichment proposal.

        Runs up to ``max_rounds`` bounded LLM passes over document chunks and
        merges them. Validation is a separate step (:func:`_validate_proposal`)
        the caller runs against the live snapshot.
        """
        schema_payload = self._snapshot_for_prompt(snapshot)
        chunks = self._chunk_documents(document_text)
        if not chunks:
            # No documents: still let the model propose links/keys from schema +
            # rules/knowledge alone, in a single round with empty document text.
            chunks = [""]

        proposals: List[Dict[str, List[dict]]] = []
        for round_index, chunk in enumerate(chunks):
            user_payload = {
                "live_schema": schema_payload,
                "documents": chunk,
                "user_rules": (user_rules or "")[:4000],
                "business_knowledge": (knowledge or "")[:4000],
                "round": round_index + 1,
                "total_rounds": len(chunks),
            }
            proposal = await self._run_llm(_SYSTEM_PROMPT, user_payload)
            proposals.append(proposal)

        return _merge_proposals(proposals)

    async def propose_validated(
        self,
        snapshot: Dict[str, Any],
        document_text: str,
        user_rules: str = "",
        knowledge: str = "",
    ) -> Dict[str, Any]:
        """Convenience: ``propose`` followed by the deterministic validation gate."""
        raw = await self.propose(snapshot, document_text, user_rules, knowledge)
        return _validate_proposal(raw, snapshot)
