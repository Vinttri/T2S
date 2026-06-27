"""Impala loader and query executor."""

import asyncio
import contextlib
import datetime
import decimal
import hashlib
import inspect
import logging
import os
import queue
import re
import threading
from typing import Any, AsyncGenerator, Dict, List, Tuple
from urllib.parse import parse_qs, urlencode, unquote, urlparse

from api.loaders.base_loader import BaseLoader
from api.tls import global_ssl_verification_disabled

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ImpalaConnectionError(Exception):
    """Exception raised when Impala connection fails."""


class ImpalaQueryError(Exception):
    """Exception raised when Impala query execution fails."""


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _strip_wrapping_quotes(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in {"'", '"'}:
        return cleaned[1:-1]
    return cleaned


def _split_semicolon_params(connection_url: str) -> tuple[str, dict[str, str]]:
    raw_url = connection_url.strip()
    if raw_url.lower().startswith("jdbc:impala://"):
        raw_url = "impala://" + raw_url[len("jdbc:impala://"):]

    if ";" not in raw_url:
        return raw_url, {}

    base_url, *parts = raw_url.split(";")
    params: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip().lower()
        if key:
            params[key] = _strip_wrapping_quotes(value) or ""
    return base_url, params


def _query_params(query: str) -> dict[str, str]:
    return {
        key.lower(): _strip_wrapping_quotes(values[-1]) or ""
        for key, values in parse_qs(query, keep_blank_values=True).items()
        if values
    }


def _param(params: dict[str, str], *names: str, default: str | None = None) -> str | None:
    for name in names:
        value = params.get(name.lower())
        if value not in (None, ""):
            return value
    return default


def _int_param(
    params: dict[str, str],
    *names: str,
    default: int,
    minimum: int = 1,
    maximum: int = 3600,
) -> int:
    raw_value = _param(params, *names, default=str(default))
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _auth_mechanism(params: dict[str, str]) -> str:
    raw_value = (_param(
        params,
        "auth_mechanism",
        "authmechanism",
        "authmech",
        default="NOSASL",
    ) or "NOSASL").strip()
    auth_mech_map = {
        "0": "NOSASL",
        "2": "PLAIN",
        "3": "LDAP",
    }
    return auth_mech_map.get(raw_value.upper(), raw_value.upper())


def _normalize_identifier(value: str) -> str:
    return str(value or "").strip().strip('"`[]').lower()


def _safe_identifier(value: str) -> str:
    normalized = _normalize_identifier(value)
    if not _IDENTIFIER_RE.match(normalized):
        raise ImpalaConnectionError(f"Invalid Impala identifier: {value}")
    return normalized


def _safe_qualified_table(database: str, table: str) -> str:
    return f"{_safe_identifier(database)}.{_safe_identifier(table)}"


class ImpalaLoader(BaseLoader):
    """Loader for Apache Impala schemas and SQL execution."""

    SCHEMA_MODIFYING_OPERATIONS = {
        "CREATE", "ALTER", "DROP", "RENAME", "TRUNCATE", "INVALIDATE", "REFRESH"
    }

    SCHEMA_PATTERNS = [
        r"^\s*CREATE\s+TABLE",
        r"^\s*CREATE\s+VIEW",
        r"^\s*ALTER\s+TABLE",
        r"^\s*DROP\s+TABLE",
        r"^\s*DROP\s+VIEW",
        r"^\s*TRUNCATE\s+TABLE",
        r"^\s*INVALIDATE\s+METADATA",
        r"^\s*REFRESH\s+",
    ]

    @staticmethod
    def _execute_sample_query(
        cursor: Any, table_name: str, col_name: str, sample_size: int = 10
    ) -> List[Any]:
        # Most-frequent values first: an arbitrary DISTINCT slice surfaces
        # noise while the dominant codes (the ones users filter by) stay
        # invisible. GROUP BY returns min(distinct, limit) rows, so flags
        # naturally yield 2-3 examples and code lists up to the limit.
        query = (
            f"SELECT `{col_name}` FROM {table_name} "
            f"WHERE `{col_name}` IS NOT NULL "
            f"GROUP BY `{col_name}` ORDER BY COUNT(*) DESC "
            f"LIMIT {int(sample_size)}"
        )
        cursor.execute(query)
        return [row[0] for row in cursor.fetchall() if row and row[0] is not None]

    @staticmethod
    def parse_connection_url(connection_url: str) -> dict[str, Any]:
        base_url, semicolon_params = _split_semicolon_params(connection_url)
        parsed = urlparse(base_url)
        params = semicolon_params | _query_params(parsed.query)
        scheme = parsed.scheme.lower()
        if scheme not in {"impala", "impala+http"}:
            raise ImpalaConnectionError("Invalid Impala URL format")

        database = parsed.path.strip("/").split("/")[0]
        database = _param(params, "database", "db", "schema", default=database) or ""
        if not database:
            raise ImpalaConnectionError("Impala URL must include a database path")

        use_http_transport = (
            scheme == "impala+http"
            or _truthy(_param(params, "use_http_transport", "http_transport", default="false"))
            or (_param(params, "transportmode", "transport_mode", default="").lower() == "http")
        )
        port = parsed.port or (28000 if use_http_transport else 21050)
        allow_self_signed = _truthy(
            _param(params, "allowselfsignedcerts", "allow_self_signed_certs", default="false")
        )
        verify_cert_value = _param(params, "verify_cert", "verifycert")
        ca_cert = _param(params, "ca_cert", "ca_cert_path", "ssl_ca_cert", "trusted_cert")
        truststore = _param(
            params,
            "truststore",
            "trust_store",
            "ssltruststore",
            "ssl_trust_store",
            "jks",
            "jks_path",
        )
        truststore_password = _param(
            params,
            "truststore_password",
            "trust_store_password",
            "ssltruststorepwd",
            "ssl_trust_store_pwd",
            "jks_password",
            default=os.getenv("IMPALA_TRUSTSTORE_PASSWORD", ""),
        )
        if allow_self_signed:
            verify_cert = False
        elif verify_cert_value is not None:
            verify_cert = _truthy(verify_cert_value)
        else:
            verify_cert = bool(ca_cert or truststore)
        if global_ssl_verification_disabled():
            verify_cert = False
            ca_cert = None
            truststore = None
        connect_timeout = _int_param(
            params,
            "connect_timeout",
            "connection_timeout",
            "timeout",
            default=int(os.getenv("IMPALA_CONNECT_TIMEOUT_SECONDS", "30") or "30"),
        )

        return {
            "host": parsed.hostname or "localhost",
            "port": port,
            "database": _safe_identifier(database),
            "graph": _safe_identifier(database),
            "user": unquote(parsed.username) if parsed.username else _param(params, "user", "username"),
            "password": (
                unquote(parsed.password) if parsed.password else _param(params, "password")
            ),
            "auth_mechanism": _auth_mechanism(params),
            "use_ssl": _truthy(_param(params, "use_ssl", "ssl", default="false")),
            "use_http_transport": use_http_transport,
            "http_path": _param(params, "http_path", "httppath", default=""),
            "metadata_path": _param(params, "metadata_path"),
            "metadata_schema": _param(params, "metadata_schema"),
            "replace": _truthy(_param(params, "replace", default="false")),
            "verify_cert": verify_cert,
            "ca_cert": ca_cert,
            "truststore": truststore,
            "truststore_password": truststore_password,
            "request_pool": _param(params, "request_pool", "requestpool"),
            "connect_timeout": connect_timeout,
        }

    @staticmethod
    def _jks_to_pem(truststore_path: str, truststore_password: str | None) -> str:
        try:
            import jks  # pylint: disable=import-outside-toplevel
            from cryptography import x509  # pylint: disable=import-outside-toplevel
            from cryptography.hazmat.primitives import serialization  # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            raise ImpalaConnectionError(
                "JKS truststore support requires pyjks. Convert the JKS to PEM "
                "and pass ca_cert=/path/to/ca.pem, or use the JKS-enabled image."
            ) from exc

        password = truststore_password or "changeit"
        truststore_path = unquote(truststore_path)
        digest = hashlib.sha256(f"{truststore_path}:{password}".encode()).hexdigest()[:16]
        output_dir = "/tmp/t2s_impala_certs"
        os.makedirs(output_dir, exist_ok=True)
        pem_path = os.path.join(output_dir, f"{digest}.pem")
        if os.path.exists(pem_path) and os.path.getsize(pem_path) > 0:
            return pem_path

        try:
            store = jks.KeyStore.load(truststore_path, password)
            cert_entries = list(store.certs.values())
            if not cert_entries:
                raise ImpalaConnectionError(f"JKS truststore has no certificates: {truststore_path}")
            with open(pem_path, "wb") as pem_file:
                for cert_entry in cert_entries:
                    cert = x509.load_der_x509_certificate(cert_entry.cert)
                    pem_file.write(cert.public_bytes(serialization.Encoding.PEM))
        except ImpalaConnectionError:
            raise
        except Exception as exc:
            raise ImpalaConnectionError(
                f"Could not read JKS truststore {truststore_path}: {exc}"
            ) from exc
        return pem_path

    @staticmethod
    def _resolve_ca_cert(options: dict[str, Any]) -> str | None:
        if options.get("ca_cert"):
            return unquote(str(options["ca_cert"]))
        if options.get("truststore"):
            return ImpalaLoader._jks_to_pem(
                str(options["truststore"]),
                options.get("truststore_password"),
            )
        return None

    @staticmethod
    def _connect_impl(connection_url: str):
        try:
            from impala.dbapi import connect  # pylint: disable=import-outside-toplevel
        except ImportError as exc:
            raise ImpalaConnectionError(
                "Impala Python driver is not installed. Install t2s[server] "
                "with impyla support or install impyla in the runtime image."
            ) from exc

        options = ImpalaLoader.parse_connection_url(connection_url)
        kwargs = {
            "host": options["host"],
            "port": options["port"],
            "database": options["database"],
            "auth_mechanism": options["auth_mechanism"],
            "use_ssl": options["use_ssl"],
            "use_http_transport": options["use_http_transport"],
        }
        if options["user"]:
            kwargs["user"] = options["user"]
        if options["password"]:
            kwargs["password"] = options["password"]
        if options["http_path"]:
            kwargs["http_path"] = options["http_path"]
        connect_signature = inspect.signature(connect).parameters
        if "verify_cert" in connect_signature:
            kwargs["verify_cert"] = options["verify_cert"]
        ca_cert = ImpalaLoader._resolve_ca_cert(options)
        if ca_cert and "ca_cert" in connect_signature:
            kwargs["ca_cert"] = ca_cert
        if "timeout" in connect_signature:
            kwargs["timeout"] = options["connect_timeout"]

        try:
            return connect(**kwargs)
        except Exception as exc:
            safe_target = f"{options['host']}:{options['port']}/{options['database']}"
            raise ImpalaConnectionError(
                f"Could not connect to Impala {safe_target}: {exc}"
            ) from exc

    @staticmethod
    def _connect(connection_url: str):
        options = ImpalaLoader.parse_connection_url(connection_url)
        timeout_seconds = int(options["connect_timeout"])
        if timeout_seconds <= 0:
            return ImpalaLoader._connect_impl(connection_url)

        result_queue: queue.Queue = queue.Queue(maxsize=1)
        abandoned = threading.Event()

        def connect_worker(url: str, output_queue: queue.Queue) -> None:
            try:
                conn = ImpalaLoader._connect_impl(url)
                if abandoned.is_set():
                    try:
                        conn.close()
                    except Exception:  # pylint: disable=broad-exception-caught
                        pass
                    return
                output_queue.put(("ok", conn))
            except Exception as exc:  # pylint: disable=broad-exception-caught
                if abandoned.is_set():
                    return
                output_queue.put(("error", repr(exc)))

        worker = threading.Thread(
            target=connect_worker,
            args=(connection_url, result_queue),
            daemon=True,
        )
        worker.start()
        worker.join(timeout_seconds)

        if worker.is_alive():
            abandoned.set()
            safe_target = f"{options['host']}:{options['port']}/{options['database']}"
            raise ImpalaConnectionError(
                f"Timed out connecting to Impala {safe_target} after {timeout_seconds}s"
            )

        try:
            status, payload = result_queue.get_nowait()
        except queue.Empty as exc:
            safe_target = f"{options['host']}:{options['port']}/{options['database']}"
            raise ImpalaConnectionError(
                f"Impala connection process exited without result for {safe_target}"
            ) from exc

        if status == "ok":
            return payload

        safe_target = f"{options['host']}:{options['port']}/{options['database']}"
        raise ImpalaConnectionError(
            f"Could not connect to Impala {safe_target}: {payload}"
        )

    @staticmethod
    def _apply_session_options(cursor: Any, options: dict[str, Any]) -> None:
        request_pool = options.get("request_pool")
        if request_pool:
            safe_pool = str(request_pool).replace("'", "''")
            cursor.execute(f"SET REQUEST_POOL='{safe_pool}'")

    @staticmethod
    def _serialize_value(value):
        if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
            return value.isoformat()
        if isinstance(value, decimal.Decimal):
            return float(value)
        return value

    @staticmethod
    def _describe_table(cursor: Any, database: str, table: str) -> dict[str, Any]:
        qualified_table = _safe_qualified_table(database, table)
        cursor.execute(f"DESCRIBE {qualified_table}")
        rows = cursor.fetchall()

        columns_info = {}
        for row in rows:
            if not row or not row[0]:
                continue
            column_name = str(row[0]).strip()
            if not column_name or column_name.startswith("#"):
                continue
            data_type = str(row[1] if len(row) > 1 else "unknown").strip() or "unknown"
            comment = str(row[2] if len(row) > 2 and row[2] is not None else "").strip()
            normalized_column = _normalize_identifier(column_name)
            description = comment or f"Column {normalized_column} of type {data_type}"
            columns_info[normalized_column] = {
                "type": data_type,
                "null": "unknown",
                "key": "NONE",
                "description": description,
                "default": None,
                # Value previews are collected in one parallel pass after the
                # metadata walk (see load()), not per column on a shared cursor.
                "sample_values": [],
            }

        table_name = qualified_table
        return {
            "description": f"Table {table_name}",
            "columns": columns_info,
            "foreign_keys": [],
            "col_descriptions": [
                column_info["description"] for column_info in columns_info.values()
            ],
        }

    @staticmethod
    def extract_tables_info(cursor: Any, database: str) -> Dict[str, Any]:
        safe_database = _safe_identifier(database)
        cursor.execute(f"SHOW TABLES IN {safe_database}")
        tables = [
            _normalize_identifier(row[0])
            for row in cursor.fetchall()
            if row and row[0]
        ]

        entities = {}
        for table in tables:
            table_name = _safe_qualified_table(safe_database, table)
            entities[table_name] = ImpalaLoader._describe_table(cursor, safe_database, table)
        return entities

    @staticmethod
    async def load(  # pylint: disable=arguments-differ
        prefix: str,
        connection_url: str,
        db=None,
    ) -> AsyncGenerator[tuple[bool, str], None]:
        """Load an Impala schema into FalkorDB.

        If the URL contains ``metadata_path=/path/to/yaml`` this delegates graph
        construction to the YAML loader and stores the Impala URL for execution.
        """
        options = ImpalaLoader.parse_connection_url(connection_url)

        if options.get("metadata_path"):
            from api.loaders.yaml_loader import YamlSchemaLoader  # pylint: disable=import-outside-toplevel

            yaml_query = urlencode({
                "graph": options["graph"],
                "schema": options.get("metadata_schema") or options["database"],
                "execute_url": connection_url,
                "replace": "true" if options["replace"] else "false",
            })
            yaml_url = f"yaml://{options['metadata_path']}?{yaml_query}"
            async for progress in YamlSchemaLoader.load(prefix, yaml_url, db=db):
                yield progress
            return

        conn = None
        cursor = None
        try:
            yield True, (
                f"Connecting to Impala {options['host']}:{options['port']} "
                f"database {options['database']} "
                f"(timeout {options['connect_timeout']}s)..."
            )
            conn = ImpalaLoader._connect(connection_url)
            cursor = conn.cursor()
            ImpalaLoader._apply_session_options(cursor, options)

            yield True, "Extracting Impala table information..."
            entities = ImpalaLoader.extract_tables_info(cursor, options["database"])
            relationships: dict[str, list[dict[str, str]]] = {}

            cursor.close()
            cursor = None
            conn.close()
            conn = None

            yield True, "Collecting value previews for filter-like columns (parallel)..."
            from api.loaders.yaml_loader import _enrich_entities_with_samples  # pylint: disable=import-outside-toplevel

            await asyncio.to_thread(_enrich_entities_with_samples, entities, connection_url)

            yield True, (
                f"Building RAG graph for Impala schema '{options['graph']}' "
                f"from {len(entities)} tables..."
            )
            from api.loaders.graph_loader import load_to_graph  # pylint: disable=import-outside-toplevel

            progress_queue: asyncio.Queue[str | object] = asyncio.Queue()
            done = object()
            graph_error: list[BaseException] = []

            async def report_graph_progress(message: str) -> None:
                await progress_queue.put(message)

            async def run_graph_load() -> None:
                try:
                    await load_to_graph(
                        f"{prefix}_{options['graph']}",
                        entities,
                        relationships,
                        db_name=options["graph"],
                        db_url=connection_url,
                        db=db,
                        generate_descriptions=False,
                        progress_callback=report_graph_progress,
                    )
                except Exception as exc:  # pylint: disable=broad-exception-caught
                    graph_error.append(exc)
                finally:
                    await progress_queue.put(done)

            graph_task = asyncio.create_task(run_graph_load())
            try:
                while True:
                    message = await progress_queue.get()
                    if message is done:
                        break
                    yield True, str(message)
                await graph_task
            finally:
                if not graph_task.done():
                    graph_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await graph_task

            if graph_error:
                raise graph_error[0]

            yield True, f"Impala schema loaded successfully. Found {len(entities)} tables."
        except ImpalaConnectionError as exc:
            logging.error("Impala connection error: %s", exc)
            yield False, str(exc)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.exception("Error loading Impala schema: %s", exc)
            yield False, "Failed to load Impala database schema"
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

    @staticmethod
    def is_schema_modifying_query(sql_query: str) -> Tuple[bool, str]:
        if not sql_query or not sql_query.strip():
            return False, ""
        normalized_query = sql_query.strip().upper()
        first_word = normalized_query.split()[0] if normalized_query.split() else ""
        if first_word in ImpalaLoader.SCHEMA_MODIFYING_OPERATIONS:
            for pattern in ImpalaLoader.SCHEMA_PATTERNS:
                if re.match(pattern, normalized_query, re.IGNORECASE):
                    return True, first_word
            return True, first_word
        return False, ""

    @staticmethod
    async def refresh_graph_schema(graph_id: str, db_url: str, db=None) -> Tuple[bool, str]:
        # Merge-style refresh: the database is the fact for structure (types,
        # new/removed tables and columns), while YAML/LLM descriptions and
        # declared REFERENCES links survive. Never drop-and-reload.
        try:
            from api.core.schema_refresh import merge_refresh_graph_schema  # pylint: disable=import-outside-toplevel

            return await merge_refresh_graph_schema(
                ImpalaLoader, graph_id, db_url, "impala", db=db,
            )
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logging.error("Error refreshing Impala graph schema: %s", exc)
            return False, "Error refreshing Impala graph schema"

    @staticmethod
    def execute_sql_query(sql_query: str, db_url: str) -> List[Dict[str, Any]]:
        conn = None
        cursor = None
        try:
            from api.config import Config  # pylint: disable=import-outside-toplevel

            options = ImpalaLoader.parse_connection_url(db_url)
            conn = ImpalaLoader._connect(db_url)
            cursor = conn.cursor()
            ImpalaLoader._apply_session_options(cursor, options)
            timeout_seconds = int(getattr(Config, "IMPALA_QUERY_TIMEOUT_SECONDS", 45) or 45)
            if timeout_seconds > 0:
                cursor.execute(f"SET QUERY_TIMEOUT_S={timeout_seconds}")
            mem_limit = str(getattr(Config, "IMPALA_MEM_LIMIT", "") or "").strip()
            if mem_limit:
                cursor.execute(f"SET MEM_LIMIT={mem_limit}")
            cursor.execute(sql_query)

            if cursor.description is not None:
                columns = [desc[0] for desc in cursor.description]
                rows = [
                    {
                        columns[index]: ImpalaLoader._serialize_value(row[index])
                        for index in range(len(columns))
                    }
                    for row in cursor.fetchall()
                ]
                return rows

            operation = sql_query.strip().split()[0].upper() if sql_query.strip() else "UNKNOWN"
            return [{"operation": operation, "status": "success"}]
        except ImpalaConnectionError:
            raise
        except Exception as exc:
            raise ImpalaQueryError(f"Impala query execution error: {exc}") from exc
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
