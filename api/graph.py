"""Module to handle the graph data loading into the database."""

import ast
import asyncio
import hashlib
import json
import logging
import math
import os
import re
from itertools import combinations
from typing import Any, Dict, List

from litellm import completion
from pydantic import BaseModel

from api.config import Config
from api.core.db_resolver import resolve_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
# pylint: disable=broad-exception-caught

GENERIC_DEFAULT_USER_RULES = """
User Rules & Specifications

These are general, domain-agnostic principles. They tell the model to read the user's exact wording together with the schema's table/column names, descriptions, comments, declared relationships, and sample values, and to decide from those — not from hardcoded mappings. They contain no fixed table/column names. Any business term below is an illustrative cue for matching by description, never a hardcoded target.

Invariants (apply always):
Generate exactly one read-only SQL statement. Never reference a table, column, join path, code value, or NULL placeholder that the provided schema does not actually expose. Treat the full-column inventory of the candidate tables as the authoritative set of available columns: a column that appears there may be used even when it was trimmed from the compact schema view (note when you relied on it), but a column appearing nowhere may not be invented.

1. Schema metadata is the source of truth; descriptions outrank names. Infer business concepts from table/column names, descriptions, comments, declared relationships, and sample values — not from fixed mappings unless the user supplies one. When name, description, and sample values disagree, the description/comment defines business meaning; the (possibly English-looking) name and the (possibly noisy) sample values are auxiliary. Business meaning may be defined entirely in a non-English description, so do not require an English column name, and do not discard a description-matched column because its samples look messy.

2. Bind every requested element to a real schema column. Map each requested output, metric, filter, grouping key, or aggregation input onto the column whose name or description (in any language, including business phrases, acronyms, and synonyms) semantically denotes it; prefer columns flagged as strong/direct matches. If such a column exists, the request is answerable — produce the query rather than declaring it impossible, and never emit a NULL or placeholder where a real column supplies the value.

3. Select the source by best-fitting row grain and meaning. Choose the table whose row granularity and column descriptions can exactly produce every requested output, metric, count, filter, and grouping key. Prefer fact/detail sources for calculated counts/sums/averages/comparisons; use a precomputed/snapshot/summary indicator only when the user asks for that stored attribute or no finer source exists. Never substitute a measure from a different grain, and never count snapshot/as-of/summary rows as a proxy for underlying records, unless the user explicitly asked for that grain.

4. Preserve every user literal and bind it by value-kind. Keep every explicit metric and filter the user states; never silently drop a literal (account number, client id, agreement number, date, code, status, or an "all X" condition meaning every row must carry that value). Identify from the wording what KIND of value each literal is — a customer-facing reference/number, an internal surrogate id, or a classification/category code — and bind it to the column whose description denotes that same kind. Do not bind a user-facing reference or number to a surrogate-id column, nor to a higher-level classification/second-order code, unless the wording asks for that kind. A "number" the user gives is not the same as an internal ID. Attributes the user asks only to OUTPUT are not filters: add a WHERE on them only when the question states a condition for them.

5. Match literals correctly by type. Filter text/code/ISO columns case-insensitively (LOWER on both sides) unless exact case is semantically meaningful; never apply LOWER to numeric, date, timestamp, or boolean columns. For reference dimensions (country/currency/region and similar), filter by the code column, choosing the exact value from sample values or the user's wording; use a free-text name only when the user gave that exact name and no code column matches. Never invent a code, status, type, or name literal from business wording. Prefer a concrete literal the user supplied over comparing two reference keys; fall back to key/id equality only when no literal is available.

6. Choose the measure representation the user asked for. For balance/rest/amount/turnover/rate and similar, pick the column whose description best matches the requested metric, object, unit, currency, conversion basis, and grain. When candidates differ only by unit/currency/conversion basis (for example account-currency versus a converted/reporting/RUB-equivalent measure), use the basis the user's wording specifies; if it is unspecified and the alternatives are not interchangeable for the answer, ask one concise clarification rather than defaulting to a fixed basis. When a requested numeric aggregation lands on a column that holds the right field but is stored as text/code, cast to numeric where the dialect allows and note the assumption.

7. Join only through real relationships; count entities distinctly. Join via declared relationships and shared stable keys; never fabricate a tautological/unconditional join (ON 1=1, CROSS JOIN) or join sibling tables just because key names look alike, unless the user explicitly asks for a cartesian product. Build the query from the table(s) whose own columns and foreign keys actually encode the requested relationship, role, validity window, and outputs. Count business entities with COUNT(DISTINCT stable key) whenever joins, snapshots, history, versions, or relationship rows can duplicate them.

8. Resolve roles and specialized categories through the link table's role/code/type column. Use relationship/link/assignment tables mainly for joins, roles, validity periods, and linked-row attributes; use their numeric measures only when the question asks for linked rows/roles/validity or no closer object-grain measure exists. For a named specialized category inside a contract/deal relationship (for example loan/settlement/collateral accounts, "ссудные"/"расчётные" and similar), pick the role value whose sample values or description match the category — use it directly if exactly one matches, and ask only when several plausibly match. Resolve deal-"leg" wording (первая/вторая нога) by the attribute whose DESCRIPTION denotes that leg (the leg identifier, the leg execution date), never by name pattern and never by mapping it to contract/agreement-level identifiers.

9. Pick the right date concept, and require a reporting slice where the schema implies one. For unqualified "on/as of/for date" filters and reporting slices, use report/as-of/snapshot/balance date columns by description; use lifecycle/event dates only when the wording asks for open/close/register/execute/trade/start/end/validity events. For perioded sources (fact, snapshot, balance/rest, report/as-of, status or assignment history) a reporting date is mandatory: apply a user-given date to the matching reporting/as-of column and keep it consistent across joined perioded tables. If the user gives no date and does not ask for current/today, ask one concise clarification — do not aggregate across all dates or silently pick the latest.

10. "Current reporting date" means the dialect's current-date expression, not MAX(date). "Текущая отчётная дата / текущий отчётный день / на сегодня / current reporting date" resolves to the target dialect's current-date expression. Snapshots may hold future-dated or historical slices, so MAX(reporting date) is not "current". Use the maximum available reporting date only when the user explicitly asks for the latest/maximum available date, or for a specific named object's own latest slice.

11. Comparison-over-time mechanics come from the schema's keys and time columns. For change/delta/dynamics versus a previous period over snapshot/as-of/report-dated sources: unless the user names a different grain, "previous period" is the previous available reporting/as-of date for the same business key. Compute the prior value with LAG/LEAD over the reporting/as-of (or effective/event) date per business key BEFORE filtering to the requested date, then restrict to the requested date in an outer query so the window can see earlier rows. Switch to endpoint comparison (max-min, first-last, start-end) only when the user's wording explicitly calls for endpoints.

12. Pin the snapshot date on every joined alias. When a period-dated source is self-joined, or several period-dated sources are joined for an "as-of date D" question, equate the snapshot/report date on EVERY alias to the same single value (a literal date or the dialect current-date) in addition to the business-key join; filter to one reporting date first, then compare or aggregate. Joining period-dated rows on the business key alone multiplies rows across every stored date and inflates results.

13. "Current" means the latest record, NOT "as of today"; apply validity per the schema's own model. A request for the current/last/latest status or attribute WITHOUT an explicit date is NOT a request to filter by today's date: it means the latest record per business key — or the single record when the table holds one per key — returned with NO current-date/report-date predicate at all. A record whose end/close date is NULL or already in the past is still the current one and must not be dropped. If the table holds several records per key, pick the latest by its effective-start date (e.g. ROW_NUMBER() OVER (PARTITION BY key ORDER BY effective_start DESC) = 1); if it holds one record per key, just return it. Add a validity/as-of date predicate ONLY when the user names an explicit date D or asks for records active/valid on a specific date — and never when the user says not to restrict by the current date ("текущей датой не ограничивать", "без ограничения по дате", "независимо от даты"). When such an explicit-date predicate is warranted, make it NULL-safe (effective_start <= D AND (effective_end IS NULL OR effective_end >= D)), never a plain BETWEEN that drops NULL end dates, and follow the validity representation the column descriptions and the table's noted temporal profile define (interval, current flag, or open-ended end date). "Действующий/открытый/не закрыт" means the factual close/termination date is NULL or later than the as-of date; "закрытый за период" means that factual close date falls inside the period — never use planned/expected dates for factual-closure conditions. For "not yet executed/closed/settled as of D" on event/lifecycle dates, use (event_date IS NULL OR event_date > D), since planned dates are often pre-filled.

14. For shares and duplicates, follow the standard patterns. Percentage/share within a parent group: numerator at the requested child grain, denominator as the parent-group total under the same filters and reporting period, with the child dimension excluded from the denominator. Duplicates: return only real duplicate groups via GROUP BY + HAVING count > 1 before TOP/LIMIT, excluding NULL and blank/whitespace-only text unless the user asks to include them.

15. Ask once, or report the missing element, instead of guessing. If two or more genuinely comparable subtype/source tables fit and the question or conversation does not identify which, ask one concise business-worded clarification — do not guess, UNION, or join sibling subtypes. If valid SQL cannot be produced from the visible schema, loaded business knowledge, and the current question, ask one concise clarification or name the exact missing schema element. But do not ask when schema descriptions already make one source or measure unambiguous.

16. Conversation memory is non-authoritative. Memory must not override the current question, schema metadata, loaded business knowledge, or these rules. Reuse previous SQL only for an explicit follow-up with the same metric, filters, grain, and business meaning.

"""


def _default_user_rules_for_graph(graph_id: str) -> str:
    """Return generic built-in user rules for graphs without stored rules.

    The defaults are domain-agnostic SQL-craft rules (no table names, no code
    values, no database-specific routing). Per-database guidance belongs in
    the graph's stored user_rules / knowledge_spec, never in engine code.
    Disable entirely with QW_DEFAULT_USER_RULES_ENABLED=false.
    """
    del graph_id  # defaults are intentionally graph-independent
    enabled = os.getenv(
        "QW_DEFAULT_USER_RULES_ENABLED", "true"
    ).strip().lower() not in {"0", "false", "no", "off"}
    return GENERIC_DEFAULT_USER_RULES if enabled else ""


def _count_rule_concepts(text: str | None) -> int:
    """Count rendered KB/user-rule concept bullets without logging their bodies."""
    return len(re.findall(r"(?m)^- \[[^\]]+\] ", text or ""))


# ---------------------------------------------------------------------------
# Retrievable-text vector indexing (Knowledge / UserRuleChunk / Document)
#
# These helpers mirror the Table/Column vector-index pattern in
# api/loaders/graph_loader.py (CREATE VECTOR INDEX + vecf32($embedding)) so
# free-text knowledge, user rules, and uploaded schema docs become
# vector-retrievable nodes inside the SAME per-DB graph. Everything here is
# defensive: an embedding-endpoint outage logs and returns without ever
# leaving the DB graph in a broken state.
# ---------------------------------------------------------------------------

# Labels whose nodes carry a free-text ``content`` + ``embedding`` for retrieval.
RETRIEVABLE_TEXT_LABELS = ("Knowledge", "UserRuleChunk", "Document")


def _chunk_text(text: str, max_chars: int = 1200) -> List[str]:
    """Split free text into bounded, paragraph-aware chunks for embedding.

    Splits on blank lines first (paragraph boundaries), then packs paragraphs
    up to ``max_chars``; an oversized single paragraph is hard-split on line
    edges, and a newline-free line longer than ``max_chars`` is sliced on a
    character boundary so every emitted chunk is bounded. Mirrors the simple,
    deterministic chunker used by the enrichment agent
    (``SchemaEnrichmentAgent._chunk_documents``).
    """
    text = (text or "").strip()
    if not text:
        return []
    try:
        max_chars = max(200, int(os.getenv("KNOWLEDGE_CHUNK_CHARS", str(max_chars))))
    except (TypeError, ValueError):
        max_chars = 1200

    # Header-aware: a structured KB (markdown "## Concept" sections) embeds best as
    # ONE node PER concept, so vector retrieval returns whole single concepts instead
    # of blobs mixing several definitions (the dilution source). Split on top-level
    # "#"-headers when the document is clearly section-structured; size-bound any
    # oversized section. General — any #-structured doc; unstructured text falls
    # through to the paragraph packer below.
    if len(re.findall(r"(?m)^\#{1,6}\s+\S", text)) >= 2:
        sections: List[str] = []
        cur: List[str] = []
        for line in text.splitlines():
            if re.match(r"^\#{1,6}\s+\S", line) and cur:
                sections.append("\n".join(cur).strip())
                cur = [line]
            else:
                cur.append(line)
        if cur:
            sections.append("\n".join(cur).strip())
        out: List[str] = []
        for sec in sections:
            sec = sec.strip()
            while len(sec) > max_chars:
                out.append(sec[:max_chars].strip())
                sec = sec[max_chars:]
            if sec:
                out.append(sec)
        if out:
            return out

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: List[str] = []
    buffer = ""
    for para in paragraphs:
        if len(para) > max_chars:
            # Flush whatever is buffered, then hard-split the big paragraph.
            if buffer:
                chunks.append(buffer)
                buffer = ""
            line_buffer = ""
            for line in para.splitlines(keepends=True):
                # A single line longer than max_chars (e.g. minified text with no
                # newlines) is sliced on character boundaries so no chunk exceeds
                # the embedding-friendly size.
                while len(line) > max_chars:
                    if line_buffer.strip():
                        chunks.append(line_buffer.strip())
                        line_buffer = ""
                    chunks.append(line[:max_chars].strip())
                    line = line[max_chars:]
                if len(line_buffer) + len(line) > max_chars and line_buffer:
                    chunks.append(line_buffer.strip())
                    line_buffer = ""
                line_buffer += line
            if line_buffer.strip():
                chunks.append(line_buffer.strip())
            continue
        if buffer and len(buffer) + 2 + len(para) > max_chars:
            chunks.append(buffer)
            buffer = para
        else:
            buffer = f"{buffer}\n\n{para}" if buffer else para
    if buffer:
        chunks.append(buffer)
    return [c for c in chunks if c.strip()]


async def _ensure_text_vector_index(graph, label: str) -> bool:
    """Create the vector index for ``(:label.embedding)`` if missing.

    Returns True when an index is present (created now or already existing),
    False when it could not be created (e.g. embedding model unavailable). Uses
    the same OPTIONS shape as the Table/Column indexes in graph_loader.py.
    """
    try:
        vec_len = Config.EMBEDDING_MODEL.get_vector_size()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning(
            "Skipping %s vector index: embedding model unavailable: %s",
            label, str(exc)[:200],
        )
        return False
    try:
        await graph.query(
            f"""
            CREATE VECTOR INDEX FOR (n:{label}) ON (n.embedding)
            OPTIONS {{dimension:$size, similarityFunction:'euclidean'}}
            """,
            {"size": vec_len},
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Index already exists (idempotent) or transient error — non-fatal.
        logging.debug("Vector index create for %s noop/failed: %s", label, str(exc)[:200])
    return True


async def index_text_chunks(
    graph_id: str,
    label: str,
    text: str,
    source: str,
    *,
    replace_source: bool = False,
    db=None,
) -> int:
    """Chunk + embed *text* and store it as retrievable ``(:label)`` nodes.

    Additive and idempotent: each chunk is keyed by a stable content hash via
    ``MERGE``, so re-indexing identical text never duplicates nodes. When
    ``replace_source`` is True, existing nodes of this label+source are deleted
    first (used for knowledge/user-rules that are kept in sync with their blob);
    when False, chunks only accumulate (used for uploaded documents).

    Never raises: an embedding/DB failure logs and returns 0 so the caller's
    primary write (the blob) is never aborted and the graph is never broken.

    Returns the number of chunks written.
    """
    if label not in RETRIEVABLE_TEXT_LABELS:
        raise ValueError(f"Unsupported retrievable text label: {label}")
    try:
        graph = resolve_db(db).select_graph(graph_id)

        if replace_source:
            try:
                await graph.query(
                    f"MATCH (n:{label} {{source: $source}}) DETACH DELETE n",
                    {"source": source},
                )
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning(
                    "Could not clear old %s nodes (source=%s): %s",
                    label, source, str(exc)[:200],
                )

        chunks = _chunk_text(text)
        if not chunks:
            return 0

        if not await _ensure_text_vector_index(graph, label):
            return 0

        written = 0
        for position, chunk in enumerate(chunks):
            try:
                embedding = Config.EMBEDDING_MODEL.embed(chunk)[0]
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning(
                    "Embedding failed for %s chunk %d (source=%s); skipping: %s",
                    label, position, source, str(exc)[:200],
                )
                continue
            chunk_hash = hashlib.sha1(
                f"{source}\x00{chunk}".encode("utf-8")
            ).hexdigest()
            try:
                await graph.query(
                    f"""
                    MERGE (n:{label} {{hash: $hash}})
                    SET n.content = $content,
                        n.source = $source,
                        n.position = $position,
                        n.embedding = vecf32($embedding)
                    """,
                    {
                        "hash": chunk_hash,
                        "content": chunk,
                        "source": source,
                        "position": position,
                        "embedding": embedding,
                    },
                )
                written += 1
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.warning(
                    "Storing %s chunk %d failed (source=%s); skipping: %s",
                    label, position, source, str(exc)[:200],
                )
        logging.info(
            "Indexed retrievable text: graph=%s label=%s source=%s chunks=%d",
            graph_id, label, source, written,
        )
        return written
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Absolute backstop: never let retrieval-indexing break the DB graph.
        logging.warning(
            "index_text_chunks failed (graph=%s label=%s source=%s): %s",
            graph_id, label, source, str(exc)[:200],
        )
        return 0


async def _find_text_chunks(
    graph, label: str, embedding: List[float], top_k: int,
) -> List[str]:
    """Vector-retrieve up to ``top_k`` ``(:label)`` chunk contents for one query.

    Returns an empty list when the index/label does not exist yet (e.g. a graph
    indexed before this feature, or no knowledge/docs loaded) — never raises.
    """
    try:
        result = await graph.query(
            f"""
            CALL db.idx.vector.queryNodes('{label}','embedding',$top_k,vecf32($embedding))
            YIELD node, score
            RETURN node.content, score
            ORDER BY score ASC
            """,
            {"top_k": top_k, "embedding": embedding},
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.debug("%s vector retrieval skipped: %s", label, str(exc)[:200])
        return []
    contents: List[str] = []
    for row in result.result_set or []:
        if row and row[0]:
            contents.append(str(row[0]))
    return contents


async def retrieve_indexed_context(
    graph_id: str,
    user_query: str,
    *,
    labels: tuple = ("Document", "Knowledge"),
    top_k: int = 4,
    db=None,
) -> str:
    """Embed *user_query* and vector-retrieve relevant indexed text chunks.

    Pulls top-K chunks from the requested retrievable labels (uploaded
    documents and/or appended knowledge) in this DB graph and returns them as a
    single formatted block ready to append to prompt context. Fully
    failure-tolerant: a missing embedding endpoint, a graph without these
    indexes, or any query error yields an empty string so the caller's existing
    behaviour is unchanged.
    """
    user_query = (user_query or "").strip()
    if not user_query:
        return ""
    try:
        embedding = Config.EMBEDDING_MODEL.embed(user_query)[0]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.debug("retrieve_indexed_context: embedding unavailable: %s", str(exc)[:200])
        return ""

    try:
        graph = resolve_db(db).select_graph(graph_id)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.debug("retrieve_indexed_context: graph unavailable: %s", str(exc)[:200])
        return ""

    label_titles = {
        "Document": "From uploaded schema documents",
        "Knowledge": "From stored business knowledge",
        "UserRuleChunk": "From stored user rules",
    }
    sections: List[str] = []
    seen: set[str] = set()
    for label in labels:
        chunks = await _find_text_chunks(graph, label, embedding, top_k)
        unique_chunks = []
        for chunk in chunks:
            key = chunk.strip()
            if key and key not in seen:
                seen.add(key)
                unique_chunks.append(chunk.strip())
        if unique_chunks:
            title = label_titles.get(label, label)
            body = "\n\n".join(f"- {c}" for c in unique_chunks)
            sections.append(f"{title}:\n{body}")
    if not sections:
        return ""
    return "\n\n".join(sections)


async def retrieve_concept_chunks(
    graph_id: str, user_query: str, *, top_k: int = 5, db=None,
) -> List[str]:
    """Embedding-KNN over the per-concept :Knowledge nodes → top_k whole concept
    chunks (raw text, each ``## Title`` + body). Lets the resolver/linker receive
    only the few concepts SEMANTICALLY closest to the question (precise, minimal),
    instead of token-matching that over-recalls every look-alike. Failure-tolerant:
    returns [] if embeddings/graph/index are unavailable (caller falls back)."""
    user_query = (user_query or "").strip()
    if not user_query:
        return []
    try:
        embedding = Config.EMBEDDING_MODEL.embed(user_query)[0]
        graph = resolve_db(db).select_graph(graph_id)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.debug("retrieve_concept_chunks: unavailable: %s", str(exc)[:200])
        return []
    try:
        return await _find_text_chunks(graph, "Knowledge", embedding, top_k)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.debug("retrieve_concept_chunks: query failed: %s", str(exc)[:200])
        return []


async def get_document_sources(graph_id: str, db=None) -> dict:
    """Read every uploaded ``:Document`` chunk's content grouped by its source,
    so a re-index can re-embed them after the graph is dropped + re-pulled.

    Returns ``{source: joined_content}`` (chunks of one source joined in
    position order). Never raises — a missing label / graph yields ``{}``.
    """
    try:
        graph = resolve_db(db).select_graph(graph_id)
        result = await graph.query(
            "MATCH (d:Document) RETURN d.source, d.content, d.position "
            "ORDER BY d.source, d.position"
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.debug("get_document_sources skipped: %s", str(exc)[:200])
        return {}
    by_source: dict[str, list[str]] = {}
    for row in (result.result_set or []):
        if not row:
            continue
        source = str(row[0] or "uploaded")
        content = str(row[1] or "")
        if content:
            by_source.setdefault(source, []).append(content)
    return {src: "\n\n".join(chunks) for src, chunks in by_source.items()}


async def delete_document_source(graph_id: str, source: str, db=None) -> int:
    """Delete all uploaded ``:Document`` chunks for one source (filename) from
    the graph. Returns the number of chunks removed. Never raises."""
    if not source:
        return 0
    try:
        graph = resolve_db(db).select_graph(graph_id)
        res = await graph.query(
            "MATCH (d:Document {source: $s}) RETURN count(d)", {"s": source})
        count = int(res.result_set[0][0]) if (res.result_set and res.result_set[0]) else 0
        if count:
            await graph.query(
                "MATCH (d:Document {source: $s}) DETACH DELETE d", {"s": source})
        return count
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("delete_document_source(%s) failed: %s", source, str(exc)[:200])
        return 0


async def graph_exists(graph_id: str, db=None) -> bool:
    """True if a graph by this name exists (via GRAPH.LIST). Never raises."""
    try:
        names = await resolve_db(db).connection.execute_command("GRAPH.LIST")
        decoded = [
            n.decode() if isinstance(n, (bytes, bytearray)) else str(n)
            for n in (names or [])
        ]
        return graph_id in decoded
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.debug("graph_exists(%s) failed: %s", graph_id, str(exc)[:200])
        return False


async def drop_graph(graph_id: str, db=None) -> None:
    """GRAPH.DELETE a graph; tolerant if it does not exist. Never raises."""
    try:
        await resolve_db(db).connection.execute_command("GRAPH.DELETE", graph_id)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.debug("drop_graph(%s) skipped: %s", graph_id, str(exc)[:200])


async def copy_graph(src: str, dest: str, db=None) -> bool:
    """GRAPH.COPY ``src`` -> ``dest`` (dest is dropped first so the copy never
    collides). Returns True on success. Used for the re-index backup/rollback
    so an interrupted rebuild can restore the previous graph."""
    try:
        conn = resolve_db(db).connection
        try:
            await conn.execute_command("GRAPH.DELETE", dest)
        except Exception:  # pylint: disable=broad-exception-caught
            pass
        await conn.execute_command("GRAPH.COPY", src, dest)
        return True
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("copy_graph %s -> %s failed: %s", src, dest, str(exc)[:200])
        return False


_FK_ANNOTATION_RE = re.compile(
    r"FK→\s+(?P<table>[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?)\((?P<column>[^)]+)\)"
)
_TOKEN_RE = re.compile(r"[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё0-9_]{2,}")
_KNOWN_VALUES_RE = re.compile(r"\s*Known values in data:.*$", re.IGNORECASE)
_RANK_STOPWORDS = {
    "and", "the", "for", "with", "from", "where", "over", "under", "into",
    "this", "that", "their", "them", "show", "list", "find", "select",
    "order", "sort", "group", "groups", "each", "all", "top", "query",
    "table", "column", "date", "data", "value", "values",
    "для", "все", "всех", "его", "них", "ним", "при", "или", "где",
    "есть", "ли",
    "надо", "нужно", "найти", "найдите", "вывести", "выведите",
    "показать", "покажи", "отсортируйте", "сгруппируйте", "каждого",
    "каждой", "таких", "этим", "этих", "дате", "дату", "дата",
    "запрос", "таблица", "колонка", "значение", "значения",
}
_CYRILLIC_LATIN_MAP = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


class TableDescription(BaseModel):
    """Table Description"""

    name: str
    description: str


class ColumnDescription(BaseModel):
    """Column Description"""

    name: str
    description: str


class Descriptions(BaseModel):
    """List of tables"""

    tables_descriptions: list[TableDescription]
    columns_descriptions: list[ColumnDescription]


def _parse_descriptions_response(response: str) -> Dict[str, Any]:
    """Extract the descriptions JSON object from an LLM response."""
    if not response:
        raise ValueError("LLM returned an empty descriptions response")

    decoder = json.JSONDecoder()
    for index, char in enumerate(response):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(response[index:])
        except json.JSONDecodeError:
            continue
        if (
            isinstance(candidate, dict)
            and "tables_descriptions" in candidate
            and "columns_descriptions" in candidate
        ):
            return candidate

    markdown_descriptions = _parse_markdown_descriptions_response(response)
    if markdown_descriptions:
        return markdown_descriptions

    raise ValueError("LLM descriptions response did not contain a valid JSON object")


def _parse_table_rerank_response(response: str) -> list[str]:
    """Extract ranked table names from a reranker response."""
    if not response:
        raise ValueError("LLM returned an empty table-rerank response")

    decoder = json.JSONDecoder()
    candidates: list[Any] = []
    for index, char in enumerate(response):
        if char not in "[{":
            continue
        try:
            candidate, _ = decoder.raw_decode(response[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            for key in ("ranked_tables", "tables", "ranking"):
                value = candidate.get(key)
                if isinstance(value, list):
                    candidates = value
                    break
        elif isinstance(candidate, list):
            candidates = candidate
        if candidates:
            break

    names: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = str(
                item.get("name")
                or item.get("table")
                or item.get("table_name")
                or ""
            ).strip()
        else:
            name = ""
        if name and name not in names:
            names.append(name)

    if not names:
        raise ValueError("LLM table-rerank response did not contain table names")
    return names


def _completion_message_content(completion_result) -> str:
    """Return text content from LiteLLM/OpenAI-compatible completion response."""
    try:
        message = completion_result.choices[0].message
    except (AttributeError, IndexError, TypeError):
        return ""

    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part).strip()
    return (content or "").strip()


def _completion_finish_reason(completion_result) -> str:
    """Return finish_reason for logging without leaking prompt contents."""
    try:
        return str(getattr(completion_result.choices[0], "finish_reason", "") or "")
    except (AttributeError, IndexError, TypeError):
        return ""


def _completion_usage_summary(completion_result) -> str:
    """Return token usage metadata for diagnostics."""
    usage = getattr(completion_result, "usage", None)
    if usage is None:
        return "unavailable"
    if isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = (
            details.get("reasoning_tokens") if isinstance(details, dict) else None
        )
    else:
        prompt_tokens = getattr(usage, "prompt_tokens", None)
        completion_tokens = getattr(usage, "completion_tokens", None)
        total_tokens = getattr(usage, "total_tokens", None)
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
    parts = [
        f"prompt={prompt_tokens}",
        f"completion={completion_tokens}",
        f"total={total_tokens}",
    ]
    if reasoning_tokens is not None:
        parts.append(f"reasoning={reasoning_tokens}")
    return " ".join(parts)


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text or "")}


def _meaningful_tokens(text: str) -> set[str]:
    return {
        token for token in _tokens(text)
        if token not in _RANK_STOPWORDS and len(token) >= 3
    }


_CYRILLIC_SUFFIXES = (
    "иями", "ями", "ами", "ого", "ему", "ими", "ыми", "его", "ому",
    "иях", "ах", "ях", "ых", "их", "ый", "ий", "ой", "ая", "яя",
    "ое", "ее", "ам", "ям", "ом", "ем", "ов", "ев", "ей", "ой",
    "ым", "им", "ую", "юю", "а", "я", "ы", "и", "е", "о", "у",
    "ю",
)


def _has_cyrillic(token: str) -> bool:
    return any("а" <= char <= "я" or char == "ё" for char in token)


def _cyrillic_light_stem(token: str) -> str:
    """Return a conservative Russian stem without domain-specific vocabulary."""
    if not _has_cyrillic(token):
        return token
    if token.endswith("ок") and len(token) >= 6:
        return token[:-2] + "к"
    for suffix in _CYRILLIC_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 5:
            return token[: -len(suffix)]
    return token


def _lexical_max_terms(default: int = 64) -> int:
    raw = os.getenv("QW_LEXICAL_MAX_TERMS", "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(8, min(value, 256))


def _aggressive_cyrillic_stem(token: str) -> str:
    """Prefix stem that bridges Russian inflection (нога/ноги, первая/первой).

    Over-matching is safe downstream: IDF weighting flattens any stem that
    starts matching half the schema, so recall can be prioritized here.
    """
    if len(token) >= 6:
        return token[:5]
    if len(token) >= 4:
        return token[:3]
    return token


def _lexical_search_terms(text: str, max_terms: int | None = None) -> list[str]:
    """Build lexical terms for metadata fallback/hybrid search.

    Keeps tokens in order of appearance (the user's own wording first) instead
    of preferring long words: short domain words ("нога", "ставки") routinely
    carry the strongest signal, and a length sort used to push them past the
    cap while keeping generic filler ("необходимо", "значительное").
    """
    if max_terms is None:
        max_terms = _lexical_max_terms()
    # Two priority tiers: every token's own form + stems first, transliteration
    # last. Translit variants rarely match real schemas but used to eat the
    # term cap, pushing out distinctive words from the end of the question.
    primary: list[str] = []
    secondary: list[str] = []
    for token in _TOKEN_RE.findall((text or "").lower()):
        if len(token) < 3 or token in _RANK_STOPWORDS:
            continue
        has_cyrillic = _has_cyrillic(token)
        variants = [token]
        if has_cyrillic:
            stem = _cyrillic_light_stem(token)
            if stem != token:
                variants.append(stem)
            prefix_stem = _aggressive_cyrillic_stem(token)
            if prefix_stem not in variants:
                variants.append(prefix_stem)
            latinized = "".join(_CYRILLIC_LATIN_MAP.get(char, char) for char in token)
            if latinized and latinized != token:
                secondary.append(latinized)
        for variant in variants:
            primary.append(variant)
            if not has_cyrillic and len(variant) >= 7:
                primary.append(variant[:5])
            if not has_cyrillic and len(variant) >= 8:
                # Generic light stemming: enough to match common suffix variants
                # without maintaining a domain dictionary.
                primary.append(variant[:6])
            if variant.endswith("s") and len(variant) > 4:
                primary.append(variant[:-1])
            if variant.endswith("es") and len(variant) > 5:
                primary.append(variant[:-2])
            if variant.endswith("ing") and len(variant) > 6:
                primary.append(variant[:-3])
    ordered = primary + secondary
    return list(dict.fromkeys(term for term in ordered if len(term) >= 3))[:max_terms]


def _combined_lexical_search_terms(
    user_query: str,
    descriptions_text: List[str],
    max_terms: int | None = None,
) -> list[str]:
    """Prefer original user wording when combining with generated descriptions."""
    if max_terms is None:
        max_terms = _lexical_max_terms(96)
    user_terms = _lexical_search_terms(user_query or "", max_terms=max_terms)
    description_terms = _lexical_search_terms(
        " ".join(descriptions_text or []),
        max_terms=max_terms,
    )
    return list(dict.fromkeys(user_terms + description_terms))[:max_terms]


def _strip_known_values(value: object) -> str:
    """Ignore volatile runtime samples when ranking schema context."""
    return _KNOWN_VALUES_RE.sub("", str(value or "")).strip()


def _normalize_foreign_keys(foreign_keys: object) -> list[dict[str, Any]]:
    """Return FK metadata as a list regardless of graph/loader representation."""
    if not foreign_keys:
        return []
    if isinstance(foreign_keys, list):
        return [dict(item) for item in foreign_keys if isinstance(item, dict)]
    if isinstance(foreign_keys, dict):
        if {"column", "referenced_table", "referenced_column"} & set(foreign_keys):
            return [dict(foreign_keys)]
        return [dict(item) for item in foreign_keys.values() if isinstance(item, dict)]
    if isinstance(foreign_keys, str):
        text = foreign_keys.strip()
        if text.lower().startswith("foreign keys:"):
            text = text.split(":", 1)[1].strip()
        if not text:
            return []
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return []
        return _normalize_foreign_keys(parsed)
    return []


def _column_name(column: dict) -> str:
    return str(column.get("columnName") or column.get("name") or "").lower()


def _fk_column_names(foreign_keys: object) -> set[str]:
    return {
        str(fk_info.get("column") or "").lower()
        for fk_info in _normalize_foreign_keys(foreign_keys)
        if str(fk_info.get("column") or "").strip()
    }


def _is_key_column(column: dict, fk_columns: set[str]) -> bool:
    name = _column_name(column)
    key = str(column.get("keyType") or column.get("key_type") or column.get("key") or "").upper()
    return (
        name in fk_columns
        or key in {"PRI", "PK", "PRIMARY KEY", "FK", "FOREIGN KEY"}
        or name.endswith("_id")
        or name == "id"
    )


def _table_structure_adjustment(
    table_info: List[Any],
    relevance_tokens: set[str],
    column_scores: list[tuple[int, bool]],
) -> int:
    """Adjust ranking using graph structure, not domain-specific words.

    Tables whose columns are mostly keys/FKs are often relationship/snapshot
    carriers. They remain useful for joins, roles, and validity, but for broad
    object-row questions they should not outrank an attribute-rich table unless
    their non-key attributes directly match the request.
    """
    columns = [
        column for column in (table_info[3] or []) if isinstance(column, dict)
    ]
    if not columns:
        return 0

    table_name = str(table_info[0] or "")
    table_text = f"{table_name} {_strip_known_values(table_info[1])}".lower()
    table_tokens = _meaningful_tokens(table_text)
    fk_count = len(_normalize_foreign_keys(table_info[2] if len(table_info) > 2 else None))
    key_count = sum(1 for _score, is_key in column_scores if is_key)
    non_key_scores = [score for score, is_key in column_scores if not is_key]
    non_key_count = len(non_key_scores)
    key_ratio = key_count / max(1, len(columns))
    non_key_signal = sum(sorted(non_key_scores, reverse=True)[:4])
    table_signal = bool(relevance_tokens & table_tokens) or any(
        token in table_name.lower() for token in relevance_tokens
    )

    adjustment = 0
    if table_signal and non_key_count >= 4:
        adjustment += min(36, non_key_count * 3)
    weak_non_key_signal = non_key_signal < 4
    if key_ratio >= 0.40 and weak_non_key_signal:
        adjustment -= int(80 * key_ratio)
    if key_count >= 3 and weak_non_key_signal:
        adjustment -= min(30, key_count * 6)
    if fk_count >= 2 and non_key_count <= 4 and weak_non_key_signal:
        adjustment -= 35
    return adjustment


def _table_haystack_text(table_info: List[Any]) -> str:
    """Full lowercase search text of a table: name, description, columns."""
    if not isinstance(table_info, list) or not table_info:
        return ""
    parts = [str(table_info[0] or ""), _strip_known_values(table_info[1])]
    for column in (table_info[3] or []) if len(table_info) > 3 else []:
        if isinstance(column, dict):
            parts.append(_column_name(column))
            parts.append(_strip_known_values(column.get("description")))
    return " ".join(parts).lower()


def _token_idf_weights(
    combined_tables: List[List[Any]],
    relevance_tokens: set[str],
) -> dict[str, float]:
    """Inverse-document-frequency weight per token over the candidate pool.

    A token matching only one or two tables (e.g. a rare business phrase from
    the user question) is far more informative than one matching half the
    schema ("date", "amount"). Without this, verbose tables that match many
    common tokens drown out the single table holding the distinctive concept.
    Pure statistics over the schema at hand — no domain hardcode.
    """
    haystacks = [
        _table_haystack_text(table_info)
        for table_info in combined_tables or []
    ]
    haystacks = [text for text in haystacks if text]
    total = len(haystacks)
    if total < 2:
        return {}
    weights: dict[str, float] = {}
    for token in relevance_tokens:
        document_frequency = sum(1 for text in haystacks if token in text)
        if document_frequency <= 0:
            weights[token] = 1.0
            continue
        weights[token] = 1.0 + math.log(total / document_frequency)
    return weights


def _table_relevance_score(
    table_info: List[Any],
    relevance_tokens: set[str],
    direct_table_names: set[str] | None = None,
    boosted_table_names: set[str] | None = None,
    token_weights: dict[str, float] | None = None,
) -> int:
    if not isinstance(table_info, list) or len(table_info) < 4:
        return 0

    def _weight(token: str) -> float:
        if not token_weights:
            return 1.0
        return token_weights.get(token, 1.0)

    def _max_weight(tokens: set[str]) -> float:
        if not tokens:
            return 1.0
        return max(_weight(token) for token in tokens)

    table_name = str(table_info[0] or "")
    table_text = f"{table_name} {_strip_known_values(table_info[1])}".lower()
    table_tokens = _meaningful_tokens(table_text)
    overlap = relevance_tokens & table_tokens
    score = int(20 * _max_weight(overlap)) if overlap else 0
    name_hit = False
    for token in relevance_tokens:
        if token in table_name.lower():
            score += int(60 * _weight(token))
            name_hit = True
    if direct_table_names and table_name in direct_table_names:
        score += 80
    if boosted_table_names and table_name in boosted_table_names:
        score += 180
    if name_hit:
        score += 35

    fk_columns = _fk_column_names(table_info[2] if len(table_info) > 2 else None)
    column_scores: list[tuple[int, bool]] = []
    for column in table_info[3] or []:
        if not isinstance(column, dict):
            continue
        name = _column_name(column)
        description = _strip_known_values(column.get("description")).lower()
        haystack = f"{name} {description}"
        haystack_tokens = _meaningful_tokens(haystack)
        column_score = 0.0
        for token in relevance_tokens:
            if token in name:
                column_score += 10 * _weight(token)
            elif token in description:
                column_score += 5 * _weight(token)
            elif len(token) >= 5 and token[:4] in haystack:
                column_score += 4 * _weight(token)
        column_overlap = relevance_tokens & haystack_tokens
        if column_overlap:
            column_score += 12 * _max_weight(column_overlap)
        column_scores.append((int(column_score), _is_key_column(column, fk_columns)))

    score += sum(sorted((item[0] for item in column_scores), reverse=True)[:8])
    score += _table_structure_adjustment(table_info, relevance_tokens, column_scores)
    return score


def _compact_column_for_rerank(column: dict) -> dict[str, str]:
    return {
        "name": str(column.get("columnName") or column.get("name") or ""),
        "type": str(column.get("dataType") or column.get("type") or ""),
        "key_type": str(
            column.get("keyType") or column.get("key_type") or column.get("key") or ""
        ),
        "description": _strip_known_values(column.get("description"))[:220],
    }


def _column_overlap_score(column: dict, relevance_tokens: set[str]) -> int:
    if not relevance_tokens:
        return 0
    name = str(column.get("columnName") or column.get("name") or "").lower()
    description = _strip_known_values(column.get("description")).lower()
    text = f"{name} {description}"
    text_tokens = _meaningful_tokens(text)
    score = len(relevance_tokens & text_tokens) * 10
    for token in relevance_tokens:
        if token in name:
            score += 8
        elif token in description:
            score += 4
    return score


def _compact_table_for_rerank(
    table_info: List[Any],
    relevance_tokens: set[str],
    max_columns: int,
    boosted_table_names: set[str] | None = None,
) -> dict[str, Any]:
    columns = [
        (index, dict(column))
        for index, column in enumerate((table_info[3] or []) if len(table_info) >= 4 else [])
        if isinstance(column, dict)
    ]
    if len(columns) > max_columns:
        scored = sorted(
            (
                (_column_overlap_score(column, relevance_tokens), index, column)
                for index, column in columns
            ),
            key=lambda item: (-item[0], item[1]),
        )
        selected_indexes = {index for _, index, _ in scored[:max_columns]}
        columns = [
            (index, column)
            for index, column in columns
            if index in selected_indexes
        ]

    return {
        "name": str(table_info[0] or ""),
        "description": _strip_known_values(table_info[1])[:420],
        "foreign_keys": str(table_info[2] or "")[:700],
        "columns": [
            _compact_column_for_rerank(column)
            for _, column in columns
        ],
        "graph_anchor": str(table_info[0] or "") in (boosted_table_names or set()),
        "omitted_columns": max(
            0,
            len((table_info[3] or []) if len(table_info) >= 4 else []) - len(columns),
        ),
    }


def _rank_tables_for_context(
    combined_tables: List[List[Any]],
    user_query: str,
    descriptions_text: List[str],
    direct_table_names: set[str] | None = None,
    boosted_table_names: set[str] | None = None,
) -> List[List[Any]]:
    relevance_tokens = set(_combined_lexical_search_terms(user_query, descriptions_text))
    if not relevance_tokens:
        return combined_tables
    token_weights = _token_idf_weights(combined_tables, relevance_tokens)
    ranked = sorted(
        enumerate(combined_tables),
        key=lambda item: (
            -_table_relevance_score(
                item[1],
                relevance_tokens,
                direct_table_names,
                boosted_table_names,
                token_weights,
            ),
            item[0],
        ),
    )
    logging.info(
        "Lexical schema rank: input_tables=%d top_tables=%s",
        len(combined_tables),
        [
            item[1][0]
            for item in ranked[: min(12, len(ranked))]
            if isinstance(item[1], list) and item[1]
        ],
    )
    return [table_info for _, table_info in ranked]


def _table_name_anchor_matches(
    tables: List[List[Any]],
    user_query: str,
) -> set[str]:
    """Return tables whose names directly match user wording.

    This is a generic graph anchor: if the user says a concrete object token
    and that token appears in a table name after light normalization, the FK
    neighborhood around that table should outrank unrelated semantic matches.
    """
    user_terms = set(_lexical_search_terms(user_query or "", max_terms=96))
    if not user_terms:
        return set()
    matches: set[str] = set()

    def _table_identifier_terms(table_name: str) -> set[str]:
        terms: set[str] = set()
        for part in re.split(r"[^A-Za-zА-Яа-яЁё0-9]+", table_name.lower()):
            if not part or len(part) < 3:
                continue
            terms.add(part)
            if part.endswith("s") and len(part) > 4:
                terms.add(part[:-1])
            if part.endswith("es") and len(part) > 5:
                terms.add(part[:-2])
        return terms

    def _term_matches_identifier(term: str, identifier_terms: set[str]) -> bool:
        if term in identifier_terms:
            return True
        if len(term) < 5:
            return False
        return any(
            identifier.startswith(term) or term.startswith(identifier)
            for identifier in identifier_terms
            if len(identifier) >= 5
        )

    for table_info in tables or []:
        if not (isinstance(table_info, list) and table_info):
            continue
        table_name = str(table_info[0] or "").lower()
        identifier_terms = _table_identifier_terms(table_name)
        if any(_term_matches_identifier(term, identifier_terms) for term in user_terms):
            matches.add(table_info[0])
    return matches


def _trim_tables_for_context(
    combined_tables: List[List[Any]],
    context_max: int,
    user_query: str,
    descriptions_text: List[str],
    protected_table_names: set[str] | None = None,
    direct_table_names: set[str] | None = None,
    protected_table_priority: dict[str, int] | None = None,
    boosted_table_names: set[str] | None = None,
) -> List[List[Any]]:
    """Trim schema context while preserving relevant FK-expanded tables."""
    if len(combined_tables) <= context_max:
        return combined_tables

    protected_table_names = protected_table_names or set()
    direct_table_names = direct_table_names or set()
    protected_table_priority = protected_table_priority or {}
    boosted_table_names = boosted_table_names or set()
    relevance_tokens = set(_combined_lexical_search_terms(user_query, descriptions_text))
    token_weights = _token_idf_weights(combined_tables, relevance_tokens)
    selected = list(combined_tables[:context_max])
    selected_names = {
        table_info[0] for table_info in selected
        if isinstance(table_info, list) and table_info
    }
    protected_candidate_records = []
    for table_info in combined_tables[context_max:]:
        if not (
            isinstance(table_info, list)
            and table_info
            and table_info[0] in protected_table_names
        ):
            continue
        score = _table_relevance_score(
            table_info, relevance_tokens, token_weights=token_weights,
        )
        if table_info[0] in boosted_table_names:
            score += 180
        protected_candidate_records.append((
            -score,
            protected_table_priority.get(table_info[0], 100_000),
            table_info,
        ))
    protected_candidate_records.sort(key=lambda item: (item[0], item[1]))
    protected_candidates = [item[2] for item in protected_candidate_records]
    replaceable_start = max(0, context_max - max(1, context_max // 4))
    inserted_protected_names: set[str] = set()

    def _replace_tail_with(candidate: List[Any], reason: str) -> bool:
        replace_index = None
        replace_score = None
        is_fk_neighbor = reason == "FK-referenced"
        candidate_name = candidate[0]
        candidate_priority = protected_table_priority.get(candidate_name, 100_000)
        min_replace_index = (
            max(0, context_max - max(1, context_max // 2))
            if is_fk_neighbor else replaceable_start
        )
        for index in range(len(selected) - 1, -1, -1):
            if index < min_replace_index:
                continue
            table_info = selected[index]
            if not (isinstance(table_info, list) and table_info):
                continue
            if table_info[0] in boosted_table_names:
                continue
            if table_info[0] in inserted_protected_names:
                continue
            target_priority = protected_table_priority.get(table_info[0], 100_000)
            if table_info[0] in protected_table_names and target_priority <= candidate_priority:
                continue
            score = _table_relevance_score(
                table_info,
                relevance_tokens,
                None,
                boosted_table_names,
                token_weights,
            )
            if (
                table_info[0] in direct_table_names
                and not is_fk_neighbor
                and candidate_name not in boosted_table_names
            ):
                continue
            if replace_score is None or score < replace_score:
                replace_index = index
                replace_score = score
        if replace_index is None:
            return False
        removed = selected[replace_index]
        selected[replace_index] = candidate
        selected_names.discard(removed[0])
        selected_names.add(candidate_name)
        logging.info(
            "Table-finder preserved %s table in context: added=%s removed=%s",
            reason,
            candidate_name,
            removed[0],
        )
        return True

    if boosted_table_names:
        anchor_candidate_records = []
        for table_info in combined_tables:
            if not (
                isinstance(table_info, list)
                and table_info
                and table_info[0] in boosted_table_names
                and table_info[0] not in selected_names
            ):
                continue
            score = _table_relevance_score(
                table_info,
                relevance_tokens,
                direct_table_names,
                boosted_table_names,
                token_weights,
            )
            anchor_candidate_records.append((
                0 if table_info[0] in protected_table_names else 1,
                -score,
                protected_table_priority.get(table_info[0], 100_000),
                table_info,
            ))
        anchor_candidate_records.sort(key=lambda item: (item[0], item[1], item[2]))
        anchor_candidates = [item[3] for item in anchor_candidate_records]
        existing_anchor_count = sum(
            1
            for table_info in selected
            if isinstance(table_info, list)
            and table_info
            and table_info[0] in boosted_table_names
        )
        anchor_context_limit = max(1, context_max // 2)
        remaining_anchor_slots = max(0, anchor_context_limit - existing_anchor_count)
        if anchor_candidates and remaining_anchor_slots > 0:
            logging.info(
                "Table-finder graph-anchor candidates preserved before FK trim: "
                "slots=%d names=%s",
                remaining_anchor_slots,
                [table_info[0] for table_info in anchor_candidates[:remaining_anchor_slots]],
            )
        for candidate in anchor_candidates[:remaining_anchor_slots]:
            if candidate[0] in selected_names:
                continue
            _replace_tail_with(candidate, "graph-anchor")

    if protected_candidates:
        logging.info(
            "Table-finder protected FK-neighbor candidates: count=%d names=%s",
            len(protected_candidates),
            [table_info[0] for table_info in protected_candidates[:8]],
        )

        # Keep the strongest semantic matches stable, but reserve the tail of the
        # context for FK-neighbor tables that carry object attributes needed for
        # joins/projections. This avoids a noisy direct match list crowding out
        # referenced object tables.
        for candidate in protected_candidates[: max(1, context_max // 3)]:
            if candidate[0] in selected_names:
                continue
            if not _replace_tail_with(candidate, "FK-referenced"):
                continue
            inserted_protected_names.add(candidate[0])

    for candidate in combined_tables:
        if not (
            isinstance(candidate, list)
            and candidate
            and candidate[0] in boosted_table_names
            and candidate[0] not in selected_names
        ):
            continue
        if not _replace_tail_with(candidate, "graph-anchor"):
            continue

    return selected


async def _rerank_tables_with_llm(
    combined_tables: List[List[Any]],
    user_query: str,
    descriptions_text: List[str],
    previous_queries: list[str] | None = None,
    db_description: str | None = None,
    user_rules_spec: str | None = None,
    stage: str = "schema",
    direct_table_names: set[str] | None = None,
    boosted_table_names: set[str] | None = None,
) -> List[List[Any]]:
    """Ask the model to rank candidate tables by relevance to the original query."""
    boosted_table_names = boosted_table_names or set()
    preliminary = _rank_tables_for_context(
        combined_tables,
        user_query,
        descriptions_text,
        direct_table_names,
        boosted_table_names,
    )
    if (
        not getattr(Config, "TABLE_RERANK_ENABLED", True)
        or len(preliminary) <= 1
    ):
        return preliminary

    relevance_tokens = set(_combined_lexical_search_terms(user_query, descriptions_text))
    candidate_limit = int(getattr(Config, "TABLE_RERANK_MAX_CANDIDATES", 32))
    column_limit = int(getattr(Config, "TABLE_RERANK_MAX_COLUMNS_PER_TABLE", 24))
    candidates = preliminary[:candidate_limit]
    overflow = preliminary[candidate_limit:]
    candidate_payload = [
        _compact_table_for_rerank(
            table_info,
            relevance_tokens,
            column_limit,
            boosted_table_names,
        )
        for table_info in candidates
    ]
    if not candidate_payload:
        return preliminary

    system_prompt = """
You are a database schema reranking agent for text-to-SQL.

Rank only the candidate tables supplied by the user message. Use the original
user query, schema-search descriptions, table descriptions, column comments,
keys, and foreign-key notes. Prefer the tables whose grain and columns best
support the requested outputs, filters, metrics, grouping, ordering, and joins.
Tables marked graph_anchor=true were matched as concrete graph/table-name
anchors from the user wording or their FK neighborhood. Rank graph_anchor tables
ahead of generic semantic matches when they can support the requested source,
metric, relationship, or join path; use generic matches only when schema
metadata proves the anchor tables cannot answer that part of the question.
Do not invent table or column names. Do not write SQL.

Return only valid JSON:
{"ranked_tables":[{"name":"schema.table","score":0-100,"reason":"short"}]}
Include every supplied candidate table name exactly once, ordered most relevant
first.
"""
    user_payload = {
        "user_query": user_query,
        "previous_user_queries": previous_queries or [],
        "database_description": _strip_known_values(db_description)[:1500],
        "schema_search_descriptions": descriptions_text,
        "user_rules": (user_rules_spec or "")[:2500],
        "direct_graph_anchor_tables": sorted(boosted_table_names),
        "candidate_tables": candidate_payload,
    }
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
    ]

    max_attempts = max(1, int(getattr(Config, "TABLE_RERANK_MAX_ATTEMPTS", 2)))
    valid_names = {table_info[0]: table_info for table_info in candidates}
    lower_to_name = {str(name).lower(): name for name in valid_names}
    last_error: Exception | None = None
    for attempt_index in range(max_attempts):
        completion_kwargs = {
            "messages": messages,
            "temperature": 0,
            "max_tokens": int(getattr(Config, "TABLE_RERANK_MAX_TOKENS", 1600)),
            "extra_body": Config.reasoning_extra_body(
                getattr(Config, "TABLE_RERANK_REASONING", None)
            ),
        }
        completion_result = await asyncio.to_thread(
            completion,
            **Config.completion_kwargs(**completion_kwargs),
        )
        raw_response = _completion_message_content(completion_result)
        try:
            ranked_names = _parse_table_rerank_response(raw_response)
        except ValueError as exc:
            last_error = exc
            logging.warning(
                "Table-rerank attempt unusable: stage=%s attempt=%d/%d "
                "finish_reason=%s usage=%s content_chars=%d error=%s preview=%s",
                stage,
                attempt_index + 1,
                max_attempts,
                _completion_finish_reason(completion_result),
                _completion_usage_summary(completion_result),
                len(raw_response),
                exc,
                raw_response[:300],
            )
            continue

        ordered: list[List[Any]] = []
        used: set[str] = set()
        for name in ranked_names:
            canonical = lower_to_name.get(name.lower())
            if not canonical or canonical in used:
                continue
            ordered.append(valid_names[canonical])
            used.add(canonical)
        for table_info in candidates:
            if table_info[0] not in used:
                ordered.append(table_info)
                used.add(table_info[0])
        if boosted_table_names:
            anchor_ordered = [
                table_info for table_info in ordered
                if isinstance(table_info, list)
                and table_info
                and table_info[0] in boosted_table_names
            ]
            non_anchor_ordered = [
                table_info for table_info in ordered
                if not (
                    isinstance(table_info, list)
                    and table_info
                    and table_info[0] in boosted_table_names
                )
            ]
            if anchor_ordered and non_anchor_ordered:
                ordered = anchor_ordered + non_anchor_ordered
        ordered.extend(overflow)
        logging.info(
            "Table-rerank result: stage=%s candidates=%d usage=%s top_tables=%s",
            stage,
            len(candidates),
            _completion_usage_summary(completion_result),
            [table_info[0] for table_info in ordered[:12]],
        )
        return ordered

    logging.warning(
        "Table-rerank exhausted attempts; using lexical order: stage=%s attempts=%d last_error=%s",
        stage,
        max_attempts,
        last_error,
    )
    return preliminary


def _fallback_descriptions_from_query(
    user_query: str,
    previous_queries: list[str] | None = None,
) -> Dict[str, Any]:
    """Build generic schema-search descriptions from the user wording.

    This keeps the pipeline alive when a provider returns an empty or malformed
    table-finder response. The descriptions are intentionally domain-neutral:
    vector search still decides the actual tables and columns from the graph.
    """
    history = " ".join(previous_queries or [])
    combined = " ".join(part for part in [history, user_query] if part).strip()
    if not combined:
        combined = "database query"
    return {
        "tables_descriptions": [
            {
                "name": "requested_entities",
                "description": combined,
            }
        ],
        "columns_descriptions": [
            {
                "name": "requested_attributes",
                "description": combined,
            }
        ],
    }


def _parse_markdown_descriptions_response(response: str) -> Dict[str, Any] | None:
    """Parse common Markdown fallback output for table/column descriptions.

    Some OpenAI-compatible gateways ignore ``response_format`` and return a
    bulleted Markdown answer. Keeping those descriptions is materially better
    than falling back to the raw user query, because the descriptions normalize
    terse or multilingual user wording before vector search.
    """
    sections = {"tables": [], "columns": []}
    current = None
    item_re = re.compile(
        r"^\s*(?:[-*]|\d+[.)])\s*(?:\*\*)?(?P<name>[^:*—–-]+)"
        r"(?:\*\*)?\s*(?:[:—–-]\s*)?(?P<desc>.*)$"
    )
    table_heading_re = re.compile(r"\btable[s]?\b.*\bdescription[s]?\b")
    column_heading_re = re.compile(r"\bcolumn[s]?\b.*\bdescription[s]?\b")

    for raw_line in response.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        normalized = re.sub(r"[*_`#]", "", line).strip().lower()
        if table_heading_re.search(normalized):
            current = "tables"
            continue
        if column_heading_re.search(normalized):
            current = "columns"
            continue
        if current not in sections:
            continue

        match = item_re.match(line)
        if not match:
            continue
        name = re.sub(r"[*_`]", "", match.group("name")).strip()
        desc = re.sub(r"[*_`]", "", match.group("desc")).strip()
        if not desc:
            desc = name
        if name and desc:
            sections[current].append({"name": name, "description": desc})

    if not sections["tables"] and not sections["columns"]:
        return None

    return {
        "tables_descriptions": sections["tables"][:5],
        "columns_descriptions": sections["columns"][:5],
    }


async def get_db_description(graph_id: str, db=None) -> tuple[str, str]:
    """Get the database description from the graph."""
    graph = resolve_db(db).select_graph(graph_id)
    query_result = await graph.query(
        """
        MATCH (d:Database)
        RETURN d.description, d.url
        """
    )

    if not query_result.result_set:
        return ("No description available for this database.",
                "No URL available for this database.")

    return (query_result.result_set[0][0],
            query_result.result_set[0][1])  # Return the first result's description


async def get_user_rules(graph_id: str, db=None) -> str:
    """Get the user rules from the graph."""
    graph = resolve_db(db).select_graph(graph_id)
    query_result = await graph.query(
        """
        MATCH (r:BusinessRules {kind: 'user_rules'})
        RETURN r.content
        """
    )
    if query_result.result_set and query_result.result_set[0][0]:
        user_rules = query_result.result_set[0][0]
        logging.info(
            "User rules retrieved: graph=%s chars=%d concepts=%d",
            graph_id,
            len(user_rules),
            _count_rule_concepts(user_rules),
        )
        return user_rules

    query_result = await graph.query(
        """
        MATCH (d:Database)
        RETURN d.user_rules
        """
    )

    if not query_result.result_set or not query_result.result_set[0][0]:
        default_rules = _default_user_rules_for_graph(graph_id)
        logging.info(
            "User rules retrieved: graph=%s chars=%d concepts=%d source=%s",
            graph_id,
            len(default_rules),
            _count_rule_concepts(default_rules),
            "default" if default_rules else "empty",
        )
        return default_rules

    user_rules = query_result.result_set[0][0]
    logging.info(
        "User rules retrieved: graph=%s chars=%d concepts=%d",
        graph_id,
        len(user_rules),
        _count_rule_concepts(user_rules),
    )
    return user_rules


def _merge_knowledge_text(existing: str, incoming: str) -> str:
    """Append *incoming* knowledge to *existing*, skipping exact duplicates.

    Knowledge is additive (R1): a new "Load Knowledge" merges with what is
    already stored rather than replacing it. To stay idempotent, an incoming
    block that is already present verbatim (or whose every concept bullet is
    already present) is not appended again.
    """
    existing = (existing or "").strip()
    incoming = (incoming or "").strip()
    if not incoming:
        return existing
    if not existing:
        return incoming
    if incoming in existing:
        return existing

    # If every concept bullet in the incoming text already appears in the
    # existing blob, treat it as a no-op re-load (idempotent).
    incoming_concepts = re.findall(r"(?m)^- \[[^\]]+\] .+$", incoming)
    if incoming_concepts and all(
        re.sub(r"^- \[[^\]]+\] ", "", c) in existing for c in incoming_concepts
    ):
        return existing

    return f"{existing}\n\n{incoming}"


async def set_user_rules(graph_id: str, user_rules: str, db=None) -> None:
    """Set the user rules in the graph.

    Keeps the existing single ``BusinessRules{kind:'user_rules'}`` blob
    (back-compat, replace semantics) AND additionally chunks + embeds the rules
    into retrievable ``(:UserRuleChunk {content, embedding})`` nodes with a
    vector index, scoped to this DB graph (R2). The embedded copy is kept in
    sync with the blob by replacing this source's chunks on each write.
    """
    graph = resolve_db(db).select_graph(graph_id)
    await graph.query(
        """
        MERGE (d:Database)
        SET d.user_rules = $user_rules
        """,
        {"user_rules": user_rules}
    )
    await graph.query(
        """
        MERGE (r:BusinessRules {kind: 'user_rules'})
        SET r.name = '__business_rules_user_rules__',
            r.content = $user_rules,
            r.description = 'User-provided business rules for this database graph'
        """,
        {"user_rules": user_rules}
    )
    # Additive vector-indexed copy for retrieval (failure-tolerant).
    chunk_count = await index_text_chunks(
        graph_id, "UserRuleChunk", user_rules or "", "user_rules",
        replace_source=True, db=db,
    )
    logging.info(
        "User rules stored: graph=%s chars=%d concepts=%d indexed_chunks=%d",
        graph_id,
        len(user_rules or ""),
        _count_rule_concepts(user_rules),
        chunk_count,
    )


async def get_knowledge(graph_id: str, db=None) -> str:
    """Get database-specific knowledge from the graph."""
    graph = resolve_db(db).select_graph(graph_id)
    query_result = await graph.query(
        """
        MATCH (r:BusinessRules {kind: 'knowledge_spec'})
        RETURN r.content
        """
    )
    if query_result.result_set and query_result.result_set[0][0]:
        knowledge_spec = query_result.result_set[0][0]
        logging.info(
            "Knowledge retrieved: graph=%s chars=%d concepts=%d",
            graph_id,
            len(knowledge_spec),
            _count_rule_concepts(knowledge_spec),
        )
        return knowledge_spec

    query_result = await graph.query(
        """
        MATCH (d:Database)
        RETURN d.knowledge_spec
        """
    )

    if not query_result.result_set or not query_result.result_set[0][0]:
        logging.info("Knowledge retrieved: graph=%s chars=0 concepts=0", graph_id)
        return ""

    knowledge_spec = query_result.result_set[0][0]
    logging.info(
        "Knowledge retrieved: graph=%s chars=%d concepts=%d",
        graph_id,
        len(knowledge_spec),
        _count_rule_concepts(knowledge_spec),
    )
    return knowledge_spec


async def set_knowledge(
    graph_id: str, knowledge_spec: str, db=None, *, append: bool = True,
) -> None:
    """Set database-specific knowledge in the graph.

    APPENDS to existing knowledge for this DB by default (R1): a new
    "Load Knowledge" merges with the prior blob rather than replacing it.
    Pass ``append=False`` for an explicit overwrite (used e.g. by the
    clear-knowledge action that sends an empty string).

    The merged blob is kept on ``Database.knowledge_spec`` and the
    ``BusinessRules{kind:'knowledge_spec'}`` node (back-compat), AND chunked +
    embedded into retrievable ``(:Knowledge {content, embedding})`` nodes with a
    vector index, scoped to this DB graph. The embedded copy is re-derived from
    the merged blob on each write so it never duplicates or drifts.
    """
    graph = resolve_db(db).select_graph(graph_id)

    if append and (knowledge_spec or "").strip():
        existing = await get_knowledge(graph_id, db=db)
        merged_knowledge = _merge_knowledge_text(existing, knowledge_spec)
    else:
        # Explicit replace, or an empty/whitespace payload (clear).
        merged_knowledge = knowledge_spec or ""

    await graph.query(
        """
        MERGE (d:Database)
        SET d.knowledge_spec = $knowledge_spec
        """,
        {"knowledge_spec": merged_knowledge}
    )
    await graph.query(
        """
        MERGE (r:BusinessRules {kind: 'knowledge_spec'})
        SET r.name = '__business_rules_knowledge_spec__',
            r.content = $knowledge_spec,
            r.description = 'Database-specific business knowledge for this database graph'
        """,
        {"knowledge_spec": merged_knowledge}
    )
    # Re-derive the vector-indexed copy from the merged blob (failure-tolerant).
    chunk_count = await index_text_chunks(
        graph_id, "Knowledge", merged_knowledge, "knowledge_spec",
        replace_source=True, db=db,
    )
    logging.info(
        "Knowledge stored: graph=%s append=%s chars=%d concepts=%d indexed_chunks=%d",
        graph_id,
        append,
        len(merged_knowledge or ""),
        _count_rule_concepts(merged_knowledge),
        chunk_count,
    )


async def _query_graph(
    graph,
    query: str,
    params: Dict[str, Any] = None,
    timeout: int = 3000
) -> List[Any]:
    """
    Run a graph query asynchronously and return the result set.

    Args:
        graph: The graph database instance.
        query: The query string to execute.
        params: Optional parameters for the query.
        timeout: Query timeout in seconds.

    Returns:
        The result set from the query.
    """
    current_timeout = timeout
    for attempt in range(2):
        try:
            result = await graph.query(query, params or {}, timeout=current_timeout)
            return result.result_set
        except Exception as exc:
            if "timed out" in str(exc).lower() and attempt == 0:
                current_timeout *= 3
                logging.warning(
                    "Graph query timed out; retrying with larger timeout=%s error=%s",
                    current_timeout,
                    str(exc)[:200],
                )
                continue
            raise
    return []


def _flatten_graph_query_results(results: List[Any], source: str) -> List[Any]:
    """Flatten parallel graph query results while tolerating partial failures."""
    rows = []
    for result in results:
        if isinstance(result, Exception):
            logging.warning(
                "Graph %s search skipped a failed parallel query: %s",
                source,
                str(result)[:200],
            )
            continue
        rows.extend(result or [])
    return rows

async def _find_tables(
    graph,
    embeddings: List[List[float]]
) -> List[Dict[str, Any]]:
    """
    Find tables based on pre-computed embeddings.

    Args:
        graph: The graph database instance.
        embeddings: Pre-computed embeddings for the table descriptions.

    Returns:
        List of matching table information.
    """
    top_k = int(getattr(Config, "TABLE_RETRIEVAL_TOP_K", 8))
    query = f"""
        CALL db.idx.vector.queryNodes('Table','embedding',{top_k},vecf32($embedding))
        YIELD node, score
        MATCH (node)-[:BELONGS_TO]-(columns)
        RETURN node.name, node.description, node.foreign_keys, collect({{
            columnName: columns.name,
            description: columns.description,
            dataType: columns.type,
            keyType: columns.key_type,
            nullable: columns.nullable,
            sampleValues: columns.sample_values
        }})
    """

    tasks = [
        _query_graph(graph, query, {"embedding": embedding})
        for embedding in embeddings
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return _flatten_graph_query_results(results, "table-vector")


async def _find_tables_by_columns(
    graph,
    embeddings: List[List[float]]
) -> List[Dict[str, Any]]:
    """
    Find tables based on pre-computed embeddings for column descriptions.

    Args:
        graph: The graph database instance.
        embeddings: Pre-computed embeddings for the column descriptions.

    Returns:
        List of matching table information.
    """
    top_k = int(getattr(Config, "TABLE_RETRIEVAL_TOP_K", 8))
    query = f"""
        CALL db.idx.vector.queryNodes('Column','embedding',{top_k},vecf32($embedding))
        YIELD node, score
        MATCH (node)-[:BELONGS_TO]-(table)-[:BELONGS_TO]-(columns)
        RETURN
            table.name,
            table.description,
            table.foreign_keys,
            collect({{
                columnName: columns.name,
                description: columns.description,
                dataType: columns.type,
                keyType: columns.key_type,
                nullable: columns.nullable,
                sampleValues: columns.sample_values
            }})
    """

    tasks = [
        _query_graph(graph, query, {"embedding": embedding})
        for embedding in embeddings
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return _flatten_graph_query_results(results, "column-vector")


async def _find_tables_by_lexical_terms(
    graph,
    user_query: str,
    descriptions_text: List[str],
) -> List[Dict[str, Any]]:
    """Fallback schema retrieval when the embedding endpoint is unavailable."""
    tokens = _combined_lexical_search_terms(user_query, descriptions_text)
    if not tokens:
        return []

    limit = max(1, int(getattr(Config, "TABLE_CONTEXT_MAX", 20)) * 3)
    query = f"""
        UNWIND $tokens AS token
        MATCH (table:Table)
        OPTIONAL MATCH (col:Column)-[:BELONGS_TO]->(table)
        WITH table, col, token,
             toLower(table.name) CONTAINS token OR
             toLower(table.description) CONTAINS token OR
             (col IS NOT NULL AND toLower(col.name) CONTAINS token) OR
             (col IS NOT NULL AND toLower(col.description) CONTAINS token)
             AS matched
        WHERE matched
        WITH table,
             count(DISTINCT token) AS token_hits,
             count(DISTINCT col) AS column_hits
        MATCH (all_col:Column)-[:BELONGS_TO]->(table)
        WITH table, token_hits, column_hits,
             collect({{
                columnName: all_col.name,
                description: all_col.description,
                dataType: all_col.type,
                keyType: all_col.key_type,
                nullable: all_col.nullable,
                sampleValues: all_col.sample_values
             }}) AS columns
        RETURN table.name, table.description, table.foreign_keys, columns
        ORDER BY token_hits DESC, column_hits DESC
        LIMIT {limit}
    """
    try:
        rows = await _query_graph(graph, query, {"tokens": tokens}, timeout=5000)
    except Exception as exc:
        logging.error("Lexical schema fallback failed: %s", exc)
        return []

    logging.info(
        "Lexical schema fallback result: tokens=%d tables=%d table_names=%s",
        len(tokens),
        len(rows),
        [row[0] for row in rows[:10]],
    )
    return rows


async def _find_tables_sphere(
    graph,
    tables: List[str]
) -> List[Dict[str, Any]]:
    """
    Find tables in the sphere of influence of given tables.

    Args:
        graph: The graph database instance.
        tables: List of table names to find connections for.

    Returns:
        List of connected table information.
    """
    query = """
        MATCH (node:Table {name: $name})
        MATCH p = (node)-[:BELONGS_TO|REFERENCES*1..4]-(table_ref:Table)
        WHERE table_ref <> node
        WITH table_ref, min(length(p)) AS distance
        ORDER BY distance
        LIMIT 12
        MATCH (table_ref)-[:BELONGS_TO]-(columns:Column)
        RETURN table_ref.name, table_ref.description, table_ref.foreign_keys,
               collect({
                   columnName: columns.name,
                   description: columns.description,
                   dataType: columns.type,
                   keyType: columns.key_type,
                   nullable: columns.nullable,
                   sampleValues: columns.sample_values
               })
    """
    try:
        tasks = [_query_graph(graph, query, {"name": name}) for name in tables]
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:
        logging.error("Error finding tables in sphere: %s", e)
        results = []

    rows = _flatten_graph_query_results(results, "sphere")
    logging.info(
        "FK sphere expansion: source_tables=%d connected_tables=%s",
        len(tables),
        [row[0] for row in rows[:12] if isinstance(row, list) and row],
    )
    return rows


async def _find_tables_by_value(
    graph,
    values: List[str],
    limit: int = 4,
) -> List[Dict[str, Any]]:
    """Value-routing: tables that HOLD a literal filter value.

    Matches the value against columns' grounded descriptions (which list their
    domain values, incl. JSON leaves like ``location.country (Italy, …)``). This
    disambiguates by the VALUE itself — e.g. the literal ``Italy`` is a country
    value held by ``circuits.location_metadata.location.country``, NOT the driver
    nationality ``Italian`` — so the correct table surfaces even when the entity
    phrasing is wrong. Returns full table info. Never raises.
    """
    vals = [str(v).strip() for v in (values or []) if str(v).strip() and len(str(v).strip()) >= 2]
    if not vals:
        return []
    query = """
        UNWIND $vals AS val
        MATCH (t:Table)<-[:BELONGS_TO]-(c:Column)
        WHERE toLower(c.description) CONTAINS toLower(val)
        WITH DISTINCT t
        LIMIT $limit
        MATCH (t)-[:BELONGS_TO]-(columns:Column)
        RETURN t.name, t.description, t.foreign_keys,
               collect({
                   columnName: columns.name,
                   description: columns.description,
                   dataType: columns.type,
                   keyType: columns.key_type,
                   nullable: columns.nullable,
                   sampleValues: columns.sample_values
               })
    """
    try:
        results = await asyncio.gather(
            _query_graph(graph, query, {"vals": vals, "limit": limit}),
            return_exceptions=True,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("value-route lookup failed: %s", str(exc)[:160])
        return []
    return _flatten_graph_query_results(results, "value-route")


async def _find_direct_fk_neighbors(
    graph,
    table_names: List[str],
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Direct (one FK hop, either direction) neighbour tables of the anchors.

    Used to GUARANTEE that a strong anchor's join partners reach the generator
    even when the embedding model ranks them low — e.g. surface `circuits`
    (holding the country filter) next to `races` so the join can form. Returns
    full table info (name, description, FKs, columns) so the tables merge into
    the finder result like any other candidate.
    """
    if not table_names:
        return []
    query = """
        MATCH (a:Table)<-[:BELONGS_TO]-(:Column)-[:REFERENCES]-(:Column)-[:BELONGS_TO]->(nb:Table)
        WHERE a.name IN $names AND nb.name <> a.name
        WITH DISTINCT nb
        LIMIT $limit
        MATCH (nb)-[:BELONGS_TO]-(columns:Column)
        RETURN nb.name, nb.description, nb.foreign_keys,
               collect({
                   columnName: columns.name,
                   description: columns.description,
                   dataType: columns.type,
                   keyType: columns.key_type,
                   nullable: columns.nullable,
                   sampleValues: columns.sample_values
               })
    """
    try:
        results = await asyncio.gather(
            _query_graph(graph, query, {"names": list(table_names), "limit": limit}),
            return_exceptions=True,
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("direct FK-neighbor lookup failed: %s", str(exc)[:160])
        return []
    return _flatten_graph_query_results(results, "fk-neighbor")


async def _find_connecting_tables(
    graph,
    table_names: List[str]
) -> List[Dict[str, Any]]:
    """
    Find all tables that form connections between pairs of tables.

    Args:
        graph: The graph database instance.
        table_names: List of table names to find connections between.

    Returns:
        List of connecting table information.
    """
    pairs = [list(pair) for pair in combinations(table_names, 2)]
    if not pairs:
        return []

    query = """
    UNWIND $pairs AS pair
    MATCH (a:Table {name: pair[0]})
    MATCH (b:Table {name: pair[1]})
    WITH a, b
    MATCH p = allShortestPaths((a)-[*..4]-(b))
    UNWIND nodes(p) AS path_node
    WITH DISTINCT path_node
    WHERE 'Table' IN labels(path_node) OR
          ('Column' IN labels(path_node) AND path_node.key_type IN ['PRI', 'PK', 'PRIMARY KEY'])
    WITH path_node,
         'Table' IN labels(path_node) AS is_table,
         'Column' IN labels(path_node) AND path_node.key_type IN ['PRI', 'PK', 'PRIMARY KEY'] AS is_pri_column
    OPTIONAL MATCH (path_node)-[:BELONGS_TO]->(parent_table:Table)
    WHERE is_pri_column
    WITH CASE
           WHEN is_table THEN path_node
           WHEN is_pri_column THEN parent_table
           ELSE null
         END AS target_table
    WHERE target_table IS NOT NULL
    WITH DISTINCT target_table
    MATCH (col:Column)-[:BELONGS_TO]->(target_table)
    WITH target_table,
         collect({
            columnName: col.name,
            description: col.description,
            dataType: col.type,
            keyType: col.key_type,
            nullable: col.nullable,
            sampleValues: col.sample_values
         }) AS columns
    RETURN target_table.name, target_table.description, target_table.foreign_keys, columns
    """
    try:
        result = await _query_graph(graph, query, {"pairs": pairs}, timeout=500)
    except Exception as e:
        logging.error("Error finding connecting tables: %s", e)
        result = []

    return result


async def _find_tables_from_fk_annotations(
    graph,
    table_names: List[str],
) -> List[Dict[str, Any]]:
    """Find tables referenced by FK annotations embedded in column comments.

    Some schemas encode logical FKs in comments because the physical database
    constraint cannot be created (for example, the referenced column is not a
    declared primary key). Those annotations are still valuable retrieval
    signals and should participate in context expansion.
    """
    if not table_names:
        return []

    query = """
    UNWIND $table_names AS table_name
    MATCH (source:Table {name: table_name})<-[:BELONGS_TO]-(column:Column)
    WHERE column.description CONTAINS 'FK→'
    RETURN source.name, column.name, column.description
    """
    try:
        rows = await _query_graph(
            graph, query, {"table_names": list(dict.fromkeys(table_names))}, timeout=500
        )
    except Exception as e:
        logging.error("Error reading FK annotations: %s", e)
        return []

    target_names = []
    for _, _, description in rows:
        for match in _FK_ANNOTATION_RE.finditer(description or ""):
            table_name = match.group("table").strip()
            if table_name:
                target_names.append(table_name)
                if "." in table_name:
                    target_names.append(table_name.rsplit(".", 1)[-1])

    target_names = list(dict.fromkeys(target_names))
    if not target_names:
        return []

    target_query = """
    UNWIND $target_names AS target_name
    MATCH (target_table:Table {name: target_name})
    WITH DISTINCT target_table
    MATCH (col:Column)-[:BELONGS_TO]->(target_table)
    WITH target_table,
         collect({
            columnName: col.name,
            description: col.description,
            dataType: col.type,
            keyType: col.key_type,
            nullable: col.nullable,
            sampleValues: col.sample_values
         }) AS columns
    RETURN target_table.name, target_table.description, target_table.foreign_keys, columns
    """
    try:
        result = await _query_graph(
            graph, target_query, {"target_names": target_names}, timeout=500
        )
    except Exception as e:
        logging.error("Error finding tables from FK annotations: %s", e)
        return []

    if result:
        logging.info(
            "FK annotation expansion: source_tables=%d referenced_tables=%s",
            len(table_names),
            [row[0] for row in result[:15]],
        )
    return result


async def materialize_fk_edges(graph) -> int:
    """Create ``:REFERENCES`` edges from each ``Table.foreign_keys`` property.

    The loader already extracts the DB's declared FKs into the ``foreign_keys``
    property, but the graph build did not always materialize them as edges (the
    live sports graph had 0). Graph join-path traversal needs edges, so this
    builds them from the property — DB-agnostic (any loader that fills the
    property), idempotent, and SELF-HEALING: it returns immediately when edges
    already exist, so it is safe to call before traversal on every request and
    automatically repairs the graph after a fresh build or a re-index (which
    rebuilds the property but drops the edges). Returns the edge count.
    """
    try:
        existing = await _query_graph(
            graph, "MATCH (:Column)-[e:REFERENCES]->(:Column) RETURN count(e)"
        )
        if existing and existing[0] and int(existing[0][0]):
            return int(existing[0][0])
    except Exception:  # pylint: disable=broad-exception-caught
        return 0
    try:
        rows = await _query_graph(
            graph,
            "MATCH (t:Table) WHERE t.foreign_keys IS NOT NULL "
            "RETURN t.name, t.foreign_keys",
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("materialize_fk_edges read failed: %s", str(exc)[:200])
        return 0
    count = 0
    for row in rows or []:
        tname = row[0]
        for fk in _normalize_foreign_keys(row[1] if len(row) > 1 else None):
            col = fk.get("column")
            rtab = fk.get("referenced_table")
            rcol = fk.get("referenced_column")
            if not (col and rtab and rcol):
                continue
            cname = fk.get("constraint_name") or f"{tname}_{col}_fk"
            try:
                await _query_graph(
                    graph,
                    """
                    MATCH (src:Column {name: $col})-[:BELONGS_TO]->(:Table {name: $tn})
                    MATCH (tgt:Column {name: $rc})-[:BELONGS_TO]->(:Table {name: $rt})
                    MERGE (src)-[r:REFERENCES {rel_name: $cn}]->(tgt)
                    ON CREATE SET r.note = $note
                    """,
                    {"col": col, "tn": tname, "rc": rcol, "rt": rtab,
                     "cn": cname, "note": f"{tname}.{col} = {rtab}.{rcol}"},
                )
                count += 1
            except Exception:  # pylint: disable=broad-exception-caught
                continue
    if count:
        logging.info("Materialized %d FK :REFERENCES edges from Table.foreign_keys", count)
    return count


async def column_json_paths(graph, table_names: List[str]) -> dict:
    """Map each JSON/JSONB column to its valid key paths, from the stored
    ``Nested fields: {...}`` description. Returns
    ``{column_lower: {"leaves": {leaf_key: (k1, k2, ...)}, "full": set(path_tuples)}}``.
    The deterministic JSON gate uses this to validate/repair JSON paths in
    generated SQL — general (any JSON column on any DB), no hardcodes.
    """
    names = list(dict.fromkeys(t for t in (table_names or []) if t))
    if not names:
        return {}
    query = """
    UNWIND $names AS tn
    MATCH (c:Column)-[:BELONGS_TO]->(t:Table {name: tn})
    WHERE toLower(coalesce(c.type,'')) CONTAINS 'json'
    RETURN c.name, c.description
    """
    try:
        rows = await _query_graph(graph, query, {"names": names}, timeout=500)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("column_json_paths failed: %s", str(exc)[:200])
        return {}
    import json as _json  # pylint: disable=import-outside-toplevel
    out: dict = {}
    for row in rows or []:
        col = str(row[0] or "")
        desc = str(row[1] or "")
        if not col or "Nested fields:" not in desc:
            continue
        tail = desc.split("Nested fields:", 1)[1].strip()
        start = tail.find("{")
        if start < 0:
            continue
        try:
            obj = _json.loads(tail[start:])
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        leaves: dict = {}
        full: set = set()

        def _walk(node, prefix=()):
            for key, val in node.items():
                pp = prefix + (key,)
                if isinstance(val, dict):
                    _walk(val, pp)
                else:
                    full.add(pp)
                    # last writer wins is fine; ambiguous leaves simply won't
                    # be auto-repaired confidently (gate checks uniqueness).
                    leaves.setdefault(key, []).append(pp)

        if isinstance(obj, dict):
            _walk(obj)
            # keep only UNAMBIGUOUS leaf->path (exactly one path) for safe repair
            uniq = {k: v[0] for k, v in leaves.items() if len(v) == 1}
            out[col.lower()] = {"leaves": uniq, "full": full}
    return out


async def json_leaf_owner_tables(graph, question: str) -> List[str]:
    """Tables that OWN a JSON column whose leaf key (multi-token) is NAMED in the
    question — scanned over the WHOLE graph, independent of what the LLM finder
    retrieved. The finder intermittently omits a table holding a value the
    question explicitly asks for inside a JSON field (e.g. an event name in a
    schedule JSON) while returning noise; this lets the caller add it back
    deterministically so a requested output's table is never lost to finder
    variance. General, graph-driven, names nothing. Only MULTI-token leaf keys
    whose tokens ALL appear in the question (specific, not a generic word)."""
    import json as _json  # pylint: disable=import-outside-toplevel
    if not question:
        return []
    qtokens = set(re.findall(r"[a-z]+", question.lower()))
    if not qtokens:
        return []
    query = """
    MATCH (c:Column)-[:BELONGS_TO]->(t:Table)
    WHERE toLower(coalesce(c.type,'')) CONTAINS 'json'
    RETURN t.name, c.description
    """
    try:
        rows = await _query_graph(graph, query, {}, timeout=500)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("json_leaf_owner_tables failed: %s", str(exc)[:200])
        return []
    owners: List[str] = []
    for row in rows or []:
        tname = str(row[0] or "")
        desc = str(row[1] or "")
        if not tname or "Nested fields:" not in desc:
            continue
        tail = desc.split("Nested fields:", 1)[1].strip()
        start = tail.find("{")
        if start < 0:
            continue
        try:
            obj = _json.loads(tail[start:])
        except Exception:  # pylint: disable=broad-exception-caught
            continue
        leaf_keys: set = set()

        def _walk(node):
            for key, val in node.items():
                if isinstance(val, dict):
                    _walk(val)
                else:
                    leaf_keys.add(str(key))

        if isinstance(obj, dict):
            _walk(obj)
        for leaf in leaf_keys:
            toks = [x for x in re.findall(r"[a-z]+", leaf.lower()) if len(x) > 2]
            if len(toks) >= 2 and all(x in qtokens for x in toks):
                owners.append(tname)
                break
    return list(dict.fromkeys(owners))


async def fetch_table_entries(graph, table_names: List[str]) -> List[list]:
    """Build full ``[name, description, foreign_keys, columns]`` table entries for
    the given names, in the same shape the table-finder returns, so they can be
    appended to the candidate set. Used to add a deterministically-required table
    the finder missed. Graph-driven; general."""
    names = list(dict.fromkeys(n for n in (table_names or []) if n))
    if not names:
        return []
    query = """
    UNWIND $names AS nm
    MATCH (target_table:Table {name: nm})
    WITH DISTINCT target_table
    MATCH (col:Column)-[:BELONGS_TO]->(target_table)
    WITH target_table,
         collect({
            columnName: col.name,
            description: col.description,
            dataType: col.type,
            keyType: col.key_type,
            nullable: col.nullable,
            sampleValues: col.sample_values
         }) AS columns
    RETURN target_table.name, target_table.description, target_table.foreign_keys, columns
    """
    try:
        rows = await _query_graph(graph, query, {"names": names}, timeout=500)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("fetch_table_entries failed: %s", str(exc)[:200])
        return []
    out: List[list] = []
    for row in rows or []:
        cols = [dict(c) for c in (row[3] or [])]
        out.append([row[0], row[1], row[2], cols])
    return out


async def compute_join_skeleton(graph, table_names: List[str]) -> List[str]:
    """Verified join conditions among ``table_names``, computed by traversing the
    FK ``:REFERENCES`` edges in the graph.

    Maximally uses the graph: instead of flattening FK metadata into text and
    hoping a weak model assembles correct joins, we return the EXACT, real join
    conditions ("table.col = ref_table.ref_col") so the generator copies verified
    joins and a gate can reject any join not present here. Direction-agnostic
    (a join may be written either way); composite FKs surface as multiple
    conditions sharing a constraint.
    """
    names = list(dict.fromkeys(t for t in (table_names or []) if t))
    if len(names) < 2:
        return []
    query = """
    MATCH (ca:Column)-[r:REFERENCES]->(cb:Column)
    MATCH (ca)-[:BELONGS_TO]->(ta:Table)
    MATCH (cb)-[:BELONGS_TO]->(tb:Table)
    WHERE ta.name IN $names AND tb.name IN $names AND ta.name <> tb.name
    RETURN DISTINCT ta.name, ca.name, tb.name, cb.name
    """
    try:
        rows = await _query_graph(graph, query, {"names": names}, timeout=500)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logging.warning("compute_join_skeleton failed: %s", str(exc)[:200])
        return []
    out: List[str] = []
    seen: set = set()
    for row in rows or []:
        if len(row) < 4:
            continue
        ta, ca, tb, cb = row[0], row[1], row[2], row[3]
        key = tuple(sorted([f"{ta}.{ca}", f"{tb}.{cb}"]))
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{ta}.{ca} = {tb}.{cb}")
    return out


async def find( # pylint: disable=too-many-locals
    graph_id: str,
    queries_history: List[str],
    db_description: str = None,
    knowledge_spec: str | None = None,
    user_rules_spec: str | None = None,
    db=None,
) -> List[List[Any]]:
    """
    Find the tables and columns relevant to the user's query.

    Args:
        graph_id: The identifier for the graph database.
        queries_history: List of previous queries, with the last one being current.
        db_description: Optional description of the database.
        knowledge_spec: Optional DB-specific business/domain knowledge.
        user_rules_spec: Optional user rules for resolving concepts.
        db: Optional FalkorDB handle; falls back to the server singleton.

    Returns:
        Combined list of relevant tables.
    """
    graph = resolve_db(db).select_graph(graph_id)
    user_query = queries_history[-1]
    previous_queries = queries_history[:-1]
    context_query_text = " ".join((previous_queries or []) + [user_query])

    logging.info("Calling LLM to find relevant tables/columns for query")

    knowledge = (knowledge_spec or "").strip()
    domain_rules = (user_rules_spec or "").strip()
    logging.info(
        "Table-finder context: graph=%s knowledge_chars=%d user_rules_chars=%d previous_queries=%d",
        graph_id,
        len(knowledge),
        len(domain_rules),
        len(previous_queries),
    )
    system_content = Config.FIND_SYSTEM_PROMPT.format(db_description=db_description)
    if knowledge:
        system_content += f"""

    The selected database has database-specific business knowledge. Use it to
    map named or implied business metrics, formulas, thresholds, and domain
    concepts into relevant table/column descriptions for schema search. Apply
    formula definitions exactly when they match the user's intent. The
    database schema is still authoritative for table and column names.

    <knowledge_spec>
    {knowledge}
    </knowledge_spec>
    """

    if domain_rules:
        system_content += f"""

    The selected database also has user rules. Use these only as supplemental
    query guidance. They are not schema: table and column names must still come
    from the database graph.

    <user_rules_spec>
    {domain_rules}
    </user_rules_spec>
    """

    messages = [
        {
            "role": "system",
            "content": system_content
        },
        {
            "role": "user",
            "content": json.dumps({
                "previous_user_queries": previous_queries,
                "user_query": user_query
            })
        },
    ]

    table_finder_rules = """

Schema-search method:
- ENTITIES FIRST: list the concrete entities in the question — business objects, metrics, attributes, filter fields, grouping keys, joins — and emit one search hint per distinct entity. Keep each a short noun phrase; decompose compound questions.
- Read table and column descriptions/comments carefully; they define business meaning and are the main signal for matching an entity to the schema.
- A metric entity must map to the column whose description best matches it, even when a similarly named generic measure exists elsewhere. Metric correctness matters more than picking a single table.
- When the output grain is a business object or an aggregate over one, include the fact/detail table whose measure description matches that object-grain metric, even if the question mentions related component/link rows.
- When the user asks for a count/list of underlying records together with
  sums/averages over those records, include the table whose rows represent that
  underlying record grain. Do not rely only on snapshot/rest/summary sources
  unless their descriptions show they preserve the requested record grain.
- When the user asks for unique values across several columns of the same
  concept, include all directly described columns and the source table needed to
  normalize those values before counting.
- Include master/entity tables needed for labels, client names, statuses, or attributes that are not stored on the selected fact table.
- For snapshot/as-of entities, include report/as-of/balance date columns and tables needed to join the same reporting slice.
"""

    strict_json_instruction = """

Return only a valid JSON object with exactly these keys:
{
  "tables_descriptions": [{"name": "entity", "description": "short entity phrase to match against table descriptions"}],
  "columns_descriptions": [{"name": "entity", "description": "short entity phrase to match against column descriptions"}],
  "values": ["literal filter value the question names, normalized to its canonical English form"]
}
One item per distinct entity (typically 2 to 8 each). In "values" list any concrete literal the question filters by — a specific country, city, status, category, code, or proper name — normalized to its canonical English form (translate/declension-normalize); use [] if the question names no literal. Use short noun phrases, not sentences. Do not use Markdown. Do not add explanatory text outside JSON.
"""
    system_content += table_finder_rules
    compact_system_content = f"""
You map a user's natural-language database question to the schema by identifying
the ENTITIES it refers to and where each is found. Return only valid JSON.

Database description:
{db_description or ""}
{table_finder_rules}
{strict_json_instruction}
"""
    if knowledge:
        compact_system_content += f"\n<knowledge_spec>\n{knowledge}\n</knowledge_spec>\n"
    if domain_rules:
        compact_system_content += f"\n<user_rules_spec>\n{domain_rules}\n</user_rules_spec>\n"

    attempt_specs = []
    if getattr(Config, "TABLE_FINDER_STRUCTURED_OUTPUT_ENABLED", False):
        attempt_specs.append({
            "name": "structured_response_format",
            "messages": messages,
            "response_format": Descriptions,
        })
    attempt_specs.extend([
        {
            "name": "prompt_json",
            "messages": [
                {"role": "system", "content": system_content + strict_json_instruction},
                messages[1],
            ],
        },
        {
            "name": "compact_prompt_json",
            "messages": [
                {"role": "system", "content": compact_system_content},
                messages[1],
            ],
        },
        {
            "name": "query_only_json",
            "messages": [
                {"role": "system", "content": compact_system_content},
                {
                    "role": "user",
                    "content": (
                        "Identify the entities to look up for this question:\n"
                        f"{user_query}"
                    ),
                },
            ],
        },
    ])

    json_data = None
    last_error = None
    if not getattr(Config, "TABLE_FINDER_LLM_ENABLED", True):
        logging.info(
            "Table-finder LLM disabled; using query-text fallback: graph=%s",
            graph_id,
        )
        json_data = _fallback_descriptions_from_query(user_query, previous_queries)
    else:
        max_attempts = max(1, int(getattr(Config, "TABLE_FINDER_MAX_ATTEMPTS", 4)))
        for attempt_index in range(max_attempts):
            spec = attempt_specs[min(attempt_index, len(attempt_specs) - 1)]
            completion_kwargs = {
                "messages": spec["messages"],
                "temperature": 0,
                "max_tokens": int(getattr(Config, "TABLE_FINDER_MAX_TOKENS", 1200)),
                "extra_body": Config.reasoning_extra_body(
                    getattr(Config, "TABLE_FINDER_REASONING", None)
                ),
            }
            if spec.get("response_format") is not None:
                completion_kwargs["response_format"] = spec["response_format"]

            completion_result = await asyncio.to_thread(
                completion,
                **Config.completion_kwargs(**completion_kwargs),
            )
            raw_descriptions = _completion_message_content(completion_result)
            try:
                json_data = _parse_descriptions_response(raw_descriptions)
                logging.info(
                    "Table-finder attempt succeeded: graph=%s attempt=%d/%d mode=%s "
                    "finish_reason=%s usage=%s content_chars=%d",
                    graph_id,
                    attempt_index + 1,
                    max_attempts,
                    spec["name"],
                    _completion_finish_reason(completion_result),
                    _completion_usage_summary(completion_result),
                    len(raw_descriptions),
                )
                break
            except ValueError as exc:
                last_error = exc
                logging.warning(
                    "Table-finder attempt unusable: graph=%s attempt=%d/%d mode=%s "
                    "finish_reason=%s usage=%s content_chars=%d error=%s preview=%s",
                    graph_id,
                    attempt_index + 1,
                    max_attempts,
                    spec["name"],
                    _completion_finish_reason(completion_result),
                    _completion_usage_summary(completion_result),
                    len(raw_descriptions),
                    exc,
                    raw_descriptions[:300],
                )

    if json_data is None:
        logging.warning(
            "Table-finder exhausted LLM attempts; using query-text fallback: "
            "graph=%s attempts=%d last_error=%s",
            graph_id,
            max_attempts,
            last_error,
        )
        json_data = _fallback_descriptions_from_query(user_query, previous_queries)

    # Literal filter VALUES the model parsed from the question (value-routing,
    # codex #2). Extracted defensively before building Descriptions so the
    # pydantic model never sees the extra key.
    filter_values: list[str] = []
    if isinstance(json_data, dict):
        _fv = json_data.pop("values", None)
        if _fv is None:
            _fv = json_data.pop("filter_values", None)
        if isinstance(_fv, list):
            filter_values = [str(x).strip() for x in _fv if str(x).strip()][:8]
        json_data = {k: json_data[k] for k in
                     ("tables_descriptions", "columns_descriptions") if k in json_data}

    # General country/demonym normalizer — independently recover literal country
    # mentions from the RAW question (any language / declension), so value-routing
    # works even when the weak extraction model misreads the literal (e.g. reads
    # Russian «Италии» as "Iran"). General world knowledge, not per-DB data.
    try:
        from api.core.gazetteer import extract_country_literals  # pylint: disable=import-outside-toplevel
        for _g in extract_country_literals(context_query_text):
            if _g not in filter_values:
                filter_values.append(_g)
    except Exception:  # pylint: disable=broad-exception-caught
        pass

    descriptions = Descriptions(**json_data)
    if filter_values:
        logging.info("Table-finder VALUES: graph=%s values=%s", graph_id, filter_values)
    logging.info(
        "Table-finder LLM descriptions: graph=%s table_descriptions=%d column_descriptions=%d",
        graph_id,
        len(descriptions.tables_descriptions),
        len(descriptions.columns_descriptions),
    )
    logging.info(
        "Table-finder ENTITIES: graph=%s tables=%s columns=%s",
        graph_id,
        [d.description for d in descriptions.tables_descriptions],
        [d.description for d in descriptions.columns_descriptions],
    )
    descriptions_text = ([desc.description for desc in descriptions.tables_descriptions] +
                         [desc.description for desc in descriptions.columns_descriptions])
    if not descriptions_text:
        return []

    try:
        logging.info(
            "Embedding schema search started: graph=%s descriptions=%d model=%s",
            graph_id,
            len(descriptions_text),
            getattr(Config, "EMBEDDING_MODEL_NAME", "unknown"),
        )
        embedding_results = Config.EMBEDDING_MODEL.embed(descriptions_text)
        logging.info(
            "Embedding schema search completed: graph=%s vectors=%d",
            graph_id,
            len(embedding_results or []),
        )

        # Split embeddings back into table and column embeddings
        table_embeddings = embedding_results[:len(descriptions.tables_descriptions)]
        column_embeddings = embedding_results[len(descriptions.tables_descriptions):]

        main_tasks = []

        if table_embeddings:
            main_tasks.append(_find_tables(graph, table_embeddings))
        if column_embeddings:
            main_tasks.append(_find_tables_by_columns(graph, column_embeddings))
        main_tasks.append(
            _find_tables_by_lexical_terms(graph, context_query_text, descriptions_text)
        )

        # Execute the main embedding-based searches in parallel
        logging.info(
            "Graph schema search tasks started: graph=%s tasks=%d",
            graph_id,
            len(main_tasks),
        )
        results = await asyncio.gather(*main_tasks)
        logging.info("Graph schema search tasks completed: graph=%s", graph_id)

        # Unpack results based on what tasks we ran
        result_index = 0
        tables_des = []
        tables_by_columns_des = []
        if table_embeddings:
            tables_des = results[result_index]
            result_index += 1
        if column_embeddings:
            tables_by_columns_des = results[result_index]
            result_index += 1
        lexical_tables = results[result_index]
    except Exception as exc:
        logging.error(
            "Embedding schema search failed after retries. graph=%s error=%s",
            graph_id,
            str(exc)[:300],
        )
        raise RuntimeError(
            "Embedding schema search failed after configured retry attempts"
        ) from exc

    # VALUE-ROUTING (codex #2): route the literal filter values the model parsed
    # to the tables that HOLD them — matched against columns' grounded
    # descriptions (which list their domain values, incl. JSON leaves). This
    # disambiguates by the VALUE itself (e.g. literal `Italy` -> the country
    # column's table, not the `Italian` nationality column), bypassing entity-
    # phrasing errors. Routed tables are first-class: merged into candidates,
    # seeded, boosted and protected.
    value_routed_tables: list = []
    value_routed_names: set[str] = set()
    if filter_values:
        try:
            value_routed_tables = await _find_tables_by_value(graph, filter_values)
            value_routed_names = {t[0] for t in value_routed_tables
                                  if isinstance(t, list) and t}
            if value_routed_names:
                logging.info(
                    "Table-finder value-routing: graph=%s values=%s -> tables=%s",
                    graph_id, filter_values, sorted(value_routed_names))
        except Exception as _vr_exc:  # pylint: disable=broad-exception-caught
            logging.warning("Table-finder value-routing skipped: %s", str(_vr_exc)[:160])

    # NB: no raw-query vector augment — embedding the raw question is language-
    # dependent (garbage for a weak-on-Russian embedder); the LLM ENTITIES +
    # value-routing are the retrieval keys instead.

    # Vector RAG seeds — per the column-first design, the tables whose COLUMNS
    # (rich per-column documents) or table embedding best match the query
    # ENTITIES are PRIMARY results. They must seed expansion, rank high, and
    # survive trimming even when their NAME shares no tokens with the question —
    # lexical-only ranking otherwise demotes a genuinely-relevant table (e.g. a
    # fraud-probability table on a "fraud" question whose name lacks "fraud") and
    # the context trim then drops it. Protect the strongest column/table hits.
    _vseed_k = int(getattr(Config, "TABLE_VECTOR_SEED_MAX", 6))
    vector_seed_names: set[str] = set(value_routed_names)
    for _src in ((tables_by_columns_des or []), (tables_des or [])):
        for _ti in _src[:_vseed_k]:
            if isinstance(_ti, list) and _ti:
                vector_seed_names.add(_ti[0])
    if vector_seed_names:
        logging.info(
            "Table-finder vector RAG seeds (protected): graph=%s seeds=%s",
            graph_id, sorted(vector_seed_names),
        )

    # Extract and rank direct semantic/vector matches before graph expansion.
    # FK/sphere expansion is a useful join-safety signal, but expanding from
    # every vector candidate is noisy on large schemas.
    direct_tables = _rank_tables_for_context(
        _get_unique_tables(
            (tables_des or []) + (tables_by_columns_des or [])
            + (value_routed_tables or []) + (lexical_tables or [])
        ),
        context_query_text,
        descriptions_text,
    )
    # Anchor table names against BOTH the raw question and the LLM-extracted
    # entities. The entities bridge language (a Russian question -> English
    # entities like "circuit"/"race"), so a core table whose NAME matches an
    # entity anchors even when the raw question shares no tokens with the
    # English schema. An anchored table is boosted into the expansion seeds, so
    # its FK neighbours (e.g. `races` one hop from `circuits`) get pulled in even
    # when the embedding model ranks overlapping synthetic tables higher. (codex)
    _entity_text = " ".join(descriptions_text)
    direct_anchor_names = (
        _table_name_anchor_matches(direct_tables, context_query_text)
        | _table_name_anchor_matches(direct_tables, _entity_text)
    )
    if direct_anchor_names:
        logging.info(
            "Table-finder direct graph anchors from user wording: graph=%s anchors=%s",
            graph_id,
            sorted(direct_anchor_names)[:20],
        )
        direct_tables = _rank_tables_for_context(
            direct_tables,
            context_query_text,
            descriptions_text,
            boosted_table_names=direct_anchor_names,
        )
    direct_table_names = {table_info[0] for table_info in direct_tables}
    seed_max = int(getattr(Config, "TABLE_EXPANSION_SEED_MAX", 10))
    found_table_names = [table_info[0] for table_info in direct_tables[:seed_max]]
    # vector RAG seeds also drive FK expansion: a column hit pulls its table's
    # FK neighbours, per the column→table→FK design.
    for _vname in vector_seed_names:
        if _vname not in found_table_names:
            found_table_names.append(_vname)
    logging.info(
        "Table-finder direct seeds: graph=%s direct_tables=%d seeds=%s",
        graph_id,
        len(direct_tables),
        found_table_names,
    )
    if len(direct_tables) > seed_max:
        logging.info(
            "Table-finder expansion seeds trimmed: graph=%s direct_tables=%d seeds=%d",
            graph_id,
            len(direct_tables),
            seed_max,
        )

    # Only run sphere and connecting searches if we found tables
    if found_table_names:
        secondary_tasks = [
            _find_tables_from_fk_annotations(graph, found_table_names),
            _find_tables_sphere(graph, found_table_names),
            _find_connecting_tables(graph, found_table_names)
        ]
        tables_by_fk_annotations, tables_by_sphere, tables_by_route = (
            await asyncio.gather(*secondary_tasks)
        )
    else:
        tables_by_fk_annotations, tables_by_sphere, tables_by_route = [], [], []

    combined_tables = _get_unique_tables(
        direct_tables +
        tables_by_fk_annotations +
        tables_by_route +
        tables_by_sphere
    )
    boosted_table_names = (
        direct_anchor_names | vector_seed_names
        | _table_name_anchor_matches(combined_tables, context_query_text)
        | _table_name_anchor_matches(combined_tables, _entity_text)
    )
    if boosted_table_names:
        logging.info(
            "Table-finder graph anchors from user wording: graph=%s anchors=%s",
            graph_id,
            sorted(boosted_table_names)[:20],
        )
        combined_tables = _rank_tables_for_context(
            combined_tables,
            context_query_text,
            descriptions_text,
            direct_table_names,
            boosted_table_names,
        )
    # FK-neighbour surfacing (codex #1): guarantee the direct FK join-partners of
    # the strongest anchors reach the generator. A join partner the embedding
    # model ranks low — e.g. `circuits` (which holds the country filter) next to
    # `races` — is otherwise trimmed and the join never forms. Deterministic, one
    # graph query, no extra LLM call.
    _ranked_names = [t[0] for t in combined_tables if isinstance(t, list) and t]
    _top_anchor_names = ([n for n in _ranked_names if n in boosted_table_names][:2]
                         or _ranked_names[:2])
    anchor_neighbor_names: set[str] = set()
    if _top_anchor_names:
        _nb = await _find_direct_fk_neighbors(graph, _top_anchor_names)
        if _nb:
            anchor_neighbor_names = {t[0] for t in _nb if isinstance(t, list) and t}
            combined_tables = _get_unique_tables(combined_tables + _nb)
            _added = anchor_neighbor_names - set(_ranked_names)
            logging.info(
                "Table-finder FK-neighbour surfacing: graph=%s anchors=%s added=%s",
                graph_id, _top_anchor_names, sorted(_added),
            )
    combined_tables = await _rerank_tables_with_llm(
        combined_tables,
        user_query,
        descriptions_text,
        previous_queries=previous_queries,
        db_description=db_description,
        user_rules_spec=domain_rules,
        stage="expanded",
        direct_table_names=direct_table_names,
        boosted_table_names=boosted_table_names,
    )
    context_max = int(getattr(Config, "TABLE_CONTEXT_MAX", 20))
    if len(combined_tables) > context_max:
        protected_table_priority: dict[str, int] = {}
        protected_table_names: set[str] = set()
        priority_index = 0
        relevance_tokens = set(_combined_lexical_search_terms(
            context_query_text,
            descriptions_text,
        ))
        # vector RAG seeds get the highest re-insertion priority: a strong
        # column/table vector match must never be trimmed for a lexical winner.
        for _vname in sorted(vector_seed_names):
            protected_table_names.add(_vname)
            protected_table_priority.setdefault(_vname, priority_index)
            priority_index += 1
        # direct FK join-partners of the top anchors: protect so the join can form
        for _nbname in sorted(anchor_neighbor_names):
            protected_table_names.add(_nbname)
            protected_table_priority.setdefault(_nbname, priority_index)
            priority_index += 1
        for protected_group in (tables_by_route, tables_by_fk_annotations):
            for table_info in protected_group:
                if not (isinstance(table_info, list) and table_info):
                    continue
                table_name = table_info[0]
                protected_table_names.add(table_name)
                protected_table_priority.setdefault(table_name, priority_index)
                priority_index += 1
        sphere_token_weights = _token_idf_weights(combined_tables, relevance_tokens)
        for table_info in tables_by_sphere:
            if not (isinstance(table_info, list) and table_info):
                continue
            if _table_relevance_score(
                table_info,
                relevance_tokens,
                direct_table_names,
                boosted_table_names,
                sphere_token_weights,
            ) <= 0:
                continue
            table_name = table_info[0]
            protected_table_names.add(table_name)
            protected_table_priority.setdefault(table_name, priority_index)
            priority_index += 1
        logging.info(
            "Table-finder context trimmed: graph=%s tables_before=%d tables_after=%d",
            graph_id,
            len(combined_tables),
            context_max,
        )
        combined_tables = _trim_tables_for_context(
            combined_tables,
            context_max,
            context_query_text,
            descriptions_text,
            protected_table_names,
            direct_table_names,
            protected_table_priority,
            boosted_table_names,
        )
    if boosted_table_names:
        combined_tables = sorted(
            enumerate(combined_tables),
            key=lambda item: (
                0 if isinstance(item[1], list)
                and item[1]
                and item[1][0] in boosted_table_names else 1,
                item[0],
            ),
        )
        combined_tables = [table_info for _index, table_info in combined_tables]
    logging.info(
        "Table-finder result: graph=%s tables=%d table_names=%s",
        graph_id,
        len(combined_tables),
        [table_info[0] for table_info in combined_tables],
    )

    return combined_tables

def _get_unique_tables(tables_list):
    # Dictionary to store unique tables with the table name as the key
    unique_tables = {}

    for table_info in tables_list:
        table_name = table_info[0]  # The first element is the table name

        # Only add if this table name hasn't been seen before
        try:
            if table_name not in unique_tables:
                table_info[3] = [dict(od) for od in table_info[3]]
                unique_tables[table_name] = table_info
        except Exception as e:  # pylint: disable=broad-exception-caught
            print(f"Error: {table_info}, Exception: {e}")

    # Return the values (the unique table info lists)
    return list(unique_tables.values())
