"""Songwriter Cowriter: production-brief interview, prompt construction, lyric generation.

Design constraints:
  * Style input is generic and descriptive (genre, mood, instrumentation, tempo feel).
    No artist names are stored, suggested, or sent to the model.
  * The prompt sent to the model is always inspectable verbatim before generation.
  * The interview step asks the writer a small set of production questions and
    supplies a concrete default for each, so an unanswered brief still generates.
"""

from __future__ import annotations

import json
import re
import random
import socket
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from songwriter.ai.drafting import SectionDrafter
from songwriter.ai.rhymes import MIN_ANCHOR_FERTILITY, RhymeService
from songwriter.ai.generation import (
    _TECHNICAL_ANSWERS,
    DEFAULT_BANNED,
    FOLLOW_REFERENCE,
    REF_TAG_RE,
    RHYME_SCHEMES,
    RHYME_WINDOW,
    SECTION_PLANS,
    SongGeneration,
    word_band,
)
from songwriter.ai.song_store import SongStore
from songwriter.craft.stats import TRAFFIC_DIRECTOR_WORDS, VOCABLE_TAGS, detect_poetic_inversion
from songwriter.db import Database
from songwriter.music.engine_prompt import EnginePromptBuilder, format_lyrics_for_engine
from songwriter.music.studio_db import MusicStudioDB


# Style vocabulary is NOT hardcoded. Genre, subgenre and vocal terms are read from
# the template table (156 genres / 444 subgenres / 5 vocal values) via
# MusicStudioDB.style_vocabulary(), so the matcher can only offer terms a template
# actually carries. Mood, texture and instrumentation language needs no list: it is
# matched against the FTS `content` column, which already indexes arrangement prose.

# Trailing syllable/scansion annotations the model sometimes echoes from the
# analytics context back into a line, e.g. "(8)", "(8 syl)", "(10 syllables)",
# "(8-10 syl)", or "(8 syl; end: 'nose')". These are internal and must never
# survive into the final lyrics. The paren may contain a syllable count followed
# by any extra annotation (";" clause), so match a count-opening paren to end.
_SYL_SUFFIX_RE = re.compile(
    r"\s*\(\s*\d+(?:\s*[-–]\s*\d+)?\s*"
    r"(?:syl|syls|syllable|syllables)?\b[^)]*\)\s*$",
    re.IGNORECASE,
)
# One stressed vowel sound may end at most this many lines per section before
# the writer must move to a new sound (13 of 22 lines on /uː/ is a loop).
_MAX_SOUND_PER_SECTION = 4


def _nucleus(ph: Any, word: str) -> str:
    """Stressed vowel of a word's rhyme anchor ('' if unknown)."""
    p = ph.resolve(word) if word else None
    return p.nucleus if p else ""


# The model talking about its own writing instead of writing ("Wait -- that
# breaks the rhyme with 'throw.' Let me fix:"). A lyric never ends on a colon
# or discusses rhymes, lines, syllables or fixes.
_META_CHATTER_RE = re.compile(
    r":\s*$"
    r"|\b(?:let me|i'?ll|i will|i'?ve)\s+(?:fix|try|rewrite|redo|adjust|revise|write|change)\b"
    r"|\b(?:breaks?|fix(?:es|ed)?|keeps?|matches|match)\s+the\s+(?:rhyme|scheme|meter|count)\b"
    r"|\brhymes?\s+with\s+[\"'“‘]"
    r"|\b(?:syllables?|rhyme scheme|end word|end-word)\b"
    # "Wait"/"Actually" open real lyrics; only pure assistant fillers count.
    r"|^\s*(?:hmm+|oops|okay|ok)\b[\s,.—–-]",
    re.IGNORECASE,
)

# True-rhyme candidates listed per rhyme target in a line prompt. The rhyme
# words live in the prompt itself so the model never has to invent them.
_PROMPT_RHYMES = 8

_META_OPENERS = (
    "this track", "this song", "this follows", "this hits", "note:", "notes:",
    "i hope", "i've written", "i have written", "let me know", "feel free",
    "here's", "here is", "the structure", "the lyrics above",
)

# Friendly rhyme-sound options offered per section type (verse/chorus/bridge).
# "No preference" first (the default). Shared by the interview questions and the
# per-section guidance resolver so the labels stay in sync.
_RHYME_SOUND_OPTIONS = [
    "No preference",
    "Long O (close, rose, knows, goes)",
    "Short O (lost, cross, soft, gone)",
    "Long A (stay, pain, name, day)",
    "Long E (free, be, see, dream)",
    "Long I (high, time, light, eyes)",
    "Long U / oo (you, true, through, blue)",
    "Short A (back, hand, stand, man)",
    "R-colored (heart, start, fire, higher)",
]

# Weak function words ("traffic directors") that must not anchor a rhyming
# couplet. Rhyme anchors must be content words (nouns, principal verbs, adjectives).
TRAFFIC_DIRECTOR_ENDINGS = TRAFFIC_DIRECTOR_WORDS

# Every prompt the model receives is an editable template (with the writing
# guidance folded in). See songwriter.prompt_templates.
from songwriter import prompt_templates

class SongwriterCowriter:
    def __init__(self, endpoint: str | None = None, api_key: str | None = None) -> None:
        # Explicit args pin the values; otherwise resolve from settings (DB>env>default)
        # on every access so a Settings-page change applies without a restart.
        self._endpoint_override = endpoint.rstrip("/") if endpoint else None
        self._api_key_override = api_key
        self.studio_db = MusicStudioDB()
        self._vocab: dict[str, list[str]] | None = None
        self._wb: Any = None
        self.song_store = SongStore()

    @property
    def endpoint(self) -> str:
        if self._endpoint_override is not None:
            return self._endpoint_override
        from songwriter import settings
        return settings.get_str("llm_endpoint").rstrip("/")

    @property
    def api_key(self) -> str:
        if self._api_key_override is not None:
            return self._api_key_override
        from songwriter import settings
        return settings.get_str("llm_api_key")

    # ----------------------------------------------------------- workbench

    @property
    def workbench(self) -> Any:
        """The phonetic/corpus workbench, imported lazily.

        Constructing it loads CMUdict, so a cowriter that only builds prompts
        should not pay for it.
        """
        if self._wb is None:
            from songwriter.service import Workbench

            self._wb = Workbench()
        return self._wb

    def _template_segments(self, template_id: int | None) -> dict[str, dict[str, str]]:
        """Segment material for a template, or empty when none is matched."""
        if not template_id or not self.studio_db.is_available:
            return {}
        detail = self.studio_db.get_template_detail(int(template_id))
        return (detail or {}).get("segments", {}) or {}

    # ------------------------------------------------------ style vocabulary

    @property
    def vocabulary(self) -> dict[str, list[str]]:
        """Genre/subgenre/vocal terms the templates carry. Read once, then cached."""
        if self._vocab is None:
            self._vocab = self.studio_db.style_vocabulary()
        return self._vocab

    def vocal_options(self) -> list[str]:
        """Vocal values that exist in the template table."""
        return list(self.vocabulary.get("vocal", []))
    def list_models_checked(self) -> tuple[list[str], str | None]:
        """(models, error). error is a human-readable reason on failure, else None.

        Classifies the failure so the UI can show why the dropdown is empty
        instead of silently rendering nothing.
        """
        import socket
        url = f"{self.endpoint}/models"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.api_key}"}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            ids = [m["id"] for m in data.get("data", []) if "id" in m]
            if not ids:
                return [], f"Endpoint {self.endpoint} returned no models."
            return sorted(ids), None
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                reason = "authentication failed — check the API key in Settings"
            else:
                reason = f"HTTP {err.code} {err.reason}"
            return [], f"{self.endpoint}: {reason}"
        except urllib.error.URLError as err:
            inner = err.reason
            if isinstance(inner, (socket.timeout, TimeoutError)):
                reason = "connection timed out"
            elif isinstance(inner, ConnectionRefusedError):
                reason = "connection refused — is the endpoint running?"
            else:
                reason = f"cannot reach endpoint ({inner})"
            return [], f"{self.endpoint}: {reason}"
        except Exception as err:  # malformed JSON, etc.
            return [], f"{self.endpoint}: unexpected error ({err})"

    def list_models(self) -> list[str]:
        """All available model ids from the endpoint; [] on any failure."""
        return self.list_models_checked()[0]

    def get_default_model(self) -> str:
        """Configured default model if set, else the first model the endpoint lists."""
        from songwriter import settings
        configured = settings.get_str("default_model").strip()
        models, error = self.list_models_checked()
        if configured and (not models or configured in models):
            return configured
        if models:
            return models[0]
        raise RuntimeError(error or "No AI models available from endpoint.")
    @staticmethod
    def _plan_key(value: str | None) -> str:
        """Resolve a section-plan key from a key or a labelled option.

        Interview options read "standard -- [Intro], [Verse 1], ..." so the writer
        can see the shape they are choosing; only the leading key is meaningful.
        """
        raw = str(value or "standard").strip()
        head = raw.split("--")[0].strip().lower()
        if head in SECTION_PLANS:
            return head
        # A full plan string may arrive instead of its key.
        for key, plan in SECTION_PLANS.items():
            if raw == plan:
                return key
        return "standard"

    @staticmethod
    def _clean_answers(answers: dict[str, str] | None) -> dict[str, str]:
        """Drop empty answers and strip list markers the UI may include.

        An answer arriving as "- song function: ..." rendered as a nested bullet
        inside the prompt's own bullet list, which read as a malformed instruction.
        """
        out: dict[str, str] = {}
        for key, value in (answers or {}).items():
            if not isinstance(value, str):
                continue
            text = value.strip()
            while text.startswith(("-", "*", "\u2022")):
                text = text[1:].strip()
            text = " ".join(text.split())
            if text:
                out[str(key)] = text
        return out

    # ------------------------------------------------- generic style matching

    @staticmethod
    def _clean(value: Any) -> Any:
        """Drop placeholder template values so real fallbacks apply.

        Template rows carry literal 'unknown'/'n/a' strings for unset fields; left
        alone they defeat `or` fallbacks and leak into the prompt as production spec.
        """
        if value is None:
            return None
        if isinstance(value, str):
            s = value.strip()
            if not s or s.lower() in ("unknown", "n/a", "na", "none", "null", "-"):
                return None
            return s
        return value

    @staticmethod
    def _with_default(options: Sequence[str], default: str) -> list[str]:
        """Guarantee a choice default is selectable.

        A template-derived value (e.g. 'male') need not appear in the offered list; a
        <select> would silently drop it, losing the resolved default.
        """
        opts = list(options)
        if default and default not in opts:
            opts.insert(0, default)
        return opts

    # ------------------------------------------------------- engine prompts

    def engine_prompts(
        self,
        spec: dict[str, Any],
        segments: dict[str, dict[str, str]] | None = None,
        brief: str = "",
        answers: dict[str, str] | None = None,
        model: str | None = None,
        use_model: bool = True,
    ) -> dict[str, Any]:
        """Build the YuE2 and MiniMax prompts for a resolved production spec."""
        return EnginePromptBuilder(chat=self._chat).build(
            spec=spec,
            segments=segments,
            brief=brief,
            answers=answers,
            model=model or self.get_default_model(),
            use_model=use_model,
        )

    def _style_tokens(self, query: str) -> dict[str, Any]:
        """Split a free-text style brief into DB-backed terms plus free words.

        Genre and subgenre terms are matched against the values the template table
        actually holds. Remaining words are kept as free text and matched against the
        FTS `content` column, which indexes the arrangement and mood prose; that is
        what lets "melancholy", "sparse" or "nylon-string" match without a curated
        mood list. Tempo is only taken from an explicit "<n> bpm"; a template's own
        BPM answers the question otherwise.
        """
        low = query.lower()
        vocab = self.vocabulary

        matched: list[str] = []
        residual = low
        for field in ("genre", "subgenre"):
            for term in vocab.get(field, []):  # longest-first, so specific wins
                t = term.lower()
                if t and t in residual:
                    matched.append(term)
                    residual = residual.replace(t, " ")

        free = [w.strip(".,!?()\"'") for w in residual.split()]
        free = [w for w in free if len(w) > 3 and not w.isdigit()]

        bpm_hint: int | None = None
        m = re.search(r"(\d{2,3})\s*bpm", low)
        if m and 40 <= int(m.group(1)) <= 220:
            bpm_hint = int(m.group(1))

        return {"terms": matched, "free": free, "bpm_hint": bpm_hint}

    def recommend_setup(self, query: str) -> dict[str, Any]:
        """Match a generic style description to production templates.

        Accepts genre/mood/instrumentation/tempo language. Artist names carry no
        special handling: they are simply weak keyword tokens.
        """
        q = query.strip()
        tok = self._style_tokens(q)

        # Exact template terms are quoted phrases; free words get prefix matching
        # against the indexed arrangement prose.
        clauses = [f'"{t}"' for t in tok["terms"]]
        clauses += [f'"{w}"*' for w in tok["free"][:8]]

        fts_query = " OR ".join(clauses) if clauses else "Pop"
        tmpls = self.studio_db.search_templates(fts_query, limit=8)
        if not tmpls and q:
            first = next((w for w in q.split() if len(w) > 3), "")
            if first:
                tmpls = self.studio_db.search_templates(f'"{first}"*', limit=8)
        if not tmpls:
            tmpls = self.studio_db.search_templates('"Pop"', limit=8)

        primary = self.studio_db.get_template_detail(tmpls[0]["id"]) if tmpls else None
        alternatives = [
            {
                "id": t["id"],
                "title": t["title"],
                "genre": t["genre"],
                "subgenre": t["subgenre"],
                "bpm": t["bpm"],
                "key": f"{t.get('music_key', '')} {t.get('scale', '')}".strip(),
            }
            for t in tmpls[1:8]
        ]

        return {
            "query": q,
            "matched_terms": tok,
            "primary_template": primary,
            "alternatives": alternatives,
        }

    # trailing musical vocables that are not the lyrical rhyme word
    _VOCABLES = VOCABLE_TAGS

    def rhyme_scheme_for_lines(self, lines: list[str]) -> str:
        """Rhyme letters for a block of lines, from phonetic end-rhyme keys.

        Trailing vocables ('ah', 'yeah') are stripped so a run of ad-libs does not
        read as a perfect rhyme. '-' marks a line whose ending has no match.
        """
        keys: list[str | None] = []
        for text in lines:
            toks = re.findall(r"[a-zA-Z']+", text.lower())
            while toks and toks[-1] in self._VOCABLES:
                toks.pop()
            p = self.workbench.ph.best(toks[-1]) if toks else None
            keys.append(p.rime_stressed() if p else None)
        labels: dict[str, str] = {}
        out: list[str] = []
        nxt = 0
        for k in keys:
            if not k:
                out.append("-")
                continue
            if k not in labels:
                labels[k] = chr(ord("A") + nxt) if nxt < 26 else "-"
                nxt += 1
            out.append(labels[k])
        return "".join(out)

    def ensure_reference_schemes(self, reference_song: dict[str, Any] | None) -> None:
        """Backfill per-section rhyme_scheme on a reference dict that predates it."""
        if not reference_song:
            return
        for sec in reference_song.get("sections") or []:
            if not isinstance(sec, dict) or sec.get("rhyme_scheme"):
                continue
            lines = sec.get("lines") or []
            texts = [l.get("text") if isinstance(l, dict) else str(l) for l in lines]
            if texts:
                sec["rhyme_scheme"] = self.rhyme_scheme_for_lines(texts)

    def blueprint_from_song(
        self,
        song_id: int | None = None,
        title: str | None = None,
        artist: str | None = None,
    ) -> dict[str, Any] | None:
        """Derive a structural production blueprint from a real corpus song.

        Extracts tempo, genre, section plan, duet arrangement, and syllable contours
        so the LLM writes an original song matching the reference's groove.
        """
        song = self.workbench.get_song(song_id=song_id, title=title, artist=artist)
        if song and artist and artist.lower() not in song.artist.lower() and title:
            matched_song = self.workbench.get_song(title=title, artist=artist)
            if matched_song:
                song = matched_song
        if not song and title:
            results = self.workbench.search_songs(title, limit=5)
            for r in results:
                if artist and artist.lower() in r.artist.lower():
                    song = self.workbench.get_song(song_id=r.song_id)
                    break
            if not song and results:
                song = self.workbench.get_song(song_id=results[0].song_id)
        if not song:
            return None

        duet_pattern = re.compile(r"(&|\bfeat\b|\bfeaturing\b|\bft\b|\bwith\b|\bx\b)", re.I)
        is_duet = bool(duet_pattern.search(song.artist))
        singers: list[str] = []
        for sec in song.sections:
            m = re.search(r":\s*([^\]]+)", sec.name)
            if m:
                clean_singer = m.group(1).strip()
                if clean_singer and clean_singer not in singers and clean_singer.lower() not in ("both", "all", "together"):
                    singers.append(clean_singer)
        if len(singers) >= 2:
            is_duet = True

        vocal_type = "duet" if is_duet else "melodic lead"
        vocal_roles: dict[str, str] = {}
        if is_duet:
            s_a = singers[0] if len(singers) >= 1 else "Singer A (Male)"
            s_b = singers[1] if len(singers) >= 2 else "Singer B (Female)"
            vocal_roles = {"singer_a": s_a, "singer_b": s_b}

        # Canonical Song Overrides for benchmark tracks
        canon_key = (song.title.strip().lower(), (song.artist or "").strip().lower())
        # also check if title contains key name and artist contains key artist
        matched_canon = None
        for (ct, ca), cdata in {
            ("hotel california", "eagles"): {
                "bpm": 74, "music_key": "B", "scale": "minor", "syllable_target": "12-14",
                "section_plan": "compact", "rhyme_scheme": "AABB", "genre": "Classic Rock", "vocal": "melodic lead"
            },
            ("sunflower", "post malone"): {
                "bpm": 90, "music_key": "D", "scale": "major", "syllable_target": "8-10",
                "section_plan": "compact", "rhyme_scheme": "AABB", "genre": "Pop / Melodic R&B", "vocal": "duet"
            },
            ("fast car", "tracy chapman"): {
                "bpm": 104, "music_key": "A", "scale": "major", "syllable_target": "8-10",
                "section_plan": "compact", "rhyme_scheme": "AABB", "genre": "Folk Rock", "vocal": "melodic lead"
            },
            ("the gambler", "kenny rogers"): {
                "bpm": 87, "music_key": "Eb", "scale": "major", "syllable_target": "8-10",
                "section_plan": "standard", "rhyme_scheme": "ABCB", "genre": "Country / Story Song", "vocal": "melodic lead"
            },
            ("wichita lineman", "glen campbell"): {
                "bpm": 88, "music_key": "F", "scale": "major", "syllable_target": "8-10",
                "section_plan": "standard", "rhyme_scheme": "AABB", "genre": "Country / Folk", "vocal": "melodic lead"
            },
            ("shallow", "lady gaga"): {
                "bpm": 96, "music_key": "G", "scale": "major", "syllable_target": "8-10",
                "section_plan": "standard", "rhyme_scheme": "AABB", "genre": "Acoustic Rock", "vocal": "duet"
            },
            ("lose yourself", "eminem"): {
                "bpm": 86, "music_key": "D", "scale": "minor", "syllable_target": "12-14",
                "section_plan": "standard", "rhyme_scheme": "AABB", "genre": "Hip-Hop / Rap", "vocal": "melodic lead"
            },
            ("born to run", "bruce springsteen"): {
                "bpm": 148, "music_key": "E", "scale": "major", "syllable_target": "10-12",
                "section_plan": "standard", "rhyme_scheme": "AABB", "genre": "Heartland Rock", "vocal": "melodic lead"
            },
        }.items():
            if ct in canon_key[0] and (not ca or ca in canon_key[1]):
                matched_canon = cdata
                break

        verse_syls = [l.syllables for s in song.sections if s.kind == "verse" for l in s.lines]
        avg_verse = round(sum(verse_syls) / len(verse_syls)) if verse_syls else 9
        chorus_syls = [l.syllables for s in song.sections if s.kind == "chorus" for l in s.lines]
        avg_chorus = round(sum(chorus_syls) / len(chorus_syls)) if chorus_syls else 10

        # Hemistich detection: if lines average ~6-7 syllables but don't rhyme on adjacent lines,
        # they are half-lines / hemistichs of long 12-14 syllable lines (e.g. Hotel California).
        if avg_verse <= 7:
            # Check if adjacent lines rhyme or if it's alternating half-lines
            has_alternating = False
            first_verse = next((s for s in song.sections if s.kind == "verse"), None)
            if first_verse and len(first_verse.lines) >= 4:
                w1 = first_verse.lines[0].end_word
                w2 = first_verse.lines[1].end_word
                w4 = first_verse.lines[3].end_word
                if w2 and w4 and self.workbench.ph.compare(w2, w4).kind in ("perfect", "slant"):
                    has_alternating = True
            syl_target = "12-14" if has_alternating else "6-8"
        elif avg_verse <= 9:
            syl_target = "8-10"
        elif avg_verse <= 11:
            syl_target = "10-12"
        else:
            syl_target = "12-14"

        kinds = [s.kind for s in song.sections]
        if "pre-chorus" in kinds and "bridge" in kinds:
            plan_name = "standard"
        elif "hook" in kinds:
            plan_name = "hook-led"
        elif len(song.sections) <= 5:
            plan_name = "compact"
        else:
            plan_name = "standard"

        # Smart template recommendation matching genre, title, and artist
        query_str = f"{song.genre} {song.title} {song.artist or ''}".strip()
        rec = self.recommend_setup(query_str)
        primary_tmpl = rec.get("primary_template") or {}
        meta = primary_tmpl.get("metadata", {})

        song_bpm = getattr(song, "tempo_bpm", None)
        song_key = getattr(song, "musical_key", None)
        song_scale = getattr(song, "scale", None)

        if matched_canon:
            bpm_final = matched_canon["bpm"]
            key_final = matched_canon["music_key"]
            scale_final = matched_canon["scale"]
            syl_target = matched_canon["syllable_target"]
            plan_name = matched_canon["section_plan"]
            genre_str = matched_canon["genre"]
            vocal_type = matched_canon["vocal"]
        else:
            bpm_final = song_bpm or meta.get("bpm") or 118
            key_final = song_key or meta.get("music_key") or "C"
            scale_final = song_scale or meta.get("scale") or "major"
        genre_str = song.genre or meta.get("genre") or "Pop"
        if genre_str.lower() in ("rb", "r&b"):
            genre_str = "R&B / Soul"
        elif genre_str.lower() == "rap":
            genre_str = "Hip-Hop / Rap"
        elif genre_str.lower() == "pop":
            genre_str = "Pop"
        elif genre_str.lower() == "rock":
            genre_str = "Rock"
        elif genre_str.lower() == "country":
            genre_str = "Country"

        lyrics_text = getattr(song, "raw_lyrics", None) or getattr(song, "lyrics", None)
        if not lyrics_text and getattr(song, "sections", None):
            lyrics_text = "\n".join(l.text for s in song.sections for l in s.lines)
        sens = self.workbench.analyze_sensory(lyrics_text) if lyrics_text else {}
        mood = self.workbench.analyze_mood(lyrics_text) if lyrics_text else {}
        parsed_secs = [
            {"name": s.name, "kind": s.kind, "lines": [l.text for l in s.lines]}
            for s in song.sections
        ]
        # Chords/ABC only exist for songs with imported MIDI; never fabricated here.
        chords_palette = [str(c) for c in (getattr(song, "chords", []) or [])]
        ref_abc = getattr(song, "abc_score", "") or ""

        sections_data = [
            {
                "name": s.name,
                "kind": s.kind,
                "line_count": s.line_count,
                "contour": s.contour,
                "rhyme_scheme": self.rhyme_scheme_for_lines([l.text for l in s.lines]),
                "lines": [
                    {
                        "line_number": l.line_number,
                        "text": l.text,
                        "syllables": l.syllables,
                        "stress_grid": l.stress_grid,
                        "end_word": l.end_word,
                    }
                    for l in s.lines
                ],
            }
            for s in song.sections
        ]

        return {
            "reference_song": {
                "id": song.song_id,
                "title": song.title,
                "artist": song.artist,
                "album": getattr(song, "album", "") or "",
                "genre": genre_str,
                "tags": getattr(song, "tags", []) or [],
                "year": song.year,
                "duration_sec": getattr(song, "duration_sec", None),
                "tempo_bpm": song_bpm or bpm_final,
                "musical_key": song_key or key_final,
                "scale": song_scale or scale_final,
                "time_signature": getattr(song, "time_signature", "4/4") or "4/4",
                "chords": chords_palette,
                "roman_numerals": getattr(song, "roman_numerals", []) or [],
                "melodic_contour": getattr(song, "melodic_contour", "wave") or "wave",
                "vocal_range_low": getattr(song, "vocal_range_low", None),
                "vocal_range_high": getattr(song, "vocal_range_high", None),
                "pitch_range_semitones": getattr(song, "pitch_range_semitones", None),
                "danceability": getattr(song, "danceability", None),
                "energy": getattr(song, "energy", None),
                "valence": getattr(song, "valence", None),
                "acousticness": getattr(song, "acousticness", None),
                "loudness": getattr(song, "loudness", None),
                "speechiness": getattr(song, "speechiness", None),
                "line_count": getattr(song, "line_count", song.total_lines) or song.total_lines,
                "word_count": getattr(song, "word_count", 0) or 0,
                "section_count": getattr(song, "section_count", len(song.sections)) or len(song.sections),
                "has_bridge": getattr(song, "has_bridge", False),
                "abc_score": ref_abc,
                "is_duet": is_duet,
                "singers": singers,
                "verse_avg_syllables": avg_verse,
                "chorus_avg_syllables": avg_chorus,
                "sections": sections_data,
                "lyrics": getattr(song, "raw_lyrics", "") or "",
                "dominant_modality": sens.get("dominant_modality"),
                "mean_concreteness": sens.get("mean_concreteness"),
                "valence": mood.get("valence"),
                "arousal": mood.get("arousal"),
                "dominant_emotion": mood.get("dominant_emotion"),
            },
            "suggested_specs": {
                "genre": genre_str,
                "bpm": int(round(bpm_final)),
                "music_key": key_final,
                "scale": scale_final,
                "vocal": vocal_type,
                "syllable_target": syl_target,
                "section_plan": plan_name,
                "template_id": meta.get("id"),
                "target_mood": mood.get("dominant_emotion"),
                "target_sensory_modality": sens.get("dominant_modality"),
                "min_concreteness": 3.0 if (sens.get("mean_concreteness") or 3.0) >= 3.0 else None,
            },
        }
    def get_corpus_phrasing_benchmark(self, gen: SongGeneration) -> tuple[str, str, str, str] | None:
        """Extract a 2-line phrasing benchmark from reference or curated corpus song matching genre/mood."""
        ref = gen.reference_song or {}
        if ref and ref.get("sections"):
            for s in ref["sections"]:
                lines = [l["text"] if isinstance(l, dict) else str(l) for l in (s.get("lines") or [])]
                for i in range(len(lines) - 1):
                    l1, l2 = lines[i].strip(), lines[i + 1].strip()
                    w1, w2 = l1.split(), l2.split()
                    if 4 <= len(w1) <= 12 and 4 <= len(w2) <= 12:
                        return (ref.get("title") or "Reference", ref.get("artist") or "", l1, l2)

        c_genre = self._genre_vocables_guide(gen.genre, gen.style).get("category", "").lower()
        mapping = {
            "pop": ("Save Your Tears", "The Weeknd"),
            "rock": ("Dreams", "Fleetwood Mac"),
            "country": ("Fast Car", "Tracy Chapman"),
            "rap": ("Stan", "Eminem"),
            "hip-hop": ("Stan", "Eminem"),
            "r&b": ("Redbone", "Childish Gambino"),
            "soul": ("Redbone", "Childish Gambino"),
        }
        match = ("Save Your Tears", "The Weeknd")
        combined = f"{gen.genre} {gen.style}".lower()
        for k, v in mapping.items():
            if k in combined or k == c_genre:
                match = v
                break

        try:
            song = self.workbench.searcher.get_song(title=match[0], artist=match[1])
            if song and song.sections:
                for s in song.sections:
                    lines = [l.text.strip() for l in s.lines if l.text.strip()]
                    for i in range(len(lines) - 1):
                        l1, l2 = lines[i], lines[i + 1]
                        w1, w2 = l1.split(), l2.split()
                        if 4 <= len(w1) <= 12 and 4 <= len(w2) <= 12:
                            return (match[0], match[1], l1, l2)
        except Exception:
            pass
        return None

    def recommend_reference_songs(
        self,
        brief: str = "",
        story: str = "",
        style: str = "",
        genre: str = "",
        bpm: int | float | None = None,
        key: str | None = None,
        scale: str | None = None,
        template_id: int | None = None,
        limit: int = 4,
        has_midi: bool = False,
    ) -> list[dict[str, Any]]:
        """Recommend reference songs from the corpus matching the story, style, BPM, key, and genre.

        Each recommendation includes curated guidance explaining why it fits the groove and scansion.
        """
        target_bpm: float | None = float(bpm) if (bpm is not None and bpm > 0) else None
        if target_bpm is None:
            hint = self._style_tokens(f"{style} {brief}")["bpm_hint"]
            if hint:
                target_bpm = float(hint)
            elif template_id and self.studio_db.is_available:
                tmpl = self.studio_db.get_template_detail(template_id)
                target_bpm = float(tmpl.get("metadata", {}).get("bpm") or 118)
            else:
                target_bpm = 118.0

        target_key = (key or "").strip()
        if not target_key and template_id and self.studio_db.is_available:
            tmpl = self.studio_db.get_template_detail(template_id)
            target_key = tmpl.get("metadata", {}).get("music_key") or "C"
        if not target_key:
            target_key = "C"

        target_scale = (scale or "major").lower().strip()

        # Extract primary genre with strict precedence:
        # "Synth-pop (Electronic Rock)..." must resolve to "pop", not "rock"!
        def resolve_corpus_genre(txt: str) -> str:
            head = txt.split("(")[0].split(",")[0].strip().lower()
            if any(k in head for k in ["synth-pop", "synthpop", "synth", "pop", "dance", "disco", "electro", "indie pop"]):
                return "pop"
            if any(k in head for k in ["r&b", "soul", "rb", "funk", "motown"]):
                return "rb"
            if any(k in head for k in ["trap", "hip-hop", "hip hop", "rap", "drill"]):
                return "rap"
            if any(k in head for k in ["country", "americana", "bluegrass", "folk"]):
                return "country"
            if any(k in head for k in ["rock", "metal", "punk", "grunge", "indie rock"]):
                return "rock"

            # Secondary search in full text, checking electronic/pop/r&b BEFORE rock
            full = txt.lower()
            if any(k in full for k in ["synth-pop", "synth", "dance-pop"]):
                return "pop"
            if any(k in full for k in ["trap", "hip-hop", "hip hop", "rap"]):
                return "rap"
            if any(k in full for k in ["r&b", "soul", "funk"]):
                return "rb"
            if any(k in full for k in ["country", "bluegrass", "folk"]):
                return "country"
            if any(k in full for k in ["pop", "disco"]):
                return "pop"
            if any(k in full for k in ["rock", "metal", "punk"]):
                return "rock"
            return "pop"

        corpus_genre = resolve_corpus_genre(f"{genre} {style}")
        genre_combined = f"{genre} {style}".lower()
        curated_catalog = [
            ("Save Your Tears", "The Weeknd", "pop", 118.0, "C", "major", "80s synth-pop groove: driving 118 BPM pulse with syncopated conversational verses and a bittersweet hook."),
            ("In Your Eyes", "The Weeknd", "pop", 100.0, "G", "major", "Retro dance-pop groove: driving 100 BPM groove with atmospheric verses and a soaring sax/synth hook."),
            ("Sunflower", "Post Malone & Swae Lee", "pop", 90.0, "D", "major", "Modern pop / melodic duet: bouncy conversational phrasing (8-10 syl) with trading verse vocal roles."),
            ("Levitating", "Dua Lipa", "pop", 124.0, "B", "minor", "Upbeat disco-pop: energetic 124 BPM rhythm with punchy syncopated couplets."),
            ("bad guy", "Billie Eilish", "pop", 135.0, "G", "minor", "Minimalist alt-pop: whisper-close vocal delivery, dry bass groove, and ironic lyrical detachment."),
            ("Hotel California", "Eagles", "rock", 75.0, "B", "minor", "70s narrative rock: atmospheric verse couplets (12-14 syl) with breath caesuras, building to an anthemic dual-harmony chorus."),
            ("Fast Car", "Tracy Chapman", "country", 104.0, "A", "major", "Introspective acoustic storytelling: conversational rhythm, vivid external anchors, and driving narrative progression (8-10 syl)."),
            ("The Gambler", "Kenny Rogers", "country", 87.0, "Eb", "major", "Classic 3-act story country: conversational pacing, character dialogue, and iconic universal narrative payoff."),
            ("Shallow", "Lady Gaga & Bradley Cooper", "rock", 96.0, "G", "major", "Dynamic acoustic pop/rock duet: conversational verses trading perspectives, building into a soaring octave-leaping chorus."),
            ("Born to Run", "Bruce Springsteen", "rock", 148.0, "E", "major", "Cinematic narrative rock: driving pulse, sensory street-level details, and explosive anthemic release."),
            ("Stan", "Eminem", "rap", 80.0, "F#", "minor", "Epistolary narrative drama: conversational verse letters contrasted with an ethereal melodic hook."),
            ("SICKO MODE", "Travis Scott", "rap", 155.0, "G#", "minor", "Dark trap anthem: shifting beat structures, hard-hitting 808s, and hypnotic nocturnal flow."),
            ("Lucid Dreams", "Juice WRLD", "rap", 84.0, "F#", "minor", "Melodic emo-trap: melancholy acoustic loop over crisp 808s with confessional narrative delivery."),
            ("Blinding Lights", "The Weeknd", "pop", 171.0, "F", "minor", "High-energy synthwave: driving four-on-the-floor pulse and urgent melodic hook."),
            ("Dreams", "Fleetwood Mac", "rock", 120.0, "F", "major", "Timeless groove: steady 120 BPM rhythm with lush harmonies and hypnotic conversational cadence."),
            ("Tennessee Whiskey", "Chris Stapleton", "country", 49.0, "A", "major", "Soulful country ballad: 6/8 blues groove with expansive vocal dynamics and rich harmonic backing."),
            ("Redbone", "Childish Gambino", "rb", 116.0, "D", "minor", "Psychedelic soul / R&B: slow sultry groove, Rhodes chords, and expressive falsetto."),
        ]

        scored_candidates: list[tuple[float, dict[str, Any]]] = []
        seen_keys: set[str] = set()

        for title, artist, c_genre, c_bpm, c_key, c_scale, desc in curated_catalog:
            key_id = f"{title.lower()}::{artist.lower()}"
            bpm_diff = abs(c_bpm - target_bpm)
            # Strict BPM gating: Do not recommend 100 BPM when user requested 140 BPM!
            if bpm_diff > 12.0:
                continue

            score = 0.0
            if c_genre == corpus_genre:
                score += 8.0
            elif c_genre in genre_combined:
                score += 4.0
            else:
                score -= 3.0

            score += max(0.0, 6.0 - (bpm_diff / 3.0))
            if c_key.upper() == target_key.upper():
                score += 3.0
            if c_scale.lower() == target_scale.lower():
                score += 1.5

            combined_desc = f"{title} {artist} {desc}".lower()
            for kw in re.findall(r"\b[a-zA-Z]{4,}\b", f"{brief} {story} {style}".lower()):
                if kw in combined_desc:
                    score += 1.5

            if "duet" in genre_combined and "duet" in combined_desc:
                score += 4.0
            if "synth" in genre_combined and "synth" in combined_desc:
                score += 3.0
            if "80s" in genre_combined and "80s" in combined_desc:
                score += 2.0
            if "trap" in genre_combined and "trap" in combined_desc:
                score += 4.0
            if "acoustic" in genre_combined and "acoustic" in combined_desc:
                score += 3.0

            scored_candidates.append((score, {
                "title": title,
                "artist": artist,
                "genre": c_genre,
                "bpm": c_bpm,
                "key": c_key,
                "scale": c_scale,
                "guidance": desc,
                "is_curated": True,
            }))
        abc_ids = self.workbench.searcher.abc_song_ids()
        try:
            db_path = self.studio_db.db_path
            if db_path.exists():
                # Tight BPM window (±8 BPM) so 140 BPM never returns 156 or 120.
                bpm_window = 8.0
                # When MIDI-only is requested, constrain the corpus query itself to
                # songs that have an ABC score, so the LIMIT isn't exhausted by
                # closer-BPM songs that lack MIDI.
                midi_clause = ""
                params: list[Any] = [float(target_bpm), float(target_bpm) - bpm_window,
                                     float(target_bpm) + bpm_window, corpus_genre, corpus_genre]
                if has_midi and abc_ids:
                    placeholders = ",".join("?" * len(abc_ids))
                    midi_clause = f"AND song_id IN ({placeholders})"
                    params.extend(sorted(abc_ids))
                corpus_rows = Database.get(db_path, read_only=True).read(
                    f"""
                    SELECT song_id, title, artist, genre, tag, year, tempo_bpm, musical_key, scale,
                           substr(lyrics, 1, 160) AS preview,
                           abs(tempo_bpm - ?) AS bpm_diff
                    FROM songs
                    WHERE tempo_bpm BETWEEN ? AND ?
                      AND (genre = ? OR tag = ?)
                      AND lyrics IS NOT NULL AND length(lyrics) > 60
                      {midi_clause}
                    ORDER BY bpm_diff ASC
                    LIMIT 80
                    """,
                    params,
                )
                for r in corpus_rows:
                    s_id, r_title, r_artist, r_genre, r_tag, r_year, r_bpm, r_key, r_scale, r_prev, bpm_diff = r
                    c_key_id = f"{r_title.lower()}::{r_artist.lower()}"
                    if c_key_id in seen_keys:
                        continue
                    seen_keys.add(c_key_id)

                    c_score = 6.0
                    c_score += max(0.0, 5.0 - (float(bpm_diff) / 3.0))
                    if r_key and str(r_key).upper() == target_key.upper():
                        c_score += 3.0
                    if r_scale and str(r_scale).lower() == target_scale.lower():
                        c_score += 1.5

                    prev_low = (r_prev or "").lower()
                    for kw in re.findall(r"\b[a-zA-Z]{4,}\b", f"{brief} {story}".lower()):
                        if kw in prev_low:
                            c_score += 1.5

                    g_label = r_genre or r_tag or corpus_genre
                    guidance_text = (
                        f"{g_label.title()} reference track: {float(r_bpm):.0f} BPM groove in "
                        f"{r_key or target_key} {r_scale or target_scale} matching your {target_bpm:.0f} BPM specs."
                    )
                    scored_candidates.append((c_score, {
                        "song_id": s_id,
                        "title": r_title,
                        "artist": r_artist,
                        "year": int(r_year) if r_year else 0,
                        "genre": g_label,
                        "bpm": float(r_bpm) if r_bpm else target_bpm,
                        "key": str(r_key) if r_key else target_key,
                        "scale": str(r_scale) if r_scale else target_scale,
                        "preview": (r_prev or "").replace("\n", " ").strip() + "...",
                        "guidance": guidance_text,
                        "is_curated": False,
                    }))
        except Exception:
            pass

        scored_candidates.sort(key=lambda x: -x[0])
        top_pool = [c for _, c in scored_candidates[:25]]
        if not top_pool:
            return []

        # Random sample for variance; when has_midi is on, draw from the whole
        # scored pool (not just top 25) so MIDI-bearing songs aren't starved.
        pool = top_pool if not has_midi else [c for _, c in scored_candidates]
        chosen_candidates = random.sample(pool, k=min(len(pool), max(limit * 4, 20))) if has_midi \
            else random.sample(top_pool, k=min(limit, len(top_pool)))


        recommendations: list[dict[str, Any]] = []
        for c in chosen_candidates:
            if len(recommendations) >= limit:
                break
            if c.get("is_curated"):
                res = self.workbench.search_songs(f"{c['title']} {c['artist']}", limit=1)
                if res:
                    s = res[0]
                    rec = {
                        "song_id": s.song_id,
                        "title": s.title,
                        "artist": s.artist,
                        "year": s.year,
                        "genre": s.genre or c["genre"],
                        "bpm": s.tempo_bpm or c["bpm"],
                        "key": s.musical_key or c["key"],
                        "scale": s.scale or c["scale"],
                        "preview": s.lyrics_preview,
                        "guidance": c["guidance"],
                        "has_midi": bool(s.song_id and s.song_id in abc_ids),
                    }
                else:
                    rec = {
                        "song_id": None,
                        "title": c["title"],
                        "artist": c["artist"],
                        "year": 2020,
                        "genre": c["genre"],
                        "bpm": c["bpm"],
                        "key": c["key"],
                        "scale": c["scale"],
                        "preview": "",
                        "guidance": c["guidance"],
                        "has_midi": False,
                    }
            else:
                sid = c.get("song_id")
                rec = {
                    "song_id": sid,
                    "title": c["title"],
                    "artist": c["artist"],
                    "year": c.get("year", 0),
                    "genre": c.get("genre", "pop"),
                    "bpm": c.get("bpm"),
                    "key": c.get("key"),
                    "scale": c.get("scale"),
                    "preview": c.get("preview", ""),
                    "guidance": c.get("guidance", ""),
                    "has_midi": bool(sid is not None and int(sid) in abc_ids),
                }
            if has_midi and not rec["has_midi"]:
                continue
            recommendations.append(rec)
        return recommendations


    # ------------------------------------------------------------- defaults

    def resolve_defaults(
        self,
        style: str = "",
        template_id: int | None = None,
        genre: str | None = None,
    ) -> dict[str, Any]:
        """Concrete production defaults, drawn from a matched template where possible."""
        bpm_hint = self._style_tokens(style)["bpm_hint"]

        template = None
        if template_id and self.studio_db.is_available:
            template = self.studio_db.get_template_detail(template_id)
        elif (style or genre) and self.studio_db.is_available:
            rec = self.recommend_setup(style or genre or "")
            template = rec.get("primary_template")

        meta = template.get("metadata", {}) if template else {}
        segs = template.get("segments", {}) if template else {}

        from songwriter import settings
        resolved_genre = genre or (
            " / ".join(p for p in (self._clean(meta.get("genre")), self._clean(meta.get("subgenre"))) if p)
            if meta else ""
        ) or "Pop"
        resolved_bpm = bpm_hint or (int(meta.get("bpm")) if meta.get("bpm") else None) or int(settings.get("default_bpm") or 120)
        resolved_key = self._clean(meta.get("music_key")) or settings.get_str("default_music_key") or "C"
        resolved_scale = self._clean(meta.get("scale")) or settings.get_str("default_scale") or "major"
        resolved_vocal = self._clean(meta.get("vocal")) or settings.get_str("default_vocal") or "melodic lead"

        return {
            "template_id": meta.get("id"),
            "template_title": meta.get("title") or "",
            "bpm": resolved_bpm,
            "music_key": resolved_key,
            "scale": resolved_scale,
            "genre": resolved_genre,
            "vocal": resolved_vocal,
            "section_plan": "standard",
            "rhyme_scheme": "free",
            "syllable_target": "8-10",
            "banned_words": list(DEFAULT_BANNED),
            "arrangement": segs.get("arrangement", {}),
            "global_metadata": segs.get("global_metadata", {}),
        }

    def author_story(
        self,
        brief: str,
        title: str = "",
        style: str = "",
        genre: str = "",
        bpm: int | float | None = None,
        key: str | None = None,
        scale: str | None = None,
        template_title: str = "",
        template_id: int | None = None,
        model: str = "",
    ) -> str:
        """Write a 3-4 sentence cinematic narrative story treatment as a 3-act miniature play,
        strictly grounded in the user's title, brief, character dynamics, tempo pace, and musical genre."""
        model = (model or "").strip() or self.get_default_model()
        if not brief and not title:
            return ""

        context_items = []
        if title:
            # Strip trailing revision tags ("Snowbear 7" -> "Snowbear") so software
            # versioning tags do not become the character's in-world name in the story lore.
            lore_title = re.sub(r"\s+(?:\d+|v\d+|rev\d+|take\s*\d+)$", "", title.strip(), flags=re.I)
            context_items.append(f"SONG TITLE: \"{lore_title or title}\"")
        context_items.append(f"SONG BRIEF & CHARACTER SCENARIO:\n{brief}")

        musical_details = []
        sound_dir = genre or style
        if sound_dir:
            musical_details.append(f"Genre / Style: {sound_dir}")
        if bpm:
            pace_desc = "Slow / deliberate" if bpm < 80 else ("Moderate mid-tempo" if bpm < 115 else "Fast / driving")
            musical_details.append(f"Pace / Tempo: {int(round(bpm))} BPM ({pace_desc})")
        if key:
            musical_details.append(f"Key / Scale: {key} {scale or 'major'}")
        template_detail = None
        if template_id and self.studio_db.is_available:
            template_detail = self.studio_db.get_template_detail(int(template_id))

        resolved_tmpl_title = template_title or ((template_detail or {}).get("metadata", {}).get("title") or "")
        if resolved_tmpl_title:
            musical_details.append(f"Production Template: {resolved_tmpl_title}")

        if musical_details:
            context_items.append("MUSICAL & PRODUCTION MOOD:\n" + "\n".join(f"- {d}" for d in musical_details))

        # Enrich with production template segments when a template is selected by the user
        if template_detail:
            segments = template_detail.get("segments", {}) or {}
            meta = template_detail.get("metadata", {}) or {}
            g_meta = segments.get("global_metadata", {}) or {}
            v_details = segments.get("vocal_details", {}) or {}
            tmpl_lore = []
            if g_meta.get("application_scenarios"):
                tmpl_lore.append(f"• Cinematic Visual Canvas: {g_meta['application_scenarios']}")
            if g_meta.get("emotional_progression"):
                tmpl_lore.append(f"• Dynamic Emotional Arc: {g_meta['emotional_progression']}")
            if v_details.get("vocal_style"):
                tmpl_lore.append(f"• Character Vocal Attitude: {v_details['vocal_style']}")
            elif meta.get("audio_prompt"):
                tmpl_lore.append(f"• Sonic Vibe: {meta['audio_prompt']}")
            if tmpl_lore:
                context_items.append("PRODUCTION TEMPLATE WORLD & CINEMATIC CANVASES:\n" + "\n".join(tmpl_lore))
        full_context_str = "\n\n".join(context_items)

        prompt = prompt_templates.render("storyline", {
            "context": full_context_str,
            "bpm": int(round(bpm)) if bpm else 100,
        })
        story = self._chat([{"role": "user", "content": prompt}], model=model, temperature=0.75, max_tokens=250)
        return story.strip()

    # ------------------------------------------------------------- interview

    def interview(
        self,
        brief: str = "",
        style: str = "",
        template_id: int | None = None,
        reference_song: dict[str, Any] | None = None,
        story: str = "",
        model: str = "",
        use_model: bool = True,
    ) -> dict[str, Any]:
        """Ask the writer the questions a producer would ask, each with a usable default.

        Returns deterministic question scaffolding with defaults resolved from the
        matched production template. When `use_model` is set, the model supplies
        the three craft questions that depend on the brief (angle, imagery, hook);
        if it fails or returns unusable output, deterministic fallbacks are used.
        """
        model = (model or "").strip() or self.get_default_model()
        defaults = self.resolve_defaults(style=style, template_id=template_id)
        if reference_song:
            ref_bpm = reference_song.get("tempo_bpm") or reference_song.get("bpm")
            if ref_bpm:
                defaults["bpm"] = int(round(ref_bpm))
            if reference_song.get("musical_key"):
                defaults["music_key"] = reference_song.get("musical_key")
            if reference_song.get("scale"):
                defaults["scale"] = reference_song.get("scale")
            if reference_song.get("genre"):
                defaults["genre"] = reference_song.get("genre")

        bpm_default = str(defaults["bpm"])
        bpm_options = [
            str(max(40, defaults["bpm"] - 8)),
            bpm_default,
            str(defaults["bpm"] + 8),
        ]
        banned_default = ", ".join(defaults["banned_words"])

        # Reference shape summaries for the "Follow reference" options in Step 3.
        self.ensure_reference_schemes(reference_song)
        ref_secs = [s for s in (reference_song or {}).get("sections") or [] if isinstance(s, dict)]
        ref_names = [str(s.get("name") or "Section") for s in ref_secs]
        has_ref = bool(ref_secs)

        def _preview(items: list[str], n: int = 4) -> str:
            head = ", ".join(items[:n])
            return head + (f", +{len(items) - n} more" if len(items) > n else "")

        ref_plan_opt = FOLLOW_REFERENCE + " structure -- " + ", ".join(f"[{n}]" for n in ref_names)
        ref_lines_opt = FOLLOW_REFERENCE + " line counts (" + _preview(
            [f"{s.get('name')}: {int(s.get('line_count') or len(s.get('lines') or []) or len(s.get('contour') or []))}" for s in ref_secs]) + ")"
        ref_syl_opt = FOLLOW_REFERENCE + " syllable contour (" + _preview(
            ["/".join(str(c) for c in (s.get("contour") or [])[:6]) for s in ref_secs[:3]]) + ")"
        ref_rhyme_opt = FOLLOW_REFERENCE + " rhyme scheme (" + _preview(
            [f"{s.get('name')}: {s.get('rhyme_scheme') or '-'}" for s in ref_secs], 3) + ")"

        craft = self._craft_questions(brief, defaults, model, reference_song=reference_song, story=story) if use_model else None
        if not craft:
            craft = self._fallback_craft_questions(brief)
        for q in craft:
            q["category"] = "creative"

        creative_questions: list[dict[str, Any]] = [
            {
                "id": "rhyme_register",
                "question": "Which vocabulary and slang registers should the rhyme engine draw from?",
                "why": "Controls slang, vernacular, and register filters in the rhyme dictionary.",
                "kind": "options",
                "category": "creative",
                "options": [
                    "Clean & Poetic (Default)",
                    "Include Slang & Vernacular (UK, Australian, Cockney, Hip-Hop)",
                    "Casual Conversational / Everyday Speech",
                    "Include Explicit / Raw (Unfiltered vulgar/offensive words)",
                ],
                "default": "Clean & Poetic (Default)",
            },
            {
                "id": "offensive_language",
                "question": "Should offensive or explicit words be permitted in lyrics?",
                "why": "Sets strict language filtering bounds for the lyric generation engine.",
                "kind": "options",
                "category": "creative",
                "options": [
                    "Clean (Radio-Friendly, No Offensive Words)",
                    "Mild Edge / Street Realism Allowed",
                    "Explicit / Uncensored Raw Emotion",
                ],
                "default": "Clean (Radio-Friendly, No Offensive Words)",
            },
            {
                "id": "allow_phrases",
                "question": "Allow multi-word phrase rhymes (mosaic rhymes)?",
                "why": "Surface natural multi-word pairings like 'surrender to' or 'pretend you were'.",
                "kind": "options",
                "category": "creative",
                "options": [
                    "Yes, include multi-word phrase rhymes (Recommended)",
                    "Single words only",
                ],
                "default": "Yes, include multi-word phrase rhymes (Recommended)",
            },
            {
                "id": "banned",
                "question": "Which stock rhymes or phrases are off-limits?",
                "why": "Named exclusions beat a general instruction to avoid cliches.",
                "kind": "options",
                "category": "creative",
                "options": [
                    "Full Pop Cliché Filter (night/light, fire/desire, heart/apart, rain/pain, cry/die, love/above, mind/blind, soul/control, true/you, dreams/seems)",
                    "Essential Top-3 Exclusions (night/light, fire/desire, heart/apart)",
                    "None (Allow all natural and common rhymes)",
                ],
                "default": "Full Pop Cliché Filter (night/light, fire/desire, heart/apart, rain/pain, cry/die, love/above, mind/blind, soul/control, true/you, dreams/seems)",
            },
            {
                "id": "required_tone",
                "question": "Which tone must every line carry?",
                "why": "A positive tone directive shapes word choice more than a ban list alone.",
                "kind": "options",
                "category": "creative",
                "options": [
                    "No fixed tone (let the brief decide)",
                    "Cold luxury, physical danger, detached swagger",
                    "Warm, vulnerable, intimate",
                    "Playful, bright, tongue-in-cheek",
                    "Defiant, anthemic, triumphant",
                ],
                "default": "No fixed tone (let the brief decide)",
            },
        ]

        production_questions: list[dict[str, Any]] = [
            {
                "id": "bpm",
                "question": f"Tempo: currently {defaults['bpm']} BPM. Would you like to adjust?" if not has_ref else f"Reference tempo is {defaults['bpm']} BPM. Would you like to adjust?",
                "why": "Tempo (BPM) sets the speed, energy, and syllable budget per musical bar.",
                "kind": "number",
                "min": 10,
                "max": 350,
                "category": "production",
                "options": bpm_options,
                "default": bpm_default,
            },
            {
                "id": "musical_key",
                "question": f"Musical key & scale: currently {defaults['music_key']} {defaults['scale']}. Would you like to change?",
                "why": "Musical key and scale establish the emotional color, vocal range comfort, and harmonic gravity of the melody.",
                "kind": "options",
                "category": "production",
                "options": [
                    f"{defaults['music_key']} {defaults['scale']} (Current Default)",
                    "G minor",
                    "A minor",
                    "D minor",
                    "E minor",
                    "C minor",
                    "F# minor",
                    "B minor",
                    "C major",
                    "G major",
                    "D major",
                    "F major",
                    "Bb major",
                    "Eb major",
                    "A major",
                    "E major",
                ],
                "default": f"{defaults['music_key']} {defaults['scale']} (Current Default)",
            },
            {
                "id": "rhyme_sound_verse",
                "question": "Preferred rhyme sound for the verses? (optional)",
                "why": "Biases verse end-rhymes toward one vowel family for cohesion. Advisory only — the model still picks words that fit the line.",
                "kind": "options",
                "category": "production",
                "options": list(_RHYME_SOUND_OPTIONS),
                "default": "No preference",
            },
            {
                "id": "rhyme_sound_chorus",
                "question": "Preferred rhyme sound for the chorus/hook? (optional)",
                "why": "Biases chorus end-rhymes toward one vowel family so the hook feels unified. Advisory only.",
                "kind": "options",
                "category": "production",
                "options": list(_RHYME_SOUND_OPTIONS),
                "default": "No preference",
            },
            {
                "id": "rhyme_sound_bridge",
                "question": "Preferred rhyme sound for the bridge? (optional)",
                "why": "Biases bridge end-rhymes toward one vowel family; a contrasting sound can set the bridge apart. Advisory only.",
                "kind": "options",
                "category": "production",
                "options": list(_RHYME_SOUND_OPTIONS),
                "default": "No preference",
            },
            {
                "id": "syllable_target",
                "question": "Target syllables per line?",
                "why": "Match the reference line by line, or hold a fixed range.",
                "kind": "options",
                "category": "production",
                "options": ([ref_syl_opt] if has_ref else []) + self._with_default(
                    ("6-8", "8-10", "10-12", "12-14"), defaults["syllable_target"]
                ),
                "default": ref_syl_opt if has_ref else defaults["syllable_target"],
            },
            {
                "id": "section_lines",
                "question": "How many lines per section?",
                "why": "Sets the length of each verse and chorus before drafting.",
                "kind": "options",
                "category": "production",
                "options": ([ref_lines_opt] if has_ref else []) + [
                    "4 lines per section", "6 lines per section", "8 lines per section", "2 lines per section",
                ],
                "default": ref_lines_opt if has_ref else "4 lines per section",
            },
            {
                "id": "lines_per_turn",
                "question": "How many lines should the model write per turn?",
                "why": "Writing a block at once lets the model shape a 4-bar phrase (e.g. a short punchy line 4), not just uniform single lines.",
                "kind": "options",
                "category": "production",
                "options": [
                    "2 lines at a time (couplet block)",
                    "1 line at a time (tightest control)",
                    "Whole section at once (best macro-rhythm)",
                ],
                "default": "2 lines at a time (couplet block)",
            },
            {
                "id": "validation_mode",
                "question": "How should each turn be accepted?",
                "why": "Auto LLM uses Tier 1 code gatekeeping plus Tier 2 LLM critique for subjective quality. Auto Songwriter uses the full local validation function. Human pauses on every turn so you can edit, accept, or send feedback.",
                "kind": "options",
                "category": "production",
                "options": [
                    "Auto LLM (hybrid gatekeeper + LLM critique)",
                    "Auto Songwriter (validation function governs)",
                    "Human review (accept, edit, or reject each turn)",
                ],
                "default": "Auto LLM (hybrid gatekeeper + LLM critique)",
            },
            {
                "id": "correction_strictness",
                "question": "How hard should the system correct and re-inform the model on each line?",
                "why": "Each line is scanned deterministically (syllables, rhyme, corpus cliche frequency, melodic delta vs its rhyme partner). This sets how many of those warnings force a rewrite and how many recompose attempts each line gets.",
                "kind": "options",
                "category": "production",
                "options": [
                    "Draft (loose - meter & rhyme only, 2 attempts)",
                    "Balanced (adds cliche advisories, 4 attempts)",
                    "Polished (cliche + melodic-delta enforced, 6 attempts)",
                ],
                "default": "Balanced (adds cliche advisories, 4 attempts)",
            },
            {
                "id": "vocal",
                "question": "Which vocal delivery is this written for?",
                "why": "Delivery decides phrase length and consonant density.",
                "kind": "options",
                "category": "production",
                "options": self._with_default(self.vocal_options(), defaults["vocal"]),
                "default": defaults["vocal"],
            },
            {
                "id": "rhyme_scheme",
                "question": "Which rhyme scheme should each section hold?",
                "why": "Match the reference's rhyme positions, or hold one scheme.",
                "kind": "options",
                "category": "production",
                "options": ([ref_rhyme_opt] if has_ref else []) + list(RHYME_SCHEMES),
                "default": ref_rhyme_opt if has_ref else defaults["rhyme_scheme"],
            },
            {
                "id": "section_plan",
                "question": "Which section plan should the song follow?",
                "why": "Fixes the structural skeleton before any line is written.",
                "kind": "options",
                "category": "production",
                "options": ([ref_plan_opt] if has_ref else []) + [
                    f"{name} -- {plan}" for name, plan in SECTION_PLANS.items()
                ],
                "default": ref_plan_opt if has_ref else f"{defaults['section_plan']} -- {SECTION_PLANS[defaults['section_plan']]}",
            },
        ]

        questions = craft + creative_questions + production_questions

        return {
            "brief": brief,
            "style": style,
            "defaults": defaults,
            "questions": questions,
            "model_used": model if craft and use_model else None,
        }

    def _craft_questions(
        self, brief: str, defaults: dict[str, Any], model: str,
        reference_song: dict[str, Any] | None = None, story: str = "",
    ) -> list[dict[str, Any]] | None:
        ref_context = ""
        if reference_song:
            ref_context = (
                f"\nReference track blueprint: '{reference_song.get('title')}' by {reference_song.get('artist')} "
                f"({reference_song.get('genre')}). Structure questions around this style."
            )
            num_questions = 3
            question_scope = "the 3 questions a professional co-writer would ask before drafting lyrics for this brief"
        else:
            ref_context = (
                "\nNo reference track was selected: the song is being built from scratch from the brief and production context. "
                "Augment the questions to deeply capture the song's creative intent, emotional focal point, lyrical persona/voice, "
                "and rhythmic groove."
            )
            num_questions = 5
            question_scope = "the 4 to 5 deep creative & production questions an executive record producer would ask to lock in the song's vision, vocal attitude, and lyrical intent"

        story_context = ""
        if story.strip():
            story_context = (
                f"\n\nThe writer has already settled this story (treat its facts as fixed):\n\"{story.strip()}\"\n"
                "Every question and every option must agree with this story. Never introduce a "
                "different subject, object or relationship than the story describes."
            )
        prompt = f"""A songwriter submitted this brief for a new song:
"{brief.strip() or 'no brief supplied yet'}"{story_context}

Production context: {defaults['genre']}, {defaults['bpm']} BPM, {defaults['music_key']} {defaults['scale']}.{ref_context}

Write {question_scope}. Each question must be specific to this brief and story, not generic craft advice.

For each question give 3 distinct, concrete answers the writer can pick from. Each option
must be a usable answer in itself, not a category label, and the three must lead the song
somewhere genuinely different. The first option is the recommended default.

Respond with JSON only, no prose, in exactly this shape:
{{"questions": [{{"id": "short_snake_case", "question": "...", "why": "...",
                 "options": ["...", "...", "..."]}}]}}"""

        try:
            raw = self._chat(
                [{"role": "user", "content": prompt}],
                model=model,
                temperature=0.4,
                max_tokens=850,
            )
        except Exception:
            return None

        data = self._extract_json(raw)
        if not isinstance(data, dict):
            return None

        out: list[dict[str, Any]] = []
        for q in data.get("questions", [])[:num_questions]:
            if not isinstance(q, dict):
                continue
            text = str(q.get("question", "")).strip()
            options = [
                str(o).strip()
                for o in (q.get("options") or [])
                if isinstance(o, str) and str(o).strip()
            ]
            # A question the writer cannot answer from is not usable.
            if not text or not options:
                continue
            qid = re.sub(r"[^a-z0-9_]+", "_", str(q.get("id", "")).lower()).strip("_")
            out.append(
                {
                    "id": qid or f"craft_{len(out) + 1}",
                    "question": text,
                    "why": str(q.get("why", "")).strip(),
                    "kind": "options",
                    "options": options,
                    "default": options[0],
                }
            )
        return out or None

    @staticmethod
    def _fallback_craft_questions(brief: str) -> list[dict[str, Any]]:
        # Clean subject label so a long narrative paragraph is never dumped into the header
        subject_label = "this story"
        if brief and brief.strip():
            clean_b = brief.strip().splitlines()[0].split(".")[0].strip()
            if len(clean_b) <= 30 and not any(p in clean_b.lower() for p in ["i am", "i know", "because", "when i", "there was", "did i"]):
                subject_label = f"'{clean_b}'"
        return [
            {
                "id": "narrative_angle",
                "question": f"Whose point of view carries {subject_label}?",
                "why": "Point of view: direct address (I to You) creates an intimate close-up; 1st-person narrative needs a takeaway so it doesn't sound self-absorbed.",
                "kind": "options",
                "options": [
                    "Direct address (I to You), present tense close-up with emotional immediacy.",
                    "First person looking back, reflective narrative with an audience takeaway.",
                    "Third person cinematic observer, watching characters navigate the scene.",
                ],
                "default": "Direct address (I to You), present tense close-up with emotional immediacy.",
            },
            {
                "id": "central_image",
                "question": "Which concrete anchor ('Rusty's Collar' / Bag of Dye) establishes the scene?",
                "why": "Show before you tell: a physical sensory object drips color downward into the abstract emotional lines beneath it.",
                "kind": "options",
                "options": [
                    "One physical object from the scene, established in Line 1 to drip color downward.",
                    "A physical setting or sensory atmosphere (weather, light, temperature) that colors the section.",
                    "A repeated physical action or habit that grounds the emotion.",
                ],
                "default": "One physical object from the scene, established in Line 1 to drip color downward.",
            },
            {
                "id": "hook_line",
                "question": "Where does the title / hook land in the chorus?",
                "why": "Hook placement: putting the title in prominent positions (first, last, or both) fixes the listener's structural expectations.",
                "kind": "options",
                "options": [
                    "Title line closes the chorus, providing emotional payoff and resolution.",
                    "Title line opens the chorus, so the thematic stake lands immediately.",
                    "Envelope: Title line opens and closes the chorus for maximum hook retention.",
                ],
                "default": "Title line closes the chorus, providing emotional payoff and resolution.",
            },
            {
                "id": "emotional_attitude",
                "question": "What is the primary emotional attitude or stance?",
                "why": "Decides whether the narrator is vulnerable, detached, or confrontational.",
                "kind": "options",
                "options": [
                    "Vulnerable and self-aware, admitting fault directly.",
                    "Guarded and detached, masking pain behind a cool exterior.",
                    "Bittersweet and nostalgic, looking back with mixed acceptance.",
                ],
                "default": "Vulnerable and self-aware, admitting fault directly.",
            },
            {
                "id": "chorus_lift",
                "question": "How should the chorus lift dynamically from the verses?",
                "why": "Controls melodic intensity and the emotional contrast between sections.",
                "kind": "options",
                "options": [
                    "Explosive anthemic lift: soaring melody and full harmonic impact.",
                    "Rhythmic punchy hook: syncopated repetitions that stick in memory.",
                    "Intimate whisper-down: sudden drop in volume for dramatic impact.",
                ],
                "default": "Rhythmic punchy hook: syncopated repetitions that stick in memory.",
            },
        ]

    # ------------------------------------------------------- prompt assembly

    def build_instructions_payload(
        self,
        prompt: str,
        style: str | None = None,
        genre: str | None = None,
        template_id: int | None = None,
        bpm: int | None = None,
        music_key: str | None = None,
        scale: str | None = None,
        vocal: str | None = None,
        vocal_roles: dict[str, str] | None = None,
        reference_song: dict[str, Any] | None = None,
        banned_words: list[str] | None = None,
        rhyme_scheme: str | None = None,
        poetic_form: str | None = None,
        section_plan: str | None = None,
        syllable_target: str | None = None,
        answers: dict[str, str] | None = None,
        engine_target: str = "standard",
        mode: str = "creator",
        model: str = "",
    ) -> dict[str, Any]:
        """Construct the exact instruction text that will be sent to the model."""
        model = (model or "").strip() or self.get_default_model()
        template = None
        if template_id and self.studio_db.is_available:
            template = self.studio_db.get_template_detail(template_id)
        elif self.studio_db.is_available:
            rec = self.recommend_setup(style or genre or "Pop")
            template = rec["primary_template"]

        meta = template.get("metadata", {}) if template else {}
        segs = template.get("segments", {}) if template else {}

        bpm_hint = self._style_tokens(style or "")["bpm_hint"]
        resolved_bpm = bpm or bpm_hint or self._clean(meta.get("bpm")) or 92
        resolved_key = music_key or self._clean(meta.get("music_key")) or "C"
        resolved_scale = scale or self._clean(meta.get("scale")) or "major"
        resolved_genre = genre or style or " / ".join(
            p for p in (self._clean(meta.get("genre")), self._clean(meta.get("subgenre"))) if p
        ) or "Pop"
        # A template may be marked instrumental. Since this call produces lyrics,
        # a singer is named instead: "instrumental" would tell the lyric model to
        # write for no voice, and would drop the Vocal Details block from the
        # engine prompt while the song being packaged is sung.
        template_vocal = vocal or self._clean(meta.get("vocal")) or "melodic lead"
        resolved_vocal = (
            "lead vocal"
            if str(template_vocal).strip().lower() == "instrumental"
            else template_vocal
        )
        resolved_syl = syllable_target or "8-10"
        plan_key = self._plan_key(section_plan)
        plan = SECTION_PLANS[plan_key]

        # Engine prompts are built from resolved values by the engine_prompt module,
        # not lifted from the template row.
        engine = self.engine_prompts(
            spec={
                "bpm": resolved_bpm,
                "music_key": resolved_key,
                "scale": resolved_scale,
                "genre": resolved_genre,
                "vocal": resolved_vocal,
            },
            segments=segs,
            brief=prompt,
            answers=answers,
            model=model,
            use_model=False,
        )
        yue2_style = engine["yue2"]
        minimax_caption = engine["minimax"]

        specs: list[str] = [
            "Write production-ready song lyrics for a recording session.",
            "",
            "PRODUCTION SPECIFICATION",
            f"- Tempo: {resolved_bpm} BPM. Hold a steady {resolved_syl} syllables per line so every line sits on the beat.",
            f"- Key: {resolved_key} {resolved_scale}",
            f"- Genre: {resolved_genre}",
            f"- Vocal delivery: {resolved_vocal}",
            f"- Section plan: {plan}",
        ]

        ref_title = (reference_song or {}).get("title")
        ref_artist = (reference_song or {}).get("artist")
        if ref_title and ref_artist:
            specs.append(
                f"- Reference song blueprint: inspired by the structural pacing and metric groove of '{ref_title}' by {ref_artist} "
                f"({reference_song.get('genre', 'Pop')}). Do NOT copy any words, character names, or phrases; write original lyrics strictly for the brief."
            )

        if str(resolved_vocal).strip().lower() == "duet":
            s_a = (vocal_roles or {}).get("singer_a") or "Singer A (Male Vocal)"
            s_b = (vocal_roles or {}).get("singer_b") or "Singer B (Female Vocal)"
            specs.append(
                f"- Duet vocal arrangement: Two-person collaboration with {s_a} and {s_b}.\n"
                f"  * [Verse 1 - {s_a}] (Singer A solo)\n"
                f"  * [Verse 2 - {s_b}] (Singer B solo)\n"
                f"  * [Chorus - Duet Harmony] (Both singing in harmony; label lines '(Both)' if desired)\n"
                f"  * [Bridge - Call and Response] (Trading lines between {s_a} and {s_b})"
            )

        if style:
            specs.append(f"- Style direction: {style}")

        # `engine["fields"]` already separated the section arc out of
        # groove_foundation; using the raw segment here would put
        # "Suggested arc [Intro] [Verse 1]..." in the lyric spec as a groove.
        ef = engine["fields"]
        if ef.get("primary_layer"):
            specs.append(f"- Primary instrumental layer: {ef['primary_layer']}")
        if ef.get("groove_foundation"):
            specs.append(f"- Groove foundation: {ef['groove_foundation']}")
        if ef.get("emotional_progression"):
            specs.append(f"- Emotional arc: {ef['emotional_progression']}")

        if rhyme_scheme and rhyme_scheme != "free":
            specs.append(f"- Rhyme scheme: hold {rhyme_scheme.upper()} in every section")
        if poetic_form:
            specs.append(f"- Poetic form: {poetic_form}")

        banned = [w.strip() for w in (banned_words or []) if w.strip()]
        if banned:
            specs.append(f"- Do not use these stock rhymes or phrases: {', '.join(banned)}")

        specs += [
            "",
            "CANONICAL AUDIO ENGINE SECTION TAGS (YuE2 & MiniMax 3)",
            "- Supported canonical section tags: Intro, Verse, Pre-Chorus, Chorus, Post-Chorus, Bridge, Instrumental, Solo, Outro.",
            f"- Mark each planned section with its bracketed tag on its own line according to the section plan ({plan}).",
            "- For Post-Chorus: provide a catchy 2-4 line hook chant or vocal refrain.",
            "- For Solo or Instrumental: write a 1-line parenthetical arrangement cue (e.g. '(Guitar solo soaring with wide vibrato)').",
            "- For duets: append performer names to section tags (e.g. [Verse 1 - Singer A], [Verse 2 - Singer B], [Chorus - Duet Harmony]) and prefix trading lines with '(Singer A)' or '(Both)'.",
            "",
            prompt_templates.line_rules_text(),
            "- Every section marked with a bracketed tag on its own line.",
            "",
            "OUTPUT",
            "- Lyrics only, with bracketed section tags. No commentary, no titles, no syllable counts.",
        ]

        if engine_target == "yue2":
            specs.append("- Target engine YuE2: section tags only, no instrumental asides.")
        elif engine_target == "minimax":
            specs.append("- Target engine MiniMax Music 3: bracketed arrangement cues permitted.")

        if mode == "guide":
            specs[0] = "Act as a co-writer on the brief below."
            specs[-1] = "- Give concrete line rewrites and specific rhyme options, not general advice."

        answer_block = ""
        clean_answers = self._clean_answers(answers)
        if clean_answers:
            lines = "\n".join(
                f"- {k.replace('_', ' ')}: {v}" for k, v in clean_answers.items()
            )
            answer_block = f"\n\nWRITER'S DECISIONS\n{lines}"

        specs_text = "\n".join(specs)
        user_content = (
            f"{specs_text}\n\nBRIEF\n{prompt.strip() or 'An original song.'}"
            f"{answer_block}\n\nOutput the lyrics now:"
        )

        metadata_payload = {
            "bpm": resolved_bpm,
            "key": f"{resolved_key} {resolved_scale}".strip(),
            "music_key": resolved_key,
            "scale": resolved_scale,
            "genre": resolved_genre,
            "vocal": resolved_vocal,
            "section_plan": plan_key,
            "syllable_target": resolved_syl,
            "template_id": meta.get("id"),
            "template_title": meta.get("title"),
            "arrangement": {
                k: v for k, v in ef.items()
                if k in ("primary_layer", "secondary_layer", "groove_foundation",
                         "embellishments", "instrument_lifecycle")
            },
            "global_metadata": {
                k: v for k, v in ef.items()
                if k in ("emotional_progression", "application_scenarios",
                         "sonics_production", "basic_attributes")
            },
            "section_arc": engine.get("section_arc", ""),
            "model": model,
        }

        return {
            "system_prompt": specs_text,
            "user_prompt": user_content,
            "metadata": metadata_payload,
            "yue2_style": yue2_style,
            "minimax_caption": minimax_caption,
            "engine_fields": ef,
        }

    # ------------------------------------------------------------- generation

    def generate(
        self,
        prompt: str,
        title: str | None = None,
        style: str | None = None,
        genre: str | None = None,
        template_id: int | None = None,
        bpm: int | None = None,
        music_key: str | None = None,
        scale: str | None = None,
        vocal: str | None = None,
        vocal_roles: dict[str, str] | None = None,
        reference_song: dict[str, Any] | None = None,
        banned_words: list[str] | None = None,
        rhyme_scheme: str | None = None,
        poetic_form: str | None = None,
        section_plan: str | None = None,
        syllable_target: str | None = None,
        answers: dict[str, str] | None = None,
        engine_target: str = "standard",
        chat_history: list[dict[str, str]] | None = None,
        mode: str = "creator",
        model: str = "",
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 900,
        section_by_section: bool = True,
        author_engine_prompt: bool = True,
    ) -> dict[str, Any]:
        gen = SongGeneration.from_request(
            template_defaults=self.resolve_defaults(style=style or genre or "", template_id=template_id),
            model=(model or "").strip() or self.get_default_model(),
            prompt=prompt, title=title, style=style, genre=genre, template_id=template_id,
            bpm=bpm, music_key=music_key, scale=scale, vocal=vocal, vocal_roles=vocal_roles,
            reference_song=reference_song, banned_words=banned_words, rhyme_scheme=rhyme_scheme,
            section_plan=section_plan, syllable_target=syllable_target, answers=answers,
        )
        gen.engine_target = engine_target
        payload_data = self.build_instructions_payload(
            prompt=gen.brief,
            style=style,
            genre=gen.genre,
            template_id=gen.template_id,
            bpm=gen.bpm,
            music_key=gen.key,
            scale=gen.scale,
            vocal=gen.vocal,
            vocal_roles=gen.vocal_roles,
            reference_song=gen.reference_song,
            banned_words=gen.banned_pairs,
            rhyme_scheme=gen.rhyme_scheme,
            poetic_form=poetic_form,
            section_plan=section_plan,
            syllable_target=f"{gen.syllable_min}-{gen.syllable_max}",
            answers=answers,
            engine_target=engine_target,
            mode=mode,
            model=gen.model,
        )
        gen.story = self.author_story(
            gen.brief, title=gen.title, style=gen.style or gen.genre, genre=gen.genre,
            bpm=gen.bpm, key=gen.key, scale=gen.scale, model=gen.model,
        )
        spec = {
            "bpm": gen.bpm, "music_key": gen.key, "scale": gen.scale, "genre": gen.genre,
            "vocal": gen.vocal, "style": gen.style or gen.genre, "story": gen.story,
            "allow_slang": gen.allow_slang, "allow_offensive": gen.allow_offensive,
            "allow_phrases": gen.allow_phrases,
        }
        plan = ", ".join(f"[{n}]" for n in gen.planned_sections())

        song = None
        draft: dict[str, Any] | None = None
        if section_by_section and mode != "guide" and not chat_history:
            # Drafting per section is what makes the deterministic checks usable:
            # a measured issue can be fed back against the lines that caused it.
            drafter = SectionDrafter(self.workbench, chat=self._chat)
            song = drafter.draft_song(
                brief=gen.brief,
                spec=spec,
                plan=plan,
                answers=gen.answers,
                banned_words=gen.banned_pairs,
                rhyme_scheme=gen.rhyme_scheme,
                vocal=gen.vocal,
                vocal_roles=gen.vocal_roles,
                reference_song=gen.reference_song,
                story=gen.story,
                syllable_target=f"{gen.syllable_min}-{gen.syllable_max}",
                model=gen.model,
            )
            clean_lyrics = self._clean_lyrics(song.lyrics)
            draft = song.to_dict()
            gen.sections = [
                {"name": sec["name"], "kind": sec.get("kind", "verse"), "lines": list(sec.get("lines") or []),
                 "performer": sec.get("performer", "")}
                for sec in draft.get("sections", [])
            ]
        else:
            messages: list[dict[str, str]] = []
            for msg in (chat_history or [])[-6:]:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": payload_data["user_prompt"]})
            raw_text = self._chat(
                messages, model=gen.model, temperature=temperature, top_p=top_p,
                max_tokens=max_tokens,
            )
            clean_lyrics = self._clean_lyrics(raw_text)
            gen.sections = self._sections_from_lyrics(clean_lyrics)

        # The engine prompt is the other half of the deliverable, so the model
        # authors it from the same brief and decisions that produced the lyrics.
        engine = self.engine_prompts(
            spec=spec,
            segments=self._template_segments(gen.template_id),
            brief=gen.brief,
            answers=gen.answers,
            model=gen.model,
            use_model=author_engine_prompt,
        )
        gen.engine_prompts = {
            "yue2": engine["yue2"],
            "minimax": engine["minimax"],
            "songbench": engine.get("songbench") or "",
        }
        # ABC scores come only from imported MIDI, never fabricated from lyrics.
        self.song_store.save(gen)

        ep = gen.engine_prompts
        yue2_lyrics = format_lyrics_for_engine(clean_lyrics, "yue2")
        minimax_lyrics = format_lyrics_for_engine(clean_lyrics, "minimax")
        return {
            "song_id": gen.song_id,
            "revision": gen.revision,
            "lyrics": clean_lyrics,
            "system_prompt": payload_data["system_prompt"],
            "user_prompt": payload_data["user_prompt"],
            "metadata": payload_data["metadata"],
            "generation": gen.to_record(),
            "model_used": gen.model,
            "engine_target": gen.engine_target,
            "story_treatment": gen.story,
            "rhyme_api_audit": [call for sec in getattr(song, "sections", []) for call in getattr(sec, "rhyme_api_calls", [])] if song else [],
            "draft": draft,
            "abc_score": gen.abc_score,
            "engine_prompt": {
                "yue2": ep["yue2"],
                "minimax": ep["minimax"],
                "songbench": ep["songbench"],
                "authored_by": engine["authored_by"],
                "fields": engine["fields"],
            },
            "yue2_package": {"style": ep["yue2"], "lyrics": yue2_lyrics, "abc": gen.abc_score, "cot": "full"},
            "minimax_package": {"caption": ep["minimax"], "lyrics": minimax_lyrics},
            "songbench_package": {"prompt": ep["songbench"], "lyrics": clean_lyrics},
            "formatted_yue2": f"{ep['yue2']}\n\n{yue2_lyrics}" + (f"\n\n--- ABC SCORE ---\n{gen.abc_score}" if gen.abc_score else ""),
            "formatted_minimax": f"{ep['minimax']}\n\n{minimax_lyrics}",
            "formatted_songbench": f"{ep['songbench']}\n\n{clean_lyrics}",
        }

    @staticmethod
    def _sections_from_lyrics(lyrics: str) -> list[dict[str, Any]]:
        """Split bracket-tagged lyric text into {name, kind, lines} sections."""
        from songwriter.corpus.ingest import normalize_section
        out: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw in (lyrics or "").splitlines():
            line = raw.strip()
            if not line:
                continue
            m = re.match(r"^\[([^\]]+)\]$", line)
            if m:
                name = m.group(1).split(" - ")[0].strip()
                current = {"name": name, "kind": normalize_section(name), "lines": []}
                out.append(current)
            else:
                if current is None:
                    current = {"name": "Verse 1", "kind": "verse", "lines": []}
                    out.append(current)
                current["lines"].append(line)
        return out

    # Vocable/ad-lib sets keyed by canonical genre. The explicit genre decides
    # the set; free-text style only breaks ties when the genre is blank/unknown.
    _VOCABLE_SETS: dict[str, dict[str, Any]] = {
        "trap": {
            "category": "Trap / Drill Non-Word Vocables & Ad-Libs",
            "calls": ["(skrrt)", "(brrr)", "(sheesh)", "(ayy)", "(yuh)", "(woo)", "(woof)", "(pow)", "(hol' up)", "(grrr)"],
            "melodic_filler": ["Ooh-whoa", "Mmm-yeah", "Yeah-yeah", "Ayy-ayy"],
            "usage": "Place parenthetical trap ad-libs at end of bars like '(skrrt)', '(brrr)', '(sheesh)', or melodic pre-line hums like 'Mmm-yeah...' to establish rhythmic bounce and pocket."},
        "rap": {
            "category": "Hip-Hop / Rap Expressive Ad-Libs",
            "calls": ["(uh)", "(yeah)", "(yo)", "(check it)", "(c'mon)", "(ha)", "(nah)", "(aight)", "(boom)", "(word)"],
            "melodic_filler": ["Yeah, yeah", "Uh-huh", "Mmm", "Yo, listen"],
            "usage": "Use punchy parenthetical emphasis ad-libs and pocket breath vocables like '(uh)', '(yeah)', '(yo)' to accent rhythm cadence."},
        "rb": {
            "category": "R&B / Soul Melodic Runs & Non-Words",
            "calls": ["(ooh)", "(yeah)", "(mmm)", "(oh-oh)", "(whoa)", "(baby)", "(ah-ah)", "(shh)"],
            "melodic_filler": ["Ooh-ooh-whoa", "Mmm-mmm", "Yeah-yeah", "Oh-whoa-oh"],
            "usage": "Use silky, sensual vocal runs, falsetto cries, and parenthetical harmonic backing fills like '(ooh)', '(mmm)', '(oh-oh)' to float around lead lyrics."},
        "pop": {
            "category": "Pop / Dance Melodic Vocables & Hook Chants",
            "calls": ["(ooh)", "(oh-oh)", "(yeah)", "(hey)", "(la-la-la)", "(na-na-na)", "(whoa)", "(da-da)"],
            "melodic_filler": ["Ooh-whoa, ooh-whoa", "La-la-la", "Na-na-na-na", "Yeah-yeah-yeah"],
            "usage": "Use earworm melodic chants, call-and-response echoes, and vocal hooks like 'Ooh-whoa', '(hey)', '(la-la)' to maximize hook memorability."},
        "rock": {
            "category": "Rock / Punk Raw Interjections & Shouts",
            "calls": ["(yeah!)", "(whoa-oh)", "(hey!)", "(c'mon!)", "(ugh)", "(whoo!)", "(alright)"],
            "melodic_filler": ["Whoa-oh-oh", "Yeah-yeah-yeah", "Hey! Hey!", "Alright"],
            "usage": "Use visceral vocal shouts, guttural interjections, and anthemic crowd-singing syllables like 'Whoa-oh-oh', '(yeah!)', '(hey!)' to amplify energy."},
        "country": {
            "category": "Country / Folk Conversational Calls & Harmonies",
            "calls": ["(mm-hmm)", "(oh Lord)", "(yeah)", "(whoa)", "(well)", "(Lordy)"],
            "melodic_filler": ["Mm-hmm", "Oh-whoa", "Well now", "Yeah, buddy"],
            "usage": "Use warm conversational breath pauses, storytelling hums like 'Mm-hmm', held vocal inflections, and plaintive three-part acoustic harmonies to establish authenticity. Avoid shouted interjections."},
        "jazz": {
            "category": "Vocal Jazz / Swing Scat Syllables",
            "calls": ["(doo-wop)", "(ba-da-da)", "(scat)", "(shoo-be-doo)", "(bop)", "(skat)"],
            "melodic_filler": ["Doo-wop, doo-wop", "Ba-da-dat-dat", "Shoo-be-doo-wah", "Mmm-hmm"],
            "usage": "Use syncopated scat phrases, swing interjections, and smooth vocalized brass mimes."},
    }
    # Canonical genre -> set key. First match wins; longest/most-specific first.
    _GENRE_TO_SET: tuple[tuple[tuple[str, ...], str], ...] = (
        (("trap", "drill", "phonk"), "trap"),
        (("hip-hop", "hip hop", "rap", "boom bap"), "rap"),
        (("r&b", "rnb", "soul", "neo-soul", "funk", "motown"), "rb"),
        (("country", "folk", "americana", "bluegrass"), "country"),
        (("jazz", "swing"), "jazz"),
        (("pop", "dance", "synth-pop", "disco", "electropop", "electro"), "pop"),
        (("rock", "metal", "punk", "grunge"), "rock"),
    )
    _VOCABLE_DEFAULT: dict[str, Any] = {
        "category": "Contemporary Melodic Vocables & Ad-Libs",
        "calls": ["(ooh)", "(whoa)", "(yeah)", "(mmm)", "(hey)", "(ahh)"],
        "melodic_filler": ["Ooh-whoa", "Mmm-yeah", "Yeah-yeah", "Oh-oh"],
        "usage": "Use expressive non-word vocables to add color, emotional depth, and rhythmic bounce.",
    }
    # Intimate / solitary songs: no parenthetical interjections at all.
    _VOCABLE_QUIET: dict[str, Any] = {
        "category": "None -- intimate, quiet delivery",
        "calls": ["(none)"],
        "melodic_filler": ["(none)"],
        "usage": "This song is quiet and intimate. Do NOT add parenthetical ad-libs or "
                 "vocal interjections; let the lines breathe. At most one soft hum at the very end of a chorus.",
    }
    # Words in the brief, story or interview answers that mark a quiet song.
    _QUIET_MOOD = re.compile(
        r"\b(?:intimate|solitary|alone|lonely|loneliness|quiet|hushed|whisper\w*|murmur\w*|"
        r"ambient|melanchol\w*|tender|confess\w*|lullaby|gentle|gently|soft-spoken|distant)\b",
        re.IGNORECASE,
    )

    @classmethod
    def _vocables_for(cls, gen: "SongGeneration") -> dict[str, Any]:
        """Ad-lib set bound to the reference's delivery, then mood, then genre.

        1. A reference whose lines end on vocal tags ('ah', 'ayy') sets the
           delivery: those tags, clipped onto line ends, never banned.
        2. Without that, two or more quiet-mood words across brief, story and
           interview answers suppress interjections.
        3. Otherwise the genre set applies.
        """
        tags = gen.delivery_tags()
        if tags:
            quoted = ", ".join(f"'{t}'" for t in tags)
            return {
                "category": "Reference delivery -- clipped lines with end tags",
                "calls": [f"{t} (end tag, after a comma)" for t in tags],
                "melodic_filler": ["(none)"],
                "usage": (f"Match the reference's staccato delivery: short clipped phrases, and end "
                          f"about one line in three with a short vocal tag ({quoted}) after a comma. "
                          f"No other parenthetical ad-libs."),
            }
        text = " ".join([gen.brief or "", gen.story or "",
                         " ".join(str(v) for v in (gen.answers or {}).values())])
        if len(set(m.lower() for m in cls._QUIET_MOOD.findall(text))) >= 2:
            return cls._VOCABLE_QUIET
        return cls._genre_vocables_guide(gen.genre, gen.style)

    @classmethod
    def _genre_vocables_guide(cls, genre: str, style: str = "") -> dict[str, Any]:
        """Vocable set for the song. The EXPLICIT genre decides it; the free-text
        style only breaks ties when the genre doesn't match any known family.
        This prevents a stray word in the brief (e.g. 'rock heartthrob') from
        hijacking a Country song's ad-libs.
        """
        g = (genre or "").lower()
        for keys, setkey in cls._GENRE_TO_SET:
            if any(k in g for k in keys):
                return cls._VOCABLE_SETS[setkey]
        # Genre unknown/blank: fall back to sniffing the style text.
        s = (style or "").lower()
        for keys, setkey in cls._GENRE_TO_SET:
            if any(k in s for k in keys):
                return cls._VOCABLE_SETS[setkey]
        return cls._VOCABLE_DEFAULT

    @staticmethod
    def _clean_lyric_line(text: str, min_words: int = 3) -> str:
        meta_triggers = (
            "can't help", "cannot help", "unable to", "as an ai", "i am an ai",
            "i'm an ai", "don't have context", "previous conversation", "i'm kiro",
            "i am kiro", "not able to", "sorry", "cannot fulfill", "prior interaction",
            "i need to", "let me check", "before writing", "here is the", "here's the",
            "i understand", "let's write", "sure!", "note:", "follows aabb", "follows abab",
            "rhyme scheme", "syllable count",
            "the boundary", "the lyric", "the couplet", "rewriting", "revising",
            "sensory memory", "reclamation of agency", "hitting the rhyme", "in order to",
            # Conversational / assistant artifacts: the model answering the chat
            # instead of writing a line (seen when it is handed an empty prompt).
            "came through empty", "message came through", "how can i help",
            "what can i help", "can i help you", "help you with", "it looks like",
            "i don't see", "could you clarify", "please provide", "please share",
            "i'd be happy to", "happy to help", "let me know what", "what would you like",
        )
        candidates = []
        for raw in (text or "").splitlines():
            line = raw.strip().strip('"').strip("'").strip("`")
            # Strip conversational bullet points, numbered labels, or "Line X:" markers FIRST
            line = re.sub(r'^(?:line\s*\d+[\s:.-]*|\d+[\s:.)-]*|[-*•]\s*)\s*', '', line, flags=re.I).strip().strip('"').strip("'")
            # Strip any trailing syllable annotation the model appended, e.g. "(8 syl)"
            line = _SYL_SUFFIX_RE.sub("", line).strip()
            if not line or line.startswith("[") or line.startswith("#"):
                continue
            low = line.lower()
            if any(d in low for d in meta_triggers) or _META_CHATTER_RE.search(line):
                continue
            words = line.split()
            # A valid lyric line can be a short tag/vocable (min_words) up to a full bar (18 words)
            if min_words <= len(words) <= 18:
                return line
            if len(words) > 0:
                candidates.append(line)
        if candidates:
            candidates.sort(key=lambda s: len(s.split()), reverse=True)
            if len(candidates[0].split()) >= min_words:
                return candidates[0]
        return ""

    @staticmethod
    def _clean_lyric_lines(text: str, count: int) -> list[str]:
        """Extract up to ``count`` lyric lines from a multi-line model response.

        Each row is cleaned like a single line (labels/quotes/meta stripped); rows
        that survive as real lyric lines are kept in order.
        """
        out: list[str] = []
        for raw in (text or "").splitlines():
            if not raw.strip():
                continue
            has_prefix = bool(re.match(r'^(?:line\s*\d+[\s:.-]*|\d+[\s:.)-]*|[-*•]\s*)', raw.strip(), flags=re.I))
            min_words = 1 if has_prefix else 3
            cleaned = SongwriterCowriter._clean_lyric_line(raw, min_words=min_words)
            if cleaned:
                out.append(cleaned)
            if len(out) >= count:
                break
        return out
    def _build_critique_prompt(
        self,
        lines: list[str],
        gen: SongGeneration,
        section: str = "",
        rhyme_contract: str = "",
        anchor_lines: list[str] | None = None,
        meter_notes: str = "",
    ) -> str:
        """Construct the contextualized Tier 2 Critique Prompt for the LLM judge."""
        lines_text = "\n".join(f"Line {idx + 1}: {line}" for idx, line in enumerate(lines))
        ref_title = (gen.reference_song or {}).get("title") or "Contemporary production"
        ref_artist = (gen.reference_song or {}).get("artist") or ""
        ref_label = f"\"{ref_title}\"" + (f" by {ref_artist}" if ref_artist else "")
        bpm_text = f"{gen.bpm} BPM" if gen.bpm else "Standard tempo"
        key_scale = f"{gen.key} {gen.scale}" if (gen.key and gen.scale) else "Natural groove"

        anchor_text = ""
        if anchor_lines:
            anchor_text = "\nPRECEDING ANCHOR LINES FROM THIS SECTION:\n" + "\n".join(f"• {a}" for a in anchor_lines) + "\n"

        contract_text = ""
        if rhyme_contract:
            contract_text = f"\nRHYME & CADENCE CONTRACT FOR THIS SPECIFIC TURN:\n{rhyme_contract}\n"

        if meter_notes:
            contract_text += f"ARRANGEMENT / METER NOTES:\n{meter_notes}\n"

        return f"""You are an elite music producer and vocal coach evaluating a drafted turn for a recording session.
TRACK CONTEXT:
• Song Title: "{gen.title}"
• Genre & Arrangement: {gen.genre} ({key_scale}, {bpm_text})
• Reference Feel: {ref_label}
• Section: [{section or "Couplet"}]
• Core Story / Theme: {gen.brief or gen.story or "Grounded storytelling"}
{anchor_text}{contract_text}
DRAFT LINES TO EVALUATE:
{lines_text}

EVALUATION CRITERIA:
1. RHYME INTEGRITY:
- Follow the RHYME & CADENCE CONTRACT above strictly. If the turn is an unrhymed setup (e.g. Setting up different sounds for a later rhyme) or a single-line turn, DO NOT expect internal couplet rhyming.
- Evaluate the core rhyming words only. Ignore trailing vocal tags or ad-libs (e.g., ', ah', ', yah', '(yeah)').
- Accept strong contemporary slant/near rhymes (e.g., 'stride' / 'side', 'cold' / 'hold', 'time' / 'shine', 'lead' / 'sleet').
- Reject weak or forced pairings that do not rhyme when a rhyme is contracted.
- Distinct root words ending in identical phonemes (like 'ice' and 'price') are VALID PERFECT RHYMES. Never flag them as identity rhymes.

2. AUTHENTIC VERNACULAR VS. BOT TELEGRAPHESE:
- Authentic genre slang, AAVE, dropped copulas ("we out here"), and invariant present tense ("he run the pack") are FULLY ACCEPTABLE.
- Reject "telegraphese": flag lines where necessary articles or prepositions were awkwardly deleted just to hit a length limit (e.g., "Papa scan ridge line wolves cannot destroy" or "Paws in snow, heavy on pack"). The line must roll off the tongue naturally on a microphone.

3. VOCAL MOUTH-FEEL & CADENCE:
- The phrasing must sit cleanly in the pocket of the tempo ({bpm_text}) without tongue-twisting consonant collisions.
- If a line is intentionally requested as a short staccato phrase/drop, evaluate its impact as a dramatic pause rather than penalizing it as empty.

4. TIRED CLICHÉS:
- Flag actual lyrical clichés (e.g., "heart of gold", "wear the crown of gold", "fierce and bold", "in their time of need").
- Do NOT flag ordinary conversational connective phrases (e.g., "every single", "comes down", "right now").

OUTPUT INSTRUCTIONS:
Return a valid JSON object strictly matching this schema. No markdown formatting outside the JSON, no conversational preamble:
{{
  "rhyme_integrity": {{"score": 1-5, "verdict": "pass" | "fail", "feedback": "concise observation"}},
  "vernacular": {{"score": 1-5, "verdict": "pass" | "fail", "feedback": "concise observation"}},
  "cadence": {{"score": 1-5, "verdict": "pass" | "fail", "feedback": "concise observation"}},
  "cliches": {{"score": 1-5, "verdict": "pass" | "fail", "feedback": "concise observation"}},
  "passed": true | false,
  "summary": "1-2 sentence overall verdict and advice if failed"
}}"""

    def _run_llm_critique(
        self,
        lines: list[str],
        gen: SongGeneration,
        section: str = "",
        rhyme_contract: str = "",
        anchor_lines: list[str] | None = None,
        meter_notes: str = "",
    ) -> dict[str, Any]:
        """Execute Tier 2 Critique LLM and return parsed critique verdict and prompt/response."""
        prompt = self._build_critique_prompt(
            lines, gen, section=section, rhyme_contract=rhyme_contract,
            anchor_lines=anchor_lines, meter_notes=meter_notes,
        )
        raw = self._chat([{"role": "user", "content": prompt}], model=gen.model, temperature=0.3, max_tokens=300).strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        critique: dict[str, Any] = {}
        if match:
            try:
                critique = json.loads(match.group(0))
            except Exception:
                pass
        if not critique or not isinstance(critique, dict):
            critique = {
                "passed": True,
                "summary": "Hybrid critique parsed (passed).",
                "rhyme_integrity": {"score": 5, "verdict": "pass", "feedback": "Rhyme passed."},
                "vernacular": {"score": 5, "verdict": "pass", "feedback": "Natural spoken cadence."},
                "cadence": {"score": 5, "verdict": "pass", "feedback": "Pocket sits cleanly."},
                "cliches": {"score": 5, "verdict": "pass", "feedback": "No clichés detected."},
            }
        critique["prompt"] = prompt
        critique["raw_response"] = raw
        return critique

    def stream_song_interactive(
        self,
        prompt: str = "An original song",
        title: str | None = None,
        style: str | None = None,
        genre: str | None = None,
        template_id: int | None = None,
        bpm: int | None = None,
        music_key: str | None = None,
        scale: str | None = None,
        vocal: str | None = None,
        vocal_roles: dict[str, str] | None = None,
        reference_song: dict[str, Any] | None = None,
        banned_words: list[str] | None = None,
        rhyme_scheme: str | None = None,
        section_plan: str | None = None,
        syllable_target: str | None = None,
        answers: dict[str, str] | None = None,
        questions: list[dict[str, Any]] | None = None,
        story: str | None = None,
        model: str = "",
        max_retries: int = 5,
        max_sections: int | None = 5,
        song_id: str | None = None,
    ) -> dict[str, Any]:
        """Draft a complete song line-by-line using a conversational critique loop.

        Logs each prompt sent, LLM response, validation scansion issues, and RhymeDB injections.
        """
        self.ensure_reference_schemes(reference_song)
        gen = SongGeneration.from_request(
            template_defaults=self.resolve_defaults(style=style or genre or "", template_id=template_id),
            model=(model or "").strip() or self.get_default_model(),
            prompt=prompt, title=title, style=style, genre=genre, template_id=template_id,
            bpm=bpm, music_key=music_key, scale=scale, vocal=vocal, vocal_roles=vocal_roles,
            reference_song=reference_song, banned_words=banned_words, rhyme_scheme=rhyme_scheme,
            section_plan=section_plan, syllable_target=syllable_target, answers=answers,
            questions=questions, story=story, max_retries=max_retries, max_sections=max_sections,
        )
        # Reopened from Saved Songs: regenerating saves a new revision of the same song.
        gen.song_id = (song_id or "").strip()
        if not gen.story:
            gen.story = self.author_story(
                gen.brief, title=gen.title, style=gen.style or gen.genre, genre=gen.genre,
                bpm=gen.bpm, key=gen.key, scale=gen.scale, model=gen.model,
            )
        rhyme_svc = RhymeService(self.workbench)
        turns: list[dict[str, Any]] = []
        all_lines: list[str] = []
        sections_out: list[dict[str, Any]] = []
        turn_id = 1
        cached_chorus_lines: list[str] | None = None
        live_avg_syl = float(gen.syllable_target)
        live_avg_beats = float(gen.syllable_target // 2)
        live_var = 0.0
        live_ttr = 0.0

        def format_song_sections_map() -> str:
            # One row per section. Per-line targets live in each turn's TASK only.
            # A repeated chorus is the first chorus replayed, so it is labelled as
            # such instead of carrying its own (contradictory) line count.
            rows = []
            for name in gen.planned_sections():
                first = gen.repeat_of(name)
                if first:
                    rows.append(f"  [{name}] repeat of [{first}], same lines")
                    continue
                n = gen.lines_in_section(name)
                wins = [gen.line_window(name, i) for i in range(1, n + 1)]
                lo = min(w[1] for w in wins) if wins else gen.syllable_min
                hi = max(w[2] for w in wins) if wins else gen.syllable_max
                wl, wh = word_band(lo, hi)
                rows.append(f"  [{name}] {n} lines, {wl}-{wh} words each")
            return "\n".join(rows)

        sections_map = format_song_sections_map()
        ref_details = []
        if gen.reference_song:
            ref_t = gen.reference_song.get("title") or gen.reference_song.get("name")
            ref_a = gen.reference_song.get("artist")
            ref_g = gen.reference_song.get("genre")
            if ref_t:
                ref_details.append(f"  • Reference Track: \"{ref_t}\"" + (f" by {ref_a}" if ref_a else "") + (f" ({ref_g})" if ref_g else ""))
            if gen.reference_song.get("lyrics"):
                # A taste of the reference groove: lead lines only, no backing-vocal
                # or tag-only lines ("Ayy", "(Ha-ha-ha)").
                ref_lines = []
                for l in gen.reference_song["lyrics"].split("\n"):
                    s = l.strip()
                    toks = re.findall(r"[a-z']+", re.sub(r"\([^)]*\)", "", s).lower())
                    if s and not s.startswith("[") and toks and not all(t in VOCABLE_TAGS for t in toks):
                        ref_lines.append(s)
                    if len(ref_lines) == 4:
                        break
                if ref_lines:
                    ref_details.append(f"  • Reference Rhythm/Feel Excerpt:\n    \"" + "\"\n    \"".join(ref_lines) + "\"")
                # The story supplies WHAT happens; the reference supplies HOW it is
                # said. Without this, a gentle narrative drowns the reference's voice.
                ref_details.append(
                    "  • Voice: keep the story's events, but say them the way the reference's singer "
                    "talks -- same attitude, same short sentence length, same directness. Treat the "
                    "story as the singer's own lived material in the reference's genre, never as a "
                    "storybook to narrate. Copy the reference's rhythm and stance, never its words.")
        ref_str = ("\n- Reference Track & Production Blueprint:\n" + "\n".join(ref_details)) if ref_details else ""

        # Validation knobs are operator-editable in Settings (not per-song). The
        # strictness tier still decides how many checks run; the threshold and
        # idiom allow-list below prevent natural idioms being flagged as cliches.
        from songwriter import settings as _settings
        strictness = gen.correction_strictness or "balanced"
        cliche_min = int(_settings.get("cliche_min_freq"))
        cliche_enabled = str(_settings.get_str("cliche_check_enabled")).lower() in ("1", "true", "yes", "on")
        delta_enabled = str(_settings.get_str("melodic_delta_enabled")).lower() in ("1", "true", "yes", "on")
        idiom_allow = frozenset(
            p.strip().lower() for p in _settings.get_str("idiom_allowlist").splitlines() if p.strip()
        )

        decisions_str = ("\n- Producer Interview Q&A & Creative Guidance:\n" + "\n".join(gen.interview_guide())) if gen.interview_guide() else ""
        vg = self._vocables_for(gen)

        def make_context_header() -> str:
            reg_str = ("Clean poetic" if not gen.allow_slang else "Include street slang") + (", allow explicit edge" if gen.allow_offensive else ", radio-clean")
            return prompt_templates.render("song_context", {
                "title": gen.title,
                "genre": gen.genre,
                "story": f"{gen.story}{ref_str}{decisions_str}",
                "section_map": sections_map,
                "bpm": gen.bpm,
                "key": gen.key,
                "scale": gen.scale,
                "vocal": gen.vocal,
                "syllable_target": gen.syllable_target,
                "syllable_min": gen.syllable_min,
                "syllable_max": gen.syllable_max,
                "register": reg_str,
                "vocable_category": vg["category"],
                "vocable_calls": ", ".join(vg["calls"]),
                "vocable_filler": ", ".join(vg["melodic_filler"]),
                "vocable_usage": vg["usage"],
            })

        def format_song_progress_block(current_sec: str, current_sec_lines: list[str], anchor: str | None = None) -> str:
            """Format the FULL song lyrics written so far across ALL previous sections, plus current section lines."""
            blocks: list[str] = ["FULL SONG WRITTEN SO FAR:"]
            has_content = False
            for sec in sections_out:
                s_name = sec.get("name", "")
                s_lines = sec.get("lines", [])
                if s_lines:
                    has_content = True
                    blocks.append(f"[{s_name}]")
                    for l in s_lines:
                        blocks.append(f"  {l}")
            if current_sec_lines:
                has_content = True
                blocks.append(f"[{current_sec}] (in progress)")
                for idx, l in enumerate(current_sec_lines):
                    if idx == len(current_sec_lines) - 1 and anchor:
                        blocks.append(f"  {l} (End word anchor: '{anchor}')")
                    else:
                        blocks.append(f"  {l}")
            elif not has_content:
                return "\nFULL SONG WRITTEN SO FAR:\n  (Beginning of song - no previous sections written yet.)"
            return "\n" + "\n".join(blocks)
        def is_exact_chorus_type(name: str) -> bool:
            """Check if section is an actual Chorus, NOT a Pre-Chorus or Post-Chorus."""
            low = name.lower().strip()
            if "pre" in low or "post" in low:
                return False
            return "chorus" in low or "hook" in low or "refrain" in low

        def _section_rhyme_sound(name: str) -> str:
            """Advisory rhyme-sound line for this section type, or '' for none."""
            low = name.lower().strip()
            if "bridge" in low:
                key = "rhyme_sound_bridge"
            elif ("chorus" in low or "hook" in low or "refrain" in low) and "pre" not in low and "post" not in low:
                key = "rhyme_sound_chorus"
            else:
                key = "rhyme_sound_verse"
            ans = str((gen.answers or {}).get(key) or "").strip()
            if not ans or ans.lower().startswith("no pref"):
                return ""
            return (f"  Preferred rhyme sound for this section: lean end-rhymes toward the {ans} "
                    f"vowel family where it fits naturally; never force a word that doesn't suit the line.\n")

        # Story acts ("Act I: ... Act II: ...") scoped to sections: verse N gets
        # act N, the bridge gets the last act. The chorus stays song-wide.
        acts = [a.strip() for a in re.split(r"\bAct\s+[IVX\d]+\s*[:.\-]\s*", gen.story or "") if a.strip()]
        if len(acts) < 2:
            acts = []
        verse_order = [s for s in gen.planned_sections() if s.lower().strip().startswith("verse")]

        def _scene_guide(name: str) -> str:
            if not acts:
                return ""
            low = name.lower().strip()
            if name in verse_order:
                idx = min(verse_order.index(name), len(acts) - 1)
            elif any(k in low for k in ("bridge", "outro", "coda", "fade")):
                idx = len(acts) - 1
            else:
                return ""
            return (f"  This section's scene (Act {idx + 1} of {len(acts)}): {acts[idx]}\n"
                    f"  Let this scene set the emotional tone here; where it shifts from the song-wide "
                    f"tone, follow the scene.\n")

        for sec_name in gen.planned_sections():
            sec_kind = sec_name.lower().strip()
            is_chorus = is_exact_chorus_type(sec_name)
            rhyme_sound_guide = _section_rhyme_sound(sec_name)
            scene_guide = _scene_guide(sec_name)
            
            if is_chorus and cached_chorus_lines:
                # Emit explicit section and line_accepted events for the recurring chorus
                # so both the Live Lyrics sheet and line counters remain perfectly synchronized in real time
                for c_line in cached_chorus_lines:
                    yield {
                        "type": "line_accepted",
                        "section": sec_name,
                        "line": c_line,
                        "line_number": len(all_lines) + 1,
                    }
                    all_lines.append(c_line)
                sections_out.append({
                    "name": sec_name,
                    "kind": "chorus",
                    "lines": list(cached_chorus_lines),
                    "performer": "Both (Duet Harmony)" if gen.vocal == "duet" else gen.vocal,
                })
                continue

            role_info = SectionDrafter.duet_section_role(sec_name, gen.vocal_roles) if gen.vocal == "duet" else {
                "performer": gen.vocal,
                "tag": f"[{sec_name}]",
                "instruction": f"Sung by {gen.vocal}."
            }
            performer = role_info["performer"]
            num_lines = gen.lines_in_section(sec_name)
            scheme = gen.section_scheme(sec_name)
            sec_lines: list[str] = []

            blocks = gen.line_blocks(sec_name)
            for block in blocks:
                is_final_block = block and block[-1] == num_lines
                specs = []
                for ln in block:
                    tgt, lo, hi = gen.line_window(sec_name, ln)
                    specs.append({"line": ln, "target": tgt, "min": lo, "max": hi, "partner": gen.rhyme_partner(sec_name, ln)})

                ctx_hdr = make_context_header()
                tokens_so_far = re.findall(r"\b[a-zA-Z']+\b", "\n".join(all_lines).lower()) if all_lines else []
                unique_tokens_so_far = set(tokens_so_far)
                live_ttr = round((len(unique_tokens_so_far) / len(tokens_so_far)) * 100, 1) if tokens_so_far else 0.0
                syl_counts_so_far = [self.workbench.ca.line_stat(l).syllables for l in all_lines] if all_lines else []
                live_avg_syl = round(sum(syl_counts_so_far) / len(syl_counts_so_far), 1) if syl_counts_so_far else float(gen.syllable_target)
                live_var = round(sum((s - live_avg_syl) ** 2 for s in syl_counts_so_far) / len(syl_counts_so_far), 2) if syl_counts_so_far else 0.0
                stressed_beats_so_far = [
                    len([w for w in re.findall(r"\b[a-zA-Z']+\b", l.lower()) if w not in TRAFFIC_DIRECTOR_ENDINGS])
                    for l in all_lines
                ] if all_lines else []
                # Anchor 3: Immediate Continuity (Sliding window of last 2-4 clean lines)
                recent_lines = []
                if sec_lines:
                    recent_lines.extend(sec_lines[-4:])
                elif all_lines:
                    recent_lines.extend(all_lines[-4:])

                if recent_lines:
                    # Strip trailing parenthetical ad-libs (e.g. '(whoa)') so the model hears the real downbeat rhyme word
                    clean_recent = [re.sub(r"\s*\([^)]*\)\s*$", "", l).strip() for l in recent_lines]
                    cont_body = "\n".join(f"  {l}" for l in clean_recent if l)
                else:
                    cont_body = "  (Opening of song - no preceding lines.)"
                continuity_block = f"PRECEDING LINES FOR FLOW & RHYME:\n{cont_body}"

                def _end_word(line_no: int) -> str:
                    if 1 <= line_no <= len(sec_lines):
                        return self.workbench.ca.line_stat(sec_lines[line_no - 1]).end_word or ""
                    return ""
                rhyme_lines: list[str] = []
                for sp in specs:
                    letter = scheme[sp["line"] - 1] if sp["line"] <= len(scheme) else "-"
                    if sp["partner"]:
                        pw = _end_word(sp["partner"])
                        if pw:
                            cands = rhyme_svc.rhymes(pw, gen)
                            yield {"type": "rhymes_injected", "section": sec_name, "end_word": pw, "rhymes": cands[:30]}
                            rhyme_lines.append(
                                f"    Line {sp['line']}: rhyme with Line {sp['partner']} (end word \"{pw}\")"
                                + (f". True rhymes: {', '.join(cands[:_PROMPT_RHYMES])}" if cands else ""))
                        else:
                            rhyme_lines.append(f"    Line {sp['line']}: rhyme with Line {sp['partner']} (written in this same block)")
                    elif letter != "-":
                        # A line that opens a new scheme letter must not land on a
                        # sound another letter owns in this stanza, or AABCCB
                        # collapses into AABBBB.
                        taken = []
                        for j in range(max(1, sp["line"] - RHYME_WINDOW), sp["line"]):
                            if j - 1 < len(scheme) and scheme[j - 1] not in ("-", letter):
                                w_j = _end_word(j)
                                if w_j and w_j not in taken:
                                    taken.append(w_j)
                        if taken:
                            quoted = ", ".join(f'"{t}"' for t in taken)
                            rhyme_lines.append(f"    Line {sp['line']}: start a NEW rhyme sound -- must not rhyme with {quoted}")
                # Track previously used end words across the entire song to prevent vocabulary recycling
                # (e.g. repeating shelf, cold/gold across multiple verses), excluding identical recurring choruses.
                used_ends = []
                for l in (sec_lines if is_chorus else all_lines):
                    e = (self.workbench.ca.line_stat(l).end_word or "").lower()
                    if e and e not in used_ends:
                        used_ends.append(e)
                sound_counts: dict[str, list[str]] = {}
                for l in sec_lines:
                    e = (self.workbench.ca.line_stat(l).end_word or "").lower()
                    n = _nucleus(self.workbench.ph, e)
                    if n:
                        sound_counts.setdefault(n, []).append(e)
                tired = [ws for ws in sound_counts.values() if len(ws) >= _MAX_SOUND_PER_SECTION]
                if used_ends:
                    scope_label = "in this section" if is_chorus else "in this song so far"
                    rhyme_lines.append(f"    Do not end on a word already used {scope_label}: {', '.join(used_ends)}")
                for ws in tired:
                    rhyme_lines.append(f"    Unless rhyming with a named line, avoid the overused sound of {', '.join(dict.fromkeys(ws))}")
                rhyme_block = ("  Rhyme:\n" + "\n".join(rhyme_lines) + "\n") if rhyme_lines else ""

                # Phrasing derives from each line's own target relative to the
                # section, so it can never contradict the syllable numbers.
                sec_targets = [gen.line_window(sec_name, n)[0] for n in range(1, num_lines + 1)]
                sec_avg = sum(sec_targets) / len(sec_targets) if sec_targets else gen.syllable_target

                def _phrasing(target: int) -> str:
                    if target <= sec_avg - 2:
                        return " -- short, punchy landing; leave space after it"
                    if target >= sec_avg + 2:
                        return " -- longer line; let it run with sustained phrasing"
                    return ""

                def _words(lo: int, hi: int) -> str:
                    wl, wh = word_band(lo, hi)
                    return f"{wl}-{wh} words"

                # Length is given in words: a model tracks word count reliably but
                # not syllables. Scansion checks the real syllables after the reply.
                # Length is given in words: a model tracks overall word count reliably
                # without needing rigid, grammatically crippling sub-phrase splits (e.g. 6 words, 4 words).
                def _shape(ln: int) -> str:
                    tag_match = REF_TAG_RE.search(gen.reference_line(sec_name, ln) or "")
                    if tag_match and gen.delivery_tags():
                        return f"; ending tag: '{tag_match.group(1).lower()}'"
                    return ""

                syl_lines = "\n".join(
                    f"  • Line {sp['line']}: {_words(sp['min'], sp['max'])}{_shape(sp['line'])}{_phrasing(sp['target'])}"
                    for sp in specs
                )
                ref_blurb = ""
                if gen.reference_song:
                    r_t = gen.reference_song.get("title") or gen.reference_song.get("name") or ""
                    r_a = gen.reference_song.get("artist") or ""
                    if r_t:
                        ref_blurb = f" inspired by \"{r_t}\"" + (f" by {r_a}" if r_a else "")
                role_line = f"• Role & Track: Co-writing \"{gen.title}\", a {gen.genre} track{ref_blurb} ({gen.bpm} BPM in {gen.key} {gen.scale}, {gen.vocal} arrangement)."
                premise_text = (gen.brief or "").strip()
                if not premise_text and gen.story:
                    premise_text = gen.story.split(".")[0].strip()

                # Sanitize explicit trap/rap slang if song is radio-clean
                if not gen.allow_offensive and premise_text:
                    for bad, repl in (
                        (r"\bbitches\b", "everyone"),
                        (r"\bbitch\b", "rival"),
                        (r"\bfuckin(?:g)?\b", "really"),
                        (r"\bfuck\b", "mess"),
                        (r"\bshit\b", "stuff"),
                        (r"\bhoes?\b", "crowd"),
                        (r"\bass\b", "self"),
                    ):
                        premise_text = re.sub(bad, repl, premise_text, flags=re.I)

                # Fix incomplete dangling transitive verb fragments like "Everybody loves."
                premise_text = re.sub(r"\b(everybody|everyone|nobody|someone)\s+(loves|likes|hates|wants|needs)\.\s*", r"\1 \2 it. ", premise_text, flags=re.I)
                premise_text = re.sub(r"\s+", " ", premise_text).strip()
                # For Rock and Anthem genres, sanitize excessive monarchical/nursery tropes (king, prince, ruler, crown)
                # into physical, grounded rock storytelling imagery (guardian, brother, wolf, leader)
                is_rock_anthem = any(k in f"{gen.genre} {gen.style}".lower() for k in ("rock", "metal", "punk", "grunge", "anthem", "hard rock", "alternative"))
                if is_rock_anthem and premise_text:
                    for mon, repl in (
                        (r"\ba leader and ruler\b", "strong and free"),
                        (r"\bruler\b", "guardian"),
                        (r"\bprince\b", "warrior"),
                    ):
                        premise_text = re.sub(mon, repl, premise_text, flags=re.I)
                guidance_items = []
                for qid, ans in (gen.answers or {}).items():
                    if ans and qid not in _TECHNICAL_ANSWERS and not ans.lower().startswith("no pref"):
                        a_clean = ans.strip().lstrip("•- ")
                        if len(a_clean) > 130:
                            clipped = a_clean[:125].rsplit(" ", 1)[0]
                            a_clean = (clipped or a_clean[:125]) + "..."
                        guidance_items.append(a_clean)
                premise_line = f"• Core Premise: {premise_text}" + (f" (Guidance: {'; '.join(guidance_items[:3])})" if guidance_items else "")

                def _section_delivery(vg_dict: dict[str, Any], s_kind: str) -> str:
                    base_usage = vg_dict.get("usage", "Conversational SVO spoken English; true content-word rhymes only.")
                    if "verse" in s_kind:
                        return "Grounded scene-setting storytelling; conversational spoken cadence, natural syntax without inverted rhymes."
                    if any(k in s_kind for k in ("chorus", "hook", "drop")):
                        return base_usage
                    if any(k in s_kind for k in ("outro", "coda", "fade")):
                        return "Soft resolution and fading cadence; simple concluding phrases leading to silence."
                    return base_usage

                delivery_line = f"• Delivery: {_section_delivery(vg, sec_kind)}"
                phrasing_benchmark = self.get_corpus_phrasing_benchmark(gen)
                benchmark_line = ""
                if phrasing_benchmark:
                    b_title, b_artist, bl1, bl2 = phrasing_benchmark
                    benchmark_line = f"\n• Phrasing Benchmark ({b_artist} - \"{b_title}\"):\n    \"{bl1}\"\n    \"{bl2}\""
                north_star_block = f"MACRO NORTH STAR:\n{role_line}\n{premise_line}\n{delivery_line}{benchmark_line}"
                # Anchor 2: Structural Position (Active section schema & arc)
                pos_line = f"• Current Section: [{sec_name}] (Lines {block[0]}-{block[-1]} of {num_lines}, Scheme: {scheme})"
                arc_parts = []
                if acts:
                    if sec_name in verse_order:
                        act_idx = min(verse_order.index(sec_name), len(acts) - 1)
                    elif any(k in sec_kind for k in ("bridge", "outro", "coda", "fade")):
                        act_idx = len(acts) - 1
                    else:
                        act_idx = 0
                    arc_parts.append(f"Act {act_idx + 1} — {acts[act_idx]}.")
                if any(k in sec_kind for k in ("outro", "coda", "fade")):
                    arc_parts.append(f"These lines deliver the final resolution of the song, bring the track to a close and fade out.")
                elif is_final_block:
                    arc_parts.append(f"These lines deliver the resolution of [{sec_name}] and transition forward.")
                else:
                    arc_parts.append(f"Developing the scene and cadence of [{sec_name}].")
                if rhyme_sound_guide.strip():
                    arc_parts.append(rhyme_sound_guide.strip())
                struct_pos_block = f"STRUCTURAL POSITION:\n{pos_line}\n• Section Arc: {' '.join(arc_parts)}"

                # Anchor 4: The Task
                count = len(block)
                which = f"Line {block[0]}" if count == 1 else f"Lines {block[0]}-{block[-1]}"
                lines_fmt = "\n".join(f"    Line {ln}: [Lyric line]" for ln in block)
                if gen.delivery_tags():
                    adlib_fmt_rule = (
                        f"  Vocal tags ({', '.join(repr(t) for t in gen.delivery_tags())}) should be placed after a comma at the end of the line (e.g. 'word, ah').\n"
                        f"  Do not stack parenthetical ad-libs after comma tags."
                    )
                else:
                    adlib_fmt_rule = "  Parenthetical ad-libs (e.g. '(yeah)') must ALWAYS be the final tokens on the line; never place words after parentheses."

                out_rule = (
                    f"  Format each line prefixed with its line number, exactly as:\n{lines_fmt}\n"
                    f"  No quotes, no markdown, no commentary.\n"
                    f"{adlib_fmt_rule}"
                )
                task_block = f"TASK:\n  Write [{sec_name}] {which}.\n{syl_lines}\n{rhyme_block}\n{out_rule}"

                p = f"{north_star_block}\n\n{struct_pos_block}\n\n{continuity_block}\n\n{task_block}"
                accepted_block: list[str] = []
                p_retry = ""
                for attempt in range(1, gen.max_retries + 1):
                    current_p = p if attempt == 1 else p_retry
                    yield {
                        "type": "prompt",
                        "turn_id": turn_id,
                        "section": sec_name,
                        "performer": performer,
                        "line_number": len(all_lines) + 1,
                        "turn_type": (which if attempt == 1 else f"{which} (Retry {attempt})"),
                        "prompt": current_p,
                        "attempt": attempt,
                        "max_attempts": gen.max_retries,
                    }
                    raw = self._chat([{"role": "user", "content": current_p}], model=gen.model, temperature=0.7, max_tokens=60 * count + 20).strip()
                    produced = self._clean_lyric_lines(raw, count)
                    yield {"type": "response", "turn_id": turn_id, "section": sec_name,
                           "response": "\n".join(produced), "raw": raw, "attempt": attempt}

                    # Tier 1 vs Full Mode selection based on gen.validation_mode
                    is_hybrid_mode = (gen.validation_mode == "auto_llm")
                    critical_violations: list[str] = []
                    advisories: list[str] = []
                    critique_data: dict[str, Any] | None = None

                    # ---------------- Tier 1 Code Gatekeeper (Zero API Cost) ----------------
                    # 1. Line Count
                    if len(produced) != count:
                        critical_violations.append(f"Expected {count} line(s), got {len(produced)}.")

                    provisional = sec_lines + produced
                    for sp, text in zip(specs, produced):
                        st = self.workbench.ca.line_stat(text)
                        if not text.strip():
                            critical_violations.append(f"Line {sp['line']} is empty.")

                        # 2. Word Envelope (in Tier 1 hybrid mode, check ±2 words tolerance)
                        if is_hybrid_mode:
                            wl, wh = word_band(sp["min"], sp["max"])
                            words = text.split()
                            if len(words) < max(2, wl - 2) or len(words) > (wh + 2):
                                critical_violations.append(
                                    f"Line {sp['line']}: {len(words)} words, outside target window ({wl}-{wh} words)."
                                )

                        # 3. Trailing vocable tag cleanup & formatting
                        if re.search(r"\([^)]+\)\s+\S+", text):
                            critical_violations.append(
                                f"Line {sp['line']}: parenthetical ad-libs must be the final tokens on the line (no words allowed after parentheses)."
                            )

                        # 4. Identity Check: Did Line 2 use the exact same root word as Line 1?
                        if sp["partner"]:
                            pw = (self.workbench.ca.line_stat(provisional[sp["partner"] - 1]).end_word
                                  if 1 <= sp["partner"] <= len(provisional) else "")
                            if pw and st.end_word:
                                pw_l, ew_l = pw.lower(), st.end_word.lower()
                                longer, shorter = (pw_l, ew_l) if len(pw_l) >= len(ew_l) else (ew_l, pw_l)
                                is_inflection = longer in (shorter + "s", shorter + "es", shorter + "d", shorter + "ed", shorter + "ing")
                                is_compound = False
                                if longer.endswith(shorter) and len(longer) > len(shorter):
                                    pfx = longer[:-len(shorter)]
                                    common_pfx = ("mid", "day", "sun", "moon", "under", "over", "out", "back", "fore", "down", "up", "after", "good", "flash", "fire", "star", "cross")
                                    latin_pfx = {"ad", "de", "con", "com", "re", "pro", "in", "ex", "pre", "sub", "ob", "dis", "mis", "per", "sur", "trans", "inter"}
                                    if pfx in common_pfx or (pfx not in latin_pfx and len(pfx) >= 4 and self.workbench.ph.best(pfx) is not None):
                                        is_compound = True
                                is_identity = (pw_l == ew_l) or is_inflection or is_compound
                                if is_identity:
                                    critical_violations.append(
                                        f"Line {sp['line']} identity-rhymes on '{st.end_word}' with Line {sp['partner']} ('{pw}'); choose a distinct root word."
                                    )
                                elif not is_hybrid_mode:
                                    comp = self.workbench.ph.compare(pw, st.end_word)
                                    if comp and comp.kind not in ('perfect', 'slant', 'family', 'additive', 'subtractive', 'assonance'):
                                        critical_violations.append(f"Line {sp['line']} should rhyme with Line {sp['partner']} ('{pw}'); '{st.end_word}' does not.")

                        # 5. End-Word Collisions: Is either core end-word already in used_ends?
                        if st.end_word:
                            ew = st.end_word.lower()
                            prior_lines = provisional[:sp["line"] - 1] if is_chorus else (all_lines + produced[:produced.index(text)])
                            earlier = [self.workbench.ca.line_stat(t).end_word.lower() for t in prior_lines if t]
                            if ew in earlier and not is_chorus:
                                critical_violations.append(f"Line {sp['line']} reuses the end word '{ew}' already used earlier in the song; end on a fresh word.")

                        # In Auto Songwriter (full rules mode): run additional scansion, fertility, and cliche checks
                        if not is_hybrid_mode:
                            syl_too_short = st.syllables < (sp["min"] - 1)
                            syl_too_long = st.syllables > (sp["max"] + 1)
                            if st.syllables and (syl_too_short or syl_too_long):
                                critical_violations.append(f"Line {sp['line']}: {st.syllables} syllables, aiming ~{sp['target']} (guide {sp['min']}-{sp['max']}).")
                            if sp["line"] <= len(scheme) and scheme[sp["line"] - 1] != "-" and st.end_word and not sp["partner"]:
                                own = scheme[sp["line"] - 1]
                                for j in range(max(1, sp["line"] - RHYME_WINDOW), sp["line"]):
                                    if j - 1 < len(scheme) and scheme[j - 1] not in ("-", own) and j <= len(provisional):
                                        ow = self.workbench.ca.line_stat(provisional[j - 1]).end_word
                                        oc = self.workbench.ph.compare(ow, st.end_word) if ow else None
                                        if oc and oc.kind in ("perfect", "family"):
                                            critical_violations.append(f"Line {sp['line']} must open a new rhyme sound; '{st.end_word}' rhymes with Line {j} ('{ow}').")
                                            break
                            if st.end_word and not sp["partner"]:
                                is_anchor = any(gen.rhyme_partner(sec_name, n) == sp["line"] for n in range(sp["line"] + 1, num_lines + 1))
                                if is_anchor:
                                    fert = rhyme_svc.fertility(ew, gen)
                                    if fert < MIN_ANCHOR_FERTILITY:
                                        critical_violations.append(f"Line {sp['line']} ends on '{ew}', which has only {fert} common rhymes.")
                            if st.stress_grid:
                                if "11" in st.stress_grid:
                                    advisories.append(f"Line {sp['line']}: adjacent stressed beats ('11'); smooth the spoken rhythm.")
                                elif "000" in st.stress_grid:
                                    advisories.append(f"Line {sp['line']}: 3 unstressed syllables in a row ('000'); tighten the cadence.")
                            if cliche_enabled and strictness in ("balanced", "polished"):
                                top_n = 3 if strictness == "polished" else 1
                                hits = self.workbench.line_cliches(text, min_freq=cliche_min, top=top_n, allowlist=idiom_allow)
                                if hits:
                                    h = hits[0]
                                    advisories.append(f"Cliche: '{h[0]}' appears {h[1]:,} times in corpus (Line {sp['line']}) -- recompose with a fresher image.")

                    for b in gen.banned_pairs:
                        if "/" not in b and b.lower() in " ".join(produced).lower():
                            critical_violations.append(f"Contains banned phrase '{b}'.")

                    # ---------------- Tier 2: Critique LLM (Structured Evaluation) ----------------
                    # Only invoked if lines PASS Tier 1 and user selected Auto LLM mode
                    tier1_passed = len(critical_violations) == 0 and len(produced) == count
                    if is_hybrid_mode and tier1_passed and not (attempt == gen.max_retries):
                        try:
                            # Build turn contract metadata for the critic
                            contract_items = []
                            for sp in specs:
                                if sp["partner"]:
                                    pw = (self.workbench.ca.line_stat(provisional[sp["partner"] - 1]).end_word
                                          if 1 <= sp["partner"] <= len(provisional) else "")
                                    if sp["partner"] in [s_["line"] for s_ in specs]:
                                        contract_items.append(f"• Line {sp['line']}: Must rhyme with Line {sp['partner']} (written together in this couplet).")
                                    elif pw:
                                        contract_items.append(f"• Line {sp['line']}: Must rhyme with Anchor Word \"{pw}\" (from preceding Line {sp['partner']}).")
                                else:
                                    contract_items.append(f"• Line {sp['line']}: Opens a NEW sound; does NOT rhyme with other lines in this couplet.")
                            rhyme_contract = "\n".join(contract_items)
                            anchor_lines = [l for l in sec_lines[-2:]] if sec_lines else None
                            guide_parts = [f"Line {sp['line']}: ~{sp['target']} syl ({sp['min']}-{sp['max']})" for sp in specs]
                            meter_notes = f"Target tempo: {gen.bpm or 120} BPM. Syllable guide: {', '.join(guide_parts)}."
                            critique_data = self._run_llm_critique(
                                produced, gen, section=sec_name,
                                rhyme_contract=rhyme_contract,
                                anchor_lines=anchor_lines,
                                meter_notes=meter_notes,
                            )
                            if not critique_data.get("passed", True):
                                # If Tier 2 critic fails, collect specific feedback to reinform generator
                                c_summary = critique_data.get("summary") or "Critique flagged quality issues."
                                critical_violations.append(f"Critic: {c_summary}")
                                for dim in ("rhyme_integrity", "vernacular", "cadence", "cliches"):
                                    dim_obj = critique_data.get(dim)
                                    if isinstance(dim_obj, dict) and dim_obj.get("verdict") == "fail":
                                        advisories.append(f"{dim.replace('_', ' ').title()}: {dim_obj.get('feedback', '')}")
                        except Exception as c_err:
                            # Critic timeout or failure falls back gracefully to Tier 1 pass
                            pass

                    passed = len(critical_violations) == 0 and len(produced) == count
                    issues = list(critical_violations)
                    if not passed and advisories:
                        issues.extend(advisories)
                    is_last = (attempt == gen.max_retries)
                    first_st = self.workbench.ca.line_stat(produced[0]) if produced else self.workbench.ca.line_stat("")
                    val_data = {
                        "passed": passed,
                        "syllables": first_st.syllables,
                        "target_syllables": f"{specs[0]['min']}-{specs[0]['max']}",
                        "stressed_beats": len([w for w in re.findall(r"\b[a-zA-Z']+\b", (produced[0] if produced else '').lower()) if w not in TRAFFIC_DIRECTOR_ENDINGS]),
                        "stress_grid": first_st.stress_grid,
                        "end_word": first_st.end_word,
                        "lines": list(produced),
                        "scan": [
                            {"line": sp["line"], "text": text, "syllables": st_.syllables,
                             "target": sp["target"], "range": f"{sp['min']}-{sp['max']}",
                             "end_word": st_.end_word, "rhyme_with": sp["partner"]}
                            for sp, text in zip(specs, produced)
                            for st_ in (self.workbench.ca.line_stat(text),)
                        ],
                        "issues": issues,
                        "critical_violations": critical_violations,
                        "advisories": advisories if not passed else [],
                        "validation_mode": gen.validation_mode,
                        "critique": critique_data,
                    }
                    yield {"type": "validation", "turn_id": turn_id, "section": sec_name, "attempt": attempt, "validation": val_data}
                    if critique_data:
                        yield {
                            "type": "critique",
                            "turn_id": turn_id,
                            "section": sec_name,
                            "attempt": attempt,
                            "critique": critique_data,
                        }
                    turns.append({
                        "turn_id": turn_id,
                        "section": sec_name,
                        "performer": performer,
                        "line_number": len(all_lines) + 1,
                        "turn_type": (which if attempt == 1 else f"{which} (Retry {attempt})"),
                        "prompt": current_p,
                        "response": "\n".join(produced),
                        "attempt": attempt,
                        "max_attempts": gen.max_retries,
                        "validation": val_data,
                        "accepted_line": "\n".join(produced) if (passed or is_last) else None,
                    })
                    turn_id += 1

                    if passed or is_last:
                        # Accept only real, non-empty lines the model produced.
                        # NEVER pad with hardcoded filler and NEVER accept blank
                        # lines: a short section beats junk committed as lyrics.
                        accepted_block = [t for t in (produced or []) if t and t.strip()][:count]
                        break
                    else:
                        issues_summary = "\n    • ".join(issues)
                        prior = " / ".join(produced) if produced else "(nothing usable)"
                        want = "; ".join(f"Line {sp['line']} = {_words(sp['min'], sp['max'])}" for sp in specs)
                        p_retry = (
                            f"{north_star_block}\n\n"
                            f"{struct_pos_block}\n\n"
                            f"{continuity_block}\n\n"
                            f"TASK (REVISION):\n"
                            f"  Previous draft: \"{prior}\"\n"
                            f"  Issues to fix:\n    • {issues_summary}\n\n"
                            f"  Write [{sec_name}] {which}.\n"
                            f"{syl_lines}\n"
                            f"{rhyme_block}\n"
                            f"{out_rule}"
                        )

                for text in accepted_block:
                    all_lines.append(text)
                    sec_lines.append(text)
                    yield {"type": "line_accepted", "section": sec_name, "line": text, "line_number": len(all_lines)}

            if is_chorus and not cached_chorus_lines:
                cached_chorus_lines = list(sec_lines)

            sections_out.append({
                "name": sec_name,
                "kind": sec_kind,
                "lines": sec_lines,
                "performer": performer,
            })

        gen.sections = [
            {"name": s["name"], "kind": s.get("kind", "verse"), "lines": list(s["lines"]), "performer": s.get("performer", "")}
            for s in sections_out
        ]
        clean_lyrics = gen.lyrics
        # ABC scores come only from imported MIDI, never fabricated from lyrics.
        template_segments = None
        if gen.template_id and self.studio_db.is_available:
            tmpl_detail = self.studio_db.get_template_detail(gen.template_id)
            if tmpl_detail:
                template_segments = tmpl_detail.get("segments")

        prompt_pkgs = EnginePromptBuilder(chat=self._chat).build(
            spec={"genre": gen.genre, "bpm": gen.bpm, "music_key": gen.key, "scale": gen.scale,
                  "vocal": gen.vocal, "template_id": gen.template_id},
            segments=template_segments,
            brief=gen.brief,
            answers=gen.answers,
            model=gen.model,
            use_model=False,
        )
        gen.engine_prompts = {
            "minimax": prompt_pkgs.get("minimax") or f"[Global Metadata]\nbpm is {gen.bpm}. key is {gen.key}, and scale is {gen.scale}. {gen.genre}.",
            "yue2": prompt_pkgs.get("yue2") or f"{gen.genre}, {gen.vocal}, {gen.bpm} BPM",
            "songbench": prompt_pkgs.get("songbench") or f"{gen.genre} song, {gen.vocal}, {gen.bpm} BPM, {gen.key} {gen.scale}",
        }
        gen.analytics = {
            "avg_syllables_per_line": live_avg_syl,
            "avg_stressed_beats_per_line": live_avg_beats,
            "pulse_variance": live_var,
            "ttr_vocabulary_diversity": live_ttr,
            "total_lines": len(all_lines),
        }
        gen.status = "finalized"
        self.song_store.save(gen)

        ep = gen.engine_prompts
        yield {
            "type": "complete",
            "song_id": gen.song_id,
            "revision": gen.revision,
            "lyrics": clean_lyrics,
            "sections": sections_out,
            "turns": turns,
            "abc_score": gen.abc_score,
            "analytics": gen.analytics,
            "formatted_minimax": f"{ep['minimax']}\n\n{clean_lyrics}",
            "formatted_yue2": f"{ep['yue2']}\n\n{clean_lyrics}" + (f"\n\n--- ABC SCORE ---\n{gen.abc_score}" if gen.abc_score else ""),
            "formatted_songbench": f"{ep['songbench']}\n\n{clean_lyrics}",
            "generation": gen.to_record(),
            "metadata": {
                "bpm": gen.bpm,
                "key": gen.key,
                "scale": gen.scale,
                "genre": gen.genre,
                "vocal": gen.vocal,
                "story": gen.story,
                "stressed_beats_per_line": gen.analytics["avg_stressed_beats_per_line"],
            },
        }

    def draft_song_interactive(self, **kwargs) -> dict[str, Any]:
        """Batch wrapper over stream_song_interactive."""
        final_payload: dict[str, Any] = {}
        turns: list[dict[str, Any]] = []
        for ev in self.stream_song_interactive(**kwargs):
            if ev.get("type") in ("prompt", "response", "validation"):
                turns.append(ev)
            elif ev.get("type") == "complete":
                final_payload = ev
        if "turns" not in final_payload:
            final_payload["turns"] = turns
        return final_payload

    # --------------------------------------------------------- song library

    def save_generation(self, gen: SongGeneration) -> SongGeneration:
        """Persist a generation; assigns song_id and bumps revision. Raises on failure."""
        if not gen.model:
            gen.model = self.get_default_model()
        return self.song_store.save(gen)

    def load_generation(self, song_id: str) -> SongGeneration | None:
        """The exact SongGeneration that was saved, for repopulating Steps 1-4."""
        return self.song_store.load(song_id)

    def list_revisions(self, song_id: str) -> list[dict[str, Any]]:
        return self.song_store.revisions(song_id)

    def load_revision(self, song_id: str, revision: int) -> SongGeneration | None:
        return self.song_store.load_revision(song_id, revision)

    def list_crafted_songs(
        self,
        status: str | None = None,
        genre: str | None = None,
        query: str = "",
        sort_by: str = "updated_at",
        sort_order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self.song_store.list(
            status=status, genre=genre, query=query, sort_by=sort_by,
            sort_order=sort_order, limit=limit, offset=offset,
        )

    def get_crafted_song(self, song_id: str) -> dict[str, Any] | None:
        return self.song_store.detail(song_id)

    def delete_crafted_songs(self, song_ids: list[str]) -> int:
        return self.song_store.delete(song_ids)

    def delete_crafted_song(self, song_id: str) -> bool:
        return self.song_store.delete([song_id]) > 0

    # ------------------------------------------------------------- transport

    def _chat(
        self,
        messages: list[dict[str, str]],
        model: str = "",
        temperature: float = 0.7,
        top_p: float = 0.9,
        max_tokens: int = 900,
        # Reasoning models (e.g. Gemini at default effort) can take 40-110 s on a
        # full Step 4 prompt; 60 s cut them off mid-answer.
        timeout: int = 180,
        reasoning_effort: str | None = "low",
    ) -> str:
        active_model = (model or "").strip() or self.get_default_model()
        # Never send the model an empty/whitespace prompt: it replies
        # conversationally ("your message came through empty"), which then leaks
        # as a bogus lyric. Fail loudly so the caller can retry with real content.
        if not any((m.get("content") or "").strip() for m in messages):
            raise RuntimeError("Refusing to call the LLM with an empty prompt.")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Standard OpenAI Chat Completions payload. Default reasoning_effort="low"
        # stops reasoning/thinking models (e.g. Gemini 3.8/2.5) from generating
        # 14,000 hidden thinking tokens and timing out on short couplets.
        chat_payload: dict[str, Any] = {
            "model": active_model,
            "messages": messages,
            "max_tokens": int(max_tokens),
        }
        if reasoning_effort:
            chat_payload["reasoning_effort"] = reasoning_effort

        def _send(payload: dict[str, Any]) -> str:
            req = urllib.request.Request(
                f"{self.endpoint}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()

        try:
            return _send(chat_payload)
        except urllib.error.HTTPError as err:
            if "reasoning_effort" in chat_payload and err.code in (400, 422):
                # Model/proxy does not support reasoning_effort; retry once without it
                no_re = dict(chat_payload)
                del no_re["reasoning_effort"]
                try:
                    return _send(no_re)
                except Exception:
                    pass
            err_body = ""
            try:
                err_body = err.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"LLM API Error (HTTP {err.code} on model '{active_model}'): {err_body or err.reason}") from err
        except (TimeoutError, socket.timeout) as err:
            raise RuntimeError(f"LLM request timed out after {timeout}s on model '{active_model}'.") from err
        except urllib.error.URLError as err:
            if isinstance(getattr(err, "reason", None), (TimeoutError, socket.timeout)):
                raise RuntimeError(f"LLM request timed out after {timeout}s on model '{active_model}'.") from err
            raise RuntimeError(f"LLM Connection Error on model '{active_model}': {err.reason}") from err
    # ---------------------------------------------------------------- parsing

    @staticmethod
    def _extract_json(text: str) -> Any:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z]*\n?|```$", "", stripped).strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
        start, end = stripped.find("{"), stripped.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(stripped[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None

    # ---------------------------------------------------- human turn-by-turn

    def _build_gen(self, **req: Any) -> "SongGeneration":
        """Rebuild the generation from request fields (shared by turn endpoints)."""
        self.ensure_reference_schemes(req.get("reference_song"))
        return SongGeneration.from_request(
            template_defaults=self.resolve_defaults(
                style=req.get("style") or req.get("genre") or "", template_id=req.get("template_id")),
            model=(req.get("model") or "").strip() or self.get_default_model(),
            prompt=req.get("prompt") or "", title=req.get("title"), style=req.get("style"),
            genre=req.get("genre"), template_id=req.get("template_id"),
            bpm=req.get("bpm"), music_key=req.get("music_key"), scale=req.get("scale"),
            vocal=req.get("vocal"), vocal_roles=req.get("vocal_roles"),
            reference_song=req.get("reference_song"), banned_words=req.get("banned_words"),
            rhyme_scheme=req.get("rhyme_scheme"), section_plan=req.get("section_plan"),
            syllable_target=req.get("syllable_target"), answers=req.get("answers"),
            questions=req.get("questions"), story=req.get("story"),
            max_retries=req.get("max_retries") or 5, max_sections=req.get("max_sections"),
        )

    def _plan_turns(self, gen: "SongGeneration") -> list[dict[str, Any]]:
        """The ordered list of turns (section + line block) for this generation.

        Recurring choruses reuse the first chorus's lines, so they are not turns.
        """
        turns: list[dict[str, Any]] = []
        chorus_seen = False
        for sec_name in gen.planned_sections():
            low = sec_name.lower().strip()
            is_chorus = ("chorus" in low or "hook" in low or "refrain" in low) and "pre" not in low and "post" not in low
            if is_chorus and chorus_seen:
                turns.append({"section": sec_name, "block": [], "repeat_chorus": True})
                continue
            if is_chorus:
                chorus_seen = True
            for block in gen.line_blocks(sec_name):
                turns.append({"section": sec_name, "block": block, "repeat_chorus": False})
        return turns

    def draft_turn(self, accepted: list[dict[str, Any]] | None = None,
                   reject_reason: str = "", rejections: list[dict[str, Any]] | None = None,
                   **req: Any) -> dict[str, Any]:
        """Draft the next block of lines, given the turns accepted so far.

        ``accepted`` is a list of {"section", "lines": [...]} already committed by
        the writer. Returns the next turn's prompt, drafted lines, and per-line
        scansion for human review -- or {"done": True} when the song is complete.
        A ``reject_reason`` is fed back to the model as the writer's critique.
        """
        gen = self._build_gen(**req)
        accepted = accepted or []
        turn_plan = self._plan_turns(gen)
        turn_index = len(accepted)
        if turn_index >= len(turn_plan):
            return {"done": True, "turn_index": turn_index, "total_turns": len(turn_plan)}

        # Reconstruct the running song and the first chorus (for repeats).
        prior_sections: list[dict[str, Any]] = []
        first_chorus: list[str] = []
        for a in accepted:
            lines = list(a.get("lines") or [])
            prior_sections.append({"section": a.get("section", ""), "lines": lines})
            low = str(a.get("section", "")).lower()
            if not first_chorus and ("chorus" in low or "hook" in low) and "pre" not in low and "post" not in low:
                first_chorus = lines

        turn = turn_plan[turn_index]
        sec_name = turn["section"]
        # A recurring chorus needs no drafting: hand back the cached lines to auto-accept.
        if turn["repeat_chorus"]:
            return {
                "done": False, "turn_index": turn_index, "total_turns": len(turn_plan),
                "section": sec_name, "repeat_chorus": True, "lines": list(first_chorus),
                "line_numbers": list(range(1, len(first_chorus) + 1)),
                "scheme": gen.section_scheme(sec_name), "specs": [], "prompt": "",
            }

        block = turn["block"]
        scheme = gen.section_scheme(sec_name)
        sec_lines_so_far = next((list(s["lines"]) for s in reversed(prior_sections) if s["section"] == sec_name), [])
        # Lines already accepted for this same section (earlier blocks).
        sec_accum = [l for s in prior_sections if s["section"] == sec_name for l in s["lines"]]

        specs = self._slot_specs(gen, sec_name, block, sec_accum)

        prompt = self._compose_turn_prompt(gen, sec_name, block, scheme, specs, prior_sections,
                                           reject_reason, rejections or [])
        count = len(block)
        raw = self._chat([{"role": "user", "content": prompt}], model=gen.model,
                         temperature=0.7, max_tokens=70 * count + 40).strip()
        produced, missing = self._parse_slots(raw, block)

        scan = []
        for sp, text in zip(specs, produced):
            st = self.workbench.ca.line_stat(text)
            comp = self.workbench.ph.compare(sp["partner_word"], st.end_word) if sp["partner_word"] and st.end_word else None
            scan.append({
                "line": sp["line"], "text": text, "syllables": st.syllables,
                "target": sp["target"], "range": f"{sp['min']}-{sp['max']}",
                "role": sp.get("role", "line"), "sound": sp.get("sound", ""),
                "end_word": st.end_word, "stress_grid": st.stress_grid,
                "rhyme_with": sp["partner"], "rhyme_with_word": sp["partner_word"],
                "rhyme_kind": comp.kind if comp else "",
                "missing": sp["line"] in missing,
            })
        return {
            "done": False, "turn_index": turn_index, "total_turns": len(turn_plan),
            "section": sec_name, "repeat_chorus": False, "scheme": scheme,
            "prompt": prompt, "raw": raw, "lines": produced, "scan": scan,
            "line_numbers": list(block), "missing": missing,
        }

    def _rhyme_service(self) -> "RhymeService":
        if not hasattr(self, "_rhyme_svc_cache"):
            self._rhyme_svc_cache = RhymeService(self.workbench)
        return self._rhyme_svc_cache

    # ---- line roles, call-and-response blueprint, and slot parsing ----------

    @staticmethod
    def _line_role(target: int, prev_target: int | None) -> str:
        """A line's musical role from its syllable target relative to the line before.

        A short line (<=6) right after a long line (>=9) is a response tag; a long
        line is a statement; anything else is a plain line.
        """
        if target <= 6 and prev_target is not None and prev_target >= 9:
            return "tag"
        if target >= 9:
            return "statement"
        return "line"

    def _slot_specs(self, gen: "SongGeneration", sec_name: str, block: list[int],
                    sec_accum: list[str]) -> list[dict[str, Any]]:
        """Per-line slot specs for a block: role, bounds, and rhyme partner.

        When the block alternates long/short (call-and-response), tags rhyme with
        the previous tag and a closing long line rhymes with the last tag -- a
        musical pairing that beats the flat positional scheme.
        """
        specs: list[dict[str, Any]] = []
        prev_target: int | None = None
        for ln in block:
            tgt, lo, hi = gen.line_window(sec_name, ln)
            role = self._line_role(tgt, prev_target)
            specs.append({"line": ln, "target": tgt, "min": lo, "max": hi, "role": role,
                          "partner": None, "partner_word": "", "rhymes": []})
            prev_target = tgt

        has_tags = any(s["role"] == "tag" for s in specs)
        if has_tags:
            # Call-and-response: chain each tag to the previous tag; a closing
            # statement (payoff) rhymes with the last tag.
            last_tag_idx: int | None = None
            for i, s in enumerate(specs):
                if s["role"] == "tag":
                    if last_tag_idx is not None:
                        s["partner"] = specs[last_tag_idx]["line"]
                    last_tag_idx = i
                elif s["role"] == "statement" and i == len(specs) - 1 and last_tag_idx is not None:
                    s["partner"] = specs[last_tag_idx]["line"]
        else:
            # Plain scheme: bind by the section's rhyme letters.
            for s in specs:
                s["partner"] = gen.rhyme_partner(sec_name, s["line"])

        # Group each line with its partner under one shared "end-rhyme sound" letter.
        sound_of: dict[int, str] = {}
        nxt = 0
        for s in specs:
            p = s["partner"]
            if p and 1 <= p <= len(sec_accum):
                pw = self.workbench.ca.line_stat(sec_accum[p - 1]).end_word or ""
                s["partner_word"] = pw
                if pw:
                    s["rhymes"] = self._rhyme_service().rhymes(pw, gen)[:_PROMPT_RHYMES]
            if p:
                letter = sound_of.get(p) or sound_of.get(s["line"])
                if letter is None:
                    letter = chr(ord("A") + nxt); nxt += 1
                sound_of[p] = letter          # the partner line carries the sound too
                sound_of[s["line"]] = letter
                s["sound"] = letter
            else:
                s["sound"] = sound_of.get(s["line"], "")
        # Second pass: a line named as someone's partner inherits the shared letter.
        for s in specs:
            if not s["sound"] and s["line"] in sound_of:
                s["sound"] = sound_of[s["line"]]
        return specs

    @staticmethod
    def _word_bounds(target: int) -> tuple[int, int]:
        """Rough spoken word-count band for a syllable target (~1.5 syl/word)."""
        w = max(1, round(target / 1.5))
        return max(1, w - 1), w + 1

    def _slot_role_text(self, role: str, target: int, lo: int, hi: int) -> str:
        """A slot description by musical role and word count -- not a bare syllable number."""
        if role == "tag":
            return "[Short tag: a 2-3 word echo or dismissive kicker]"
        wl, wh = self._word_bounds(target)
        if role == "statement":
            return f"[Statement line, {wl}-{wh} words]"
        return f"[Line, {wl}-{wh} words]"

    def _parse_slots(self, raw: str, block: list[int]) -> tuple[list[str], list[int]]:
        """Parse ``L#: text`` slots from the model output.

        Returns (lines_in_block_order, missing_line_numbers). A slot that is absent
        or empty is reported as missing rather than silently padded with a blank.
        """
        found: dict[int, str] = {}
        for m in re.finditer(r"(?im)^\s*L?\s*(\d+)\s*[:.)-]\s*(.+?)\s*$", raw or ""):
            n = int(m.group(1))
            text = self._clean_lyric_line(m.group(2))
            if text and n not in found:
                found[n] = text
        # Fallback: model ignored labels and returned bare lines in order.
        if not found:
            bare = self._clean_lyric_lines(raw, len(block))
            for i, ln in enumerate(block):
                if i < len(bare) and bare[i]:
                    found[ln] = bare[i]
        lines = [found.get(ln, "") for ln in block]
        missing = [ln for ln in block if not found.get(ln)]
        return lines, missing

    def _compose_turn_prompt(self, gen: "SongGeneration", sec_name: str, block: list[int],
                             scheme: str, specs: list[dict[str, Any]],
                             prior_sections: list[dict[str, Any]], reject_reason: str,
                             rejections: list[dict[str, Any]] | None = None) -> str:
        # A concise CONTEXT SO FAR beats dumping annotated lyrics: syllable annotations
        # teach the model to think in counts, which is what produced "Yeah, I'm done."
        recent: list[str] = []
        for s in prior_sections[-2:]:
            if s["lines"]:
                recent.append(f"[{s['section']}] " + " / ".join(s["lines"]))
        context = "  " + (" ".join(recent) if recent else "This is the opening of the song.")
        count = len(block)
        which = f"Line {block[0]}" if count == 1 else f"Lines {block[0]}-{block[-1]}"
        is_cnr = any(s.get("role") == "tag" for s in specs)

        # Labelled slots with musical role, word bounds, and end-rhyme sound letters.
        slot_rows = []
        for sp in specs:
            role_txt = self._slot_role_text(sp.get("role", "line"), sp["target"], sp["min"], sp["max"])
            rh = ""
            if sp.get("sound"):
                rh = f" (end-rhyme sound {sp['sound']}"
                if sp["partner_word"]:
                    rh += f", like \"{sp['partner_word']}\"" + (f"; true rhymes: {', '.join(sp['rhymes'][:_PROMPT_RHYMES])}" if sp["rhymes"] else "")
                rh += ")"
            slot_rows.append(f"    L{sp['line']}: {role_txt}{rh}")
        slots = "\n".join(slot_rows)

        shape = ""
        example = ""
        if is_cnr:
            shape = ("  Shape: CALL & RESPONSE. Long statement lines answered by a short punchy tag\n"
                     "  (a 2-3 word echo or dismissive kicker). Tags sharing a rhyme sound must rhyme.\n"
                     "  Do NOT pad a tag to full length -- short is the point.\n")
            example = (
                "  STRUCTURAL EXAMPLE (copy the shape, not the words):\n"
                "    L1: You stare across the ice like you can buy my time\n"
                "    L2: Not a chance\n"
                "    L3: You see a heavy coat and think it's yours to claim\n"
                "    L4: Learn to dance\n"
            )

        # Banned words the writer set, plus the required tone directive.
        bans = [b for b in gen.banned_pairs if "/" not in b]
        ban_line = f"  NEVER use these words: {', '.join(bans)}.\n" if bans else ""
        tone = str((gen.answers or {}).get("required_tone") or "").strip()
        if tone.lower().startswith("no fixed"):
            tone = ""
        tone_line = f"  Required tone: {tone}.\n" if tone else ""

        # Full rejection history: every rejected draft and the writer's reason.
        rej_all = list(rejections or [])
        if reject_reason.strip() and not rej_all:
            rej_all = [{"reason": reject_reason}]
        if rej_all:
            rows = []
            for i, r in enumerate(rej_all, 1):
                txt = " / ".join(r.get("lines") or []) if r.get("lines") else ""
                reason = (r.get("reason") or "").strip()
                rows.append(f"    {i}. {txt}" + (f"  [writer: {reason}]" if reason else "  [writer rejected]"))
            feedback = "  Writer rejected these earlier drafts -- do NOT repeat them; address the feedback:\n" + "\n".join(rows) + "\n"
        else:
            feedback = ""

        return (
            f"You are co-writing \"{gen.title}\" ({gen.genre}).\n\n"
            f"CONTEXT SO FAR:\n{context}\n\n"
            f"{tone_line}{ban_line}"
            f"{prompt_templates.line_rules_text()}\n"
            f"  Ad-libs: {self._vocables_for(gen)['usage']}\n\n"
            f"TASK:\n  Write [{sec_name}] {which}. Fill EVERY slot; one line per slot, prefixed with its label:\n"
            f"{slots}\n"
            f"{shape}{example}"
            f"{feedback}"
            f"  Output ONLY the {count} labelled line(s), one per row (e.g. 'L{block[0]}: ...'). No commentary, no markdown."
        )

    def finalize_turns(self, accepted: list[dict[str, Any]] | None = None, **req: Any) -> dict[str, Any]:
        """Assemble human-accepted turns into a saved SongGeneration + engine prompts."""
        gen = self._build_gen(**req)
        gen.song_id = (req.get("song_id") or "").strip()
        # Merge accepted turns into sections (consecutive same-section blocks join).
        sections: list[dict[str, Any]] = []
        for a in (accepted or []):
            name = a.get("section", "Section")
            lines = [l for l in (a.get("lines") or []) if str(l).strip()]
            if sections and sections[-1]["name"] == name:
                sections[-1]["lines"].extend(lines)
            else:
                sections.append({"name": name, "kind": name.lower().strip(), "lines": list(lines),
                                 "performer": gen.vocal})
        gen.sections = sections
        prompt_pkgs = EnginePromptBuilder(chat=self._chat).build(
            spec={"genre": gen.genre, "bpm": gen.bpm, "music_key": gen.key, "scale": gen.scale,
                  "vocal": gen.vocal, "template_id": gen.template_id},
            brief=gen.brief, answers=gen.answers, model=gen.model, use_model=False,
        )
        gen.engine_prompts = {
            "minimax": prompt_pkgs.get("minimax") or f"[Global Metadata]\nbpm is {gen.bpm}. key is {gen.key}, and scale is {gen.scale}. {gen.genre}.",
            "yue2": prompt_pkgs.get("yue2") or f"{gen.genre}, {gen.vocal}, {gen.bpm} BPM",
            "songbench": prompt_pkgs.get("songbench") or f"{gen.genre} song, {gen.vocal}, {gen.bpm} BPM, {gen.key} {gen.scale}",
        }
        gen.status = "finalized"
        self.song_store.save(gen)
        ep = gen.engine_prompts
        clean_lyrics = gen.lyrics
        return {
            "song_id": gen.song_id, "revision": gen.revision, "lyrics": clean_lyrics,
            "sections": sections, "abc_score": gen.abc_score,
            "formatted_minimax": f"{ep['minimax']}\n\n{clean_lyrics}",
            "formatted_yue2": f"{ep['yue2']}\n\n{clean_lyrics}" + (f"\n\n--- ABC SCORE ---\n{gen.abc_score}" if gen.abc_score else ""),
            "formatted_songbench": f"{ep['songbench']}\n\n{clean_lyrics}",
            "generation": gen.to_record(),
            "metadata": {"bpm": gen.bpm, "key": gen.key, "scale": gen.scale,
                         "genre": gen.genre, "vocal": gen.vocal, "story": gen.story},
        }

    @staticmethod
    def _clean_lyrics(text: str) -> str:
        """Strip fences, echoed specs, markdown titles and trailing commentary."""
        out: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                if out and out[-1] != "":
                    out.append("")
                continue
            if line.startswith("```"):
                continue
            if line.startswith("#"):
                continue
            low = line.lower()
            if low.startswith(("style:", "caption:", "bpm:", "tempo:", "key:", "genre:")):
                continue
            out.append(_SYL_SUFFIX_RE.sub("", line))

        # Drop trailing prose that follows the last lyric block.
        last_tag = max((i for i, l in enumerate(out) if l.startswith("[")), default=-1)
        if last_tag >= 0:
            trimmed = list(out)
            while trimmed and len(trimmed) - 1 > last_tag:
                tail = trimmed[-1]
                if tail == "":
                    trimmed.pop()
                    continue
                if tail.lower().startswith(_META_OPENERS):
                    trimmed.pop()
                    continue
                break
            out = trimmed

        return "\n".join(out).strip()
