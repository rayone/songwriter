# Songwriter

AI-augmented songwriting workbench: deterministic phonetic analysis + MCP tools
so an LLM never has to judge sound. The model gets short, capped, pre-ranked
verdicts on syllables, stress and rhyme instead of guessing at them.

## Quickstart

```sh
uv sync --frozen --no-dev
uv run python -m songwriter.web          # Web UI & API on http://0.0.0.0:8000
uv run python -m songwriter.mcp.server   # MCP stdio server
```

### Runtime Data

All runtime data lives flat in a single directory (`<data_dir>/`) with zero subdirectories:
* `app.duckdb` (rhyme dictionary & linguistic statistics, required)
* `songs.parquet` (reference song corpus, required)
* `workbench.duckdb` (user songs and revision history, initialized automatically)
* Uploaded and rendered audio files (`upload-*.bin`, `generation-*`) are stored flat in `<data_dir>/`

## Production Deployment

Run the installer on any Linux host with systemd:

```sh
curl -fsSLO https://raw.githubusercontent.com/rayone/songwriter/main/deploy/install.sh
sudo bash install.sh --service
```

The installer automatically downloads release data assets, installs dependencies via `uv`, creates a systemd service, and starts the server.

## Configuration

Configuration is environment-driven:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SONGWRITER_DATA_DIR` | `<repo>/data` | Runtime data directory (holds `app.duckdb`, `songs.parquet`, `workbench.duckdb`). |
| `SONGWRITER_LLM_ENDPOINT` | `http://127.0.0.1:11434/v1` | OpenAI-compatible LLM endpoint for the cowriter. |
| `SONGWRITER_LLM_API_KEY` | *(empty)* | Optional API key for the LLM endpoint. |
| `SONGWRITER_HOST` | `0.0.0.0` | HTTP bind host. |
| `SONGWRITER_PORT` | `8000` | HTTP bind port. |
| `SONGWRITER_AUTH_USER` / `SONGWRITER_AUTH_PASSWORD` | *(empty)* | Optional HTTP Basic login for all routes except `/healthz`. |
| `SONGWRITER_CORS_ORIGINS` | *(empty)* | Comma-separated allowed cross-origin hosts (empty = same-origin). |
| `SONGWRITER_DUCKDB_THREADS` | *(empty)* | DuckDB CPU thread limit (empty = DuckDB default). |
| `SONGWRITER_DUCKDB_MEMORY_LIMIT` | *(empty)* | DuckDB RAM memory limit (e.g. `1GB`). |

## License

MIT (see [LICENSE](LICENSE)).
