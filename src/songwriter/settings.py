"""Runtime settings persisted in workbench.duckdb.

Resolution order for every key: DB value (set via the Settings page) wins, else
the environment variable, else the code default. This lets an operator change
the LLM endpoint/key and generation defaults from the UI and have them persist
across restarts, while env vars remain the deploy-time default.

The store is a single ``app_settings(key, value)`` table. Values are stored as
text; typed accessors coerce. Secrets (the API key) live in plaintext here, same
exposure as ``deploy/.env`` -- the file is local and gitignored.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from songwriter import config
from songwriter.db import Database, workbench_db

# key -> (env var name, code default). Only these keys are settable; a PUT of an
# unknown key is rejected so the table cannot accumulate junk.
_SPEC: dict[str, tuple[str, Any]] = {
    "llm_endpoint": ("SONGWRITER_LLM_ENDPOINT", config.LLM_ENDPOINT),
    "llm_api_key": ("SONGWRITER_LLM_API_KEY", config.LLM_API_KEY),
    "default_model": ("SONGWRITER_DEFAULT_MODEL", ""),
    "default_bpm": ("SONGWRITER_DEFAULT_BPM", 92),
    "default_music_key": ("SONGWRITER_DEFAULT_MUSIC_KEY", "C"),
    "default_scale": ("SONGWRITER_DEFAULT_SCALE", "major"),
    "default_vocal": ("SONGWRITER_DEFAULT_VOCAL", "melodic lead"),
    # Validation rules (deterministic line checks).
    "cliche_min_freq": ("SONGWRITER_CLICHE_MIN_FREQ", 8000),
    "cliche_check_enabled": ("SONGWRITER_CLICHE_CHECK", "true"),
    "melodic_delta_enabled": ("SONGWRITER_MELODIC_DELTA_CHECK", "true"),
    "idiom_allowlist": ("SONGWRITER_IDIOM_ALLOWLIST",
                        "no one knows\none knows\nlord knows\nnobody knows\nwho knows\n"
                        "god only knows\nheaven knows\ni don't know\nthe way it goes\n"
                        "as the story goes\nthe way it is\nhere we go\nlet me know\n"
                        "every single\nall the\nright now\nlook at\ncome on\n"
                        "out here\nevery time\none more time\nback and forth\n"
                        "side by side\nday and night\nnight and day"),
}

SECRET_KEYS = frozenset({"llm_api_key"})

_lock = threading.RLock()
_cache: dict[str, str] | None = None


def _ensure_table() -> Database:
    db = workbench_db()
    with db.transaction() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS app_settings "
            "(key VARCHAR PRIMARY KEY, value VARCHAR)"
        )
    return db


def _load() -> dict[str, str]:
    global _cache
    with _lock:
        if _cache is None:
            _ensure_table()
            db = workbench_db()
            rows = db.read("SELECT key, value FROM app_settings")
            _cache = {k: v for k, v in rows if v is not None}
        return dict(_cache)


def _raw(key: str) -> str | None:
    """DB value, else env var, else None (caller applies the code default)."""
    db_vals = _load()
    if key in db_vals:
        return db_vals[key]
    env_name = _SPEC[key][0]
    env_val = os.environ.get(env_name)
    if env_val is not None and env_val != "":
        return env_val
    return None


def get(key: str) -> Any:
    """Resolved value with code-default fallback, coerced to the default's type."""
    if key not in _SPEC:
        raise KeyError(key)
    default = _SPEC[key][1]
    raw = _raw(key)
    if raw is None:
        return default
    if isinstance(default, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default
    return raw


def get_str(key: str) -> str:
    return str(get(key))


def source(key: str) -> str:
    """Where the current value comes from: 'db', 'env' or 'default'."""
    if key in _load():
        return "db"
    env_val = os.environ.get(_SPEC[key][0])
    if env_val:
        return "env"
    return "default"


def set_many(values: dict[str, str]) -> None:
    """Persist settings. Empty string clears a key back to env/default."""
    global _cache
    unknown = set(values) - set(_SPEC)
    if unknown:
        raise KeyError(f"unknown settings: {sorted(unknown)}")
    db = _ensure_table()
    with _lock:
        with db.transaction() as cur:
            for k, v in values.items():
                if v is None or v == "":
                    cur.execute("DELETE FROM app_settings WHERE key = ?", [k])
                else:
                    cur.execute(
                        "INSERT INTO app_settings(key, value) VALUES (?, ?) "
                        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                        [k, str(v)],
                    )
        _cache = None  # force reload on next read


def all_resolved(mask_secrets: bool = True) -> dict[str, dict[str, Any]]:
    """Every settable key with its resolved value and source, for the API."""
    out: dict[str, dict[str, Any]] = {}
    for key in _SPEC:
        val = get(key)
        src = source(key)
        if mask_secrets and key in SECRET_KEYS:
            s = str(val)
            masked = (s[:3] + "…" + s[-4:]) if len(s) > 8 else ("set" if s else "")
            out[key] = {"value": masked, "source": src, "is_secret": True, "configured": bool(s)}
        else:
            out[key] = {"value": val, "source": src, "is_secret": key in SECRET_KEYS}
    return out


def keys() -> list[str]:
    return list(_SPEC)
