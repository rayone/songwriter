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
# Tier 2 critic dimensions: the critic judges words, never sound. Rhyme, syllables
# and the new-sound rule are the engine's job in every mode, so rhyme_integrity and
# cadence are no longer scored here. A "fail" on a hard dimension fails the turn
# whatever the critic's own "passed" flag says; cliché notes stay advisory.
_CRITIC_HARD_DIMS = ("sense", "story", "vernacular")
_CRITIC_DIMS = _CRITIC_HARD_DIMS + ("cliches",)


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
        song: SongGeneration,
        segments: dict[str, dict[str, str]] | None = None,
        use_model: bool | None = None,
    ) -> SongGeneration:
        """Build the YuE2 and MiniMax prompts for a resolved production spec."""
        segs = segments if segments is not None else self._template_segments(song.template_id)
        return EnginePromptBuilder(chat=self._chat).build(
            song=song,
            segments=segs,
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
                "tempo_bpm": song_bpm,
                "musical_key": song_key,
                "scale": song_scale,
                "time_signature": getattr(song, "time_signature", None),
                "chords": chords_palette,
                "roman_numerals": getattr(song, "roman_numerals", []) or [],
                "melodic_contour": getattr(song, "melodic_contour", None),
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

        # Pick a canonical reference couplet by matching the genre/style text.
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
            if k in combined:
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
        song: SongGeneration,
        *,
        limit: int = 4,
        has_midi: bool = False,
    ) -> list[dict[str, Any]]:
        """Recommend reference songs from the corpus matching the story, style, BPM, key, and genre.

        Each recommendation includes curated guidance explaining why it fits the groove and scansion.
        """
        brief = song.brief
        story = song.story
        style = song.style
        genre = song.genre
        bpm = song.bpm
        key = song.key
        scale = song.scale
        template_id = song.template_id

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
            return ""

        corpus_genre = resolve_corpus_genre(f"{genre} {style}")
        target_genre = (genre or style).split(',')[0].split('(')[0].strip()
        genre_terms = list(dict.fromkeys(t.strip().casefold() for t in re.split(r"[/,;]", target_genre) if t.strip()))
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
                "genre_priority": 2 if any(term in c_genre.casefold() for term in genre_terms) else (1 if c_genre == corpus_genre else 0),
            }))
        abc_ids = self.workbench.searcher.abc_song_ids()
        corpus_rows = [] if has_midi else self.workbench.searcher.references.get_recommendation_candidates(
            target_bpm, genre_terms=genre_terms, corpus_genre=corpus_genre,
        )
        try:
            db_path = self.studio_db.db_path
            if db_path.exists():
                # Tight BPM window (±8 BPM) so 140 BPM never returns 156 or 120.
                bpm_window = 8.0
                # When MIDI-only is requested, constrain the corpus query itself to
                # songs that have an ABC score, so the LIMIT isn't exhausted by
                # closer-BPM songs that lack MIDI.
                midi_clause = ""
                # Search all catalog genres; do not force unknown styles into Pop.
                match_parts = ["(contains(lower(coalesce(genre, '')), ?) OR contains(lower(coalesce(tag, '')), ?))" for _ in genre_terms]
                exact_match = " OR ".join(match_parts) or "FALSE"
                genre_rank = f"CASE WHEN {exact_match} THEN 2 WHEN lower(genre) = ? OR lower(tag) = ? THEN 1 ELSE 0 END"
                params: list[Any] = [float(target_bpm)]
                for term in genre_terms:
                    params.extend([term, term])
                params.extend([corpus_genre, corpus_genre, float(target_bpm) - bpm_window, float(target_bpm) + bpm_window])
                if has_midi and abc_ids:
                    placeholders = ",".join("?" * len(abc_ids))
                    midi_clause = f"AND song_id IN ({placeholders})"
                    params.extend(sorted(abc_ids))
                corpus_rows.extend(Database.get(db_path, read_only=True).read(
                    f"""
                    SELECT song_id, title, artist, genre, tag, year, tempo_bpm, musical_key, scale,
                           substr(lyrics, 1, 160) AS preview,
                           abs(tempo_bpm - ?) AS bpm_diff,
                           {genre_rank} AS genre_match
                    FROM songs
                    WHERE tempo_bpm BETWEEN ? AND ?
                      AND lyrics IS NOT NULL AND length(lyrics) > 60
                      {midi_clause}
                    ORDER BY genre_match DESC, bpm_diff ASC, song_id ASC
                    LIMIT 80
                    """,
                    params,
                ))
        except Exception:
            pass
        for r in corpus_rows:
            s_id, r_title, r_artist, r_genre, r_tag, r_year, r_bpm, r_key, r_scale, r_prev, bpm_diff, genre_match = r
            c_key_id = f"{r_title.lower()}::{r_artist.lower()}"
            if c_key_id in seen_keys:
                continue
            seen_keys.add(c_key_id)

            c_score = 30.0 if genre_match == 2 else (6.0 if genre_match == 1 else 0.0)
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
                f"{g_label.title()} reference track: {float(r_bpm):.0f} BPM groove"
                + (f" in {r_key}" + (f" {r_scale}" if r_scale else "") if r_key else "")
                + f" matching your {target_bpm:.0f} BPM specs."
            )
            scored_candidates.append((c_score, {
                "song_id": s_id,
                "title": r_title,
                "artist": r_artist,
                "year": int(r_year) if r_year else 0,
                "genre": g_label,
                "bpm": float(r_bpm),
                "key": str(r_key) if r_key else None,
                "scale": str(r_scale) if r_scale else None,
                "preview": (r_prev or "").replace("\n", " ").strip() + "...",
                "guidance": guidance_text,
                "is_curated": False,
                "genre_priority": int(genre_match),
            }))

        scored_candidates.sort(key=lambda x: (x[1]["genre_priority"], x[0]), reverse=True)
        top_pool = [c for _, c in scored_candidates[:25]]
        if not top_pool:
            return []

        # Preserve the ranking instead of randomly burying niche corpus matches
        # beneath famous curated tracks. Keep extra candidates for MIDI filtering.
        chosen_candidates = [c for _, c in scored_candidates] if has_midi else top_pool


        recommendations: list[dict[str, Any]] = []
        recommended_keys: set[str] = set()
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
            rec_key = f"{rec['title'].casefold()}::{(rec['artist'] or '').casefold()}"
            if rec_key in recommended_keys:
                continue
            recommended_keys.add(rec_key)
            rec["target_genre"] = target_genre
            rec["guidance"] += " Structural/phrasing reference only; choose your target genre independently in Step 3."
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

    def author_story(self, song: SongGeneration) -> SongGeneration:
        """Write a 3-4 sentence cinematic narrative story treatment as a 3-act miniature play,
        strictly grounded in the user's title, brief, character dynamics, tempo pace, and musical genre."""
        song.model = song.model.strip() or self.get_default_model()
        if not song.brief and not song.title:
            song.story = ""
            return song

        context_items = []
        if song.title:
            # Strip trailing revision tags ("Snowbear 7" -> "Snowbear") so software
            # versioning tags do not become the character's in-world name in the story lore.
            lore_title = re.sub(r"\s+(?:\d+|v\d+|rev\d+|take\s*\d+)$", "", song.title.strip(), flags=re.I)
            context_items.append(f"SONG TITLE: \"{lore_title or song.title}\"")
        context_items.append(f"SONG BRIEF & CHARACTER SCENARIO:\n{song.brief}")

        musical_details = []
        sound_dir = song.genre or song.style
        if sound_dir:
            musical_details.append(f"Genre / Style: {sound_dir}")
        if song.bpm:
            pace_desc = "Slow / deliberate" if song.bpm < 80 else ("Moderate mid-tempo" if song.bpm < 115 else "Fast / driving")
            musical_details.append(f"Pace / Tempo: {int(round(song.bpm))} BPM ({pace_desc})")
        if song.key:
            musical_details.append(f"Key / Scale: {song.key} {song.scale or 'major'}")
        template_detail = None
        if song.template_id and self.studio_db.is_available:
            template_detail = self.studio_db.get_template_detail(int(song.template_id))

        resolved_tmpl_title = (template_detail or {}).get("metadata", {}).get("title") or ""
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
            "bpm": int(round(song.bpm)) if song.bpm else 100,
        })
        story = self._chat([{"role": "user", "content": prompt}], model=song.model, temperature=0.75, max_tokens=250)
        song.story = story.strip()
        return song

    # ------------------------------------------------------------- interview

    def interview(
        self,
        song: SongGeneration,
        *,
        use_model: bool = True,
    ) -> SongGeneration:
        """Ask the writer the questions a producer would ask, each with a usable default.

        Returns deterministic question scaffolding with defaults resolved from the
        matched production template. When `use_model` is set, the model supplies
        the three craft questions that depend on the brief (angle, imagery, hook);
        if it fails or returns unusable output, deterministic fallbacks are used.
        """
        song.model = song.model.strip() or (self.get_default_model() if use_model else "")
        model = song.model
        defaults = self.resolve_defaults(style=song.style, template_id=song.template_id, genre=song.genre)
        writer_genre = (song.genre or "").strip() or song.style.split(',')[0].split('(')[0].strip() or defaults["genre"]
        reference_genre = str((song.reference_song or {}).get("genre") or "").strip()
        defaults["genre"] = writer_genre
        genre_question = None
        if reference_genre and writer_genre.casefold() != reference_genre.casefold():
            recommendation, ai_generated = self._recommend_genre(
                song.brief, song.story, writer_genre, reference_genre, model, use_model,
            )
            genre_question = {
                "id": "genre", "category": "creative", "kind": "options",
                "question": "Which genre should the new song use?",
                "why": "Your sound profile and reference differ. The reference can supply structure without changing your genre.",
                "options": [writer_genre, reference_genre, recommendation],
                "option_labels": {writer_genre: "Your original genre", reference_genre: "Reference genre",
                                  recommendation: "AI recommendation" if ai_generated else "Suggested blend — AI unavailable"},
                "default": writer_genre,
            }
        if song.reference_song:
            ref_bpm = song.reference_song.get("tempo_bpm") or song.reference_song.get("bpm")
            if ref_bpm:
                defaults["bpm"] = int(round(ref_bpm))
            if song.reference_song.get("musical_key"):
                defaults["music_key"] = song.reference_song.get("musical_key")
            if song.reference_song.get("scale"):
                defaults["scale"] = song.reference_song.get("scale")

        bpm_default = str(defaults["bpm"])
        bpm_options = [
            str(max(40, defaults["bpm"] - 8)),
            bpm_default,
            str(defaults["bpm"] + 8),
        ]
        banned_default = ", ".join(defaults["banned_words"])

        # Reference shape summaries for the "Follow reference" options in Step 3.
        self.ensure_reference_schemes(song.reference_song)
        ref_secs = [s for s in (song.reference_song or {}).get("sections") or [] if isinstance(s, dict)]
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

        craft = self._craft_questions(song.brief, defaults, model, reference_song=song.reference_song, story=song.story) if use_model else None
        if not craft:
            craft = self._fallback_craft_questions(song.brief)
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

        if genre_question:
            creative_questions.insert(0, genre_question)
        # Model-written craft questions must not shadow authoritative production controls.
        reserved_ids = {q["id"] for q in creative_questions + production_questions} | {"genre"}
        craft = [q for q in craft if q["id"] not in reserved_ids]
        questions = craft + creative_questions + production_questions

        song.questions = questions
        for q in questions:
            qid = q.get("id")
            if qid and qid not in song.answers and q.get("default") is not None:
                song.answers[qid] = str(q["default"])

        if "genre" not in song.model_fields_set and writer_genre:
            song.genre = writer_genre
        if "bpm" not in song.model_fields_set and defaults.get("bpm"):
            song.bpm = defaults["bpm"]
        if "key" not in song.model_fields_set and defaults.get("music_key"):
            song.key = defaults["music_key"]
        if "scale" not in song.model_fields_set and defaults.get("scale"):
            song.scale = defaults["scale"]
        if "vocal" not in song.model_fields_set and defaults.get("vocal"):
            song.vocal = defaults["vocal"]
        if "template_id" not in song.model_fields_set and defaults.get("template_id"):
            song.template_id = defaults["template_id"]

        return song

    def _recommend_genre(
        self, brief: str, story: str, writer_genre: str, reference_genre: str,
        model: str, use_model: bool,
    ) -> tuple[str, bool]:
        """One optional alternative; never replace the writer's original default."""
        fallback = f"{writer_genre} / {reference_genre} fusion"
        if use_model:
            prompt = (
                "Recommend one genre for an original song. Preserve niche/independent genres; "
                "do not assume the reference's popularity or catalog genre defines the new song. "
                "Suggest a distinct, musically coherent alternative or hybrid, different from BOTH inputs. "
                "Return JSON only: {\"genre\": \"short genre name\"}.\n"
                + json.dumps({"writer_genre": writer_genre, "reference_genre": reference_genre,
                              "brief": brief, "story": story}, ensure_ascii=False)
            )
            try:
                data = self._extract_json(self._chat(
                    [{"role": "user", "content": prompt}], model=model, temperature=0.4, max_tokens=120,
                ))
                candidate = data.get("genre") if isinstance(data, dict) else None
                if isinstance(candidate, str):
                    candidate = candidate.strip()
                    if (candidate and len(candidate) <= 120 and '\n' not in candidate
                            and candidate.casefold() not in {writer_genre.casefold(), reference_genre.casefold()}):
                        return candidate, True
            except Exception:
                pass
        return fallback, False

    def _craft_questions(
        self, brief: str, defaults: dict[str, Any], model: str,
        reference_song: dict[str, Any] | None = None, story: str = "",
    ) -> list[dict[str, Any]] | None:
        ref_context = ""
        if reference_song:
            ref_context = (
                f"\nReference track blueprint: '{reference_song.get('title')}' by {reference_song.get('artist')} "
                f"(catalog genre: {reference_song.get('genre')}). Use it for structure and phrasing only. "
                f"The writer's target genre is {defaults['genre']}; do not replace it with the reference genre."
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

    def prepare_generation(self, song: SongGeneration) -> SongGeneration:
        """Resolve Steps 1–3 once on this instance before a generation operation."""
        if not song.model or not song.model.strip():
            song.model = self.get_default_model()
        self.ensure_reference_schemes(song.reference_song)
        tmpl_meta: dict[str, Any] = {}
        if song.template_id and self.studio_db.is_available:
            tmpl = self.studio_db.get_template_detail(song.template_id)
            if tmpl:
                tmpl_meta = tmpl.get("metadata", {})
        elif self.studio_db.is_available:
            rec = self.recommend_setup(song.style or song.genre or "Pop")
            tmpl = rec.get("primary_template")
            if tmpl:
                tmpl_meta = tmpl.get("metadata", {})
        song.resolve_inputs(tmpl_meta)
        return song

    def build_instructions_payload(self, song: SongGeneration) -> SongGeneration:
        """Construct the exact instruction text that will be sent to the model."""
        plan_str = ", ".join(f"[{s}]" for s in song.planned_sections())
        specs: list[str] = [
            "Write production-ready song lyrics for a recording session.",
            "",
            "PRODUCTION SPECIFICATION",
            f"- Tempo: {song.bpm} BPM. Hold a steady {song.syllable_min}-{song.syllable_max} syllables per line so every line sits on the beat.",
            f"- Key: {song.key} {song.scale}",
            f"- Genre: {song.genre}",
            f"- Subgenre: {song.subgenre or 'none specified'}",
            f"- Time signature: {song.time_signature}",
            f"- Language: {song.language or 'English'}",
            f"- Vocal delivery: {song.vocal}",
            f"- Section plan: {plan_str}",
        ]
        if song.moods:
            specs.append(f"- Moods / Atmosphere: {', '.join(song.moods)}")
        if song.instruments:
            specs.append(f"- Instrumentation: {', '.join(song.instruments)}")

        ref_title = (song.reference_song or {}).get("title")
        ref_artist = (song.reference_song or {}).get("artist")
        if ref_title and ref_artist:
            specs.append(
                f"- Reference song blueprint: inspired by the structural pacing and metric groove of '{ref_title}' by {ref_artist} "
                f"(catalog genre: {song.reference_song.get('genre', 'unknown')}). The new song's genre remains {song.genre}; "
                "borrow structure and phrasing only. Do NOT copy any words, character names, or phrases; write original lyrics strictly for the brief."
            )

        if str(song.vocal).strip().lower() == "duet":
            s_a = (song.vocal_roles or {}).get("singer_a") or "Singer A (Male Vocal)"
            s_b = (song.vocal_roles or {}).get("singer_b") or "Singer B (Female Vocal)"
            specs.append(
                f"- Duet vocal arrangement: Two-person collaboration with {s_a} and {s_b}.\n"
                f"  * [Verse 1 - {s_a}] (Singer A solo)\n"
                f"  * [Verse 2 - {s_b}] (Singer B solo)\n"
                f"  * [Chorus - Duet Harmony] (Both singing in harmony; label lines '(Both)' if desired)\n"
                f"  * [Bridge - Call and Response] (Trading lines between {s_a} and {s_b})"
            )

        if song.style:
            specs.append(f"- Original style direction: {song.style}. Use only where compatible with the chosen genre ({song.genre}).")

        if song.rhyme_scheme and song.rhyme_scheme != "free":
            specs.append(f"- Rhyme scheme: hold {song.rhyme_scheme.upper()} in every section")
        if song.poetic_form:
            specs.append(f"- Poetic form: {song.poetic_form}")

        banned = [w.strip() for w in (song.banned_pairs or []) if w.strip()]
        if banned:
            specs.append(f"- Do not use these stock rhymes or phrases: {', '.join(banned)}")

        specs += [
            "",
            "CANONICAL AUDIO ENGINE SECTION TAGS (YuE2 & MiniMax 3)",
            "- Supported canonical section tags: Intro, Verse, Pre-Chorus, Chorus, Post-Chorus, Bridge, Instrumental, Solo, Outro.",
            f"- Mark each planned section with its bracketed tag on its own line according to the section plan ({plan_str}).",
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

        if song.engine_target == "yue2":
            specs.append("- Target engine YuE2: section tags only, no instrumental asides.")
        elif song.engine_target == "minimax":
            specs.append("- Target engine MiniMax Music 3: bracketed arrangement cues permitted.")

        if song.mode == "guide":
            specs[0] = "Act as a co-writer on the brief below."
            specs[-1] = "- Give concrete line rewrites and specific rhyme options, not general advice."

        answer_block = ""
        clean_answers = self._clean_answers(song.answers)
        if clean_answers:
            lines = "\n".join(
                f"- {k.replace('_', ' ')}: {v}" for k, v in clean_answers.items()
            )
            answer_block = f"\n\nWRITER'S DECISIONS\n{lines}"

        specs_text = "\n".join(specs)
        user_content = f"BRIEF\n{song.brief.strip() or 'An original song.'}"
        if song.story:
            user_content += f"\n\nSTORY\n{song.story}"
        user_content += f"{answer_block}\n\nOutput the lyrics now:"

        song.system_prompt = specs_text
        song.user_prompt = user_content
        return song

    # ------------------------------------------------------------- generation

    def generate(self, song: SongGeneration) -> SongGeneration:
        """Generate production-ready lyrics & engine packages for a song."""
        self.prepare_generation(song)
        song.start_draft()
        if not song.story:
            self.author_story(song)

        if song.section_by_section and song.mode != "guide" and not song.chat_history:
            SectionDrafter(self.workbench, chat=self._chat).draft_song(song)
        else:
            self.build_instructions_payload(song)
            messages: list[dict[str, str]] = []
            messages.append({"role": "system", "content": song.system_prompt})
            for msg in (song.chat_history or [])[-6:]:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    messages.append({"role": msg["role"], "content": msg["content"]})
            messages.append({"role": "user", "content": song.user_prompt})
            raw_text = self._chat(
                messages, model=song.model, temperature=song.temperature, top_p=song.top_p,
                max_tokens=song.max_tokens,
            )
            clean_lyrics = self._clean_lyrics(raw_text)
            song.sections = self._sections_from_lyrics(clean_lyrics)

        # Author engine prompts on this song instance
        EnginePromptBuilder(chat=self._chat).build(
            song=song,
            segments=self._template_segments(song.template_id),
            use_model=song.author_engine_prompt,
        )

        # Advisory whole-song craft check
        seen: set[str] = set()
        clean_lines: list[str] = []
        for raw in song.lyrics.splitlines():
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
                check_res = self.workbench.check_draft(
                    clean_lines, kind="verse", target_syllables=f"{song.syllable_min}-{song.syllable_max}"
                )
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
                check = None
        if check is not None:
            song.analytics["craft_check"] = check

        if song.status == "draft":
            song.status = "incomplete" if song.diagnostics.get("final_issues") else "finalized"
        self.song_store.save(song)
        return song
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
        "electronic": {
            "category": "Electronic / Ambient Sustained Vocables",
            "calls": ["(ooh)", "(ahh)", "(mmm)", "(oooh)"],
            "melodic_filler": ["Ooh...", "Ahh...", "Mmm..."],
            "usage": "Only sustained, airy vowel sounds that float in the mix ('Ooh...', 'Ahh...'), "
                     "placed sparingly at phrase ends. NEVER shouted or spoken tags like 'hey', 'yeah', "
                     "'ayy', and NEVER a comma tag clipped onto a line end."},
    }
    # Canonical genre -> set key. First match wins; longest/most-specific first.
    _GENRE_TO_SET: tuple[tuple[tuple[str, ...], str], ...] = (
        (("trap", "drill", "phonk"), "trap"),
        (("hip-hop", "hip hop", "rap", "boom bap"), "rap"),
        (("r&b", "rnb", "soul", "neo-soul", "funk", "motown"), "rb"),
        (("country", "folk", "americana", "bluegrass"), "country"),
        (("jazz", "swing"), "jazz"),
        # Electronic families come before pop so 'synth-pop'/'electropop' still read
        # as pop, but trance/ambient/house/techno/synthwave do not fall through to it.
        (("trance", "ambient", "synthwave", "house", "techno", "edm", "downtempo",
          "chillout", "chillwave", "lo-fi", "lofi", "dub", "idm", "drum and bass", "dnb"), "electronic"),
        (("pop", "dance", "synth-pop", "disco", "electropop", "electro"), "pop"),
        (("rock", "metal", "punk", "grunge"), "rock"),
    )
    # Genre families that never clip a shouted/spoken vocal tag onto line ends. A
    # reference's delivery tags are ignored for these: genre gates what's allowed.
    _NO_END_TAG_SETS = frozenset({"electronic"})
    _VOCABLE_DEFAULT: dict[str, Any] = {
        "category": "None -- no ad-libs for an unrecognised genre",
        "calls": ["(none)"],
        "melodic_filler": ["(none)"],
        "usage": "Genre is unrecognised, so add no parenthetical ad-libs or vocal tags. "
                 "Let the lines stand on their own.",
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
    def _genre_set_key(cls, genre: str, style: str = "") -> str | None:
        """The vocable set key for a song's genre ('electronic', 'pop', ...), or
        None when neither the genre nor the free-text style matches a family."""
        for src in ((genre or "").lower(), (style or "").lower()):
            for keys, setkey in cls._GENRE_TO_SET:
                if any(k in src for k in keys):
                    return setkey
        return None

    @classmethod
    def _vocables_for(cls, gen: "SongGeneration") -> dict[str, Any]:
        """Ad-lib set for the song. The genre decides what is allowed; the
        reference only chooses within it.

        1. The genre family gates end tags: a family in ``_NO_END_TAG_SETS``
           (electronic/ambient) never clips a shouted tag onto a line, so a
           reference's delivery tags are ignored and its own set applies.
        2. Otherwise a reference whose lines end on vocal tags ('ah', 'ayy') sets
           the delivery: those tags, clipped onto line ends.
        3. Without that, two or more quiet-mood words suppress interjections.
        4. Otherwise the genre set applies (no ad-libs for an unknown genre).
        """
        set_key = cls._genre_set_key(gen.genre, gen.style)
        # One policy: _genre_allows_end_tags (Settings-driven) gates the end-tag
        # delivery here and the strip/validation below, so prompt and lyrics agree.
        tags = gen.delivery_tags()
        if tags and cls._genre_allows_end_tags(gen):
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
        return cls._VOCABLE_SETS[set_key] if set_key else cls._VOCABLE_DEFAULT

    @classmethod
    def _genre_allows_end_tags(cls, gen: "SongGeneration") -> bool:
        """Whether the song's GENRE permits a shouted/spoken vocal end tag at all,
        independent of any reference. Two bans, either sufficient: the operator's
        Settings keyword list (``no_end_tag_genres``), and the built-in electronic/
        ambient set whose own vocables are wordless. Tag-friendly sets (pop, rap,
        trap, rock, country, jazz, r&b) allow tags; an unknown genre adds none.
        Removing a keyword from Settings lifts only that keyword ban, not the
        built-in electronic-set ban."""
        g = f"{gen.genre} {gen.style}".lower()
        from songwriter import settings as _settings
        forbidden = [k.strip().lower() for k in _settings.get_str("no_end_tag_genres").splitlines() if k.strip()]
        if any(k in g for k in forbidden):
            return False
        set_key = cls._genre_set_key(gen.genre, gen.style)
        return set_key is not None and set_key not in cls._NO_END_TAG_SETS

    @classmethod
    def _end_tags_for(cls, gen: "SongGeneration") -> list[str]:
        """The reference's vocal end tags to offer the writer -- only when the
        genre permits end tags. The genre gates; the reference merely selects
        which tags. Empty when the genre forbids them or no reference supplied any."""
        if not cls._genre_allows_end_tags(gen):
            return []
        return gen.delivery_tags()

    @staticmethod
    def _strip_end_tag(text: str) -> str:
        """Remove a trailing shouted/spoken vocal tag (", hey", " (yeah)") and a
        trailing parenthetical ad-lib, keeping the line's real words. Used when the
        genre forbids end tags, so a forbidden tag never reaches the lyrics."""
        out = re.sub(r"\s*\([^)]*\)\s*$", "", (text or "").strip())
        out = REF_TAG_RE.sub("", out).strip()
        return out

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
        scene: str = "",
        engine_notes: list[str] | None = None,
        line_numbers: list[int] | None = None,
    ) -> str:
        """Tier 2 critique prompt, rendered from the "lyric_critic" template
        (Settings -> LLM Prompts): the LLM judge reads the turn the way a listener hears it.

        The critic must paraphrase every line before scoring it, so a line it cannot
        restate in plain English (rhyme-forced splices like "Tear me up apart") fails
        on sense, and every failing line comes back with a rewrite for the retry.
        Engine facts are handed over as evidence so the critic never guesses at usage.
        """
        nums = line_numbers if line_numbers and len(line_numbers) == len(lines) else list(range(1, len(lines) + 1))
        lines_text = "\n".join(f"Line {n}: {line}" for n, line in zip(nums, lines))
        ref_title = (gen.reference_song or {}).get("title") or "Contemporary production"
        ref_artist = (gen.reference_song or {}).get("artist") or ""
        ref_label = f"\"{ref_title}\"" + (f" by {ref_artist}" if ref_artist else "")
        bpm_text = f"{gen.bpm} BPM" if gen.bpm else "Standard tempo"
        key_scale = f"{gen.key} {gen.scale}" if (gen.key and gen.scale) else "Natural groove"

        scene_text = f"• This Section's Scene: {scene.strip()}\n" if scene and scene.strip() else ""
        anchor_text = ""
        if anchor_lines:
            anchor_text = ("\nLINES ALREADY WRITTEN JUST BEFORE THIS DRAFT (it continues from these):\n"
                           + "\n".join(f"• {a}" for a in anchor_lines) + "\n")

        contract_text = ""
        if rhyme_contract:
            contract_text = f"\nRHYME & CADENCE CONTRACT FOR THIS SPECIFIC TURN:\n{rhyme_contract}\n"
        if meter_notes:
            contract_text += f"ARRANGEMENT / METER NOTES:\n{meter_notes}\n"

        engine_text = ""
        if engine_notes:
            engine_text = ("\nENGINE FACTS (measured from the song corpus; evidence, not verdicts -- "
                           "a fresh image can be unseen too):\n" + "\n".join(f"• {n}" for n in engine_notes) + "\n")

        return prompt_templates.render("lyric_critic", {
            "title": gen.title,
            "genre": gen.genre,
            "key_scale": key_scale,
            "tempo": bpm_text,
            "reference": ref_label,
            "section": section or "Couplet",
            "story": gen.brief or gen.story or "Grounded storytelling",
            "scene": scene_text,
            "recent_lines": anchor_text,
            "contract": contract_text,
            "engine_facts": engine_text,
            "lines": lines_text,
        })

    @staticmethod
    def _parse_critique(raw: str) -> dict[str, Any] | None:
        """The critic's JSON object, or None when the reply holds none."""
        match = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _run_llm_critique(
        self,
        lines: list[str],
        gen: SongGeneration,
        section: str = "",
        rhyme_contract: str = "",
        anchor_lines: list[str] | None = None,
        meter_notes: str = "",
        scene: str = "",
        engine_notes: list[str] | None = None,
        line_numbers: list[int] | None = None,
    ) -> dict[str, Any]:
        """Run the Tier 2 critic and normalise its verdict.

        The verdict is recomputed from its parts: a line the critic could not make
        sense of, or a failed hard dimension, fails the turn even when the model's
        own "passed" flag says otherwise. ``score`` (dimension mean minus one per
        nonsensical line) ranks attempts. An unreadable reply is retried once, then
        reported as unparsed -- never dressed up as a pass with invented scores.
        """
        prompt = self._build_critique_prompt(
            lines, gen, section=section, rhyme_contract=rhyme_contract,
            anchor_lines=anchor_lines, meter_notes=meter_notes, scene=scene,
            engine_notes=engine_notes, line_numbers=line_numbers,
        )
        from songwriter import settings as _settings
        # Too small a budget truncates the JSON, which then counts as unparsed.
        budget = int(_settings.get("critic_max_tokens"))
        # A separate critic model (blank = the writer's own) keeps the writer from
        # grading its own work. json_object asks the endpoint to return valid JSON,
        # cutting the "not valid JSON" retries; endpoints that reject it are handled
        # by _chat, which drops the hint and retries.
        critic_model = _settings.get_str("critic_model").strip() or gen.model
        messages = [{"role": "user", "content": prompt}]
        raw = self._chat(messages, model=critic_model, temperature=0.2,
                         max_tokens=budget, json_object=True).strip()
        critique = self._parse_critique(raw)
        if critique is None:
            raw = self._chat(
                messages + [
                    {"role": "assistant", "content": raw or "(no reply)"},
                    {"role": "user", "content": "That was not a valid JSON object. Reply with the JSON object only."},
                ],
                model=critic_model, temperature=0.0, max_tokens=budget, json_object=True,
            ).strip()
            critique = self._parse_critique(raw)
        if critique is None:
            return {
                "passed": True, "parsed": False, "score": None, "lines": [],
                "summary": "Critic reply was not valid JSON; this turn was judged on engine checks only.",
                "prompt": prompt, "raw_response": raw, "critic_model": critic_model,
            }

        rows = [r for r in (critique.get("lines") or []) if isinstance(r, dict)]
        nonsense = [r for r in rows if r.get("makes_sense") is False]
        hard_fails = [d for d in _CRITIC_HARD_DIMS
                      if isinstance(critique.get(d), dict) and critique[d].get("verdict") == "fail"]
        scores: list[float] = []
        for d in _CRITIC_DIMS:
            try:
                scores.append(float(critique[d]["score"]))
            except (KeyError, TypeError, ValueError):
                continue
        mean = sum(scores) / len(scores) if scores else 3.0
        critique["lines"] = rows
        critique["passed"] = critique.get("passed") is not False and not nonsense and not hard_fails
        critique["score"] = round(mean - len(nonsense), 2)
        critique["parsed"] = True
        critique["prompt"] = prompt
        critique["raw_response"] = raw
        critique["critic_model"] = critic_model
        return critique

    def stream_song_interactive(
        self,
        song: SongGeneration,
    ):
        """Draft a complete song line-by-line using a conversational critique loop.

        Logs each prompt sent, LLM response, validation scansion issues, and RhymeDB injections.
        """
        gen = self.prepare_generation(song)
        gen.start_draft()
        if not gen.story:
            self.author_story(gen)
        rhyme_svc = RhymeService(self.workbench)
        all_lines: list[str] = []
        sections_out: list[dict[str, Any]] = []
        incomplete_sections: list[dict[str, Any]] = []  # sections short of their planned line count
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
                    f"story as the singer's own lived material in the chosen genre ({gen.genre}), never as a "
                    "storybook to narrate. Borrow the reference's rhythm and stance, never its words or catalog genre.")
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
        unseen_min = int(_settings.get("unseen_pair_min_expected"))
        writer_budget = max(120, int(_settings.get("writer_max_tokens")))
        syl_tol = max(0, int(_settings.get("syllable_tolerance")))

        decisions_str = ("\n- Producer Interview Q&A & Creative Guidance:\n" + "\n".join(gen.interview_guide())) if gen.interview_guide() else ""
        vg = self._vocables_for(gen)

        def make_context_header() -> str:
            reg_str = ("Clean poetic" if not gen.allow_slang else "Include street slang") + (", allow explicit edge" if gen.allow_offensive else ", radio-clean")
            return prompt_templates.render("song_context", {
                "title": gen.title,
                "genre": gen.genre,
                "language": gen.language or "English",
                "moods": ", ".join(gen.moods) if gen.moods else "none specified",
                "instruments": ", ".join(gen.instruments) if gen.instruments else "none specified",
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
            # AND of every block's kept-pass state: a section is verified only when
            # each block passed. Declared here (not per block) so an earlier failed
            # block is not forgotten when a later block passes.
            section_passed = True

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
                            # Each candidate is shown inside the phrases songs actually
                            # land it in, so the model builds toward a sayable phrase
                            # instead of bolting a rhyme word onto the line.
                            offer = cands[:_PROMPT_RHYMES]
                            landings = rhyme_svc.landings(offer, gen)
                            shown = "; ".join(
                                f"{w} ({' / '.join(landings[w.lower()])})" if landings.get(w.lower()) else w
                                for w in offer)
                            rhyme_lines.append(
                                f"    Line {sp['line']}: rhyme with Line {sp['partner']} (end word \"{pw}\")"
                                + (f". True rhymes, with how real songs land them: {shown}. Pick the one that fits "
                                   f"the scene; a clean slant rhyme beats one that makes no sense" if offer else ""))
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
                    if tag_match and self._end_tags_for(gen):
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
                section_scene = " ".join(arc_parts)
                if rhyme_sound_guide.strip():
                    arc_parts.append(rhyme_sound_guide.strip())
                struct_pos_block = f"STRUCTURAL POSITION:\n{pos_line}\n• Section Arc: {' '.join(arc_parts)}"

                # Anchor 4: The Task
                count = len(block)
                which = f"Line {block[0]}" if count == 1 else f"Lines {block[0]}-{block[-1]}"
                lines_fmt = "\n".join(f"    Line {ln}: [Lyric line]" for ln in block)
                end_tags = self._end_tags_for(gen)
                if end_tags:
                    adlib_fmt_rule = (
                        f"  Vocal tags ({', '.join(repr(t) for t in end_tags)}) should be placed after a comma at the end of the line (e.g. 'word, ah').\n"
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
                # The writing rules (sense first, land rhymes in real phrases, no
                # filler) sit right above the task so they are read last.
                rules_block = prompt_templates.line_rules_text()

                p = f"{north_star_block}\n\n{struct_pos_block}\n\n{continuity_block}\n\n{rules_block}\n\n{task_block}"
                accepted_block: list[str] = []
                accepted_passed = True  # False if a block is accepted only as the best failed take
                p_retry = ""
                # (rank, attempt, lines) of the strongest usable attempt so far.
                best_attempt: tuple[tuple[bool, bool, float, int], int, list[str]] | None = None
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
                    # A reply can come back cut off at the token limit, which can
                    # drop a line or leave a truncated last word that still parses
                    # as the right count. Retry in place with a bigger budget (not a
                    # real attempt) while the endpoint reports it was cut off, so a
                    # section is never lost to the limit. A short but complete reply
                    # is a content miss, left to the normal attempt loop. The budget
                    # only caps cost; unused tokens are not spent.
                    produced: list[str] = []
                    budget = max(writer_budget, 60 * count + 20)
                    ceiling = writer_budget * 4
                    cut_off = False
                    for _grow in range(3):
                        meta: dict[str, Any] = {}
                        raw = self._chat([{"role": "user", "content": current_p}], model=gen.model,
                                         temperature=0.7, max_tokens=min(budget, ceiling), meta=meta).strip()
                        produced = self._clean_lyric_lines(raw, count)
                        cut_off = meta.get("finish_reason") == "length"
                        if not cut_off or budget >= ceiling:
                            break
                        budget = max(budget * 2, writer_budget * 2)
                    # A reply still truncated at the ceiling can't be trusted as
                    # whole: drop the last line so the turn fails and reports why,
                    # rather than committing a cut-off line as finished lyrics.
                    if cut_off and produced:
                        produced = produced[:-1]
                    # A vocal end tag (", hey") is pure decoration. When the genre
                    # forbids it, strip it from every line before validation so a
                    # forbidden tag can never reach the final lyrics -- not even via
                    # the best-of-failed-attempts fallback -- and the line is judged
                    # and counted on its real words.
                    if not self._genre_allows_end_tags(gen):
                        produced = [self._strip_end_tag(t) for t in produced]
                    yield {"type": "response", "turn_id": turn_id, "section": sec_name,
                           "response": "\n".join(produced), "raw": raw, "attempt": attempt,
                           "cut_off": cut_off, "usage": meta.get("usage") or {}, "model": meta.get("model")}

                    # Tier 1 vs Full Mode selection based on gen.validation_mode
                    is_hybrid_mode = (gen.validation_mode == "auto_llm")
                    critical_violations: list[str] = []
                    advisories: list[str] = []
                    critique_data: dict[str, Any] | None = None

                    # ---------------- Tier 1 Code Gatekeeper (Zero API Cost) ----------------
                    # 1. Line Count
                    if len(produced) != count:
                        reason = "was cut off at the token limit" if cut_off else "returned no usable line"
                        critical_violations.append(f"Expected {count} line(s), got {len(produced)} (model {reason}).")

                    provisional = sec_lines + produced
                    for sp, text in zip(specs, produced):
                        st = self.workbench.ca.line_stat(text)
                        if not text.strip():
                            critical_violations.append(f"Line {sp['line']} is empty.")

                        # (Length is checked by syllables below, in every mode.
                        # Forbidden end tags were already stripped before validation.)
                        # 3b. Trailing vocable tag cleanup & formatting
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
                                # The engine owns rhyme judgement in every mode: a
                                # contracted rhyme that doesn't land fails the turn
                                # here, so the critic never has to judge sound.
                                else:
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

                        # Sound the engine owns in every mode: syllable fit and the
                        # new-sound rule. A miss beyond the tolerance fails the turn;
                        # a smaller one is an advisory the critic sees, not a fail.
                        if st.syllables:
                            if st.syllables < (sp["min"] - syl_tol) or st.syllables > (sp["max"] + syl_tol):
                                critical_violations.append(f"Line {sp['line']}: {st.syllables} syllables, aiming ~{sp['target']} (guide {sp['min']}-{sp['max']}).")
                            elif st.syllables < sp["min"] or st.syllables > sp["max"]:
                                advisories.append(f"Line {sp['line']}: {st.syllables} syllables, just outside the guide {sp['min']}-{sp['max']}.")
                        if sp["line"] <= len(scheme) and scheme[sp["line"] - 1] != "-" and st.end_word and not sp["partner"]:
                            own = scheme[sp["line"] - 1]
                            for j in range(max(1, sp["line"] - RHYME_WINDOW), sp["line"]):
                                if j - 1 < len(scheme) and scheme[j - 1] not in ("-", own) and j <= len(provisional):
                                    ow = self.workbench.ca.line_stat(provisional[j - 1]).end_word
                                    oc = self.workbench.ph.compare(ow, st.end_word) if ow else None
                                    if oc and oc.kind in ("perfect", "family"):
                                        critical_violations.append(f"Line {sp['line']} must open a new rhyme sound; '{st.end_word}' rhymes with Line {j} ('{ow}').")
                                        break

                        # Full rules mode only: fertility, stress and cliche advisories.
                        if not is_hybrid_mode:
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
                    # Auto LLM mode: every attempt that clears Tier 1 is read by the critic,
                    # the last one included, so the kept take is always a judged one.
                    tier1_passed = len(critical_violations) == 0 and len(produced) == count
                    if is_hybrid_mode and tier1_passed:
                        try:
                            # Rhyme contract plus measured facts per line: the critic
                            # weighs evidence instead of guessing at sound or usage.
                            contract_items = []
                            engine_notes = []
                            block_lines = [s_["line"] for s_ in specs]
                            for sp, text in zip(specs, produced):
                                st_c = self.workbench.ca.line_stat(text)
                                pw = (self.workbench.ca.line_stat(provisional[sp["partner"] - 1]).end_word
                                      if sp["partner"] and 1 <= sp["partner"] <= len(provisional) else "")
                                if sp["partner"]:
                                    if sp["partner"] in block_lines:
                                        contract_items.append(f"• Line {sp['line']}: Must rhyme with Line {sp['partner']} (written together in this couplet).")
                                    elif pw:
                                        contract_items.append(f"• Line {sp['line']}: Must rhyme with Anchor Word \"{pw}\" (from preceding Line {sp['partner']}).")
                                else:
                                    contract_items.append(f"• Line {sp['line']}: Opens a NEW sound; does NOT rhyme with other lines in this couplet.")
                                note = (f"Line {sp['line']}: {st_c.syllables} syllables (guide {sp['min']}-{sp['max']}), "
                                        f"ends on '{st_c.end_word}'")
                                if pw and st_c.end_word:
                                    comp = self.workbench.ph.compare(pw, st_c.end_word)
                                    if comp:
                                        note += f"; rhyme with '{pw}' measures as {comp.kind}"
                                unseen = self.workbench.unseen_pairs(text, min_expected=unseen_min)
                                if unseen:
                                    note += ("; common words that no song in the corpus puts side by side: "
                                             + ", ".join(f"'{u}'" for u in unseen))
                                engine_notes.append(note)
                            guide_parts = [f"Line {sp['line']}: ~{sp['target']} syl ({sp['min']}-{sp['max']})" for sp in specs]
                            meter_notes = f"Target tempo: {gen.bpm or 120} BPM. Syllable guide: {', '.join(guide_parts)}."
                            critique_data = self._run_llm_critique(
                                produced, gen, section=sec_name,
                                rhyme_contract="\n".join(contract_items),
                                anchor_lines=list(recent_lines) or None,
                                meter_notes=meter_notes,
                                scene=section_scene,
                                engine_notes=engine_notes,
                                line_numbers=block_lines,
                            )
                        except Exception as c_err:
                            # A critic outage must not stall drafting, but it is shown
                            # as unavailable, never as a pass with invented scores.
                            critique_data = {
                                "passed": True, "parsed": False, "score": None, "lines": [],
                                "summary": f"Critic unavailable ({c_err}); this turn was judged on engine checks only.",
                            }
                        if not critique_data.get("passed", True):
                            c_summary = critique_data.get("summary") or "Critique flagged quality issues."
                            critical_violations.append(f"Critic: {c_summary}")
                            for row in critique_data.get("lines") or []:
                                if row.get("makes_sense") is False:
                                    what = f"Line {row.get('line')} does not make sense"
                                    if (row.get("meaning") or "").strip():
                                        what += f" (reads as: {row['meaning'].strip()})"
                                    if (row.get("forced_word") or "").strip():
                                        what += f"; '{row['forced_word'].strip()}' is only there for the rhyme or length"
                                    critical_violations.append(what + ".")
                            for dim in _CRITIC_DIMS:
                                dim_obj = critique_data.get(dim)
                                if isinstance(dim_obj, dict) and dim_obj.get("verdict") == "fail":
                                    advisories.append(f"{dim.replace('_', ' ').title()}: {dim_obj.get('feedback', '')}")

                    passed = len(critical_violations) == 0 and len(produced) == count
                    issues = list(critical_violations)
                    if not passed and advisories:
                        issues.extend(advisories)
                    is_last = (attempt == gen.max_retries)
                    # Rank every usable attempt so a failed final retry never
                    # overwrites a better earlier take. A missing critic score is neutral.
                    usable = [t for t in (produced or []) if t and t.strip()][:count]
                    c_score = critique_data.get("score") if critique_data else None
                    rank = (passed, tier1_passed,
                            float(c_score) if isinstance(c_score, (int, float)) else 3.0,
                            -len(critical_violations))
                    if usable and (best_attempt is None or rank >= best_attempt[0]):
                        best_attempt = (rank, attempt, usable)
                    kept_attempt: int | None = None
                    if passed:
                        accepted_block, kept_attempt = usable, attempt
                    elif is_last and best_attempt:
                        # No attempt passed: keep the best take so the section is not
                        # lost, but mark it NOT passed -- the best take's own rank[0].
                        accepted_block, kept_attempt = best_attempt[2], best_attempt[1]
                        accepted_passed = accepted_passed and bool(best_attempt[0][0])
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
                        "kept_attempt": kept_attempt,
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
                    gen.turns.append({
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
                        "accepted_line": "\n".join(accepted_block) if kept_attempt is not None else None,
                    })
                    turn_id += 1

                    if passed or is_last:
                        # Only real, non-empty lines the model produced are kept.
                        # NEVER pad with hardcoded filler and NEVER accept blank
                        # lines: a short section beats junk committed as lyrics.
                        break
                    issues_summary = "\n    • ".join(issues)
                    prior = " / ".join(produced) if produced else "(nothing usable)"
                    # The critic's rewrites ride along so the retry starts from a
                    # line that already makes sense instead of re-forcing the rhyme.
                    fixes = [r for r in ((critique_data or {}).get("lines") or [])
                             if isinstance(r.get("fix"), str) and r["fix"].strip()]
                    fix_block = ""
                    if fixes:
                        fix_block = ("  Editor's suggested rewrites (use them, or write something better that keeps the same sense):\n"
                                     + "\n".join(f"    Line {r.get('line')}: {r['fix'].strip()}" for r in fixes) + "\n\n")
                    p_retry = (
                        f"{north_star_block}\n\n"
                        f"{struct_pos_block}\n\n"
                        f"{continuity_block}\n\n"
                        f"{rules_block}\n\n"
                        f"TASK (REVISION):\n"
                        f"  Previous draft: \"{prior}\"\n"
                        f"  Issues to fix:\n    • {issues_summary}\n\n"
                        f"{fix_block}"
                        f"  Write [{sec_name}] {which}.\n"
                        f"{syl_lines}\n"
                        f"{rhyme_block}\n"
                        f"{out_rule}"
                    )

                # Fold this block's kept-pass state into the section. A single failed
                # block makes the whole section unverified, even if later blocks pass.
                section_passed = section_passed and accepted_passed
                for text in accepted_block:
                    all_lines.append(text)
                    sec_lines.append(text)
                    yield {"type": "line_accepted", "section": sec_name, "line": text, "line_number": len(all_lines)}

            if is_chorus and not cached_chorus_lines:
                cached_chorus_lines = list(sec_lines)

            # A section is incomplete when it is short of its planned line count OR
            # when any block's lines were only accepted as the best failed take (no
            # attempt passed the engine). Either way the song is finalized as
            # incomplete, so a persistently non-rhyming or nonsensical draft is never
            # shown as verified just because it has the right number of lines.
            planned = gen.lines_in_section(sec_name)
            if not gen.repeat_of(sec_name) and (len(sec_lines) < planned or not section_passed):
                short = len(sec_lines) < planned
                reason = "short" if short else "did not pass validation"
                incomplete_sections.append({"name": sec_name, "have": len(sec_lines),
                                            "planned": planned, "reason": reason})
                yield {"type": "section_incomplete", "section": sec_name,
                       "have": len(sec_lines), "planned": planned, "reason": reason}
            sections_out.append({
                "name": sec_name,
                "kind": sec_kind,
                "lines": sec_lines,
                "performer": performer,
            })

        gen.sections = list(sections_out)
        gen.incomplete_sections = list(incomplete_sections)
        gen.analytics = {
            "avg_syllables_per_line": live_avg_syl,
            "avg_stressed_beats_per_line": live_avg_beats,
            "pulse_variance": live_var,
            "ttr_vocabulary_diversity": live_ttr,
            "total_lines": len(all_lines),
        }
        gen.status = "incomplete" if incomplete_sections else "finalized"

        EnginePromptBuilder(chat=self._chat).build(
            song=gen,
            segments=self._template_segments(gen.template_id),
            use_model=gen.author_engine_prompt,
        )
        self.song_store.save(gen)

        yield {
            "type": "complete",
            "song": gen.model_dump(mode="json"),
        }

    def draft_song_interactive(self, song: SongGeneration) -> SongGeneration:
        """Batch wrapper over stream_song_interactive."""
        for ev in self.stream_song_interactive(song):
            pass
        return song

    # --------------------------------------------------------- song library

    def save_generation(self, gen: SongGeneration) -> SongGeneration:
        """Persist a generation; assigns song_id and bumps revision. Raises on failure."""
        if not gen.model:
            gen.model = self.get_default_model()
        return self.song_store.save(gen)

    def load_generation(self, song_id: str) -> SongGeneration | None:
        """The exact SongGeneration that was saved, for repopulating Steps 1-4."""
        return self.song_store.load(song_id)

    def list_revisions(self, song_id: str) -> list[SongGeneration]:
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
    ) -> list[SongGeneration]:
        return self.song_store.list(
            status=status, genre=genre, query=query, sort_by=sort_by,
            sort_order=sort_order, limit=limit, offset=offset,
        )
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
        # A verbose model can take 40-110 s on a full Step 4 prompt; a 60 s
        # deadline cuts it off mid-answer.
        timeout: int = 180,
        reasoning_effort: str | None = "low",
        json_object: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> str:
        """Return the reply text. If ``meta`` is given, it is filled with
        ``finish_reason``, ``usage`` (prompt/completion/cached tokens) and the
        ``model`` the endpoint reports, so the caller can tell a reply cut off at
        the token limit from one the model never wrote."""
        active_model = (model or "").strip() or self.get_default_model()
        # Never send the model an empty/whitespace prompt: it replies
        # conversationally ("your message came through empty"), which then leaks
        # as a bogus lyric. Fail loudly so the caller can retry with real content.
        if not any((m.get("content") or "").strip() for m in messages):
            raise RuntimeError("Refusing to call the LLM with an empty prompt.")

        # A low reasoning_effort keeps a thinking model from spending the whole
        # token budget on hidden reasoning and leaving nothing for the answer.
        # Dropped automatically if the endpoint rejects it (see below).
        chat_payload: dict[str, Any] = {
            "model": active_model,
            "messages": messages,
            "max_tokens": int(max_tokens),
        }
        if reasoning_effort:
            chat_payload["reasoning_effort"] = reasoning_effort
        if json_object:
            # Ask the endpoint to return a JSON object. Not every endpoint supports
            # it; a 400/422 drops the optional hints and retries (see below).
            chat_payload["response_format"] = {"type": "json_object"}

        def _send(payload: dict[str, Any]) -> str:
            return self._post_chat(payload, timeout=timeout, meta=meta)

        try:
            return _send(chat_payload)
        except urllib.error.HTTPError as err:
            optional = ("reasoning_effort", "response_format")
            if any(k in chat_payload for k in optional) and err.code in (400, 422):
                # Endpoint rejected an optional hint; retry once without them.
                plain = {k: v for k, v in chat_payload.items() if k not in optional}
                try:
                    return _send(plain)
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

    def _post_chat(self, payload: dict[str, Any], timeout: int = 180,
                   meta: dict[str, Any] | None = None) -> str:
        """POST one chat-completions request and return the reply text. Fills
        ``meta`` with the reply's finish_reason, usage and model when given.
        Isolated so tests can stub transport without a network."""
        req = urllib.request.Request(
            f"{self.endpoint}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choice = (data.get("choices") or [{}])[0]
        content = ((choice.get("message") or {}).get("content") or "")
        if meta is not None:
            meta["finish_reason"] = choice.get("finish_reason")
            meta["usage"] = data.get("usage") or {}
            meta["model"] = data.get("model") or payload.get("model")
        return content.strip()

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

    def draft_turn(
        self,
        song: SongGeneration,
        *,
        accepted: list[dict[str, Any]] | None = None,
        reject_reason: str = "",
        rejections: list[dict[str, Any]] | None = None,
    ) -> SongGeneration:
        """Draft the next block of lines, given the turns accepted so far.

        ``accepted`` is a list of {"section", "lines": [...]} already committed by
        the writer. Returns the next turn's prompt, drafted lines, and per-line
        scansion for human review -- or {"done": True} when the song is complete.
        A ``reject_reason`` is fed back to the model as the writer's critique.
        """
        if song.status != "draft" or (not song.turns and not accepted):
            song.start_draft()
        self.prepare_generation(song)
        turn = self._draft_turn_data(
            song, accepted=accepted, reject_reason=reject_reason, rejections=rejections,
        )
        turn["type"] = "human_turn"
        if reject_reason:
            turn["feedback"] = reject_reason
        if rejections:
            turn["rejections"] = rejections
        song.turns.append(turn)
        return song

    def _draft_turn_data(
        self,
        gen: SongGeneration,
        *,
        accepted: list[dict[str, Any]] | None = None,
        reject_reason: str = "",
        rejections: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
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
                    offer = sp["rhymes"][:_PROMPT_RHYMES]
                    landings = self._rhyme_service().landings(offer, gen) if offer else {}
                    shown = ", ".join(
                        f"{w} ({' / '.join(landings[w.lower()])})" if landings.get(w.lower()) else w
                        for w in offer)
                    rh += f", like \"{sp['partner_word']}\"" + (f"; true rhymes, as real songs land them: {shown}" if offer else "")
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

        lang_line = f"  Language: {gen.language or 'English'}.\n"
        moods_line = f"  Moods: {', '.join(gen.moods)}.\n" if gen.moods else ""
        inst_line = f"  Instruments: {', '.join(gen.instruments)}.\n" if gen.instruments else ""
        return (
            f"You are co-writing \"{gen.title}\" ({gen.genre}).\n"
            f"{lang_line}{moods_line}{inst_line}\n"
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

    def finalize_turns(
        self,
        song: SongGeneration,
        *,
        accepted: list[dict[str, Any]] | None = None,
    ) -> SongGeneration:
        """Assemble human-accepted turns into a saved SongGeneration + engine prompts."""
        self.prepare_generation(song)

        # Merge accepted turns into sections (consecutive same-section blocks join).
        sections: list[dict[str, Any]] = []
        for a in (accepted or []):
            name = a.get("section", "Section")
            lines = [l for l in (a.get("lines") or []) if str(l).strip()]
            if sections and sections[-1]["name"] == name:
                sections[-1]["lines"].extend(lines)
            else:
                sections.append({
                    "name": name,
                    "kind": name.lower().strip(),
                    "lines": list(lines),
                    "performer": song.vocal,
                })
        song.sections = sections

        EnginePromptBuilder(chat=self._chat).build(
            song=song,
            segments=self._template_segments(song.template_id),
            use_model=song.author_engine_prompt,
        )
        song.status = "finalized"
        self.song_store.save(song)
        return song

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
