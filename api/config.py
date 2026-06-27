
"""
This module contains the configuration for the text2sql module.
"""

import logging
import dataclasses
import json
import os
import time
from typing import Union

from dotenv import load_dotenv

load_dotenv()

# Per-user runtime settings live in FalkorDB and are applied at app startup
# (api.app_factory lifespan -> runtime_settings_store.apply_user_settings), after
# Config is built here — apply_runtime_overrides reassigns the relevant attrs.

from api.tls import (  # pylint: disable=wrong-import-position
    configure_global_ssl_verification,
    global_ssl_verification_disabled,
)

configure_global_ssl_verification()

import httpx  # pylint: disable=wrong-import-position
import litellm  # pylint: disable=wrong-import-position
from litellm import embedding
from openai import OpenAI  # pylint: disable=wrong-import-position

# Configure litellm logging to prevent sensitive data leakage
def configure_litellm_logging():
    """Configure litellm to suppress completion logs."""

    # Disable LiteLLM logger that outputs
    litellm_logger = logging.getLogger("LiteLLM")
    litellm_logger.setLevel(logging.ERROR)
    litellm_logger.disabled = True


# Initialize litellm configuration
configure_litellm_logging()


_FALSE_VALUES = {"0", "false", "no", "n", "off", "disable", "disabled"}
_TRUE_VALUES = {"1", "true", "yes", "y", "on", "enable", "enabled"}


def _first_env(*names: str) -> str:
    """Return the first non-empty environment value from names."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _ssl_verify_setting(role: str = "LLM") -> bool | str:
    """Return TLS verification setting for OpenAI-compatible HTTP clients."""
    if global_ssl_verification_disabled():
        return False

    role = role.upper()
    verify = _first_env(
        f"{role}_VERIFY_SSL",
        "OPENAI_VERIFY_SSL",
        "LLM_VERIFY_SSL",
        "LITELLM_VERIFY_SSL",
    )
    ca_bundle = _first_env(
        f"{role}_CA_BUNDLE",
        "OPENAI_CA_BUNDLE",
        "LLM_CA_BUNDLE",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
    )

    if not verify:
        return ca_bundle or True

    normalized = verify.lower()
    if normalized in _FALSE_VALUES:
        return False
    if normalized in _TRUE_VALUES:
        return ca_bundle or True
    return verify


def openai_http_client(role: str = "LLM") -> httpx.Client:
    """Create a sync httpx client for the OpenAI SDK."""
    return httpx.Client(verify=_ssl_verify_setting(role))


def openai_async_http_client(role: str = "LLM") -> httpx.AsyncClient:
    """Create an async httpx client for the OpenAI SDK."""
    return httpx.AsyncClient(verify=_ssl_verify_setting(role))


litellm.ssl_verify = _ssl_verify_setting("LITELLM")


class EmbeddingsModel:
    """Embeddings model wrapper for text embedding operations."""

    def __init__(self, model_name: str, config: dict = None):
        self.model_name = model_name
        self.config = config

    def _openai_embedding_model(self) -> str | None:
        """Return OpenAI-compatible model name when direct SDK calls are safer."""
        if not self.model_name.startswith("openai/"):
            return None
        if not (os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY")):
            return None
        return self.model_name.removeprefix("openai/")

    def _openai_client(self) -> OpenAI:
        return OpenAI(
            api_key=os.getenv("EMBEDDING_API_KEY") or os.getenv("OPENAI_API_KEY"),
            base_url=(
                os.getenv("EMBEDDING_API_BASE")
                or os.getenv("OPENAI_BASE_URL")
                or os.getenv("OPENAI_API_BASE")
            ),
            http_client=openai_http_client("EMBEDDING"),
            timeout=_int_env("EMBEDDING_TIMEOUT_SECONDS", 20, 1, 300),
            max_retries=openai_max_retries("EMBEDDING"),
        )

    @staticmethod
    def _dimensions() -> int | None:
        raw = os.getenv("EMBEDDING_DIMENSION", "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def _embedding_kwargs(self, text: Union[str, list]) -> dict:
        kwargs = {
            "model": self._openai_embedding_model() or self.model_name,
            "input": text,
        }
        if not self._openai_embedding_model():
            kwargs["timeout"] = _int_env("EMBEDDING_TIMEOUT_SECONDS", 20, 1, 300)
            kwargs["ssl_verify"] = _ssl_verify_setting("EMBEDDING")
        dimensions = self._dimensions()
        if dimensions:
            kwargs["dimensions"] = dimensions
        return kwargs

    @staticmethod
    def _expected_count(text: Union[str, list]) -> int:
        return len(text) if isinstance(text, list) else 1

    @staticmethod
    def _validate_embeddings(embeddings: list, expected_count: int) -> list:
        if len(embeddings) != expected_count:
            raise ValueError(
                f"Expected {expected_count} embeddings, received {len(embeddings)}"
            )
        if any(not embedding_vector for embedding_vector in embeddings):
            raise ValueError("No embedding data received")
        return embeddings

    def _embed_once(self, text: Union[str, list]) -> list:
        expected_count = self._expected_count(text)
        openai_model = self._openai_embedding_model()
        if openai_model:
            response = self._openai_client().embeddings.create(
                **self._embedding_kwargs(text),
            )
            return self._validate_embeddings(
                [item.embedding for item in response.data],
                expected_count,
            )

        embeddings = embedding(**self._embedding_kwargs(text))
        return self._validate_embeddings(
            [item["embedding"] for item in embeddings.data],
            expected_count,
        )

    def embed(self, text: Union[str, list]) -> list:
        """
        Get the embeddings of the text

        Args:
            text (str|list): The text(s) to embed

        Returns:
            list: The embeddings of the text

        """
        max_attempts = _int_env("EMBEDDING_MAX_ATTEMPTS", 5, 1, 10)
        last_error = None
        for attempt in range(1, max_attempts + 1):
            try:
                return self._embed_once(text)
            except Exception as exc:  # pylint: disable=broad-exception-caught
                last_error = exc
                if attempt == max_attempts:
                    break
                logging.warning(
                    "Embedding request failed; retrying attempt=%d/%d error=%s",
                    attempt,
                    max_attempts,
                    str(exc)[:200],
                )
                time.sleep(0.75 * attempt)

        if isinstance(text, list) and len(text) > 1:
            logging.warning(
                "Batch embedding failed after %d attempts; retrying items individually. "
                "error=%s",
                max_attempts,
                str(last_error)[:200],
            )
            embeddings = []
            for item in text:
                embeddings.extend(self.embed(item))
            return embeddings

        raise last_error

    def get_vector_size(self) -> int:
        """
        Get the size of the vector

        Returns:
            int: The size of the vector

        """
        openai_model = self._openai_embedding_model()
        if openai_model:
            response = self._openai_client().embeddings.create(
                **self._embedding_kwargs(["Hello World"]),
            )
            return len(response.data[0].embedding)

        response = embedding(**self._embedding_kwargs(["Hello World"]))
        size = len(response.data[0]["embedding"])
        return size


def _with_prefix(model: str, provider: str) -> str:
    """Ensure a model string has exactly one provider prefix."""
    prefix = f"{provider}/"
    return prefix + model.removeprefix(prefix)


def _json_env(name: str) -> dict:
    """Parse a JSON object from an environment variable."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} must be a JSON object")
    return parsed


def _int_env(name: str, default: int, min_value: int = 1, max_value: int = 100) -> int:
    """Read a bounded integer environment variable."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


def openai_max_retries(role: str = "LLM") -> int:
    """Read retry count for OpenAI SDK clients."""
    role_default = _int_env("OPENAI_MAX_RETRIES", 0, 0, 10)
    return _int_env(f"{role.upper()}_MAX_RETRIES", role_default, 0, 10)


def llm_timeout_seconds(role: str = "LLM") -> int:
    """Read timeout for LiteLLM/OpenAI-compatible completion calls."""
    role_default = _int_env("LLM_TIMEOUT_SECONDS", 60, 1, 3600)
    return _int_env(f"{role.upper()}_TIMEOUT_SECONDS", role_default, 1, 3600)


def llm_max_retries(role: str = "LLM") -> int:
    """Read retry count for LiteLLM completion calls."""
    role_default = _int_env("LLM_MAX_RETRIES", 0, 0, 10)
    return _int_env(f"{role.upper()}_MAX_RETRIES", role_default, 0, 10)


def _optional_int_env(name: str, min_value: int = 1, max_value: int = 100000) -> int | None:
    """Read an optional bounded integer environment variable."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return max(min_value, min(max_value, value))


def _bool_env(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _float_env(
    name: str,
    default: float | None = None,
    min_value: float = 0.0,
    max_value: float = 2.0,
) -> float | None:
    """Read an optional bounded float environment variable."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, value))


def _reasoning_env(name: str, default: str | None = None) -> dict | str | None:
    """Parse a reasoning setting for OpenAI-compatible providers.

    *default* is used when the env var is unset, so the effective default can
    match the Settings-page default (reasoning OFF) instead of silently leaving
    thinking models in their ON-by-default state.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        raw = (default or "").strip()
    if not raw:
        return None
    if raw.lower() in {"off", "none", "false", "0"}:
        # OpenAI-like gateways such as OpenRouter may enable reasoning by
        # default for thinking models. Omitting the parameter is therefore not
        # the same as disabling it.
        return {"effort": "none", "exclude": True}
    if raw.lower() in {"on", "true", "yes", "enable", "enabled"}:
        # Reasoning explicitly enabled: use the model's default thinking
        # behaviour (no override, no exclude).
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, (dict, str, int, float, bool)):
        return parsed
    if raw.lower() in {"low", "medium", "high"}:
        return {"effort": raw.lower()}
    return raw


def _reasoning_is_disabled(reasoning: "dict | str | None") -> bool:
    """True when a reasoning setting means "thinking OFF", however it is spelled.

    Providers express the OFF state differently — the bare strings
    ``off``/``none``/``false``, an OpenRouter-style ``{"effort":"none",
    "exclude":true}`` object, etc. This normalises all of them so callers can
    emit the correct disable signal. Note: a *missing* setting is NOT "off" —
    thinking models default to ON, so only an explicit disable returns True.
    """
    if reasoning is None:
        return False
    if isinstance(reasoning, str):
        return reasoning.strip().lower() in {
            "off", "none", "false", "0", "disable", "disabled", "no",
        }
    if isinstance(reasoning, dict):
        if reasoning.get("exclude") is True or reasoning.get("enabled") is False:
            return True
        return str(reasoning.get("effort", "")).strip().lower() in {"none", "off"}
    return False


SUPPORTED_VENDORS = ("openai", "anthropic", "gemini", "azure", "ollama", "cohere")


@dataclasses.dataclass
class Config:
    """
    Configuration class for the text2sql module.
    """

    # User-provided overrides via env vars
    _user_completion = os.getenv("COMPLETION_MODEL", "")
    _user_embedding = os.getenv("EMBEDDING_MODEL", "")
    _openai_like_key = (
        os.getenv("OPENAI_API_KEY")
        or os.getenv("COMPLETION_API_KEY")
        or os.getenv("MEMORY_API_KEY")
        or os.getenv("EMBEDDING_API_KEY")
    )

    # Determine the provider and models based on available API keys
    # Priority: Ollama > OpenAI > Gemini > Anthropic > Cohere > Azure (default)
    if os.getenv("OLLAMA_MODEL"):
        LLM_PROVIDER = "ollama"
        AZURE_FLAG = False
        COMPLETION_MODEL = _user_completion or _with_prefix(
            os.getenv("OLLAMA_MODEL"), "ollama")
        EMBEDDING_MODEL_NAME = _user_embedding or _with_prefix(
            os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text"), "ollama")
    elif _openai_like_key:
        LLM_PROVIDER = "openai"
        AZURE_FLAG = False
        COMPLETION_MODEL = _user_completion or "openai/gpt-4.1"
        EMBEDDING_MODEL_NAME = _user_embedding or "openai/text-embedding-ada-002"
    elif os.getenv("GEMINI_API_KEY"):
        LLM_PROVIDER = "gemini"
        AZURE_FLAG = False
        COMPLETION_MODEL = _user_completion or "gemini/gemini-3-pro-preview"
        EMBEDDING_MODEL_NAME = _user_embedding or "gemini/gemini-embedding-001"
    elif os.getenv("ANTHROPIC_API_KEY"):
        LLM_PROVIDER = "anthropic"
        AZURE_FLAG = False
        COMPLETION_MODEL = _user_completion or "anthropic/claude-sonnet-4-5-20250929"
        if _user_embedding:
            EMBEDDING_MODEL_NAME = _user_embedding
        elif os.getenv("VOYAGE_API_KEY"):
            EMBEDDING_MODEL_NAME = "voyage/voyage-3"
        else:
            raise ValueError(
                "ANTHROPIC_API_KEY is set, but Anthropic has no native embeddings. "
                "Set EMBEDDING_MODEL or VOYAGE_API_KEY for embeddings."
            )
    elif os.getenv("COHERE_API_KEY"):
        LLM_PROVIDER = "cohere"
        AZURE_FLAG = False
        COMPLETION_MODEL = _user_completion or _with_prefix(
            os.getenv("COHERE_MODEL", "command-a-03-2025"), "cohere")
        EMBEDDING_MODEL_NAME = _user_embedding or _with_prefix(
            os.getenv("COHERE_EMBEDDING_MODEL", "embed-v4.0"), "cohere")
    else:
        # Default to Azure
        LLM_PROVIDER = "azure"
        AZURE_FLAG = True
        COMPLETION_MODEL = _user_completion or "azure/gpt-4.1"
        EMBEDDING_MODEL_NAME = _user_embedding or "azure/text-embedding-ada-002"

    DB_MAX_DISTINCT: int = 100  # pylint: disable=invalid-name
    DB_UNIQUENESS_THRESHOLD: float = 0.5  # pylint: disable=invalid-name
    SHORT_MEMORY_LENGTH = 5  # Maximum number of questions to keep in short-term memory
    TABLE_RETRIEVAL_TOP_K = _int_env("TABLE_RETRIEVAL_TOP_K", 8, 1, 100)
    TABLE_CONTEXT_MAX = _int_env("TABLE_CONTEXT_MAX", 20, 1, 200)
    TABLE_EXPANSION_SEED_MAX = _int_env("TABLE_EXPANSION_SEED_MAX", 8, 1, 200)
    # --- Blackboard pipeline: shared-JSON agents + schema top-up + rule-RAG ---
    # Off by default; the legacy single-pass path is unchanged when disabled.
    BLACKBOARD_PIPELINE_ENABLED = _bool_env("BLACKBOARD_PIPELINE_ENABLED", False)
    # Tool-based two-phase agent (rule-aware planner -> SQL writer) that mutates
    # the blackboard via validated tool calls instead of free-text JSON.
    BLACKBOARD_TOOL_AGENT_ENABLED = _bool_env("BLACKBOARD_TOOL_AGENT_ENABLED", False)
    BLACKBOARD_INITIAL_TABLE_LIMIT = _int_env(
        "BLACKBOARD_INITIAL_TABLE_LIMIT", 12, 1, 200)
    BLACKBOARD_MAX_TOPUPS = _int_env("BLACKBOARD_MAX_TOPUPS", 2, 0, 5)
    BLACKBOARD_TOPUP_MAX_TABLES = _int_env(
        "BLACKBOARD_TOPUP_MAX_TABLES", 6, 1, 50)
    RULE_RAG_ENABLED = _bool_env("RULE_RAG_ENABLED", False)
    RULE_RAG_MAX_CHARS = _int_env("RULE_RAG_MAX_CHARS", 3500, 500, 20000)
    # --- Rule-gate agent: apply user + business rules to the generated SQL ---
    # Runs after the generator and before the deterministic sql_gate. Focused
    # (no full schema dump); skips itself when there is no rule text to apply.
    RULE_GATE_ENABLED = _bool_env("RULE_GATE_ENABLED", True)
    # When user_rules is empty, also run the gate to apply RAG'd business rules.
    # Owner spec says the gate applies BOTH; keep this toggle so the pass can be
    # restricted to user_rules-only if business-rule gating proves too costly.
    RULE_GATE_ON_BUSINESS_RULES = _bool_env("RULE_GATE_ON_BUSINESS_RULES", True)
    # Column-aware knowledge retrieval for the generator (candidate columns) and
    # the gate (selected columns). Cheap embedding calls, no extra LLM.
    KNOWLEDGE_BY_COLUMNS_ENABLED = _bool_env("KNOWLEDGE_BY_COLUMNS_ENABLED", True)
    SCHEMA_PRUNING_ENABLED = _bool_env("SCHEMA_PRUNING_ENABLED", True)
    SCHEMA_PRIMARY_MAX_COLUMNS = _int_env("SCHEMA_PRIMARY_MAX_COLUMNS", 12, 1, 200)
    SCHEMA_SECONDARY_MAX_COLUMNS = _int_env("SCHEMA_SECONDARY_MAX_COLUMNS", 0, 0, 200)
    SCHEMA_TOP_TABLE_FULL_COUNT = _int_env("SCHEMA_TOP_TABLE_FULL_COUNT", 1, 0, 20)
    SCHEMA_TOP_TABLE_MAX_COLUMNS = _int_env("SCHEMA_TOP_TABLE_MAX_COLUMNS", 24, 1, 500)
    SCHEMA_TABLE_DESCRIPTION_MAX_CHARS = _int_env(
        "SCHEMA_TABLE_DESCRIPTION_MAX_CHARS", 220, 40, 5000
    )
    SCHEMA_COLUMN_DESCRIPTION_MAX_CHARS = _int_env(
        "SCHEMA_COLUMN_DESCRIPTION_MAX_CHARS", 180, 40, 5000
    )
    # JSON/JSONB columns carry their whole nested-key map in the description
    # (sample_values are empty for them), so they need a much larger budget than
    # scalar columns — otherwise the model never sees keys like final_position /
    # birth_date / coordinates.elevation_m and guesses wrong JSON paths.
    SCHEMA_JSON_DESCRIPTION_MAX_CHARS = _int_env(
        "SCHEMA_JSON_DESCRIPTION_MAX_CHARS", 2600, 180, 12000
    )
    SCHEMA_EVIDENCE_MAX_LINES = _int_env("SCHEMA_EVIDENCE_MAX_LINES", 32, 8, 200)
    QW_VALUE_SAMPLING_ENABLED = _bool_env("QW_VALUE_SAMPLING_ENABLED", True)
    QW_VALUE_SAMPLE_LIMIT = _int_env("QW_VALUE_SAMPLE_LIMIT", 8, 1, 25)
    QW_VALUE_SAMPLE_MAX_COLUMNS = _int_env("QW_VALUE_SAMPLE_MAX_COLUMNS", 12, 0, 120)
    QW_VALUE_SAMPLE_MAX_SQLS_PER_COLUMN = _int_env(
        "QW_VALUE_SAMPLE_MAX_SQLS_PER_COLUMN", 2, 1, 20
    )
    TEXT2SQL_FAST_MODE = _bool_env("TEXT2SQL_FAST_MODE", False)
    TEXT2SQL_RELEVANCY_ENABLED = _bool_env(
        "TEXT2SQL_RELEVANCY_ENABLED",
        not TEXT2SQL_FAST_MODE,
    )
    TABLE_FINDER_LLM_ENABLED = _bool_env(
        "TABLE_FINDER_LLM_ENABLED",
        not TEXT2SQL_FAST_MODE,
    )
    RESPONSE_FORMATTER_ENABLED = _bool_env(
        "RESPONSE_FORMATTER_ENABLED",
        not TEXT2SQL_FAST_MODE,
    )
    SQL_QUERY_CACHE_ENABLED = _bool_env(
        "SQL_QUERY_CACHE_ENABLED",
        _bool_env("IMPALA_QUERY_CACHE_ENABLED", True),
    )
    SQL_QUERY_CACHE_TTL_SECONDS = _int_env(
        "SQL_QUERY_CACHE_TTL_SECONDS",
        _int_env("IMPALA_QUERY_CACHE_TTL_SECONDS", 10800, 1, 86400),
        1,
        86400,
    )
    SQL_QUERY_CACHE_MAX_ENTRIES = _int_env(
        "SQL_QUERY_CACHE_MAX_ENTRIES",
        _int_env("IMPALA_QUERY_CACHE_MAX_ENTRIES", 512, 1, 10000),
        1,
        10000,
    )
    SQL_QUERY_CACHE_MAX_ROWS = _int_env(
        "SQL_QUERY_CACHE_MAX_ROWS",
        _int_env("IMPALA_QUERY_CACHE_MAX_ROWS", 1000, 1, 100000),
        1,
        100000,
    )
    IMPALA_QUERY_TIMEOUT_SECONDS = _int_env(
        "IMPALA_QUERY_TIMEOUT_SECONDS", 45, 1, 3600
    )
    IMPALA_MEM_LIMIT = os.getenv("IMPALA_MEM_LIMIT", "1g").strip()
    SQL_HEALING_MAX_ATTEMPTS = _int_env("SQL_HEALING_MAX_ATTEMPTS", 3, 1, 30)
    # Preflight EXPLAIN + one heal loop on the GENERATE path (/sql), so SQL
    # returned without execution is still runnable (catches CTE-scope/column
    # errors the static gate can't see). EXPLAIN-only — nothing executes.
    GENERATE_PREFLIGHT_HEAL_ENABLED = _bool_env("GENERATE_PREFLIGHT_HEAL_ENABLED", True)
    # Resolver schema budget: the metric resolver only binds formula terms to a
    # few columns, so a broad candidate dump dilutes the weak model into
    # collapsing/dropping a multi-term formula. Keep it tight (proven: ~40
    # columns made it drop "+points" from SPI; ~4 kept it).
    RESOLVER_MAX_COLS = _int_env("RESOLVER_MAX_COLS", 22, 6, 120)
    RESOLVER_JSON_LEAF_CAP = _int_env("RESOLVER_JSON_LEAF_CAP", 4, 1, 40)
    # Generator self-consistency: draw N SQL candidates and keep the modal
    # (sqlglot-canonical) one — damps weak-model non-determinism (same prompt →
    # different SQL across runs). 1 = off (no added cost); 3 is the useful minimum
    # for a majority. Costs N× generation, so OFF by default — enable per-deploy.
    GENERATOR_SELF_CONSISTENCY = _int_env("GENERATOR_SELF_CONSISTENCY", 1, 1, 5)
    # Narrow critic agent that re-checks resolved-metric column bindings against
    # the concept definition + column descriptions and rebinds a mis-bound column.
    # OFF by default: on the weak local model it shares the resolver's semantic-
    # binding weakness and MIS-corrects (rebinds to a wrong "classification"-
    # sounding column instead of the right status column) + adds an LLM pass. The
    # agent is sound for a STRONGER generator — enable then. (Verified: it did not
    # fix 3/5 and occasionally mis-bound; deterministic phantom/stub guards already
    # cover the structural errors safely.)
    FORMULA_VALIDATOR_ENABLED = _bool_env("FORMULA_VALIDATOR_ENABLED", False)
    LLM_COMPLETION_MAX_ATTEMPTS = _int_env("LLM_COMPLETION_MAX_ATTEMPTS", 3, 1, 10)
    TABLE_FINDER_MAX_ATTEMPTS = _int_env("TABLE_FINDER_MAX_ATTEMPTS", 4, 1, 10)
    # Generous so reasoning ("thinking") models have room to think AND emit
    # content; a small cap gets fully consumed by reasoning tokens -> empty output.
    TABLE_FINDER_MAX_TOKENS = _int_env("TABLE_FINDER_MAX_TOKENS", 8000, 128, 32000)
    TABLE_FINDER_STRUCTURED_OUTPUT_ENABLED = _bool_env(
        "TABLE_FINDER_STRUCTURED_OUTPUT_ENABLED",
        False,
    )
    TABLE_RERANK_ENABLED = _bool_env("TABLE_RERANK_ENABLED", False)
    TABLE_RERANK_MAX_ATTEMPTS = _int_env("TABLE_RERANK_MAX_ATTEMPTS", 2, 1, 5)
    TABLE_RERANK_MAX_CANDIDATES = _int_env("TABLE_RERANK_MAX_CANDIDATES", 20, 1, 200)
    TABLE_RERANK_MAX_COLUMNS_PER_TABLE = _int_env(
        "TABLE_RERANK_MAX_COLUMNS_PER_TABLE", 16, 1, 200
    )
    TABLE_RERANK_MAX_TOKENS = _int_env("TABLE_RERANK_MAX_TOKENS", 8000, 128, 32000)
    # Feature B — per-column evidence grounding. The analysis agent emits a
    # column_evidence justification for every filter/metric/join column; a pure
    # sqlglot-AST validator checks each used column is grounded, and the SQL is
    # rendered with those justifications as inline comments. ON = compute + log +
    # surface (advisory). REPAIR adds an optional one-shot re-grounding LLM pass
    # when filters/metrics are ungrounded (OFF by default to bound latency).
    # Recall-generous top_k for knowledge/document RAG. Used to focus the
    # knowledge sent to the prompt (relevant chunks instead of the full blob)
    # when the KB is not in the trimmable structured-concept format.
    KNOWLEDGE_RETRIEVAL_TOP_K = _int_env("KNOWLEDGE_RETRIEVAL_TOP_K", 8, 1, 50)
    # Max tables in the GENERATOR's schema prompt. find() may surface ~20
    # candidates (~45K schema chars → ~200s prefill on a weak model → timeouts).
    # The generator only needs the linker-selected + join-skeleton tables plus a
    # few top finder-ranked ones as safety. Pruning the generator's input (NOT
    # the gates, which keep the full graph) cuts prefill latency and noise.
    # 0 = off (send all). Pinned linker/skeleton tables may overflow this cap.
    GENERATOR_TABLE_CAP = _int_env("GENERATOR_TABLE_CAP", 8, 0, 100)
    # Relevance-ranked candidate columns shown to the LINKER (chunk=field). Ranked
    # by query↔description so obfuscated names surface and decoys rank out.
    LINKER_CANDIDATE_COLUMNS = _int_env("LINKER_CANDIDATE_COLUMNS", 50, 10, 300)
    # Max columns in the GENERATOR's schema (relevance-ranked + linker-pinned +
    # join keys). 0 = keep all columns of the (table-capped) schema.
    GENERATOR_COLUMN_CAP = _int_env("GENERATOR_COLUMN_CAP", 45, 0, 400)
    # If the generated SQL references a real field pruned out of the generator's
    # schema, re-add it from the full candidate set (RAG top-up) and regenerate,
    # up to this many times; after that the generator's missing_information asks
    # the user which source/field to use. Honors "find ≤5 times, then ask user".
    GENERATOR_MAX_TOPUPS = _int_env("GENERATOR_MAX_TOPUPS", 5, 1, 10)
    EVIDENCE_GROUNDING_ENABLED = _bool_env("EVIDENCE_GROUNDING_ENABLED", True)
    EVIDENCE_GROUNDING_REPAIR_ENABLED = _bool_env(
        "EVIDENCE_GROUNDING_REPAIR_ENABLED", False
    )
    EMBEDDING_DIMENSION = _optional_int_env("EMBEDDING_DIMENSION", 1, 2000000)

    EMBEDDING_MODEL = EmbeddingsModel(model_name=EMBEDDING_MODEL_NAME)

    COMPLETION_API_KEY = os.getenv("COMPLETION_API_KEY") or os.getenv("OPENAI_API_KEY")
    COMPLETION_API_BASE = (
        os.getenv("COMPLETION_API_BASE")
        or os.getenv("COMPLETION_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
        or os.getenv("OPENAI_API_BASE")
    )
    COMPLETION_EXTRA_BODY = _json_env("COMPLETION_EXTRA_BODY")
    # Low default temperature for deterministic SQL/structured output (avoids
    # hallucination); agents may still override per call.
    COMPLETION_TEMPERATURE = _float_env("COMPLETION_TEMPERATURE", 0.0, 0.0, 2.0)
    COMPLETION_MAX_TOKENS = _optional_int_env("COMPLETION_MAX_TOKENS", 1, 100000)
    COMPLETION_CONTEXT_TOKENS = _int_env("COMPLETION_CONTEXT_TOKENS", 32000, 1, 2000000)
    BUSINESS_CURRENT_DATE = os.getenv("BUSINESS_CURRENT_DATE", "").strip()
    COMPLETION_REASONING = _reasoning_env("COMPLETION_REASONING", "off")
    # Reasoning is configured PER ENDPOINT only. The former per-stage reasoning
    # settings are retired: every stage inherits the completion endpoint's
    # reasoning so a single per-endpoint toggle controls thinking everywhere.
    ANALYSIS_REASONING = COMPLETION_REASONING
    HEALER_REASONING = COMPLETION_REASONING
    TABLE_FINDER_REASONING = COMPLETION_REASONING
    TABLE_RERANK_REASONING = COMPLETION_REASONING
    # Memory ALWAYS uses the main completion model/endpoint — there is no
    # separate memory model. (A stale separate model is why memory silently
    # failed: it pointed at an unloaded model.) Keeping these as mirrors of the
    # COMPLETION_* values means graphiti_tool keeps reading Config.MEMORY_* with
    # no code change, and the Settings page no longer needs a memory-model knob.
    MEMORY_COMPLETION_MODEL = COMPLETION_MODEL
    MEMORY_API_KEY = COMPLETION_API_KEY
    MEMORY_API_BASE = COMPLETION_API_BASE
    MEMORY_TEMPERATURE = COMPLETION_TEMPERATURE
    MEMORY_MAX_TOKENS = COMPLETION_MAX_TOKENS
    MEMORY_CONTEXT_TOKENS = _int_env("MEMORY_CONTEXT_TOKENS", 128000, 1, 2000000)
    MEMORY_REASONING = COMPLETION_REASONING
    MEMORY_EXTRA_BODY = COMPLETION_EXTRA_BODY

    @staticmethod
    def reasoning_extra_body(reasoning: dict | str | None) -> dict | None:
        """Return an extra_body override for stage-specific reasoning."""
        if reasoning is None:
            return None
        body: dict = {"reasoning": reasoning}
        if _reasoning_is_disabled(reasoning):
            # Mirror _apply_model_parameters: a disabled stage must also send the
            # `reasoning_effort="none"` body field that local OpenAI-compatible
            # servers (LM Studio, vLLM) honour to actually suppress thinking.
            body["reasoning_effort"] = "none"
        return body

    @staticmethod
    def _apply_model_parameters(
        args: dict,
        temperature: float | None,
        max_tokens: int | None,
        reasoning: dict | str | None,
        extra_body: dict | None = None,
    ) -> None:
        """Apply runtime model parameters to LiteLLM kwargs."""
        if temperature is not None and "temperature" not in args:
            args["temperature"] = temperature
        if max_tokens is not None and "max_tokens" not in args:
            args["max_tokens"] = max_tokens

        configured_extra_body = dict(extra_body or {})
        if reasoning is not None:
            configured_extra_body["reasoning"] = reasoning
            if _reasoning_is_disabled(reasoning):
                # Turning thinking OFF is provider-specific and OMITTING the
                # parameter is NOT "off" (thinking models default to ON). So
                # when reasoning is disabled we emit EVERY known disable signal
                # and let each provider honour the one it understands:
                #   * extra_body.reasoning {"effort":"none","exclude":true}
                #     — OpenRouter / OpenAI-gateway style (set just above).
                #   * extra_body.reasoning_effort="none" — the field local
                #     OpenAI-compatible servers (LM Studio, vLLM) honour. It is
                #     placed INSIDE extra_body on purpose: litellm rejects
                #     `reasoning_effort` as a top-level param for some models,
                #     but forwards extra_body verbatim into the request body.
                # No model name is hardcoded — this works for any thinking model.
                configured_extra_body.setdefault("reasoning_effort", "none")
        if not configured_extra_body:
            return

        existing_extra_body = args.get("extra_body")
        if isinstance(existing_extra_body, dict):
            args["extra_body"] = {**configured_extra_body, **existing_extra_body}
        elif existing_extra_body is None:
            args["extra_body"] = configured_extra_body

    @staticmethod
    def completion_kwargs(custom_model: str = None,
                          custom_api_key: str = None,
                          **kwargs) -> dict:
        """Build LiteLLM kwargs for completion calls."""
        args = {
            "model": custom_model if custom_model else Config.COMPLETION_MODEL,
            **kwargs,
        }
        api_key = custom_api_key or Config.COMPLETION_API_KEY
        if api_key:
            args["api_key"] = api_key
        if Config.COMPLETION_API_BASE:
            args["api_base"] = Config.COMPLETION_API_BASE
        args.setdefault("ssl_verify", _ssl_verify_setting("COMPLETION"))
        args.setdefault("timeout", llm_timeout_seconds("COMPLETION"))
        args.setdefault("num_retries", llm_max_retries("COMPLETION"))
        Config._apply_model_parameters(
            args,
            Config.COMPLETION_TEMPERATURE,
            Config.COMPLETION_MAX_TOKENS,
            Config.COMPLETION_REASONING,
            Config.COMPLETION_EXTRA_BODY,
        )
        return args

    @staticmethod
    def memory_completion_kwargs(custom_model: str = None,
                                 custom_api_key: str = None,
                                 **kwargs) -> dict:
        """Build LiteLLM kwargs for memory completion calls."""
        args = {
            "model": custom_model if custom_model else Config.MEMORY_COMPLETION_MODEL,
            **kwargs,
        }
        api_key = custom_api_key or Config.MEMORY_API_KEY
        if api_key:
            args["api_key"] = api_key
        if Config.MEMORY_API_BASE:
            args["api_base"] = Config.MEMORY_API_BASE
        args.setdefault("ssl_verify", _ssl_verify_setting("MEMORY"))
        args.setdefault("timeout", llm_timeout_seconds("MEMORY"))
        args.setdefault("num_retries", llm_max_retries("MEMORY"))
        Config._apply_model_parameters(
            args,
            Config.MEMORY_TEMPERATURE,
            Config.MEMORY_MAX_TOKENS,
            Config.MEMORY_REASONING,
            Config.MEMORY_EXTRA_BODY,
        )
        return args

    @staticmethod
    def apply_runtime_overrides(overrides: "dict | None") -> None:
        """Apply Settings-page edits LIVE (no restart): update os.environ and
        recompute the import-frozen model attributes the kwargs builders read."""
        for key, value in (overrides or {}).items():
            if value is None:
                continue
            os.environ[str(key)] = str(value)
        Config.COMPLETION_MODEL = os.getenv("COMPLETION_MODEL") or Config.COMPLETION_MODEL
        Config.COMPLETION_API_KEY = (
            os.getenv("COMPLETION_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or Config.COMPLETION_API_KEY
        )
        Config.COMPLETION_API_BASE = (
            os.getenv("COMPLETION_API_BASE")
            or os.getenv("COMPLETION_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or os.getenv("OPENAI_API_BASE")
            or Config.COMPLETION_API_BASE
        )
        Config.COMPLETION_TEMPERATURE = _float_env(
            "COMPLETION_TEMPERATURE", Config.COMPLETION_TEMPERATURE, 0.0, 2.0
        )
        Config.COMPLETION_MAX_TOKENS = _optional_int_env("COMPLETION_MAX_TOKENS", 1, 100000)
        Config.COMPLETION_EXTRA_BODY = _json_env("COMPLETION_EXTRA_BODY")
        Config.COMPLETION_REASONING = _reasoning_env("COMPLETION_REASONING", "off")
        # Memory mirrors the main completion model/endpoint (no separate model).
        Config.MEMORY_COMPLETION_MODEL = Config.COMPLETION_MODEL
        Config.MEMORY_API_KEY = Config.COMPLETION_API_KEY
        Config.MEMORY_API_BASE = Config.COMPLETION_API_BASE
        Config.MEMORY_TEMPERATURE = Config.COMPLETION_TEMPERATURE
        Config.MEMORY_MAX_TOKENS = Config.COMPLETION_MAX_TOKENS
        Config.MEMORY_EXTRA_BODY = Config.COMPLETION_EXTRA_BODY
        Config.MEMORY_REASONING = Config.COMPLETION_REASONING
        Config.ANALYSIS_REASONING = Config.COMPLETION_REASONING
        Config.HEALER_REASONING = Config.COMPLETION_REASONING
        Config.TABLE_FINDER_REASONING = Config.COMPLETION_REASONING
        Config.TABLE_RERANK_REASONING = Config.COMPLETION_REASONING
        # Embedding endpoint: api_base/api_key are read live via os.environ; the
        # model name is import-frozen, so rebuild the embeddings client when it
        # changes (a model/dimension change also requires re-indexing the DB).
        _new_embed = os.getenv("EMBEDDING_MODEL", "").strip()
        if _new_embed and _new_embed != getattr(Config, "EMBEDDING_MODEL_NAME", ""):
            try:
                Config.EMBEDDING_MODEL_NAME = _new_embed
                Config.EMBEDDING_MODEL = EmbeddingsModel(model_name=_new_embed)
            except Exception as exc:  # pylint: disable=broad-except
                logging.warning("Embedding model reload failed: %s", exc)

    FIND_SYSTEM_PROMPT = """
    You map a natural-language database question to the schema by first
    identifying the ENTITIES the question refers to, then saying where each is
    found.

    Step 1 — Entities. Extract the concrete entities the query mentions: business
    objects, metrics / measures, attributes, filter fields, grouping keys, and
    join concepts. Decompose a compound question into its separate entities. Keep
    each entity a short noun phrase, not a sentence.

    Step 2 — Map each entity to a schema search hint:
    - If the entity names a business object or record grain, emit a TABLE hint.
    - If the entity names a measure, attribute, flag, key, or filter field, emit a
      COLUMN hint.
    - Cover every part of the query; produce a hint per distinct entity.
    - Keep hints generic — never embed specific codes, values, or conditions.
    - List hints in order of relevance.

    Keep in mind that the database that you work with has the following DB description: {db_description}.

    **Input:**
    * **Relational Database:** database name and the description of the database domain.
    * **Previous User Queries:** a list of previous queries (each prefixed "Query N:") for session context.
    * **User Query (Natural Language):** the user's current question.

    **Output:**
    * **Table Descriptions:** entities that name a table / business object.
    * **Column Descriptions:** entities that name a measure, attribute, or filter field.
    """

    Text_To_SQL_PROMPT = """
    You are a Text-to-SQL model. Your task is to generate SQL queries based on natural language questions and a provided database schema.

    **Instructions:**
    1. **Understand the Database Schema:** Carefully analyze the provided database schema to understand the tables, columns, data types, and relationships.
    2. **Consider Previous Queries:** Review the user's previous queries to understand the context of their current question and maintain consistency in your approach.
    3. **Interpret the User's Question:** Understand the user's question and identify the relevant entities, attributes, and relationships.
    4. **Generate the SQL Query:** Construct a valid SQL query that accurately reflects the user's question and uses the provided database schema.
    5. **Adhere to SQL Standards:** Ensure the generated SQL query follows standard SQL syntax and conventions.
    6. **Return Only the SQL:** Do not include any explanations, justifications, or additional text. Only return the generated SQL query.
    7. **Handle Ambiguity:** If the user's question is ambiguous, make reasonable assumptions based on the schema and previous queries to generate the most likely SQL query.
    8. **Handle Unknown Information:** If the user's question refers to information not present in the schema, return an appropriate error message or a query that retrieves as much relevant information as possible.
    9. **Prioritize Accuracy:** Accuracy is paramount. Ensure the generated SQL query returns the correct results.
    10. **Assume standard SQL dialect.**
    11. **Do not add any comments to the generated SQL.**
    12. **When you use WHERE clause, please use the exact value as the user provided, and dont make up values.**
    13. **If you dont have the value for the WHERE clause, use "TBD" for string and "1111" for number.**
    14. **Only create JOIN between tables based on the foreign key that point on referenced table and column.**
    15. **Do not create JOIN between tables that are not explicitly connected by foreign key in the input schema.**
    16. **Try to use explict condition column instead of indication wherever possible.**

    Keep in mind that the database that you work with has the following description: {db_description}.

    Before you start to answer, analyze the user_query step by step and try to understand the user's intent and the relevant tables and columns.

    **Input:**
    * **Database Schema:**
    You will be provided with part of the database schema that might be relevant to the user's question.
    With the following structure:
    {{"schema": [["table_name", description, foreign keys[list], [{{"column_name": "column_description", "data_type": "data_type",...}},...]],...]}}

    * **Previous Queries:**
    You will be provided with a list of the user's previous queries in this session. Each query will be prefixed with "Query N:" where N is the query number, followed by both the natural language question and the SQL query that was generated. Use these to maintain consistency and understand the user's evolving information needs.

    * **User Query (Natural Language):**
    You will be given a user's current question or request in natural language.
    """
