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
    "- Style: Conversational SVO spoken English; concrete sensory details; no melodramatic filler.\n"
    "- Rhymes: True content-word rhymes only (ban clichés: night/light, fire/desire, love). Never invert syntax.\n"
    "- Continuity: Each line continues the narrative scene of the line before it."
)

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
        ["title", "genre", "story", "section_map", "bpm", "key", "scale", "vocal",
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
    """The folded line-writing guidance as plain prose (no attributions).

    Used by prompt builders that assemble their text directly rather than via a
    stored template, so the same guidance appears everywhere.
    """
    return _LINE_RULES


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
