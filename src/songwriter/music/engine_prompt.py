"""Engine prompt construction for YuE2 and MiniMax Music 3.

The lyrics are half the deliverable; the engine prompt is the other half. This
module builds both from resolved production values, with the model authoring the
prose and the template segments supplied to it as *input material* rather than
copied verbatim.

Two facts from the template table shape the design:

  * `basic_attributes` is already in MiniMax's wire format
    ("bpm is 75. key is Eb, and scale is major. A Cappella / Choral Pop."), so the
    attributes line is rebuilt deterministically from resolved values instead of
    lifted from the row. A lifted line carries the template's own BPM and
    contradicts the spec the lyrics were written to.
  * `groove_foundation` is overloaded: 4,979 of 5,960 rows hold a section arc
    ("Suggested arc [Intro] [Verse 1] [Hook]...") rather than a groove
    description. Copied into a style string it produces
    "English, 92 BPM, ..., Suggested arc [Intro] [Verse 1] [Hook]". `split_groove`
    separates the two readings.

Field coverage is uneven (vocal_style/vocal_fx/harmony_backing exist for 981 of
5,960 templates; sonics_production for 2,494), so every field degrades
independently and a missing input never blanks the whole prompt.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

# MiniMax section order, matching the engine's documented prompt layout.
MINIMAX_LAYOUT: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        "Global Metadata",
        (
            ("basic_attributes", "Basic Attributes"),
            ("emotional_progression", "Global Emotional Progression"),
            ("application_scenarios", "Application Scenarios & Imagery"),
            ("sonics_production", "Sonics & Production Profile"),
        ),
    ),
    (
        "Vocal Details",
        (
            ("vocal_gender_timbre", "Vocal Gender & Timbre"),
            ("vocal_style", "Vocal Style"),
            ("harmony_backing", "Harmony/Backing Vocals"),
            ("vocal_fx", "Vocal FX"),
        ),
    ),
    (
        "Arrangement",
        (
            ("instrument_lifecycle", "Instrument Lifecycle Description (Primary/Secondary Layering)"),
            ("groove_foundation", "Groove & Foundation Progression"),
            ("embellishments", "Embellishments, Textures & Spatial FX"),
        ),
    ),
)

VOCAL_FIELDS = ("vocal_gender_timbre", "vocal_style", "harmony_backing", "vocal_fx")

_ARC_RE = re.compile(r"suggested arc\b", re.I)
_SECTION_TAG_RE = re.compile(r"\[[^\]]+\]")
_BPM_RE = re.compile(r"\b\d{2,3}\s*bpm\b", re.I)


def split_groove(content: str | None) -> tuple[str, str]:
    """Separate a groove description from a section arc.

    `groove_foundation` holds either, and 83% of rows hold the arc. Returns
    (groove_prose, section_arc); either may be empty.
    """
    if not content:
        return "", ""
    text = content.strip()
    match = _ARC_RE.search(text)
    if not match:
        return text, ""

    arc_part = text[match.start():]
    groove_part = text[: match.start()].strip()
    # Keep only the bracketed tags from the arc; the surrounding words add nothing.
    tags = _SECTION_TAG_RE.findall(arc_part)
    return groove_part, " ".join(tags)


def basic_attributes(bpm: int, key: str, scale: str, genre: str) -> str:
    """The attributes line in MiniMax's exact wire format."""
    return f"bpm is {bpm}. key is {key}, and scale is {scale}. {genre}."


def retune_tempo(text: str, bpm: int) -> str:
    """Rewrite any tempo a template's prose states to the resolved tempo.

    Segment prose sometimes names the template's own BPM ("...tempo 92 BPM,
    suited to..."). Carried into the engine prompt beside a rebuilt attributes
    line, it states two different tempos in one prompt.
    """
    if not text:
        return text
    return _BPM_RE.sub(f"{bpm} BPM", text)


def template_inputs(segments: dict[str, dict[str, str]]) -> dict[str, str]:
    """Flatten template segments into the field names the layout uses.

    Section arcs are separated out of `groove_foundation` so they cannot leak into
    a groove description or a style tag.
    """
    flat: dict[str, str] = {}
    for section in segments.values():
        for field, content in section.items():
            if content and content.strip():
                flat[field] = content.strip()

    groove, arc = split_groove(flat.get("groove_foundation"))
    flat["groove_foundation"] = groove
    if arc:
        flat["section_arc"] = arc
    elif "section_arc" in flat:
        del flat["section_arc"]

    if not flat.get("instrument_lifecycle"):
        primary = flat.get("primary_layer", "")
        secondary = flat.get("secondary_layer", "")
        parts = []
        if primary:
            parts.append(f"Primary: {primary}")
        if secondary:
            parts.append(f"Secondary: {secondary}")
        if parts:
            flat["instrument_lifecycle"] = "\n".join(parts)

    return {k: v for k, v in flat.items() if v}


def render_minimax(fields: dict[str, str], instrumental: bool = False) -> str:
    """Assemble the sectioned MiniMax prompt, skipping fields with no content."""
    blocks: list[str] = []
    for heading, spec in MINIMAX_LAYOUT:
        if instrumental and heading == "Vocal Details":
            continue
        lines: list[str] = []
        for key, label in spec:
            value = (fields.get(key) or "").strip()
            if not value:
                continue
            # The lifecycle field carries its own Primary:/Secondary: lines.
            if key == "instrument_lifecycle" and "\n" in value:
                lines.append(f"{label}:")
                lines.extend(l.strip() for l in value.splitlines() if l.strip())
            else:
                lines.append(f"{label}: {value}")
        if lines:
            blocks.append(heading + "\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def render_yue2(tags: list[str], bpm: int) -> str:
    """Assemble the single-line YuE2 style string.

    YuE2 takes comma-separated tags, not prose. The tempo tag is always the
    resolved one: a model-authored tag may fuse a tempo with other words
    ("167 BPM psychological intensity"), which reads as neither a clean tempo
    marker nor a clean descriptor.
    """
    cleaned = clean_yue2_tags(tags)
    cleaned.append(f"{bpm} BPM")
    return ", ".join(cleaned)


def render_songbench(
    genre: str,
    mood: str | None = None,
    lead: str | None = None,
    bed: str | None = None,
    groove: str | None = None,
    vocal: str | None = None,
    bpm: int | None = None,
) -> str:
    """Assemble the 6-aspect SongBench-optimized audio prompt.

    Formula: [Genre / Subgenre], [Dominant Mood], [Lead Instrument], [Harmonic Bed], [Groove / Rhythm], [Vocal], [BPM]
    Demonstrated by Tencent SongBench (2026) to maximize musicality and structure scores across modern AI music backends.
    """
    tokens: list[str] = []
    g = genre.strip() if genre else "Pop"
    tokens.append(g)

    if mood:
        clean_m = mood.strip().capitalize()
        if clean_m and clean_m.lower() not in g.lower():
            tokens.append(clean_m)

    if lead:
        clause = lead.split(".")[0].strip()
        clean_lead = re.sub(r"^(a|an|the)\s+", "", clause, flags=re.I).strip()
        clean_lead = re.sub(r"\s+serves as.*|\s+features.*|\s+acts as.*|\s+provides.*", "", clean_lead, flags=re.I).strip()
        if 1 <= len(clean_lead.split()) <= 6:
            tokens.append(clean_lead.title())

    if bed:
        clause = bed.split(".")[0].strip()
        clean_bed = re.sub(r"^(a|an|the)\s+", "", clause, flags=re.I).strip()
        if 1 <= len(clean_bed.split()) <= 6:
            tokens.append(clean_bed.title())

    if groove:
        clean_g, _ = split_groove(groove)
        if clean_g:
            clause = clean_g.split(".")[0].strip()
            clean_groove = re.sub(
                r"^(a|an|the|the\s+rhythm\s+section\s+(is\s+driven\s+by|begins\s+with|is\s+anchored\s+by|establishes))\s+",
                "",
                clause,
                flags=re.I,
            ).strip()
            clean_groove = re.sub(r"\s+featuring.*", "", clean_groove, flags=re.I).strip()
            if 1 <= len(clean_groove.split()) <= 6:
                tokens.append(clean_groove.title())

    if vocal:
        first_clause = vocal.split(".")[0].strip()
        low = first_clause.lower()
        if "duet" in low or ("male" in low and "female" in low):
            tokens.append("Male and Female Duet Vocals")
        elif "instrumental" in low or "no lead" in low:
            tokens.append("Instrumental")
        elif "vocal" in low or "voice" in low:
            tokens.append(first_clause)
        else:
            tokens.append(f"{first_clause} vocals")

    if bpm:
        tokens.append(f"{bpm} BPM")

    return ", ".join(dict.fromkeys(tokens))

def clean_yue2_tags(tags: list[str]) -> list[str]:
    """Split to atomic tags, drop prose, arcs, tempo markers and duplicates.

    Models return tags with embedded commas and fused clauses, so each entry is
    split before it is judged. Tempo is dropped here and reapplied by
    `render_yue2` from the resolved spec.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        for piece in re.split(r"[,;]", str(raw or "")):
            tag = re.sub(r"\s+", " ", piece).strip().strip(".,;:")
            if not tag:
                continue
            if _ARC_RE.search(tag) or _SECTION_TAG_RE.search(tag):
                continue
            # Strip a tempo marker; a leftover descriptor is still usable.
            if _BPM_RE.search(tag):
                tag = _BPM_RE.sub(" ", tag).strip().strip(".,;:")
                tag = re.sub(r"\s+", " ", tag)
                if not tag:
                    continue
            # A tag is a phrase, not a sentence.
            if len(tag.split()) > 6 or "." in tag or tag.endswith(("!", "?")):
                continue
            low = tag.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(tag)
    return out


class EnginePromptBuilder:
    """Builds engine prompts, with the model authoring prose from the brief.

    `chat` is injected so this module holds no transport concerns and stays
    testable without a network call.
    """

    def __init__(self, chat: Callable[..., str] | None = None) -> None:
        self.chat = chat

    def build(
        self,
        spec: dict[str, Any],
        segments: dict[str, dict[str, str]] | None = None,
        brief: str = "",
        answers: dict[str, str] | None = None,
        model: str | None = None,
        use_model: bool = True,
    ) -> dict[str, Any]:
        """Return both engine prompts plus the field values behind them.

        Deterministic scaffolding always applies: the attributes line is rebuilt
        from `spec`, so both prompts agree with the lyric specification even when
        the model call fails.
        """
        inputs = template_inputs(segments or {})
        vocal = str(spec.get("vocal") or "").strip()
        instrumental = vocal.lower() == "instrumental"
        resolved_bpm = int(spec.get("bpm") or 90)

        attributes = basic_attributes(
            bpm=resolved_bpm,
            key=str(spec.get("music_key") or "C"),
            scale=str(spec.get("scale") or "major"),
            genre=str(spec.get("genre") or "Pop"),
        )
        # Template prose may name the template's own tempo; reconcile it so the
        # prompt never states two different tempos.
        fields: dict[str, str] = {
            k: retune_tempo(v, resolved_bpm) for k, v in inputs.items()
        }
        fields["basic_attributes"] = attributes

        authored: dict[str, Any] | None = None
        if use_model and self.chat is not None:
            authored = self._author(spec, inputs, brief, answers or {}, model, instrumental)

        if authored:
            for key, value in (authored.get("minimax") or {}).items():
                text = str(value or "").strip()
                if text:
                    fields[key] = text
            # The attributes line is never model-authored: it must match the spec.
            fields["basic_attributes"] = attributes

        if instrumental:
            for key in VOCAL_FIELDS:
                fields.pop(key, None)
        elif vocal.lower() == "duet":
            if not fields.get("vocal_gender_timbre") or "singer" not in fields.get("vocal_gender_timbre", "").lower():
                fields["vocal_gender_timbre"] = (
                    "Singer A (Male): warm, resonant baritone with emotional depth. "
                    "Singer B (Female): clear, expressive mezzo-soprano with airy highs."
                )
            if not fields.get("vocal_style") or "duet" not in fields.get("vocal_style", "").lower():
                fields["vocal_style"] = (
                    "Narrative conversational verses trading between Singer A and Singer B, "
                    "swelling into full dual-vocal harmony in the choruses."
                )
            if not fields.get("harmony_backing") or "harmony" not in fields.get("harmony_backing", "").lower():
                fields["harmony_backing"] = (
                    "Tight two-part vocal harmonies across all choruses and the outro, "
                    "blending both lead timbres."
                )
        # Augmentation layer: If the template is sparse (lacks vocal details, instrument lifecycle,
        # or global scenarios), augment with verified genre-specific MiniMax M3 architecture.
        fields = self._augment_sparse_fields(fields, spec, inputs, instrumental)

        yue2_tags = (authored or {}).get("yue2") or []
        if not yue2_tags:
            yue2_tags = self._fallback_tags(spec, inputs, instrumental)
        songbench_prompt = render_songbench(
            genre=str(spec.get("genre") or "Pop"),
            mood=spec.get("target_mood") or (answers or {}).get("emotional_attitude"),
            lead=inputs.get("primary_layer"),
            bed=inputs.get("secondary_layer") or inputs.get("embellishments"),
            groove=inputs.get("groove_foundation"),
            vocal=vocal,
            bpm=resolved_bpm,
        )

        return {
            "minimax": render_minimax(fields, instrumental=instrumental),
            "yue2": render_yue2(list(yue2_tags), resolved_bpm),
            "songbench": songbench_prompt,
            "fields": fields,
            "section_arc": inputs.get("section_arc", ""),
            "authored_by": "model" if authored else "template",
        }

    # ------------------------------------------------------------- authoring

    def _author(
        self,
        spec: dict[str, Any],
        inputs: dict[str, str],
        brief: str,
        answers: dict[str, str],
        model: str | None,
        instrumental: bool,
    ) -> dict[str, Any] | None:
        """Ask the model to write the engine prompt for *this* song."""
        wanted = [
            (key, label)
            for _, group in MINIMAX_LAYOUT
            for key, label in group
            if key != "basic_attributes"
            and not (instrumental and key in VOCAL_FIELDS)
        ]

        reference = "\n".join(
            f"- {key}: {value[:400]}"
            for key, value in inputs.items()
            if key not in ("section_arc",)
        ) or "- none available"

        decisions = "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in answers.items() if v)

        field_list = "\n".join(f'  "{key}"  -- {label}' for key, label in wanted)

        prompt = f"""Write the production prompt for an AI music engine for this song.

SONG BRIEF
{brief.strip() or 'An original song.'}

CONFIRMED PRODUCTION SPEC
- tempo: {spec.get('bpm')} BPM
- key: {spec.get('music_key')} {spec.get('scale')}
- vocal: {'instrumental, no vocals' if instrumental else ('Duet (Singer A Male and Singer B Female with trading verses and chorus harmonies)' if str(spec.get('vocal', '')).lower() == 'duet' else spec.get('vocal'))}

WRITER'S DECISIONS
{decisions or '- none recorded'}

REFERENCE MATERIAL from a comparable production template. Use it for register and
level of detail. Describe THIS song; do not copy these sentences.
{reference}

Produce two things.

1. "minimax": one entry per field below. Each is 2-4 sentences of specific
   production description written for this brief: name instruments, frequency
   character, spatial treatment and how the arrangement changes across sections.
   For "instrument_lifecycle" write two lines exactly, starting "Primary:" and
   "Secondary:".
{field_list}

2. "yue2": 6-9 short descriptive tags for the same song, each 1-5 words, no
   sentences and no section names. Cover language, genre, voice character, key
   instruments, melodic character and phrasing feel.
   Example shape: ["English", "warm piano pop", "expressive female voice",
   "acoustic piano", "rounded bass and light drums", "unhurried phrasing"]

Respond with JSON only:
{{"minimax": {{"<field>": "<text>"}}, "yue2": ["tag", "tag"]}}"""

        try:
            raw = self.chat(
                [{"role": "user", "content": prompt}],
                model=model,
                temperature=0.5,
                max_tokens=1600,
            )
        except Exception:
            return None

        data = _extract_json(raw)
        if not isinstance(data, dict):
            return None

        minimax = data.get("minimax")
        yue2 = data.get("yue2")
        if not isinstance(minimax, dict):
            minimax = {}
        if not isinstance(yue2, list):
            yue2 = []

        allowed = {key for key, _ in wanted}
        minimax = {k: v for k, v in minimax.items() if k in allowed and isinstance(v, str)}
        if not minimax and not yue2:
            return None
        return {"minimax": minimax, "yue2": [t for t in yue2 if isinstance(t, str)]}
    @staticmethod
    def _augment_sparse_fields(
        fields: dict[str, str],
        spec: dict[str, Any],
        inputs: dict[str, str],
        instrumental: bool,
    ) -> dict[str, str]:
        """Consolidate and augment missing or sparse fields to meet MiniMax M3 wire standards.
        
        Preserves existing rich prose if present, but synthesizes high-fidelity 
        vocal, arrangement, and spatial details if the template is sparse.
        """
        genre = str(spec.get("genre") or "Pop").strip()
        bpm = int(spec.get("bpm") or 90)
        vocal = str(spec.get("vocal") or "melodic lead").strip()
        is_duet = vocal.lower() == "duet"
        g_low = genre.lower()

        # 1. Global Metadata: Application Scenarios & Sonics Profile
        if not fields.get("application_scenarios") or len(fields.get("application_scenarios", "")) < 25:
            if any(k in g_low for k in ["jazz", "soul", "blues", "noir"]):
                fields["application_scenarios"] = "A dimly lit club late at night, solitary reflection in a classic film noir, or watching streetlights blur across rain-soaked windows."
            elif any(k in g_low for k in ["hip-hop", "trap", "rap", "boom bap"]):
                fields["application_scenarios"] = "Late-night urban driving, nocturnal contemplation, studio isolation, or cinematic scenes of ambition and focus."
            elif any(k in g_low for k in ["folk", "acoustic", "singer-songwriter", "americana"]):
                fields["application_scenarios"] = "Open highway travel at twilight, intimate fireside storytelling, cabin solitude, or porch acoustic performance."
            elif any(k in g_low for k in ["rock", "metal", "punk"]):
                fields["application_scenarios"] = "High-octane stadium performance, driving fast down open highways, anthemic rebellion, or live club energy."
            else:
                fields["application_scenarios"] = "Late-night highway travel, cinematic modern storytelling, emotional crossroads, or festival main stage energy."

        if not fields.get("sonics_production") or len(fields.get("sonics_production", "")) < 25:
            fields["sonics_production"] = (
                f"The production features a spacious, wide stereo soundstage with punchy low-end presence "
                f"and clear high-frequency articulation. The dynamic range breathes naturally, balancing "
                f"focused center vocals with wide atmospheric stereo textures."
            )

        # 2. Vocal Details (if not instrumental)
        if not instrumental:
            vt = fields.get("vocal_gender_timbre", "").strip()
            if not vt or "unspecified" in vt.lower() or len(vt) < 30:
                if is_duet:
                    fields["vocal_gender_timbre"] = "Singer A (Male) and Singer B (Female) trading vocal roles, blending baritone warmth with bright expressive highs."
                elif any(k in g_low for k in ["jazz", "soul"]):
                    fields["vocal_gender_timbre"] = "A smoky, breathy vocal texture with velvety mid-range warmth and intimate mic presence."
                elif any(k in g_low for k in ["hip-hop", "trap", "rap", "boom bap"]):
                    fields["vocal_gender_timbre"] = "A focused, mid-range delivery with crisp articulation and conversational rhythmic presence."
                else:
                    fields["vocal_gender_timbre"] = f"A clear, emotive lead vocal with melodic warmth and expressive dynamic control."

            vs = fields.get("vocal_style", "")
            if not vs or len(vs) < 20:
                if any(k in g_low for k in ["hip-hop", "trap", "rap"]):
                    fields["vocal_style"] = "Melodic cadence blending rhythmic conversational phrasing with subtle melodic inflection on line endings."
                else:
                    fields["vocal_style"] = "Conversational verses with subtle breath inflections, expanding into resonant, soaring projection in the chorus."

            if not fields.get("vocal_fx") or len(fields.get("vocal_fx", "")) < 15:
                if any(k in g_low for k in ["trap", "synth-pop", "edm"]):
                    fields["vocal_fx"] = "Stylized pitch-correction sheen, stereo ping-pong delay on vocal throws, and a clean medium hall reverb."
                else:
                    fields["vocal_fx"] = "Warm plate reverb, subtle opto-compression for smooth dynamic leveling, and intimate close-mic proximity."

        # 3. Arrangement: Instrument Lifecycle & Groove Progression
        il = fields.get("instrument_lifecycle", "")
        if not il or "Primary:" not in il or len(il) < 40:
            primary_lead = retune_tempo(inputs.get("primary_layer") or "A central melodic motif on piano or synth", bpm)
            secondary_bed = retune_tempo(inputs.get("secondary_layer") or inputs.get("embellishments") or "Bass foundation and harmonic pads", bpm)
            fields["instrument_lifecycle"] = (
                f"Primary: {primary_lead.split('.')[0].strip()}.\n"
                f"Secondary: {secondary_bed.split('.')[0].strip()} entering to support dynamic lifts."
            )

        gf = fields.get("groove_foundation", "").strip()
        if not gf or len(gf) < 20:
            if any(k in g_low for k in ["trap", "hip-hop", "boom bap"]):
                fields["groove_foundation"] = f"A solid kick and crisp snare on the backbeat, driven by fast rolling hi-hat patterns at {bpm} BPM."
            elif any(k in g_low for k in ["jazz", "ballad"]):
                fields["groove_foundation"] = f"A gentle brushed drum pattern and walking upright bass pulse maintaining a relaxed {bpm} BPM groove."
            elif any(k in g_low for k in ["rock"]):
                fields["groove_foundation"] = f"Driving acoustic kick and snare backbeat anchored by an electric bass guitar locked to {bpm} BPM."
            else:
                fields["groove_foundation"] = f"A steady rhythmic pulse at {bpm} BPM providing continuous forward momentum across sections."

        if not fields.get("embellishments") or len(fields.get("embellishments", "")) < 20:
            fields["embellishments"] = "Subtle transitional risers, soft cymbal swells, and stereo spatial delays marking section boundaries."

        return fields


    @staticmethod
    def _fallback_tags(
        spec: dict[str, Any], inputs: dict[str, str], instrumental: bool
    ) -> list[str]:
        """Tags distilled from resolved values when the model supplies none."""
        tags = ["English"]
        genre = str(spec.get("genre") or "").strip()
        if genre:
            tags.extend(part.strip() for part in genre.split("/") if part.strip())

        if instrumental:
            tags.append("instrumental")
        else:
            vocal = str(spec.get("vocal") or "").strip()
            if vocal:
                low = vocal.lower()
                if low == "duet":
                    tags.extend(["duet", "male and female vocals", "vocal harmony"])
                else:
                    tags.append(vocal if ("vocal" in low or "voice" in low) else f"{vocal} vocal")

        # First clause of the primary layer names the lead instrument.
        primary = inputs.get("primary_layer", "")
        if primary:
            clause = primary.split(".")[0]
            head = re.sub(r"^(a|an|the)\s+", "", clause, flags=re.I).strip()
            if 1 <= len(head.split()) <= 5:
                tags.append(head)

        key = str(spec.get("music_key") or "").strip()
        scale = str(spec.get("scale") or "").strip()
        if key and scale:
            tags.append(f"{key} {scale}")
        return tags


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


def format_lyrics_for_engine(lyrics: str, engine: str = "yue2") -> str:
    """Format section tags according to the target engine conventions.

    YuE2 expects Title/Camel Case: [Intro], [Verse 1], [Pre-Chorus], [Chorus], [Post-Chorus], [Bridge], [Instrumental], [Solo], [Outro].
    MiniMax Music 3 expects lowercase: [intro], [verse 1], [pre-chorus], [chorus], [post-chorus], [bridge], [instrumental], [solo], [outro].
    """
    def replace_tag(match: re.Match) -> str:
        tag_content = match.group(1).strip()
        if engine.lower() == "minimax":
            return f"[{tag_content.lower()}]"
        else:
            words = tag_content.split()
            titled = " ".join(w.capitalize() if not w.startswith("-") else w for w in words)
            titled = re.sub(r"\bPre-chorus\b", "Pre-Chorus", titled, flags=re.I)
            titled = re.sub(r"\bPost-chorus\b", "Post-Chorus", titled, flags=re.I)
            return f"[{titled}]"

    return re.sub(r"^\[([^\]]+)\]", replace_tag, lyrics or "", flags=re.M)
