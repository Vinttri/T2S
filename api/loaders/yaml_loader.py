"""YAML metadata loader for building T2S graphs from dbt-style files."""

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, Iterable, List, Tuple
from urllib.parse import parse_qs, unquote, urlparse

import yaml

from api.loaders.base_loader import BaseLoader

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

_FK_EXPR_RE = re.compile(
    r"^\s*(?:(?P<schema>[A-Za-z0-9_]+)\.)?"
    r"(?P<table>[A-Za-z0-9_]+)\s*\(\s*(?P<column>[A-Za-z0-9_]+)\s*\)\s*$"
)
_DESCRIPTION_FK_RE = re.compile(
    r"\s*\bFK\s*(?:->|→)\s*[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?"
    r"\s*\(\s*[A-Za-z_][A-Za-z0-9_]*\s*\)",
    re.IGNORECASE,
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TYPE_LENGTH_RE = re.compile(r"\((\d+)\)")
_SPACE_RE = re.compile(r"\s+")
_CODED_VALUE_ITEM_RE = re.compile(
    r"(?:^|[\s(;,.])(?P<code>[A-Za-zА-Яа-я0-9_]{1,16})\s*(?:-|–|—|=|:)\s*\S"
)
_URL_PASSWORD_RE = re.compile(r"(://[^:/@\s]+:)([^@\s]+)(@)")
_API_KEY_RE = re.compile(r"\b(sk-[A-Za-z0-9._-]{6,})\b")


class YAMLMetadataError(Exception):
    """Raised when YAML metadata cannot be parsed or loaded."""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_identifier(value: Any) -> str:
    """Normalize SQL identifiers from metadata files to QW's lower-case style."""
    return str(value or "").strip().strip('"`[]').lower()


def _normalize_table_name(table_name: str, schema: str | None = None) -> str:
    """Return a lower-case, optionally schema-qualified table name."""
    table_name = str(table_name or "").strip().strip('"`[]')
    if "." in table_name:
        return ".".join(_normalize_identifier(part) for part in table_name.split(".") if part)
    normalized = _normalize_identifier(table_name)
    if schema:
        return f"{_normalize_identifier(schema)}.{normalized}"
    return normalized


def _bounded_int_env(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


def _compact_text(value: Any, max_chars: int = 500) -> str:
    text = _SPACE_RE.sub(" ", str(value or "").strip())
    if not text:
        return ""
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 1].rstrip() + "…"
    return text


def _safe_error_message(exc: Exception, max_chars: int = 700) -> str:
    """Return an actionable import error while redacting common secret shapes."""
    text = _compact_text(exc, max_chars)
    if not text:
        text = exc.__class__.__name__
    text = _URL_PASSWORD_RE.sub(r"\1***\3", text)
    text = _API_KEY_RE.sub("sk-***", text)
    return text


def _strip_fk_text(value: Any) -> str:
    """Remove generated FK fragments from human descriptions before rebuilding."""
    return _compact_text(_DESCRIPTION_FK_RE.sub("", str(value or "")), 0)


def _dedupe_text_parts(parts: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for part in parts:
        normalized = _compact_text(part, 0)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _parse_fk_expression(expression: str, default_schema: str | None) -> tuple[str, str] | None:
    """Parse ``SCHEMA.TABLE(COLUMN)`` foreign-key expressions from YAML."""
    match = _FK_EXPR_RE.match(str(expression or ""))
    if not match:
        return None
    target_schema = _normalize_identifier(match.group("schema") or default_schema or "")
    target_table = _normalize_identifier(match.group("table"))
    target_column = _normalize_identifier(match.group("column"))
    table_name = f"{target_schema}.{target_table}" if target_schema else target_table
    return table_name, target_column


def _constraint_types(column: dict) -> set[str]:
    return {
        str(item.get("type", "")).strip().lower()
        for item in column.get("constraints", []) or []
        if isinstance(item, dict)
    }


def _column_key_type(types: set[str], has_valid_fk: bool = True) -> str:
    if "primary_key" in types:
        return "PRIMARY KEY"
    if "foreign_key" in types and has_valid_fk:
        return "FOREIGN KEY"
    return "NONE"


def _column_nullable(types: set[str]) -> str:
    return "NO" if "not_null" in types or "primary_key" in types else "YES"


def _column_description(
    column: dict,
    column_name: str,
    data_type: str,
    constraint_types: set[str],
    fk_targets: list[tuple[str, str]],
) -> str:
    parts = []
    description = _strip_fk_text(_compact_text(
        column.get("description"),
        _bounded_int_env("YAML_COLUMN_DESCRIPTION_MAX_CHARS", 600, 80, 5000),
    ))
    if description:
        parts.append(description)
    else:
        parts.append(f"Column {column_name} of type {data_type}")

    if "primary_key" in constraint_types:
        parts.append("(PRIMARY KEY)")
    if "foreign_key" in constraint_types and fk_targets:
        parts.append("(FOREIGN KEY)")
    if "not_null" in constraint_types or "primary_key" in constraint_types:
        parts.append("(NOT NULL)")

    for target_table, target_column in fk_targets:
        parts.append(f"FK→ {target_table}({target_column})")

    return " ".join(_dedupe_text_parts(parts))


def _table_description(model: dict, table_name: str, columns: dict[str, dict]) -> str:
    description = _compact_text(
        model.get("description"),
        _bounded_int_env("YAML_TABLE_DESCRIPTION_MAX_CHARS", 700, 80, 5000),
    )
    if description:
        return description

    return f"Table {table_name}"


def _is_generated_table_description(description: Any, table_name: str) -> bool:
    text = _compact_text(description, 0)
    return not text or text.lower() == f"table {table_name}".lower()


def _identifier_tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.split(r"[^0-9A-Za-zА-Яа-я]+", str(value or "").lower())
        if token
    }


def _is_coded_filter_column(name: str, info: dict[str, Any]) -> bool:
    """Detect generic type/code/category columns worth surfacing in table text.

    This intentionally does not list concrete business columns. It preserves
    compact descriptions for columns that define row categories, statuses, roles,
    flags, or code values, especially when the YAML description contains a value
    mapping such as "0 - Legal entity, 2 - Individual entrepreneur".
    """
    name_tokens = _identifier_tokens(name)
    description = _compact_text(
        info.get("_base_description") or info.get("description"),
        0,
    ).lower()
    description_tokens = _identifier_tokens(description)
    data_type = str(info.get("type") or "").lower()
    semantic_tokens = {
        "type",
        "category",
        "status",
        "role",
        "class",
        "kind",
        "code",
        "cd",
        "flag",
        "indicator",
        "тип",
        "категория",
        "статус",
        "роль",
        "класс",
        "код",
        "признак",
        "группа",
    }
    has_semantic_name = bool(name_tokens & semantic_tokens)
    has_semantic_description = bool(description_tokens & semantic_tokens)
    value_mapping_matches = list(_CODED_VALUE_ITEM_RE.finditer(description))
    has_value_mapping = (
        len(value_mapping_matches) >= 2
        or any(match.group("code").isdigit() for match in value_mapping_matches)
    )
    is_compact_scalar = any(
        token in data_type
        for token in ("smallint", "tinyint", "int", "char", "varchar", "string", "text")
    )
    return is_compact_scalar and (
        has_value_mapping
        or has_semantic_name
        or (has_semantic_description and len(description) <= 260)
    )


def _auto_table_description(table_name: str, table_info: dict[str, Any]) -> str:
    """Build compact table-level retrieval text when YAML lacks a model description.

    Table embeddings are created from table descriptions. If YAML files only
    describe columns, a table node like "Table schema.foo" carries almost no
    business signal. This summary keeps retrieval compact while exposing the
    most useful schema facts: declared FK targets and key/date columns.
    """
    max_chars = _bounded_int_env("YAML_AUTO_TABLE_DESCRIPTION_MAX_CHARS", 900, 120, 5000)
    column_limit = _bounded_int_env("YAML_AUTO_TABLE_DESCRIPTION_COLUMN_LIMIT", 8, 0, 30)
    parts = [f"Table {table_name}"]

    foreign_keys = table_info.get("foreign_keys", []) or []
    if foreign_keys:
        links: list[str] = []
        seen_links: set[str] = set()
        for fk in foreign_keys:
            source_column = _normalize_identifier(fk.get("column"))
            target_table = _normalize_table_name(str(fk.get("referenced_table") or ""))
            target_column = _normalize_identifier(fk.get("referenced_column"))
            if not source_column or not target_table or not target_column:
                continue
            link = f"{source_column}-> {target_table}({target_column})"
            if link in seen_links:
                continue
            seen_links.add(link)
            links.append(link)
            if len(links) >= 8:
                break
        if links:
            parts.append("Declared FK links: " + "; ".join(links))

    if column_limit > 0:
        selected_columns: list[tuple[str, dict[str, Any]]] = []
        columns = table_info.get("columns", {}) or {}

        def add_column(name: str, info: dict[str, Any]) -> None:
            if len(selected_columns) >= column_limit:
                return
            if any(existing_name == name for existing_name, _ in selected_columns):
                return
            selected_columns.append((name, info))

        for name, info in columns.items():
            key_type = str(info.get("key") or "").upper()
            if key_type in {"PRIMARY KEY", "FOREIGN KEY", "PK", "PRI"}:
                add_column(name, info)
        for name, info in columns.items():
            if _is_coded_filter_column(name, info):
                add_column(name, info)
        for name, info in columns.items():
            data_type = str(info.get("type") or "").lower()
            if "date" in data_type or "time" in data_type:
                add_column(name, info)
        for name, info in columns.items():
            add_column(name, info)

        column_parts = []
        for name, info in selected_columns:
            description = _compact_text(info.get("description"), 140)
            if description:
                column_parts.append(f"{name}: {description}")
            else:
                column_parts.append(name)
        if column_parts:
            parts.append("Important columns: " + "; ".join(column_parts))

    return _compact_text(". ".join(_dedupe_text_parts(parts)), max_chars)


def _read_yaml_document(name: str, content: bytes | str) -> dict:
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else str(content)
        data = yaml.safe_load(text) or {}
    except Exception as exc:  # pylint: disable=broad-exception-caught
        raise YAMLMetadataError(f"Failed to parse YAML file {name}") from exc
    if not isinstance(data, dict):
        raise YAMLMetadataError(f"YAML file {name} must contain a mapping")
    return data


def _iter_yaml_paths(path: str) -> list[Path]:
    metadata_path = Path(path).expanduser()
    if metadata_path.is_dir():
        files = sorted(
            list(metadata_path.glob("*.yml")) + list(metadata_path.glob("*.yaml"))
        )
    elif metadata_path.is_file():
        files = [metadata_path]
    else:
        raise YAMLMetadataError(f"YAML metadata path does not exist: {metadata_path}")

    if not files:
        raise YAMLMetadataError(f"No YAML files found in {metadata_path}")
    return files


def _documents_from_paths(path: str) -> list[tuple[str, bytes]]:
    documents = []
    for yaml_path in _iter_yaml_paths(path):
        documents.append((yaml_path.name, yaml_path.read_bytes()))
    return documents


def _short_table_name(name: str) -> str:
    return str(name or "").strip().lower().rsplit(".", 1)[-1]


def _resolve_names_to_graph(
    entities: dict[str, Any],
    relationships: dict[str, list[dict[str, str]]],
    existing: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, list[dict[str, str]]]]:
    """Re-key YAML tables onto the canonical names of the live graph.

    A schema prefix (dm_mis.*, core_tmp.*) names the environment the YAML
    files were authored against, not the table itself; the graph indexed
    from the executable database is the fact. Tables are matched by short
    name; a short name that is ambiguous in the graph (present in several
    schemas) is matched only exactly.
    """
    if not existing:
        return entities, relationships

    by_short: dict[str, list[str]] = {}
    for graph_name in existing:
        by_short.setdefault(_short_table_name(graph_name), []).append(graph_name)

    def canonical(name: str) -> str:
        raw = str(name or "")
        if not raw or raw in existing:
            return raw
        candidates = by_short.get(_short_table_name(raw)) or []
        if len(candidates) == 1:
            return candidates[0]
        return raw

    renamed = 0
    resolved_entities: dict[str, Any] = {}
    for yaml_name, info in entities.items():
        target = canonical(yaml_name)
        if target != yaml_name:
            renamed += 1
        for fk in info.get("foreign_keys", []) or []:
            ref = fk.get("referenced_table")
            if ref:
                fk["referenced_table"] = canonical(ref)
        resolved_entities[target] = info

    resolved_relationships: dict[str, list[dict[str, str]]] = {}
    for rel_name, rels in (relationships or {}).items():
        fixed: list[dict[str, str]] = []
        for rel in rels:
            rel = dict(rel)
            if rel.get("from"):
                rel["from"] = canonical(rel["from"])
            if rel.get("to"):
                rel["to"] = canonical(rel["to"])
            fixed.append(rel)
        resolved_relationships[canonical(rel_name)] = fixed

    if renamed:
        logging.info(
            "YAML merge: %d tables re-qualified to live graph names "
            "(schema prefix in YAML is advisory; the database is the fact)",
            renamed,
        )
    return resolved_entities, resolved_relationships


def _filter_invalid_relationships(
    entities: dict[str, Any],
    relationships: dict[str, list[dict[str, str]]],
) -> tuple[dict[str, list[dict[str, str]]], list[dict[str, str]]]:
    """Keep only FK relationships whose source and target columns exist."""
    valid_relationships: dict[str, list[dict[str, str]]] = {}
    skipped: list[dict[str, str]] = []
    corrections: dict[tuple[str, str, str, str], str] = {}

    valid_pairs = set()
    seen_pairs: set[tuple[str, str, str, str]] = set()
    for rel_name in sorted(relationships):
        rels = relationships[rel_name]
        for rel in rels:
            original_pair = (
                rel["from"],
                rel["source_column"],
                rel["to"],
                rel["target_column"],
            )
            source_columns = entities.get(rel["from"], {}).get("columns", {})
            target_columns = entities.get(rel["to"], {}).get("columns", {})
            if (
                rel["source_column"] in source_columns
                and rel["target_column"] not in target_columns
                and target_columns
            ):
                repaired_column = _repair_missing_fk_target(rel, source_columns, target_columns)
                if repaired_column:
                    logging.info(
                        "YAML FK target repaired: %s.%s -> %s.%s changed target %s -> %s",
                        rel["from"],
                        rel["source_column"],
                        rel["to"],
                        repaired_column,
                        rel["target_column"],
                        repaired_column,
                    )
                    corrections[original_pair] = repaired_column
                    rel = dict(rel)
                    rel["target_column"] = repaired_column
                    rel["note"] = (
                        f"{rel.get('note', '')}; YAML target column repaired "
                        f"from {original_pair[3]} to {repaired_column}"
                    ).strip("; ")

            pair = (
                rel["from"],
                rel["source_column"],
                rel["to"],
                rel["target_column"],
            )
            if pair in seen_pairs:
                continue
            is_valid = (
                rel["source_column"] in source_columns
                and rel["target_column"] in target_columns
            )
            if is_valid:
                seen_pairs.add(pair)
                valid_relationships.setdefault(rel_name, []).append(rel)
                valid_pairs.add(pair)
            else:
                skipped.append(rel)

    for table_name, table_info in entities.items():
        filtered_fks: list[dict[str, str]] = []
        for fk in table_info.get("foreign_keys", []) or []:
            fixed_fk = dict(fk)
            original_pair = (
                table_name,
                fk.get("column"),
                fk.get("referenced_table"),
                fk.get("referenced_column"),
            )
            if original_pair in corrections:
                fixed_fk["referenced_column"] = corrections[original_pair]
                fixed_fk["note"] = (
                    f"{fixed_fk.get('note', '')}; YAML target column repaired "
                    f"from {original_pair[3]} to {fixed_fk['referenced_column']}"
                ).strip("; ")
            fixed_pair = (
                table_name,
                fixed_fk.get("column"),
                fixed_fk.get("referenced_table"),
                fixed_fk.get("referenced_column"),
            )
            if fixed_pair in valid_pairs:
                filtered_fks.append(fixed_fk)
        table_info["foreign_keys"] = filtered_fks

    return valid_relationships, skipped


def _type_family(data_type: Any) -> str:
    text = str(data_type or "").strip().lower()
    text = _TYPE_LENGTH_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _semantic_tokens(*parts: Any) -> set[str]:
    text = " ".join(str(part or "").lower() for part in parts)
    return {
        token
        for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9_]+", text)
        if len(token) >= 2
    }


def _single_primary_key(target_columns: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    pk_columns = [
        (column_name, column_info)
        for column_name, column_info in target_columns.items()
        if str(column_info.get("key") or "").upper() in {"PRIMARY KEY", "PK", "PRI"}
    ]
    if len(pk_columns) == 1:
        return pk_columns[0]
    return None


def _repair_missing_fk_target(
    rel: dict[str, str],
    source_columns: dict[str, Any],
    target_columns: dict[str, Any],
) -> str | None:
    """Repair a YAML FK target only when a single target PK is an obvious match."""
    pk = _single_primary_key(target_columns)
    if not pk:
        return None

    source_column = source_columns.get(rel["source_column"], {})
    target_column_name, target_column = pk
    source_type = _type_family(source_column.get("type"))
    target_type = _type_family(target_column.get("type"))
    if source_type and target_type and source_type != target_type:
        return None

    source_tokens = _semantic_tokens(rel["source_column"], source_column.get("description"))
    target_tokens = _semantic_tokens(target_column_name, target_column.get("description"))
    if not source_tokens or not target_tokens:
        return None

    overlap = source_tokens & target_tokens
    required = max(1, min(len(source_tokens), len(target_tokens)) // 2)
    if len(overlap) >= required:
        return target_column_name
    return None


def _finalize_entity_descriptions(entities: dict[str, Any]) -> None:
    """Rebuild compact descriptions after FK validation so text and edges agree."""
    for table_name, table_info in entities.items():
        valid_targets_by_column: dict[str, list[tuple[str, str]]] = {}
        for fk in table_info.get("foreign_keys", []) or []:
            column_name = str(fk.get("column") or "")
            target_table = str(fk.get("referenced_table") or "")
            target_column = str(fk.get("referenced_column") or "")
            if column_name and target_table and target_column:
                valid_targets_by_column.setdefault(column_name, []).append(
                    (target_table, target_column)
                )

        for column_name, column_info in table_info.get("columns", {}).items():
            types = set(column_info.pop("_constraint_types", []) or [])
            base_description = column_info.pop("_base_description", None)
            if base_description is None:
                base_description = column_info.get("description", "")
            fk_targets = valid_targets_by_column.get(column_name, [])
            column_info["key"] = _column_key_type(types, bool(fk_targets))
            column_info["description"] = _column_description(
                {"description": base_description},
                column_name,
                column_info.get("type", "unknown"),
                types,
                fk_targets,
            )

        table_info["col_descriptions"] = [
            column_info["description"]
            for column_info in table_info.get("columns", {}).values()
        ]
        if _is_generated_table_description(table_info.get("description"), table_name):
            table_info["description"] = _auto_table_description(table_name, table_info)


def _is_sample_candidate(column_name: str, column_info: dict[str, Any]) -> bool:
    """Pick compact value examples by type and name, not domain word lists."""
    key_type = str(column_info.get("key") or "").upper()
    if key_type in {"PRIMARY KEY", "PRI", "PK"}:
        return False

    data_type = str(column_info.get("type") or "").strip().lower()
    # Same candidate rule as index-time sampling: filter-like types only,
    # identifier-named columns (...id/guid/uuid/hash) excluded.
    if not BaseLoader.is_sample_candidate_column(column_name, data_type):
        return False

    if any(token in data_type for token in ("char", "string", "text")):
        match = _TYPE_LENGTH_RE.search(data_type)
        length = int(match.group(1)) if match else 64
        return length <= _bounded_int_env("YAML_SAMPLE_TEXT_MAX_LENGTH", 80, 1, 512)

    return any(token in data_type for token in ("boolean", "bool", "bit", "tinyint", "smallint"))


def _quote_sql_identifier(identifier: str, quote_char: str) -> str:
    normalized = _normalize_identifier(identifier)
    if not _IDENTIFIER_RE.match(normalized):
        return ""
    return f"{quote_char}{normalized}{quote_char}"


def _quote_sql_qualified_name(name: str, db_type: str) -> str:
    quote_char = "`" if db_type in {"impala", "mysql"} else '"'
    parts = [_quote_sql_identifier(part, quote_char) for part in str(name or "").split(".")]
    if not parts or any(not part for part in parts):
        return ""
    return ".".join(parts)


def _loader_for_execute_url(execute_url: str):
    url = str(execute_url or "").strip().lower()
    if url.startswith(("impala://", "impala+http://")):
        from api.loaders.impala_loader import ImpalaLoader  # pylint: disable=import-outside-toplevel

        return "impala", ImpalaLoader
    if url.startswith(("postgresql://", "postgres://")):
        from api.loaders.postgres_loader import PostgresLoader  # pylint: disable=import-outside-toplevel

        return "postgresql", PostgresLoader
    if url.startswith("mysql://"):
        from api.loaders.mysql_loader import MySQLLoader  # pylint: disable=import-outside-toplevel

        return "mysql", MySQLLoader
    if url.startswith("snowflake://"):
        from api.loaders.snowflake_loader import SnowflakeLoader  # pylint: disable=import-outside-toplevel

        return "snowflake", SnowflakeLoader
    return None, None


def _extract_sample_values(rows: list[Any], limit: int) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    max_len = _bounded_int_env("YAML_SAMPLE_VALUE_MAX_CHARS", 60, 8, 240)
    for row in rows or []:
        if isinstance(row, dict):
            raw_value = next(iter(row.values()), None)
        elif isinstance(row, (list, tuple)) and row:
            raw_value = row[0]
        else:
            raw_value = row
        value = _compact_text(raw_value, max_len)
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
        if len(values) >= limit:
            break
    return values


def _enrich_entities_with_samples(entities: dict[str, Any], execute_url: str | None) -> None:
    """Persist most-frequent value examples in graph metadata when DB is reachable.

    Candidates (filter-like columns) are collected first, then fetched in a
    thread pool: every execute_sql_query call opens its own connection, so
    per-column previews run concurrently instead of one by one.
    """
    if not execute_url or not _truthy(os.getenv("YAML_LOAD_VALUE_SAMPLING_ENABLED", "true")):
        return

    db_type, loader_class = _loader_for_execute_url(execute_url)
    if not db_type or not loader_class:
        return

    sample_limit = _bounded_int_env("YAML_LOAD_SAMPLE_LIMIT", 10, 1, 10)
    max_columns = _bounded_int_env("YAML_LOAD_SAMPLE_MAX_COLUMNS", 200, 0, 1000)
    max_per_table = _bounded_int_env("YAML_LOAD_SAMPLE_MAX_COLUMNS_PER_TABLE", 8, 0, 50)
    concurrency = _bounded_int_env("QW_SAMPLE_CONCURRENCY", 10, 1, 32)
    if max_columns <= 0 or max_per_table <= 0:
        return

    candidates: list[tuple[str, str, dict[str, Any], str]] = []
    for table_name, table_info in entities.items():
        if len(candidates) >= max_columns:
            break
        table_ref = _quote_sql_qualified_name(table_name, db_type)
        if not table_ref:
            continue
        in_table = 0
        for column_name, column_info in table_info.get("columns", {}).items():
            if len(candidates) >= max_columns or in_table >= max_per_table:
                break
            if not _is_sample_candidate(column_name, column_info):
                continue
            column_ref = _quote_sql_identifier(column_name, "`" if db_type in {"impala", "mysql"} else '"')
            if not column_ref:
                continue
            # Most-frequent values first: an arbitrary DISTINCT slice surfaces
            # noise rows while the dominant real codes (the ones users filter
            # by) stay invisible to the model.
            sql_query = (
                f"SELECT {column_ref} AS sample_value, COUNT(*) AS cnt "
                f"FROM {table_ref} "
                f"WHERE {column_ref} IS NOT NULL "
                f"GROUP BY {column_ref} "
                f"ORDER BY cnt DESC "
                f"LIMIT {sample_limit}"
            )
            candidates.append((table_name, column_name, column_info, sql_query))
            in_table += 1

    sampled_columns = 0
    if candidates:
        def _fetch_one(item: tuple[str, str, dict[str, Any], str]) -> tuple[dict[str, Any], list[str]]:
            c_table, c_column, c_info, c_sql = item
            try:
                rows = loader_class.execute_sql_query(c_sql, execute_url)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logging.info(
                    "YAML load sample skipped: table=%s column=%s error=%s",
                    c_table,
                    c_column,
                    _compact_text(exc, 220),
                )
                return c_info, []
            return c_info, _extract_sample_values(rows, sample_limit)

        with ThreadPoolExecutor(max_workers=min(concurrency, len(candidates))) as pool:
            for column_info, values in pool.map(_fetch_one, candidates):
                if not values:
                    continue
                column_info["sample_values"] = values
                sampled_columns += 1

    logging.info(
        "YAML load value sampling completed: db_type=%s sampled_columns=%d (parallel=%d)",
        db_type,
        sampled_columns,
        min(concurrency, max(len(candidates), 1)),
    )


class YamlSchemaLoader(BaseLoader):
    """Build a schema graph from dbt-style YAML model files."""

    SCHEMA_MODIFYING_OPERATIONS = {"CREATE", "ALTER", "DROP", "RENAME", "TRUNCATE"}

    @staticmethod
    def _execute_sample_query(
        cursor: Any, table_name: str, col_name: str, sample_size: int = 3
    ) -> List[Any]:
        return []

    @staticmethod
    def parse_metadata_url(metadata_url: str) -> dict[str, Any]:
        parsed = urlparse(metadata_url)
        params = parse_qs(parsed.query)
        path = unquote(parsed.path or "")
        if parsed.netloc and not path.startswith("/"):
            path = f"/{parsed.netloc}/{path}"
        elif parsed.netloc and not path:
            path = f"/{parsed.netloc}"

        database = (
            params.get("graph", [None])[0]
            or params.get("db", [None])[0]
            or params.get("database", [None])[0]
        )
        if not database:
            path_name = Path(path).name if path else "yaml_database"
            database = path_name or "yaml_database"

        schema = params.get("schema", [None])[0] or params.get("db_schema", [None])[0]
        execute_url = params.get("execute_url", [None])[0] or params.get("db_url", [None])[0]

        return {
            "path": path,
            "database": _normalize_identifier(database),
            "schema": _normalize_identifier(schema) if schema else None,
            "execute_url": execute_url,
            "replace": _truthy(params.get("replace", ["false"])[0]),
        }

    @staticmethod
    def build_graph_data(
        documents: Iterable[tuple[str, bytes | str]],
        schema: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, str]]]]:
        """Convert YAML documents into graph-loader entities and relationships."""
        entities: dict[str, Any] = {}
        relationships: dict[str, list[dict[str, str]]] = {}

        for file_name, content in documents:
            data = _read_yaml_document(file_name, content)
            models = data.get("models", []) or []
            if not isinstance(models, list):
                raise YAMLMetadataError(f"YAML file {file_name}: models must be a list")

            for model in models:
                if not isinstance(model, dict):
                    continue
                raw_table_name = model.get("name")
                if not raw_table_name:
                    raise YAMLMetadataError(f"YAML file {file_name}: model without name")

                table_name = _normalize_table_name(raw_table_name, schema)
                columns_info: dict[str, Any] = {}
                foreign_keys: list[dict[str, str]] = []

                columns = model.get("columns", []) or []
                if not isinstance(columns, list):
                    raise YAMLMetadataError(
                        f"YAML file {file_name}, model {raw_table_name}: columns must be a list"
                    )

                for column in columns:
                    if not isinstance(column, dict):
                        continue
                    raw_column_name = column.get("name")
                    if not raw_column_name:
                        raise YAMLMetadataError(
                            f"YAML file {file_name}, model {raw_table_name}: column without name"
                        )

                    column_name = _normalize_identifier(raw_column_name)
                    data_type = str(column.get("data_type") or "unknown").strip()
                    types = _constraint_types(column)

                    fk_targets: list[tuple[str, str]] = []
                    for constraint in column.get("constraints", []) or []:
                        if not isinstance(constraint, dict):
                            continue
                        if str(constraint.get("type", "")).strip().lower() != "foreign_key":
                            continue
                        for expression in constraint.get("expressions", []) or []:
                            parsed_fk = _parse_fk_expression(expression, schema)
                            if parsed_fk:
                                fk_targets.append(parsed_fk)

                    base_description = _strip_fk_text(_compact_text(
                        column.get("description"),
                        _bounded_int_env("YAML_COLUMN_DESCRIPTION_MAX_CHARS", 600, 80, 5000),
                    ))
                    description = _column_description(
                        {"description": base_description},
                        column_name,
                        data_type,
                        types,
                        [],
                    )
                    columns_info[column_name] = {
                        "type": data_type,
                        "null": _column_nullable(types),
                        "key": _column_key_type(types, False),
                        "description": description,
                        "default": None,
                        "sample_values": [],
                        "_constraint_types": sorted(types),
                        "_base_description": base_description,
                    }

                    for target_index, (target_table, target_column) in enumerate(fk_targets, start=1):
                        constraint_name = (
                            f"fk_{table_name.replace('.', '_')}_{column_name}_"
                            f"{target_table.replace('.', '_')}_{target_column}_{target_index}"
                        )
                        foreign_keys.append({
                            "constraint_name": constraint_name,
                            "column": column_name,
                            "referenced_table": target_table,
                            "referenced_column": target_column,
                        })
                        relationships.setdefault(constraint_name, []).append({
                            "from": table_name,
                            "to": target_table,
                            "source_column": column_name,
                            "target_column": target_column,
                            "note": f"Foreign key from YAML metadata: {constraint_name}",
                        })

                entities[table_name] = {
                    "description": _table_description(model, table_name, columns_info),
                    "columns": columns_info,
                    "foreign_keys": foreign_keys,
                    "col_descriptions": [
                        column_info["description"] for column_info in columns_info.values()
                    ],
                }

        if not entities:
            raise YAMLMetadataError("YAML metadata did not contain any models")

        return entities, relationships

    @staticmethod
    async def _ensure_graph_can_load(graph_id: str, replace: bool, db=None) -> None:
        from api.core.db_resolver import resolve_db  # pylint: disable=import-outside-toplevel

        graph = resolve_db(db).select_graph(graph_id)
        try:
            result = await graph.query("MATCH (d:Database) RETURN count(d)")
            existing = bool(result.result_set and int(result.result_set[0][0]) > 0)
        except Exception:  # pylint: disable=broad-exception-caught
            existing = False

        if existing and not replace:
            raise YAMLMetadataError(
                f"Graph {graph_id} already exists; pass replace=true to reload it"
            )
        if existing and replace:
            await graph.delete()

    @staticmethod
    async def _graph_exists(graph_id: str, db=None) -> bool:
        from api.core.db_resolver import resolve_db  # pylint: disable=import-outside-toplevel

        graph = resolve_db(db).select_graph(graph_id)
        try:
            result = await graph.query("MATCH (d:Database) RETURN count(d)")
            return bool(result.result_set and int(result.result_set[0][0]) > 0)
        except Exception:  # pylint: disable=broad-exception-caught
            return False

    @staticmethod
    async def _existing_database_url(graph_id: str, db=None) -> str:
        from api.core.db_resolver import resolve_db  # pylint: disable=import-outside-toplevel

        graph = resolve_db(db).select_graph(graph_id)
        try:
            result = await graph.query("MATCH (d:Database) RETURN d.url LIMIT 1")
            if result.result_set and result.result_set[0]:
                return str(result.result_set[0][0] or "")
        except Exception:  # pylint: disable=broad-exception-caught
            logging.info("Could not read existing graph execute URL: graph=%s", graph_id)
        return ""

    @staticmethod
    async def _existing_graph_entities(graph_id: str, db=None) -> dict[str, Any]:
        """Return existing table/column names for validating partial YAML merges."""
        from api.core.db_resolver import resolve_db  # pylint: disable=import-outside-toplevel

        graph = resolve_db(db).select_graph(graph_id)
        try:
            result = await graph.query(
                """
                MATCH (c:Column)-[:BELONGS_TO]->(t:Table)
                RETURN t.name, c.name, c.type, c.description, c.key_type
                """
            )
        except Exception:  # pylint: disable=broad-exception-caught
            logging.info("Could not read existing graph schema for YAML merge: graph=%s", graph_id)
            return {}

        entities: dict[str, Any] = {}
        for row in result.result_set or []:
            if len(row) < 2 or not row[0] or not row[1]:
                continue
            table_name = str(row[0])
            column_name = str(row[1])
            entities.setdefault(table_name, {"columns": {}, "foreign_keys": []})
            entities[table_name]["columns"][column_name] = {
                "type": row[2] if len(row) > 2 else "unknown",
                "description": row[3] if len(row) > 3 else "",
                "key": row[4] if len(row) > 4 else "unknown",
            }
        return entities

    @staticmethod
    async def _merge_graph_data(  # pylint: disable=too-many-arguments
        graph_id: str,
        entities: dict[str, Any],
        relationships: dict[str, list[dict[str, str]]],
        db_name: str,
        execute_url: str,
        db=None,
        existing_entities: dict[str, Any] | None = None,
    ) -> None:
        """Merge YAML metadata into an existing graph without duplicating nodes.

        Thin compatibility wrapper around the canonical, loader-agnostic
        :func:`api.loaders.graph_merge.merge_graph_data`. The mutation engine was
        lifted into ``graph_merge`` so the agent loader can import the same
        writer instead of reaching for a ``@staticmethod`` on this class; this
        method is kept so existing call sites (``load_documents``) and any
        external imports keep working unchanged. Behaviour is identical.
        """
        from api.loaders.graph_merge import merge_graph_data  # pylint: disable=import-outside-toplevel

        await merge_graph_data(
            graph_id,
            entities,
            relationships,
            db_name=db_name,
            execute_url=execute_url,
            db=db,
            existing_entities=existing_entities,
        )

    @staticmethod
    async def load_documents(  # pylint: disable=too-many-arguments
        prefix: str,
        documents: Iterable[tuple[str, bytes | str]],
        database: str,
        schema: str | None = None,
        execute_url: str | None = None,
        replace: bool = False,
        db=None,
    ) -> AsyncGenerator[tuple[bool, str], None]:
        """Load schema graph from in-memory YAML documents."""
        try:
            db_name = _normalize_identifier(database)
            schema_name = _normalize_identifier(schema or database)
            if schema_name != db_name:
                raise YAMLMetadataError(
                    "YAML import expects one graph per database: "
                    "database and SQL schema names must match."
                )
            graph_id = f"{prefix}_{db_name}"
            existing_graph = await YamlSchemaLoader._graph_exists(graph_id, db=db)
            existing_url = await YamlSchemaLoader._existing_database_url(graph_id, db=db) if existing_graph else ""
            effective_execute_url = execute_url or existing_url
            preserved_user_rules = ""
            preserved_knowledge_spec = ""
            if replace and existing_graph:
                try:
                    from api.graph import get_knowledge, get_user_rules  # pylint: disable=import-outside-toplevel

                    preserved_user_rules = await get_user_rules(graph_id, db=db)
                    preserved_knowledge_spec = await get_knowledge(graph_id, db=db)
                except Exception:  # pylint: disable=broad-exception-caught
                    logging.info(
                        "No existing graph rules/knowledge preserved before YAML replace: graph=%s",
                        graph_id,
                    )

            yield True, "Parsing YAML metadata files..."
            entities, relationships = YamlSchemaLoader.build_graph_data(
                documents, schema=schema_name
            )
            validation_entities = entities
            existing_entities: dict[str, Any] = {}
            if existing_graph and not replace:
                existing_entities = await YamlSchemaLoader._existing_graph_entities(graph_id, db=db)
                entities, relationships = _resolve_names_to_graph(
                    entities, relationships, existing_entities
                )
                validation_entities = {**existing_entities, **entities}
            relationships, skipped_relationships = _filter_invalid_relationships(
                validation_entities, relationships
            )
            _finalize_entity_descriptions(entities)
            if effective_execute_url:
                yield True, "Sampling compact filter values from executable database..."
                _enrich_entities_with_samples(entities, effective_execute_url)

            if existing_graph and not replace:
                yield True, "Merging YAML metadata into existing graph..."
                await YamlSchemaLoader._merge_graph_data(
                    graph_id,
                    entities,
                    relationships,
                    db_name=db_name,
                    execute_url=effective_execute_url,
                    db=db,
                    existing_entities=existing_entities,
                )
                yield True, (
                    f"YAML metadata merged successfully. Updated {len(entities)} tables "
                    f"and {sum(len(items) for items in relationships.values())} valid relationships."
                    + (
                        f" Skipped {len(skipped_relationships)} invalid FK references."
                        if skipped_relationships else ""
                    )
                )
                return

            await YamlSchemaLoader._ensure_graph_can_load(graph_id, replace, db=db)

            yield True, "Loading YAML metadata into graph..."
            from api.loaders.graph_loader import load_to_graph  # pylint: disable=import-outside-toplevel

            try:
                await load_to_graph(
                    graph_id,
                    entities,
                    relationships,
                    db_name=db_name,
                    db_url=effective_execute_url or "",
                    db=db,
                    generate_descriptions=False,
                )
            except Exception:  # pylint: disable=broad-exception-caught
                try:
                    from api.core.db_resolver import resolve_db  # pylint: disable=import-outside-toplevel

                    await resolve_db(db).select_graph(graph_id).delete()
                    logging.info("Deleted partial YAML graph after failed load: %s", graph_id)
                except Exception:  # pylint: disable=broad-exception-caught
                    logging.exception(
                        "Failed to delete partial YAML graph after failed load: %s",
                        graph_id,
                    )
                raise
            if preserved_user_rules or preserved_knowledge_spec:
                from api.graph import set_knowledge, set_user_rules  # pylint: disable=import-outside-toplevel

                if preserved_user_rules:
                    await set_user_rules(graph_id, preserved_user_rules, db=db)
                if preserved_knowledge_spec:
                    await set_knowledge(graph_id, preserved_knowledge_spec, db=db)
                logging.info(
                    "Restored graph rules/knowledge after YAML replace: graph=%s "
                    "user_rules_chars=%d knowledge_chars=%d",
                    graph_id,
                    len(preserved_user_rules or ""),
                    len(preserved_knowledge_spec or ""),
                )

            yield True, (
                f"YAML metadata loaded successfully. Found {len(entities)} tables "
                f"and {sum(len(items) for items in relationships.values())} valid relationships."
                + (
                    f" Skipped {len(skipped_relationships)} invalid FK references."
                    if skipped_relationships else ""
                )
            )
        except YAMLMetadataError as exc:
            logging.error("YAML metadata load error: %s", exc)
            yield False, str(exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.exception("Unexpected YAML metadata load error: %s", exc)
            yield False, f"Failed to load YAML metadata: {_safe_error_message(exc)}"

    @staticmethod
    async def load(  # pylint: disable=arguments-differ
        prefix: str,
        metadata_url: str,
        db=None,
    ) -> AsyncGenerator[tuple[bool, str], None]:
        options = YamlSchemaLoader.parse_metadata_url(metadata_url)
        documents = _documents_from_paths(options["path"])
        async for progress in YamlSchemaLoader.load_documents(
            prefix=prefix,
            documents=documents,
            database=options["database"],
            schema=options["schema"] or options["database"],
            execute_url=options["execute_url"],
            replace=options["replace"],
            db=db,
        ):
            yield progress

    @staticmethod
    def is_schema_modifying_query(sql_query: str) -> Tuple[bool, str]:
        if not sql_query or not sql_query.strip():
            return False, ""
        first_word = sql_query.strip().split()[0].upper()
        return first_word in YamlSchemaLoader.SCHEMA_MODIFYING_OPERATIONS, first_word

    @staticmethod
    async def refresh_graph_schema(graph_id: str, db_url: str, db=None) -> Tuple[bool, str]:
        return False, "YAML-only graphs cannot be refreshed without re-uploading metadata files"

    @staticmethod
    def execute_sql_query(sql_query: str, db_url: str) -> List[Dict[str, Any]]:
        raise YAMLMetadataError(
            "This graph was loaded from YAML without an executable database URL"
        )
