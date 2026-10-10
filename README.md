# Songwriter

AI-augmented songwriting workbench: deterministic phonetic analysis, comprehensive rhyme indexing, and lyric craft diagnostics exposed through a Web UI, REST API, and stdio Model Context Protocol (MCP) server.

---

## 1. What It Does

Large Language Models cannot hear acoustic signals. Because they predict text as subword tokens rather than speech phonemes, they frequently hallucinate syllable counts, confuse poetic meter, and recommend weak or false rhymes.

**Songwriter solves this deterministically.** It handles all auditory and metric evaluation using phonetic dictionaries, linguistic corpora, and stress analysis algorithms. Instead of asking a model to judge sound, Songwriter computes exact syllable counts, binary stress grids, 16 phonetic rhyme classifications, sensory imagery scores, and corpus cliché frequencies. 

These verdicts are returned as compact, pre-ranked ratings so that human songwriters and small/cheap language models can compose lyrics with metrical accuracy.

---

## 2. How It Works

Songwriter uses a layered architecture separating pure core algorithms from database gateways and protocol adapters:

```
[ Clients ]       Web UI (Browser)  |  MusicStudio (/v1)  |  AI Agents / LLMs (MCP)
                        \                     |                    /
[ Adapters ]             +------------- FastAPI App --------------+
                                              |
[ Facade ]                       service.Workbench (Unified API)
                               /              |               \
[ Domain ]              Craft Diagnostics  Phonetics Engine  Corpus Search
                         (Sensory/VAD)      (CMU/Stress)   (DuckDB/Parquet)
                               \              |               /
[ Storage ]                     +------ Flat Data Root -------+
                          (app.duckdb, songs.parquet, workbench.duckdb)
```

### The Analysis Pipeline
1. **Phonetic & Stress Decomposition:** Words are resolved against the CMU Pronouncing Dictionary to extract ARPAbet phoneme sequences. Each vowel phoneme carries an explicit stress level (`0` = unstressed, `1` = primary stress, `2` = secondary stress). For compound or derived out-of-vocabulary (OOV) terms, phonetic morphology rules and lexical lookups provide fallback pronunciations.
2. **Meter & Rhythm Grids:** Lines are syllabified and aligned to musical bars (e.g. 4/4 meter with 4 beats), producing a visual binary stress grid (`•` for unstressed, `/` for stressed) that immediately identifies awkward cadences.
3. **16-Class Rhyme Classification:** Rather than generic string suffix matching, rhymes are evaluated on vowel nuclei and coda consonant phoneme similarity:
   * *Strict:* Perfect rhymes, family rhymes, additive/subtractive rhymes.
   * *Slant & Phonic:* Assonance, consonance, slant, rich rhymes, eye rhymes.
   * *Complex:* Multi-syllabic (mosaic) rhymes, rhyming slang substitutions.
4. **Corpus Frequency & Cliché Scoring:** Querying an indexed 3.18-million-song corpus, Songwriter evaluates exact phrase frequencies (2–5 word n-grams). Lines containing overused tropes or predictable rhyme pairings are flagged with rarity metrics (`fresh`, `uncommon`, `cliche`).
5. **Craft Imagery & Emotion Profiling:** Tokens are mapped against empirical psychological lexicons:
   * *Sensory Concreteness:* Concreteness ratings (Brysbaert) and dominant sensory modalities (*visual*, *auditory*, *haptic*, *olfactory*, *gustatory*, *action*).
   * *VAD Emotion Model:* Valence (positivity), Arousal (energy), and Dominance (intensity) profiles to gauge lyric mood stability.

---

## 3. Features

* **Interactive Web Studio:** Full-featured browser UI for drafting songs, analyzing lyric takes, inspecting line-by-line metrics, and exploring reference blueprints.
* **16 Distinct Rhyme Categories:** Filter and rank rhymes by syllable count, corpus frequency, phonetic strength, register tags (slang, poetic, vulgar), and emotional alignment.
* **Cadence & Meter Diagnostics:** Real-time feedback on average syllable counts, variance, cadence consistency, and rhythm grid alignment.
* **Cliché Detection:** Identifies overused phrasing windows and predictable rhyme pairs backed by real-world corpus statistics.
* **Sensory & Emotional Mapping:** Visual breakdown of showing-vs-telling abstractions and emotional trajectories across verses, choruses, and bridges.
* **AI Cowriter Engine:** Interactive turn-by-turn lyric generator with section contour planning, interview setup wizards, and reference song blueprints.
* **MusicStudio Integration (`/v1` API):** Production REST contract for DAW integration, supporting song revisions, metadata synchronization, and direct binary audio take uploads.
* **Stdio MCP Server:** Plug-and-play Model Context Protocol tool provider compatible with Claude Desktop, Cursor, and any MCP client.

<img width="729" height="852" alt="image" src="https://github.com/user-attachments/assets/665a7fef-959d-4442-b026-6ba9d0defc4e" />

<img width="724" height="755" alt="image" src="https://github.com/user-attachments/assets/dd88f034-ad8a-4fbd-affc-dfe24f572d5b" />

<img width="731" height="779" alt="image" src="https://github.com/user-attachments/assets/303378c6-1822-46e0-9536-be68b1182a1f" />

<img width="731" height="826" alt="image" src="https://github.com/user-attachments/assets/bef5757f-887f-46b6-868a-cb5034ab888b" />

<img width="1040" height="836" alt="image" src="https://github.com/user-attachments/assets/ccb38f73-16f1-455c-beb4-9114d210e957" />

---

## 4. Setup & Running

### Requirements
* **Python:** `>= 3.13`
* **Dependency Manager:** [`uv`](https://docs.astral.sh/uv/)

---

### Local Quickstart

1. **Clone and install dependencies:**
   ```sh
   git clone https://github.com/rayone/songwriter.git
   cd songwriter
   uv sync --frozen --no-dev
   ```

2. **Download required runtime corpus data:**
   All runtime files reside flat inside the `data/` directory (zero subdirectories). Download and extract the two required corpus archives from release [`v0.2.0`](https://github.com/rayone/songwriter/releases/tag/v0.2.0):
   ```sh
   mkdir -p data
   curl -fL https://github.com/rayone/songwriter/releases/download/v0.2.0/app.duckdb.tar.gz | tar -xz -C data/
   curl -fL https://github.com/rayone/songwriter/releases/download/v0.2.0/songs.parquet.tar.gz | tar -xz -C data/
   ```

3. **Start the Web UI & REST API:**
   ```sh
   uv run python -m songwriter.web
   ```
   Open [http://localhost:8000](http://localhost:8000) in your browser. `workbench.duckdb` initializes automatically on first start.

4. **Or start the stdio MCP server:**
   ```sh
   uv run python -m songwriter.mcp.server
   ```

---

### Production Deployment (Linux with systemd)

Run the automated installer on any Linux host (Debian, Ubuntu, etc.):

```sh
curl -fsSLO https://raw.githubusercontent.com/rayone/songwriter/main/deploy/install.sh
sudo bash install.sh --service
```

The installer:
* Clones the repository to `/opt/songwriter`.
* Installs `uv` and locks dependencies with `--no-dev`.
* Fetches and verifies `app.duckdb` and `songs.parquet` release archives.
* Initializes a clean `workbench.duckdb` database schema.
* Configures and enables `/etc/systemd/system/songwriter.service`.
* Starts the web server bound to `http://0.0.0.0:8000`.

---

### Cloud Hosting (Render.com)

1. Create a **Web Service** connected to `rayone/songwriter`.
2. **Build Command:** `pip install uv && uv sync --frozen --no-dev`
3. **Start Command:** `bash deploy/render-start.sh`
4. **Storage:**
   * **Permanent (Paid Starter + Disk):** Add a Persistent Disk with mount path `/var/data` and set `SONGWRITER_DATA_DIR=/var/data`.
   * **Disposable Testing (Free Tier):** Set `SONGWRITER_DATA_DIR=/opt/render/project/src/data` in the Environment tab.
5. **Health Check Path:** `/healthz`

---

## 5. Configuration

All runtime settings are configured via environment variables in `src/songwriter/config.py`:

| Variable | Default | Description |
|---|---|---|
| `SONGWRITER_DATA_DIR` | `<repo>/data` | Directory holding `app.duckdb`, `songs.parquet`, and `workbench.duckdb`. |
| `SONGWRITER_LLM_ENDPOINT` | `http://127.0.0.1:11434/v1` | OpenAI-compatible base URL for the cowriter (e.g. Ollama, vLLM, OpenAI). |
| `SONGWRITER_LLM_API_KEY` | *(empty)* | Optional API key for the LLM endpoint. |
| `SONGWRITER_HOST` | `0.0.0.0` | HTTP bind host. |
| `SONGWRITER_PORT` | `8000` | HTTP bind port (falls back to platform `$PORT` on cloud PaaS). |
| `SONGWRITER_AUTH_USER` / `PASSWORD` | *(empty)* | Optional HTTP Basic authentication for routes except `/healthz`. |
| `SONGWRITER_CORS_ORIGINS` | *(empty)* | Comma-separated allowed CORS origins (empty = same-origin only). |
| `SONGWRITER_DUCKDB_THREADS` | *(empty)* | Cap on DuckDB query execution threads (empty = DuckDB default). |
| `SONGWRITER_DUCKDB_MEMORY_LIMIT`| *(empty)* | Cap on DuckDB RAM usage (e.g. `512MB` or `2GB`). |

---

## 6. Technical Review & Invariants

### Architectural Guarantees
* **Deterministic Computation:** Phonetic, metric, and rhyme verdicts are strictly mathematical and rule-based. Model hallucinations cannot corrupt phonetic scores or rhyme classifications.
* **Flat Single-Directory Storage:** All persistent artifacts (`app.duckdb`, `songs.parquet`, `workbench.duckdb`, and audio uploads/renders) reside flat directly inside `SONGWRITER_DATA_DIR` with zero nested subdirectories.
* **Safe Startup & Schema Lifecycle:** Applications initialize through a managed FastAPI lifespan (`init_db()`). Fresh installs create an empty `workbench.duckdb` without requiring a pre-seeded database.
* **Memory & Throughput Performance:**
  * In-memory CMUDict lookups execute in $O(1)$ time.
  * Line-by-line self-similarity matrices precompile token regexes and compute upper-triangle Jaccard overlaps ($j > i$), reducing regex operations from $N^2 + N$ to $N$.
  * High-frequency queries (phrase frequencies, word sensory ratings, emotion scores) are cached using LRU caches (`maxsize=32768`), returning defensive copies to avoid shared-state corruption.
  * Audio binary uploads stream chunks asynchronously (`anyio.open_file`) with immediate abort and file deletion if byte thresholds or checksums fail.

### Concurrency Model
* **Corpus Database (`app.duckdb`):** Opened in read-only mode via a process-wide `Database` gateway. Multiple concurrent threads access per-thread cursors safely.
* **Workbench Database (`workbench.duckdb`):** Open-per-read/write file lock semantics to support concurrent operations while releasing file locks promptly.
* **Worker Process Sizing:** Single-worker process per host (`WEB_CONCURRENCY=1`) is recommended because DuckDB is file-backed and does not support multi-process concurrent writes to the same database file.

### Operational Boundaries
* **External LLM Dependency:** The AI Cowriter requires an accessible OpenAI-compatible endpoint (`SONGWRITER_LLM_ENDPOINT`). The core workbench (rhymes, meter, statistics, search, craft analytics) functions fully offline without any LLM configured.
* **Single-Node Design:** Songwriter is built for local workstation, private LAN, or single-container instances. It is not designed as a distributed multi-node clustered database.

---

## License

Code is licensed under the [MIT License](LICENSE).
The song corpus dataset carries a verified MIT license and is authorized for full unrestricted use.
