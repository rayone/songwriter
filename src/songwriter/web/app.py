"""FastAPI web application for Songwriter Studio & Cowriter.

Theme: Tokyo Night (folke/tokyonight.nvim - Night variant)
Features:
  * Unified rhyme engine (4M+ pairs, exact syllables, freshness verdicts)
  * DuckDB song corpus explorer (3.18M songs, sub-second lyrics search)
  * Local MusicStudio production templates (5,960 templates with FTS5 semantic search)
  * AI Songwriting Cowriter with complete model packaging for YuE2 and MiniMax Music 03
  * Full JSON REST API with OpenAPI/Swagger at /docs
"""

from __future__ import annotations

import html
import json
import re
import traceback
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager
import duckdb
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from songwriter import settings as app_settings
from songwriter import prompt_templates
from songwriter.ai.cowriter import SongwriterCowriter
from songwriter.ai.generation import SongGeneration
from songwriter.db import corpus_db
from songwriter.lexicon.query import RhymeDB, RhymeType
from songwriter.service import Workbench

from songwriter.web.musicstudio_v1 import router as musicstudio_v1_router, init_db
from songwriter.config import AUTH_ENABLED, AUTH_PASSWORD, AUTH_USER, CORPUS_DB, CORS_ORIGINS, DATA_DIR, SONGS_PARQUET, WORKBENCH_DB
from songwriter.web.auth import BasicAuthMiddleware
DB_PATH = CORPUS_DB
RHYME_DB_PATH = CORPUS_DB
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Songwriter Studio & Cowriter API",
    description="Deterministic phonetic analysis, 4M+ unified rhyme database, 3.18M song corpus explorer, and AI music cowriter.",
    version="1.2.0",
    lifespan=lifespan,
)

# Same-origin by default: the bundled UI needs no CORS. Cross-origin callers
# must be listed in SONGWRITER_CORS_ORIGINS; credentials are never sent with "*".
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials="*" not in CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
# Added last so it runs first: nothing (CORS preflight included) bypasses auth.
if AUTH_ENABLED:
    app.add_middleware(BasicAuthMiddleware, username=AUTH_USER, password=AUTH_PASSWORD)
app.include_router(musicstudio_v1_router)


wb = Workbench(DB_PATH)
cowriter = SongwriterCowriter()

# The rebuilt rhyme dictionary. Optional so the rest of the API still serves if
# it has not been built yet; /api/rhymes reports that rather than failing opaquely.
try:
    rhyme_db: RhymeDB | None = RhymeDB(RHYME_DB_PATH)
except FileNotFoundError:
    rhyme_db = None

_WORD_RE = re.compile(r"\b[a-zA-Z']+\b")

# ---------------------------------------------------------------- Models

class AnalyzeLineRequest(BaseModel):
    text: str = Field(..., description="Lyric line to analyze")
    beats: int = Field(default=4, description="Beats per measure (usually 4)")


class CheckDraftRequest(BaseModel):
    lines: list[str] = Field(..., description="List of lyric lines")
    kind: str = Field(default="verse", description="Section kind: verse, chorus, bridge, hook")
    target_syllables: int | None = Field(default=None, description="Optional target syllable count per line")


class ValidateFormRequest(BaseModel):
    form: str = Field(..., description="Poetic form name (sonnet, ballad, limerick, haiku, or scheme:ABAB)")
    lines: list[str] = Field(..., description="Draft lines")
    word: str = Field(default="", description="Target word for acrostic form")


class GenerateRequest(BaseModel):
    title: str | None = Field(default=None, description="Song title")
    prompt: str = Field(..., description="Song brief: story, scene, or subject")
    style: str | None = Field(default=None, description="Generic style direction: genre, mood, instrumentation, tempo feel")
    genre: str | None = Field(default=None, description="Genre selected from DB")
    template_id: int | None = Field(default=None, description="Production template ID")
    bpm: int | None = Field(default=None, description="BPM override")
    music_key: str | None = Field(default=None, description="Key override")
    scale: str | None = Field(default=None, description="Scale override")
    vocal: str | None = Field(default=None, description="Vocal delivery")
    vocal_roles: dict[str, str] | None = Field(default=None, description="Duet roles (singer_a, singer_b)")
    reference_song: dict[str, Any] | None = Field(default=None, description="Reference song blueprint")
    banned_words: list[str] | None = Field(default=None, description="Stock rhymes or phrases to exclude")
    rhyme_scheme: str | None = Field(default=None, description="Target rhyme scheme (AABB, ABAB, etc.)")
    poetic_form: str | None = Field(default=None, description="Poetic form if any")
    section_plan: str | None = Field(default=None, description="standard, compact, hook-led, through-composed")
    syllable_target: str | None = Field(default=None, description="Syllables per line, e.g. 8-10")
    answers: dict[str, str] | None = Field(default=None, description="Answers to the interview questions")
    engine_target: str = Field(default="standard", description="Audio engine target: yue2, minimax, or standard")
    chat_history: list[dict[str, str]] | None = Field(default=None, description="Recent conversation messages")
    mode: str = Field(default="creator", description="creator (full song) or guide (co-writing)")
    model: str = Field(default="auto/chat", description="Model identifier")
    temperature: float = Field(default=0.7, description="Sampling temperature")
    section_by_section: bool = Field(default=True, description="Draft and validate each section, then polish")
    author_engine_prompt: bool = Field(default=True, description="Have the model write the YuE2/MiniMax engine prompt")


class PreviewInstructionsRequest(BaseModel):
    prompt: str = Field(default="", description="Song brief")
    style: str | None = Field(default=None, description="Generic style direction")
    genre: str | None = Field(default=None, description="Genre")
    template_id: int | None = Field(default=None, description="Template ID")
    bpm: int | None = Field(default=None, description="BPM override")
    music_key: str | None = Field(default=None, description="Key override")
    scale: str | None = Field(default=None, description="Scale override")
    vocal: str | None = Field(default=None, description="Vocal delivery")
    vocal_roles: dict[str, str] | None = Field(default=None, description="Duet roles")
    reference_song: dict[str, Any] | None = Field(default=None, description="Reference song blueprint")
    banned_words: list[str] | None = Field(default=None, description="Excluded stock rhymes")
    rhyme_scheme: str | None = Field(default=None, description="Rhyme scheme")
    poetic_form: str | None = Field(default=None, description="Poetic form")
    section_plan: str | None = Field(default=None, description="Section plan key")
    syllable_target: str | None = Field(default=None, description="Syllables per line")
    answers: dict[str, str] | None = Field(default=None, description="Interview answers")
    engine_target: str = Field(default="standard")
    mode: str = Field(default="creator")
    model: str = Field(default="auto/chat")


class InterviewRequest(BaseModel):
    brief: str = Field(default="", description="Song brief: story, scene, or subject")
    style: str | None = Field(default=None, description="Generic style direction")
    template_id: int | None = Field(default=None, description="Production template ID")
    reference_song: dict[str, Any] | None = Field(default=None, description="Reference song blueprint")
    story: str | None = Field(default="", description="Step 1 narrative; questions and their defaults must agree with it")
    model: str = Field(default="", description="Model identifier selected by user")
    use_model: bool = Field(default=True, description="Ask the model for brief-specific questions")
class StorylineRequest(BaseModel):
    title: str = Field(default="", description="Song title")
    brief: str = Field(..., description="Song brief: story, scene, or subject")
    style: str | None = Field(default="", description="Style direction")
    genre: str | None = Field(default="", description="Genre")
    bpm: float | int | None = Field(default=None, description="Tempo in BPM")
    key: str | None = Field(default=None, description="Key")
    scale: str | None = Field(default=None, description="Scale")
    template_title: str | None = Field(default="", description="Template title")
    template_id: int | None = Field(default=None, description="Selected production template ID")
    model: str = Field(default="", description="Model identifier selected by user")

class RecommendReferencesRequest(BaseModel):
    brief: str = Field(default="", description="Song brief")
    story: str = Field(default="", description="Storyline treatment")
    style: str | None = Field(default="", description="Style direction")
    genre: str | None = Field(default="", description="Genre")
    bpm: float | None = Field(default=None, description="Tempo in BPM")
    key: str | None = Field(default=None, description="Musical Key")
    scale: str | None = Field(default=None, description="Musical Scale (major/minor)")
    template_id: int | None = Field(default=None, description="Selected template ID")
    limit: int = Field(default=4, description="Max recommendations")
    has_midi: bool = Field(default=False, description="Only songs that have an imported MIDI (ABC score)")


class FormatMusicRequest(BaseModel):
    text: str = Field(..., description="Raw song text or lyrics to format")
    engine: str = Field(default="all", description="yue2, minimax, or all")
    template_id: int | None = Field(default=None, description="Production template ID")
    genre: str | None = Field(default=None, description="Optional genre override")
    title: str | None = Field(default="Untitled", description="Optional song title")

class DraftInteractiveRequest(BaseModel):
    prompt: str = Field(default="An original song", description="Song brief")
    title: str | None = Field(default="Untitled Song", description="Song title")
    style: str | None = Field(default=None, description="Style description")
    genre: str | None = Field(default=None, description="Genre")
    template_id: int | None = Field(default=None, description="Template ID")
    bpm: int | None = Field(default=None, description="BPM")
    music_key: str | None = Field(default=None, description="Key")
    scale: str | None = Field(default=None, description="Scale")
    vocal: str | None = Field(default=None, description="Vocal")
    vocal_roles: dict[str, str] | None = Field(default=None, description="Duet roles")
    reference_song: dict[str, Any] | None = Field(default=None, description="Reference song")
    banned_words: list[str] | None = Field(default=None, description="Banned words")
    rhyme_scheme: str | None = Field(default=None, description="Rhyme scheme")
    section_plan: str | None = Field(default=None, description="Section plan")
    syllable_target: str | None = Field(default=None, description="Syllable target")
    answers: dict[str, Any] | None = Field(default=None, description="Producer interview answers")
    questions: list[dict[str, Any]] | None = Field(default=None, description="Step 3 questions as shown (id, question, why, options, category)")
    song_id: str | None = Field(default=None, description="Saved song being revised; regenerating saves a new revision of it")
    story: str | None = Field(default=None, description="3-act story")
    model: str = Field(default="", description="Model identifier selected by user")
    max_retries: int = Field(default=5, description="Max validation retry loops per line")
    max_sections: int | None = Field(default=3, description="Max sections to draft interactively")

class DraftTurnRequest(DraftInteractiveRequest):
    accepted: list[dict[str, Any]] | None = Field(default=None, description="Turns the writer has already accepted: [{section, lines:[...]}]")
    reject_reason: str = Field(default="", description="Writer feedback sent to the model when trying a turn again")
    rejections: list[dict[str, Any]] | None = Field(default=None, description="This turn's rejected drafts + reasons: [{lines:[...], reason}]")

# ---------------------------------------------------------------- JSON APIs (for LLMs & Agents)

@app.get("/api/status", summary="System Status")
def get_status() -> dict[str, Any]:
    db = wb.db
    counts = {}
    if db:
        for t in ("unified_rhyme", "corpus_song", "word", "studio_prompts", "unrhymable"):
            try:
                counts[t] = db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            except Exception:
                pass
    return {
        "status": "ready",
        "database_connected": db is not None,
        "table_counts": counts,
        "corpus_parquet_available": (DATA_DIR / "raw").exists(),
    }


@app.get("/api/ai/models", summary="List Available AI Models")
def get_ai_models() -> dict[str, Any]:
    """Available model ids, the configured default, plus a human-readable error."""
    models, error = cowriter.list_models_checked()
    return {
        "models": models,
        "error": error,
        "endpoint": cowriter.endpoint,
        "default_model": app_settings.get_str("default_model"),
    }


class SettingsUpdate(BaseModel):
    llm_endpoint: str | None = Field(default=None, description="LLM API base URL")
    llm_api_key: str | None = Field(default=None, description="LLM API key (write-only; blank keeps existing, '-' clears)")
    default_model: str | None = Field(default=None, description="Default model id")
    default_bpm: int | None = Field(default=None, description="Default BPM")
    default_music_key: str | None = Field(default=None, description="Default musical key")
    default_scale: str | None = Field(default=None, description="Default scale (major/minor)")
    default_vocal: str | None = Field(default=None, description="Default vocal delivery")
    cliche_min_freq: int | None = Field(default=None, description="Cliche corpus-frequency threshold")
    cliche_check_enabled: str | None = Field(default=None, description="Enable cliche check (true/false)")
    melodic_delta_enabled: str | None = Field(default=None, description="Enable melodic-delta check (true/false)")
    idiom_allowlist: str | None = Field(default=None, description="Newline-separated idioms never flagged as cliche")


@app.get("/api/settings", summary="Read Runtime Settings")
def get_settings() -> dict[str, Any]:
    """All settings with resolved value and source (db/env/default). API key masked."""
    return {"settings": app_settings.all_resolved(mask_secrets=True)}


@app.put("/api/settings", summary="Update Runtime Settings")
def put_settings(body: SettingsUpdate) -> dict[str, Any]:
    """Persist settings to the workbench DB. Applies immediately (no restart).

    For the API key: omit or send blank to keep the current value; send '-' to
    clear it back to the env/default. All other fields overwrite when present.
    """
    updates: dict[str, str] = {}
    data = body.model_dump(exclude_unset=True)
    for key, val in data.items():
        if key == "llm_api_key":
            if val is None or val == "":
                continue  # keep existing secret
            updates[key] = "" if val == "-" else str(val)
        elif val is not None:
            updates[key] = str(val)
    if updates:
        app_settings.set_many(updates)
    return {"settings": app_settings.all_resolved(mask_secrets=True)}



class PromptSave(BaseModel):
    key: str = Field(..., description="Prompt key (storyline, interview_questions, song_context, line_writing, whole_song)")
    template: str = Field(..., description="Prompt text with {placeholder} params")


class PromptActivate(BaseModel):
    key: str = Field(..., description="Prompt key")
    version: int = Field(..., description="Version number to activate")


@app.get("/api/prompts", summary="List Editable Prompts")
def get_prompts() -> dict[str, Any]:
    """Every LLM prompt: friendly name, text, params, active version and version list."""
    return {"prompts": prompt_templates.list_prompts()}


@app.put("/api/prompts", summary="Save a Prompt (new active version)")
def save_prompt(body: PromptSave) -> dict[str, Any]:
    """Save the text as a new version and make it active; applies to next generation."""
    try:
        version = prompt_templates.save_version(body.key, body.template)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"version": version, "prompts": prompt_templates.list_prompts()}


@app.post("/api/prompts/activate", summary="Activate an Existing Prompt Version")
def activate_prompt(body: PromptActivate) -> dict[str, Any]:
    try:
        prompt_templates.activate_version(body.key, body.version)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"prompts": prompt_templates.list_prompts()}


@app.post("/api/prompts/{key}/reset", summary="Reset a Prompt to its Original Default")
def reset_prompt(key: str) -> dict[str, Any]:
    try:
        prompt_templates.reset_default(key)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"prompts": prompt_templates.list_prompts()}

@app.get("/api/ai/recommend", summary="Match Generic Style Description to Production Templates")
def recommend_setup(
    q: str = Query("", description="Generic style description: genre, mood, instrumentation, tempo feel")
) -> dict[str, Any]:
    return cowriter.recommend_setup(query=q)


@app.get("/api/ai/vocabulary", summary="Style Terms Present in the Template Table")
def get_vocabulary() -> dict[str, list[str]]:
    """Genre, subgenre and vocal values the 5,960 templates actually carry."""
    return cowriter.vocabulary


@app.get("/api/ai/blueprint", summary="Extract Production Blueprint from a Reference Song")
def get_song_blueprint(
    q: str = Query("", description="Song title, artist, or query like 'Sunflower by Post Malone'"),
    song_id: int | None = Query(None, description="Song ID if known"),
    title: str | None = Query(None, description="Song title"),
    artist: str | None = Query(None, description="Artist name"),
) -> dict[str, Any]:
    query_title = title
    query_artist = artist
    if not query_title and q:
        if " by " in q.lower():
            parts = re.split(r"\s+by\s+", q, flags=re.I)
            query_title = parts[0].strip()
            query_artist = parts[1].strip()
        else:
            query_title = q.strip()
    bp = cowriter.blueprint_from_song(song_id=song_id, title=query_title, artist=query_artist)
    if not bp:
        raise HTTPException(status_code=404, detail="Song not found in corpus")
    return bp

@app.post("/api/ai/storyline", summary="Generate 3-Act Narrative Storyline")
def generate_storyline(req: StorylineRequest) -> dict[str, Any]:
    story = cowriter.author_story(
        brief=req.brief,
        title=req.title or "",
        style=req.style or "",
        genre=req.genre or "",
        bpm=req.bpm,
        key=req.key,
        scale=req.scale,
        template_title=req.template_title or "",
        template_id=req.template_id,
        model=req.model,
    )
    return {
        "title": req.title or "Untitled Song",
        "storyline": story,
    }

@app.post("/api/ai/recommend_references", summary="Recommend Reference Songs Based on Story/Style")
def recommend_references(req: RecommendReferencesRequest) -> list[dict[str, Any]]:
    return cowriter.recommend_reference_songs(
        brief=req.brief,
        story=req.story,
        style=req.style or "",
        genre=req.genre or "",
        bpm=req.bpm,
        key=req.key,
        scale=req.scale,
        template_id=req.template_id,
        limit=req.limit,
        has_midi=req.has_midi,
    )


@app.post("/api/ai/interview", summary="Production Questions with Defaults for a Brief")
def interview(req: InterviewRequest) -> dict[str, Any]:
    return cowriter.interview(
        brief=req.brief,
        style=req.style or "",
        template_id=req.template_id,
        reference_song=req.reference_song,
        story=req.story or "",
        model=req.model,
        use_model=req.use_model,
    )

@app.post("/api/ai/preview_instructions", summary="Preview Verbatim Prompt Sent to Model")
def preview_instructions(req: PreviewInstructionsRequest) -> dict[str, Any]:
    return cowriter.build_instructions_payload(
        prompt=req.prompt or "An original song",
        style=req.style,
        genre=req.genre,
        template_id=req.template_id,
        bpm=req.bpm,
        music_key=req.music_key,
        scale=req.scale,
        vocal=req.vocal,
        vocal_roles=req.vocal_roles,
        reference_song=req.reference_song,
        banned_words=req.banned_words,
        rhyme_scheme=req.rhyme_scheme,
        poetic_form=req.poetic_form,
        section_plan=req.section_plan,
        syllable_target=req.syllable_target,
        answers=req.answers,
        engine_target=req.engine_target,
        mode=req.mode,
        model=req.model,
    )
@app.post("/api/ai/draft_interactive", summary="Interactive Turn-by-Turn Line Generation Loop")
def draft_interactive(req: DraftInteractiveRequest) -> dict[str, Any]:
    return cowriter.draft_song_interactive(
        prompt=req.prompt,
        style=req.style,
        genre=req.genre,
        template_id=req.template_id,
        bpm=req.bpm,
        music_key=req.music_key,
        scale=req.scale,
        vocal=req.vocal,
        vocal_roles=req.vocal_roles,
        reference_song=req.reference_song,
        banned_words=req.banned_words,
        rhyme_scheme=req.rhyme_scheme,
        section_plan=req.section_plan,
        syllable_target=req.syllable_target,
        answers=req.answers,
        questions=req.questions,
        song_id=req.song_id,
        story=req.story,
        model=req.model,
        max_retries=req.max_retries,
        max_sections=req.max_sections,
    )
@app.post("/api/ai/draft_turn", summary="Draft one turn for human review (accept / edit / try again)")
def draft_turn(req: DraftTurnRequest) -> dict[str, Any]:
    data = req.model_dump()
    accepted = data.pop("accepted", None)
    reject_reason = data.pop("reject_reason", "") or ""
    rejections = data.pop("rejections", None)
    return cowriter.draft_turn(accepted=accepted, reject_reason=reject_reason, rejections=rejections, **data)
@app.post("/api/ai/finalize_turns", summary="Assemble human-accepted turns into a saved song")
def finalize_turns(req: DraftTurnRequest) -> dict[str, Any]:
    data = req.model_dump()
    accepted = data.pop("accepted", None)
    data.pop("reject_reason", None)
    data.pop("rejections", None)
    return cowriter.finalize_turns(accepted=accepted, **data)
@app.post("/api/ai/draft_stream", summary="Stream LLM Prompts, Responses, and Scansion Turns in Real Time")
def draft_stream(req: DraftInteractiveRequest):
    def event_generator():
        try:
            generator = cowriter.stream_song_interactive(
                prompt=req.prompt,
                title=req.title,
                style=req.style,
                genre=req.genre,
                template_id=req.template_id,
                bpm=req.bpm,
                music_key=req.music_key,
                scale=req.scale,
                vocal=req.vocal,
                vocal_roles=req.vocal_roles,
                reference_song=req.reference_song,
                banned_words=req.banned_words,
                rhyme_scheme=req.rhyme_scheme,
                section_plan=req.section_plan,
                syllable_target=req.syllable_target,
                answers=req.answers,
                questions=req.questions,
                song_id=req.song_id,
                story=req.story,
                model=req.model,
                max_retries=req.max_retries,
                max_sections=req.max_sections,
            )
            for ev in generator:
                yield f"data: {json.dumps(ev)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")




@app.get("/api/templates/search", summary="Search Production Templates")
def search_templates(
    q: str = Query("", description="Semantic or keyword query"),
    genre: str | None = Query(None, description="Exact template genre (from /api/templates/genres)"),
    limit: int = Query(500, ge=1, le=5000)
) -> list[dict[str, Any]]:
    return cowriter.studio_db.search_templates(query=q, genre=genre, limit=limit)


@app.get("/api/templates/genres", summary="Every Template Genre with Its Template Count")
def template_genres() -> dict[str, Any]:
    genres = cowriter.studio_db.list_genres(limit=1000)
    return {
        "genres": genres,
        "genre_count": len(genres),
        "template_count": sum(g["count"] for g in genres),
    }


@app.get("/api/templates/{template_id}", summary="Get Full MusicStudio Template Details")
def get_template(template_id: int) -> dict[str, Any]:
    tmpl = cowriter.studio_db.get_template_detail(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")
    return tmpl


@app.post("/api/music/format", summary="Format Any Raw Lyrics for YuE2 & MiniMax Music 03")
def format_music(req: FormatMusicRequest) -> dict[str, Any]:
    template = None
    if req.template_id and cowriter.studio_db.is_available:
        template = cowriter.studio_db.get_template_detail(req.template_id)
    elif cowriter.studio_db.is_available:
        template = cowriter.studio_db.get_best_match(genre=req.genre or "Pop")

    bpm = 90
    genre_str = req.genre or "Pop"
    yue2_style = f"English, {bpm} BPM, {genre_str}, melodic vocals, acoustic accompaniment"
    minimax_caption = f"[Global Metadata]\nbpm is {bpm}. key is C, and scale is major. {genre_str}."

    if template:
        meta = template.get("metadata", {})
        bpm = meta.get("bpm") or bpm
        genre_str = f"{meta.get('genre', '')} / {meta.get('subgenre', '')}".strip(" /") or genre_str
        yue2_style = template.get("yue2_style") or yue2_style
        minimax_caption = template.get("minimax_caption") or minimax_caption

    clean_lyrics = cowriter._clean_lyrics(req.text)
    if not clean_lyrics.startswith("["):
        clean_lyrics = "[Verse 1]\n" + clean_lyrics

    return {
        "title": req.title,
        "clean_lyrics": clean_lyrics,
        "bpm": bpm,
        "genre": genre_str,
        "yue2_package": {
            "style": yue2_style,
            "lyrics": clean_lyrics,
            "cot": "full"
        },
        "minimax_package": {
            "caption": minimax_caption,
            "lyrics": clean_lyrics
        },
        "formatted_yue2": f"Style: {yue2_style}\n\n{clean_lyrics}",
        "formatted_minimax": f"{minimax_caption}\n\n{clean_lyrics}"
    }


@app.post("/api/ai/generate", summary="Generate Production-Ready Lyrics & Audio Packages")
def generate_lyrics(req: GenerateRequest) -> dict[str, Any]:
    try:
        result = cowriter.generate(
            title=req.title or "Untitled Song",
            prompt=req.prompt,
            style=req.style,
            genre=req.genre,
            template_id=req.template_id,
            bpm=req.bpm,
            music_key=req.music_key,
            scale=req.scale,
            vocal=req.vocal,
            vocal_roles=req.vocal_roles,
            reference_song=req.reference_song,
            banned_words=req.banned_words,
            rhyme_scheme=req.rhyme_scheme,
            poetic_form=req.poetic_form,
            section_plan=req.section_plan,
            syllable_target=req.syllable_target,
            answers=req.answers,
            engine_target=req.engine_target,
            chat_history=req.chat_history,
            mode=req.mode,
            model=req.model,
            temperature=req.temperature,
            section_by_section=req.section_by_section,
            author_engine_prompt=req.author_engine_prompt,
        )

        # Validate unique sung lines: a chorus repeating three times is how songs
        # work, not a repetition defect, and counting it as one inflates every ratio.
        seen: set[str] = set()
        clean_lines: list[str] = []
        for raw in result["lyrics"].splitlines():
            line = raw.strip()
            if not line or line.startswith(("[", "<|", "(")):
                continue
            low = line.lower()
            if low in seen:
                continue
            seen.add(low)
            clean_lines.append(line)
        check = None
        if clean_lines:
            try:
                # A whole song, not the first 16 lines: cross-section stock pairs
                # are exactly what a one-section window cannot see.
                check_res = wb.check_draft(clean_lines, kind="verse", target_syllables=req.syllable_target or "8-10")
                check = {
                    "line_count": check_res.line_count,
                    "rhyme_scheme": check_res.rhyme_scheme,
                    "rhyme_density": check_res.rhyme_density,
                    "metric_consistency": check_res.metric_consistency,
                    "contour": check_res.contour,
                    "issues": check_res.issues,
                    "mean_concreteness": check_res.mean_concreteness,
                    "dominant_modality": check_res.dominant_modality,
                    "valence": check_res.valence,
                    "arousal": check_res.arousal,
                    "dominant_emotion": check_res.dominant_emotion,
                    "sensory_anchors": check_res.sensory_anchors,
                }
            except Exception:
                # The craft report is advisory: a failure here must not lose the
                # generated lyrics, which are the actual result.
                traceback.print_exc()
                check = None

        return {
            **result,
            "craft_check": check,
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


#: Every rhyme relation the dictionary supports, for the API and the UI.
RHYME_KINDS = [t.value for t in RhymeType]


@app.get("/api/rhymes", summary="Find Rhymes (14 Types, Slang-Aware)")
def get_rhymes(
    word: str = Query(..., description="Word or phrase to find rhymes for"),
    kind: str = Query("perfect", description=f"One of: {', '.join(RHYME_KINDS)}"),
    syllables: int | None = Query(None, description="Exact syllable count filter"),
    min_freq: int = Query(0, ge=0, description="Minimum corpus frequency"),
    prefer: str = Query("singable",
                        description="singable (line-end frequency), fresh (rare), close (phonetic depth)"),
    phrases: bool = Query(True, description="Allow multi-word rhymes (mosaic)"),
    exclude_tags: str = Query(
        "",
        description=("Comma-separated register labels to drop: slang, informal, "
                     "vulgar, offensive, archaic, dialect, poetic, proper"),
    ),
    verified_only: bool = Query(False, description="Only pronunciations corroborated by 2+ sources"),
    limit: int = Query(25, ge=1, le=200, description="Max results"),
) -> dict[str, Any]:
    """Rhymes from the rebuilt dictionary.

    Returns the target word's own analysis alongside the matches, so a caller
    can see what it rhymed *on* rather than trusting the list blindly.
    """
    if rhyme_db is None:
        raise HTTPException(
            status_code=503,
            detail="rhymes.duckdb not built. Run: uv run python scripts/build_rhymes.py")

    if kind not in RHYME_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"unknown kind {kind!r}; expected one of {RHYME_KINDS}")

    target = rhyme_db.info(word)
    matches = rhyme_db.rhymes(
        word,
        kinds=(kind,),
        limit=limit,
        min_freq=min_freq,
        min_syllables=syllables,
        max_syllables=syllables,
        allow_phrases=phrases,
        exclude_tags=tuple(t.strip() for t in exclude_tags.split(",") if t.strip()),
        require_verified=verified_only,
        prefer=prefer,
    )
    return {
        "word": word,
        "kind": kind,
        "known": target is not None,
        "unrhymable": rhyme_db.is_unrhymable(word),
        "target": target,
        "count": len(matches),
        "matches": [
            {
                "word": m.word,
                "kind": m.kind,
                "arpabet": m.arpabet,
                "syllables": m.syllables,
                "depth": m.depth,
                "corpus_freq": m.corpus_freq,
                "end_freq": m.end_freq,
                "verdict": m.verdict,
                # Definitions and part of speech: a rhyme cannot be used to
                # write a line without knowing what the word means.
                "glosses": list(m.glosses),
                "definition": m.definition,
                "pos": list(m.pos),
                "tags": list(m.tags),
                "is_phrase": m.is_phrase,
                "verified": m.is_verified,
            }
            for m in matches
        ],
    }


@app.get("/api/rhymes/kinds", summary="List Supported Rhyme Types")
def list_rhyme_kinds() -> list[dict[str, str]]:
    """The supported relations with an example of each, for UI population."""
    examples = {
        "perfect": "cat / hat", "perfect_masculine": "cat / hat",
        "perfect_feminine": "motion / ocean",
        "perfect_dactylic": "mysterious / delirious",
        "rich": "knight / night", "assonance": "late / shape",
        "consonance": "bridge / grudge", "family": "cat / cap",
        "pararhyme": "flesh / flash", "reverse": "late / lake",
        "alliteration": "bold / bitter", "eye": "cough / rough",
        "mosaic": "dismay / this may", "additive": "coal / cold",
        "subtractive": "cold / coal", "editorial": "human-asserted near rhyme",
    }
    return [{"kind": k, "example": examples.get(k, "")} for k in RHYME_KINDS]


@app.get("/api/rhymes/sounds", summary="Target Rhyme Sounds for a Word")
def get_rhyme_sounds(
    word: str = Query(..., description="Seed word (e.g. a line's end word) to group rhymes by vowel sound"),
    mood: str | None = Query(None, description="Optional mood to bias examples toward (tone-aligned)"),
    per_sound: int = Query(6, ge=1, le=12, description="Example words per sound"),
) -> dict[str, Any]:
    """Rhyme sounds for a word, each labelled (Long O, Short O, ...) with example words.

    Deterministic sound grouping; examples are ranked common/singable and biased
    by mood where given. The LLM still makes the final semantic choice.
    """
    return {"word": word, "sounds": wb.rhyme_sound_options(word, mood=mood, per_sound=per_sound)}


@app.get("/api/rhymes/word", summary="Full Dictionary Entry for One Word")
def get_word_entry(
    word: str = Query(..., description="Word to look up"),
) -> dict[str, Any]:
    """Pronunciations, provenance, register flags and frequency for one word."""
    if rhyme_db is None:
        raise HTTPException(status_code=503, detail="rhymes.duckdb not built")
    info = rhyme_db.info(word)
    if info is None:
        raise HTTPException(status_code=404, detail=f"{word!r} not in dictionary")
    info["unrhymable"] = rhyme_db.is_unrhymable(word)
    return info


@app.get("/api/rhymes/health", summary="Rhyme Database Build Verification")
def rhyme_health() -> dict[str, Any]:
    """Build statistics and the database's own verification results."""
    if rhyme_db is None:
        raise HTTPException(status_code=503, detail="rhymes.duckdb not built")
    checks = rhyme_db.checks()
    return {
        "stats": rhyme_db.stats(),
        "checks_passed": sum(1 for c in checks if c[1]),
        "checks_total": len(checks),
        "checks": [
            {"name": c[0], "passed": bool(c[1]), "observed": c[2],
             "expected": c[3], "detail": c[4]}
            for c in checks
        ],
    }


@app.get("/api/songs/search", summary="Search 3.18M Songs Dataset")
def search_songs(
    q: str = Query("", description="Query string"),
    by: str = Query("all", enum=["all", "title", "artist", "lyrics"], description="Search field"),
    genre: str | None = Query(None, description="Genre/tag filter"),
    year_min: int | None = Query(None, description="Minimum release year"),
    year_max: int | None = Query(None, description="Maximum release year"),
    limit: int = Query(200, ge=1, le=500, description="Max results"),
    has_midi: bool = Query(False, description="Only songs that have an imported MIDI (ABC score available)")
) -> list[dict[str, Any]]:
    results = wb.search_songs(
        query=q,
        search_by=by,
        genre=genre,
        year_min=year_min,
        year_max=year_max,
        limit=limit,
        has_midi=has_midi
    )
    return [
        {
            "title": s.title,
            "artist": s.artist,
            "genre": s.genre,
            "year": s.year,
            "line_count": s.line_count,
            "word_count": s.word_count,
            "section_count": s.section_count,
            "repetition_ratio": s.repetition_ratio,
            "preview": s.lyrics_preview,
            "song_id": s.song_id,
            "bpm": s.tempo_bpm,
            "key": s.musical_key,
            "scale": s.scale,
            "has_midi": s.has_midi,
        }
        for s in results
    ]


@app.get("/api/songs/{id_or_title}", summary="Get Full Song Lyrics & Scansion")
def get_song(
    id_or_title: str,
    artist: str | None = Query(None, description="Artist name if querying by title")
) -> dict[str, Any]:
    song_id = int(id_or_title) if id_or_title.isdigit() else None
    title = id_or_title if not song_id else None
    song = wb.get_song(title=title, artist=artist, song_id=song_id)
    if not song:
        raise HTTPException(status_code=404, detail="Song not found in corpus")

    return {
        "title": song.title,
        "artist": song.artist,
        "genre": song.genre,
        "year": song.year,
        "total_lines": song.total_lines,
        "sections": [
            {
                "name": sec.name,
                "kind": sec.kind,
                "line_count": sec.line_count,
                "contour": sec.contour,
                "lines": [
                    {
                        "line_number": l.line_number,
                        "text": l.text,
                        "syllables": l.syllables,
                        "stress_grid": l.stress_grid,
                        "end_word": l.end_word,
                    }
                    for l in sec.lines
                ],
            }
            for sec in song.sections
        ],
        "raw_lyrics": song.raw_lyrics,
    }


@app.post("/api/analyze_line", summary="Analyze Lyric Line Syllables & Stress")
def analyze_line(req: AnalyzeLineRequest) -> dict[str, Any]:
    rep = wb.analyze_line(req.text, beats=req.beats)
    return {
        "text": rep.text,
        "syllables": rep.syllables,
        "stress_grid": rep.stress_grid,
        "end_word": rep.end_word,
        "unknown_words": rep.unknown_words,
        "prosody_flags": rep.prosody_flags,
        "internal_rhymes": rep.internal_rhymes,
    }

def compute_harmony(key: str = "C", scale: str = "major") -> dict[str, Any]:
    key_clean = (key or "C").strip().capitalize()
    scale_clean = (scale or "major").strip().lower()
    roots = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    enharmonics = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}
    root_norm = enharmonics.get(key_clean, key_clean)
    if root_norm not in roots:
        root_norm = "C"
    root_idx = roots.index(root_norm)

    if "minor" in scale_clean:
        deg_offsets = [0, 2, 3, 5, 7, 8, 10]
        chord_qualities = ["m", "dim", "", "m", "m", "", ""]
        roman_degrees = ["i", "ii°", "III", "iv", "v", "VI", "VII"]
        prog_indices = [0, 5, 2, 6]
        prog_name = "Minor Pop Axis (i - VI - III - VII)"
    else:
        deg_offsets = [0, 2, 4, 5, 7, 9, 11]
        chord_qualities = ["", "m", "m", "", "", "m", "dim"]
        roman_degrees = ["I", "ii", "iii", "IV", "V", "vi", "vii°"]
        prog_indices = [0, 4, 5, 3]
        prog_name = "Major Pop Axis (I - V - vi - IV)"

    diatonic_chords = [
        roots[(root_idx + deg_offsets[i]) % 12] + chord_qualities[i]
        for i in range(7)
    ]
    progression_chords = [diatonic_chords[i] for i in prog_indices]
    progression_roman = [roman_degrees[i] for i in prog_indices]

    return {
        "key": key_clean,
        "scale": scale_clean,
        "progression_name": prog_name,
        "chords": progression_chords,
        "roman_numerals": progression_roman,
        "diatonic_scale_chords": diatonic_chords,
    }

class SongAnalysisRequest(BaseModel):
    lyrics: str = Field(..., description="Full raw lyrics text including optional [Section] tags")
    key: str | None = Field(default="C", description="Musical key")
    scale: str | None = Field(default="major", description="Scale: major or minor")
    chords: list[str] | None = Field(default=None, description="Optional chord sequence")

@app.post("/api/analyze_song", summary="Comprehensive 4-Pillar Songwriting Analytics")
def analyze_song(req: SongAnalysisRequest) -> dict[str, Any]:
    lyrics = req.lyrics.strip()
    if not lyrics:
        return {
            "word_count": 0, "unique_words": 0, "ttr": 0.0, "ttr_diagnostic": "Empty",
            "line_count": 0, "avg_syllables": 0.0, "variance": 0.0, "cadence_consistency": "-",
            "lines": [], "similarity_matrix": [], "section_deltas": [], "issues": [],
            "sensory": {}, "mood": {}, "harmony": compute_harmony(req.key or "C", req.scale or "major")
        }
    raw_lines = lyrics.split("\n")
    parsed_lines: list[tuple[str, str]] = []
    current_sec = "Verse"
    for r in raw_lines:
        s = r.strip()
        if not s:
            continue
        if s.startswith("[") and s.endswith("]"):
            current_sec = s[1:-1].strip()
        else:
            parsed_lines.append((current_sec, s))

    tokens = re.findall(r"\b[a-zA-Z']+\b", lyrics.lower())
    unique_tokens = set(tokens)
    ttr = (len(unique_tokens) / len(tokens)) * 100 if tokens else 0.0

    line_details = []
    syllable_counts = []
    for sec, line in parsed_lines:
        st = wb.ca.line_stat(line)
        syllable_counts.append(st.syllables)
        line_details.append({
            "section": sec,
            "text": line,
            "syllables": st.syllables,
            "stress_grid": st.stress_grid,
            "end_word": st.end_word or ""
        })

    avg_syl = sum(syllable_counts) / len(syllable_counts) if syllable_counts else 0.0
    variance = sum((s - avg_syl) ** 2 for s in syllable_counts) / len(syllable_counts) if syllable_counts else 0.0
    std_dev = round(variance ** 0.5, 2)

    # Self-similarity matrix (0.0 to 1.0 Jaccard overlap between lines)
    n = len(parsed_lines)
    word_sets = [set(_WORD_RE.findall(l[1].lower())) for l in parsed_lines]
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        words_i = word_sets[i]
        matrix[i][i] = 1.0 if words_i else 0.0
        for j in range(i + 1, n):
            words_j = word_sets[j]
            sim = (len(words_i & words_j) / len(words_i | words_j)) if (words_i and words_j) else 0.0
            r_sim = round(sim, 2)
            matrix[i][j] = r_sim
            matrix[j][i] = r_sim
    just_lines = [l[1] for l in parsed_lines]
    draft_rep = wb.check_draft(just_lines) if just_lines else None

    # Parse draft issues so line-specific clichés attach directly to that line index
    line_issues_map: dict[int, list[str]] = {}
    global_issues: list[str] = []
    if draft_rep and draft_rep.issues:
        for iss in draft_rep.issues:
            m = re.match(r"^line\s+(\d+)\s*:\s*(.*)", iss, re.I)
            if m:
                l_idx = int(m.group(1)) - 1
                line_issues_map.setdefault(l_idx, []).append(m.group(2).strip())
            else:
                global_issues.append(iss)

    # Section syllable delta & symmetry (e.g. Verse 1 line i vs Verse 2 line i)
    sections_map: dict[str, list[tuple[int, str]]] = {}
    for idx, (sec, line) in enumerate(parsed_lines):
        sections_map.setdefault(sec.lower(), []).append((idx, line))

    line_symmetry_map: dict[int, dict[str, Any]] = {}
    sec_deltas: list[dict[str, Any]] = []
    v1 = sections_map.get("verse 1", []) or sections_map.get("verse", [])
    v2 = sections_map.get("verse 2", [])
    if v1 and v2:
        for line_num_1idx in range(min(len(v1), len(v2))):
            orig_idx_1, text_1 = v1[line_num_1idx]
            orig_idx_2, text_2 = v2[line_num_1idx]
            s1 = wb.ca.line_stat(text_1).syllables
            s2 = wb.ca.line_stat(text_2).syllables
            delta = abs(s1 - s2)
            flag = delta > 1
            sec_deltas.append({
                "line_num": line_num_1idx + 1,
                "v1_syl": s1,
                "v2_syl": s2,
                "delta": delta,
                "flag": flag,
            })
            msg_1 = f"Symmetric with V2 Line {line_num_1idx+1} ({s2} syl, Δ{delta})" if not flag else f"⚠ Awkward Melodic Delta with V2 Line {line_num_1idx+1} ({s2} syl, Δ{delta} > 1 risks vocal stumble)"
            msg_2 = f"Symmetric with V1 Line {line_num_1idx+1} ({s1} syl, Δ{delta})" if not flag else f"⚠ Awkward Melodic Delta with V1 Line {line_num_1idx+1} ({s1} syl, Δ{delta} > 1 risks vocal stumble)"
            line_symmetry_map[orig_idx_1] = {"delta": delta, "flag": flag, "msg": msg_1}
            line_symmetry_map[orig_idx_2] = {"delta": delta, "flag": flag, "msg": msg_2}
    # Enrich each line in line_details with its inline issues and symmetry
    for idx, item in enumerate(line_details):
        item["cliches"] = line_issues_map.get(idx, [])
        item["symmetry"] = line_symmetry_map.get(idx, None)

    ttr_diag = "Dense / Lyric-Heavy / Folk / Rap" if ttr > 55 else ("Commercial Pop / High-Recall Hook" if ttr < 42 else "Balanced Contemporary")
    cadence_diag = "Very regular pulse (<3.0)" if variance < 3 else ("Flexible / Conversational pulse" if variance < 6 else "Erratic / Dynamic pulse (>6.0)")
    return {
        "word_count": len(tokens),
        "unique_words": len(unique_tokens),
        "ttr": round(ttr, 1),
        "ttr_diagnostic": ttr_diag,
        "line_count": len(parsed_lines),
        "avg_syllables": round(avg_syl, 1),
        "variance": round(variance, 2),
        "std_dev": std_dev,
        "cadence_consistency": cadence_diag,
        "rhyme_scheme": getattr(draft_rep, "rhyme_scheme", None),
        "rhyme_density": getattr(draft_rep, "rhyme_density", None),
        "metric_consistency": getattr(draft_rep, "metric_consistency", None),
        "abstract_ratio": getattr(draft_rep, "abstract_ratio", None),
        "sensory_ratio": getattr(draft_rep, "sensory_ratio", None),
        "dominant_modality": getattr(draft_rep, "dominant_modality", None),
        "lines": line_details,
        "similarity_matrix": matrix,
        "section_deltas": sec_deltas,
        "issues": getattr(draft_rep, "issues", []),
        "sensory": wb.analyze_sensory(lyrics) if lyrics else {},
        "mood": wb.analyze_mood(lyrics) if lyrics else {},
        "harmony": compute_harmony(req.key or "C", req.scale or "major")
    }

@app.post("/api/check_draft", summary="Check Full Draft for Meter & Clichés")
def check_draft(req: CheckDraftRequest) -> dict[str, Any]:
    rep = wb.check_draft(req.lines, kind=req.kind, target_syllables=req.target_syllables)
    return {
        "line_count": rep.line_count,
        "total_syllables": rep.total_syllables,
        "rhyme_scheme": rep.rhyme_scheme,
        "rhyme_density": rep.rhyme_density,
        "metric_consistency": rep.metric_consistency,
        "mean_syllables": rep.mean_syllables,
        "contour": rep.contour,
        "abstract_ratio": rep.abstract_ratio,
        "sensory_ratio": rep.sensory_ratio,
        "repeat_ratio": rep.repeat_ratio,
        "readability_grade": rep.readability_grade,
        "issues": rep.issues,
    }


@app.post("/api/validate_form", summary="Validate Poetic Form")
def validate_form(req: ValidateFormRequest) -> dict[str, Any]:
    res = wb.validate_form(req.form, req.lines, word=req.word)
    return {
        "form": res["form"],
        "ok": res["ok"],
        "line_count": res["line_count"],
        "violations": res["violations"],
        "notes": res["notes"],
    }


@app.get("/api/slang", summary="Rhyming Slang Substitutions")
def get_slang(target: str = Query(..., description="Target word (e.g. stairs)")) -> list[dict[str, Any]]:
    return wb.phrase_rhymes(target)


@app.get("/api/unrhymable", summary="Check if Word Has No Rhyme")
def check_unrhymable(word: str = Query(...)) -> dict[str, Any]:
    return {"word": word, "is_unrhymable": bool(wb.is_unrhymable(word))}


@app.get("/api/craft/starter-prompts", summary="Songwriting Craft Prompts")
def get_starter_prompts(limit: int = 10) -> list[dict[str, Any]]:
    return wb.starter_prompts(limit=limit)



# -------------------------------------------------------------
# Crafted Songs API (for MusicStudio integration)
# -------------------------------------------------------------

class CraftedSongSaveRequest(BaseModel):
    id: str | None = Field(default=None, description="Song id when updating an existing song")
    status: str = Field(default="draft", description="Workflow status: draft, finalized, rendered")
    generation: dict[str, Any] = Field(..., description="Complete SongGeneration document")


@app.get("/api/crafted_songs", summary="List All Crafted Songs (for MusicStudio)")
def list_crafted_songs(
    status: str | None = Query(None, description="Filter by status (draft, finalized, rendered)"),
    genre: str | None = Query(None, description="Filter by genre"),
    q: str = Query("", description="Search in title, artist, or brief"),
    sort_by: str = Query("updated_at", description="Sort by field: updated_at, created_at, title, bpm, genre, status"),
    sort_order: str = Query("desc", description="Sort order: asc or desc"),
    limit: int = Query(100, ge=1, le=500, description="Max songs to return"),
    offset: int = Query(0, ge=0, description="Offset"),
) -> list[dict[str, Any]]:
    return cowriter.list_crafted_songs(
        status=status, genre=genre, query=q, sort_by=sort_by, sort_order=sort_order, limit=limit, offset=offset
    )

@app.get("/api/crafted_songs/{song_id}", summary="Get Full Crafted Song Details & ABC Score")
def get_crafted_song(song_id: str) -> dict[str, Any]:
    song = cowriter.get_crafted_song(song_id)
    if not song:
        raise HTTPException(status_code=404, detail=f"Crafted song '{song_id}' not found")
    return song


@app.post("/api/crafted_songs", summary="Save or Update a Crafted Song")
def save_crafted_song(req: CraftedSongSaveRequest) -> dict[str, Any]:
    gen = SongGeneration.from_record(req.generation, song_id=req.id or "", status=req.status)
    if not gen.model:
        raise HTTPException(status_code=422, detail="generation.model is required; select a model in Step 1.")
    prior = cowriter.load_generation(req.id) if req.id else None
    cowriter.save_generation(gen)
    # Identical content is not re-stored: tell the UI so it can say "no changes".
    unchanged = prior is not None and gen.revision == prior.revision
    return {"status": "unchanged" if unchanged else "saved", "song_id": gen.song_id, "revision": gen.revision}


@app.get("/api/crafted_songs/{song_id}/generation", summary="Load a Song's Full Generation for Steps 1-4")
def load_crafted_generation(song_id: str) -> dict[str, Any]:
    gen = cowriter.load_generation(song_id)
    if gen is None:
        raise HTTPException(status_code=404, detail=f"Crafted song '{song_id}' not found")
    return {"song_id": gen.song_id, "revision": gen.revision, "status": gen.status, "generation": gen.to_record()}


@app.get("/api/crafted_songs/{song_id}/revisions", summary="List a Song's Saved Revisions")
def list_crafted_revisions(song_id: str) -> dict[str, Any]:
    revs = cowriter.list_revisions(song_id)
    if not revs:
        raise HTTPException(status_code=404, detail=f"Crafted song '{song_id}' not found")
    return {"song_id": song_id, "revisions": revs}


@app.get("/api/crafted_songs/{song_id}/revisions/{revision}", summary="Load One Saved Revision")
def load_crafted_revision(song_id: str, revision: int) -> dict[str, Any]:
    gen = cowriter.load_revision(song_id, revision)
    if gen is None:
        raise HTTPException(status_code=404, detail=f"Revision {revision} of '{song_id}' not found")
    return {"song_id": song_id, "revision": revision, "status": gen.status, "generation": gen.to_record()}

class BatchDeleteSongsRequest(BaseModel):
    song_ids: list[str] = Field(..., description="List of song IDs to delete")

@app.post("/api/crafted_songs/batch_delete", summary="Batch Delete Crafted Songs")
def batch_delete_crafted_songs(req: BatchDeleteSongsRequest) -> dict[str, Any]:
    count = cowriter.delete_crafted_songs(req.song_ids)
    return {"status": "deleted", "deleted_count": count, "song_ids": req.song_ids}

@app.delete("/api/crafted_songs/{song_id}", summary="Delete a Crafted Song")
def delete_crafted_song(song_id: str) -> dict[str, Any]:
    ok = cowriter.delete_crafted_song(song_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Song not found or could not be deleted")
    return {"status": "deleted", "song_id": song_id}

@app.get("/api/stats", summary="Get Database and Corpus Statistics")
def get_stats() -> dict[str, Any]:
    db = corpus_db()
    try:
        cnt = db.read_one("SELECT count(*) FROM songs")[0]
    except Exception:
        cnt = 0
    try:
        t_cnt = db.read_one("SELECT count(*) FROM studio_templates")[0]
    except Exception:
        t_cnt = 0
    return {"reference_songs_count": cnt, "templates_count": t_cnt}


@app.get("/healthz", summary="Liveness and data readiness (no auth)")
def healthz() -> JSONResponse:
    """200 when both runtime databases are present, 503 otherwise.

    Exempt from auth so load balancers and the install script can probe it;
    it reveals only whether the data files exist.
    """
    ready = {"corpus_db": CORPUS_DB.exists(), "songs_parquet": SONGS_PARQUET.exists(), "workbench_db": WORKBENCH_DB.exists()}
    ok = all(ready.values())
    return JSONResponse({"status": "ok" if ok else "missing_data", **ready}, status_code=200 if ok else 503)
# ---------------------------------------------------------------- Web UI
UI_PATH = Path(__file__).resolve().parent / "ui.html"


class RevalidatingStaticFiles(StaticFiles):
    """Static files that the browser must revalidate on every load.

    Without a Cache-Control header browsers cache JS/CSS heuristically from
    Last-Modified, so after an update they keep running the old scripts.
    ``no-cache`` still allows caching, but checks the ETag first (a 304 when
    unchanged), so a deploy takes effect on the next page load.
    """

    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStaticFiles(directory=Path(__file__).resolve().parent / "static"), name="static")


@app.get("/", response_class=HTMLResponse, summary="Songwriter Studio Web UI")
def index() -> HTMLResponse:
    if UI_PATH.exists():
        return HTMLResponse(content=UI_PATH.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>UI file missing</h1>", status_code=500)

@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> HTMLResponse:
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="%236366f1"><path d="M12 3v10.55c-.59-.34-1.27-.55-2-.55-2.21 0-4 1.79-4 4s1.79 4 4 4 4-1.79 4-4V7h4V3h-6z"/></svg>'
    return HTMLResponse(content=svg, media_type="image/svg+xml")
