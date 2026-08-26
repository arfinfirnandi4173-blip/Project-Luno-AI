"""
response_output.py
====================

Chat <-> Voice Dual Output sprint - the ONE presentation-adaptation layer
that takes a single LLM response and derives two independent presentation
strings from it, without ever running a second reasoning pass:

    response_text (one LLM reply)
            |
            v
      build_dual_response()
            |
      +-----+-----+
      v           v
  chat_text   voice_text
      |           |
      v           v
  Chat/UI       TTS

  - `chat_text` - what the Chat/text UI shows. Identical to the input
    `response_text` - markdown/code/lists/technical detail are ALL
    preserved. Chat is allowed to be as detailed as the LLM made it.
  - `voice_text` - what TTS actually speaks. Built by cleaning the text
    through the EXISTING `luno.text_normalizer.normalize_for_speech()`
    (markdown/code/links/URLs/bullets/emoji stripped, numbers spoken
    naturally - none of that is reimplemented here), then, depth-aware,
    compressing PRESENTATION (never MEANING) for DETAILED replies only.

THIS IS A PRESENTATION TRANSFORMATION LAYER ONLY - `build_dual_response()`
is a pure function of `(response_text, response_policy)`. It does NOT:
  - call an LLM (deterministic/rule-based only, mirrors
    `luno.response_policy`'s own "no second AI model" contract - verified
    by this module importing nothing beyond the standard library plus
    `luno.text_normalizer`/`luno.response_policy`'s pure helpers)
  - retrieve memory, call the Planner, or call any tool
  - mutate memory, importance, usefulness, evaluation, relationship
    state, or emotion state
  - implement TTS streaming/chunking (a later, separate sprint)
  - duplicate `luno.response_policy.compute_response_policy()` - the
    depth passed in must already have been computed ONCE per turn by the
    caller; this module never re-derives depth from text itself, and
    never invents a second per-channel depth decision.

REUSED, NOT DUPLICATED:
  - `luno.text_normalizer.normalize_for_speech()` - all markdown/code/
    link/URL/bullet/emoji stripping and number-to-words conversion goes
    through this existing, already-tested function. This module never
    reimplements any of that.
  - `luno.text_normalizer.rules.NUMBER_RE`/`NUMBER_RANGE_RE` - reused
    (not reimplemented) purely to DETECT whether a raw sentence carries a
    number, before that number is converted to words further down the
    pipeline (detection has to happen on the ORIGINAL digits; by the time
    `normalize_for_speech()` has run, "5" has already become "five"/
    "lima", so detection is done first, conversion is still done only by
    the existing function).
  - `luno.response_policy`'s three depth strings (`DEPTH_SHORT`/
    `DEPTH_NORMAL`/`DEPTH_DETAILED`) - imported directly, never redefined
    or shadowed by a new enum.

NEW IN THIS MODULE (did not exist anywhere in the repo before this
sprint - confirmed by the Phase 1 audit): sentence splitting, numbered-
list marker stripping (`luno.text_normalizer`'s own `BULLET_RE` only
covers `-`/`*`/`+` markers, never `1.`/`2)`-style numbering), near-
duplicate sentence detection, and depth-budgeted priority selection.
These are additive, self-contained, and used only by this module.

DEPTH BEHAVIOR (updated by the Voice Output Optimization sprint - see
docs/change_impact/voice_output_optimization.md for the full design
rationale and worked examples)
---------------------------------------------------------------------
`voice_text` is ALWAYS a bounded, priority-selected SUBSET of the
cleaned text's own sentences at every depth now - never a character-
count truncation, never a second LLM call, never a rewrite of any
sentence's own wording. The lead sentence (almost always the direct
answer) is always kept; sentences carrying a number/spec or a warning/
prohibition keyword are always kept (hard must-keep, never budget-
limited - see `_WARNING_KEYWORDS`); a soft conditional clause
("kalau"/"if"/"unless") scores higher but is NOT a hard must-keep (the
brief's own worked SHORT example drops one); a genuine closing/summary
sentence is kept too.

  - SHORT: the smallest budget (`max(2, ceil(0.3 * sentence_count))`) -
    "1-2 concise sentences" in practice for an ordinary reply. List
    items are NOT exempt from SHORT's budget (a SHORT answer should not
    read a whole list aloud regardless).
  - NORMAL: a moderate budget (`max(5, ceil(0.35 * sentence_count))`) -
    "usually 2-4 sentences" for a typical reply; only compresses at all
    once a reply is genuinely long/exhaustive (5+ non-list sentences).
    List-item sentences are exempt from NORMAL's budget-based dropping
    (see `_select_by_priority`'s `protect_list_items` - a long list is a
    separate, still-unsolved concern, documented as a known limitation
    rather than silently truncated or given fabricated summary wording).
  - DETAILED: unchanged from the prior sprint - budget
    `max(3, ceil(0.4 * sentence_count))`, list items NOT exempt (matches
    this depth's pre-existing, already-tested behavior exactly). An
    EXPLICIT "give me full detail" instruction this turn
    (`ResponsePolicy.explicit and depth == "detailed"`) skips compression
    entirely instead - the user asked for everything, so they get
    everything (still cleaned for speech, never markdown/code-literal).

This compresses HOW MUCH is spoken, not WHICH FACTS survive - see
`tests/test_response_output.py`'s semantic-safety tests and
`tests/test_voice_output_optimization.py`'s dedicated suite.

See `docs/change_impact/chat_voice_dual_output.md` for the original
Chat/Voice split's design rationale, and
`docs/change_impact/voice_output_optimization.md` for this sprint's own
(no LLM-based paraphrasing, no parenthetical-aside stripping, no cross-
sentence rewriting, no list-run summarization - all still deliberately
out of scope for this deterministic version).

TTS CHUNKING / VOICE STREAMING SPRINT (additive - see
docs/change_impact/tts_chunking_streaming.md)
--------------------------------------------------------------------------
`DualResponse` gained one new field, `voice_chunks: List[str]` - the
SAME sentence selection already computed for `voice_text` (`selected`,
below), grouped into playback-sized pieces so Fish Audio can start
speaking chunk 1 while later chunks are still being synthesized,
instead of waiting for one giant `client.play(voice_text)` call.
`voice_chunks` and `voice_text` are ALWAYS derived from the identical
`selected` list - there is no separate chunking-time re-selection, so
"the chunk sequence equals the same full response" holds by
construction, not by a second, potentially-divergent pass.

Default granularity: ONE SENTENCE = ONE CHUNK (the smallest natural
spoken unit) - chosen so the first chunk is as short as possible and
playback can start as soon as possible. Consecutive list-item sentences
are the one exception: they are grouped into a single chunk (comma-
joined, exactly like `_join_sentences` already does for the single-
string `voice_text`) because reading a bullet list as N separate,
independently-synthesized TTS calls sounds worse (audible micro-gaps
between items) than reading it as one continuous, comma-paced chunk -
unless that combined chunk would exceed `max_chunk_chars`, in which
case the list is split at item boundaries (never mid-item).

`max_chunk_chars` is a ceiling/safety-net, never a packing target -
most chunks will be well under it. A single sentence longer than the
ceiling is split at clause boundaries first (comma/semicolon - keeps
the cut at a natural pause), falling back to whitespace boundaries only
if it has no clause punctuation at all. Character-count slicing is
NEVER the primary strategy and NEVER cuts inside a word/URL/number - if
a single "word" (by whitespace) is itself longer than `max_chunk_chars`
(e.g. a long URL), it is kept whole rather than mid-cut.

This module still does not implement retry/fallback/queue-scheduling -
those are FishAudioAdapter's job (luno/adapters/fish_audio.py); this
module only ever produces the deterministic, ordered list of chunk
strings.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import List, NamedTuple, Optional, Union

from .response_policy import DEPTH_DETAILED, DEPTH_NORMAL, DEPTH_SHORT, ResponsePolicy
from .text_normalizer import normalize_for_speech
from .text_normalizer import rules as _tn_rules
from .voice_output_mode import (
    DEFAULT_VOICE_OUTPUT_MODE,
    VOICE_OUTPUT_MODE_ALL,
    resolve_voice_output_mode,
)

#: The one, explicit output contract. `chat_text`/`voice_text` are
#: deliberately never named `text`/`response`/`content` (per this
#: sprint's own "no ambiguous fields" instruction) - the field name
#: alone always says which channel a string is for.
@dataclass
class DualResponse:
    """`chat_text` - unmodified `response_text`, for the Chat/UI channel.
    `voice_text` - cleaned (and, for DETAILED depth, compressed) text for
    the TTS channel. `depth` - the SAME `ResponsePolicy.depth` string
    this was built from (`"short"`/`"normal"`/`"detailed"`) - never
    recomputed here. `request_id` - optional passthrough for correlating
    this result back to the turn it came from (never used internally by
    this module). `voice_adapted` - True only when `voice_text` actually
    differs in CONTENT from a plain cleaned-but-uncompressed pass (near-
    duplicate removal and/or DETAILED-depth budget selection actually
    removed something) - False for an ordinary SHORT/NORMAL turn where
    cleaning was the only transformation applied."""

    chat_text: str
    voice_text: str
    depth: str
    request_id: Optional[str] = None
    voice_adapted: bool = False
    #: TTS Chunking/Streaming sprint (additive) - `voice_text` grouped into
    #: playback-sized pieces, in speaking order, derived from the SAME
    #: sentence selection as `voice_text` (see module docstring). Empty
    #: list only when `voice_text` itself is empty. A single-element list
    #: (`[voice_text]`-equivalent) is a completely normal, common result -
    #: NOT a degraded/fallback case; it just means the reply was short
    #: enough to fit in one chunk.
    voice_chunks: List[str] = field(default_factory=list)
    #: TTS Chunk Queue/Cancellation sprint (additive) - the RAW (still
    #: markdown-bearing, pre-`normalize_for_speech`) reference text for
    #: each entry of `voice_chunks`, same length/order, 1:1 aligned - used
    #: only for `SpeechChunk.raw_text` (see luno/speech_chunk.py);
    #: `voice_chunks` itself (the CLEANED text) is still what's actually
    #: sent to TTS. Degrades to matching `voice_chunks` exactly for the
    #: rare oversized-single-sentence-split case (see
    #: `_group_sentences_into_chunk_pairs()`'s own docstring).
    voice_chunks_raw: List[str] = field(default_factory=list)
    #: Voice Output Mode sprint (additive) - the RESOLVED mode
    #: (`"ALL"`/`"SHORT"`) this `DualResponse` was actually built under,
    #: always one of `luno.voice_output_mode.VOICE_OUTPUT_MODES` - never
    #: `None`, even when the caller passed nothing (resolves to
    #: `DEFAULT_VOICE_OUTPUT_MODE`). Purely observational passthrough -
    #: nothing downstream is required to read it - but useful for tests/
    #: debug logging to confirm which path a given reply actually took.
    voice_output_mode: str = DEFAULT_VOICE_OUTPUT_MODE


# ─────────────────────────────────────────────
#  Sentence splitting (NEW - nothing in the repo did this before).
#  Operates on the RAW (still markdown-bearing) text so numeric/keyword
#  DETECTION below sees real digits - each resulting raw chunk is only
#  cleaned (via the existing normalizer) once splitting is done.
# ─────────────────────────────────────────────

#: Abbreviation stems that must never be treated as a sentence boundary
#: (mirrors `luno.text_normalizer.rules.ABBREVIATIONS`'s own coverage -
#: reused as a guard here, not reimplemented as a conversion).
_ABBREV_STEMS = (
    "Dr", "Mr", "Mrs", "Ms", "Prof", "St",
    "e.g", "i.e", "etc", "vs", "approx",
    "Yth", "dkk", "dll", "tsb", "sbb", "yth",
)

#: A sentence boundary is `.`/`!`/`?` followed by whitespace and (a
#: capital letter, a digit, an opening quote, or end of string) - a
#: deliberately conservative heuristic (never a full NLP sentence
#: tokenizer - this sprint's own "deterministic, local, no
#: over-engineering" rule).
_SENTENCE_END_RE = re.compile(r'(?<=[.!?])\s+(?=[A-Z0-9"‘“’”]|$)')

_NUMBERED_MARKER_RE = re.compile(r'^\s*\d+[.)]\s+')
_BULLET_MARKER_RE = re.compile(r'^\s*[-*+]\s+')
_BLANK_LINES_RE = re.compile(r'\n\s*\n')


def _looks_like_list_item(line: str) -> bool:
    s = line.strip()
    return bool(_BULLET_MARKER_RE.match(s) or _NUMBERED_MARKER_RE.match(s))


def _ends_with_abbrev(s: str) -> bool:
    s = s.rstrip()
    return any(s.endswith(stem + ".") for stem in _ABBREV_STEMS)


def _split_sentences_guarded(paragraph: str) -> List[str]:
    pieces = _SENTENCE_END_RE.split(paragraph)
    merged: List[str] = []
    for piece in pieces:
        if merged and _ends_with_abbrev(merged[-1]):
            merged[-1] = f"{merged[-1]} {piece}"
        else:
            merged.append(piece)
    return [p.strip() for p in merged if p.strip()]


class _Chunk(NamedTuple):
    """One raw, still-markdown-bearing sentence-like unit. `is_list_item`
    marks a chunk that came from a bullet/numbered list LINE (as opposed
    to ordinary sentence splitting) - used only to pick the right join
    separator later (list items read naturally as a comma-joined run,
    e.g. "Wi-Fi, MQTT, Home Assistant", never space-glued into one
    run-on word sequence)."""
    raw: str
    is_list_item: bool


def _split_into_raw_sentences(text: str) -> List[_Chunk]:
    """Splits `text` into raw (still markdown-bearing, digits intact)
    sentence-like chunks. Standalone fenced code blocks are dropped
    entirely here (never spoken - see module docstring); inline code
    inside an ordinary sentence is left for `normalize_for_speech()` to
    handle when that chunk is cleaned. Each bullet/numbered list LINE
    becomes its own chunk (one list item = one spoken unit, never merged
    with a neighboring line or split further) - handled line-by-line
    (not "only if every line in the paragraph is a list item") so a
    header line immediately followed by a list ("Steps:\\n1. ...\\n2. ...")
    doesn't drag its list markers into an ordinary sentence split, which
    would otherwise strand a bare "2." as its own accidental sentence."""
    no_code = _tn_rules.CODE_BLOCK_RE.sub("\n", text)
    chunks: List[_Chunk] = []
    buffer: List[str] = []

    def _flush_buffer() -> None:
        if not buffer:
            return
        joined = " ".join(ln.strip() for ln in buffer if ln.strip())
        buffer.clear()
        if joined:
            chunks.extend(_Chunk(s, False) for s in _split_sentences_guarded(joined))

    for para in _BLANK_LINES_RE.split(no_code):
        para = para.strip("\n")
        if not para.strip():
            continue
        for ln in para.split("\n"):
            if not ln.strip():
                continue
            if _looks_like_list_item(ln):
                _flush_buffer()
                chunks.append(_Chunk(ln.strip(), True))
            else:
                buffer.append(ln)
        _flush_buffer()
    return chunks


def _strip_list_markers(chunk: str) -> str:
    """Removes a leading numbered-list marker (`"1. "`, `"2) "`) - bullet
    markers (`-`/`*`/`+`) are already stripped by
    `normalize_for_speech()`'s own `BULLET_RE`, so only the numbered-list
    case (which that rule does NOT cover) needs handling here."""
    return _NUMBERED_MARKER_RE.sub("", chunk)


# ─────────────────────────────────────────────
#  Near-duplicate sentence removal (applies at every depth - "avoid
#  repeated conclusions" from the sprint brief).
# ─────────────────────────────────────────────

_WORD_RE = re.compile(r"[a-zA-Z0-9']+")
_DUPLICATE_JACCARD_THRESHOLD = 0.82


def _word_set(s: str):
    return {w.lower() for w in _WORD_RE.findall(s)}


class _Sentence(NamedTuple):
    """A `_Chunk` after cleaning - `cleaned` is what actually gets
    spoken; `raw`/`is_list_item` are carried through purely for scoring
    (numeric detection needs the original digits) and join-separator
    selection."""
    raw: str
    cleaned: str
    is_list_item: bool


def _dedupe(sentences: List[_Sentence]) -> List[_Sentence]:
    kept: List[_Sentence] = []
    kept_word_sets: List[set] = []
    for s in sentences:
        ws = _word_set(s.cleaned)
        is_duplicate = False
        if ws:
            for prev_ws in kept_word_sets:
                union = ws | prev_ws
                if not union:
                    continue
                if len(ws & prev_ws) / len(union) >= _DUPLICATE_JACCARD_THRESHOLD:
                    is_duplicate = True
                    break
        if not is_duplicate:
            kept.append(s)
            kept_word_sets.append(ws)
    return kept


# ─────────────────────────────────────────────
#  DETAILED-depth priority selection - compress PRESENTATION, never
#  MEANING. The lead sentence, any sentence carrying a number/spec, and
#  any sentence carrying a warning/imperative keyword are always
#  prioritized; a genuine closing/summary sentence is kept too.
# ─────────────────────────────────────────────

_WARNING_KEYWORDS = (
    # Indonesian
    "penting", "wajib", "jangan", "hati-hati", "hati hati", "harus",
    "perhatikan", "peringatan", "awas", "jangan lupa", "wajib dicatat",
    "wajib diperhatikan", "dilarang", "berbahaya", "tidak boleh",
    # English
    "important", "warning", "caution", "must", "required", "note that",
    "critical", "never ", "always remember", "danger", "be careful",
    "make sure",
    # Voice Output Optimization sprint - explicit prohibition phrasing
    # (the brief's own anti-example: "Do not connect this directly to
    # 220V." must never be softened into "Be careful with the wiring." -
    # these were the missing English prohibition terms found in Phase 0's
    # audit; Indonesian "jangan"/"dilarang"/"berbahaya"/"tidak boleh"
    # already existed or were added just above).
    "do not ", "don't ", "avoid ", "forbidden",
)

_CONCLUSION_CUES = (
    "jadi", "kesimpulannya", "intinya", "singkatnya", "pada akhirnya",
    "in summary", "overall", "in conclusion", "to summarize", "in short",
)

#: Voice Output Optimization sprint - SOFT conditional-clause signal
#: ("kalau"/"jika"/"if"/"unless"/"only when" ...). Deliberately a SCORING
#: boost only, never a hard must-keep like `_WARNING_KEYWORDS` - the
#: sprint brief's own worked SHORT example drops a soft recommendation
#: conditional ("kalau hotspot mulai mendekati 90°C, sebaiknya periksa
#: airflow...") while a HARD prohibition ("jangan sambungkan ke 220V")
#: must never be dropped at any depth. `_has_warning()` (hard must-keep)
#: already covers the prohibition case; this covers the softer "this
#: matters, weight it a bit higher" case without making every kalau/jika
#: clause unconditionally survive compression - see
#: docs/change_impact/voice_output_optimization.md for the full
#: reasoning behind this deliberate hard/soft split.
_CONDITION_KEYWORDS = (
    "kalau", "kalo", "jika", "jikalau", "apabila", "kecuali", "asalkan",
    "unless", "only when", "only if", "in case", "if ", "if,",
)

_DETAILED_BUDGET_FLOOR = 3
_DETAILED_BUDGET_RATIO = 0.4

#: Voice Output Optimization sprint - SHORT/NORMAL now also compress
#: (previously only DETAILED did - see module docstring). Floors chosen
#: deliberately generously so every pre-existing chunking-mechanics test
#: in `tests/test_response_output.py` (which used NORMAL/SHORT as a
#: "nothing gets dropped" neutral baseline for testing chunk splitting,
#: not compression) keeps passing unmodified wherever that was the
#: test's actual intent; only `test_c3` (19 near-identical, genuinely
#: exhaustive placeholder sentences at NORMAL depth) needed an explicit,
#: documented update, because that IS exactly the "exhaustive list of
#: examples" shape this sprint asks NORMAL to stop reading in full. See
#: the change-impact doc's "Known limitations" section for the resulting,
#: honestly-documented trade-off: the canonical GPU-temperature worked
#: example (NORMAL 5 sentences -> 4) is demonstrated in this sprint's own
#: test suite at a sentence count comfortably above these floors, not by
#: lowering the floor to 4-5 (which would have regressed `test_c12`, an
#: existing, deliberately-asserted chunk-order test using exactly 5
#: ordinary NORMAL sentences).
_SHORT_BUDGET_FLOOR = 2
_SHORT_BUDGET_RATIO = 0.3
_NORMAL_BUDGET_FLOOR = 5
_NORMAL_BUDGET_RATIO = 0.35


def _has_number(raw: str) -> bool:
    return bool(_tn_rules.NUMBER_RE.search(raw) or _tn_rules.NUMBER_RANGE_RE.search(raw))


def _compile_word_boundary_marker_pattern(markers) -> re.Pattern:
    """Voice Output Coherence sprint - word-boundary-safe marker matching,
    reusing the EXACT technique `luno.memory._compile_word_boundary_marker_pattern()`
    already established for the identical class of bug ("lanjut" matching
    inside "selanjutnya"). Here it fixes a real, reproduced false positive:
    plain `"harus" in cleaned_lower` substring matching wrongly fired on
    "seharusnya" ("should"/"supposedly" - an ordinary probabilistic
    statement, not a warning at all), silently promoting an unrelated
    sentence to hard must-keep status and displacing a genuinely more
    load-bearing sentence from a bounded budget (see
    docs/change_impact/voice_output_coherence.md for the concrete,
    reproduced example). `\\b` on both ends of each (`.strip()`ped, since
    some existing `_WARNING_KEYWORDS` entries carry a trailing space for
    the OLD substring-search convention - `\\b` itself already provides
    the boundary, so the literal trailing space is redundant and would
    otherwise sit awkwardly inside the boundary-anchored pattern) marker -
    still plain `re`, the same library this module already imports, not a
    second tokenizer."""
    escaped = [re.escape(m.strip()) for m in markers if m.strip()]
    return re.compile(r'\b(?:' + '|'.join(escaped) + r')\b', re.IGNORECASE)


_WARNING_RE = _compile_word_boundary_marker_pattern(_WARNING_KEYWORDS)


def _has_warning(cleaned_lower: str) -> bool:
    return bool(_WARNING_RE.search(cleaned_lower))


def _has_conclusion_cue(cleaned_lower: str) -> bool:
    return any(kw in cleaned_lower for kw in _CONCLUSION_CUES)


def _has_condition(cleaned_lower: str) -> bool:
    return any(kw in cleaned_lower for kw in _CONDITION_KEYWORDS)


#: Voice Output Coherence sprint - a sentence that immediately PRECEDES a
#: soft-conditional sentence (`_has_condition()`, the SAME existing
#: detector - no new classifier) gets a bounded "sets up a conditional"
#: bonus in `_select_by_priority()` below. A soft conditional clause
#: ("Jika WiFi sudah terhubung tapi MQTT masih gagal, periksa kembali...")
#: almost always presupposes the sentence right before it just described
#: the state/action/check the condition refers to - `_score_sentence()`
#: otherwise gives an ordinary explanatory/diagnostic sentence with no
#: number/warning/condition of its OWN a near-zero score, so it was the
#: first thing dropped even when a SURVIVING conditional sentence's
#: meaning depended on it (reproduced concretely - see
#: docs/change_impact/voice_output_coherence.md). Deliberately smaller
#: than `_has_condition`'s own +20 (this is an INDIRECT signal about the
#: NEXT sentence, not this sentence's own content) and applied only
#: between adjacent sentences in the ALREADY-selected candidate list -
#: never a lookahead/lookbehind across a dropped sentence, never a
#: dependency graph, never semantic understanding.
_CONDITION_SETUP_BONUS = 12.0


# ─────────────────────────────────────────────
# Voice Response Intelligence sprint (Sprint 1 - Context-Preserving
# Response Selection) - GENERALIZES the Voice Output Coherence sprint's
# own condition-adjacency mechanism (per this project's own
# ARCHITECTURE_GUARD.md guidance: "if a future report reproduces a
# similar orphaning pattern for a DIFFERENT discourse marker, extend
# `_select_scores_with_setup_bonus()`'s SAME one-hop-adjacency mechanism
# rather than inventing a second one").
#
# PROBLEM this addresses: even after the coherence fix, two sentences can
# each individually score well (e.g. both carry a number, or one is the
# lead) and both survive selection, yet NOT form a coherent pair when
# heard together - because one of them OPENS with a discourse marker
# (causal/continuation/reference/conditional) that presupposes its
# immediate predecessor, and that predecessor did NOT happen to score
# well enough to also survive on its own merits. The coherence sprint's
# fix only covered this for CONDITIONAL openers; this sprint extends the
# SAME detection-and-bonus idea to three more marker categories, plus adds
# a deterministic post-selection repair pass (`_repair_orphans()`, below)
# as a stronger guarantee than a scoring bonus alone can offer (a bonus
# can still lose to enough competing high scorers under a tight budget).
#
# All matching is LEADING-WINDOW ONLY (the marker must OPEN the sentence,
# i.e. anchor at position 0 of the first `_DEPENDENCY_LEADING_WINDOW_WORDS`
# words) - deliberately stricter than `_has_condition`'s own whole-sentence
# substring check (kept unchanged for backward compatibility - see
# `_is_dependent_sentence()` below, which still reuses `_has_condition`
# for the conditional category exactly as before). A marker merely
# APPEARING somewhere mid-sentence is a much weaker "this sentence depends
# on what came before" signal than one that opens the sentence - anchoring
# also sidesteps most false positives (e.g. "motor ini" - "ini" as an
# ordinary noun modifier - vs. "Ini terjadi karena..." - "ini" as a
# sentence-opening backward reference).
#
# Still: plain keyword/regex matching, still `re` (no new tokenizer, no
# embeddings, no LLM judge, no semantic understanding) - reuses
# `_compile_word_boundary_marker_pattern()` exactly as `_WARNING_RE`
# already does.
# ─────────────────────────────────────────────

_CAUSAL_KEYWORDS = (
    "karena", "sebab", "akibatnya", "sehingga", "oleh karena itu",
    "karena itu", "dampaknya", "hal ini menyebabkan", "yang menyebabkan",
    "because", "since", "as a result", "therefore", "hence", "due to this",
    "this causes", "which causes",
)

_CONTINUATION_KEYWORDS = (
    "selanjutnya", "kemudian", "lalu", "setelah itu", "setelah", "selain itu",
    "berikutnya", "lebih lanjut", "meskipun", "walaupun", "walau",
    "namun", "tapi", "tetapi",
    "however", "moreover", "furthermore", "additionally", "next",
    "then", "after that", "meanwhile", "besides that",
)

_REFERENCE_KEYWORDS = (
    "itu", "ini", "tersebut", "hal itu", "hal ini", "hal tersebut",
    "ini terjadi", "hal inilah", "inilah",
    "that", "this", "those", "these", "it",
)

#: The marker must open the sentence (anchored match, see below) - a
#: window wide enough to hold the longest multi-word phrase above
#: ("oleh karena itu", "as a result") without needing an exact-length
#: match.
_DEPENDENCY_LEADING_WINDOW_WORDS = 6

_CAUSAL_RE = _compile_word_boundary_marker_pattern(_CAUSAL_KEYWORDS)
_CONTINUATION_RE = _compile_word_boundary_marker_pattern(_CONTINUATION_KEYWORDS)
_REFERENCE_RE = _compile_word_boundary_marker_pattern(_REFERENCE_KEYWORDS)

_DEP_INDEPENDENT = "INDEPENDENT"
_DEP_SUPPORTING = "SUPPORTING"
_DEP_DEPENDENT = "DEPENDENT"


def _leading_window(cleaned_lower: str, n: int = _DEPENDENCY_LEADING_WINDOW_WORDS) -> str:
    words = cleaned_lower.strip().split()
    return " ".join(words[:n])


def _has_leading_marker(cleaned_lower: str, pattern: re.Pattern) -> bool:
    """True only if `pattern` matches starting at the very first word of
    the sentence (`.match()`, not `.search()`) - the marker must OPEN the
    sentence, not merely appear somewhere within the leading window. This
    is what keeps an ordinary sentence like "Konfigurasi ini bisa diubah
    kapan saja." (where "ini" is the 2nd word, an ordinary noun modifier)
    from being misclassified - only a sentence that genuinely STARTS with
    a backward-referencing/contrastive/causal word triggers this."""
    return pattern.match(_leading_window(cleaned_lower)) is not None


def _has_causal_lead(cleaned_lower: str) -> bool:
    return _has_leading_marker(cleaned_lower, _CAUSAL_RE)


def _has_continuation_lead(cleaned_lower: str) -> bool:
    return _has_leading_marker(cleaned_lower, _CONTINUATION_RE)


def _has_reference_lead(cleaned_lower: str) -> bool:
    return _has_leading_marker(cleaned_lower, _REFERENCE_RE)


def _is_dependent_sentence(sentences: List[_Sentence], i: int, condition_indices: set) -> bool:
    """True if sentence `i` OPENS with a causal/continuation/reference
    marker, or (reusing the EXISTING, unchanged whole-sentence detector -
    see module note above) carries a soft conditional. `condition_indices`
    is precomputed once by the caller (over ALL sentences) so this stays a
    cheap set lookup rather than re-scanning text per call."""
    if i in condition_indices:
        return True
    lower = sentences[i].cleaned.lower()
    return _has_causal_lead(lower) or _has_continuation_lead(lower) or _has_reference_lead(lower)


def _dependency_kind(sentences: List[_Sentence], i: int, condition_indices: set) -> str:
    """Classifies sentence `i` as DEPENDENT (opens with a marker that
    presupposes the sentence right before it), SUPPORTING (not itself
    dependent, but sentence `i + 1` IS dependent on it - i.e. `i` is the
    predecessor a DEPENDENT sentence needs to remain understandable), or
    INDEPENDENT (neither). Index 0 (the lead sentence) is always
    INDEPENDENT - there is no predecessor for it to depend on. One-hop
    only (looks at `i` and `i + 1`, never further) - same bounded,
    deterministic, no-dependency-graph constraint the rest of this module
    already honors."""
    if i == 0:
        return _DEP_INDEPENDENT
    if _is_dependent_sentence(sentences, i, condition_indices):
        return _DEP_DEPENDENT
    if i + 1 < len(sentences) and _is_dependent_sentence(sentences, i + 1, condition_indices):
        return _DEP_SUPPORTING
    return _DEP_INDEPENDENT


def _starts_list_run(sentences: List[_Sentence], i: int) -> bool:
    """True if sentence `i` is a list item AND is the FIRST item of its
    run (i.e. `i == 0`, or the immediately preceding sentence is NOT
    itself a list item). Voice Output Naturalness sprint - a bulleted/
    numbered list depends on its own immediately-preceding setup/
    explanation sentence exactly the same way a causal/continuation/
    reference/conditional sentence depends on ITS predecessor (see
    `_is_dependent_sentence()` above) - but a list item never opens with
    one of those discourse markers (it is typically a noun phrase or
    "Label: value", e.g. "Controller: ESP32 / Arduino / Raspberry Pi"),
    so `_is_dependent_sentence()` was structurally blind to this second,
    equally-real kind of dependency. This is a PURE STRUCTURAL check
    (reuses the EXISTING `_Sentence.is_list_item` flag `_looks_like_list_item()`
    already computes at segmentation time - no new classifier, no new
    keyword table) - only the run's OWN first item triggers this; the
    2nd/3rd/... item of the same run does not (it shares the same setup
    the run's first item already protects, one hop is enough, same
    "no lookahead/lookbehind beyond one hop" bound every other dependency
    check in this module already honors)."""
    if not sentences[i].is_list_item:
        return False
    if i == 0:
        return False  # nothing precedes it - there is no setup to protect
    return not sentences[i - 1].is_list_item


def _build_semantic_units(sentences: List[_Sentence], condition_indices: set) -> List[tuple]:
    """SEMANTIC SPEECH UNIT grouping (Voice Pipeline Latency & Semantic
    Speech Segmentation sprint - Sprint 2 - Phase 3/4).

    A "semantic speech unit" is defined here as the smallest run of
    CONSECUTIVE sentences that should normally stay together when
    spoken: a maximal chain starting at an INDEPENDENT or SUPPORTING
    sentence and extending forward through every immediately-following
    DEPENDENT sentence (`_is_dependent_sentence()`, above - the SAME
    causal/continuation/reference/conditional leading-marker detection
    `_dependency_kind()` already uses one hop at a time for
    `_repair_orphans()`'s own rescue walk). This is a pure REGROUPING of
    that existing signal - deterministic segmentation only, no new
    keyword tables, no embeddings, no LLM judge, no second tokenizer,
    reusing exactly the helpers Sprint 1 already built and proved.

    Returns a list of `(start, end)` inclusive index tuples covering
    every sentence exactly once, in original order. A sentence with no
    dependent successor and that is not itself dependent forms its own
    single-sentence unit (this sprint's own "UNIT 1" worked example - a
    standalone, self-contained sentence needs no company).

    This function is intentionally NOT wired into `_repair_orphans()`'s
    own selection/rescue loop as a replacement mechanism - see that
    function's docstring for why: `_repair_orphans()`'s existing
    one-hop-at-a-time iterative walk already IS a context-preserving,
    bounded semantic-unit-preservation mechanism (proven by the existing
    `MANY_DEPENDENTS` pathological-chain regression test - see
    `tests/test_voice_response_intelligence.py`), and replacing it with
    an atomic "rescue the WHOLE unit or drop the WHOLE unit" pass was
    traced through that exact regression case and found to risk
    discarding the must-keep lead sentence whenever a long dependent
    chain exceeds the bounded repair cap - a regression, not an
    improvement. This function is instead used (a) directly by tests to
    verify unit boundaries match this sprint's own worked examples, and
    (b) as a building block for short-sentence FUNCTION classification
    below (a sentence that is the tail of a multi-sentence unit is, by
    definition, part of something that already needs its setup kept -
    see `_has_confirmation_lead()` and the sprint's change-impact doc)."""
    total = len(sentences)
    units: List[tuple] = []
    start = 0
    for i in range(1, total):
        if not _is_dependent_sentence(sentences, i, condition_indices):
            units.append((start, i - 1))
            start = i
    units.append((start, total - 1))
    return units


def _unit_bounds(units: List[tuple], i: int) -> tuple:
    """Returns the `(start, end)` unit range containing sentence index
    `i`. `units` is expected to come from `_build_semantic_units()`
    (covers every index exactly once, in order) - a plain linear scan is
    all that's warranted since a spoken reply is always a handful of
    sentences, never worth a bisect/index structure."""
    for start, end in units:
        if start <= i <= end:
            return (start, end)
    return (i, i)


#: Phase 5 (CRITICAL, per the sprint brief) - a short sentence must NOT
#: be automatically treated as low value, but the fix is NOT a blind
#: word-count bonus ("do NOT add a simple word count bonus that blindly
#: promotes every short sentence"). Instead this classifies one concrete
#: FUNCTION a short sentence commonly performs: reporting a status or
#: outcome ("Sudah terhubung.", "Berhasil diaktifkan.", "Gagal
#: terhubung.", "Relay sekarang aktif.") - a confirmation/completion
#: report is exactly as informative as a long explanation and is
#: routinely the ONE sentence a listener most needs to hear. Matched via
#: the SAME leading-window, word-boundary-anchored technique already
#: used by `_has_causal_lead()`/`_has_continuation_lead()`/
#: `_has_reference_lead()` above - no new matching mechanism invented.
_CONFIRMATION_KEYWORDS = (
    "sudah", "berhasil", "selesai", "siap", "aktif", "gagal", "belum",
    "sukses", "terhubung", "terputus",
    "done", "completed", "ready", "active", "failed", "successful",
    "connected", "disconnected",
)

_CONFIRMATION_RE = _compile_word_boundary_marker_pattern(_CONFIRMATION_KEYWORDS)

#: Only a genuinely SHORT sentence counts as a "status report" for this
#: bonus - a long sentence that merely contains one of these words
#: somewhere is an ordinary explanatory sentence, already scored on its
#: own merits (numbers/warnings/condition/lead), not this function.
_CONFIRMATION_MAX_WORDS = 6

#: Deliberately smaller than `_has_warning`'s +35 (a status report is
#: important but not a safety-critical warning) and than `_has_number`'s
#: +40, but enough to lift a short, otherwise-unscored confirmation
#: sentence above the generic "gentle earlier-is-better" tiebreak alone
#: would give it - see `_score_sentence()` below.
_CONFIRMATION_BONUS = 18.0


def _has_confirmation_lead(cleaned_lower: str) -> bool:
    """True if the sentence OPENS with a status/outcome word (see
    `_CONFIRMATION_KEYWORDS`) AND is short enough to plausibly BE just
    that status report, not a longer explanatory sentence that happens
    to use the same word. Anchored the same way `_has_leading_marker()`
    is - the word must open the sentence, not merely appear in it."""
    words = cleaned_lower.strip().split()
    if not words or len(words) > _CONFIRMATION_MAX_WORDS:
        return False
    return _has_leading_marker(cleaned_lower, _CONFIRMATION_RE)


# ─────────────────────────────────────────────
# Semantic Voice Selection & Coherent SHORT Mode sprint - GENERALIZES
# `_starts_list_run()` (Voice Output Naturalness sprint) from a per-
# sentence "does THIS sentence need its predecessor" signal into a full
# LIST RUN structure (setup / items / conclusion), so a bullet/numbered
# list can be selected or dropped as a coherent whole - the SAME
# structural primitives already in this module (`is_list_item`,
# adjacency), no new tokenizer/classifier/keyword table.
# ─────────────────────────────────────────────

def _find_list_runs(sentences: List[_Sentence]) -> List[dict]:
    """Groups consecutive `is_list_item` sentences into runs. Each run is
    `{"setup": int|None, "items": [int, ...], "conclusion": int|None}`.

    `setup` is the sentence immediately before the run, if that sentence
    is itself not a list item (Phase 1's LIST_SETUP - "Berikut beberapa
    pilihan mikrofon:"). `conclusion` is the sentence immediately after
    the run's last item, if that sentence is not itself a list item AND
    is not ALSO another run's own setup (Phase 1's LIST_CONCLUSION,
    Phase 9 scenario 16 "two separate lists" - a short sentence between
    two lists is the SECOND list's setup, not the FIRST list's
    conclusion). Purely positional/structural - no keyword table, no
    `_has_conclusion_cue()` dependency (that fixed-phrase check is the
    root cause of dropping a genuine closing/answer sentence that
    doesn't happen to contain "jadi"/"kesimpulannya"/etc. - see
    docs/change_impact/semantic_voice_selection.md's root-cause section)."""
    runs: List[dict] = []
    n = len(sentences)
    i = 0
    while i < n:
        if sentences[i].is_list_item and (i == 0 or not sentences[i - 1].is_list_item):
            start = i
            j = i
            while j < n and sentences[j].is_list_item:
                j += 1
            end = j - 1
            setup = start - 1 if start > 0 else None
            runs.append({"setup": setup, "items": list(range(start, end + 1)), "conclusion": None})
            i = j
        else:
            i += 1
    setups = {r["setup"] for r in runs if r["setup"] is not None}
    for r in runs:
        cand = r["items"][-1] + 1
        if cand < n and not sentences[cand].is_list_item and cand not in setups:
            r["conclusion"] = cand
    return runs


#: Bonus for a list item whose own DISTINCTIVE words (words not shared by
#: every item in its run - see `_list_run_relevant_items()`) also appear
#: in the run's own conclusion sentence - i.e. the item the response's
#: OWN closing sentence actually singles out as the answer (Phase 5
#: scenario C - "Mana yang paling bagus?" should keep the relevant item +
#: reasoning + conclusion, not every item). Deterministic word-overlap
#: only (reuses `_word_set()` - the SAME Jaccard-style primitive
#: `_dedupe()` already established in this module), never embeddings,
#: never an LLM judge, never a second ranking system. Scaled between
#: `_CONDITION_SETUP_BONUS` (+12, a much weaker indirect signal) and
#: `_has_warning`'s hard must-keep - strong enough to reliably outrank an
#: irrelevant sibling item under a tight budget, not strong enough to
#: outrank an actual safety warning or number-bearing sentence elsewhere
#: in the reply.
_LIST_RELEVANCE_BONUS = 22.0


def _list_run_relevant_items(sentences: List[_Sentence], run: dict) -> set:
    """Returns the subset of `run["items"]` whose distinctive words (their
    own words MINUS whatever words every item in the run shares - e.g. a
    device name shared by all items, or the word "mikrofon" repeated in
    every line, is not "distinctive") overlap the run's own conclusion
    sentence, if it has one. Empty set when the run has no conclusion, or
    when nothing distinctive lines up - meaning "no relevance signal was
    found", the honest, common case for an ordinary enumerated list with
    no closing recommendation."""
    conclusion = run.get("conclusion")
    items = run.get("items") or []
    if conclusion is None or len(items) < 2:
        return set()
    concl_words = _word_set(sentences[conclusion].cleaned)
    if not concl_words:
        return set()
    item_word_sets = {idx: _word_set(sentences[idx].cleaned) for idx in items}
    common = set.intersection(*item_word_sets.values()) if item_word_sets else set()
    relevant = set()
    for idx, words in item_word_sets.items():
        distinctive = words - common
        if distinctive & concl_words:
            relevant.add(idx)
    return relevant


def _apply_list_relevance_bonus(sentences: List[_Sentence], scored_pairs: List[tuple], list_runs: List[dict]) -> List[tuple]:
    bonus_indices: set = set()
    for run in list_runs:
        bonus_indices |= _list_run_relevant_items(sentences, run)
    if not bonus_indices:
        return scored_pairs
    return [(i, score + _LIST_RELEVANCE_BONUS if i in bonus_indices else score) for i, score in scored_pairs]


#: Slack for list-run-coherence rescue - mirrors `_repair_orphans()`'s own
#: `cap = min(total, budget + 4)` bounded-slack philosophy exactly (same
#: "+4" constant, not a new number invented from nothing), so a list
#: run's coherence rescue never blows the budget open more aggressively
#: than the pre-existing dependent-sentence rescue already does.
_LIST_RUN_REPAIR_SLACK = 4


def _repair_list_run_coherence(
    sentences: List[_Sentence], keep: set, must_keep: set, budget: int, total: int,
    list_runs: List[dict], scored_map: dict,
) -> set:
    """Deterministic, bounded generalization of `_repair_orphans()`'s own
    one-hop rescue-or-drop philosophy (Phase 3), applied to a WHOLE list
    run instead of a single dependent sentence's single predecessor:

    - If >= 1 item of a run is in `keep` (or independently must-keep,
      e.g. it carries its own warning), the run's setup/conclusion MUST
      also end up in `keep` - never a bullet with no introducing
      sentence, never a dangling "Berikut pilihannya:" with nothing
      after it. Bounded by `_LIST_RUN_REPAIR_SLACK`; if genuinely out of
      room, the run's own (non-must-keep) items are dropped entirely
      rather than left half-introduced - the SAME "rescue or drop,
      never leave a fragment" rule `_repair_orphans()` already applies,
      just at run granularity.
    - If a run's setup already survived (most commonly because it is
      sentence 0, the unconditionally must-keep lead) but NO item of
      that run did, the single highest-scored item is rescued - this is
      the direct, reproduced fix for "setup selected, payload dropped"
      (see Phase 0's DETAILED-mode reproduction in the change-impact
      doc) - Phase 3's explicit "jangan pernah menghasilkan kalimat
      setup tanpa payload"."""
    keep = set(keep)
    cap = min(total, budget + _LIST_RUN_REPAIR_SLACK)

    for run in list_runs:
        setup, items, conclusion = run["setup"], run["items"], run["conclusion"]
        selected_items = [idx for idx in items if idx in keep]
        forced = any(idx in must_keep for idx in items)

        if not selected_items and not forced:
            if setup is not None and setup in keep and items:
                best = max(items, key=lambda idx: scored_map.get(idx, 0.0))
                if len(keep) < cap:
                    keep.add(best)
                    selected_items = [best]
                else:
                    continue
            else:
                continue

        for side in (setup, conclusion):
            if side is None or side in keep:
                continue
            if forced or len(keep) < cap:
                keep.add(side)
            else:
                # No room left for this run's own context sentence -
                # never leave a headless/dangling fragment; drop the
                # run's (non-must-keep) items instead of showing them
                # without it.
                for idx in selected_items:
                    if idx not in must_keep:
                        keep.discard(idx)
    return keep


def _select_scores_with_setup_bonus(sentences: List[_Sentence], candidates: List[int], total: int) -> List[tuple]:
    """Computes `_score_sentence()` for every `candidates` index, then adds
    `_CONDITION_SETUP_BONUS` to any candidate `i` whose immediate successor
    `i + 1` is DEPENDENT (conditional/causal/continuation/reference - see
    `_is_dependent_sentence()` above) - see `_CONDITION_SETUP_BONUS`'s own
    docstring. `condition_indices` is computed once, over ALL sentences
    (not just candidates), since the conditional sentence that triggers
    the bonus may itself already be a must-keep sentence (e.g. the lead) -
    the bonus still correctly applies to whatever immediately precedes it.
    Never applied to a sentence that is itself DEPENDENT (would
    double-count two adjacent dependent sentences rather than protecting
    one dependent sentence's own setup). Originally only checked soft
    conditionals (Voice Output Coherence sprint); generalized to the three
    additional marker categories by the Voice Response Intelligence sprint
    - same mechanism, same bonus constant, purely additive (a superset of
    triggers), so every previously-covered case behaves identically.

    Voice Output Naturalness sprint: ALSO applies when `i + 1` is the
    first item of a list run (`_starts_list_run()`) - the same setup-
    protection reasoning, extended to list-preceding sentences. This is
    scoring only (a soft nudge); the HARD guarantee that a selected list
    run's setup sentence is never left behind is `_repair_orphans()`'s
    own job below, exactly mirroring how the discourse-marker case is
    ALSO hard-guaranteed there, not left to scoring alone."""
    condition_indices = {i for i, s in enumerate(sentences) if _has_condition(s.cleaned.lower())}

    def _scored(i: int) -> float:
        base = _score_sentence(sentences[i].raw, sentences[i].cleaned, i, total)
        nxt = i + 1
        if nxt < total and not sentences[i].is_list_item:
            if _is_dependent_sentence(sentences, nxt, condition_indices) and not _is_dependent_sentence(sentences, i, condition_indices):
                base += _CONDITION_SETUP_BONUS
            elif _starts_list_run(sentences, nxt):
                base += _CONDITION_SETUP_BONUS
        return base

    return [(i, _scored(i)) for i in candidates]


def _repair_orphans(
    sentences: List[_Sentence], keep_indices: set, budget: int, total: int, condition_indices: set,
) -> set:
    """Context-preserving selection (Sprint 1 - Voice Response
    Intelligence) - a deterministic POST-selection pass, stronger than the
    scoring bonus above can guarantee on its own (a bonus can still lose
    to enough competing high scorers under a tight budget). Removes any
    selected DEPENDENT sentence whose immediate predecessor (`i - 1`) is
    NOT also selected (the exact symptom reported: an individually
    "important" sentence survives, like a conditional or a causal
    "Akibatnya, ..." clause, but the sentence it depends on for its
    meaning to land does not), then either RESCUES it by admitting its
    immediate predecessor (bounded by `cap`, below - this sprint's "voice
    budget is an information budget, not a sentence-count target" framing,
    the SAME "correctness outranks hitting an exact target length"
    principle `_select_by_priority`'s own must-keep set already
    established for warnings), or DROPS it permanently if even that
    bounded slack is exhausted - never a partial state where the orphan
    survives without its predecessor, since that is exactly the bug being
    fixed.

    Iterates to a FIXED POINT rather than a single pass: rescuing a
    predecessor can itself introduce a new orphan one hop further back
    (e.g. a run of "Selanjutnya, langkah N..." sentences, each depending
    on the one before it) - each iteration still only ever looks at ONE
    sentence and its ONE immediate predecessor (no dependency graph, no
    lookahead/lookbehind beyond one hop, no semantic understanding), it
    just repeats that same bounded check until nothing changes. Bounded
    by `total` iterations (one sentence can be newly implicated per
    iteration at most), so this always terminates.

    Voice Pipeline Latency & Semantic Speech Segmentation sprint (Sprint
    2, Phase 3/6): this IS the codebase's semantic-speech-unit-
    preservation mechanism - `_build_semantic_units()` (above) names the
    same underlying one-hop DEPENDENT-chain relationship this function
    already walks, and this function's fixed-point iteration already
    achieves "keep the whole unit together, bounded" in practice (each
    iteration rescues one more hop of the chain until either the whole
    unit is present or the bounded `cap` below is hit). Sprint 2
    deliberately did NOT replace this with an atomic
    "rescue-the-whole-unit-or-drop-the-whole-unit" pass - tracing that
    alternative through the existing `MANY_DEPENDENTS` pathological
    long-chain regression test (`tests/test_voice_response_intelligence.
    py`) showed it would risk discarding the must-keep lead sentence
    whenever a long dependent chain exceeds the bounded repair cap. The
    existing per-hop walk is both already-proven and strictly safer, so
    it remains the SINGLE selection/repair authority (no second selector
    or ranking system was introduced - see Phase 17's explicit
    prohibition).

    Voice Output Naturalness sprint: the orphan check below ALSO covers
    a selected list run whose own setup sentence isn't selected
    (`_starts_list_run()`) - the exact reproduced production bug ("TTS
    speaks mainly the bullets, skipping the sentence that introduces
    them"). List items are frequently must-keep already
    (`protect_list_items`), which is precisely why a scoring bonus alone
    (see `_select_scores_with_setup_bonus()`) is not enough - a long list
    run can consume the ENTIRE remaining budget before the setup sentence
    ever gets a chance to be picked by score, exactly the mechanism that
    produced the reported bug. This hard rescue-or-drop guarantee is the
    fix, mirrored one-for-one from the pre-existing discourse-marker
    case, not a second mechanism."""
    keep = set(keep_indices)

    #: Bounded slack - generous enough that a typical 1-2-orphan reply
    #: (this sprint's own worked examples) fully rescues every pair, while
    #: still preventing a many-dependent-sentence reply from silently
    #: ballooning voice_text back to nearly the full, uncompressed length.
    cap = min(total, budget + 4)

    for _ in range(total):
        orphans = [
            i for i in sorted(keep)
            if i != 0 and (i - 1) not in keep and (
                _is_dependent_sentence(sentences, i, condition_indices) or _starts_list_run(sentences, i)
            )
        ]
        if not orphans:
            break
        # Highest-scored orphan first - most "important" dependent
        # sentence gets first claim on the limited rescue slack.
        i = max(
            orphans,
            key=lambda idx: _score_sentence(sentences[idx].raw, sentences[idx].cleaned, idx, total),
        )
        pred = i - 1
        if len(keep) + 1 <= cap:
            keep.add(pred)  # rescued - `pred` itself is re-checked next iteration
        else:
            keep.discard(i)  # doesn't fit even under the generous cap - drop, never dangle
    return keep


def _score_sentence(raw: str, cleaned: str, index: int, total: int) -> float:
    cleaned_lower = cleaned.lower()
    score = 0.0
    if index == 0:
        score += 100.0
    if _has_number(raw):
        score += 40.0
    if _has_warning(cleaned_lower):
        score += 35.0
    if _has_condition(cleaned_lower):
        score += 20.0  # soft boost only - see `_CONDITION_KEYWORDS` docstring
    if index == total - 1 and _has_conclusion_cue(cleaned_lower):
        score += 15.0
    if _has_confirmation_lead(cleaned_lower):
        score += _CONFIRMATION_BONUS  # Phase 5 (Sprint 2) - short status/outcome report
    score += max(0.0, 5.0 - index * 0.1)  # gentle earlier-is-better tiebreak
    return score


def _compute_budget(sentence_count: int) -> int:
    """DETAILED's own budget - unchanged from the prior sprint (this
    depth's ratio/floor were deliberately left untouched, see module
    docstring's DEPTH BEHAVIOR section)."""
    return max(_DETAILED_BUDGET_FLOOR, math.ceil(sentence_count * _DETAILED_BUDGET_RATIO))


def _compute_budget_for_depth(depth: str, sentence_count: int) -> int:
    """Voice Output Optimization sprint - generalizes budget computation
    to all three depths (previously DETAILED-only). SHORT is the most
    aggressive (fewest sentences kept), NORMAL moderate, DETAILED
    unchanged/least aggressive - matching the brief's own "SHORT keeps
    least, DETAILED retains most" ordering."""
    if depth == DEPTH_SHORT:
        floor, ratio = _SHORT_BUDGET_FLOOR, _SHORT_BUDGET_RATIO
    elif depth == DEPTH_DETAILED:
        floor, ratio = _DETAILED_BUDGET_FLOOR, _DETAILED_BUDGET_RATIO
    else:
        floor, ratio = _NORMAL_BUDGET_FLOOR, _NORMAL_BUDGET_RATIO
    return max(floor, math.ceil(sentence_count * ratio))


def _select_by_priority(
    sentences: List[_Sentence], budget: int, *, protect_list_items: bool = False,
) -> List[_Sentence]:
    """Selects up to `budget` sentences, but the lead sentence and EVERY
    sentence carrying a warning/imperative keyword are always kept
    regardless of score or budget ("preserve critical warnings" is a
    hard requirement, not just a scoring nudge - see the sprint's own
    semantic-safety test requirements). If those "always keep" sentences
    alone exceed `budget`, the effective result is larger than `budget`
    - correctness (never silently dropping a warning) outranks hitting
    an exact target length, and the brief itself states the sentence
    count is "NOT a hard limit".

    `protect_list_items` (Voice Output Optimization sprint, additive,
    default False so DETAILED's existing, already-tested selection
    behavior is unaffected by this flag) - when True, a list run with NO
    detectable relevance signal (`_list_run_relevant_items()` - see
    Semantic Voice Selection sprint) is kept in full, exactly as before
    (the safe default for an ordinary enumerated list with no closing
    recommendation to key off of). When the run's OWN conclusion sentence
    DOES single out a specific item (Phase 5 scenario C), that item is no
    longer blanket-protected - it now competes and wins on its boosted
    score instead, which lets a genuinely irrelevant sibling item be
    dropped while the run's setup/conclusion still travel with whichever
    item(s) survive (`_repair_list_run_coherence()`, below). This is an
    intentional relaxation of the PRIOR blanket "every list item is
    always must-keep" rule - see docs/change_impact/semantic_voice_selection.md
    for the reasoning and the concrete before/after examples."""
    total = len(sentences)
    if budget >= total:
        return sentences

    list_runs = _find_list_runs(sentences)

    must_keep = set()
    for i, s in enumerate(sentences):
        if i == 0:
            must_keep.add(i)
        elif _has_warning(s.cleaned.lower()):
            must_keep.add(i)
        elif i == total - 1 and _has_conclusion_cue(s.cleaned.lower()):
            must_keep.add(i)

    if protect_list_items:
        for run in list_runs:
            if not _list_run_relevant_items(sentences, run):
                must_keep.update(run["items"])

    condition_indices = {i for i, s in enumerate(sentences) if _has_condition(s.cleaned.lower())}
    scored_pairs = _apply_list_relevance_bonus(
        sentences, _select_scores_with_setup_bonus(sentences, list(range(total)), total), list_runs,
    )
    scored_map = dict(scored_pairs)

    remaining_budget = max(0, budget - len(must_keep))
    candidates = [i for i in range(total) if i not in must_keep]
    scored = sorted(((i, scored_map[i]) for i in candidates), key=lambda t: t[1], reverse=True)
    keep = must_keep | {i for i, _score in scored[:remaining_budget]}

    # Semantic Voice Selection sprint - run-level coherence FIRST (never
    # leave a bullet without its setup/conclusion, never leave a setup
    # with no payload), THEN the pre-existing Sprint 1 dependent-sentence
    # repair (never leave a causal/continuation/reference/conditional
    # opener without its own immediate predecessor also selected) -
    # completely unchanged, still runs on whatever `keep` looks like at
    # this point, still the final safety net for the non-list case (see
    # `_repair_orphans()`).
    keep = _repair_list_run_coherence(sentences, keep, must_keep, budget, total, list_runs, scored_map)
    keep = _repair_orphans(sentences, keep, budget, total, condition_indices)

    return [sentences[i] for i in sorted(keep)]


_TERMINAL_PUNCT = (".", "!", "?", ",")


def _join_sentences(sentences: List[_Sentence]) -> str:
    """Joins selected sentences into the final spoken string. Consecutive
    list-item sentences are comma-joined ("Wi-Fi, MQTT, Home Assistant"
    - the same "line break becomes a pause" spirit `normalize_for_speech`
    already applies within a single call, preserved here even though
    each list item was cleaned as its own separate chunk) UNLESS the
    first of the pair already ends in its own terminal punctuation (a
    numbered-list item that was a full sentence, e.g. "ESP32 publish
    data.") - stacking a comma right after a period reads wrong, so a
    plain space is used instead in that case."""
    parts: List[str] = []
    for i, s in enumerate(sentences):
        if i == 0:
            parts.append(s.cleaned)
            continue
        prev = sentences[i - 1]
        wants_comma = prev.is_list_item and s.is_list_item and not prev.cleaned.endswith(_TERMINAL_PUNCT)
        sep = ", " if wants_comma else " "
        parts.append(sep)
        parts.append(s.cleaned)
    return "".join(parts).strip()


# ─────────────────────────────────────────────
#  TTS Chunking/Streaming (NEW - see module docstring). Operates on the
#  SAME `_Sentence` list already produced for `voice_text` - never a
#  second/independent split of the raw text.
# ─────────────────────────────────────────────

#: Ceiling/safety-net only (see module docstring) - most chunks will be
#: well under this. Mirrors `luno.config.VOICE_CHUNK_MAX_CHARS`'s own
#: default; kept as a plain literal here (not imported from `config`) so
#: this module stays a pure function of its arguments with zero I/O/env
#: coupling, matching its own "no I/O, no randomness" contract above -
#: callers that want the configured value pass it explicitly.
DEFAULT_MAX_CHUNK_CHARS = 220

#: Comma/semicolon boundary - used only to split a single sentence that
#: is ALREADY longer than `max_chunk_chars` on its own. Never applied to
#: ordinary-length sentences (those are always one sentence = one chunk).
_CLAUSE_BOUNDARY_RE = re.compile(r'(?<=[,;])\s+')


def _split_at_whitespace(cleaned: str, max_chars: int) -> List[str]:
    """Last-resort fallback ONLY - reached when a single sentence/clause
    has no comma/semicolon at all and still exceeds `max_chars` (e.g. one
    very long unbroken run of words). Splits at whitespace boundaries,
    never mid-word. If a single WORD alone exceeds `max_chars` (a long
    URL, a long identifier), it is kept whole rather than cut - never
    slicing inside a URL/number/identifier is a hard requirement, not a
    nice-to-have."""
    words = [w for w in cleaned.split(" ") if w]
    if not words:
        return [cleaned] if cleaned else []
    pieces: List[str] = []
    buf = ""
    for w in words:
        trial = f"{buf} {w}".strip() if buf else w
        if buf and len(trial) > max_chars:
            pieces.append(buf)
            buf = w
        else:
            buf = trial
    if buf:
        pieces.append(buf)
    return pieces


def _split_long_sentence(cleaned: str, max_chars: int) -> List[str]:
    """Splits ONE already-cleaned sentence that exceeds `max_chars` into
    multiple pieces - clause (comma/semicolon) boundaries first (keeps
    the cut at a natural spoken pause), whitespace boundaries only as a
    fallback for a piece that still doesn't fit (or has no clause
    punctuation at all). Character-count slicing is never the PRIMARY
    strategy here - it is only ever the last-resort boundary choice."""
    if len(cleaned) <= max_chars:
        return [cleaned]

    clauses = [c for c in _CLAUSE_BOUNDARY_RE.split(cleaned) if c]
    if len(clauses) <= 1:
        return _split_at_whitespace(cleaned, max_chars)

    grouped: List[str] = []
    buf = ""
    for c in clauses:
        trial = f"{buf} {c}".strip() if buf else c
        if buf and len(trial) > max_chars:
            grouped.append(buf.strip())
            buf = c
        else:
            buf = trial
    if buf:
        grouped.append(buf.strip())

    final: List[str] = []
    for piece in grouped:
        if len(piece) <= max_chars:
            final.append(piece)
        else:
            final.extend(_split_at_whitespace(piece, max_chars))
    return final


class _ChunkPair(NamedTuple):
    """One playback-sized chunk's two text forms - `raw` (reference/
    debug/`SpeechChunk.raw_text`) and `cleaned` (what's actually spoken,
    `SpeechChunk.text`/`voice_chunks`)."""
    raw: str
    cleaned: str


def _join_raw(sentences: List[_Sentence]) -> str:
    """Reference-only counterpart to `_join_sentences()` for a group's
    RAW (still markdown-bearing) text - a plain space-join is sufficient
    here since `raw_text` is a debug/observability/correlation field, not
    itself sent to the TTS engine (only `cleaned`/`text` is)."""
    return " ".join(s.raw.strip() for s in sentences if s.raw.strip())


def _group_sentences_into_chunk_pairs(sentences: List[_Sentence], max_chars: int) -> List[_ChunkPair]:
    """Groups already-cleaned `_Sentence`s (the SAME list `voice_text` was
    joined from) into playback-sized chunks, in order, returning BOTH the
    spoken (`cleaned`) text AND a reference (`raw`) text for each chunk -
    used by the TTS Chunk Queue sprint's `SpeechChunk.raw_text`/`.text`
    fields (see `luno/speech_chunk.py`). Default granularity is one
    ordinary sentence = one chunk; consecutive list-item sentences are
    grouped into a single comma-joined chunk (matching `_join_sentences`'s
    own list-reading style) unless that would exceed `max_chars`, in
    which case the run splits at list-item boundaries. A sentence longer
    than `max_chars` on its own is split by `_split_long_sentence()` -
    for that rare fallback case only, `raw` degrades to the same
    (already-cleaned) text as `cleaned` for each resulting piece, since a
    single raw sentence has no natural N-way split aligned with its
    cleaned counterpart's pieces (documented limitation, see module
    docstring). Deterministic - a single forward pass, pure function of
    its arguments, same input always produces the same ordered output."""
    if not sentences:
        return []

    pairs: List[_ChunkPair] = []
    buffer: List[_Sentence] = []

    def _flush() -> None:
        if buffer:
            pairs.append(_ChunkPair(_join_raw(list(buffer)), _join_sentences(list(buffer))))
            buffer.clear()

    for s in sentences:
        if len(s.cleaned) > max_chars:
            _flush()
            for piece in _split_long_sentence(s.cleaned, max_chars):
                pairs.append(_ChunkPair(piece, piece))  # degraded raw==cleaned, see docstring
            continue
        if buffer and s.is_list_item and buffer[-1].is_list_item:
            trial_len = len(_join_sentences(buffer + [s]))
            if trial_len <= max_chars:
                buffer.append(s)
                continue
            _flush()
            buffer.append(s)
            continue
        _flush()
        buffer.append(s)
    _flush()
    return pairs


def _group_sentences_into_chunks(sentences: List[_Sentence], max_chars: int) -> List[str]:
    """Thin, cleaned-text-only view of `_group_sentences_into_chunk_pairs()`
    - kept as its own name for callers that only need `voice_chunks`
    (no raw/reference text)."""
    return [cleaned for _, cleaned in _group_sentences_into_chunk_pairs(sentences, max_chars)]


# ─────────────────────────────────────────────
#  Public entry point
# ─────────────────────────────────────────────

def _resolve_depth(response_policy: Union[ResponsePolicy, str, None]) -> str:
    depth = getattr(response_policy, "depth", None)
    if depth is None:
        depth = response_policy if isinstance(response_policy, str) else None
    if depth not in (DEPTH_SHORT, DEPTH_NORMAL, DEPTH_DETAILED):
        depth = DEPTH_NORMAL
    return depth


def _resolve_explicit(response_policy: Union[ResponsePolicy, str, None]) -> bool:
    """Voice Output Optimization sprint - reads `ResponsePolicy.explicit`
    (already computed once per turn by `compute_response_policy()`; see
    that module's `_EXPLICIT_SHORT_PHRASES`/`_EXPLICIT_DETAILED_PHRASES`)
    so this module can honor an explicit "give me full detail"/"keep it
    short" instruction WITHOUT running any text-matching of its own - "use
    the existing response-depth policy where possible instead of creating
    a competing classifier" (the brief's own instruction). Plain-string
    `response_policy` callers (no `ResponsePolicy` object available) get
    `False` - same defensive default `_resolve_depth()` already uses."""
    return bool(getattr(response_policy, "explicit", False))


def build_dual_response(
    response_text: str,
    response_policy: Union[ResponsePolicy, str, None],
    *,
    language: Optional[str] = None,
    request_id: Optional[str] = None,
    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS,
    voice_output_mode: Optional[str] = None,
) -> DualResponse:
    """The one entry point this module exposes. Pure function of
    `(response_text, response_policy, voice_output_mode)` - no I/O, no
    randomness, no LLM call, no memory access. `response_policy` may be a
    `ResponsePolicy` (the normal case - the SAME object the caller
    already computed once this turn via
    `luno.response_policy.compute_response_policy()`) or a plain depth
    string, for callers that only kept the resolved depth. `language`
    mirrors `normalize_for_speech()`'s own optional parameter (defaults
    to `LUNO_LANGUAGE`/`"english"` when omitted, same as that function).
    `max_chunk_chars` is the TTS Chunking sprint's ceiling for
    `voice_chunks` (see module docstring) - callers typically pass
    `luno.config.VOICE_CHUNK_MAX_CHARS` explicitly; this module never
    reads config/env itself. Safe to call with empty/None `response_text`
    - `chat_text`/`voice_text` come back as `""` and `voice_chunks` as
    `[]`, never raises.

    `voice_output_mode` (Voice Output Mode sprint - additive) - resolved
    via `luno.voice_output_mode.resolve_voice_output_mode()` (never
    raises, falls back to `"SHORT"` for `None`/invalid). `"SHORT"` is
    byte-identical to this function's pre-existing behavior (the
    `skip_compression`/budget/`_select_by_priority()` path below is
    completely untouched). `"ALL"` bypasses BOTH `_dedupe()` AND
    priority-selection entirely - the brief's own explicit "jangan drop
    sentence" requirement is stricter than the pre-existing "explicit
    DETAILED" skip (which only ever skipped priority-selection, not
    dedup) - so `selected` becomes the full, unmodified sentence list.
    `chat_text` is never touched by either mode - it is always the raw
    `response_text`, unconditionally."""
    depth = _resolve_depth(response_policy)
    explicit = _resolve_explicit(response_policy)
    mode = resolve_voice_output_mode(voice_output_mode)

    if not response_text:
        empty = response_text or ""
        return DualResponse(
            chat_text=empty, voice_text=empty, depth=depth,
            request_id=request_id, voice_adapted=False, voice_chunks=[],
            voice_output_mode=mode,
        )

    chat_text = response_text

    raw_chunks = _split_into_raw_sentences(response_text)
    sentences: List[_Sentence] = []
    for chunk in raw_chunks:
        cleaned = normalize_for_speech(_strip_list_markers(chunk.raw), language=language).strip()
        if cleaned:
            sentences.append(_Sentence(chunk.raw, cleaned, chunk.is_list_item))

    if not sentences:
        # Splitter found nothing sentence-shaped (e.g. the whole reply is
        # one bare code block, or punctuation-free text) - fall back to
        # cleaning the whole text in one pass rather than returning "".
        fallback = normalize_for_speech(response_text, language=language).strip()
        fallback_chunks = [fallback] if fallback else []
        return DualResponse(
            chat_text=chat_text, voice_text=fallback, depth=depth,
            request_id=request_id, voice_adapted=False, voice_chunks=fallback_chunks,
            voice_chunks_raw=fallback_chunks, voice_output_mode=mode,
        )

    if mode == VOICE_OUTPUT_MODE_ALL:
        # Voice Output Mode sprint - "ALL" bypasses voice compression
        # ENTIRELY: no dedup, no priority-budget selection, no orphan
        # repair, no summarization, no second LLM call. Every sentence
        # the splitter found (still cleaned through the SAME mandatory
        # `normalize_for_speech()` pass above - that is TTS legibility,
        # not compression) reaches `voice_text`/`voice_chunks` in full,
        # original order. `voice_adapted` is False here by construction -
        # nothing was adapted/removed, matching that field's own
        # documented contract.
        selected: List[_Sentence] = sentences
        voice_adapted = False
    else:
        deduped = _dedupe(sentences)
        voice_adapted = len(deduped) < len(sentences)

        selected = deduped
        # Voice Output Optimization sprint - budget-based priority selection
        # now applies at every depth (previously DETAILED-only; see module
        # docstring's DEPTH BEHAVIOR section). An EXPLICIT "give me full
        # detail" instruction (`explicit=True` on a DETAILED-depth policy -
        # e.g. "jelaskan semuanya"/"secara lengkap"/"jangan disingkat", see
        # `luno.response_policy._EXPLICIT_DETAILED_PHRASES`) skips compression
        # entirely for this turn - the brief's own "must NOT aggressively
        # shorten when the user explicitly asked for full detail" requirement,
        # satisfied by reading the EXISTING policy's own `explicit` flag
        # rather than re-detecting intent here.
        skip_compression = explicit and depth == DEPTH_DETAILED
        if not skip_compression:
            budget = _compute_budget_for_depth(depth, len(deduped))
            if budget < len(deduped):
                protect_list_items = depth != DEPTH_DETAILED
                selected = _select_by_priority(deduped, budget, protect_list_items=protect_list_items)
                voice_adapted = True

    voice_text = _join_sentences(selected)
    chunk_pairs = _group_sentences_into_chunk_pairs(selected, max_chunk_chars)
    voice_chunks = [cleaned for _, cleaned in chunk_pairs]
    voice_chunks_raw = [raw for raw, _ in chunk_pairs]
    return DualResponse(
        chat_text=chat_text, voice_text=voice_text, depth=depth,
        voice_chunks_raw=voice_chunks_raw, voice_output_mode=mode,
        request_id=request_id, voice_adapted=voice_adapted, voice_chunks=voice_chunks,
    )
