from __future__ import annotations

import os
import re
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit


SECRET_KEY_NAMES = {
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "passwd",
    "pwd",
    "pass",
    "user",
    "username",
    "login",
}
SECRET_KEY_SUFFIXES = ("_token", "_secret", "_password", "_passwd", "_pwd", "_pass", "_api_key", "_apikey")

_URL_WITH_USERINFO_RE = re.compile(
    r"\b([a-z][a-z0-9+.-]*://)([^/\s?#@]+@)([^/\s?#]+)",
    re.IGNORECASE,
)
_KEY_VALUE_SECRET_RE = re.compile(
    r"(?i)\b(user|username|login|password|pass|passwd|pwd|api_key|apikey|token|secret)\s*=\s*"
    r"(\"[^\"]*\"|'[^']*'|[^\s;,&]+)"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:authorization|api_key|apikey|token|secret|password|passwd|pwd|pass|user|username|login)"\s*:\s*)'
    r'("[^"]*"|[^\s,}]+)'
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|api_key|apikey|token|secret|password|passwd|pwd|pass|user|username|login)=)[^&\s]+"
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer)\s+[A-Za-z0-9._~+/\-=]+")
_COMMON_TOKEN_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|sk-or-[A-Za-z0-9_-]{8,}|app-[A-Za-z0-9_-]{12,})\b")


def is_secret_key(key: str) -> bool:
    low = str(key or "").lower()
    norm = re.sub(r"[^a-z0-9]+", "_", low).strip("_")
    if norm in SECRET_KEY_NAMES:
        return True
    return any(norm.endswith(suffix) for suffix in SECRET_KEY_SUFFIXES)


def redact_url(value: str) -> str:
    if not value:
        return value
    try:
        parts = urlsplit(value)
    except Exception:
        return value
    if not parts.scheme or not parts.netloc or "@" not in parts.netloc:
        return value
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"<redacted>@{host}", parts.path, parts.query, parts.fragment))


def _env_secret_values() -> list[str]:
    vals: list[str] = []
    for key, value in os.environ.items():
        if key in {"PWD", "OLDPWD", "HOME", "USER", "LOGNAME"}:
            continue
        if not value or len(value) < 6:
            continue
        if is_secret_key(key):
            vals.append(value)
    return vals


def redact_text(value: Any, *, extra_secrets: Iterable[Any] | None = None) -> str:
    text = "" if value is None else str(value)
    if not text:
        return text

    text = _URL_WITH_USERINFO_RE.sub(lambda m: f"{m.group(1)}<redacted>@{m.group(3)}", text)
    text = _QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}<redacted>", text)
    text = _KEY_VALUE_SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    text = _JSON_SECRET_RE.sub(lambda m: f"{m.group(1)}\"<redacted>\"", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} <redacted>", text)
    text = _COMMON_TOKEN_RE.sub("<redacted>", text)

    for secret in [*(extra_secrets or ()), *_env_secret_values()]:
        if not secret:
            continue
        secret_text = str(secret)
        if len(secret_text) >= 6:
            text = text.replace(secret_text, "<redacted>")
    return text


def redact_obj(value: Any, *, extra_secrets: Iterable[Any] | None = None) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key_text = str(key)
            if is_secret_key(key_text):
                out[key] = "<redacted>" if item not in (None, "") else item
            else:
                out[key] = redact_obj(item, extra_secrets=extra_secrets)
        return out
    if isinstance(value, list):
        return [redact_obj(item, extra_secrets=extra_secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_obj(item, extra_secrets=extra_secrets) for item in value)
    if isinstance(value, str):
        return redact_text(value, extra_secrets=extra_secrets)
    return value


def safe_exception(exc: Exception, *, extra_secrets: Iterable[Any] | None = None, limit: int | None = None) -> str:
    text = redact_text(f"{type(exc).__name__}: {exc}", extra_secrets=extra_secrets)
    return text[:limit] if limit else text
