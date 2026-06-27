"""Settings and configuration routes for the text2sql API."""

import json
import logging
import os
import ssl
from typing import Any
import urllib.error
import urllib.request

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from litellm import completion

from api.auth.user_management import token_required
from api.routes.tokens import UNAUTHORIZED_RESPONSE
from api.tls import global_ssl_verification_disabled

settings_router = APIRouter(tags=["Settings"])

DEFAULT_COMPLETION_TEMPERATURE = "0.0"
DEFAULT_COMPLETION_MAX_TOKENS = "8000"
DEFAULT_COMPLETION_REASONING = "off"
DEFAULT_COMPLETION_CONTEXT = "128000"
DEFAULT_DISABLE_THINKING_EXTRA_BODY = '{"chat_template_kwargs":{"enable_thinking":false}}'
DEFAULT_COMPLETION_EXTRA_BODY = DEFAULT_DISABLE_THINKING_EXTRA_BODY
DEFAULT_MEMORY_TEMPERATURE = "0.1"
DEFAULT_MEMORY_MAX_TOKENS = "8000"
DEFAULT_MEMORY_REASONING = "off"
DEFAULT_MEMORY_CONTEXT = "128000"
DEFAULT_MEMORY_EXTRA_BODY = DEFAULT_DISABLE_THINKING_EXTRA_BODY
DEFAULT_SCHEMA_RETRIEVAL_TOP_K = "8"
DEFAULT_SCHEMA_TABLE_LIMIT = "12"
DEFAULT_ITERATION_LIMIT = "3"
DEFAULT_SCHEMA_PRUNING_ENABLED = "true"
DEFAULT_SCHEMA_PRIMARY_MAX_COLUMNS = "12"
DEFAULT_SCHEMA_SECONDARY_MAX_COLUMNS = "0"
DEFAULT_VALUE_SAMPLING_ENABLED = "true"
DEFAULT_VALUE_SAMPLE_MAX_COLUMNS = "12"
DEFAULT_VALUE_SAMPLE_MAX_SQLS_PER_COLUMN = "2"
DEFAULT_SQL_QUERY_CACHE_ENABLED = "true"
DEFAULT_SQL_QUERY_CACHE_TTL_SECONDS = "10800"
DEFAULT_SQL_QUERY_CACHE_MAX_ENTRIES = "512"
DEFAULT_SQL_QUERY_CACHE_MAX_ROWS = "1000"

# The schema-search / prompt-budget / reasoning / deployment knobs are FIXED in
# code (the pipeline is deterministic and not user-tunable). The Settings page
# exposes only the model endpoints, memory, and rules, so this list is empty.
ADVANCED_RUNTIME_SETTINGS: list[dict[str, Any]] = [
    {
        "key": "STAGE_DEBUG",
        "label": "Stage debug logging",
        "kind": "bool",
        "default": "true",
        "help": "Log each pipeline stage's input and output (linker / resolver / "
                "generator) to the in-app debug panel. Toggle off to silence.",
    },
]

ADVANCED_RUNTIME_DEFAULTS = {
    item["key"]: str(item["default"]) for item in ADVANCED_RUNTIME_SETTINGS
}


def _sanitize_for_log(value: str) -> str:
    """Remove control characters that could enable log injection."""
    return str(value).replace("\r", "").replace("\n", "").replace("\t", " ")


class ValidateKeyRequest(BaseModel):
    """Request model for API key validation."""
    api_key: str
    vendor: str = "openai"
    model: str = "gpt-3.5-turbo"


class RuntimeModelGroup(BaseModel):
    """Model endpoint settings for one OpenAI-compatible runtime role."""
    model: str
    api_base: str | None = None
    api_key: str | None = None
    temperature: Any | None = None
    max_tokens: Any | None = None
    reasoning: str | None = None
    context: Any | None = None
    extra_body: Any | None = None
    dimensions: Any | None = None


class RuntimeModelsRequest(BaseModel):
    """Request model for deployment-level model settings."""
    completion: RuntimeModelGroup
    memory: RuntimeModelGroup
    # Embeddings are served locally inside the container and are NOT configurable.
    # Accepted for backward compatibility but ignored if present.
    embedding: RuntimeModelGroup | None = None
    schema_retrieval_top_k: Any | None = None
    schema_table_limit: Any | None = None
    schema_pruning_enabled: Any | None = None
    schema_primary_max_columns: Any | None = None
    schema_secondary_max_columns: Any | None = None
    value_sampling_enabled: Any | None = None
    value_sample_max_columns: Any | None = None
    value_sample_max_sqls_per_column: Any | None = None
    sql_query_cache_enabled: Any | None = None
    sql_query_cache_ttl_seconds: Any | None = None
    sql_query_cache_max_entries: Any | None = None
    sql_query_cache_max_rows: Any | None = None
    iteration_limit: Any | None = None
    advanced_settings: dict[str, Any] | None = None
    restart: bool = True


class ContextTestRequest(BaseModel):
    """Request model for probing a completion endpoint context window."""
    role: str = "completion"
    model: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    max_tokens: Any | None = None
    reasoning: str | None = None
    extra_body: Any | None = None
    max_probe_tokens: Any | None = None


class PrefsRequest(BaseModel):
    """Per-user UI preferences, stored in the graph DB (not the browser)."""
    use_memory: bool | None = None
    debug_mode: bool | None = None


def _runtime_settings_supported() -> bool:
    """Settings are writable when the local JSON settings store is writable.

    T2S runs as a local Docker container (no Kubernetes), so writability is
    determined by the persistent data volume, not an in-cluster service account.
    """
    try:
        from api.runtime_settings_store import store_writable  # pylint: disable=import-outside-toplevel
        return store_writable()
    except Exception:  # pylint: disable=broad-except
        return False


def _clean_required_setting(name: str, value: str) -> str:
    clean = (value or "").strip()
    if not clean:
        raise ValueError(f"{name} is required")
    return clean


def _clean_optional_text(value: Any, default: str | None = None) -> str | None:
    clean = str(value or "").strip()
    return clean or default


def _clean_optional_float(
    name: str,
    value: Any,
    min_value: float = 0.0,
    max_value: float = 2.0,
    default: str | None = None,
) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return default
    try:
        parsed = float(clean)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed < min_value or parsed > max_value:
        raise ValueError(f"{name} must be between {min_value:g} and {max_value:g}")
    return f"{parsed:g}"


def _clean_optional_int(
    name: str,
    value: Any,
    min_value: int = 1,
    max_value: int = 100000,
    default: str | None = None,
) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return default
    try:
        parsed = int(clean)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed < min_value or parsed > max_value:
        raise ValueError(f"{name} must be between {min_value} and {max_value}")
    return str(parsed)


def _clean_optional_bool(value: Any, default: str = "false") -> str:
    clean = str(value or "").strip().lower()
    if not clean:
        return default
    if clean in {"1", "true", "yes", "y", "on"}:
        return "true"
    if clean in {"0", "false", "no", "n", "off"}:
        return "false"
    raise ValueError("Boolean settings must be true or false")


def _json_dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _clean_optional_json_text(
    name: str,
    value: Any,
    default: str = "",
) -> str:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return _json_dumps_compact(value)
    clean = str(value or "").strip()
    if not clean:
        return default
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return _json_dumps_compact(parsed)


def _clean_advanced_setting(definition: dict[str, Any], value: Any) -> str:
    name = str(definition["label"])
    default = str(definition.get("default", ""))
    kind = str(definition.get("kind", "text"))
    if kind == "bool":
        return _clean_optional_bool(value, default)
    if kind == "int":
        return _clean_optional_int(
            name,
            value,
            int(definition.get("min", 0)),
            int(definition.get("max", 2000000)),
            default,
        ) or default
    if kind == "json":
        return _clean_optional_json_text(name, value, default)
    return _clean_optional_text(value, default) or default


def _clean_advanced_settings(values: dict[str, Any] | None) -> dict[str, str]:
    source = values or {}
    cleaned: dict[str, str] = {}
    for definition in ADVANCED_RUNTIME_SETTINGS:
        key = str(definition["key"])
        if key in source:
            cleaned[key] = _clean_advanced_setting(definition, source[key])
    return cleaned


def _setting_value(name: str, *fallback_names: str, default: str = "") -> str:
    for env_name in (name, *fallback_names):
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return default


def _effective_api_base(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _effective_api_key(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _runtime_group(
    role: str,
    model: str,
    api_base: str,
    api_key: str,
    explicit_api_key: str,
    temperature: str = "",
    max_tokens: str = "",
    reasoning: str = "",
    context: str = "",
    extra_body: str = "",
    dimensions: str = "",
) -> dict[str, Any]:
    return {
        "role": role,
        "model": model,
        "api_base": api_base,
        "has_api_key": bool(api_key),
        "has_explicit_api_key": bool(explicit_api_key),
        "api_key_mask": "masked key configured" if api_key else "",
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning": reasoning,
        "context": context,
        "extra_body": extra_body,
        "dimensions": dimensions,
    }


def _runtime_models_response() -> dict[str, Any]:
    openai_base = _effective_api_base("OPENAI_BASE_URL", "OPENAI_API_BASE")
    openai_key = _effective_api_key("OPENAI_API_KEY")
    completion_key = _effective_api_key("COMPLETION_API_KEY", "OPENAI_API_KEY")
    memory_key = _effective_api_key("MEMORY_API_KEY", "OPENAI_API_KEY")

    return {
        "completion": _runtime_group(
            "completion",
            os.getenv("COMPLETION_MODEL", ""),
            _effective_api_base("COMPLETION_API_BASE", "COMPLETION_BASE_URL") or openai_base,
            completion_key,
            os.getenv("COMPLETION_API_KEY", ""),
            _setting_value("COMPLETION_TEMPERATURE", default=DEFAULT_COMPLETION_TEMPERATURE),
            _setting_value("COMPLETION_MAX_TOKENS", default=DEFAULT_COMPLETION_MAX_TOKENS),
            _setting_value("COMPLETION_REASONING", default=DEFAULT_COMPLETION_REASONING),
            _setting_value(
                "COMPLETION_CONTEXT_TOKENS",
                default=DEFAULT_COMPLETION_CONTEXT,
            ),
            _setting_value(
                "COMPLETION_EXTRA_BODY",
                default=DEFAULT_COMPLETION_EXTRA_BODY,
            ),
        ),
        "memory": _runtime_group(
            "memory",
            os.getenv("MEMORY_COMPLETION_MODEL", os.getenv("COMPLETION_MODEL", "")),
            _effective_api_base("MEMORY_API_BASE") or openai_base,
            memory_key,
            os.getenv("MEMORY_API_KEY", ""),
            _setting_value("MEMORY_TEMPERATURE", default=DEFAULT_MEMORY_TEMPERATURE),
            _setting_value("MEMORY_MAX_TOKENS", default=DEFAULT_MEMORY_MAX_TOKENS),
            _setting_value("MEMORY_REASONING", default=DEFAULT_MEMORY_REASONING),
            _setting_value("MEMORY_CONTEXT_TOKENS", default=DEFAULT_MEMORY_CONTEXT),
            _setting_value("MEMORY_EXTRA_BODY", default=DEFAULT_MEMORY_EXTRA_BODY),
        ),
        "embedding": _runtime_group(
            "embedding",
            os.getenv("EMBEDDING_MODEL", ""),
            _effective_api_base("EMBEDDING_API_BASE"),
            _effective_api_key("EMBEDDING_API_KEY"),
            os.getenv("EMBEDDING_API_KEY", ""),
            dimensions=_setting_value("EMBEDDING_DIMENSION", default="1024"),
        ),
        "schema_retrieval_top_k": _setting_value(
            "TABLE_RETRIEVAL_TOP_K",
            default=DEFAULT_SCHEMA_RETRIEVAL_TOP_K,
        ),
        "schema_table_limit": _setting_value(
            "TABLE_CONTEXT_MAX",
            default=DEFAULT_SCHEMA_TABLE_LIMIT,
        ),
        "schema_pruning_enabled": _setting_value(
            "SCHEMA_PRUNING_ENABLED",
            default=DEFAULT_SCHEMA_PRUNING_ENABLED,
        ),
        "schema_primary_max_columns": _setting_value(
            "SCHEMA_PRIMARY_MAX_COLUMNS",
            default=DEFAULT_SCHEMA_PRIMARY_MAX_COLUMNS,
        ),
        "schema_secondary_max_columns": _setting_value(
            "SCHEMA_SECONDARY_MAX_COLUMNS",
            default=DEFAULT_SCHEMA_SECONDARY_MAX_COLUMNS,
        ),
        "value_sampling_enabled": _setting_value(
            "QW_VALUE_SAMPLING_ENABLED",
            default=DEFAULT_VALUE_SAMPLING_ENABLED,
        ),
        "value_sample_max_columns": _setting_value(
            "QW_VALUE_SAMPLE_MAX_COLUMNS",
            default=DEFAULT_VALUE_SAMPLE_MAX_COLUMNS,
        ),
        "value_sample_max_sqls_per_column": _setting_value(
            "QW_VALUE_SAMPLE_MAX_SQLS_PER_COLUMN",
            default=DEFAULT_VALUE_SAMPLE_MAX_SQLS_PER_COLUMN,
        ),
        "sql_query_cache_enabled": _setting_value(
            "SQL_QUERY_CACHE_ENABLED",
            default=DEFAULT_SQL_QUERY_CACHE_ENABLED,
        ),
        "sql_query_cache_ttl_seconds": _setting_value(
            "SQL_QUERY_CACHE_TTL_SECONDS",
            default=DEFAULT_SQL_QUERY_CACHE_TTL_SECONDS,
        ),
        "sql_query_cache_max_entries": _setting_value(
            "SQL_QUERY_CACHE_MAX_ENTRIES",
            default=DEFAULT_SQL_QUERY_CACHE_MAX_ENTRIES,
        ),
        "sql_query_cache_max_rows": _setting_value(
            "SQL_QUERY_CACHE_MAX_ROWS",
            default=DEFAULT_SQL_QUERY_CACHE_MAX_ROWS,
        ),
        "iteration_limit": _setting_value(
            "SQL_HEALING_MAX_ATTEMPTS",
            "LLM_COMPLETION_MAX_ATTEMPTS",
            default=DEFAULT_ITERATION_LIMIT,
        ),
        "advanced_settings": {
            key: _setting_value(key, default=default)
            for key, default in ADVANCED_RUNTIME_DEFAULTS.items()
        },
        "advanced_setting_definitions": ADVANCED_RUNTIME_SETTINGS,
        "openai_api_base": openai_base,
        "has_openai_api_key": bool(openai_key),
        "settings_writable": _runtime_settings_supported(),
    }


@settings_router.get("/runtime-models", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def get_runtime_models(request: Request):
    """Return the requesting user's model settings (applied live, per-user)."""
    try:
        from api.runtime_settings_store import apply_user_settings  # pylint: disable=import-outside-toplevel
        await apply_user_settings(getattr(request.state, "user_id", None) or "default")
    except Exception:  # pylint: disable=broad-except
        pass
    return JSONResponse(content=_runtime_models_response(), status_code=200)


@settings_router.get("/prefs", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def get_prefs(request: Request):
    """Return the requesting user's UI preferences (memory + debug), from the DB."""
    from api.runtime_settings_store import load_settings  # pylint: disable=import-outside-toplevel
    s = await load_settings(getattr(request.state, "user_id", None) or "default")
    return JSONResponse(content={
        "use_memory": str(s.get("USE_MEMORY", "false")).lower() == "true",
        "debug_mode": str(s.get("DEBUG_MODE", "false")).lower() == "true",
    }, status_code=200)


@settings_router.put("/prefs", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def put_prefs(request: Request, data: PrefsRequest):
    """Persist the requesting user's UI preferences (memory + debug) to the DB."""
    from api.runtime_settings_store import save_settings  # pylint: disable=import-outside-toplevel
    updates: dict[str, str] = {}
    if data.use_memory is not None:
        updates["USE_MEMORY"] = "true" if data.use_memory else "false"
    if data.debug_mode is not None:
        updates["DEBUG_MODE"] = "true" if data.debug_mode else "false"
    if updates:
        await save_settings(getattr(request.state, "user_id", None) or "default", updates)
    return JSONResponse(content={"success": True}, status_code=200)


@settings_router.get("/debug-logs", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def get_debug_logs(request: Request):  # pylint: disable=unused-argument
    """Return recent backend log lines for the in-app debug panel."""
    params = request.query_params
    try:
        limit = int(params.get("limit", "300"))
    except (TypeError, ValueError):
        limit = 300
    try:
        after_id = int(params.get("after_id", "0"))
    except (TypeError, ValueError):
        after_id = 0
    level = params.get("level") or None
    try:
        from api.logging_buffer import get_recent_logs  # pylint: disable=import-outside-toplevel
        logs = get_recent_logs(limit=limit, level=level, after_id=after_id,
                               user_id=getattr(request.state, "user_id", None))
    except Exception:  # pylint: disable=broad-except
        logs = []
    return JSONResponse(content={"logs": logs}, status_code=200)


@settings_router.post("/runtime-models", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def update_runtime_models(
    request: Request,  # pylint: disable=unused-argument
    data: RuntimeModelsRequest,
):
    """Persist deployment-level model settings and optionally restart T2S."""
    try:
        completion_model = _clean_required_setting("Completion model", data.completion.model)
        memory_model = _clean_required_setting(
            "Memory model", data.memory.model
        )
        completion_temperature = _clean_optional_float(
            "Completion temperature",
            data.completion.temperature,
            default=DEFAULT_COMPLETION_TEMPERATURE,
        )
        memory_temperature = _clean_optional_float(
            "Memory temperature",
            data.memory.temperature,
            default=DEFAULT_MEMORY_TEMPERATURE,
        )
        completion_max_tokens = _clean_optional_int(
            "Completion max tokens",
            data.completion.max_tokens,
            1,
            100000,
            DEFAULT_COMPLETION_MAX_TOKENS,
        )
        memory_max_tokens = _clean_optional_int(
            "Memory max tokens",
            data.memory.max_tokens,
            1,
            100000,
            DEFAULT_MEMORY_MAX_TOKENS,
        )
        completion_context = _clean_optional_int(
            "Completion context",
            data.completion.context,
            1,
            2000000,
            DEFAULT_COMPLETION_CONTEXT,
        )
        completion_extra_body = _clean_optional_json_text(
            "Completion extra body",
            data.completion.extra_body,
            DEFAULT_COMPLETION_EXTRA_BODY,
        )
        memory_context = _clean_optional_int(
            "Memory context",
            data.memory.context,
            1,
            2000000,
            DEFAULT_MEMORY_CONTEXT,
        )
        memory_extra_body = _clean_optional_json_text(
            "Memory extra body",
            data.memory.extra_body,
            DEFAULT_MEMORY_EXTRA_BODY,
        )
        schema_retrieval_top_k = _clean_optional_int(
            "Schema retrieval top K",
            data.schema_retrieval_top_k,
            1,
            100,
            DEFAULT_SCHEMA_RETRIEVAL_TOP_K,
        )
        schema_table_limit = _clean_optional_int(
            "Schema table limit",
            data.schema_table_limit,
            1,
            200,
            DEFAULT_SCHEMA_TABLE_LIMIT,
        )
        schema_pruning_enabled = _clean_optional_bool(
            data.schema_pruning_enabled,
            DEFAULT_SCHEMA_PRUNING_ENABLED,
        )
        schema_primary_max_columns = _clean_optional_int(
            "Schema primary max columns",
            data.schema_primary_max_columns,
            1,
            200,
            DEFAULT_SCHEMA_PRIMARY_MAX_COLUMNS,
        )
        schema_secondary_max_columns = _clean_optional_int(
            "Schema secondary max columns",
            data.schema_secondary_max_columns,
            0,
            200,
            DEFAULT_SCHEMA_SECONDARY_MAX_COLUMNS,
        )
        value_sampling_enabled = _clean_optional_bool(
            data.value_sampling_enabled,
            DEFAULT_VALUE_SAMPLING_ENABLED,
        )
        value_sample_max_columns = _clean_optional_int(
            "Value sample max columns",
            data.value_sample_max_columns,
            0,
            120,
            DEFAULT_VALUE_SAMPLE_MAX_COLUMNS,
        )
        value_sample_max_sqls_per_column = _clean_optional_int(
            "Value sample max SQLs per column",
            data.value_sample_max_sqls_per_column,
            1,
            20,
            DEFAULT_VALUE_SAMPLE_MAX_SQLS_PER_COLUMN,
        )
        sql_query_cache_enabled = _clean_optional_bool(
            data.sql_query_cache_enabled,
            DEFAULT_SQL_QUERY_CACHE_ENABLED,
        )
        sql_query_cache_ttl_seconds = _clean_optional_int(
            "SQL query cache TTL seconds",
            data.sql_query_cache_ttl_seconds,
            1,
            86400,
            DEFAULT_SQL_QUERY_CACHE_TTL_SECONDS,
        )
        sql_query_cache_max_entries = _clean_optional_int(
            "SQL query cache max entries",
            data.sql_query_cache_max_entries,
            1,
            10000,
            DEFAULT_SQL_QUERY_CACHE_MAX_ENTRIES,
        )
        sql_query_cache_max_rows = _clean_optional_int(
            "SQL query cache max rows",
            data.sql_query_cache_max_rows,
            1,
            100000,
            DEFAULT_SQL_QUERY_CACHE_MAX_ROWS,
        )
        iteration_limit = _clean_optional_int(
            "Iteration limit",
            data.iteration_limit,
            1,
            10,
            DEFAULT_ITERATION_LIMIT,
        )
        advanced_settings = _clean_advanced_settings(data.advanced_settings)
    except ValueError as exc:
        return JSONResponse(content={"error": str(exc)}, status_code=400)

    config_data: dict[str, str | None] = {
        "COMPLETION_MODEL": completion_model,
        "MEMORY_COMPLETION_MODEL": memory_model,
        "COMPLETION_TEMPERATURE": completion_temperature,
        "COMPLETION_MAX_TOKENS": completion_max_tokens,
        "COMPLETION_REASONING": _clean_optional_text(
            data.completion.reasoning,
            DEFAULT_COMPLETION_REASONING,
        ),
        "COMPLETION_CONTEXT_TOKENS": completion_context,
        "COMPLETION_EXTRA_BODY": completion_extra_body,
        "MEMORY_TEMPERATURE": memory_temperature,
        "MEMORY_MAX_TOKENS": memory_max_tokens,
        "MEMORY_REASONING": _clean_optional_text(
            data.memory.reasoning,
            DEFAULT_MEMORY_REASONING,
        ),
        "MEMORY_CONTEXT_TOKENS": memory_context,
        "MEMORY_EXTRA_BODY": memory_extra_body,
        "TABLE_RETRIEVAL_TOP_K": schema_retrieval_top_k,
        "TABLE_CONTEXT_MAX": schema_table_limit,
        "SCHEMA_PRUNING_ENABLED": schema_pruning_enabled,
        "SCHEMA_PRIMARY_MAX_COLUMNS": schema_primary_max_columns,
        "SCHEMA_SECONDARY_MAX_COLUMNS": schema_secondary_max_columns,
        "QW_VALUE_SAMPLING_ENABLED": value_sampling_enabled,
        "QW_VALUE_SAMPLE_MAX_COLUMNS": value_sample_max_columns,
        "QW_VALUE_SAMPLE_MAX_SQLS_PER_COLUMN": value_sample_max_sqls_per_column,
        "SQL_QUERY_CACHE_ENABLED": sql_query_cache_enabled,
        "SQL_QUERY_CACHE_TTL_SECONDS": sql_query_cache_ttl_seconds,
        "SQL_QUERY_CACHE_MAX_ENTRIES": sql_query_cache_max_entries,
        "SQL_QUERY_CACHE_MAX_ROWS": sql_query_cache_max_rows,
        "SQL_HEALING_MAX_ATTEMPTS": iteration_limit,
        "LLM_COMPLETION_MAX_ATTEMPTS": iteration_limit,
    }
    config_data.update(advanced_settings)

    completion_api_base = (data.completion.api_base or "").strip()
    if completion_api_base:
        config_data["COMPLETION_API_BASE"] = completion_api_base

    memory_api_base = (data.memory.api_base or "").strip()
    if memory_api_base:
        config_data["MEMORY_API_BASE"] = memory_api_base

    secret_data: dict[str, str] = {}
    completion_api_key = (data.completion.api_key or "").strip()
    if completion_api_key:
        secret_data["COMPLETION_API_KEY"] = completion_api_key
    memory_api_key = (data.memory.api_key or "").strip()
    if memory_api_key:
        secret_data["MEMORY_API_KEY"] = memory_api_key

    # Embedding endpoint (built-in by default, but editable). Persist model /
    # base / key; changing model or dims requires re-indexing the database.
    if data.embedding is not None:
        embed_model = (data.embedding.model or "").strip()
        if embed_model:
            config_data["EMBEDDING_MODEL"] = embed_model
        embed_api_base = (data.embedding.api_base or "").strip()
        if embed_api_base:
            config_data["EMBEDDING_API_BASE"] = embed_api_base
        embed_api_key = (data.embedding.api_key or "").strip()
        if embed_api_key:
            secret_data["EMBEDDING_API_KEY"] = embed_api_key
        embed_dim = str(data.embedding.dimensions or "").strip()
        if embed_dim:
            config_data["EMBEDDING_DIMENSION"] = embed_dim

    try:
        from api.runtime_settings_store import save_settings  # pylint: disable=import-outside-toplevel
        from api.config import Config  # pylint: disable=import-outside-toplevel
        # Persist ONLY the model-endpoint settings locally (the algorithm is
        # fixed). API keys live in the same file on the local-only data volume
        # (chmod 600) and are never returned by GET.
        persist_keys = {
            "COMPLETION_MODEL", "MEMORY_COMPLETION_MODEL",
            "COMPLETION_TEMPERATURE", "COMPLETION_MAX_TOKENS", "COMPLETION_REASONING",
            "COMPLETION_CONTEXT_TOKENS", "COMPLETION_EXTRA_BODY", "COMPLETION_API_BASE",
            "MEMORY_TEMPERATURE", "MEMORY_MAX_TOKENS", "MEMORY_REASONING",
            "MEMORY_CONTEXT_TOKENS", "MEMORY_EXTRA_BODY", "MEMORY_API_BASE",
            "COMPLETION_API_KEY", "MEMORY_API_KEY",
            "EMBEDDING_MODEL", "EMBEDDING_API_BASE", "EMBEDDING_API_KEY", "EMBEDDING_DIMENSION",
        }
        persisted = {
            key: value
            for key, value in {**config_data, **secret_data}.items()
            if key in persist_keys and value is not None
        }
        user_id = getattr(request.state, "user_id", None) or "default"
        await save_settings(user_id, persisted)  # per-user, persisted in the graph DB
        Config.apply_runtime_overrides(persisted)  # apply live, no restart
    except Exception:  # pylint: disable=broad-except
        logging.exception("Failed to persist runtime model settings")
        return JSONResponse(
            content={"error": "Failed to save runtime model settings to the local store."},
            status_code=500,
        )

    response = _runtime_models_response()
    response.update(
        {
            "completion": {
                **response["completion"],
                "model": completion_model,
                "api_base": completion_api_base or response["completion"]["api_base"],
                "has_api_key": response["completion"]["has_api_key"] or bool(completion_api_key),
                "has_explicit_api_key": response["completion"]["has_explicit_api_key"] or bool(completion_api_key),
                "temperature": completion_temperature or "",
                "max_tokens": completion_max_tokens or "",
                "reasoning": _clean_optional_text(
                    data.completion.reasoning,
                    DEFAULT_COMPLETION_REASONING,
                ) or "",
                "context": completion_context or "",
                "extra_body": completion_extra_body or "",
            },
            "memory": {
                **response["memory"],
                "model": memory_model,
                "api_base": memory_api_base or response["memory"]["api_base"],
                "has_api_key": response["memory"]["has_api_key"] or bool(memory_api_key),
                "has_explicit_api_key": response["memory"]["has_explicit_api_key"] or bool(memory_api_key),
                "temperature": memory_temperature or "",
                "max_tokens": memory_max_tokens or "",
                "reasoning": _clean_optional_text(
                    data.memory.reasoning,
                    DEFAULT_MEMORY_REASONING,
                ) or "",
                "context": memory_context or "",
                "extra_body": memory_extra_body or "",
            },
            "schema_retrieval_top_k": schema_retrieval_top_k or "",
            "schema_table_limit": schema_table_limit or "",
            "schema_pruning_enabled": schema_pruning_enabled or "",
            "schema_primary_max_columns": schema_primary_max_columns or "",
            "schema_secondary_max_columns": schema_secondary_max_columns or "",
            "value_sampling_enabled": value_sampling_enabled or "",
            "value_sample_max_columns": value_sample_max_columns or "",
            "value_sample_max_sqls_per_column": value_sample_max_sqls_per_column or "",
            "sql_query_cache_enabled": sql_query_cache_enabled or "",
            "sql_query_cache_ttl_seconds": sql_query_cache_ttl_seconds or "",
            "sql_query_cache_max_entries": sql_query_cache_max_entries or "",
            "sql_query_cache_max_rows": sql_query_cache_max_rows or "",
            "iteration_limit": iteration_limit or "",
            "advanced_settings": {
                **response.get("advanced_settings", {}),
                **advanced_settings,
            },
            "restart_scheduled": data.restart,
            "updated_keys": sorted(config_data.keys() | secret_data.keys()),
        }
    )
    return JSONResponse(content=response, status_code=200)


def _maybe_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    clean = str(value or "").strip()
    if not clean:
        return {}
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _reasoning_request_body(reasoning: str | None) -> dict[str, Any]:
    clean = str(reasoning or "").strip()
    if not clean:
        return {}
    lowered = clean.lower()
    if lowered in {"off", "none", "false", "0"}:
        return {"reasoning": {"effort": "none", "exclude": True}}
    if lowered in {"low", "medium", "high"}:
        return {"reasoning": {"effort": lowered}}
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        return {"reasoning": clean}
    return {"reasoning": parsed}


def _https_context() -> ssl.SSLContext | None:
    if global_ssl_verification_disabled():
        return ssl._create_unverified_context()  # type: ignore[attr-defined]
    return None


def _model_info_url(api_base: str) -> str:
    root = api_base.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    return f"{root}/model/info"


# Standard context-window sizes, used to snap a detected value to the intended one.
STANDARD_CONTEXTS = [4096, 8192, 16384, 32768, 65536, 131072, 200000, 262144, 524288, 1048576]


def _snap_context(value: int | None) -> int | None:
    """Snap a detected size to the nearest standard window (within ~6%)."""
    if not value or value <= 0:
        return value
    for std in STANDARD_CONTEXTS:
        if abs(value - std) <= max(256, int(std * 0.06)):
            return std
    return value


def _extract_context_from_model_info(raw: Any, model: str) -> int | None:
    # Ordered by reliability: an explicitly loaded/served window first, then a
    # model max, across vLLM / LM Studio / llama.cpp / litellm field names.
    keys = (
        "loaded_context_length",   # LM Studio: currently loaded window
        "max_model_len",           # vLLM
        "max_context_length",      # LM Studio: model max
        "n_ctx",                   # llama.cpp
        "context_length",
        "max_input_tokens",
        "context_window",
        "max_context_tokens",
        "input_token_limit",
        "max_tokens",
    )

    def read_candidate(item: Any) -> int | None:
        if not isinstance(item, dict):
            return None
        nested = []
        if isinstance(item.get("model_info"), dict):
            nested.append(item["model_info"])
        if isinstance(item.get("litellm_params"), dict):
            nested.append(item["litellm_params"])
        if isinstance(item.get("default_generation_settings"), dict):
            nested.append(item["default_generation_settings"])  # llama.cpp /props
        nested.append(item)
        for candidate in nested:
            for key in keys:
                value = candidate.get(key)
                if value is None:
                    continue
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    return parsed
        return None

    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                names = {
                    str(item.get("id", "")),
                    str(item.get("model_name", "")),
                    str(item.get("model", "")),
                }
                if model in names:
                    found = read_candidate(item)
                    if found:
                        return found
            for item in data:
                found = read_candidate(item)
                if found:
                    return found
        found = read_candidate(raw)
        if found:
            return found
    return None


def _context_metadata_urls(api_base: str) -> list[str]:
    """Endpoints that may declare a model's context window, across server types."""
    base = api_base.rstrip("/")
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base
    return [
        f"{base}/models",         # OpenAI list (vLLM exposes max_model_len here)
        f"{root}/api/v0/models",  # LM Studio native REST (max/loaded_context_length)
        f"{root}/model/info",     # litellm proxy
        f"{root}/props",          # llama.cpp server
    ]


def _fetch_model_info(api_base: str, api_key: str, model: str) -> dict[str, Any]:
    """Read the declared context window from whichever metadata endpoint exists."""
    tried: list[str] = []
    for url in _context_metadata_urls(api_base):
        try:
            request = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                method="GET",
            )
            with urllib.request.urlopen(request, context=_https_context(), timeout=15) as response:
                body = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(body) if body else {}
        except Exception:  # pylint: disable=broad-except
            tried.append(url)
            continue
        ctx = _extract_context_from_model_info(parsed, model)
        if ctx:
            return {"available": True, "context_tokens": ctx, "source": url}
        tried.append(url)
    return {"available": False, "context_tokens": None, "tried": tried}


def _chat_completion_request(
    api_base: str,
    api_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{api_base.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, context=_https_context(), timeout=180) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body) if body else {}


def _probe_context_window(
    api_base: str,
    api_key: str,
    model: str,
    max_completion_tokens: int,
    max_probe_tokens: int,
    reasoning: str,
    extra_body: dict[str, Any],
) -> dict[str, Any]:
    steps = [4096, 8192, 16384, 32768, 65536, 98304, 131072, 196608]
    steps = [step for step in steps if step <= max_probe_tokens]
    if max_probe_tokens not in steps:
        steps.append(max_probe_tokens)
    steps = sorted(set(steps))

    last_success: dict[str, Any] | None = None
    failures: list[dict[str, Any]] = []

    for estimated_tokens in steps:
        filler = " ".join(f"ctx{i}" for i in range(estimated_tokens))
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Return exactly one word: OK\n\n"
                        f"{filler}"
                    ),
                }
            ],
            "temperature": 0,
            "max_tokens": max(1, min(max_completion_tokens, 8)),
        }
        payload.update(extra_body)
        payload.update(_reasoning_request_body(reasoning))

        try:
            result = _chat_completion_request(api_base, api_key, payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            failures.append(
                {
                    "estimated_tokens": estimated_tokens,
                    "status": exc.code,
                    "error": detail,
                }
            )
            break
        except Exception as exc:  # pylint: disable=broad-except
            failures.append(
                {
                    "estimated_tokens": estimated_tokens,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            break

        usage = result.get("usage") or {}
        choice = (result.get("choices") or [{}])[0]
        last_success = {
            "estimated_tokens": estimated_tokens,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "finish_reason": choice.get("finish_reason"),
            "content_preview": (
                (choice.get("message") or {}).get("content") or ""
            )[:120],
        }

    detected = None
    if last_success:
        try:
            detected = int(last_success.get("prompt_tokens") or 0) or None
        except (TypeError, ValueError):
            detected = None
        if detected is None:
            detected = int(last_success["estimated_tokens"])

    return {
        "detected_context_tokens": detected,
        "last_success": last_success,
        "failures": failures,
        "tested_steps": steps,
    }


@settings_router.post("/context-test", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def test_model_context(request: Request, data: ContextTestRequest):  # pylint: disable=unused-argument
    """Probe the configured OpenAI-compatible completion route context window."""
    role = (data.role or "completion").strip().lower()
    if role not in {"completion", "memory"}:
        return JSONResponse(
            content={"error": "Context test supports completion or memory roles"},
            status_code=400,
        )

    api_base = (data.api_base or "").strip()
    model = (data.model or "").strip()
    api_key = (data.api_key or "").strip()
    if role == "memory":
        api_base = api_base or _effective_api_base("MEMORY_API_BASE") or _effective_api_base(
            "OPENAI_BASE_URL", "OPENAI_API_BASE"
        )
        model = model or os.getenv("MEMORY_COMPLETION_MODEL", os.getenv("COMPLETION_MODEL", ""))
        api_key = api_key or _effective_api_key("MEMORY_API_KEY", "OPENAI_API_KEY")
        reasoning = (data.reasoning or _setting_value("MEMORY_REASONING", default=DEFAULT_MEMORY_REASONING))
        extra_body = _maybe_json_object(
            data.extra_body
            if data.extra_body is not None
            else _setting_value("MEMORY_EXTRA_BODY", default=DEFAULT_MEMORY_EXTRA_BODY)
        )
    else:
        api_base = api_base or _effective_api_base(
            "COMPLETION_API_BASE", "COMPLETION_BASE_URL"
        ) or _effective_api_base("OPENAI_BASE_URL", "OPENAI_API_BASE")
        model = model or os.getenv("COMPLETION_MODEL", "")
        api_key = api_key or _effective_api_key("COMPLETION_API_KEY", "OPENAI_API_KEY")
        reasoning = (data.reasoning or _setting_value("COMPLETION_REASONING", default=DEFAULT_COMPLETION_REASONING))
        extra_body = _maybe_json_object(
            data.extra_body
            if data.extra_body is not None
            else _setting_value("COMPLETION_EXTRA_BODY", default=DEFAULT_COMPLETION_EXTRA_BODY)
        )

    if not api_base or not model or not api_key:
        return JSONResponse(
            content={"error": "Model, API base, and API key are required for context test"},
            status_code=400,
        )

    try:
        max_completion_tokens = int(str(data.max_tokens or "8").strip() or "8")
        max_probe_tokens = int(str(data.max_probe_tokens or "256000").strip() or "256000")
    except ValueError:
        return JSONResponse(
            content={"error": "Max tokens and max probe tokens must be integers"},
            status_code=400,
        )
    max_completion_tokens = max(1, min(max_completion_tokens, 64))
    max_probe_tokens = max(4096, min(max_probe_tokens, 256000))

    model_info: dict[str, Any]
    try:
        model_info = _fetch_model_info(api_base, api_key, model)
    except Exception as exc:  # pylint: disable=broad-except
        model_info = {
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
            "context_tokens": None,
        }

    # Prefer declared metadata — it is exact for any size (incl. 256k–1M, which a
    # filler probe cannot reach). Probe only when nothing declares a window.
    declared = model_info.get("context_tokens")
    probe: dict[str, Any] = {}
    if declared:
        detected = _snap_context(int(declared))
    else:
        probe = _probe_context_window(
            api_base, api_key, model,
            max_completion_tokens, max_probe_tokens, reasoning, extra_body,
        )
        detected = _snap_context(probe.get("detected_context_tokens"))

    return JSONResponse(
        content={
            "role": role,
            "model": model,
            "api_base": api_base,
            "declared_context_tokens": declared,
            "detected_context_tokens": detected,
            "model_info": model_info,
            "probe": probe,
        },
        status_code=200,
    )


@settings_router.get("/embedding-info", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def get_embedding_info(request: Request):  # pylint: disable=unused-argument
    """Auto-detect the embedding endpoint's vector dimension (and device/model)."""
    api_base = (os.getenv("EMBEDDING_API_BASE") or "").strip()
    api_key = (os.getenv("EMBEDDING_API_KEY") or "local").strip()
    model = (os.getenv("EMBEDDING_MODEL") or "").strip()
    info: dict[str, Any] = {"dimensions": None, "device": None, "model": model, "api_base": api_base}
    if not api_base:
        return JSONResponse(content=info, status_code=200)

    base = api_base.rstrip("/")
    root = base[:-3].rstrip("/") if base.endswith("/v1") else base
    # 1) the built-in embedding server exposes /health with the native dim + device.
    try:
        req = urllib.request.Request(f"{root}/health", headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, context=_https_context(), timeout=10) as resp:
            health = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        if health.get("dim"):
            info["dimensions"] = int(health["dim"])
        if health.get("device"):
            info["device"] = str(health["device"])
    except Exception:  # pylint: disable=broad-except
        pass
    # 2) fallback for any OpenAI-compatible endpoint: one embed reveals the length.
    if not info["dimensions"]:
        try:
            served = model.split("/")[-1] if model else "embedding"
            payload = {"model": served, "input": "dimension probe"}
            req = urllib.request.Request(
                f"{base}/embeddings", data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, context=_https_context(), timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            vec = ((body.get("data") or [{}])[0] or {}).get("embedding") or []
            if vec:
                info["dimensions"] = len(vec)
        except Exception:  # pylint: disable=broad-except
            pass
    return JSONResponse(content=info, status_code=200)


@settings_router.post("/validate-api-key", responses={401: UNAUTHORIZED_RESPONSE})
@token_required
async def validate_api_key(request: Request, data: ValidateKeyRequest):  # pylint: disable=too-many-return-statements,unused-argument
    """
    Validate an AI provider API key by making a simple test request.
    This endpoint does not store the key, it only validates it.
    Supports: openai, google, anthropic
    """
    api_key = data.api_key.strip()
    vendor = data.vendor.lower()
    model = data.model

    if not api_key:
        return JSONResponse(
            content={"valid": False, "error": "API key is required"},
            status_code=400
        )

    # Validate vendor — only key-based vendors can be validated via API call
    validatable_vendors = ("openai", "anthropic", "gemini", "cohere")
    if vendor not in validatable_vendors:
        allowed = ", ".join(validatable_vendors)
        return JSONResponse(
            content={"valid": False, "error": f"Unsupported vendor for key validation. Supported: {allowed}"},
            status_code=400
        )

    # Validate model is not empty
    if not model or not model.strip():
        return JSONResponse(
            content={"valid": False, "error": "Model name is required"},
            status_code=400
        )

    # Validate key format based on vendor
    if vendor == "openai" and not api_key.startswith('sk-'):
        return JSONResponse(
            content={"valid": False, "error": "Invalid OpenAI API key format"},
            status_code=400
        )
    if vendor == "anthropic" and not api_key.startswith('sk-ant-'):
        return JSONResponse(
            content={"valid": False, "error": "Invalid Anthropic API key format"},
            status_code=400
        )

    try:
        # Construct model name for LiteLLM (vendor/model format)
        full_model_name = f"{vendor}/{model}"

        test_response = completion(
            model=full_model_name,
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1,
            api_key=api_key,
        )

        # If we get here without exception, the key is valid
        if test_response and test_response.choices:
            return JSONResponse(
                content={"valid": True},
                status_code=200
            )
        return JSONResponse(
            content={"valid": False, "error": "Invalid API key"},
            status_code=401
        )

    except Exception as e:  # pylint: disable=broad-except
        error_lower = str(e).lower()
        logging.warning("API key validation failed for vendor=%s",
                        _sanitize_for_log(vendor))

        # Return generic messages — never expose exception details
        if "invalid" in error_lower or "authentication" in error_lower:
            return JSONResponse(
                content={"valid": False, "error": "Invalid API key"},
                status_code=401
            )
        if "quota" in error_lower or "rate" in error_lower:
            return JSONResponse(
                content={"valid": False, "error": "API quota exceeded or rate limited"},
                status_code=429
            )
        return JSONResponse(
            content={"valid": False, "error": "Failed to validate API key"},
            status_code=500
        )
