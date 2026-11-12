"""SongGeneration: the single source of truth for one song.

Every setting a generation needs lives here exactly once. The UI fills it in
over Steps 1-4, the line-writing loop reads from it, and the whole object is
saved as one JSON document and loaded back into an identical object.

Rules:
  * one field per fact -- no ``final_*`` / ``sec_*`` copies anywhere else;
  * priority is applied once, in ``resolve_inputs``:
        Step 3 answer -> Step 2 control -> reference song -> template default;
  * per-section / per-line syllable windows are computed on demand by
    ``line_window`` and never stored.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field as field, computed_field, field_serializer, field_validator, model_validator

from songwriter.corpus.ingest import normalize_section


def _is_chorus_name(name: str) -> bool:
    """Chorus-family label ("Chorus", "Repeat Chorus", "Hook"), not pre/post-chorus."""
    low = (name or "").lower()
    if "pre" in low or "post" in low:
        return False
    return any(k in low for k in ("chorus", "hook", "refrain"))


def couple_singletons(scheme: str) -> str:
    """Pair adjacent unrhymed reference lines into couplets, then relabel.

    A detected scheme like ABCDEEFF means the first four reference lines rhymed
    loosely (slant/internal) or not at all; copying that leaves four lines of the
    new song unrhymed. Adjacent single-use letters are joined into couplets, so
    ABCDEEFF -> AABBCCDD. '-' (no end rhyme, e.g. a repeated hook word) is kept.
    """
    if not scheme:
        return scheme
    counts = {c: scheme.count(c) for c in set(scheme) if c != "-"}
    marks = list(scheme)
    i = 0
    pair_id = 0
    while i < len(marks) - 1:
        a, b = marks[i], marks[i + 1]
        if a != "-" and b != "-" and counts.get(a) == 1 and counts.get(b) == 1:
            tag = f"#{pair_id}"
            marks[i] = marks[i + 1] = tag
            pair_id += 1
            i += 2
        else:
            i += 1
    labels: dict[str, str] = {}
    out = []
    for m in marks:
        if m == "-":
            out.append("-")
            continue
        if m not in labels:
            labels[m] = chr(ord("A") + len(labels)) if len(labels) < 26 else "-"
        out.append(labels[m])
    return "".join(out)


# Trailing vocal tag a reference line ends on: "I love my baby, ah".
REF_TAG_RE = re.compile(r"[,\s]+(ah|ayy|ay|yeah|yah|uh|oh|huh|hey|woo)\W*$", re.IGNORECASE)
# Share of reference lines that must end on a tag for tags to be part of the
# song's delivery (Starboy: 15 of 64 lines).
REF_TAG_MIN_RATIO = 0.15


def word_band(lo_syl: int, hi_syl: int) -> tuple[int, int]:
    """Spoken word-count band for a syllable window (~1.45 syllables per word).

    Kept tight so a 10-syllable target reads as 6-8 words, not 6-10: loose word
    budgets are what let lines bloat into 13-word ballad sentences.
    """
    lo = max(3 if lo_syl >= 5 else 2, round(lo_syl / 1.45))
    hi = max(lo + 1, round(hi_syl / 1.45))
    return lo, hi

RHYME_SCHEMES = ("free", "AABB", "ABAB", "ABCB", "AAAA", "ABBA")

SECTION_PLANS = {
    "standard": "[Intro], [Verse 1], [Pre-Chorus], [Chorus], [Post-Chorus], [Verse 2], [Pre-Chorus], [Chorus], [Bridge], [Chorus], [Outro]",
    "anthem": "[Intro], [Verse 1], [Pre-Chorus], [Chorus], [Post-Chorus], [Verse 2], [Pre-Chorus], [Chorus], [Solo], [Bridge], [Chorus], [Outro]",
    "pop-dance": "[Intro], [Verse 1], [Pre-Chorus], [Chorus], [Post-Chorus], [Verse 2], [Pre-Chorus], [Chorus], [Bridge], [Instrumental], [Chorus], [Outro]",
    "ballad": "[Intro], [Verse 1], [Verse 2], [Chorus], [Verse 3], [Chorus], [Solo], [Chorus], [Outro]",
    "compact": "[Verse 1], [Chorus], [Verse 2], [Chorus], [Outro]",
    "hook-led": "[Intro], [Hook], [Verse 1], [Hook], [Verse 2], [Hook], [Outro]",
    "through-composed": "[Verse 1], [Verse 2], [Verse 3], [Coda]",
}

DEFAULT_BANNED = (
    "night/light", "fire/desire", "heart/apart",
    "rain/pain", "cry/die", "love/above",
    "mind/blind", "soul/control", "true/you",
    "cold/hold", "fall/call", "dreams/seems",
    "say/away", "life/strife", "tears/fears",
)

# Answer ids that configure the engine rather than describe the song; they are
# applied to fields, not echoed into the prompt's creative guidance.
_TECHNICAL_ANSWERS = frozenset({
    "rhyme_register", "offensive_language", "allow_phrases", "banned", "target_mood",
    "genre", "vocal", "vocal_type", "arrangement", "bpm", "musical_key", "scale",
    "syllable_target", "rhyme_scheme", "section_plan", "section_lines", "lines_per_turn",
    "validation_mode", "required_tone", "correction_strictness",
    "language", "subgenre", "moods", "mood", "instruments", "instrumentation",
    "time_signature", "duration_hint_sec", "vocal_style",
    "rhyme_sound_verse", "rhyme_sound_chorus", "rhyme_sound_bridge",
})

_SHORT_SECTION = ("intro", "outro", "hook")

# Step 3 options that keep the reference song's own shape start with this.
FOLLOW_REFERENCE = "Follow reference"

_RHYMING_KINDS = ("perfect", "slant", "family", "additive", "subtractive", "assonance")


# Most lines an intro or outro may have. Longer "intros" are a parsing
# artefact (untagged lyrics), not a real song shape.
MAX_FRAME_LINES = 4

# Furthest back (in lines) a rhyme partner may sit. Beyond a 4-line stanza
# the ear loses the sound, so the line opens a new rhyme instead.
RHYME_WINDOW = 4


def extend_scheme(pattern: str, n: int) -> str:
    """Stretch a scheme to n lines; each repeat gets fresh letters (ABAB x2 -> ABABCDCD).

    '-' marks an unrhymed line and is kept as-is.
    """
    if not pattern or n <= 0:
        return ""
    width = len({c for c in pattern if c.isalpha()}) or 1
    out: list[str] = []
    block = 0
    while len(out) < n:
        for c in pattern:
            if len(out) >= n:
                break
            out.append(chr(ord(c.upper()) + block * width) if c.isalpha() else "-")
        block += 1
    return "".join(out)


def parse_section_plan(value: str | None) -> list[str]:
    """Plan key ("standard"), labelled option ("standard -- [...]") or bracket string -> names."""
    raw = str(value or "").strip()
    if not raw:
        return []
    if "[" not in raw:
        raw = SECTION_PLANS.get(raw.split("--")[0].strip().lower(), "")
    elif "--" in raw and raw.split("--")[0].strip().lower() in SECTION_PLANS:
        raw = raw.split("--", 1)[1]
    return [s.strip() for s in re.findall(r"\[([^\]]+)\]", raw) if s.strip()]


def parse_syllable_range(value: str | None) -> tuple[int, int] | None:
    nums = [int(n) for n in re.findall(r"\d+", str(value or ""))]
    if len(nums) >= 2:
        return min(nums[0], nums[1]), max(nums[0], nums[1])
    if len(nums) == 1:
        return max(1, nums[0] - 1), nums[0] + 1
    return None


def _first_int(value: Any) -> int | None:
    m = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return int(round(float(m.group(0)))) if m else None


def _clean_genre(value: str | None) -> str:
    return str(value or "").split(",")[0].split("(")[0].strip()


class SongGeneration(BaseModel):
    """The song document used by authoring, storage, the web app, and the API."""

    model_config = ConfigDict(extra="forbid")

    # --- identity ----------------------------------------------------------
    song_id: str = ""
    revision: int = 0
    # Take grouping: songs are creative works, takes are their generated versions.
    # `take_group` is a stable id shared by every take of one song (the first take's
    # song_id); `take_number` is 1..N within that group. Empty/0 = a standalone song
    # (its own single take). Persisted in the generation JSON, surfaced by the API.
    take_group: str = ""
    take_number: int = 0
    status: str | None = "draft"
    created_at: datetime | None = None
    updated_at: datetime | None = None
    take_count: int = 1
    rating: int | None = None
    notes: str | None = None
    title: str = "Untitled Song"
    artist: str = ""
    brief: str = ""
    story: str = ""

    # --- model (user-selected, never hardcoded) ------------------------------
    model: str = ""

    # --- music ---------------------------------------------------------------
    language: str | None = None
    bpm: int = 92
    key: str = "C"
    scale: str = "major"
    genre: str = "Pop"
    subgenre: str = ""
    style: str = ""
    moods: list[str] = field(default_factory=list)
    instruments: list[str] = field(default_factory=list)
    duration_hint_sec: int | None = None
    vocal: str = "melodic lead"
    vocal_roles: dict[str, str] = field(default_factory=dict)
    time_signature: str = "4/4"
    chords: list[str] = field(default_factory=list)

    # --- reference -----------------------------------------------------------
    template_id: int | None = None
    reference_song: dict[str, Any] | None = None

    # --- structure & meter ---------------------------------------------------
    section_plan: list[str] = field(default_factory=list)
    max_sections: int | None = None
    syllable_min: int = 8
    syllable_max: int = 10
    # Step 3 "follow reference" choices: each copies one part of the reference's shape.
    follow_reference_syllables: bool = False
    follow_reference_lines: bool = False
    follow_reference_rhyme: bool = False
    section_lines: int = 4
    # How many lines the model drafts per turn: 1, 2, or 0 = the whole section
    # in one pass (so it can shape a 4-bar block, e.g. a short punchy line 4).
    lines_per_turn: int = 2
    # The lyric critic, deterministic rules alone, or the writer accepts each turn.
    validation_mode: str = "auto_llm"

    # --- rhyme & register ------------------------------------------------------
    rhyme_scheme: str = "free"
    allow_slang: bool = False
    allow_offensive: bool = False
    allow_phrases: bool = True
    banned_pairs: list[str] = field(default_factory=lambda: list(DEFAULT_BANNED))

    # --- Step 3 interview ------------------------------------------------------
    answers: dict[str, str] = field(default_factory=dict)
    # The questions as shown to the writer (id, question, why, options, category),
    # so a reopened song can offer every choice again, not just the saved answer.
    questions: list[dict[str, Any]] = field(default_factory=list)

    # --- run budget ------------------------------------------------------------
    max_retries: int = 5
    # How hard the validator corrects/reinforms the model: "draft" (loose, few
    # retries, meter+rhyme only), "balanced" (adds cliche advisories), "polished"
    # (adds cliche + melodic-delta as hard failures, most retries).
    correction_strictness: str = "balanced"
    mode: str = "creator"
    temperature: float = 0.7
    top_p: float = 0.9
    max_tokens: int = 900
    poetic_form: str | None = None
    section_by_section: bool = True
    author_engine_prompt: bool = True
    chat_history: list[dict[str, str]] = field(default_factory=list)

    # --- Step 4 output ---------------------------------------------------------
    sections: list[dict[str, Any]] = field(default_factory=list)
    abc_score: str = ""
    engine_prompts: dict[str, str] = field(default_factory=dict)
    engine_target: str = "standard"
    analytics: dict[str, Any] = field(default_factory=dict)
    engine_prompt_author: str = ""
    engine_prompt_section_arc: str = ""
    system_prompt: str = ""
    user_prompt: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)
    incomplete_sections: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- derived

    @property
    def syllable_target(self) -> int:
        return (self.syllable_min + self.syllable_max) // 2

    @computed_field
    @property
    def lyrics(self) -> str:
        parts: list[str] = []
        for sec in self.sections:
            parts.append(f"[{sec.get('name', 'Section')}]")
            parts.extend(sec.get("lines") or [])
            parts.append("")
        return "\n".join(parts).strip()

    @computed_field
    @property
    def instrumental(self) -> bool:
        vocal = self.vocal.strip().lower()
        return vocal in {"none", "no vocals"} or "instrumental" in vocal

    @computed_field
    @property
    def has_abc(self) -> bool:
        return bool(self.abc_score.strip())

    def planned_sections(self) -> list[str]:
        names = self.section_plan or parse_section_plan("standard")
        return names[: self.max_sections] if self.max_sections else list(names)

    def repeat_of(self, section_name: str) -> str | None:
        """The earlier planned section this one repeats, if any.

        Any later chorus ("Chorus", "Repeat Chorus", "Final Chorus") replays the
        first chorus verbatim, so its line count and lines come from that chorus,
        never from its own reference entry (which may be a medley of 7 lines).
        """
        if not _is_chorus_name(section_name):
            return None
        names = self.planned_sections()
        idx = names.index(section_name) if section_name in names else len(names)
        for earlier in names[:idx]:
            if _is_chorus_name(earlier):
                return earlier
        return None

    def lines_in_section(self, section_name: str) -> int:
        """Line count for a section: the reference's own count, or the Step 3 choice.

        Intros and outros are capped at ``MAX_FRAME_LINES`` whatever the source:
        a reference parsed as one long intro must not produce a 25-line intro.
        A repeated chorus always has the first chorus's count.
        """
        first = self.repeat_of(section_name)
        if first:
            return self.lines_in_section(first)
        low = section_name.lower()
        is_frame = "intro" in low or "outro" in low
        if self.follow_reference_lines:
            ref = self.reference_section(section_name) or {}
            n = int(ref.get("line_count") or len(ref.get("lines") or []) or len(ref.get("contour") or []) or 0)
            if n:
                return min(n, MAX_FRAME_LINES) if is_frame else n
        return 2 if is_frame else self.section_lines

    def section_scheme(self, section_name: str) -> str:
        """Rhyme letters for every line of a section ('-' = no end rhyme).

        Following the reference uses the scheme most sections of this kind share,
        so one off verse in the reference (ABCB among ABAB verses) does not break
        the song's verse pattern.
        """
        first = self.repeat_of(section_name)
        if first:
            return self.section_scheme(first)
        n = self.lines_in_section(section_name)
        if self.follow_reference_rhyme:
            ref_scheme = couple_singletons(self._dominant_reference_scheme(section_name))
            if ref_scheme.strip("-"):
                return extend_scheme(ref_scheme, n)
        if self.rhyme_scheme and self.rhyme_scheme != "free":
            return extend_scheme(self.rhyme_scheme, n)
        return extend_scheme("AA", n)  # free: rhyming couplets

    def _dominant_reference_scheme(self, section_name: str) -> str:
        """Most common reference scheme among same-kind, same-length sections."""
        own = self.reference_section(section_name) or {}
        own_scheme = str(own.get("rhyme_scheme") or "")
        kind = normalize_section(section_name)
        if kind == "other":
            return own_scheme
        n = len(own_scheme)
        counts: dict[str, int] = {}
        for sec in (self.reference_song or {}).get("sections") or []:
            if not isinstance(sec, dict):
                continue
            sk = normalize_section(str(sec.get("kind") or sec.get("name") or ""))
            sch = str(sec.get("rhyme_scheme") or "")
            if sk == kind and sch.strip("-") and (not n or len(sch) == n):
                counts[sch] = counts.get(sch, 0) + 1
        if not counts:
            return own_scheme
        best = max(counts.values())
        # Ties keep the section's own scheme.
        return own_scheme if counts.get(own_scheme) == best else max(counts, key=counts.get)

    def rhyme_partner(self, section_name: str, line_index: int) -> int | None:
        """1-based earlier line in the section this line must rhyme with, if any.

        The partner is the MOST RECENT line sharing this line's scheme letter, and
        only within ``RHYME_WINDOW`` lines: nobody hears a rhyme across 20 bars, so
        a letter that last appeared further back starts a fresh sound instead.
        """
        scheme = self.section_scheme(section_name)
        if not 1 <= line_index <= len(scheme):
            return None
        letter = scheme[line_index - 1]
        if letter == "-":
            return None
        lo = max(0, line_index - 1 - RHYME_WINDOW)
        for j in range(line_index - 2, lo - 1, -1):
            if scheme[j] == letter:
                return j + 1
        return None

    def line_blocks(self, section_name: str) -> list[list[int]]:
        """Group a section's line numbers into per-turn blocks (1, 2, or all-at-once)."""
        n = self.lines_in_section(section_name)
        step = n if self.lines_per_turn <= 0 else max(1, self.lines_per_turn)
        return [list(range(i + 1, min(i + step, n) + 1)) for i in range(0, n, step)]

    def interview_guide(self) -> list[str]:
        labels = {q.get("id"): q.get("question") for q in self.questions}
        out: list[str] = []
        for qid, answer in self.answers.items():
            if answer and qid not in _TECHNICAL_ANSWERS:
                label = labels.get(qid) or qid.replace("_", " ").title()
                out.append(f"  • {label} -> {answer}")
        return out

    def reference_section(self, section_name: str) -> dict[str, Any] | None:
        """The reference section a planned section is modelled on: same name, else same kind."""
        secs = [s for s in (self.reference_song or {}).get("sections") or [] if isinstance(s, dict)]
        low = section_name.strip().lower()
        for sec in secs:
            if str(sec.get("name") or "").strip().lower() == low:
                return sec
        kind = normalize_section(section_name)
        if kind == "other":
            return None
        for sec in secs:
            if normalize_section(str(sec.get("kind") or sec.get("name") or "")) == kind:
                return sec
        return None

    def line_window(self, section_name: str, line_index: int) -> tuple[int, int, int]:
        """(target, min, max) syllables for one line; the only place a window is computed.

        Following the reference uses its per-line syllable counts; otherwise the
        Step 3 range applies. Intros, outros and hooks get a shorter chant window.
        """
        low = section_name.lower()
        if self.follow_reference_syllables:
            # Vocable-only reference lines ("Ahum") scan to 0 syllables; they are
            # not lyric slots. References stored before parsing dropped them may
            # still carry the zeros, so filter them here too.
            contour = [int(c) for c in (self.reference_section(section_name) or {}).get("contour") or [] if int(c) > 0]
            # Only trust the reference contour if it is in a singable range. A
            # degenerate reference (e.g. a counting/chant track) yields 1-3
            # syllable lines that contradict the writer's stated range; ignore it
            # and fall back to the Step-3 range so targets stay coherent.
            if contour and (sum(contour) / len(contour)) >= max(4, self.syllable_min - 3):
                if 1 <= line_index <= len(contour):
                    c = contour[line_index - 1]
                    # Tight band: +1 at most, so the word budget cannot drift past
                    # the reference line into a ballad-length sentence.
                    return c, max(2, c - 2), c + 1
                avg = round(sum(contour) / len(contour))
                return avg, max(2, min(contour) - 1), max(contour) + 1
        if any(k in low for k in _SHORT_SECTION):
            return min(self.syllable_target, 6), 2, max(self.syllable_max, 10)
        return self.syllable_target, self.syllable_min, self.syllable_max

    def reference_line(self, section_name: str, line_index: int) -> str:
        """Text of the reference line this line is modelled on ('' if none)."""
        if not self.reference_song:
            return ""
        lines = (self.reference_section(section_name) or {}).get("lines") or []
        if 1 <= line_index <= len(lines):
            ln = lines[line_index - 1]
            return str(ln.get("text") if isinstance(ln, dict) else ln)
        return ""

    def reference_line_shape(self, section_name: str, line_index: int) -> str:
        """Structural cadence of the reference line, without its words.

        'House so empty, need a centerpiece' -> '3 words, 3 words' and
        'Then she clean it with her face, man, I love my baby, ah' ends on a tag.
        The model copies the rhythm (phrase split, tag), never the lyric.
        """
        text = re.sub(r"\([^)]*\)", "", self.reference_line(section_name, line_index)).strip()
        if not text:
            return ""
        tag_m = REF_TAG_RE.search(text)
        body = REF_TAG_RE.sub("", text) if tag_m else text
        parts = [p for p in re.split(r"\s*[,;:]\s*", body) if re.search(r"[A-Za-z]", p)]
        counts = [len(re.findall(r"[A-Za-z']+", p)) for p in parts]
        shape = ", ".join(f"{n} words" for n in counts) if len(counts) > 1 else ""
        if tag_m and self.delivery_tags():
            shape = (shape + ", " if shape else "") + f"then the tag '{tag_m.group(1).lower()}'"
        return shape

    def delivery_tags(self) -> list[str]:
        """Vocal end-tags the reference actually uses ('ah', 'yeah'), most used first.

        Empty unless enough reference lines end on one to make it part of the
        delivery rather than a one-off.
        """
        lines = [str(l.get("text") if isinstance(l, dict) else l)
                 for s in (self.reference_song or {}).get("sections") or [] if isinstance(s, dict)
                 for l in s.get("lines") or []]
        if not lines:
            return []
        tags = Counter(m.group(1).lower() for l in lines if (m := REF_TAG_RE.search(re.sub(r"\([^)]*\)", "", l).strip())))
        if sum(tags.values()) / len(lines) < REF_TAG_MIN_RATIO:
            return []
        return [t for t, _ in tags.most_common(3)]

    # --------------------------------------------------------- construction

    @field_serializer("created_at", "updated_at", when_used="json")
    def serialize_timestamp(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @model_validator(mode="before")
    @classmethod
    def read_document(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        # Derived exports are read-only, never another editable value.
        for name in cls.model_computed_fields:
            data.pop(name, None)
        answers = data.get("answers")
        if isinstance(answers, dict):
            questions = list(data.get("questions") or [])
            question_ids = {q.get("id") for q in questions if isinstance(q, dict)}
            normalized = {}
            for qid, answer in answers.items():
                if isinstance(answer, dict):
                    if answer.get("question") and qid not in question_ids:
                        questions.append({"id": qid, "question": answer["question"]})
                    answer = answer.get("answer")
                normalized[qid] = str(answer if answer is not None else "").strip()
            data["answers"] = normalized
            data["questions"] = questions
        return data

    @field_validator("section_plan", mode="before")
    @classmethod
    def read_section_plan(cls, value: Any) -> Any:
        return parse_section_plan(value) if isinstance(value, str) else value

    def resolve_inputs(self, template_defaults: dict[str, Any]) -> SongGeneration:
        """Resolve Steps 1–3 once on this instance, never when reading a saved song."""
        if not self.model.strip():
            raise ValueError("A model must be selected before generating.")
        provided = frozenset(self.model_fields_set)
        tmpl = template_defaults or {}
        ref = self.reference_song or {}
        ans = self.answers
        self.model = self.model.strip()
        self.title = self.title.strip() or "Untitled Song"

        self.bpm = (_first_int(ans.get("bpm"))
                    or (self.bpm if "bpm" in provided else None)
                    or _first_int(ref.get("tempo_bpm") or ref.get("bpm"))
                    or _first_int(tmpl.get("bpm")) or 92)
        key_answer = ans.get("musical_key", "").split()
        self.key = ((key_answer[0] if key_answer else None)
                    or (self.key if "key" in provided else None)
                    or ref.get("musical_key") or ref.get("key") or tmpl.get("music_key") or "C")
        self.scale = ((key_answer[1].strip("()").lower() if len(key_answer) > 1 else None)
                      or ans.get("scale") or (self.scale if "scale" in provided else None)
                      or ref.get("scale") or tmpl.get("scale") or "major")
        self.genre = (ans.get("genre") or (self.genre if "genre" in provided else None)
                      or _clean_genre(self.style) or _clean_genre(tmpl.get("genre"))
                      or _clean_genre(ref.get("genre")) or "Pop")
        self.subgenre = (ans.get("subgenre") or self.subgenre
                         or tmpl.get("subgenre") or "")
        self.language = ans.get("language") or self.language or tmpl.get("language") or "English"
        for name, answer_ids in (("moods", ("moods", "mood", "target_mood")),
                                 ("instruments", ("instruments", "instrumentation"))):
            answer = next((ans[k] for k in answer_ids if ans.get(k)), "")
            if answer:
                setattr(self, name, [v.strip() for v in re.split(r"[,;\n]", answer) if v.strip()])
            elif not getattr(self, name) and tmpl.get(name):
                value = tmpl[name]
                setattr(self, name, value if isinstance(value, list) else
                        [v.strip() for v in str(value).split(",") if v.strip()])
        self.time_signature = (ans.get("time_signature")
                               or (self.time_signature if "time_signature" in provided else None)
                               or ref.get("time_signature") or tmpl.get("time_signature") or "4/4")
        if ans.get("duration_hint_sec"):
            self.duration_hint_sec = _first_int(ans["duration_hint_sec"])
        elif self.duration_hint_sec is None and tmpl.get("duration_hint_sec") is not None:
            self.duration_hint_sec = int(tmpl["duration_hint_sec"])
        self.vocal = (ans.get("vocal") or ans.get("vocal_style")
                      or (self.vocal if "vocal" in provided else None)
                      or tmpl.get("vocal") or "melodic lead")
        if self.vocal.lower() == "duet" and not self.vocal_roles:
            self.vocal_roles = {"singer_a": "Singer A (Male Vocal)", "singer_b": "Singer B (Female Vocal)"}

        def follows(qid: str) -> bool:
            return ans.get(qid, "").lower().startswith(FOLLOW_REFERENCE.lower())

        ref_sections = [s for s in ref.get("sections") or [] if isinstance(s, dict)]
        ref_names = [str(s.get("name") or "Section") for s in ref_sections]
        if follows("section_plan") and ref_names:
            self.section_plan = ref_names
        else:
            self.section_plan = parse_section_plan(ans.get("section_plan")) or self.section_plan or ref_names
        self.section_plan = self.section_plan or parse_section_plan(tmpl.get("section_plan") or "standard")

        answer = ans.get("section_lines", "")
        if answer:
            self.follow_reference_lines = bool(ref_sections) and follows("section_lines")
        elif "follow_reference_lines" not in provided:
            self.follow_reference_lines = bool(ref_sections) and "section_lines" not in provided
        self.section_lines = max(2, min(16, 4 if follows("section_lines") else
                                       (_first_int(answer) or self.section_lines)))
        answer = ans.get("lines_per_turn", "").lower()
        if any(word in answer for word in ("whole", "section", "entire")):
            self.lines_per_turn = 0
        elif "1" in answer:
            self.lines_per_turn = 1
        elif answer:
            self.lines_per_turn = 2
        answer = ans.get("validation_mode", "").lower()
        if answer:
            self.validation_mode = ("human" if "human" in answer else
                                    "auto_songwriter" if "songwriter" in answer else "auto_llm")
        elif "validation_mode" not in provided:
            self.validation_mode = "auto_llm"
        answer = ans.get("correction_strictness", "").lower()
        if answer:
            self.correction_strictness = ("draft" if any(v in answer for v in ("draft", "light", "low")) else
                                         "polished" if any(v in answer for v in ("polish", "strict", "high")) else "balanced")
        if self.max_retries == 5:
            self.max_retries = {"draft": 2, "polished": 6}.get(self.correction_strictness, 4)

        if follows("syllable_target"):
            self.follow_reference_syllables = bool(ref_sections)
        else:
            syllables = parse_syllable_range(ans.get("syllable_target"))
            if syllables:
                self.follow_reference_syllables = False
            elif "follow_reference_syllables" not in provided:
                self.follow_reference_syllables = bool(ref_sections) and not (
                    {"syllable_min", "syllable_max"} & provided)
            if not syllables and not ({"syllable_min", "syllable_max"} & provided):
                syllables = parse_syllable_range(tmpl.get("syllable_target"))
            if syllables:
                self.syllable_min, self.syllable_max = syllables
        if follows("rhyme_scheme"):
            self.rhyme_scheme = "free"
            self.follow_reference_rhyme = bool(ref_sections)
        else:
            scheme = ans.get("rhyme_scheme", "").split(" ")[0]
            if scheme:
                self.follow_reference_rhyme = False
            elif "follow_reference_rhyme" not in provided:
                self.follow_reference_rhyme = bool(ref_sections) and "rhyme_scheme" not in provided
            self.rhyme_scheme = scheme or (self.rhyme_scheme if "rhyme_scheme" in provided else None) or tmpl.get("rhyme_scheme") or "free"

        register = ans.get("rhyme_register", "").lower()
        offensive = ans.get("offensive_language", "").lower()
        phrases = ans.get("allow_phrases", "").lower()
        if register:
            self.allow_slang = "slang" in register or "vernacular" in register or "all" in register
        if offensive or register:
            self.allow_offensive = any(v in offensive for v in ("explicit", "raw", "mild")) or "explicit" in register
        if phrases:
            self.allow_phrases = "yes" in phrases or "include" in phrases
        banned = ans.get("banned", "").lower()
        extra_banned = [pair for pair in self.banned_pairs if pair not in DEFAULT_BANNED] if banned else []
        if "none" in banned:
            self.banned_pairs = extra_banned
        elif "top-3" in banned or "essential" in banned:
            self.banned_pairs = list(DEFAULT_BANNED[:3]) + extra_banned
        elif banned:
            self.banned_pairs = list(DEFAULT_BANNED) + extra_banned
        elif "banned_pairs" not in provided:
            self.banned_pairs = list(tmpl.get("banned_words", DEFAULT_BANNED))
        if not self.chords:
            self.chords = [str(c) for c in ref.get("chords") or []]
        if not self.abc_score:
            self.abc_score = ref.get("abc_score") or ""
        if self.template_id is None:
            self.template_id = tmpl.get("template_id")
        return self

    def start_draft(self) -> SongGeneration:
        """Start new output on this song without erasing its production choices."""
        for name in (
            "status", "sections", "turns", "incomplete_sections", "analytics", "diagnostics",
            "engine_prompts", "engine_prompt_author", "engine_prompt_section_arc",
            "system_prompt", "user_prompt",
        ):
            setattr(self, name, type(self).model_fields[name].get_default(call_default_factory=True))
        return self

    # ------------------------------------------------------------ persistence

    # Row lifecycle facts live in their own DB columns, not in the JSON,
    # so each is stored exactly once.
    ROW_FIELDS: ClassVar[tuple[str, ...]] = (
        "song_id", "revision", "status", "created_at", "updated_at", "take_count", "rating", "notes",
    )

    def to_record(self) -> dict[str, Any]:
        """Persist each fact once. Lyrics and row lifecycle values are not copied."""
        return self.model_dump(mode="json", exclude=set(self.ROW_FIELDS), exclude_computed_fields=True)

    @classmethod
    def from_record(cls, rec: dict[str, Any], **row: Any) -> SongGeneration:
        """Load the stored document without resolving inputs or authoring anything."""
        data = dict(rec or {})
        for name, value in row.items():
            if value is not None or name not in data:
                data[name] = value
        song = cls.model_validate(data)
        if song.song_id:
            song.take_group = song.take_group or song.song_id
            if song.take_number <= 0:
                song.take_number = 1
        return song
