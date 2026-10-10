"""MCP server exposing the workbench to any model client.

Tool design is tuned for small models:
  * few tools, each with one obvious job
  * small, pre-ranked results (no candidate dumps to filter)
  * verdict labels ('cliche', 'fresh') rather than raw numbers to interpret
  * text output, compact and scannable

Run:
    uv run python -m songwriter.mcp.server
"""

from __future__ import annotations
import anyio

from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from songwriter.lexicon.query import RhymeDB, RhymeType
from songwriter.service import Workbench

wb = Workbench()
server = Server("songwriter")

# The rebuilt rhyme dictionary. Optional so the other tools still work if it has
# not been built; find_rhymes reports that rather than failing opaquely.
try:
    rdb: RhymeDB | None = RhymeDB()
except FileNotFoundError:
    rdb = None


def _text(s: str) -> list[TextContent]:
    return [TextContent(type="text", text=s)]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="analyze_line",
            description=(
                "Syllable count, stress grid, prosody problems and internal "
                "rhymes for one line. Use before judging whether a line fits "
                "a meter -- do not count syllables yourself."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "the line"},
                    "beats": {"type": "integer", "default": 4,
                              "description": "beats per bar"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="find_rhymes",
            description=(
                "Rhymes for a word or phrase, from a verified phonetic "
                "dictionary including modern slang. Results are ranked by how "
                "often the word ends a line in 3M real songs, so the top "
                "entries are the singable ones. Each carries a commonness "
                "verdict (common, familiar, uncommon, rare) and flags for "
                "slang and phrases. Never judge rhyme by spelling yourself -- "
                "ask here."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "word": {"type": "string",
                             "description": "word or phrase to rhyme"},
                    "kind": {
                        "type": "string",
                        "default": "perfect",
                        "enum": [t.value for t in RhymeType],
                        "description": (
                            "perfect (cat/hat), rich (knight/night), "
                            "assonance (late/shape), consonance "
                            "(bridge/grudge), family (cat/cap), pararhyme "
                            "(flesh/flash), reverse (late/lake), mosaic "
                            "(phrase), eye (cough/rough), additive "
                            "(coal/cold), subtractive, alliteration, "
                            "editorial (human-asserted near rhyme)"
                        ),
                    },
                    "syllables": {
                        "type": "integer",
                        "description": "exact syllable count required",
                    },
                    "prefer": {
                        "type": "string", "default": "singable",
                        "enum": ["singable", "fresh", "close"],
                        "description": (
                            "singable = most used at line ends; "
                            "fresh = rare, for surprise; "
                            "close = deepest phonetic match"
                        ),
                    },
                    "limit": {"type": "integer", "default": 12},
                    "allow_phrases": {
                        "type": "boolean", "default": True,
                        "description": "include multi-word rhymes",
                    },
                    "exclude_tags": {
                        "type": "array", "items": {"type": "string"},
                        "default": [],
                        "description": (
                            "drop words carrying any of these register "
                            "labels: slang, informal, vulgar, offensive, "
                            "archaic, dialect, poetic, proper"
                        ),
                    },
                    "min_freq": {
                        "type": "integer", "default": 0,
                        "description": "minimum uses in the song corpus",
                    },
                },
                "required": ["word"],
            },
        ),
        Tool(
            name="word_sound",
            description=(
                "How a word is pronounced: phonemes, syllable count, stress "
                "pattern, the rime it rhymes on, and where the pronunciation "
                "came from. Use to check a word exists and how it sounds "
                "before building a line around it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "word": {"type": "string"},
                },
                "required": ["word"],
            },
        ),
        Tool(
            name="check_draft",
            description=(
                "Diagnose a draft: meter consistency, rhyme scheme and "
                "density, abstract vs sensory language, repeated lines, "
                "cliche phrases and stock rhyme pairs. Returns a specific "
                "issue list to fix. Run this before claiming a draft is done."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {"type": "array", "items": {"type": "string"}},
                    "kind": {"type": "string", "default": "verse",
                             "description": "verse, chorus, bridge, hook"},
                    "target_syllables": {
                        "type": "integer",
                        "description": "if set, flag lines off this count",
                    },
                },
                "required": ["lines"],
            },
        ),
        Tool(
            name="validate_form",
            description=(
                "Check a draft against a poetic form and get exact "
                "violations (e.g. 'line 3 has 8 syllables, needs 7'). "
                "Forms: haiku, cinquain, limerick, acrostic, sonnet, "
                "villanelle, sestina, pantoum, ghazal, ballad, or "
                "'scheme:ABAB' for an arbitrary rhyme scheme."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "form": {"type": "string"},
                    "lines": {"type": "array", "items": {"type": "string"}},
                    "word": {"type": "string",
                             "description": "target word for acrostic"},
                },
                "required": ["form", "lines"],
            },
        ),
        Tool(
            name="fit_contour",
            description=(
                "Find words with an exact syllable count, optionally rhyming "
                "with a given word. Use to repair a metric hitch without "
                "breaking the rhyme."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "syllables": {"type": "integer"},
                    "rhyme_with": {"type": "string"},
                    "limit": {"type": "integer", "default": 12},
                },
                "required": ["syllables"],
            },
        ),
        Tool(
            name="phrase_frequency",
            description=(
                "How often an exact 2-5 word phrase appears across 3M songs. "
                "High counts mean cliche. Use to test a line you are about "
                "to write."
            ),
            inputSchema={
                "type": "object",
                "properties": {"phrase": {"type": "string"}},
                "required": ["phrase"],
            },
        ),
        Tool(
            name="section_template",
            description=(
                "Observed structure for a section kind: typical line counts, "
                "syllable contours, rhyme schemes and what usually follows. "
                "Use to decide how long a chorus should be."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "default": "chorus"},
                    "genre": {"type": "string"},
                },
            },
        ),
        Tool(
            name="search_songs",
            description=(
                "Search the 3.18M Genius lyrics corpus by title, artist, or lyrics text. "
                "Returns song matches with title, artist, year, and lyric preview."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "query string"},
                    "search_by": {
                        "type": "string",
                        "enum": ["all", "title", "artist", "lyrics"],
                        "default": "all",
                        "description": "field to search"
                    },
                    "limit": {"type": "integer", "default": 10, "description": "max matches"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_song",
            description=(
                "Retrieve complete lyrics, sections ([Verse], [Chorus]), and line scansion "
                "for a song from the Genius lyrics corpus."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "song title"},
                    "artist": {"type": "string", "description": "artist name"},
                    "song_id": {"type": "integer", "description": "song id in corpus"},
                },
            },
        ),
        Tool(
            name="phrase_rhymes",
            description=(
                "Look up Cockney rhyming slang phrases that rhyme on a target word "
                "(e.g. 'stairs' -> 'apples and pears')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "word": {"type": "string", "description": "target word"},
                },
                "required": ["word"],
            },
        ),
        Tool(
            name="surprising_rhymes",
            description=(
                "Editorial surprising / slant rhymes that sound great "
                "in lyrics."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "word": {"type": "string", "description": "word to find rhymes for"},
                    "limit": {"type": "integer", "default": 12},
                },
                "required": ["word"],
            },
        ),
        Tool(
            name="analyze_sensory",
            description=(
                "Sense-writing analysis: evaluates concreteness (1.0-5.0), "
                "abstract vs concrete ratio, sensory modality breakdown (visual, auditory, "
                "haptic, gustatory, olfactory, interoceptive, action), and identifies sensory anchors."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "line or lyric section text to analyze"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="analyze_mood",
            description=(
                "Emotion and mood architecture: calculates continuous VAD coordinates "
                "(Valence: pleasantness, Arousal: energy, Dominance: control) and NRC emotion distribution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "line or lyric section text to analyze"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="mosaic_rhymes",
            description=(
                "Find multi-word mosaic rhymes with connected speech weak forms "
                "(e.g. 'remember' -> 'surrender to', 'pretend you were')."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "word": {"type": "string", "description": "target word to rhyme against"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["word"],
            },
        ),
        Tool(
            name="get_exemplar",
            description=(
                "Retrieve real song section scansion as an exemplar stencil to model lyrics on. "
                "Shows line syllables, mid-line phrasing, and rhyme bindings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "default": "verse", "description": "section kind (verse, chorus, bridge)"},
                    "song_title": {"type": "string", "description": "title of reference song (e.g. 'Hotel California')"},
                    "artist": {"type": "string", "description": "artist name"},
                    "genre": {"type": "string", "description": "musical genre if no title given"},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        return await anyio.to_thread.run_sync(_dispatch, name, arguments)
    except Exception as e:  # keep the client alive on bad input
        return _text(f"error in {name}: {e}")


def _dispatch(name: str, a: dict[str, Any]) -> list[TextContent]:
    if name == "analyze_line":
        r = wb.analyze_line(a["text"], beats=a.get("beats", 4))
        out = [f"syllables: {r.syllables}", f"stress:    {r.stress_grid}",
               f"end word:  {r.end_word}"]
        if r.unknown_words:
            out.append(f"unknown (not in dictionary): {', '.join(r.unknown_words)}")
        if r.prosody_flags:
            out.append("prosody: " + "; ".join(r.prosody_flags))
        else:
            out.append("prosody: no collisions")
        if r.internal_rhymes:
            out.append("internal rhyme: " + ", ".join(r.internal_rhymes))
        return _text("\n".join(out))

    if name == "find_rhymes":
        if rdb is None:
            return _text("rhyme dictionary not built; run "
                         "scripts/build_rhymes.py")
        word = a["word"]
        syl = a.get("syllables")
        kind = a.get("kind", "perfect")
        matches = rdb.rhymes(
            word, kinds=(kind,),
            limit=a.get("limit", 12),
            min_freq=a.get("min_freq", 0),
            min_syllables=syl, max_syllables=syl,
            allow_phrases=a.get("allow_phrases", True),
            exclude_tags=tuple(a.get("exclude_tags") or ()),
            prefer=a.get("prefer", "singable"))

        if not matches:
            target = rdb.info(word)
            if target is None:
                return _text(f"'{word}' is not in the dictionary, so it has "
                             f"no known pronunciation to rhyme on")
            if rdb.is_unrhymable(word):
                return _text(f"'{word}' is listed as having no rhyme")
            return _text(f"no {kind} rhymes for '{word}' "
                         f"(rime: {target['pronunciations'][0]['rime']})")

        # Lead with what the word rhymes ON, so the model can tell a wrong
        # result from a right one instead of trusting the list.
        target = rdb.info(word)
        rime = target["pronunciations"][0]["rime"] if target else "?"
        lines = [f"{kind} rhymes for '{word}' (rhymes on: {rime})"]
        for m in matches:
            labels = list(m.tags)
            if m.is_phrase:
                labels.append("phrase")
            if not m.is_verified:
                labels.append("unverified")
            suffix = f"  [{', '.join(labels)}]" if labels else ""
            lines.append(f"  {m.word:<20} {m.syllables}syl  "
                         f"{m.verdict:<9}{suffix}")
            # The definition, so the model can pick a word that means what the
            # line needs. Without it 'knight' and 'night' are indistinguishable
            # and it writes 'a knight sky'.
            if m.definition:
                lines.append(f"      {m.definition}")
        return _text("\n".join(lines))

    if name == "word_sound":
        if rdb is None:
            return _text("rhyme dictionary not built; run "
                         "scripts/build_rhymes.py")
        word = a["word"]
        info = rdb.info(word)
        if info is None:
            return _text(f"'{word}' is not in the dictionary")
        lines = [f"'{info['word']}'"]
        for p in info["pronunciations"]:
            label = " (colloquial variant)" if p["is_variant"] else ""
            lines.append(f"  {p['arpabet']}{label}")
            lines.append(f"    {p['syllables']} syllables, stress "
                         f"{p['stress']}, rhymes on '{p['rime']}'")
        # What the word MEANS, immediately after how it sounds. A rhyme tool
        # that reports sound alone cannot prevent 'a knight sky'.
        for gloss in info["glosses"]:
            lines.append(f"  {gloss}")
        marks = list(info["tags"])
        if info["is_phrase"]:
            marks.append(f"{info['word_count']}-word phrase")
        if info["confidence"] < 2:
            marks.append("single-source pronunciation")
        if marks:
            lines.append("  " + ", ".join(marks))
        lines.append(f"  used {info['corpus_freq']:,} times in 3M songs, "
                     f"{info['end_freq']:,} of those at a line end")
        return _text("\n".join(lines))

    if name == "check_draft":
        r = wb.check_draft(a["lines"], kind=a.get("kind", "verse"),
                           target_syllables=a.get("target_syllables"))
        out = [
            f"lines: {r.line_count}   syllables/line: {r.contour}",
            f"scheme: {r.rhyme_scheme}   rhyme density: {r.rhyme_density:.0%}",
            f"meter consistency: {r.metric_consistency:.2f} (1.0 = perfectly even)",
            f"concreteness: {r.mean_concreteness:.2f}/5.0 (sensory: {r.sensory_ratio:.0%}, abstract: {r.abstract_ratio:.0%})",
            f"mood: {r.dominant_emotion} (valence: {r.valence:.2f}, arousal: {r.arousal:.2f}, dominant sensory: {r.dominant_modality})",
        ]
        if r.sensory_anchors:
            out.append(f"sensory anchors: {', '.join(r.sensory_anchors)}")
        if r.readability_grade is not None:
            out.append(f"reading grade: {r.readability_grade}")
        if r.issues:
            out.append("\nissues to fix:")
            out += [f"  - {i}" for i in r.issues]
        else:
            out.append("\nno issues detected")
        return _text("\n".join(out))

    if name == "validate_form":
        d = wb.validate_form(a["form"], a["lines"], word=a.get("word", ""))
        out = [f"form: {d['form']}   lines: {d['line_count']}   "
               f"valid: {d['ok']}"]
        for v in d["violations"]:
            out.append(f"  - {v['detail']}")
        for n in d["notes"]:
            out.append(f"  note: {n}")
        return _text("\n".join(out))

    if name == "fit_contour":
        cands = wb.fit_contour(a["syllables"], rhyme_with=a.get("rhyme_with"),
                               limit=a.get("limit", 12))
        if not cands:
            return _text("no candidates found")
        head = f"{a['syllables']}-syllable words"
        if a.get("rhyme_with"):
            head += f" rhyming with '{a['rhyme_with']}'"
        lines = [head + ":"]
        for c in cands:
            lines.append(f"  {c.word:<18} {c.kind:<10} {c.verdict}")
        return _text("\n".join(lines))

    if name == "phrase_frequency":
        f = wb.phrase_freq(a["phrase"])
        if f is None:
            return _text("phrase must be 2-5 words, and corpus must be built")
        verdict = ("fresh" if f == 0 else "uncommon" if f < 100
                   else "common" if f < 2000 else "cliche")
        return _text(f"'{a['phrase']}': {f:,} uses in corpus -> {verdict}")

    if name == "section_template":
        d = wb.section_template(a.get("kind", "chorus"), a.get("genre"))
        if not d.get("available"):
            return _text("corpus not available")
        out = [f"{d['kind']} (genre: {d['genre']})",
               f"  typical: {d['avg_lines']} lines, "
               f"{d['avg_syllables_per_line']} syllables/line"]
        if d["common_contours"]:
            out.append("  common contours: " + ", ".join(
                c["contour"] for c in d["common_contours"][:5]))
        if d["common_schemes"]:
            out.append("  common schemes: " + ", ".join(
                s["scheme"] for s in d["common_schemes"][:5]))
        if d["usually_followed_by"]:
            out.append("  usually followed by: " + ", ".join(
                k["kind"] for k in d["usually_followed_by"]))
        return _text("\n".join(out))

    if name == "search_songs":
        results = wb.search_songs(
            query=a["query"],
            search_by=a.get("search_by", "all"),
            limit=a.get("limit", 10)
        )
        if not results:
            return _text(f"no songs found matching '{a['query']}'")
        lines = [f"songs matching '{a['query']}' ({len(results)} matches):"]
        for s in results:
            lines.append(f"  {s.title} -- {s.artist} ({s.year}, {s.genre})\n    {s.lyrics_preview}")
        return _text("\n".join(lines))

    if name == "get_song":
        song = wb.get_song(
            title=a.get("title"),
            artist=a.get("artist"),
            song_id=a.get("song_id")
        )
        if not song:
            return _text("song not found in corpus")
        lines = [f"=== {song.title} -- {song.artist} ({song.year}, {song.genre}) ==="]
        total_len = len(lines[0])
        truncated = False
        for sec in song.sections:
            header = f"\n[{sec.name}] ({sec.kind}, {sec.line_count} lines)"
            if total_len + len(header) > 1800:
                truncated = True
                break
            lines.append(header)
            total_len += len(header)
            for line in sec.lines:
                row = f"  {line.syllables:>2}syl | {line.text}"
                if total_len + len(row) > 1800:
                    truncated = True
                    break
                lines.append(row)
                total_len += len(row)
            if truncated:
                break
        if truncated:
            lines.append("\n... [truncated to keep output compact]")
        return _text("\n".join(lines))

    if name == "phrase_rhymes":
        phrases = wb.phrase_rhymes(a["word"])
        if not phrases:
            return _text(f"no slang phrase rhymes found for '{a['word']}'")
        lines = [f"slang phrase rhymes for '{a['word']}':"]
        for p in phrases:
            reg_str = f" [{', '.join(p['regions'])}]" if p.get("regions") else ""
            lines.append(f"  {p['phrase']}{reg_str}")
        return _text("\n".join(lines))

    if name == "surprising_rhymes":
        surprises = wb.surprising_rhymes(a["word"], limit=a.get("limit", 12))
        if not surprises:
            return _text(f"no surprising rhymes found for '{a['word']}'")
        lines = [f"surprising rhymes for '{a['word']}':"]
        for s in surprises:
            lines.append(f"  {s['word']:<18} tier={s.get('syllable_tier', 1)}  {s.get('verdict', 'fresh')}")
        return _text("\n".join(lines))
    if name == "analyze_sensory":
        s = wb.analyze_sensory(a["text"])
        lines = [
            f"mean concreteness: {s['mean_concreteness']:.2f}/5.0",
            f"concrete ratio:    {s['concrete_ratio']:.0%}",
            f"abstract ratio:    {s['abstract_ratio']:.0%}",
            f"dominant modality: {s['dominant_modality']}",
        ]
        if s.get("modality_scores"):
            scores_str = ", ".join(f"{k}: {v:.0%}" for k, v in s["modality_scores"].items() if v > 0.05)
            lines.append(f"modalities:        {scores_str}")
        if s.get("sensory_words"):
            w_strs = [f"{w['word']} ({w['modality']})" for w in s["sensory_words"][:6]]
            lines.append(f"sensory words:     {', '.join(w_strs)}")
        if s.get("abstract_words"):
            lines.append(f"abstract words:    {', '.join(s['abstract_words'][:6])}")
        return _text("\n".join(lines))

    if name == "analyze_mood":
        m = wb.analyze_mood(a["text"])
        lines = [
            f"valence:           {m['valence']:.2f} ({m['valence_label']})",
            f"arousal:           {m['arousal']:.2f} ({m['arousal_label']})",
            f"dominance:         {m['dominance']:.2f}",
            f"dominant emotion:  {m['dominant_emotion']}",
        ]
        if m.get("emotions"):
            emos = [f"{k}: {v}" for k, v in m["emotions"].items() if v > 0]
            if emos:
                lines.append(f"emotion counts:    {', '.join(emos)}")
        return _text("\n".join(lines))

    if name == "mosaic_rhymes":
        mosaics = wb.mosaic_rhymes(a["word"], limit=a.get("limit", 10))
        if not mosaics:
            return _text(f"no mosaic rhymes found for '{a['word']}'")
        lines = [f"mosaic rhymes for '{a['word']}':"]
        for m in mosaics:
            lines.append(
                f"  {m['phrase']:<24} {m['kind']:<10} {m['rhyme_class']:<15} "
                f"{m['syllables']}syl ({m['rhyme_ending']})")
        return _text("\n".join(lines))

    if name == "get_exemplar":
        ex = wb.get_section_exemplar(
            kind=a.get("kind", "verse"),
            song_title=a.get("song_title"),
            artist=a.get("artist"),
            genre=a.get("genre"),
        )
        if not ex:
            return _text("no exemplar section found")
        return _text(ex.format_stencil())

    return _text(f"unknown tool: {name}")


async def amain() -> None:
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())


def main() -> None:
    import asyncio
    asyncio.run(amain())


if __name__ == "__main__":
    main()
