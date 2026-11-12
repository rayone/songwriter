"""Editable LLM prompt templates with version history, in workbench.duckdb.

Every prompt the app sends to the model is one editable text template here, with
``{placeholder}`` params the app fills at generation time. The Settings page
shows a dropdown of the prompts (friendly names), a textbox of the selected
one, a versions dropdown, Save (creates a new active version) and Reset to
Default (restores the original).

Guarantees:
  * The original default of every prompt is kept as version 0 and never
    overwritten, so Reset always restores it.
  * Rendering is SAFE: unknown/missing ``{params}`` stay literal as ``{name}``
    rather than raising, so a bad edit degrades visibly instead of breaking
    generation.
  * No references to real people anywhere -- craft guidance is plain prose.
"""

from __future__ import annotations

import threading
from typing import Any

from songwriter.db import Database, workbench_db

_lock = threading.RLock()
_seeded = False

# Craft guidance folded into prompts as plain prose (no attributions). Editing a
# prompt in the UI lets an operator change any of this per prompt.
_LINE_RULES = (
    "WRITING RULES:\n"
    "- Sense first: every line says something a real person would sing; you could restate it in plain words.\n"
    "- Style: Conversational SVO spoken English; concrete sensory details; no melodramatic filler.\n"
    "- Rhymes: True content-word rhymes only (ban clichés: night/light, fire/desire, love). Never invert syntax.\n"
    "- Land the rhyme inside a phrase people actually say ('fall apart', 'from the start'); never bolt a\n"
    "  rhyme word onto a line it does not belong in, and never splice two idioms together. A clean slant\n"
    "  rhyme beats a perfect rhyme that makes no sense.\n"
    "- Length: a word or two short is fine; never add filler words just to reach a count.\n"
    "- Continuity: Each line continues the narrative scene of the line before it."
)

# Step 4 critic (Auto LLM mode). Doubled braces are literal JSON braces; the app
# fills the single-brace placeholders.
_LYRIC_CRITIC = """You are a hit songwriter and lyric editor reviewing a co-writer's draft before the vocal session. Judge it the way a listener hears it: every line must say something a real person would sing, and the lines must carry the story forward together.

EVALUATION CRITERIA:
(Judge the words, not the sound. Rhyme, syllables and cadence are checked by the engine before you see the line.)
1. SENSE (decides the verdict):
- First restate each line in plain everyday English. If you cannot say what a line means without inventing a backstory for it, it fails.
- Typical failures: spliced idioms ("Tear me up apart" glues "tear me up" onto "tear me apart"); a word that is only there to fill the line ("Bleed for art", "Softest strike you will prefer", "Yielding to this baby sir"); images that clash with the scene or with each other.
- Fresh imagery passes when its meaning is clear ("My breath hangs silver as the hinges fold"). The simplest natural phrasing usually wins: "Take my heart / Break me apart".

2. STORY & FLOW:
- Each line follows from the line before it and from the section's scene; the turn adds a new beat instead of circling.
- Tone and point of view (I / you / he / she) stay consistent with the premise and the lines already written.

3. VERNACULAR & GENRE FIT:
- Genre slang, AAVE, dropped copulas ("we out here") and invariant present tense ("he run the pack") are fine when they suit the genre.
- Reject telegraphese: articles or prepositions deleted to hit a length ("Paws in snow, heavy on pack").
- Reject words whose register clashes with the genre ("drop-top coupe" in an ethereal ambient track); the words must sound like this genre's own.

4. TIRED CLICHÉS:
- Flag only stock rhyme pairs and worn-out images ("fire / desire", "heart of gold", "fierce and bold").
- Everyday idioms and conversational phrases ("take my heart", "fall apart", "right now") are natural speech, not clichés.

FIXES:
For every line that fails SENSE or STORY, write a replacement that keeps its place in the rhyme scheme and roughly its length, uses the simplest natural phrasing, and fits the scene. Leave "fix" empty for lines that pass.

OUTPUT INSTRUCTIONS:
Return one valid JSON object, no markdown, no preamble, exactly this shape. The "lines" array has one entry per draft line, in order:
{{
  "lines": [
    {{"line": <number>, "meaning": "plain-English paraphrase", "makes_sense": true | false, "forced_word": "word only there for rhyme or length, else empty", "fix": "rewritten line, else empty"}}
  ],
  "sense": {{"score": 1-5, "verdict": "pass" | "fail", "feedback": "concise observation"}},
  "story": {{"score": 1-5, "verdict": "pass" | "fail", "feedback": "concise observation"}},
  "vernacular": {{"score": 1-5, "verdict": "pass" | "fail", "feedback": "concise observation"}},
  "cliches": {{"score": 1-5, "verdict": "pass" | "fail", "feedback": "concise observation"}},
  "passed": true | false,
  "summary": "1-2 sentences: the single most important change, or why the draft works"
}}

TRACK CONTEXT:
• Song Title: "{title}"
• Genre & Arrangement: {genre} ({key_scale}, {tempo})
• Reference Feel: {reference}
• Section: [{section}]
• Core Story / Theme: {story}
{scene}{recent_lines}{contract}{engine_facts}
DRAFT LINES TO EVALUATE (one "lines" entry each, in this order):
{lines}"""

# key -> (friendly_name, template_text, [param names], description)
_DEFAULTS: dict[str, tuple[str, str, list[str], str]] = {
    "storyline": (
        "Storyline (narrative treatment)",
        "You are an expert songwriter creating a 3-4 sentence narrative story treatment "
        "as a 3-act miniature play.\n\n"
        "{context}\n\n"
        "CRITICAL RULES:\n"
        "1. Ground the story directly in the specific characters, situation, and emotional "
        "dilemma in the TITLE and BRIEF.\n"
        "   - Do NOT use generic or out-of-context cliches (do NOT default to 'cold corridors', "
        "'staring into terminals', 'cold coffee', or 'worn-out hands' unless specifically "
        "requested in the brief).\n"
        "   - Use physical setting, props, textures, and sensory actions authentic to THIS "
        "specific song and its characters.\n"
        "2. Structure in 3 cinematic movements:\n"
        "   - Act I (Atmosphere & Ambiguity): Establish the immediate physical setting, "
        "character interaction, and visceral tension without giving away the resolution.\n"
        "   - Act II (Human Action & Conflict): Ground the characters in concrete physical "
        "gestures that expose the friction or vulnerability between them.\n"
        "   - Act III (The Climax & Emotional Revelation): Reveal what is fundamentally on the "
        "line and the emotional truth that drives the song's lyric hook.\n"
        "3. Match the narrative energy to the musical tempo ({bpm} BPM) and sound direction.\n\n"
        "Output ONLY the 3-4 sentence narrative treatment. No title, no commentary, no quotes, "
        "no markdown headers.",
        ["context", "bpm"],
        "Writes the short story/concept treatment from the title and brief. "
        "{context} = title/brief/musical context assembled by the app; {bpm} = tempo.",
    ),
    "interview_questions": (
        "Interview Questions (producer Q&A)",
        "A songwriter submitted this brief for a new song:\n"
        "\"{brief}\"\n\n"
        "Production context: {genre}, {bpm} BPM, {music_key} {scale}.{reference}\n\n"
        "Write the 3 questions a professional co-writer would ask before drafting lyrics for "
        "this brief. Each question must be specific to this brief, not generic craft advice.\n\n"
        "For each question give 3 distinct, concrete answers the writer can pick from. Each "
        "option must be a usable answer in itself, not a category label, and the three must "
        "lead the song somewhere genuinely different. The first option is the recommended default.\n\n"
        "Respond with JSON only, no prose, in exactly this shape:\n"
        '{{"questions": [{{"id": "short_snake_case", "question": "...", "why": "...", '
        '"options": ["...", "...", "..."]}}]}}',
        ["brief", "genre", "bpm", "music_key", "scale", "reference"],
        "Generates the Step-3 producer questions. {reference} = optional reference-track note.",
    ),
    "song_context": (
        "Song Context (shared brief header)",
        "You are an expert songwriter co-writing an original song.\n"
        "FULL CREATIVE BRIEF & PRODUCTION CONTEXT:\n"
        "- Song Title: {title}\n"
        "- Genre / Sound Direction: {genre}\n"
        "- Lyrics Language: {language}\n"
        "- Moods / Atmosphere: {moods}\n"
        "- Instrumentation: {instruments}\n"
        "- Story & Narrative Arc: {story}\n"
        "- Song Structure (Section Plan):\n{section_map}\n"
        "- Musical Specs: {bpm} BPM in {key} {scale} ({vocal} arrangement)\n"
        "- Vocabulary & Register: {register}.\n"
        "- Non-Word Vocables & Ad-Libs for this style:\n"
        "  Style Mode: {vocable_category}\n"
        "  Parenthetical Accent Ad-Libs: {vocable_calls}\n"
        "  Melodic Pre-Line Hums & Chants: {vocable_filler}\n"
        "  Placement & Rhythm Rule: {vocable_usage}\n\n"
        + _LINE_RULES,
        ["title", "genre", "language", "moods", "instruments", "story", "section_map", "bpm", "key", "scale", "vocal",
         "register", "vocable_category", "vocable_calls", "vocable_filler", "vocable_usage"],
        "The shared creative-brief header prepended to every line-writing prompt. "
        "The writing rules live at the end of this prompt.",
    ),
    "line_writing": (
        "Line Writing (one line / block at a time)",
        "{context}\n\n"
        "TASK:\n"
        "  Write [{section}] {which} (Scheme: {scheme}).\n"
        "  Length (guides, not hard rules — singability and natural phrasing come first):\n"
        "{syllable_guide}\n"
        "{rhyme_block}"
        "{macro}"
        "  {output_rule} No quotes, no markdown, no commentary.",
        ["context", "section", "which", "scheme", "syllable_guide", "rhyme_block",
         "macro", "output_rule"],
        "Writes each lyric line in the streaming path. {context} = the Song Context header "
        "plus progress so far; the rest are per-line targets.",
    ),
    "whole_song": (
        "Whole Song (one-shot full lyric)",
        "{context}\n\n"
        + _LINE_RULES + "\n\n"
        "TASK:\n"
        "  Write the complete lyric now, following the section plan. Mark each section with "
        "its bracketed tag on its own line.\n"
        "  {output_rule}",
        ["context", "output_rule"],
        "Writes a whole lyric in a single call (used when not drafting section by section).",
    ),
    "line_rules": (
        "Writing Rules (sent with every lyric line)",
        _LINE_RULES,
        [],
        "Craft rules placed right above every Step 4 writing task and in the copy-paste "
        "instructions. Edit them to change what each line is told to do.",
    ),
    "lyric_critic": (
        "Lyric Critic (Step 4 line review)",
        _LYRIC_CRITIC,
        ["title", "genre", "key_scale", "tempo", "reference", "section", "story",
         "scene", "recent_lines", "contract", "engine_facts", "lines"],
        "Reviews each Step 4 draft in Auto LLM mode. Its JSON verdict decides whether the "
        "turn passes and its rewrites go into the retry, so keep the JSON keys. Literal "
        "braces must be doubled: {{ }}.",
    ),
    "engine_prompt": (
        "Engine Prompts (YuE2 & MiniMax)",
        "Write the production prompt for an AI music engine for this song.\n\n"
        "SONG BRIEF & CREATIVE DIRECTION\n"
        "- Title: {title}\n"
        "- Brief: {brief}\n"
        "- Storyline: {story}\n"
        "- Style & Sound Direction: {style}\n\n"
        "CONFIRMED PRODUCTION SPEC\n"
        "- genre: {genre} (authoritative; do not replace with reference/template genre)\n"
        "- subgenre/influences: {subgenre}\n"
        "- lyrics language: {language}\n"
        "- tempo: {bpm} BPM\n"
        "- key: {music_key} {scale}\n"
        "- vocal: {vocal}\n\n"
        "WRITER'S DECISIONS & PRODUCER ANSWERS\n"
        "{decisions}\n\n"
        "REFERENCE MATERIAL from a comparable production template. Use it for register and\n"
        "level of detail. Describe THIS song; do not copy these sentences.\n"
        "{reference}\n\n"
        "Produce the following in JSON:\n\n"
        "1. \"minimax\": one entry per field below. Each is 2-4 sentences of specific\n"
        "   production description written for this brief: name instruments, frequency\n"
        "   character, spatial treatment and how the arrangement changes across sections.\n"
        "   For \"instrument_lifecycle\" write two lines exactly, starting \"Primary:\" and\n"
        "   \"Secondary:\".\n"
        "{field_list}\n\n"
        "2. \"yue2\": 8-14 concise, comma-separated style tags for YuE2 audio generation.\n"
        "   Each tag must be a short phrase (1-5 words), NOT a complete sentence, and NO bracketed section names.\n"
        "   Construct the tags by integrating ALL supplied production information and producer answers:\n"
        "   - Language (e.g. \"English\")\n"
        "   - Tempo and Key/Scale: exactly \"{bpm} BPM\" and \"{music_key} {scale}\"\n"
        "   - Genre and Influences: authoritative genre \"{genre}\" and stylistic subgenres/influences\n"
        "   - Concrete Instruments: specific physical instruments, synth types, bass types, and drum elements reflecting the brief and answers\n"
        "   - Dominant Mood & Atmosphere: emotional attitude and sonic ambiance\n"
        "   - Vocal Tone & Delivery: vocal gender and delivery tone (or \"instrumental\" if instrumental)\n"
        "   Shape: [\"<language>\", \"{bpm} BPM\", \"{genre}\", \"<influence>\", \"<instruments>\", \"<mood>\", \"<vocal gender>\", \"<vocal tone>\", \"{music_key} {scale}\"]\n\n"
        "3. Canonical metadata fields:\n"
        "   - \"language\": string naming the lyric language (e.g. \"English\")\n"
        "   - \"moods\": list of 2-5 concise mood/atmosphere terms (e.g. [\"dark\", \"melancholic\"])\n"
        "   - \"instruments\": list of 3-6 specific concrete instruments chosen for this song (e.g. [\"layered acoustic piano\", \"warm bass guitar\", \"subtle brushed snare\"])\n"
        "   - \"vocal\": concise vocal delivery specification (e.g. \"warm baritone lead vocal\" or \"instrumental\")\n\n"
        "Respond with JSON only:\n"
        '{{"minimax": {{"<field>": "<text>"}}, "yue2": ["tag", "tag"], "language": "English", "moods": ["mood1", "mood2"], "instruments": ["inst1", "inst2"], "vocal": "vocal description"}}',
        [
            "title", "brief", "story", "style", "genre", "subgenre", "language",
            "bpm", "music_key", "scale", "vocal", "decisions", "reference", "field_list",
        ],
        "Authors production prompts for MiniMax Music 3 and concise style tags for "
        "YuE2 from the song brief, story, musical specs, and producer decisions. "
        "Literal braces must be doubled: {{ }}.",
    ),
}

_SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS prompt_template (
    key          VARCHAR PRIMARY KEY,
    name         VARCHAR NOT NULL,
    params       VARCHAR NOT NULL,
    description  VARCHAR DEFAULT '',
    active       INTEGER NOT NULL          -- active version number
)
"""
# Every saved version of every prompt. Version 0 is the pristine original.
_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS prompt_version (
    key        VARCHAR NOT NULL,
    version    INTEGER NOT NULL,
    template   VARCHAR NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    PRIMARY KEY (key, version)
)
"""


class _SafeDict(dict):
    def __missing__(self, k: str) -> str:
        return "{" + k + "}"


def _ensure_tables() -> Database:
    db = workbench_db()
    with db.transaction() as cur:
        cur.execute(_SCHEMA_TEMPLATE)
        cur.execute(_SCHEMA_VERSION)
    return db


def seed_defaults(force: bool = False) -> int:
    """Insert any missing prompt + its version-0 original. Keeps edits unless force."""
    db = _ensure_tables()
    with _lock:
        with db.transaction() as cur:
            existing = {r[0] for r in cur.execute("SELECT key FROM prompt_template").fetchall()}
            n = 0
            for key, (name, tmpl, params, desc) in _DEFAULTS.items():
                pstr = ",".join(params)
                if force or key not in existing:
                    cur.execute("DELETE FROM prompt_template WHERE key=?", [key])
                    cur.execute("DELETE FROM prompt_version WHERE key=? AND version=0", [key])
                    cur.execute(
                        "INSERT INTO prompt_template (key, name, params, description, active) "
                        "VALUES (?, ?, ?, ?, 0)",
                        [key, name, pstr, desc],
                    )
                    cur.execute(
                        "INSERT INTO prompt_version (key, version, template) VALUES (?, 0, ?)",
                        [key, tmpl],
                    )
                    n += 1
                else:
                    # Keep edits/active pointer; refresh name/params/desc and the v0 original.
                    cur.execute(
                        "UPDATE prompt_template SET name=?, params=?, description=? WHERE key=?",
                        [name, pstr, desc, key],
                    )
                    cur.execute(
                        "INSERT INTO prompt_version (key, version, template) VALUES (?, 0, ?) "
                        "ON CONFLICT (key, version) DO UPDATE SET template=excluded.template",
                        [key, tmpl],
                    )
            return n


def _ensure_seeded() -> None:
    global _seeded
    if _seeded:
        return
    with _lock:
        if _seeded:
            return
        seed_defaults()
        _seeded = True


def get_template(key: str) -> str:
    """The active version's text for ``key`` (falls back to code default)."""
    _ensure_seeded()
    db = workbench_db()
    row = db.read_one(
        "SELECT v.template FROM prompt_template t "
        "JOIN prompt_version v ON v.key = t.key AND v.version = t.active "
        "WHERE t.key = ?",
        [key],
    )
    if row and row[0] is not None:
        return row[0]
    return _DEFAULTS[key][1] if key in _DEFAULTS else ""


def render(key: str, params: dict[str, Any]) -> str:
    """Render the active template for ``key``; unknown placeholders stay literal."""
    tmpl = get_template(key)
    try:
        return tmpl.format_map(_SafeDict(params))
    except (ValueError, IndexError):
        return tmpl


def list_prompts() -> list[dict[str, Any]]:
    """All prompts for the UI: friendly name, params, active version, version list."""
    _ensure_seeded()
    db = workbench_db()
    rows = db.read_dicts(
        "SELECT key, name, params, description, active FROM prompt_template ORDER BY name"
    )
    out = []
    for r in rows:
        versions = db.read(
            "SELECT version FROM prompt_version WHERE key=? ORDER BY version", [r["key"]]
        )
        out.append({
            "key": r["key"],
            "name": r["name"],
            "params": [p for p in str(r["params"] or "").split(",") if p],
            "description": r["description"],
            "active": r["active"],
            "versions": [v[0] for v in versions],
            "template": get_template(r["key"]),
        })
    return out


def line_rules_text() -> str:
    """The active "Writing Rules" prompt (Settings -> LLM Prompts).

    Inserted by the prompt builders that assemble their text directly: the Step 4
    line and turn prompts and the copy-paste instructions.
    """
    return get_template("line_rules")


def save_version(key: str, template: str) -> int:
    """Save ``template`` as a NEW version and make it active. Returns version no."""
    _ensure_seeded()
    if key not in _DEFAULTS:
        raise KeyError(f"unknown prompt: {key}")
    db = _ensure_tables()
    with _lock:
        with db.transaction() as cur:
            nxt = cur.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM prompt_version WHERE key=?", [key]
            ).fetchone()[0]
            cur.execute(
                "INSERT INTO prompt_version (key, version, template) VALUES (?, ?, ?)",
                [key, nxt, template],
            )
            cur.execute("UPDATE prompt_template SET active=? WHERE key=?", [nxt, key])
            return nxt


def activate_version(key: str, version: int) -> None:
    """Make an existing saved version the active one."""
    _ensure_seeded()
    db = _ensure_tables()
    with _lock:
        with db.transaction() as cur:
            exists = cur.execute(
                "SELECT 1 FROM prompt_version WHERE key=? AND version=?", [key, int(version)]
            ).fetchone()
            if not exists:
                raise KeyError(f"no version {version} for {key}")
            cur.execute("UPDATE prompt_template SET active=? WHERE key=?", [int(version), key])


def reset_default(key: str) -> None:
    """Make version 0 (the pristine original) active again."""
    activate_version(key, 0)
