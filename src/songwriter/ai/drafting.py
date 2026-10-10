"""Section-by-section lyric drafting with deterministic validation.

Generating a whole song in one call produces measurably poor lyrics: the model
reaches for stock rhyme pairs (stone/zone/throne/own), repeats corpus cliches
('running through the', 6,749 uses) and drifts off the syllable target. It has no
way to know any of that -- it cannot hear, and it cannot count.

So drafting runs per section:

  1. Target contour comes from the corpus (`section_template`), not a guess.
  2. Fresh rhyme candidates are pulled from the rhyme database BEFORE drafting and
     handed to the model, so the stock pair is not the only option it can reach.
  3. The draft is validated with `check_draft`, which measures syllables, stock
     pairs and cliche n-grams.
  4. Named issues plus fresh replacement rhymes are fed back for repair, bounded
     by `max_attempts`.
  5. A final whole-song pass validates across sections, where repetition and
     cross-section stock pairs become visible.

Repeated sections (a chorus appearing three times) are drafted and validated once,
then emitted at each position. Repeating a chorus verbatim is how songs work; what
the earlier one-shot output got wrong was the rhyme quality inside it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from songwriter.corpus.ingest import normalize_section
from songwriter.craft.stats import TRAFFIC_DIRECTOR_WORDS, detect_poetic_inversion

_TAG_RE = re.compile(r"\[([^\]]+)\]")

try:
    from songwriter.lexicon.query import RhymeDB, RhymeType
except ImportError:
    RhymeDB = None
    RhymeType = None

# Sections that carry no sung lines worth validating as verse-like text.
_SPARSE_KINDS = {"intro", "outro", "solo", "instrumental", "breakdown", "drop", "interlude"}

# A stock pair flagged by check_draft looks like:
#   "lines 7/13: 'stone/throne' is a stock pair (1,065 uses)"
_STOCK_RE = re.compile(r"'([a-z'\-]+)/([a-z'\-]+)' is a stock pair", re.I)
# "line 3: 'running through the' appears 6,749 times in corpus"
_CLICHE_RE = re.compile(r"'([^']+)' appears [\d,]+ times in corpus", re.I)


@dataclass
class SectionDraft:
    name: str
    kind: str
    lines: list[str]
    attempts: int = 1
    target_syllables: int | None = None
    issues: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    performer: str = ""
    display_tag: str = ""
    rhyme_api_calls: list[dict[str, Any]] = field(default_factory=list)
    attempt_history: list[dict[str, Any]] = field(default_factory=list)
    def ok(self) -> bool:
        return not self.issues


@dataclass
class SongDraft:
    sections: list[SectionDraft]
    lyrics: str
    final_issues: list[str] = field(default_factory=list)
    repaired: int = 0
    polished: bool = False

    def to_dict(self) -> dict[str, Any]:
        sec_list = []
        for s in self.sections:
            sec_list.append({
                "name": s.name,
                "kind": s.kind,
                "line_count": len(s.lines),
                "lines": list(s.lines),
                "attempts": s.attempts,
                "issues": s.issues,
                "resolved": s.resolved,
                "performer": getattr(s, "performer", ""),
                "display_tag": getattr(s, "display_tag", ""),
                "rhyme_api_calls": getattr(s, "rhyme_api_calls", []),
                "attempt_history": getattr(s, "attempt_history", []),
            })
        return {
            "lyrics": self.lyrics,
            "polished": self.polished,
            "repaired_sections": self.repaired,
            "final_issues": self.final_issues,
            "sections": sec_list,
        }


def parse_plan(plan: str) -> list[str]:
    """Section names in order from a plan string like '[Intro], [Verse 1], ...'."""
    return [t.strip() for t in _TAG_RE.findall(plan or "") if t.strip()]


class SectionDrafter:
    """Drafts a song section by section, validating each against the workbench."""

    def __init__(
        self,
        workbench: Any,
        chat: Callable[..., str],
        max_attempts: int = 3,
    ) -> None:
        self.wb = workbench
        self.chat = chat
        self.max_attempts = max(1, int(max_attempts))
        self._rdb = None
        if RhymeDB is not None:
            try:
                self._rdb = RhymeDB()
            except Exception:
                self._rdb = None
        # One lookup per (method, word, filters) for this drafter's lifetime:
        # retries and repeated seed words never hit the database twice.
        self._rhyme_cache: dict[tuple, list[str]] = {}
        self.rhyme_lookups = 0
        for name in ("fresh_rhymes", "query_rhymes"):
            setattr(self, name, self._cached(name, getattr(self, name)))

    def _cached(self, name: str, fn: Callable[..., list[str]]) -> Callable[..., list[str]]:
        def wrapper(word: str, *args: Any, **kwargs: Any) -> list[str]:
            key = (name, (word or "").strip().lower(), args, tuple(sorted(kwargs.items())))
            if key not in self._rhyme_cache:
                self.rhyme_lookups += 1
                self._rhyme_cache[key] = fn(word, *args, **kwargs)
            return list(self._rhyme_cache[key])
        return wrapper
    # ------------------------------------------------------------ targets

    def contour_target(self, kind: str, syllable_target: str | None) -> tuple[int | None, str]:
        """Resolve a per-line syllable target and the evidence behind it.

        An explicit writer choice wins. Otherwise the corpus supplies the
        commonest contour for this section kind.
        """
        if syllable_target:
            nums = [int(n) for n in re.findall(r"\d+", syllable_target)]
            if nums:
                mid = round(sum(nums) / len(nums))
                return mid, f"writer target {syllable_target}"

        try:
            tmpl = self.wb.section_template(kind=kind)
        except Exception:
            tmpl = {}
        if tmpl.get("available") and tmpl.get("common_contours"):
            top = tmpl["common_contours"][0]
            nums = [int(n) for n in re.findall(r"\d+", str(top.get("contour", "")))]
            if nums:
                mid = round(sum(nums) / len(nums))
                return mid, (
                    f"corpus: {top['contour']} is the commonest {kind} contour "
                    f"({top.get('freq', 0):,} sections)"
                )
        avg = tmpl.get("avg_syllables_per_line")
        if avg:
            return round(float(avg)), f"corpus average for a {kind} line"
        return None, ""

    def contour_range(self, kind: str, syllable_target: str | None) -> tuple[int | None, int | None]:
        """Resolve the (min, max) syllable bounds for a section kind."""
        if syllable_target:
            nums = [int(n) for n in re.findall(r"\d+", syllable_target)]
            if len(nums) >= 2:
                return min(nums[0], nums[1]), max(nums[0], nums[1])
            if len(nums) == 1:
                return nums[0], nums[0]
        mid, _ = self.contour_target(kind, syllable_target)
        if mid:
            return max(1, mid - 1), mid + 1
        return None, None

    @staticmethod
    def duet_section_role(name: str, roles: dict[str, str]) -> dict[str, str]:
        """Map a song section to a specific vocal performer, tag, and delivery style."""
        low = name.lower()
        singer_a = roles.get("singer_a") or "Singer A (Male Vocal)"
        singer_b = roles.get("singer_b") or "Singer B (Female Vocal)"

        if "verse 1" in low or low == "verse":
            return {
                "performer": singer_a,
                "tag": f"[{name} - {singer_a}]",
                "instruction": f"Sung entirely by {singer_a} solo. Sing from this character's individual perspective."
            }
        if "verse 2" in low:
            return {
                "performer": singer_b,
                "tag": f"[{name} - {singer_b}]",
                "instruction": f"Sung entirely by {singer_b} solo. Sing from this character's distinct perspective, responding to or contrasting Verse 1."
            }
        if "verse 3" in low:
            return {
                "performer": singer_a,
                "tag": f"[{name} - {singer_a}]",
                "instruction": f"Sung primarily by {singer_a}, or alternating lines between {singer_a} and {singer_b}."
            }
        if "pre-chorus" in low or "prechorus" in low:
            return {
                "performer": f"{singer_a} & {singer_b}",
                "tag": f"[{name} - Duet Build]",
                "instruction": f"Urgent build. Alternate short phrases between ({singer_a}) and ({singer_b}) or trade every line."
            }
        if "chorus" in low or "hook" in low:
            return {
                "performer": "Both (Duet Harmony)",
                "tag": f"[{name} - Duet Harmony]",
                "instruction": f"Sung by BOTH {singer_a} and {singer_b} in full dual-vocal harmony. You may precede lines with '(Both)'."
            }
        if "bridge" in low:
            return {
                "performer": f"Call and Response ({singer_a} & {singer_b})",
                "tag": f"[{name} - Trading / Call and Response]",
                "instruction": f"Emotional dialogue / call-and-response. Alternate lines between ({singer_a}) and ({singer_b}), uniting on the final line as (Both)."
            }
        if "outro" in low:
            return {
                "performer": "Both (Duet Outro)",
                "tag": f"[{name} - Duet Outro]",
                "instruction": f"Both {singer_a} and {singer_b} harmonize or trade quiet parting lines, fading out together as (Both)."
            }
        return {
            "performer": singer_a,
            "tag": f"[{name} - {singer_a}]",
            "instruction": f"Sung by {singer_a}."
        }

    def _annotate_line_syllables(self, line: str) -> tuple[int, str]:
        """Annotate each word in a line with its phonetic syllable count."""
        sung_text = re.sub(r"^\s*[\(\[][^\]\)]+[\)\]]\s*", "", line)
        words = sung_text.split()
        annotated: list[str] = []
        total = 0
        for w in words:
            clean = re.sub(r"[^\w']", "", w)
            if not clean:
                continue
            syl = (self.wb.ph.syllables(clean) if self.wb else 1) or 1
            total += syl
            annotated.append(f"{clean} ({syl})")
        return total, " ".join(annotated)

    def section_line_count(self, kind: str) -> int:
        """Singable, symmetrical line count for standard song sections."""
        if kind in _SPARSE_KINDS:
            if kind in ("solo", "instrumental", "breakdown", "drop"):
                return 1
            return 2
        if kind in ("post_chorus", "post-chorus"):
            return 2
        if kind in ("pre_chorus", "pre-chorus"):
            return 4
        if kind in ("chorus", "hook"):
            return 4
        if kind == "bridge":
            return 4
        if kind == "verse":
            return 4
        return 4

    # ------------------------------------------------------- rhyme material

    # A word nobody sings is not a usable rhyme. `find_rhymes` re-admits its
    # whole candidate list when every entry fails its own min_word_freq gate
    # ("if not out and fallback_out"), which is right for a browsing UI but wrong
    # here: handing 'bus-stop' or 'car-hop' to the model makes lyrics worse.
    MIN_SINGABLE_FREQ = 200

    def fresh_rhymes(self, word: str, limit: int = 6, mood: str | None = None,
                     sensory_modality: str | None = None,
                     min_concreteness: float | None = None) -> list[str]:
        """Fresh rhymes for a word: in the singing vocabulary, rarely paired."""
        if not word:
            return []
        try:
            cands = self.wb.find_rhymes(
                word=word, kinds=("perfect", "slant"), prefer="fresh", limit=limit * 4,
                mood=mood, sensory_modality=sensory_modality, min_concreteness=min_concreteness,
            )
        except Exception:
            return []
        usable: list[str] = []
        seen: set[str] = set()
        for c in cands:
            if c.verdict not in ("fresh", "uncommon"):
                continue
            if (c.corpus_freq or 0) < self.MIN_SINGABLE_FREQ:
                continue
            # rime_member holds duplicate rows, and a plural beside its singular
            # ('siren'/'sirens') is one rhyme option, not two.
            stem = c.word.lower().rstrip("s")
            if stem in seen:
                continue
            seen.add(stem)
            usable.append(c.word)
            if len(usable) >= limit:
                break
        return usable

    def query_rhymes(
        self,
        word: str,
        limit: int = 25,
        allow_slang: bool = True,
        allow_offensive: bool = False,
        allow_phrases: bool = True,
        mood: str | None = None,
        min_concreteness: float | None = None,
    ) -> list[str]:
        """Query the rich DuckDB RhymeDB with tag filtering, returning a balanced mix of rich multisyllabic and vivid monosyllabic rhymes."""
        if not word:
            return []
        w_clean = word.strip().lower()
        if self._rdb is not None:
            try:
                exclude = []
                if not allow_offensive:
                    exclude.extend(["offensive", "vulgar"])
                if not allow_slang:
                    exclude.append("slang")
                matches = self._rdb.rhymes(
                    w_clean,
                    kinds=(RhymeType.PERFECT, RhymeType.MOSAIC) if allow_phrases and RhymeType else (RhymeType.PERFECT,),
                    limit=100,
                    allow_phrases=allow_phrases,
                    exclude_tags=tuple(exclude),
                    prefer="singable",
                )
                if matches:
                    seen = {w_clean}
                    stop = {"all", "y'all", "at all", "not at all", "of all", "it all", "fuck all"}
                    filtered = []
                    for m in matches:
                        mw = m.word.lower().strip()
                        if mw in seen or mw in stop or mw.endswith(w_clean):
                            continue
                        seen.add(mw)
                        filtered.append(m)

                    multi = [m.word for m in filtered if m.syllables >= 2 and m.word not in ("football", "basketball", "baseball")]
                    mono = [m.word for m in filtered if m.syllables == 1 and m.word not in ("call", "fall")]
                    combined = []
                    for pair in zip(multi, mono):
                        combined.extend(pair)
                    rem = [w for w in (multi + mono) if w not in combined]
                    combined.extend(rem)
                    if combined:
                        return combined[:limit]
            except Exception:
                pass
        return self.fresh_rhymes(w_clean, limit=limit, mood=mood, min_concreteness=min_concreteness)

    def build_thematic_clusters(
        self, brief: str, story: str, answers: dict[str, str], spec: dict[str, Any]
    ) -> list[list[str]]:
        """Extract key physical seeds from brief/story and build rich Rhyme Suites from RhymeDB."""
        raw_seeds = self._seed_words(brief + " " + story, answers)
        banned_suffixes = ("ing", "tion", "sion", "ment", "ence", "ance", "ity", "ness", "able", "ible", "fully", "lessly", "ed")
        banned_words = {"project", "system", "issue", "issues", "landscape", "timetabling", "stability", "schedule", "schedules", "process", "feature", "features", "architecture", "infrastructure"}
        clean_seeds = []
        for s in raw_seeds:
            s_low = s.lower().strip()
            if any(s_low.endswith(sfx) for sfx in banned_suffixes) or s_low in banned_words:
                continue
            if len(s_low) <= 2:
                continue
            clean_seeds.append(s_low)

        candidate_seeds = list(dict.fromkeys(clean_seeds))[:5]
        clusters = []
        seen_rimes = set()
        for s in candidate_seeds:
            rhymes = self.query_rhymes(
                s,
                limit=8,
                allow_slang=spec.get("allow_slang", True),
                allow_offensive=spec.get("allow_offensive", False),
                allow_phrases=spec.get("allow_phrases", True),
                mood=spec.get("target_mood"),
                min_concreteness=spec.get("min_concreteness"),
            )
            # Filter out non-poetic words from rhymes as well
            clean_rhymes = [
                w for w in rhymes
                if not any(w.lower().endswith(sfx) for sfx in ("ing", "tion", "ness"))
                and w.lower() not in banned_words
            ]
            if len(clean_rhymes) >= 3:
                st = self.wb.ca.line_stat(s)
                rk = st.rime_key or s
                if rk not in seen_rimes:
                    seen_rimes.add(rk)
                    clusters.append([s] + clean_rhymes)
            if len(clusters) >= 8:
                break
        return clusters

    def draft_couplet(
        self,
        name: str,
        kind: str,
        couplet_idx: int,
        target_syls: tuple[int, int],
        story: str,
        spec: dict[str, Any],
        prior_lines: list[str],
        prior_rimes: set[str],
        suggested_cluster: list[str] | None = None,
        role_info: dict[str, str] | None = None,
        model: str | None = None,
    ) -> tuple[str, str, str, dict[str, Any]]:
        """Draft a 2-line rhyming couplet with upfront rhyme candidates and Line 1 replacement on dead ends."""
        t_a = target_syls[0]
        context_lines = "\n".join(prior_lines[-4:]) if prior_lines else ""
        context_block = f"\nPREVIOUS LINES:\n{context_lines}\n" if context_lines else ""
        prior_rime_rule = f"- End word must NOT rhyme with: {', '.join(list(prior_rimes)[:4])}" if prior_rimes else ""

        anchor_prompt_str = ""
        if suggested_cluster:
            anchor_prompt_str = f"- Recommended ending words from rhyme dictionary: {', '.join(suggested_cluster[:12])} (or choose a tangible physical noun)\n"

        p_a = f"""Audio engine scansion task: Generate Line 1 of Couplet {couplet_idx+1} for [{name}].
NARRATIVE CONTEXT:
{story or 'An original narrative.'}
{context_block}
CONSTRAINTS:
- Write exactly ONE line.
- Target: exactly {t_a} syllables (include a mid-line pause/comma at syllable {t_a // 2}).
- {"Concrete sensory anchor (Rusty's Collar): Name a specific physical object, setting, or sound in the scene. Do NOT open with an abstract feeling. SVO word order." if couplet_idx == 0 else "Concrete physical sensory scene or action. SVO word order."}
{anchor_prompt_str}{prior_rime_rule}

Output the ONE line only. No quotes, no commentary."""

        text_a = self.chat([{"role": "user", "content": p_a}], model=model, temperature=0.75, max_tokens=150).strip()
        line_a = self._clean_single_line(text_a)
        if not line_a and text_a.splitlines():
            line_a = text_a.splitlines()[0].strip()

        st_a = self.wb.ca.line_stat(line_a)
        w_a = st_a.end_word or (suggested_cluster[0] if suggested_cluster else "screen")

        # ACTIVATE RHYME ENGINE ON END WORD
        rhymes = self.query_rhymes(
            w_a,
            limit=25,
            allow_slang=spec.get("allow_slang", True),
            allow_offensive=spec.get("allow_offensive", False),
            allow_phrases=spec.get("allow_phrases", True),
            mood=spec.get("target_mood"),
            min_concreteness=spec.get("min_concreteness"),
        )
        if not rhymes and suggested_cluster:
            rhymes = [w for w in suggested_cluster if w.lower() != w_a.lower()]
        if not rhymes:
            rhymes = self.fresh_rhymes(w_a, limit=8)
        if not rhymes:
            rhymes = ["seen", "clean", "green", "routine", "machine"]

        # DEAD END CHECK & LINE 1 BACKTRACK:
        # If w_a has no rhyming partners or is an empty stop-word, replace Line 1!
        if (len(rhymes) < 2 or w_a.lower() in TRAFFIC_DIRECTOR_WORDS) and suggested_cluster:
            fallback_anchor = suggested_cluster[1] if len(suggested_cluster) > 1 and suggested_cluster[1].lower() != w_a.lower() else suggested_cluster[0]
            p_a_retry = f"""Audio engine scansion task: Rewrite Line 1 of Couplet {couplet_idx+1} for [{name}].
NARRATIVE CONTEXT:
{story or 'An original narrative.'}
{context_block}
CONSTRAINTS:
- Write exactly ONE line.
- Target: exactly {t_a} syllables (split {t_a // 2} // {t_a - t_a // 2}).
- MUST END with this exact rhyme anchor: {fallback_anchor}
- {"Concrete sensory anchor (Rusty's Collar): Ground the line in a specific physical object, setting, or sound. SVO word order." if couplet_idx == 0 else "Concrete physical sensory scene or action. SVO word order."}

Output the ONE line only. No quotes, no commentary."""
            text_a2 = self.chat([{"role": "user", "content": p_a_retry}], model=model, temperature=0.75, max_tokens=150).strip()
            line_a2 = self._clean_single_line(text_a2)
            if line_a2:
                line_a = line_a2
                st_a = self.wb.ca.line_stat(line_a)
                w_a = st_a.end_word or fallback_anchor
                rhymes = self.query_rhymes(w_a, limit=8, allow_slang=spec.get("allow_slang", True), allow_offensive=spec.get("allow_offensive", False), allow_phrases=spec.get("allow_phrases", True))
                if not rhymes and suggested_cluster:
                    rhymes = [w for w in suggested_cluster if w.lower() != w_a.lower()]

        remaining_pool = [w for w in (rhymes + (suggested_cluster or [])) if w.lower() != w_a.lower() and not w_a.lower().endswith(w.lower())]
        if not remaining_pool:
            remaining_pool = ["seen", "clean", "green", "routine"]

        t_b = target_syls[1]
        p_b = f"""Audio engine scansion task: Complete Couplet {couplet_idx+1} for [{name}].
Line 1 of the couplet is:
\"{line_a}\"

Write Line 2 to complete this rhyming couplet.
NARRATIVE CONTEXT:
{story or 'An original narrative.'}

CONSTRAINTS:
- Write exactly ONE line completing the action of Line 1.
- Target: exactly {t_b} syllables (include a mid-line pause/comma at syllable {t_b // 2}).
- MANDATORY ENDING WORD: Line 2 MUST end with one of these verified rhyme words from the rhyme dictionary:
  {', '.join(remaining_pool[:20])}
- Natural conversational spoken English. SVO word order only. The line MUST make literal, physical storytelling sense. Do NOT write inverted or nonsensical phrases just to force a rhyme.
Output the ONE line only. No quotes, no commentary."""

        text_b = self.chat([{"role": "user", "content": p_b}], model=model, temperature=0.75, max_tokens=150).strip()
        line_b = self._clean_single_line(text_b)
        if not line_b and text_b.splitlines():
            line_b = text_b.splitlines()[0].strip()

        st_b = self.wb.ca.line_stat(line_b)
        w_b = st_b.end_word or ""
        j = self.wb.ph.compare(w_a, w_b)

        # BACKTRACK CHECK: If Line 2 failed to rhyme or used inverted syntax, retry Line 2
        inv_b = detect_poetic_inversion(line_b)
        if j.kind in ("none", "identity") or w_a == w_b or inv_b:
            reason = f"Your previous attempt used inverted word order ('{inv_b}')." if inv_b else f"Your previous attempt did not rhyme with '{w_a}'."
            p_b_retry = f"""Audio engine scansion task: Correct Line 2 to complete this rhyming couplet for [{name}].
Line 1 is: \"{line_a}\" (ends on '{w_a}')
{reason}
CONSTRAINTS:
- Write exactly ONE line.
- Target: exactly {t_b} syllables.
- You MUST end the line with one of these exact rhyme words: {', '.join(remaining_pool[:5])}
- Natural conversational English. SVO word order.

Output the ONE line only."""
            text_b2 = self.chat([{"role": "user", "content": p_b_retry}], model=model, temperature=0.7, max_tokens=150).strip()
            line_b2 = self._clean_single_line(text_b2)
            if line_b2:
                line_b = line_b2
                st_b = self.wb.ca.line_stat(line_b)
                w_b = st_b.end_word or ""
                j = self.wb.ph.compare(w_a, w_b)

        audit_entry = {
            "couplet": couplet_idx + 1,
            "line_1": line_a,
            "line_1_syllables": st_a.syllables,
            "anchor_word": w_a,
            "rhyme_api_candidates": remaining_pool[:6],
            "line_2": line_b,
            "line_2_syllables": st_b.syllables,
            "chosen_rhyme_word": w_b,
            "phonetic_verdict": j.kind,
            "rhyme_class": j.rhyme_class,
        }
        rime_a = st_a.rime_key or w_a
        return line_a, line_b, rime_a, audit_entry
    def rhyme_palette(self, seeds: list[str], per_seed: int = 5, mood: str | None = None,
                      sensory_modality: str | None = None,
                      min_concreteness: float | None = None) -> dict[str, list[str]]:
        """Fresh rhyme options for each seed word, for the model to draw on."""
        palette: dict[str, list[str]] = {}
        for seed in seeds:
            options = self.fresh_rhymes(
                seed, limit=per_seed, mood=mood,
                sensory_modality=sensory_modality, min_concreteness=min_concreteness,
            )
            if options:
                palette[seed] = options
        return palette

    # --------------------------------------------------------------- drafting

    def draft_song(
        self,
        brief: str,
        spec: dict[str, Any],
        plan: str,
        answers: dict[str, str] | None = None,
        banned_words: list[str] | None = None,
        rhyme_scheme: str | None = None,
        syllable_target: str | None = None,
        vocal: str | None = None,
        vocal_roles: dict[str, str] | None = None,
        reference_song: dict[str, Any] | None = None,
        story: str = "",
        model: str | None = None,
        polish: bool = True,
    ) -> SongDraft:
        names = parse_plan(plan) or ["Verse 1", "Chorus"]
        drafted: dict[str, SectionDraft] = {}
        ordered: list[SectionDraft] = []
        repaired = 0

        vocal_type = (vocal or spec.get("vocal") or "").strip().lower()
        is_duet = vocal_type == "duet"
        roles = vocal_roles or {}
        if is_duet and not roles:
            roles = {"singer_a": "Singer A (Male Vocal)", "singer_b": "Singer B (Female Vocal)"}

        used_motifs: list[str] = []
        used_openers: list[str] = []

        for name in names:
            key = name.lower()
            if key in drafted:
                # A repeated chorus is the same words in a new position.
                ordered.append(drafted[key])
                continue

            role_info = self.duet_section_role(name, roles) if is_duet else None

            section = self.draft_section(
                name=name,
                brief=brief,
                spec=spec,
                answers=answers or {},
                banned_words=banned_words or [],
                rhyme_scheme=rhyme_scheme,
                syllable_target=syllable_target,
                prior=ordered,
                role_info=role_info,
                used_motifs=used_motifs,
                used_openers=used_openers,
                reference_song=reference_song,
                story=story or spec.get("story", ""),
                model=model,
            )
            if section.attempts > 1:
                repaired += 1
            drafted[key] = section
            ordered.append(section)

            # Accumulate used imagery & openers to avoid echo-chamber repetitions in later sections
            for ln in section.lines:
                clean_ln = re.sub(r"^\s*[\(\[][^\]\)]+[\)\]]\s*", "", ln).strip()
                words = [w.lower().strip(".,!?;:'\"") for w in clean_ln.split() if w.strip()]
                if len(words) >= 2:
                    used_openers.append(" ".join(words[:2]))
                for w in words:
                    if len(w) > 4 and w not in used_motifs:
                        used_motifs.append(w)

        lyrics = self._assemble(names, drafted)
        range_target = self.contour_range("verse", syllable_target)
        final_issues = self._validate_song(ordered, target_syllables=range_target)

        draft = SongDraft(
            sections=ordered, lyrics=lyrics, final_issues=final_issues, repaired=repaired
        )

        if polish and final_issues:
            polished = self._polish(draft, brief, spec, model)
            if polished:
                new_issues = self._validate_lyrics(polished, target_syllables=range_target)
                try:
                    p_rep = self.wb.check_draft(self._extract_lines(polished))
                    orig_rep = self.wb.check_draft(self._extract_lines(lyrics))
                    # Accept polish only if it maintains rhyme density and strictly reduces issues
                    if p_rep.rhyme_density >= orig_rep.rhyme_density and len(new_issues) < len(final_issues):
                        draft.lyrics = polished
                        draft.polished = True
                        draft.final_issues = new_issues
                except Exception:
                    pass
        return draft

    def draft_section(
        self,
        name: str,
        brief: str,
        spec: dict[str, Any],
        answers: dict[str, str],
        banned_words: list[str],
        rhyme_scheme: str | None,
        syllable_target: str | None,
        prior: list[SectionDraft],
        role_info: dict[str, str] | None = None,
        used_motifs: list[str] | None = None,
        used_openers: list[str] | None = None,
        reference_song: dict[str, Any] | None = None,
        story: str = "",
        model: str | None = None,
    ) -> SectionDraft:
        kind = normalize_section(name) or "verse"
        target, evidence = self.contour_target(kind, syllable_target)
        target_range = self.contour_range(kind, syllable_target)
        lines_wanted = self.section_line_count(kind)

        seeds = self._seed_words(brief, answers)
        palette = self.rhyme_palette(
            seeds,
            mood=spec.get("target_mood"),
            sensory_modality=spec.get("target_sensory_modality"),
            min_concreteness=spec.get("min_concreteness"),
        )
        ref_title = reference_song.get("title") if reference_song else None
        ref_artist = reference_song.get("artist") if reference_song else None
        ref_id = reference_song.get("id") if reference_song else None
        exemplar = None
        if hasattr(self.wb, "get_section_exemplar"):
            try:
                exemplar = self.wb.get_section_exemplar(
                    kind=kind,
                    song_title=ref_title,
                    artist=ref_artist,
                    song_id=ref_id,
                    genre=spec.get("genre"),
                    limit_lines=(lines_wanted * 2) if lines_wanted else 8,
                )
                if exemplar and hasattr(exemplar, "lines") and exemplar.lines:
                    lines_wanted = len(exemplar.lines)
            except Exception:
                exemplar = None

        lines: list[str] = []
        issues: list[str] = []
        resolved: list[str] = []
        rhyme_api_calls: list[dict[str, Any]] = []
        attempt_history: list[dict[str, Any]] = []
        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            attempts = attempt
            if attempt == 1:
                thematic_clusters = self.build_thematic_clusters(brief, story or spec.get("story", ""), answers, spec)
                text = self._ask_draft(
                    name=name, kind=kind, brief=brief, spec=spec, answers=answers,
                    banned_words=banned_words, rhyme_scheme=rhyme_scheme,
                    target=target, target_range=target_range, evidence=evidence,
                    lines_wanted=lines_wanted, palette=palette, prior=prior,
                    role_info=role_info, used_motifs=used_motifs, used_openers=used_openers,
                    exemplar=exemplar,
                    thematic_clusters=thematic_clusters,
                    story=story or spec.get("story", ""),
                    model=model,
                )
            else:
                # Retrieve another structural exemplar from the corpus matching the same structure
                alt_exemplar = None
                if hasattr(self.wb, "get_section_exemplar"):
                    try:
                        alt_exemplar = self.wb.get_section_exemplar(
                            kind=kind,
                            target_syllables=target_range or target,
                            rhyme_scheme=rhyme_scheme,
                            lines_wanted=lines_wanted,
                            exclude_title=exemplar.song_title if exemplar else None,
                            limit_lines=(lines_wanted * 2) if lines_wanted else 8,
                        )
                    except Exception:
                        alt_exemplar = None
                text = self._ask_repair(
                    name=name, kind=kind, lines=lines, issues=issues,
                    target=target, target_range=target_range, spec=spec,
                    banned_words=banned_words, role_info=role_info,
                    exemplar=alt_exemplar or exemplar,
                    model=model,
                )
            new_lines = self._extract_lines(text)
            if not new_lines:
                break
            if lines_wanted and len(new_lines) > lines_wanted:
                new_lines = new_lines[:lines_wanted]
            prev_issues = issues
            lines = new_lines
            issues = self._validate_section(lines, kind, target_range, target, exemplar=exemplar)
            attempt_history.append({
                "attempt": attempt,
                "lines": list(lines),
                "raw_text": text,
                "issues": list(issues),
                "rejected": bool(issues),
            })
            if prev_issues:
                fixed = [i for i in prev_issues if i not in issues]
                resolved.extend(fixed)
            if not issues:
                break

        performer = role_info.get("performer", "") if role_info else ""
        display_tag = role_info.get("tag", f"[{name}]") if role_info else f"[{name}]"

        return SectionDraft(
            name=name, kind=kind, lines=lines, attempts=attempts,
            target_syllables=target, issues=issues, resolved=resolved,
            performer=performer, display_tag=display_tag,
            rhyme_api_calls=rhyme_api_calls,
            attempt_history=attempt_history,
        )
    # ------------------------------------------------------------ validation

    def _validate_section(
        self, lines: list[str], kind: str, target_range: tuple[int | None, int | None] | None = None,
        target: int | None = None, exemplar: Any = None
    ) -> list[str]:
        if kind in ("solo", "instrumental", "breakdown", "drop"):
            return []
        sung = [l for l in lines if l.strip() and not (l.strip().startswith("[") and l.strip().endswith("]"))]
        clean_sung = [re.sub(r"^\s*[\(\[][^\]\)]+[\)\]]\s*", "", l) for l in sung if l.strip()]
        if not clean_sung:
            return ["no sung lines"]
        target_param = target_range if (target_range and target_range[0] is not None) else target
        issues: list[str] = []
        try:
            report = self.wb.check_draft(clean_sung, kind=kind, target_syllables=target_param)
            issues.extend(report.issues)
        except Exception:
            pass

        # Strict pattern matching against exemplar
        if exemplar and hasattr(exemplar, "lines") and exemplar.lines:
            ex_len = len(exemplar.lines)
            if len(clean_sung) != ex_len:
                issues.append(f"line count mismatch: got {len(clean_sung)} lines, exemplar requires {ex_len} lines")

            if exemplar.rhyme_scheme:
                tag_to_lines: dict[str, list[int]] = {}
                for idx, tag in enumerate(exemplar.rhyme_scheme[:len(clean_sung)]):
                    if tag != "-":
                        tag_to_lines.setdefault(tag, []).append(idx)
                for tag, indices in tag_to_lines.items():
                    if len(indices) >= 2:
                        for a_idx, b_idx in zip(indices[:-1], indices[1:]):
                            if a_idx < len(clean_sung) and b_idx < len(clean_sung):
                                ea = self.wb.ca.line_stat(clean_sung[a_idx]).end_word
                                eb = self.wb.ca.line_stat(clean_sung[b_idx]).end_word
                                if ea and eb:
                                    j = self.wb.ph.compare(ea, eb)
                                    if j.kind in ("none", "identity") and ea != eb:
                                        issues.append(
                                            f"lines {a_idx+1}/{b_idx+1} must rhyme as Rhyme {tag} "
                                            f"(exemplar requires '{ea}' and '{eb}' to rhyme, verdict: {j.kind})"
                                        )
                # Contrast check: distinct rhyme tags (e.g. A vs B) must NOT rhyme with each other (prevent monorhyme collapse)
                tags = list(tag_to_lines.keys())
                for t_i in range(len(tags)):
                    for t_j in range(t_i + 1, len(tags)):
                        tag_a, tag_b = tags[t_i], tags[t_j]
                        idx_a = tag_to_lines[tag_a][0]
                        idx_b = tag_to_lines[tag_b][0]
                        if idx_a < len(clean_sung) and idx_b < len(clean_sung):
                            ea = self.wb.ca.line_stat(clean_sung[idx_a]).end_word
                            eb = self.wb.ca.line_stat(clean_sung[idx_b]).end_word
                            if ea and eb:
                                j = self.wb.ph.compare(ea, eb)
                                if j.kind in ("perfect", "slant"):
                                    issues.append(
                                        f"monorhyme defect: Rhyme {tag_a} ('{ea}') and Rhyme {tag_b} ('{eb}') sound identical. "
                                        f"Lines under Rhyme {tag_b} must use a contrasting rhyme sound."
                                    )

        return issues

    def _validate_song(self, sections: list[SectionDraft], target_syllables: Any = None) -> list[str]:
        """Validate unique sections as one body, where cross-section pairs show."""
        seen: set[str] = set()
        sung: list[str] = []
        for section in sections:
            if section.name.lower() in seen:
                continue
            seen.add(section.name.lower())
            sung.extend(
                re.sub(r"^\s*[\(\[][^\]\)]+[\)\]]\s*", "", l)
                for l in section.lines
                if l.strip() and not (l.strip().startswith("[") and l.strip().endswith("]"))
            )
        if not sung:
            return ["no sung lines"]
        try:
            report = self.wb.check_draft(sung, kind="verse", target_syllables=target_syllables)
        except Exception:
            return []
        return list(report.issues)

    def _validate_lyrics(self, lyrics: str, target_syllables: Any = None) -> list[str]:
        sung = [
            re.sub(r"^\s*[\(\[][^\]\)]+[\)\]]\s*", "", l.strip())
            for l in lyrics.splitlines()
            if l.strip() and not (l.strip().startswith("[") and l.strip().endswith("]"))
        ]
        # Unique lines only: a repeated chorus is not a repetition defect.
        unique: list[str] = []
        seen: set[str] = set()
        for line in sung:
            low = line.lower()
            if low in seen:
                continue
            seen.add(low)
            unique.append(line)
        if not unique:
            return ["no sung lines"]
        try:
            return list(self.wb.check_draft(unique, kind="verse", target_syllables=target_syllables).issues)
        except Exception:
            return []

    # ------------------------------------------------------------- prompting

    def _ask_draft(
        self,
        name: str,
        kind: str,
        brief: str,
        spec: dict[str, Any],
        answers: dict[str, str],
        banned_words: list[str],
        rhyme_scheme: str | None,
        target: int | None,
        target_range: tuple[int | None, int | None] | None,
        evidence: str,
        lines_wanted: int,
        palette: dict[str, list[str]],
        prior: list[SectionDraft],
        role_info: dict[str, str] | None = None,
        used_motifs: list[str] | None = None,
        used_openers: list[str] | None = None,
        exemplar: Any = None,
        thematic_clusters: list[list[str]] | None = None,
        story: str = "",
        model: str | None = None,
    ) -> str:
        decisions = "\n".join(
            f"- {k.replace('_', ' ')}: {v}" for k, v in answers.items() if v
        ) or "- none recorded"

        context = ""
        written = [s for s in prior if s.lines]
        if written:
            last = written[-1]
            context = (
                f"\nALREADY WRITTEN -- [{last.name}] (do not repeat these lines "
                f"or their rhymes):\n" + "\n".join(last.lines[:8])
            )

        if thematic_clusters:
            suite_lines = []
            for idx, s_words in enumerate(thematic_clusters[:4]):
                suite_lines.append(f"- Suite {chr(ord('A') + idx)}: {', '.join(s_words[:20])}")
            rhyme_block = (
                "\nEXHAUSTIVE RHYME VOCABULARY FROM DUCKDB RHYME ENGINE:\n"
                "Incorporate rhymes freely from these rich verified suites to link your couplets:\n"
                + "\n".join(suite_lines)
            )
        else:
            palette_text = "\n".join(
                f"- rhymes with '{seed}': {', '.join(words)}"
                for seed, words in list(palette.items())[:4]
            )
            rhyme_block = (
                "\nFRESH RHYME OPTIONS, measured against 3.18M songs:\n"
                + palette_text
                if palette_text
                else ""
            )

        if target_range and target_range[0] and target_range[1]:
            if target_range[0] == target_range[1]:
                target_line = f"- exactly {target_range[0]} syllables per line ({evidence})"
            else:
                target_line = f"- strictly hold {target_range[0]} to {target_range[1]} syllables per line ({evidence})"
        elif target:
            target_line = f"- {target} syllables per line ({evidence})"
        else:
            target_line = "- keep every line the same length"

        if rhyme_scheme and rhyme_scheme.lower() != "free":
            scheme_line = f"- rhyme scheme: hold {rhyme_scheme.upper()} strictly across these {lines_wanted} lines"
        else:
            scheme_line = "- rhyme scheme: hold AABB or ABAB rhyming pairs. Every line must rhyme cleanly with its partner line. No unrhymed stray lines."
        banned_line = (
            f"- never use: {', '.join(banned_words)}" if banned_words else ""
        )

        role_block = ""
        if role_info:
            role_block = (
                f"\nVOCAL ROLE & DELIVERY FOR THIS SECTION\n"
                f"- Performer: {role_info.get('performer', 'Lead')}\n"
                f"- Direction: {role_info.get('instruction', 'Sing naturally.')}\n"
                f"- Mark lines with singer tags if trading or harmonizing (e.g. '({role_info.get('performer')}) ...')\n"
            )

        avoid_parts: list[str] = []
        if used_motifs:
            avoid_parts.append(f"- Do NOT reuse these nouns/images from earlier sections: {', '.join(list(dict.fromkeys(used_motifs))[:10])}")
        if used_openers:
            avoid_parts.append(f"- Do NOT start lines with these already used stems: {', '.join(list(dict.fromkeys(used_openers))[:6])}")
        memory_block = ("\nWHOLE-SONG MOTIF MEMORY\n" + "\n".join(avoid_parts) + "\n") if avoid_parts else ""
        style_val = spec.get("style", "")
        style_block = f"- Style direction: {style_val}\n" if style_val else ""
        exemplar_list = []
        if exemplar:
            exemplar_list.append(exemplar)
        if hasattr(self.wb, "get_section_exemplar"):
            try:
                ex2 = self.wb.get_section_exemplar(
                    kind=kind,
                    target_syllables=target_range or target,
                    rhyme_scheme=rhyme_scheme,
                    lines_wanted=lines_wanted,
                    exclude_title=exemplar.song_title if exemplar else None,
                )
                if ex2:
                    exemplar_list.append(ex2)
            except Exception:
                pass

        exemplar_blocks = []
        for i, ex in enumerate(exemplar_list):
            exemplar_blocks.append(f"Exemplar {i+1} (from '{ex.song_title}' by {ex.artist}):\n{ex.format_stencil()}")
        exemplar_block = (
            "\nSAMPLE LINES & CADENCE FROM REAL CANONICAL SONGS (Observe how real songs balance narrative sense, sensory metaphor, and mid-line rhythm):\n"
            + "\n\n".join(exemplar_blocks)
            if exemplar_blocks else ""
        )
        story_block = f"\nNARRATIVE STORY TREATMENT:\n{story}\n" if story else ""
        exemplar_rule = "- Model your lines on the scansion, phrasing rhythm, and rhyme bindings of the exemplars above."

        passages = []
        if hasattr(self.wb, "get_corpus_lyric_passages"):
            try:
                passages = self.wb.get_corpus_lyric_passages(
                    reference_title=exemplar.song_title if exemplar else None,
                    genre=spec.get("genre"),
                    limit=4,
                )
            except Exception:
                passages = []

        passages_text = []
        for p in passages:
            lines_str = "\n".join(f"    {l}" for l in p["lines"])
            passages_text.append(f"  From '{p['title']}' ({p['artist']}):\n{lines_str}")
        passages_block = (
            "\nREAL-WORLD LYRIC PASSAGES FROM CANONICAL SONGS (Study how real songwriters balance colloquial shorthand, rhythm, attitude, and atmosphere):\n"
            + "\n\n".join(passages_text) + "\n"
            if passages_text else ""
        )

        prompt = f"""Write ONLY the [{name}] section of a song.

BRIEF
{brief.strip() or 'An original song.'}
{story_block}
PRODUCTION
- {spec.get('bpm')} BPM, {spec.get('music_key')} {spec.get('scale')}, {spec.get('genre')}
{style_block}
WRITER'S DECISIONS
{decisions}
{role_block}
CONSTRAINTS FOR THIS SECTION
- exactly {lines_wanted} lines
{target_line}
{scheme_line}
{banned_line}
{exemplar_rule}
- CRAFT PRINCIPLES:
  1. Three-act miniature play: drop the listener into an intimate scene without giving away the plot or title in line 1. Establish the sensory atmosphere (ambient sounds, lighting, temperature, physical objects) and human tension before naming the crisis.
  2. Conversational reality: natural colloquial rhythm, sentence fragments, informal contractions, authentic attitude. No PR announcements or dry technical prose.
  3. Related rhymes: anchor couplets on rhymes that organically belong to the story world rather than random dictionary matches.
  4. Reverse-couplet rule: know the payoff word at the end of line 2 before writing line 1, so the rhyme feels inevitable rather than forced.
- TOGGLING & TRIPOD RULES:
  1. Tripod of connection (Who, Where, When): the first two lines establish WHO is speaking/acting, WHERE they are physically situated, and WHEN in time (consistent verb tense).
  2. External vs. internal toggling:
     - External lines (E): sense-bound physical objects and actions happening around the character (doors, chairs, fluorescent hum, spilled coffee, cold glass).
     - Internal lines (I): thoughts, emotions and evaluations inside the mind and heart.
  3. The power position: the final line of the section must be an internal (I) line or summary thought that gives purpose and emotional weight to the preceding external details.
- Syllable Control: Keep the lines strictly within the target range. If lines feel too long, split the thought across two beats or cut redundant filler words.
- Ground the scene in physical sensory reality (screens, cold coffee, flickering tube lights, cables, midnight corridors) rather than abstract corporate jargon.
- No cliche openers such as 'running through the' or 'rising from the'
{memory_block}{exemplar_block}{passages_block}{rhyme_block}{context}

Output the {lines_wanted} lines only. No commentary, no numbering."""

        return self.chat(
            [{"role": "user", "content": prompt}],
            model=model,
            temperature=0.8,
            max_tokens=500,
        )

    def _ask_repair(
        self,
        name: str,
        kind: str,
        lines: list[str],
        issues: list[str],
        target: int | None,
        target_range: tuple[int | None, int | None] | None = None,
        spec: dict[str, Any] | None = None,
        banned_words: list[str] | None = None,
        role_info: dict[str, str] | None = None,
        exemplar: Any = None,
        model: str | None = None,
    ) -> str:
        numbered = "\n".join(f"{i + 1}. {l}" for i, l in enumerate(lines))

        # Annotate any lines with syllable issues
        annotated_issues: list[str] = []
        t_min = target_range[0] if target_range and target_range[0] else (target or 8)
        t_max = target_range[1] if target_range and target_range[1] else (target or 10)
        for issue in issues:
            annotated_issues.append(f"- {issue}")
            m = re.search(r"line\s+(\d+):\s+(\d+)\s+syllables", issue, re.I)
            if m:
                line_idx = int(m.group(1)) - 1
                if 0 <= line_idx < len(lines):
                    raw_l = lines[line_idx]
                    tot, annot = self._annotate_line_syllables(raw_l)
                    if tot > t_max:
                        diff = tot - t_max
                        annotated_issues.append(f"  * Word syllables: \"{annot}\"")
                        annotated_issues.append(f"  * ACTION: Over target by {diff} syllable{'s' if diff > 1 else ''}. Remove or replace multi-syllable words marked (2) or (3) with 1-syllable words to achieve {t_min}-{t_max} syllables.")
                    elif tot < t_min:
                        diff = t_min - tot
                        annotated_issues.append(f"  * Word syllables: \"{annot}\"")
                        annotated_issues.append(f"  * ACTION: Line is {diff} syllable{'s' if diff > 1 else ''} too short. Add a descriptive 1-syllable word to achieve {t_min}-{t_max} syllables.")

        issues_block = "\n".join(annotated_issues)
        # Offer measured replacements for the words that were actually flagged.
        flagged: list[str] = []
        for issue in issues:
            m = _STOCK_RE.search(issue)
            if m:
                flagged.extend([m.group(1), m.group(2)])
        replacements = ""
        if flagged:
            palette = self.rhyme_palette(list(dict.fromkeys(flagged))[:3], per_seed=6)
            if palette:
                replacements = "\nFRESH REPLACEMENTS for the flagged rhymes:\n" + "\n".join(
                    f"- instead of pairing on '{seed}': {', '.join(words)}"
                    for seed, words in palette.items()
                )

        cliches = [m.group(1) for i in issues if (m := _CLICHE_RE.search(i))]
        cliche_line = (
            "\nREWRITE these overused phrases entirely: " + ", ".join(dict.fromkeys(cliches))
            if cliches
            else ""
        )

        target_line = f"- hold {t_min} to {t_max} syllables per line" if t_min and t_max else ""

        exemplar_prompt = ""
        if exemplar and hasattr(exemplar, "format_stencil"):
            exemplar_prompt = (
                f"\nHERE'S ANOTHER EXAMPLE from a real song executing this exact structure "
                f"({exemplar.song_title} by {exemplar.artist}):\n"
                f"{exemplar.format_stencil()}\n"
            )

        prompt = f"""Editorial polish task: Refine the lyrics for [{name}].

CURRENT LINES:
{numbered}

MEASURED PROBLEMS (failed for reasons):
{issues_block}
{exemplar_prompt}{replacements}{cliche_line}
CRAFT GUIDELINES:
{target_line}
- Match line-by-line the rhythm, syllable counts, and couplet rhyme bindings.
- Conversational reality: Natural cadence and sensory grounding ('leaving the skins on the potato chips') over textbook formal grammar.
- Reverse couplet logic: Ensure the rhyme landing feels inevitable rather than forced.
- Keep the same narrative meaning, but solve the identified rhythm, meter, or stock rhyme problems.
{('- never use: ' + ', '.join(banned_words)) if banned_words else ''}
Output the revised lines only. No conversational preamble, no commentary, no numbering."""
        return self.chat(
            [{"role": "user", "content": prompt}],
            model=model,
            temperature=0.6,
            max_tokens=500,
        )


    def _polish(
        self, draft: SongDraft, brief: str, spec: dict[str, Any], model: str | None
    ) -> str | None:
        """Final whole-song pass over issues only visible across sections."""
        prompt = f"""This song was measured section by section and is nearly finished.
Fix the remaining problems without rewriting what already works.

BRIEF
{brief.strip() or 'An original song.'}

PRODUCTION
- {spec.get('bpm')} BPM, {spec.get('music_key')} {spec.get('scale')}, {spec.get('genre')}

CURRENT LYRICS
{draft.lyrics}

REMAINING MEASURED PROBLEMS
{chr(10).join('- ' + i for i in draft.final_issues)}

RULES
- keep every section tag exactly as it appears, in the same order
- a chorus that repeats must stay identical at each repeat
- keep line counts and syllable lengths
- fix only the listed problems

Output the complete lyrics with section tags. No commentary."""

        try:
            text = self.chat(
                [{"role": "user", "content": prompt}],
                model=model,
                temperature=0.5,
                max_tokens=1400,
            )
        except Exception:
            return None

        cleaned = self._strip_commentary(text)
        # A polish pass that loses sections is a regression, not an improvement.
        if len(_TAG_RE.findall(cleaned)) < len(_TAG_RE.findall(draft.lyrics)):
            return None
        return cleaned or None

    # ---------------------------------------------------------------- output

    @staticmethod
    def _assemble(names: list[str], drafted: dict[str, SectionDraft]) -> str:
        blocks: list[str] = []
        for name in names:
            section = drafted.get(name.lower())
            if not section or not section.lines:
                continue
            tag = getattr(section, "display_tag", "") or f"[{name}]"
            blocks.append(f"{tag}\n" + "\n".join(section.lines))
        return "\n\n".join(blocks)

    @staticmethod
    def _seed_words(brief: str, answers: dict[str, str]) -> list[str]:
        """Content words worth building rhymes around."""
        text = " ".join([brief] + [v for v in answers.values() if v])
        words = re.findall(r"[a-zA-Z']{4,}", text.lower())
        stop = {
            "this", "that", "with", "from", "they", "them", "then", "than", "into",
            "song", "about", "there", "their", "been", "have", "what", "when",
            "where", "which", "while", "would", "could", "should", "first",
            "person", "present", "tense", "line", "lines", "verse", "chorus",
            "bass", "synth", "drums", "tempo", "bpm", "beat", "beats", "key",
            "scale", "major", "minor", "guitar", "piano", "production", "vocal",
            "vocals", "singer", "harmony", "duet", "ballad", "melody", "chords",
            "track", "music", "audio", "acoustic", "electric", "instrumental",
            "solo", "outro", "intro", "bridge", "post", "hook", "rhyme", "sound",
        }
        seen: set[str] = set()
        out: list[str] = []
        for word in words:
            if word in stop or word in seen:
                continue
            seen.add(word)
            out.append(word)
        return out[:6]

    @staticmethod
    def _extract_lines(text: str) -> list[str]:
        out: list[str] = []
        for raw in (text or "").splitlines():
            line = raw.strip()
            if not line or line.startswith("```") or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                if re.match(r"^\[(intro|verse|chorus|bridge|outro|hook|pre-chorus)[\w\s\-:]*\]$", line, re.I):
                    continue
            line = re.sub(r"^\s*\d+[.)]\s*", "", line)
            line = re.sub(r"\s*\(\d+\)\s*$", "", line)
            low = line.lower()
            if low.startswith((
                "here", "note:", "notes:", "i've", "i have", "this section", "these lines",
                "corrected lines", "i need to clarify", "i can't assist", "i cannot assist",
                "you've shared", "the request asks", "the framing", "what are you actually",
                "i'm happy to help", "if you're working"
            )):
                continue
            out.append(line)
        return out
    @staticmethod
    def _clean_single_line(text: str) -> str:
        for raw in (text or "").splitlines():
            line = raw.strip().strip('"').strip("'")
            if line.lower().startswith("line 1:"):
                line = line[7:].strip().strip('"').strip("'")
            elif line.lower().startswith("line 2:"):
                line = line[7:].strip().strip('"').strip("'")
            line = re.sub(r"^\s*\d+[.)]\s*", "", line)
            line = re.sub(r"\s*\(\d+\)\s*$", "", line)
            if line and not line.startswith(("*", "#", "-", "[", "(")) and len(line.split()) >= 3:
                return line
        return text.splitlines()[0].strip().strip('"').strip("'") if (text or "").splitlines() else ""

    @staticmethod
    def _strip_commentary(text: str) -> str:
        lines = (text or "").splitlines()
        out: list[str] = []
        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("#"):
                continue
            low = stripped.lower()
            if low.startswith(("here", "note:", "notes:", "i've", "i have", "the changes", "changes:")):
                continue
            out.append(line)
        return "\n".join(out).strip()
