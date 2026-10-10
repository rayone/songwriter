"""SongGeneration: the single source of truth for one song.

Every setting a generation needs lives here exactly once. The UI fills it in
over Steps 1-4, the line-writing loop reads from it, and the whole object is
saved as one JSON document and loaded back into an identical object.

Rules:
  * one field per fact -- no ``final_*`` / ``sec_*`` copies anywhere else;
  * priority is applied once, in ``from_request``:
        Step 3 answer -> Step 2 control -> reference song -> template default;
  * per-section / per-line syllable windows are computed on demand by
    ``line_window`` and never stored.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from typing import Any

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
    "vocal", "vocal_type", "arrangement", "bpm", "musical_key", "scale",
    "syllable_target", "rhyme_scheme", "section_plan", "section_lines", "lines_per_turn",
    "validation_mode", "required_tone", "correction_strictness",
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


@dataclass
class SongGeneration:
    # --- identity ----------------------------------------------------------
    song_id: str = ""
    revision: int = 0
    status: str = "draft"
    title: str = "Untitled Song"
    artist: str = ""
    brief: str = ""
    story: str = ""

    # --- model (user-selected, never hardcoded) ------------------------------
    model: str = ""

    # --- music ---------------------------------------------------------------
    bpm: int = 92
    key: str = "C"
    scale: str = "major"
    genre: str = "Pop"
    subgenre: str = ""
    style: str = ""
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
    # "auto": the validation function accepts lines; "human": the writer accepts each turn.
    validation_mode: str = "auto"

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

    # --- Step 4 output ---------------------------------------------------------
    sections: list[dict[str, Any]] = field(default_factory=list)
    abc_score: str = ""
    engine_prompts: dict[str, str] = field(default_factory=dict)
    engine_target: str = "standard"
    analytics: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------- derived

    @property
    def syllable_target(self) -> int:
        return (self.syllable_min + self.syllable_max) // 2

    @property
    def lyrics(self) -> str:
        parts: list[str] = []
        for sec in self.sections:
            parts.append(f"[{sec.get('name', 'Section')}]")
            parts.extend(sec.get("lines") or [])
            parts.append("")
        return "\n".join(parts).strip()

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

    @classmethod
    def from_request(
        cls,
        *,
        template_defaults: dict[str, Any],
        model: str,
        prompt: str = "",
        title: str | None = None,
        style: str | None = None,
        genre: str | None = None,
        template_id: int | None = None,
        bpm: int | float | None = None,
        music_key: str | None = None,
        scale: str | None = None,
        vocal: str | None = None,
        vocal_roles: dict[str, str] | None = None,
        reference_song: dict[str, Any] | None = None,
        banned_words: list[str] | None = None,
        rhyme_scheme: str | None = None,
        section_plan: str | None = None,
        syllable_target: str | None = None,
        answers: dict[str, Any] | None = None,
        questions: list[dict[str, Any]] | None = None,
        story: str | None = None,
        max_retries: int = 5,
        max_sections: int | None = None,
    ) -> SongGeneration:
        """Build the generation, applying priority exactly once per fact."""
        if not (model or "").strip():
            raise ValueError("A model must be selected before generating.")
        tmpl = template_defaults or {}
        ref = reference_song or {}

        ans: dict[str, str] = {}
        asked: dict[str, str] = {}
        for qid, item in (answers or {}).items():
            if isinstance(item, dict):
                ans[qid] = str(item.get("answer") or "").strip()
                if item.get("question"):
                    asked[qid] = str(item["question"])
            else:
                ans[qid] = str(item or "").strip()
        q_defs = [
            {
                "id": str(q["id"]),
                "question": str(q.get("question") or q["id"]),
                "why": str(q.get("why") or ""),
                "options": [str(o) for o in (q.get("options") or [])],
                "category": str(q.get("category") or "creative"),
            }
            for q in (questions or []) if isinstance(q, dict) and q.get("id")
        ] or [
            {"id": qid, "question": text, "why": "", "options": [ans.get(qid, "")], "category": "creative"}
            for qid, text in asked.items()
        ]

        # Music: Step 3 answer -> Step 2 control -> reference -> template.
        res_bpm = (_first_int(ans.get("bpm")) or (int(round(bpm)) if bpm else None)
                   or _first_int(ref.get("tempo_bpm") or ref.get("bpm"))
                   or _first_int(tmpl.get("bpm")) or 92)
        key_ans = ans.get("musical_key", "").split()
        res_key = (key_ans[0] if key_ans else None) or music_key or ref.get("musical_key") or tmpl.get("music_key") or "C"
        res_scale = ((key_ans[1].strip("()").lower() if len(key_ans) > 1 else None)
                     or scale or ref.get("scale") or tmpl.get("scale") or "major")
        res_genre = (_clean_genre(genre) or _clean_genre(ref.get("genre")) or _clean_genre(style)
                     or _clean_genre(tmpl.get("genre")) or "Pop")
        vocal_ans = ans.get("vocal", "").split(" ")[0].lower() if ans.get("vocal") else ""
        res_vocal = vocal_ans or vocal or tmpl.get("vocal") or "melodic lead"
        roles = dict(vocal_roles or {})
        if res_vocal == "duet" and not roles:
            roles = {"singer_a": "Singer A (Male Vocal)", "singer_b": "Singer B (Female Vocal)"}

        # Step 3 "Follow reference ..." answers keep that part of the reference's shape.
        # With no answer at all, a reference with sections is followed by default.
        def follows(qid: str) -> bool:
            return ans.get(qid, "").lower().startswith(FOLLOW_REFERENCE.lower())

        ref_sections = [s for s in ref.get("sections") or [] if isinstance(s, dict)]
        ref_names = [str(s.get("name") or "Section") for s in ref_sections]

        # Structure.
        if follows("section_plan") and ref_names:
            plan = ref_names
        else:
            plan = parse_section_plan(ans.get("section_plan")) or parse_section_plan(section_plan) or ref_names
        plan = plan or parse_section_plan(tmpl.get("section_plan") or "standard")

        # Lines per section.
        lines_ans = ans.get("section_lines", "")
        follow_lines = bool(ref_sections) and (follows("section_lines") or not lines_ans)
        sec_lines = 4 if follows("section_lines") else (_first_int(lines_ans) or 4)
        sec_lines = max(2, min(16, sec_lines))

        # How many lines the model drafts per turn.
        lpt_ans = ans.get("lines_per_turn", "").lower()
        if "whole" in lpt_ans or "section" in lpt_ans or "entire" in lpt_ans:
            lines_per_turn = 0
        elif "1" in lpt_ans:
            lines_per_turn = 1
        else:
            lines_per_turn = 2

        # Who accepts each turn: "auto_llm" (hybrid), "auto_songwriter" (rules only), or "human".
        v_ans = ans.get("validation_mode", "").lower()
        if "human" in v_ans:
            validation_mode = "human"
        elif "songwriter" in v_ans:
            validation_mode = "auto_songwriter"
        else:
            validation_mode = "auto_llm"
        # How aggressively the validator corrects/reinforms the model. The tier
        # sets the retry budget and which checks run (see stream loop).
        cs_ans = ans.get("correction_strictness", "").lower()
        if "draft" in cs_ans or "light" in cs_ans or "low" in cs_ans:
            correction_strictness, cs_retries = "draft", 2
        elif "polish" in cs_ans or "strict" in cs_ans or "high" in cs_ans:
            correction_strictness, cs_retries = "polished", 6
        else:
            correction_strictness, cs_retries = "balanced", 4
        # The tier owns the retry budget unless the caller passed a non-default.
        if max_retries == 5:
            max_retries = cs_retries

        # Meter.
        if follows("syllable_target"):
            user_range = None
            follow_syl = bool(ref_sections)
        else:
            user_range = parse_syllable_range(ans.get("syllable_target")) or parse_syllable_range(syllable_target)
            follow_syl = bool(ref_sections) and user_range is None
        syl_min, syl_max = user_range or parse_syllable_range(tmpl.get("syllable_target")) or (8, 10)

        # Rhyme scheme.
        if follows("rhyme_scheme"):
            scheme = "free"
            follow_rhyme = bool(ref_sections)
        else:
            scheme = ans.get("rhyme_scheme", "").split(" ")[0] or rhyme_scheme or ""
            follow_rhyme = bool(ref_sections) and not scheme
            scheme = scheme or tmpl.get("rhyme_scheme") or "free"

        # Register & filters.
        reg = ans.get("rhyme_register", "").lower()
        off = ans.get("offensive_language", "").lower()
        phr = ans.get("allow_phrases", "").lower()
        banned_ans = ans.get("banned", "").lower()
        extra_banned = [b.strip() for b in (banned_words or []) if str(b).strip()]
        if "none" in banned_ans:
            banned: list[str] = list(extra_banned)
        elif "top-3" in banned_ans or "essential" in banned_ans:
            banned = ["night/light", "fire/desire", "heart/apart"] + extra_banned
        elif banned_ans:
            banned = list(DEFAULT_BANNED) + extra_banned
        else:
            raw_b = banned_words if banned_words is not None else tmpl.get("banned_words", DEFAULT_BANNED)
            banned = [b.strip() for b in raw_b if str(b).strip()]

        # Only real chord data; the corpus has it for songs with imported MIDI.
        chords = [str(c) for c in ref.get("chords") or []]

        return cls(
            title=(title or "").strip() or "Untitled Song",
            brief=prompt or "",
            story=(story or "").strip(),
            model=model.strip(),
            bpm=int(res_bpm),
            key=str(res_key),
            scale=str(res_scale),
            genre=res_genre,
            style=style or "",
            vocal=res_vocal,
            vocal_roles=roles,
            time_signature=ref.get("time_signature") or "4/4",
            chords=chords,
            abc_score=ref.get("abc_score") or "",
            template_id=template_id if template_id is not None else tmpl.get("template_id"),
            reference_song=reference_song,
            section_plan=plan,
            max_sections=max_sections,
            syllable_min=syl_min,
            syllable_max=syl_max,
            follow_reference_syllables=follow_syl,
            follow_reference_lines=follow_lines,
            follow_reference_rhyme=follow_rhyme,
            section_lines=sec_lines,
            lines_per_turn=lines_per_turn,
            validation_mode=validation_mode,
            rhyme_scheme=scheme,
            allow_slang="slang" in reg or "vernacular" in reg or "all" in reg,
            allow_offensive="explicit" in off or "raw" in off or "mild" in off or "explicit" in reg,
            allow_phrases=("yes" in phr or "include" in phr or not phr),
            banned_pairs=banned,
            answers=ans,
            questions=q_defs,
            max_retries=max_retries,
            correction_strictness=correction_strictness,
        )

    # ------------------------------------------------------------ persistence

    # Row lifecycle facts live in their own DB columns, not in the JSON,
    # so each is stored exactly once.
    ROW_FIELDS = ("song_id", "revision", "status")

    def to_record(self) -> dict[str, Any]:
        """The JSON document stored in songs.generation (plus rendered lyrics for search)."""
        rec = asdict(self)
        for k in self.ROW_FIELDS:
            rec.pop(k, None)
        rec["lyrics"] = self.lyrics
        return rec

    @classmethod
    def from_record(cls, rec: dict[str, Any], **row: Any) -> SongGeneration:
        """Rebuild from the JSON document; ``row`` supplies song_id/revision/status."""
        names = {f.name for f in fields(cls)}
        data = {k: v for k, v in (rec or {}).items() if k in names}
        data.update({k: v for k, v in row.items() if k in names and v is not None})
        return cls(**data)
