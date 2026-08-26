"""
response_policy.py
====================

Response Depth Policy sprint - a small, deterministic, explainable
policy that decides how LONG/detailed Luno's reply to a given user turn
should be: `"short"` / `"normal"` / `"detailed"`. Computed once per
turn from plain text (plus one optional, ephemeral, in-memory
continuation hint) - no LLM call, no external API, no second AI model,
no new tokenizer, no persistent storage.

SCOPE (this sprint only):
  - Decide the depth. Return a structured, inspectable
    `ResponsePolicy`.
  - Do NOT implement Chat-vs-Voice dual output.
  - Do NOT implement TTS chunking/summarization.
  - Do NOT persist a depth preference anywhere (not to
    `long_term_memory.json`, not anywhere else) - a caller MAY keep the
    last resolved depth in its own bounded, in-memory, per-conversation
    state (mirroring `main_runtime_demo.py`'s existing
    `_session_feedback_target` convention) and pass it back in via
    `previous_score`/`previous_depth` next turn, but this module itself
    is stateless and persists nothing.

DECISION PRECEDENCE (highest first):
  1. Explicit user instruction ("jawab singkat", "jelaskan detail", ...)
  2. Conversational context / follow-up state (`previous_score`,
     confusion/repeat-request signals)
  3. Request/task type (definition, how-to, troubleshooting,
     comparison, tutorial, architecture/deep-analysis)
  4. Question complexity (multiple questions, multiple concepts)
  5. Default NORMAL

PHILOSOPHY - choose the SMALLEST useful depth. A long sentence does not
mean a detailed answer is needed; a technical question is not
automatically DETAILED; continuing a conversation does not automatically
mean DETAILED either - it only prevents an unrelated, jarring drop to
SHORT when the follow-up itself is short.

NAMING NOTE: `luno/emotion_engine.py` ALREADY defines its OWN, unrelated
`ResponsePolicy` dataclass (small signed -1/0/+1 tone leans - warmth/
humor/verbosity/etc., derived from estimated user emotion). That is a
DIFFERENT concept from this module's `ResponsePolicy` (depth/score/
reasons/explicit) - the two are never imported into the same name in
`main_runtime_demo.py` (only `derive_response_policy()` is imported from
`emotion_engine`, bound to a separate `_emotion_policy` variable; this
module's result is bound to `response_policy`), but future readers
should not confuse the two `ResponsePolicy` classes living in different
modules for the same thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

#: Public depth levels - the ONLY three values ever exposed to the rest
#: of the application (`ResponsePolicy.depth`). No MINIMAL/EXHAUSTIVE
#: level is exposed - the brief's own "do not overengineer" / "do not
#: expose unnecessary complexity" rule. Internally this module works
#: entirely in a single bounded 0-100 `score` and buckets it into one
#: of these three at the very end - no separate 5-level internal enum
#: was needed to implement the required behavior.
DEPTH_SHORT = "short"
DEPTH_NORMAL = "normal"
DEPTH_DETAILED = "detailed"

#: Score thresholds (inclusive lower bound), per the sprint spec:
#: 0-29 SHORT, 30-64 NORMAL, 65-100 DETAILED.
_SHORT_MAX = 29
_NORMAL_MAX = 64
SCORE_MIN = 0
SCORE_MAX = 100


@dataclass
class ResponsePolicy:
    """The one, structured result this module produces. `reasons` is a
    bounded list of short, machine-stable string tags (not prose) -
    useful for debugging/dashboard display, never shown to the end user
    verbatim. `task_type` is an additional, genuinely useful field (not
    required by the minimum spec) exposing which deterministic bucket
    (if any) drove the heuristic score - `None` when an explicit
    instruction short-circuited task classification entirely."""

    depth: str
    score: int
    reasons: List[str] = field(default_factory=list)
    explicit: bool = False
    task_type: Optional[str] = None

    def to_dict(self) -> dict:
        """Read-only, dashboard/debug-friendly shape - never used to
        reconstruct a `ResponsePolicy` (this module is stateless; there
        is no `from_dict`)."""
        return {
            "depth": self.depth,
            "score": self.score,
            "reasons": list(self.reasons),
            "explicit": self.explicit,
            "task_type": self.task_type,
        }


def _score_to_depth(score: int) -> str:
    if score <= _SHORT_MAX:
        return DEPTH_SHORT
    if score <= _NORMAL_MAX:
        return DEPTH_NORMAL
    return DEPTH_DETAILED


def _clamp_score(score: float) -> int:
    return max(SCORE_MIN, min(SCORE_MAX, round(score)))


# ─────────────────────────────────────────────
#  1. Explicit user instruction - highest precedence, always wins.
#  Deterministic phrase matching (substring, on lowercased/normalized
#  text) - bounded, hand-curated lists, not a fuzzy NLP classifier.
# ─────────────────────────────────────────────

#: Indonesian-first (this project's primary conversational language),
#: with the equivalent common English phrasings included too, matching
#: this codebase's existing bilingual convention (see e.g.
#: `luno/memory.py`'s own `_CATEGORY_KEYWORDS`).
_EXPLICIT_SHORT_PHRASES = (
    "jawab singkat", "jawab yang singkat", "singkat aja", "singkat saja",
    "yang singkat aja", "ringkas aja", "ringkas saja", "ringkas",
    "intinya aja", "intinya saja", "intinya?", "intinya",
    "langsung jawab", "langsung ke intinya", "langsung intinya",
    "nggak usah panjang", "ga usah panjang", "gausah panjang",
    "gak usah panjang", "tidak usah panjang", "jangan panjang panjang",
    "jangan panjang-panjang", "pendek aja", "pendek saja",
    "to the point", "short answer", "keep it short", "briefly please",
    "just the gist", "just answer briefly", "answer briefly",
    # Voice Output Optimization sprint - additive, matches the brief's own
    # explicit brevity-trigger list ("singkat"/"ringkas"/"intinya"/"TL;DR"/
    # "jawab pendek" already covered above; these three phrasings were the
    # only gaps found during Phase 0 audit).
    "tl;dr", "tldr", "jawab pendek", "short and sweet",
)

_EXPLICIT_DETAILED_PHRASES = (
    "jelaskan detail", "jelasin detail", "jelaskan secara detail",
    "jelasin secara detail", "jelaskan secara mendalam",
    "jelasin secara mendalam", "jelasin lengkap", "jelaskan lengkap",
    "jelaskan dari awal", "jelasin dari awal", "step by step",
    "step-by-step", "satu-satu", "satu per satu", "satu-persatu",
    "bedah semuanya", "bedah semua", "jelaskan sampai aku ngerti",
    "jelasin sampai aku ngerti", "jelaskan sampai saya mengerti",
    "in detail", "in-depth", "in depth", "explain in detail",
    "explain thoroughly", "walk me through", "break it down completely",
    # Voice Output Optimization sprint - additive. These are the specific
    # "user explicitly wants full detail" phrasings the sprint brief names
    # (jelaskan semuanya / secara lengkap / semua penyebabnya / jangan
    # disingkat / rinci / full explanation) that Phase 0's audit of the
    # existing phrase lists found were NOT yet covered. Added here (the
    # ONE existing explicit-instruction authority) rather than building a
    # second, competing intent classifier inside the new voice optimizer -
    # per the brief's own "use the existing response-depth policy where
    # possible" instruction. Purely additive: every phrase already covered
    # above is untouched, and these are specific multi-word phrases chosen
    # to avoid false-positive substring collisions (e.g. "secara rinci"
    # rather than a bare "rinci", which could match inside an unrelated
    # word like "terinci").
    "jelaskan semuanya", "jelasin semuanya", "secara lengkap",
    "semua penyebabnya", "sebutkan semua", "jangan disingkat",
    "jangan diringkas", "secara rinci", "dengan rinci", "full explanation",
    "explain fully", "explain in full",
)


def _matches_any_phrase(normalized_text: str, phrases) -> Optional[str]:
    for phrase in phrases:
        if phrase in normalized_text:
            return phrase
    return None


# ─────────────────────────────────────────────
#  2. Conversational-context modifiers (confusion / repeat-request /
#  continuation) - applied on top of the heuristic score, never able to
#  fabricate an "explicit" result.
# ─────────────────────────────────────────────

_CONFUSION_PHRASES = (
    "aku masih bingung", "saya masih bingung", "masih bingung",
    "kurang paham", "kurang ngerti", "belum ngerti", "belum paham",
    "gak ngerti", "ga ngerti", "tidak mengerti", "i'm still confused",
    "still confused", "i don't get it", "i dont get it",
)

_REPEAT_EXPLAIN_PHRASES = (
    "jelasin lagi", "jelaskan lagi", "ulangi lagi", "ulangi penjelasannya",
    "tolong jelasin lagi", "tolong jelaskan lagi", "explain again",
    "can you explain again", "say that again",
)

#: A short, connective-word-led message with no strong task-type signal
#: of its own is the shape of a genuine follow-up ("kalau yang bagian
#: regulator gimana?") - never a proxy for "the user wrote a long
#: sentence" (that is explicitly NOT a signal this module uses).
_CONTINUATION_LEAD_WORDS = (
    "kalau", "kalo", "terus", "trus", "terus kalau", "yang bagian",
    "gimana kalau", "gimana kalo", "terus gimana", "trus gimana",
    "lah kalau", "terus yang", "kalau yang",
)
_CONTINUATION_MAX_WORDS = 12


# ─────────────────────────────────────────────
#  3. Request/task type - deterministic keyword buckets. Each bucket's
#  BASE score reflects the sprint spec's own qualitative guidance
#  (simple factual/yes-no -> low, how-to/troubleshooting/comparison ->
#  moderate, tutorial -> moderate-high, architecture/deep-analysis ->
#  high). Checked in a fixed priority order so overlapping keywords
#  (e.g. "kenapa" appearing in both troubleshooting and deep-analysis
#  phrasing) resolve deterministically, never ambiguously.
# ─────────────────────────────────────────────

_TASK_BUCKETS = (
    # (task_type, base_score, keyword tuple) - order matters, first match wins.
    ("architecture_or_deep_analysis", 75, (
        "arsitektur", "architecture", "cara kerja", "bagaimana cara kerja",
        "gimana cara kerja", "end to end", "end-to-end", "menyeluruh",
        "secara keseluruhan", "root cause", "akar masalah", "analisis mendalam",
        "analisa mendalam", "deep dive", "dari nol sampai", "dari awal sampai",
    )),
    ("tutorial", 55, (
        "tutorial", "panduan lengkap", "panduan", "cara setup", "cara install",
        "cara membuat dari awal", "buatkan tutorial", "guide me through",
    )),
    ("troubleshooting", 42, (
        "kenapa", "kok tidak", "kok gak", "kok ga", "tidak bisa", "gabisa",
        "ga bisa", "gak bisa", "error", "gagal", "masalah", "bug",
        "not working", "isn't working", "doesn't work", "why isn't",
        "why doesn't", "kenapa bisa", "kenapa gak bisa",
    )),
    ("comparison", 40, (
        "bedanya", "beda antara", "perbedaan", "dibanding", "dibandingkan",
        " vs ", " vs.", "versus", "mana yang lebih", "lebih bagus mana",
        "lebih baik mana",
    )),
    ("how_to_instruction", 38, (
        "cara ", "bagaimana cara", "gimana cara", "how to", "how do i",
        "langkah untuk", "langkah-langkah",
    )),
    ("definition_or_yes_no", 15, (
        "apa itu", "apa fungsi", "apa arti", "apa maksud", "apakah",
        "apa saja", "definisi", "what is", "is it true", "does it",
        "can it", "is there",
    )),
)

#: Applied ONLY when a bucket above already matched, as a bounded
#: "this specific request wants full breadth" nudge - never stacked
#: with the architecture bucket itself (that bucket's own base score
#: already reflects "high"), so this cannot silently double-count.
_COMPREHENSIVE_SPAN_PHRASES = (
    "dari awal", "dari nol", "sampai akhir", "secara lengkap", "lengkap banget",
    "menyeluruh", "end to end", "end-to-end",
)


def _classify_task_type(normalized_text: str):
    for task_type, base_score, keywords in _TASK_BUCKETS:
        for kw in keywords:
            if kw in normalized_text:
                return task_type, base_score
    return None, 32  # ordinary question, no explicit-instruction, no bucket match -> lean NORMAL


# ─────────────────────────────────────────────
#  4. Question complexity - bounded, structural signals only. NEVER
#  character/word count as the primary driver (the sprint spec's own
#  explicit "do not over-infer" rule).
# ─────────────────────────────────────────────

def _count_question_marks(text: str) -> int:
    return text.count("?")


_MULTI_CONCEPT_CONNECTORS = (" dan ", " serta ", " juga ", " and ", " plus ")


def compute_response_policy(
    text: str,
    *,
    previous_score: Optional[int] = None,
    previous_depth: Optional[str] = None,
    adaptive_modifier: Optional[int] = None,
) -> ResponsePolicy:
    """The one entry point this module exposes. Pure function of
    `(text, previous_score, previous_depth)` - deterministic, no I/O, no
    randomness, no clock, no LLM/API call. `previous_score`/
    `previous_depth` are OPTIONAL, caller-supplied hints about the
    immediately preceding turn's resolved depth for this SAME
    conversation (never read from disk by this module, never computed
    by re-deriving anything from long-term memory)."""
    raw_text = text or ""
    normalized = raw_text.strip().lower()

    if not normalized:
        return ResponsePolicy(depth=DEPTH_NORMAL, score=32, reasons=["empty_or_missing_text"], explicit=False)

    # --- 1. Explicit instruction - highest precedence, always wins. ---
    short_match = _matches_any_phrase(normalized, _EXPLICIT_SHORT_PHRASES)
    detailed_match = _matches_any_phrase(normalized, _EXPLICIT_DETAILED_PHRASES)
    if short_match:
        return ResponsePolicy(
            depth=DEPTH_SHORT, score=10, reasons=["explicit_short_instruction"], explicit=True,
        )
    if detailed_match:
        return ResponsePolicy(
            depth=DEPTH_DETAILED, score=90, reasons=["explicit_detailed_instruction"], explicit=True,
        )

    reasons: List[str] = []

    # --- 3. Request/task type (base score). ---
    task_type, score = _classify_task_type(normalized)
    if task_type is not None:
        reasons.append(f"task_type:{task_type}")
    else:
        reasons.append("ordinary_question_no_task_signal")

    if task_type is not None and task_type != "architecture_or_deep_analysis":
        if _matches_any_phrase(normalized, _COMPREHENSIVE_SPAN_PHRASES):
            score += 15
            reasons.append("comprehensive_span_request")

    # --- 4. Question complexity. ---
    if _count_question_marks(normalized) >= 2:
        score += 12
        reasons.append("multiple_questions")

    concept_connector_hits = sum(1 for c in _MULTI_CONCEPT_CONNECTORS if c in normalized)
    # A connector alone (e.g. "roti dan selai") means nothing - only
    # count it as "multiple concepts" alongside genuine task-type
    # richness (a second, DIFFERENT bucket's keyword also present), so
    # an ordinary sentence with "dan" in it never inflates the score.
    if concept_connector_hits and task_type is not None:
        other_bucket_hit = any(
            other_type != task_type and any(kw in normalized for kw in kws)
            for other_type, _base, kws in _TASK_BUCKETS
        )
        if other_bucket_hit:
            score += 10
            reasons.append("multiple_concepts")

    # --- 2. Conversational context modifiers. ---
    if _matches_any_phrase(normalized, _CONFUSION_PHRASES):
        score += 25
        reasons.append("user_confusion_signal")
    if _matches_any_phrase(normalized, _REPEAT_EXPLAIN_PHRASES):
        score += 20
        reasons.append("clarification_repeat_request")

    if previous_score is not None:
        word_count = len(normalized.split())
        looks_like_followup = (
            word_count <= _CONTINUATION_MAX_WORDS
            and any(normalized.startswith(w) or f" {w} " in f" {normalized} " for w in _CONTINUATION_LEAD_WORDS)
        )
        if looks_like_followup and task_type is None:
            nudged = max(score, previous_score - 10)
            if nudged != score:
                score = nudged
                reasons.append("conversational_continuation")

    # --- 5. Adaptive Response Depth Learning sprint - a BOUNDED modifier,
    # never a replacement for anything above. Applied LAST, after every
    # other heuristic signal, matching the sprint's own explicit priority
    # order (explicit instruction > safety > existing policy > adaptive
    # preference > default). Never reached by the explicit-instruction
    # branches above (they already `return` before this point), so an
    # explicit "jawab singkat"/"jelaskan detail" instruction always wins
    # regardless of `adaptive_modifier`'s value - this is a structural
    # guarantee, not a runtime check. `adaptive_modifier` is the CALLER's
    # own bounded, conversation-scoped preference signal (see
    # `main_runtime_demo.py`'s `PlannerBridgeModule._depth_preference` /
    # `apply_depth_feedback()` below) - this function does not compute it
    # itself and does not know anything about feedback text. Re-clamped
    # here defensively (`_DEPTH_BIAS_MIN`/`_MAX`) even though every
    # legitimate caller already bounds it via `apply_depth_feedback()`,
    # so a malformed/out-of-range caller value can never push the score
    # further than this module's own designed bounds allow.
    if adaptive_modifier:
        bounded_modifier = max(_DEPTH_BIAS_MIN, min(_DEPTH_BIAS_MAX, adaptive_modifier))
        if bounded_modifier:
            score += bounded_modifier
            reasons.append(f"adaptive_depth_preference:{'+' if bounded_modifier >= 0 else ''}{bounded_modifier}")

    score = _clamp_score(score)
    depth = _score_to_depth(score)
    return ResponsePolicy(depth=depth, score=score, reasons=reasons, explicit=False, task_type=task_type)


# ─────────────────────────────────────────────
#  Adaptive Response Depth Learning sprint - a bounded, deterministic,
#  conversation-scoped MODIFIER layered on top of everything above, never
#  a second classifier and never a replacement for it. See
#  docs/change_impact/adaptive_response_depth.md for the full design
#  rationale, the priority-order proof, and the "why not persisted"
#  reasoning.
#
#  Two separate, small, pure pieces:
#
#    1. `detect_depth_feedback(text)` - reads a user's turn and decides
#       whether it is explicit feedback about how LONG/DETAILED the
#       PREVIOUS reply was ("kepanjangan", "kurang jelas", "pas") or not
#       ("itu salah", an ordinary follow-up question, silence). Never
#       confuses CONTENT feedback (already handled entirely separately by
#       the memory module's own `classify_context_outcome()`/
#       `detect_*_memory_feedback()` functions - not imported or touched
#       here) with DEPTH feedback - these are
#       deliberately two disjoint phrase sets checked by two entirely
#       separate functions in two different modules, per the sprint's own
#       explicit "'itu salah' != request lebih pendek/panjang" rule.
#
#    2. `apply_depth_feedback(preference, feedback)` - a pure, bounded
#       accumulator: takes the conversation's current `DepthPreference`
#       (or `None` for a conversation with no prior feedback) plus ONE
#       new feedback label, returns a NEW `DepthPreference` with an
#       updated, clamped `bias`. Never mutates its input. The caller
#       (`main_runtime_demo.py`'s `PlannerBridgeModule`) owns WHEN this
#       runs (once per turn, on that turn's own text) and WHERE the
#       resulting state lives (a small, bounded, conversation-scoped,
#       never-persisted dict - see that module for the full wiring).
# ─────────────────────────────────────────────

DEPTH_FEEDBACK_PREFER_SHORT = "prefer_short"
DEPTH_FEEDBACK_PREFER_DETAILED = "prefer_detailed"
DEPTH_FEEDBACK_NEUTRAL = "neutral"

#: "Too long" feedback about the PREVIOUS reply - a request for the
#: PREVIOUS depth to be smaller. Deliberately narrow, substring-matched
#: (not whole-message-anchored like the memory module's own memory-content
#: detectors) since the sprint's own worked examples show this
#: combined with other clauses in the same message ("kepanjangan, singkat
#: aja") - a "search anywhere in the message" check, not "the ENTIRE
#: message must be exactly this phrase".
_PREFER_SHORT_FEEDBACK_RE = re.compile(
    r'kepanjangan|terlalu panjang|'
    r'too long|way too long|that\'?s too long',
    re.IGNORECASE,
)

#: "Too short / not clear enough" feedback about the PREVIOUS reply - a
#: request for the PREVIOUS depth to be bigger. Deliberately requires a
#: length/clarity QUALIFIER after "kurang" ("kurang jelas"/"kurang
#: detail"/"kurang lengkap"/"kurang rinci") - a bare "informasinya
#: kurang" is a CONTENT-completeness complaint, not necessarily a depth
#: request (the sprint brief's own explicit example of what must NOT be
#: auto-classified as depth feedback), so it is deliberately left
#: unmatched here.
_PREFER_DETAILED_FEEDBACK_RE = re.compile(
    r'terlalu singkat|kurang jelas|kurang detail|kurang lengkap|kurang rinci|'
    r'too short|not (?:clear|detailed) enough|needs? more detail',
    re.IGNORECASE,
)

#: "The previous depth was right" - an explicit, narrow, whole-message
#: confirmation (same anchoring discipline as the memory module's own
#: `_POSITIVE_MEMORY_FEEDBACK_RE`) that the length was satisfactory.
#: Deliberately NOT inferred from "the user didn't complain" or "the user
#: kept talking" (hard constraint: silence/continuation is never
#: feedback) - only this specific, narrow phrase shape counts.
_NEUTRAL_DEPTH_FEEDBACK_RE = re.compile(
    r'^(?:panjangnya\s+)?pas\.?$|^udah\s+pas\.?$|^sudah\s+pas\.?$|'
    r'^segini\s+(?:oke|pas|cukup)\.?$|'
    r"^that'?s\s+(?:just\s+)?(?:right|perfect)\.?$",
    re.IGNORECASE,
)


def detect_depth_feedback(user_text: Optional[str]) -> Optional[str]:
    """Returns `DEPTH_FEEDBACK_PREFER_SHORT` / `_PREFER_DETAILED` /
    `_NEUTRAL`, or `None` if `user_text` is not a recognized depth-
    feedback shape at all - including `None`/empty/whitespace-only text
    (silence is NEVER feedback, hard constraint). Pure, deterministic,
    no LLM call, no I/O - a plain regex classification, same discipline
    as every other detector in this codebase.

    Checked in this order: PREFER_SHORT and PREFER_DETAILED first (their
    phrase sets are disjoint by construction - a message could not
    realistically match both), NEUTRAL last (the narrowest, most
    specific match) - a message matching neither directional phrase nor
    the exact neutral shape returns `None`, i.e. "not depth feedback",
    which is the correct, safe default for ordinary conversation,
    content-only complaints ("itu salah"), and explicit depth REQUESTS
    for the CURRENT turn (e.g. "jelaskan secara lengkap tentang X" is
    already handled entirely separately by this module's own
    `_EXPLICIT_DETAILED_PHRASES` - it is a request for THIS reply, not
    feedback about a PREVIOUS one, and is deliberately not matched here
    even though the words overlap conceptually)."""
    text = (user_text or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if _PREFER_SHORT_FEEDBACK_RE.search(lowered):
        return DEPTH_FEEDBACK_PREFER_SHORT
    if _PREFER_DETAILED_FEEDBACK_RE.search(lowered):
        return DEPTH_FEEDBACK_PREFER_DETAILED
    if _NEUTRAL_DEPTH_FEEDBACK_RE.match(text):
        return DEPTH_FEEDBACK_NEUTRAL
    return None


#: Bounded nudge magnitude per single feedback event - deliberately
#: "small" (per the sprint's own D/E test requirements: one feedback
#: event alone should visibly move the score without single-handedly
#: overriding a solidly-scored heuristic result several buckets away).
_DEPTH_BIAS_STEP = 10

#: Hard bounds on the accumulated bias - well inside the full 0-100 score
#: range, so this modifier can only ever nudge a BORDERLINE heuristic
#: decision, never override one that's already solidly within a bucket
#: (e.g. a base score of 90 minus the maximum possible -25 bias is still
#: 65, still DETAILED).
_DEPTH_BIAS_MIN = -25
_DEPTH_BIAS_MAX = 25

#: Persistent Adaptive Response Depth Preference sprint - PUBLIC aliases
#: of the two bounds immediately above. This module still performs NO
#: I/O of any kind (unchanged - see
#: `tests/test_response_policy.py::test_response_policy_module_imports_no_memory_or_persistence_modules`,
#: still enforced, still passing), but the bound VALUES themselves are a
#: shared contract a separate, I/O-capable module
#: (`luno/response_depth_preference.py`) needs to clamp a PERSISTED bias
#: to the exact same range - exported here so that module imports the
#: single source of truth instead of hardcoding a duplicate literal that
#: could silently drift out of sync.
DEPTH_BIAS_MIN = _DEPTH_BIAS_MIN
DEPTH_BIAS_MAX = _DEPTH_BIAS_MAX

#: Applied to the EXISTING accumulated bias before folding in a NEW
#: feedback event ("confidence-aware": recent feedback counts for more
#: than older feedback, and repeated OPPOSING feedback converges back
#: toward neutral rather than fighting to the opposite extreme - see
#: `apply_depth_feedback()`'s own docstring for a worked example). Chosen
#: over a wall-clock-based decay because nothing else in this codebase's
#: existing bounded/reset state (`_response_depth_context`,
#: `_last_turn_trace`, `_session_feedback_target` - all in
#: `main_runtime_demo.py`) is time-based; every one of them is a plain
#: count-bounded, event-driven dict. An event-based decay is fully
#: deterministic and trivially testable (no wall-clock mocking needed),
#: matching that same established convention rather than inventing a new
#: one.
_DEPTH_BIAS_DECAY_RATIO = 0.5


@dataclass
class DepthPreference:
    """One conversation's bounded, adaptive depth-preference signal.
    NEVER persisted to disk (see docs/change_impact/adaptive_response_depth.md's
    "Persistence" section) - this mirrors `main_runtime_demo.py`'s own
    `_response_depth_context`'s explicit, deliberate "never persisted,
    conversation-scoped, reset at conversation boundary" precedent, not a
    new convention.

    `bias` - a signed integer in [`_DEPTH_BIAS_MIN`, `_DEPTH_BIAS_MAX`].
    Negative leans SHORT, positive leans DETAILED, zero is neutral/no
    signal yet.

    `feedback_count` - a plain observability counter (how many depth-
    feedback events this conversation has ever produced). Never itself
    used to gate whether `bias` applies - `compute_response_policy()`
    only ever reads `bias`.

    `last_updated_at` - an ISO-8601 string, purely for debugging/
    observability. NOT read by the decay math (`_DEPTH_BIAS_DECAY_RATIO`'s
    own docstring explains why this module uses event-based, not
    wall-clock-based, decay)."""

    bias: int = 0
    feedback_count: int = 0
    last_updated_at: Optional[str] = None


def apply_depth_feedback(
    preference: Optional[DepthPreference], feedback: str, *, now: Optional[datetime] = None,
) -> DepthPreference:
    """Pure function - returns a NEW `DepthPreference`, never mutates
    `preference` (matching this module's own `compute_response_policy()`
    "no I/O, no mutation of caller state" discipline). `feedback` must be
    one of `DEPTH_FEEDBACK_PREFER_SHORT`/`_PREFER_DETAILED`/`_NEUTRAL`
    (`detect_depth_feedback()`'s own return value) - this function does
    not itself re-detect anything from raw text, and does not accept
    `None` (the caller is expected to only call this when
    `detect_depth_feedback()` returned a real label - the same
    "detect, then act on a real label" discipline the memory module's own
    content-feedback evidence recorder already follows, applied here to
    depth instead of content).

    Worked example (the exact sprint brief scenario - "kepanjangan" once,
    then "kurang jelas" once, on an otherwise-fresh conversation):
        start:                    bias=0
        after "kepanjangan":      bias = round(0*0.5)  - 10 = -10
        after "kurang jelas":     bias = round(-10*0.5) + 10 =  +5
    One opposing feedback event pulls the bias back toward (and, in this
    case, slightly past) neutral rather than the two events fighting to
    the opposite -20/+20 extremes - satisfies the sprint's own "opposing
    feedback tidak menyebabkan oscillation ekstrem" requirement by
    construction, not by a special-cased oscillation guard."""
    current_bias = preference.bias if preference is not None else 0
    count = preference.feedback_count if preference is not None else 0
    decayed = current_bias * _DEPTH_BIAS_DECAY_RATIO
    if feedback == DEPTH_FEEDBACK_PREFER_SHORT:
        new_bias = decayed - _DEPTH_BIAS_STEP
    elif feedback == DEPTH_FEEDBACK_PREFER_DETAILED:
        new_bias = decayed + _DEPTH_BIAS_STEP
    else:  # DEPTH_FEEDBACK_NEUTRAL (or any unrecognized label) - decay only
        new_bias = decayed
    new_bias = max(_DEPTH_BIAS_MIN, min(_DEPTH_BIAS_MAX, round(new_bias)))
    now = now or datetime.now()
    return DepthPreference(
        bias=int(new_bias), feedback_count=count + 1, last_updated_at=now.isoformat(timespec="seconds"),
    )


# ─────────────────────────────────────────────
#  Prompt integration - a small instruction layer, NOT three giant
#  separate prompts. Kept short and generic on purpose so it never
#  competes with or overrides Luno's existing persona/system prompt
#  (see main_runtime_demo.py's `build_persona_prompt()` - this is one
#  more `notes.append(...)`-style block, same convention as every other
#  note in that method).
# ─────────────────────────────────────────────

_DEPTH_INSTRUCTIONS = {
    DEPTH_SHORT: (
        "Response depth: SHORT. Keep the answer concise and answer the user's "
        "request directly. Avoid unnecessary background explanation."
    ),
    DEPTH_NORMAL: (
        "Response depth: NORMAL. Give a useful explanation with relevant context. "
        "Include practical details when they help answer the request."
    ),
    DEPTH_DETAILED: (
        "Response depth: DETAILED. Give a comprehensive explanation. Break complex "
        "material into logical sections and steps when useful. Do not add "
        "irrelevant information merely to increase length."
    ),
}


def build_depth_instruction(policy: ResponsePolicy) -> str:
    """Renders the ONE small instruction block for `policy.depth` -
    never a full prompt rewrite, never referencing internal
    score/reasons (those stay debug-only, per the sprint's own
    explainability-without-user-exposure rule)."""
    return _DEPTH_INSTRUCTIONS.get(policy.depth, _DEPTH_INSTRUCTIONS[DEPTH_NORMAL])
