"""Single source of truth for runtime configuration.

Every path, secret and bind address resolves here, via environment variables
with dev-friendly defaults, so no module hardcodes a filesystem path or key.
Production sets the ``SONGWRITER_*`` env vars;
local runs leave them unset and get the repo-local ``./data`` tree.
"""

from __future__ import annotations

import os
from pathlib import Path

# Project root = repo root (parents[2] from src/songwriter/config.py).
ROOT = Path(__file__).resolve().parents[2]

# Data directory: env override wins, else repo-local ./data (dev default).
DATA_DIR = Path(os.environ.get("SONGWRITER_DATA_DIR", ROOT / "data"))

CORPUS_DB = DATA_DIR / "app.duckdb"
WORKBENCH_DB = DATA_DIR / "workbench.duckdb"
SONGS_PARQUET = DATA_DIR / "songs.parquet"

# DuckDB resource caps (optional, empty = DuckDB defaults).
DUCKDB_THREADS = os.environ.get("SONGWRITER_DUCKDB_THREADS", "")
DUCKDB_MEMORY_LIMIT = os.environ.get("SONGWRITER_DUCKDB_MEMORY_LIMIT", "")

# LLM backend for the cowriter (previously hardcoded in cowriter.py).
LLM_ENDPOINT = os.environ.get("SONGWRITER_LLM_ENDPOINT", "http://127.0.0.1:11434/v1")
LLM_API_KEY = os.environ.get("SONGWRITER_LLM_API_KEY", "")

# HTTP bind for the web server.
HTTP_HOST = os.environ.get("SONGWRITER_HOST", "0.0.0.0")
HTTP_PORT = int(os.environ.get("SONGWRITER_PORT", os.environ.get("PORT", "8000")))

# HTTP Basic auth for every route except /healthz. Both must be set to enable it.
AUTH_USER = os.environ.get("SONGWRITER_AUTH_USER", "")
AUTH_PASSWORD = os.environ.get("SONGWRITER_AUTH_PASSWORD", "")
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASSWORD)


# Browser origins allowed to call the API cross-origin, comma-separated. Empty
# means same-origin only (the bundled UI is served from the same origin).
CORS_ORIGINS = [o.strip() for o in os.environ.get("SONGWRITER_CORS_ORIGINS", "").split(",") if o.strip()]

