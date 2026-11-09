"""Tests run against a throwaway data directory.

Drafting a song saves it, so tests must never write into the real workbench
database. The corpus (app.duckdb, songs.parquet) is linked in read-only from the
configured data directory when present; tests that need it skip otherwise.
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

_SRC = Path(os.environ.get("SONGWRITER_DATA_DIR") or Path(__file__).resolve().parents[1] / "data")
_TMP = Path(tempfile.mkdtemp(prefix="songwriter-tests-"))
atexit.register(shutil.rmtree, _TMP, ignore_errors=True)

for name in ("app.duckdb", "songs.parquet"):
    if (_SRC / name).exists():
        (_TMP / name).symlink_to((_SRC / name).resolve())

os.environ["SONGWRITER_DATA_DIR"] = str(_TMP)
