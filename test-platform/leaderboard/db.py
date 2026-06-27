from __future__ import annotations

from dataclasses import dataclass, field
import os
from urllib.parse import parse_qs, unquote, urlsplit

from .redaction import safe_exception


@dataclass
class SelectResult:
    ok: bool
    rows: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    row_count: int = 0
    error: str | None = None


class PgExecutor:
    """Minimal read-only SQL executor.

    Supports Postgres DSNs through psycopg and Impala DSNs through impyla.
    The class name is kept for compatibility with the rest of the app.
    """

    def __init__(self, dsn: str, statement_timeout_ms: int = 30000):
        self.dsn = dsn
        self.statement_timeout_ms = statement_timeout_ms

    def _connect(self):
        if self._scheme() == "impala":
            return self._connect_impala()
        try:
            import psycopg  # v3

            return psycopg.connect(self.dsn)
        except ImportError:
            import psycopg2

            return psycopg2.connect(self.dsn)

    def _scheme(self) -> str:
        return (urlsplit(self.dsn).scheme or "").lower()

    @staticmethod
    def _bool(value, default=False) -> bool:
        if value is None or value == "":
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return str(value).strip().lower() not in {"0", "false", "no", "off", "disable", "disabled"}

    @staticmethod
    def _first(q: dict, key: str, default=None):
        vals = q.get(key)
        return vals[0] if vals else default

    def _secret_values(self) -> list[str]:
        try:
            parts = urlsplit(self.dsn)
            q = parse_qs(parts.query, keep_blank_values=True)
        except Exception:
            return [self.dsn]
        values = [self.dsn, parts.username or "", parts.password or ""]
        for key in ("user", "username", "login", "password", "passwd", "pwd", "token", "jwt"):
            values.extend(q.get(key) or [])
        return [unquote(str(v)) for v in values if v]

    def _impala_params(self) -> tuple[dict, dict]:
        parts = urlsplit(self.dsn)
        q = parse_qs(parts.query, keep_blank_values=True)
        timeout = self._first(q, "connect_timeout", self._first(q, "timeout"))
        verify_default = self._env_bool("BENCH_APP_DB_SSL_VERIFY", self._env_bool("BENCH_APP_SSL_VERIFY", False))
        params = {
            "host": parts.hostname or "localhost",
            "port": parts.port or 21050,
            "database": unquote(parts.path.lstrip("/")) or None,
            "timeout": int(timeout) if timeout else None,
            "use_ssl": self._bool(self._first(q, "use_ssl"), False),
            "verify_cert": self._bool(self._first(q, "verify_cert"), verify_default),
            "auth_mechanism": self._first(q, "auth_mechanism", "NOSASL"),
            "user": unquote(parts.username or "") or self._first(q, "user"),
            "password": unquote(parts.password or "") or self._first(q, "password"),
            "kerberos_service_name": self._first(q, "kerberos_service_name", "impala"),
            "use_http_transport": self._bool(self._first(q, "use_http_transport"), False),
            "http_path": self._first(q, "http_path", ""),
            "retries": int(self._first(q, "retries", 3) or 3),
        }
        if self._first(q, "ca_cert"):
            params["ca_cert"] = self._first(q, "ca_cert")
        if self._first(q, "jwt"):
            params["jwt"] = self._first(q, "jwt")
        runtime = {
            "request_pool": self._first(q, "request_pool"),
            "graph": self._first(q, "graph"),
        }
        return {k: v for k, v in params.items() if v is not None}, runtime

    def _connect_impala(self):
        try:
            from impala import dbapi
        except ImportError as exc:
            raise RuntimeError("Для impala:// DSN нужен пакет impyla. Установите dependency: impyla>=0.23") from exc
        params, _runtime = self._impala_params()
        return dbapi.connect(**params)

    def _apply_session_settings(self, cur):
        if self._scheme() == "impala":
            _params, runtime = self._impala_params()
            if runtime.get("request_pool"):
                pool = str(runtime["request_pool"]).replace("'", "''")
                cur.execute(f"SET REQUEST_POOL='{pool}'")
            if self.statement_timeout_ms:
                seconds = max(1, int(self.statement_timeout_ms / 1000))
                for option in ("QUERY_TIMEOUT_S", "EXEC_TIME_LIMIT_S"):
                    try:
                        cur.execute(f"SET {option}={seconds}")
                    except Exception:  # noqa: BLE001
                        pass
            return
        if self.statement_timeout_ms:
            cur.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")

    def execute_select(self, sql) -> SelectResult:
        if not sql:
            return SelectResult(ok=False, error="empty SQL")
        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()
            self._apply_session_settings(cur)
            cur.execute(sql)
            if cur.description:
                cols = [d[0] for d in cur.description]
                rows = [tuple(r) for r in cur.fetchall()]
            else:
                cols, rows = [], []
            cur.close()
            return SelectResult(ok=True, rows=rows, columns=cols, row_count=len(rows))
        except Exception as exc:  # noqa: BLE001
            return SelectResult(ok=False, error=safe_exception(exc, extra_secrets=self._secret_values()))
        finally:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:  # noqa: BLE001
                    pass
                conn.close()
