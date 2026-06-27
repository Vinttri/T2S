from __future__ import annotations

import os


_FALSE = {"0", "false", "no", "off", "disable", "disabled"}


def env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in _FALSE


def ssl_verify(*names: str, default: bool = False) -> bool:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value.strip().lower() not in _FALSE
    return env_flag("BENCH_APP_SSL_VERIFY", default)


def httpx_verify(*names: str, default: bool = False) -> bool:
    return ssl_verify(*names, default=default)
