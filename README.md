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

* **Interactive Web Studio:** Full-featured browser UI for drafting songs, analyzing lyric takes, inspecting line-by-line metrics, and exploring reference blueprints. Import an artist from the licensed lyrics dataset, paste a song, or import a JSON batch.
* **16 Distinct Rhyme Categories:** Filter and rank rhymes by syllable count, corpus frequency, phonetic strength, register tags (slang, poetic, vulgar), and emotional alignment.
* **Cadence & Meter Diagnostics:** Real-time feedback on average syllable counts, variance, cadence consistency, and rhythm grid alignment.
* **Cliché Detection:** Identifies overused phrasing windows and predictable rhyme pairs backed by real-world corpus statistics.
* **Sensory & Emotional Mapping:** Visual breakdown of showing-vs-telling abstractions and emotional trajectories across verses, choruses, and bridges.
* **AI Cowriter Engine:** Interactive turn-by-turn lyric generator with section contour planning, interview setup wizards, and reference song blueprints. References lend structure without replacing your genre. When genres differ, Producer Questions offers your original genre (default), the reference genre, and an AI-recommended alternative. Indie and other niche genres work even without a matching reference; available niche corpus matches are ranked ahead of broad catalog fallbacks.
* **MusicStudio Integration (`/v1` API):** Contract 6 exposes the complete canonical song document, exact saved engine prompts, song revisions, and direct binary audio take uploads.
* **Stdio MCP Server:** Plug-and-play Model Context Protocol tool provider compatible with Claude Desktop, Cursor, and any MCP client.
* [**API for MusicStudio:** ](https://github.com/rayone/MusicStudio)


<img width="729" height="852" alt="image" src="https://github.com/user-attachments/assets/665a7fef-959d-4442-b026-6ba9d0defc4e" />

<img width="724" height="755" alt="image" src="https://github.com/user-attachments/assets/dd88f034-ad8a-4fbd-affc-dfe24f572d5b" />

<img width="731" height="779" alt="image" src="https://github.com/user-attachments/assets/303378c6-1822-46e0-9536-be68b1182a1f" />

<img width="731" height="826" alt="image" src="https://github.com/user-attachments/assets/bef5757f-887f-46b6-868a-cb5034ab888b" />

<img width="1040" height="836" alt="image" src="https://github.com/user-attachments/assets/ccb38f73-16f1-455c-beb4-9114d210e957" />

---

## 4. Setup & Running

### Versioning: code vs. data (read this first)

This project has **two independent version streams**, and the GitHub Releases page only shows the second one. This commonly causes confusion, so it is spelled out here:

| Stream | Where it lives | How it is versioned | What the GitHub "Releases" page shows |
|---|---|---|---|
| **Application code** | `main` branch commits | Short commit hash (e.g. `1af3e45`), surfaced in-app as `v0.1.0 · 1af3e45` | **Nothing by default** — app changes ship as commits, not tagged releases |
| **Runtime databases** | Release assets (`app.duckdb.tar.gz`, `songs.parquet.tar.gz`) | A data tag such as `v0.2.0` | The database-only release you see on the Releases page |

**Key points:**
* The `v0.2.0` release on the Releases page is a **data release** (the corpus databases). It is **not** the application version and rarely changes.
* **Application updates are plain commits on `main`.** They do not create a GitHub release, so the Releases page will not announce them. To see what changed in the app, use the [commit history](https://github.com/rayone/songwriter/commits/main), not the Releases page.
* The authoritative "what am I running" answer is the in-app build badge and the `/api/version` endpoint (see [Checking the running version](#checking-the-running-version)). The database tag is separate from the application build and the two move on their own schedules.
* To find the newest application build: `git -C <install dir> fetch && git -C <install dir> log --oneline origin/main -5`, or open the repo's commit history. The newest commit hash on `main` is the latest build.

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
   All runtime files reside flat inside the `data/` directory (zero subdirectories). Download and extract the two required corpus archives from the data release [`v0.2.0`](https://github.com/rayone/songwriter/releases/tag/v0.2.0). This tag versions the **databases only**, not the application code (see [Versioning](#versioning-code-vs-data-read-this-first)):
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

### YuE2 style prompts

New generations use the LLM to author comma-separated YuE2 tags from the
Step 1 brief/story, Step 2 production choices, and Step 3 producer answers.
The authoring prompt covers genre and influences, concrete instruments, mood,
vocal gender and tone, language, key/scale, and tempo—not just the short specs
line. Resolved production choices take precedence over reference/template hints.

Edit this prompt under **Settings → LLM Prompts → Engine Prompts (YuE2 & MiniMax)**.
Your saved prompt edits are preserved. Existing saved song/take prompts are not
rewritten; generate a new take to use the improved authoring.

### Canonical song document and API — contract 6

[`SongGeneration`](src/songwriter/ai/generation.py) is the single song schema for
authoring, generation, persistence, revisions, and API responses. The browser
loads its defaults from `GET /api/ai/song_defaults`; it does not maintain a
second song schema or separate model, song-ID, and take-group variables.
Operation-specific controls can wrap a `song` document without redeclaring its
parameters.

The document includes production metadata (`language`, `genre`, `subgenre`,
`moods`, `instruments`, `bpm`, `key`, `scale`, `time_signature`, `vocal`, and
`duration_hint_sec`), brief/story, reference/template choices, producer questions
and answers, sections, drafting attempts and validation, analytics, ABC score,
generation prompts, and engine prompts with authoring provenance.

* `POST /api/crafted_songs` accepts and returns the complete document.
* `GET /v1/songs/{song_id}` returns it directly; `?revision=N` returns the saved
  revision. `GET /v1/songs` contains complete documents in `songs`, alongside
  `next_cursor`.
* `engine_prompts.yue2`, `.minimax`, and `.songbench` contain the saved prompt
  strings. Reading, listing, formatting, or reopening a song never reauthors
  them. New generation authors prompts once from the canonical song.
* `sections` owns lyric content. `lyrics`, `instrumental`, and `has_abc` are
  read-only derived output fields, not independently stored song variables.
* New revision snapshots capture status. Historical snapshots that never
  recorded status return `null`, rather than borrowing the current status.
  Missing legacy language/duration remains `null`; previously discarded data
  cannot be recovered by this change.

**Breaking MusicStudio change:** update clients to contract 6. The `creative`
and `model_inputs` wrappers are removed. Use the canonical root fields:
`song_id` instead of `id`, `brief` instead of `summary`, `vocal` instead of
`vocal_style`, and `engine_prompts` for saved engine styles/captions. `key` is the
tonic (for example `Bb`); `scale` remains separate (`major`). The generated
OpenAPI schema documents the complete input/output fields.

### Adding or updating reference songs

In **Cowriter → Step 2: Reference & Specs**, expand **Add / update references**,
enter **Mr.Kitty** under **Artist to import**, and click **Import Artist**.
The app finds all matching artist records in the
[licensed English lyrics dataset](https://huggingface.co/datasets/theelderemo/genius-lyrics-cleaned)
and adds missing references without copying lyrics by hand. Artist lookup ignores
case, spaces, and punctuation, so **Mr. Kitty** also works.

This imports the available dataset snapshot, not a live Genius discography:
newer releases may be absent. Existing bundled tracks keep their IDs and music
enrichment. Previously imported tracks keep known music metadata while their
lyrics refresh. No BPM, key, chords, audio, or MIDI is guessed from the lyrics.
The completed import clears the MIDI-only filter and searches the source artist;
click **Select Reference** on a resulting card.

Imports run in the background and can take several minutes. Progress shows checked
records, matching songs, and completed files rather than one unchanged scan message.
Bounded I/O workers reuse HTTPS connections to scan the artist columns in every
dataset file, then read lyric columns only for row groups containing a match.
No DuckDB network extension download is required.

Reloading the tab reconnects to progress; restarting the server interrupts an
unfinished job. Network failures save no partial import. **Settings → Reference
Imports** controls the per-request timeout, not the whole job duration.

To supply a song yourself, enter its artist, title, and lyrics, then click
**Save Reference Song**. Keep `[Verse]`, `[Chorus]`, and other section headings
when available.

Genre, year, BPM, key, and scale are optional. Supply known values rather than
guessing: unspecified music metadata stays unspecified in the inspector.
Recommendations can match imported tracks when their BPM is supplied; selecting
a track manually does not require BPM. Generation can still use its usual
production defaults. Importing lyrics does not extract audio, chords, or MIDI.

For your own catalog file, use **Download Example JSON**, replace the example
with your songs, choose the file, then click **Import JSON Batch**. The file is
a JSON array of up to 200 objects. Required keys: `title`, `artist`, `lyrics`.
Optional keys: `genre`, `year`, `tempo_bpm`, `musical_key`, `scale`. Lyrics are
limited to 100,000 characters per song. This path uses the lyrics you supply.

Manual reimports of the same artist and title (ignoring case and repeated whitespace)
replace that imported record's lyrics and metadata without changing its ID.
Omitted optional metadata resets to unspecified (`misc` genre, `0` year).
Invalid batches save nothing. The search shows matches for the first imported
artist; other imported artists can be searched normally.

Imports are saved in **`workbench.duckdb`**, separately from the bundled corpus,
and become searchable and selectable immediately, without a restart or corpus
rebuild. They survive application and corpus updates when the data directory is
persistent; include `workbench.duckdb` in backups. They extend the reference
library, not the bundled word-frequency/rhyme statistics.

The same authenticated REST API is available at
`POST /api/references/import`, with body `{"songs": [song objects]}`. It returns
`added`, `updated`, `total`, and `song_ids`; imported IDs are negative and work
with the existing song-detail and blueprint endpoints.

For automated artist imports, use `POST /api/references/artist-import` with
`{"artist": "Mr.Kitty"}`. It returns HTTP 202 and a job snapshot. Read progress
with `GET /api/references/artist-import`; a second start while one is running
returns HTTP 409. The automated path is not limited to the manual JSON batch's
200 records.

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

**Update an existing install (including an LXC):**
```sh
sudo bash /opt/songwriter/deploy/install.sh --service --non-interactive
```
This reuses `deploy/.env`, pulls the configured branch, syncs dependencies,
restarts the service, and waits for the health check. The changes must have been
pushed to the remote repository first. Replace `/opt/songwriter` if installed elsewhere.

**Check the running version:** <a id="checking-the-running-version"></a>the header beneath **SONGWRITER** shows the package
version and the source commit, for example `v0.1.0 · 1af3e45`. Click it for the full
commit, branch, and exact tag at `/api/version` (normal application authentication
applies). `(modified)` indicates tracked source changes at startup. The commit is
captured when the server starts, so pulling without restarting does not make the
old process claim to be running a new build. Installs without Git explicitly show
`commit unavailable`. The installer also prints the deployed build after a successful
restart.

The application version shown here (a commit hash) is distinct from the `v0.2.0`
database release tag on the GitHub Releases page. See
[Versioning: code vs. data](#versioning-code-vs-data-read-this-first) for why
application updates do not appear as GitHub releases and how to find the latest build.

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

The **Settings** tab overrides these at runtime, saved to `workbench.duckdb` and applied
without a restart: LLM backend, generation defaults, validation rules (including the
Step 4 lyric critic's reply budget, its unusual word-pair threshold, and the minimum
songs for rhyme phrase examples), and every LLM prompt, including **Writing Rules** and
**Lyric Critic**, with version history and Reset to Default.

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
