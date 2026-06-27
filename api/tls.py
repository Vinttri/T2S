"""TLS verification controls for controlled internal deployments."""

import logging
import os
import ssl
from typing import Any


_FALSE_VALUES = {"0", "false", "no", "n", "off", "disable", "disabled"}
_TRUE_VALUES = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_CONFIGURED = False


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    return default


def global_ssl_verification_disabled() -> bool:
    """Return True when T2S should skip TLS certificate verification."""
    return _bool_env("T2S_DISABLE_SSL_VERIFY") or _bool_env("DISABLE_SSL_VERIFY")


def _unverified_default_context(*_args: Any, **_kwargs: Any) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _disable_requests_verification() -> None:
    try:
        import requests  # pylint: disable=import-outside-toplevel
        import urllib3  # pylint: disable=import-outside-toplevel
    except Exception:  # pylint: disable=broad-exception-caught
        return

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    original_merge = requests.Session.merge_environment_settings

    def merge_environment_settings(self, url, proxies, stream, verify, cert):
        settings = original_merge(self, url, proxies, stream, verify, cert)
        settings["verify"] = False
        return settings

    requests.Session.merge_environment_settings = merge_environment_settings


def _disable_litellm_verification() -> None:
    try:
        import litellm  # pylint: disable=import-outside-toplevel
    except Exception:  # pylint: disable=broad-exception-caught
        return
    try:
        litellm.ssl_verify = False
    except Exception:  # pylint: disable=broad-exception-caught
        pass


def _disable_httpx_verification() -> None:
    """Patch httpx context creation at every module that bound the symbol.

    httpx does not use ssl.create_default_context, and from-imports bind
    create_ssl_context into several httpx modules at import time — patching
    only httpx._config misses clients built via already-bound references.
    """
    try:
        import httpx  # pylint: disable=import-outside-toplevel
    except Exception:  # pylint: disable=broad-exception-caught
        return

    def _unverified_httpx_context(*_args: Any, **_kwargs: Any) -> ssl.SSLContext:
        return _unverified_default_context()

    modules = [httpx]
    for name in ("_config", "_client", "_transports.default"):
        try:
            module = __import__(f"httpx.{name}", fromlist=["_"])
            modules.append(module)
        except Exception:  # pylint: disable=broad-exception-caught
            continue
    for module in modules:
        if hasattr(module, "create_ssl_context"):
            try:
                module.create_ssl_context = _unverified_httpx_context
            except Exception:  # pylint: disable=broad-exception-caught
                continue


def configure_global_ssl_verification() -> None:
    """Disable TLS certificate verification process-wide when explicitly requested."""
    global _CONFIGURED  # pylint: disable=global-statement
    if _CONFIGURED or not global_ssl_verification_disabled():
        return

    _CONFIGURED = True
    os.environ["PYTHONHTTPSVERIFY"] = "0"
    os.environ["CURL_CA_BUNDLE"] = ""
    os.environ["REQUESTS_CA_BUNDLE"] = ""
    # litellm builds its own httpx clients and consults this env (str_to_bool)
    # before litellm.ssl_verify — covers completion AND embedding calls.
    os.environ["SSL_VERIFY"] = "false"

    ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[attr-defined]
    ssl.create_default_context = _unverified_default_context
    _disable_requests_verification()
    _disable_litellm_verification()
    _disable_httpx_verification()
    logging.warning("TLS certificate verification is disabled for T2S process")
