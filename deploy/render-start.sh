#!/usr/bin/env bash
set -euo pipefail

resolve_data_dir() {
  if [ -n "${SONGWRITER_DATA_DIR:-}" ]; then
    if mkdir -p "$SONGWRITER_DATA_DIR" 2>/dev/null; then
      echo "$SONGWRITER_DATA_DIR"
      return 0
    fi
    echo "WARNING: SONGWRITER_DATA_DIR='$SONGWRITER_DATA_DIR' is not writable (no persistent disk attached?)." >&2
  fi

  if mkdir -p "/var/data" 2>/dev/null; then
    echo "/var/data"
    return 0
  fi

  echo "================================================================================" >&2
  echo "NOTICE: Running in ephemeral storage mode (data/)." >&2
  echo "Corpus data and user songs will reset on container restart or redeploy." >&2
  echo "For permanent storage on Render: attach a Persistent Disk mounted at /var/data." >&2
  echo "================================================================================" >&2
  mkdir -p "data"
  echo "data"
}

DATA_DIR="$(resolve_data_dir)"
export SONGWRITER_DATA_DIR="$DATA_DIR"
echo "Active storage directory: $DATA_DIR"

APP_DB="$DATA_DIR/app.duckdb"
SONGS_PQ="$DATA_DIR/songs.parquet"
RELEASE_TAG="${SONGWRITER_RELEASE_TAG:-v0.2.0}"

fetch_asset() {
  local asset_name="$1"
  local target_file="$2"
  local expected_member="$3"

  if [ -s "$target_file" ]; then
    return 0
  fi

  echo "Fetching $asset_name ($RELEASE_TAG)..."
  local tmp_tar
  tmp_tar="$(mktemp "$DATA_DIR/.${expected_member}.XXXXXX.tar.gz")"
  local tmp_file
  tmp_file="$(mktemp "$DATA_DIR/.${expected_member}.XXXXXX.part")"

  cleanup() {
    rm -f "$tmp_tar" "$tmp_file"
  }
  trap cleanup EXIT INT TERM

  curl -fL --retry 3 "https://github.com/rayone/songwriter/releases/download/$RELEASE_TAG/$asset_name" -o "$tmp_tar"

  # Validate that the archive contains exactly the expected member and nothing else
  if [ "$(tar -tzf "$tmp_tar")" != "$expected_member" ]; then
    echo "ERROR: $asset_name does not match expected single member $expected_member" >&2
    exit 1
  fi

  tar -xOzf "$tmp_tar" "$expected_member" > "$tmp_file"
  if [ ! -s "$tmp_file" ]; then
    echo "ERROR: extracted $expected_member is empty" >&2
    exit 1
  fi

  mv "$tmp_file" "$target_file"
  rm -f "$tmp_tar"
  trap - EXIT INT TERM
}

fetch_asset "app.duckdb.tar.gz" "$APP_DB" "app.duckdb"
fetch_asset "songs.parquet.tar.gz" "$SONGS_PQ" "songs.parquet"

echo "Starting Songwriter server on port ${PORT:-8000}..."
exec .venv/bin/python -m songwriter.web
