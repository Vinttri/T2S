"""Per-user app settings, persisted in FalkorDB (the graph DB), not a file.

Every user's Settings-page configuration (model endpoints, temperatures,
reasoning, embedding endpoint/dimensions, and UI prefs) is stored as a single
JSON blob on a ``(:AppSettings {user_id})`` node in a dedicated system graph.
This makes settings PER-USER and DB-resident (surviving restarts via the
FalkorDB data volume), and ready for multi-user deployments.

At app startup the default user's settings are loaded and applied to the process
``Config`` (see :mod:`api.app_factory` lifespan); on save, the requesting user's
settings are stored under their ``user_id`` and applied live.
"""

import json
import logging
from typing import Any, Dict

SETTINGS_GRAPH = "t2s_app_settings"


def store_writable() -> bool:
    """Settings are writable whenever the graph DB is reachable (always, locally)."""
    return True


async def load_settings(user_id: str, db=None) -> Dict[str, str]:
    """Return the persisted settings dict for *user_id* (``{}`` when none)."""
    if not user_id:
        return {}
    try:
        from api.core.db_resolver import resolve_db  # pylint: disable=import-outside-toplevel
        graph = resolve_db(db).select_graph(SETTINGS_GRAPH)
        result = await graph.query(
            "MATCH (s:AppSettings {user_id: $uid}) RETURN s.data",
            {"uid": str(user_id)},
        )
        if result.result_set and result.result_set[0] and result.result_set[0][0]:
            data = json.loads(result.result_set[0][0])
            if isinstance(data, dict):
                return {str(k): ("" if v is None else str(v)) for k, v in data.items()}
    except Exception as exc:  # pylint: disable=broad-except
        logging.warning("runtime_settings_store.load_settings(%s) failed: %s", user_id, exc)
    return {}


async def save_settings(user_id: str, values: Dict[str, Any], db=None) -> Dict[str, str]:
    """Merge *values* into *user_id*'s persisted settings and store them.

    Keys with a ``None`` value are removed. Returns the merged dict.
    """
    if not user_id:
        user_id = "default"
    merged = await load_settings(user_id, db=db)
    for key, value in (values or {}).items():
        if value is None:
            merged.pop(str(key), None)
        else:
            merged[str(key)] = str(value)
    from api.core.db_resolver import resolve_db  # pylint: disable=import-outside-toplevel
    graph = resolve_db(db).select_graph(SETTINGS_GRAPH)
    await graph.query(
        "MERGE (s:AppSettings {user_id: $uid}) SET s.data = $data",
        {"uid": str(user_id), "data": json.dumps(merged, ensure_ascii=False)},
    )
    return merged


async def apply_user_settings(user_id: str, db=None) -> Dict[str, str]:
    """Load *user_id*'s settings and apply them to the process ``Config`` live."""
    settings = await load_settings(user_id, db=db)
    if settings:
        try:
            from api.config import Config  # pylint: disable=import-outside-toplevel
            # Apply only the env-style keys Config understands (skip UI-only prefs).
            env_overrides = {k: v for k, v in settings.items() if k.isupper()}
            if env_overrides:
                Config.apply_runtime_overrides(env_overrides)
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("runtime_settings_store.apply_user_settings(%s): %s", user_id, exc)
    return settings
