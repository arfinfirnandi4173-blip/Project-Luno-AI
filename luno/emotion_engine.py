"""
emotion_engine.py
==================

LUNO Emotion Engine sprint - a small, additive, conversational USER-
emotion estimator that lets Luno's REPLY adapt its tone (warmth, humor,
verbosity, ...) to how the user seems to be feeling right now, without
ever touching verified facts, tool results, safety rules, or personality
identity.

WHY THIS IS A NEW MODULE, NOT AN EXTENSION OF `luno/behavior_tree/emotion.py`
------------------------------------------------------------------------------
Per the sprint's own mandatory pre-flight audit, `luno/behavior_tree/
emotion.py`'s `EmotionEstimator` already exists and was inspected first.
It is a DIFFERENT thing, serving a DIFFERENT consumer:

- It estimates LUNO'S OWN internal/expressive state (`Blackboard.emotion`)
  from a blend of signals (vision-detected user emotion, Luno's own
  consecutive tool errors, whether a conversation is ongoing, time of
  day) - not a text-based read of what the user just said.
- Its only real consumers are `luno/behavior_tree/planner.py` (bundles it
  into a context dict that IS published on the "user_utterance" event but
  is - confirmed by direct inspection of `PlannerBridgeModule.
  _handle_utterance()` in `main_runtime_demo.py` - never read from
  there), the dashboard's `current_emotion` introspection provider, and
  (currently a no-op, `set_avatar_emotion=lambda emotion: None`) a future
  avatar hookup. It was never wired into the LLM prompt path.
- Its own module docstring is explicit about this: "Voice tone
  specifically isn't analyzed here at all yet - `EmotionSignals.
  conversation_sentiment` is a slot for a future upstream signal ... to
  plug into" - i.e. it was already designed with a seam for exactly this
  module to eventually feed it, not to duplicate it.

This module fills that seam from the other side: it reads the user's own
recent text, produces a `UserEmotionState` (with an honest confidence
score), and exposes a `ResponsePolicy` + a bounded prompt block that
`main_runtime_demo.py`'s `PlannerBridgeModule` injects into the LLM
system prompt. It does not touch `Blackboard`, does not touch the
Behavior Tree, and `luno/behavior_tree/emotion.py` is left completely
unmodified - see `ARCHITECTURE_GUARD.md`'s Emotion Engine section for the
full boundary write-up.

Architectural goal (kept as 3 distinct concepts, never conflated):

    USER EMOTION            - what the user's text suggests they feel
    LUNO INTERNAL STATE      - out of scope here; see behavior_tree/emotion.py
    RESPONSE BEHAVIOR        - how Luno's reply should lean, given the above

Conceptual flow implemented by this module:

    user text
        -> EmotionEstimator.estimate_from_text()   (pure function)
        -> UserEmotionState                         (emotion + confidence + ...)
        -> EmotionStateTracker.observe()             (decay / replacement)
        -> derive_response_policy()                  (small deterministic table)
        -> build_emotional_context_prompt()           (bounded prompt string)

Everything here is a pure function or a small, explicitly-scoped stateful
tracker - no I/O, no network call, no second LLM round trip (performance:
regex/keyword matching only, sub-millisecond per call - see
ARCHITECTURE_GUARD.md's Emotion Engine section for measured numbers).

HONEST LIMITATION: this is conversational, rule-based inference over
punctuation/keywords, in the same spirit as `luno/expressions.py`'s
`guess_expression()` and `luno/vision_memory/utils.py`'s
`parse_description_heuristic()` - NOT a validated psychological or
medical instrument, and it never claims to be. Confidence is a rough,
self-reported heuristic score, not a calibrated probability.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Pattern, Tuple

from . import config


# ─────────────────────────────────────────────
#  USER EMOTION MODEL
# ─────────────────────────────────────────────


class UserEmotion(str, Enum):
    """Deliberately NOT treated as an immutable/exhaustive taxonomy (see
    the sprint brief's own "do not treat this list as immutable") - it is
    a plain string Enum specifically so a future category can be added
    without breaking anything that already does `state.emotion.value`."""

    NEUTRAL = "neutral"
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    FRUSTRATED = "frustrated"
    ANXIOUS = "anxious"
    EXCITED = "excited"
    TIRED = "tired"
    CONFUSED = "confused"
    CURIOUS = "curious"
    PLAYFUL = "playful"
    STRESSED = "stressed"
    DISAPPOINTED = "disappointed"
    SURPRISED = "surprised"
    #: No confident signal at all - the honest default. Distinct from
    #: NEUTRAL (which would mean "text actively reads as calm/plain"),
    #: matching section 6/7's "never force a confident classification
    #: when the evidence is weak" requirement.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UserEmotionState:
    """Structured representation of the user's current emotional context.
    Frozen/immutable - a new turn always produces a new instance rather
    than mutating one in place, so nothing downstream can accidentally
    hold a stale reference across turns."""

    emotion: UserEmotion = UserEmotion.UNKNOWN
    intensity: float = 0.0     # 0.0-1.0, how strongly expressed
    confidence: float = 0.0    # 0.0-1.0, how sure the estimator is
    energy: float = 0.5        # 0.0 (low-energy) - 1.0 (high-energy)
    valence: float = 0.0       # -1.0 (negative) - 1.0 (positive)
    timestamp: float = field(default_factory=time.time)
    source: str = "text_heuristic"

    def is_confident(self, threshold: Optional[float] = None) -> bool:
        """`threshold` defaults to `config.EMOTION_LOW_CONFIDENCE_THRESHOLD`
        - a separate parameter only exists so tests can probe the
        boundary without monkeypatching config."""
        t = config.EMOTION_LOW_CONFIDENCE_THRESHOLD if threshold is None else threshold
        return self.emotion != UserEmotion.UNKNOWN and self.confidence >= t

    def to_dict(self) -> Dict[str, object]:
        """Plain-dict projection - the "structured output a future TTS/
        avatar layer can consume" the sprint brief asks for (section 17/
        18), without this module importing or depending on either."""
        return {
            "emotion": self.emotion.value,
            "intensity": self.intensity,
            "confidence": self.confidence,
            "energy": self.energy,
            "valence": self.valence,
            "timestamp": self.timestamp,
            "source": self.source,
        }


_UNKNOWN_STATE = UserEmotionState()  # confidence=0.0, emotion=UNKNOWN - the safe default


# ─────────────────────────────────────────────
#  TEXT -> EMOTION ESTIMATION (rule-based, bilingual ID/EN)
# ─────────────────────────────────────────────
#
# Same "ordered/weighted pattern list" heuristic style as
# `luno/expressions.py:guess_expression()` and
# `luno/vision_memory/utils.py:parse_description_heuristic()` - every
# category is a list of (compiled regex, weight) pairs; a matched pattern
# adds its weight to that category's score. The highest-scoring category
# wins; if the runner-up is close behind, confidence is deliberately
# discounted (section 6: "must gracefully handle ... mixed emotion").

def _p(pattern: str) -> Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


_EMOTION_PATTERNS: Dict[UserEmotion, List[Tuple[Pattern[str], float]]] = {
    UserEmotion.EXCITED: [
        (_p(r"\b(yes+s*|yay|woohoo|akhirnya|finally)\b"), 1.0),
        (_p(r"\b(excited|can'?t wait|nggak sabar|ga sabar|udah gak sabar)\b"), 1.5),
        (_p(r"!{2,}"), 0.8),
        (_p(r"\b(berhasil|worked|it works|works now)\b"), 0.8),
    ],
    UserEmotion.HAPPY: [
        (_p(r"\b(senang|seneng|happy|glad|makasih banget|thank you so much|thanks a lot)\b"), 1.2),
        (_p(r"\b(mantap|keren|awesome|nice|great|love it|suka banget)\b"), 1.0),
        (_p(r"(:\)|:d|😊|😄|😁|🎉)"), 0.8),
    ],
    UserEmotion.PLAYFUL: [
        (_p(r"\b(wkwk+|haha+|lol|lmao|xixi+|awkwkwk+)\b"), 1.2),
        (_p(r"(😂|🤣)"), 1.0),
        (_p(r"\b(iseng|becanda|just kidding|jk\b)\b"), 0.8),
    ],
    UserEmotion.SURPRISED: [
        (_p(r"\b(wow|whoa|omg|astaga|gila(?!\s*(sih|banget)?\s*(kesel|marah))|serius(?:an)?[!?]|nggak nyangka|gak nyangka|no way)\b"), 1.2),
        (_p(r"\?!|!\?"), 0.8),
    ],
    UserEmotion.CURIOUS: [
        # "kenapa"/"why does"/a bare trailing "?" alone are deliberately
        # WEAK signals here - those also cover ordinary technical
        # troubleshooting questions ("kenapa Docker container-ku restart
        # terus?"), which must NOT read as an emotional state at all
        # (Emotion Engine sprint section 10's own worked example). Only
        # an EXPLICIT curiosity word ("penasaran", "curious", "i wonder")
        # is weighted strongly enough to cross the confidence threshold
        # on its own.
        (_p(r"\b(kenapa|kok bisa|gimana caranya|how does|why does)\b"), 0.5),
        (_p(r"\b(i wonder|penasaran|curious)\b"), 1.3),
        (_p(r"\?$"), 0.2),
    ],
    UserEmotion.CONFUSED: [
        (_p(r"\b(bingung|nggak ngerti|ga ngerti|confused|i don'?t get it|i don'?t understand|maksudnya apa)\b"), 1.3),
        (_p(r"\b(huh\??|wait what|lah\?)\b"), 0.8),
    ],
    UserEmotion.TIRED: [
        (_p(r"\b(capek|cape|lelah|ngantuk|exhausted|so tired|tired|sleepy|burnout|burned out)\b"), 1.4),
        (_p(r"\b(capek banget|cape banget|so exhausted)\b"), 1.8),
    ],
    UserEmotion.STRESSED: [
        (_p(r"\b(stress|stres|banyak banget kerjaan|overwhelmed|kewalahan|deadline)\b"), 1.3),
        (_p(r"\b(banyak banget|too much|so much to do)\b"), 0.7),
    ],
    UserEmotion.ANXIOUS: [
        (_p(r"\b(khawatir|cemas|takut|nervous|anxious|worried|deg-?degan|gugup)\b"), 1.4),
    ],
    UserEmotion.FRUSTRATED: [
        (_p(r"\b(kesel|sebel|frustrasi|frustrating|annoying|ugh+|argh+|ribet banget|ga jelas banget)\b"), 1.4),
        (_p(r"\b(masih (aja|error|gagal))\b"), 0.8),
    ],
    UserEmotion.ANGRY: [
        (_p(r"\b(marah|kesal banget|geram|pissed|furious|damn it|goddamn)\b"), 1.6),
    ],
    UserEmotion.DISAPPOINTED: [
        (_p(r"\b(kecewa|disappointed|nyesel|nyesal|padahal udah)\b"), 1.4),
        (_p(r"\b(yah\.{0,3}\s*$|aw man|aww)\b"), 0.6),
    ],
    UserEmotion.SAD: [
        (_p(r"\b(sedih|sad|nangis|crying|down banget|feeling down|patah hati)\b"), 1.5),
        (_p(r"(:\(|😢|😭|💔)"), 1.0),
    ],
}

#: Per-emotion baseline energy/valence (section 6's "extensible model" -
#: a plain lookup table, trivially extendable if a new category is added
#: above without touching the scoring logic itself).
_EMOTION_BASELINES: Dict[UserEmotion, Tuple[float, float]] = {
    # emotion: (energy, valence)
    UserEmotion.NEUTRAL: (0.5, 0.0),
    UserEmotion.HAPPY: (0.7, 0.7),
    UserEmotion.EXCITED: (0.9, 0.8),
    UserEmotion.PLAYFUL: (0.8, 0.6),
    UserEmotion.SURPRISED: (0.8, 0.1),
    UserEmotion.CURIOUS: (0.6, 0.3),
    UserEmotion.CONFUSED: (0.4, -0.1),
    UserEmotion.TIRED: (0.15, -0.2),
    UserEmotion.STRESSED: (0.6, -0.5),
    UserEmotion.ANXIOUS: (0.6, -0.5),
    UserEmotion.FRUSTRATED: (0.6, -0.6),
    UserEmotion.ANGRY: (0.75, -0.8),
    UserEmotion.DISAPPOINTED: (0.3, -0.5),
    UserEmotion.SAD: (0.25, -0.7),
    UserEmotion.UNKNOWN: (0.5, 0.0),
}

#: Score below which nothing is considered a match at all - keeps a
#: single weak, generic pattern from ever producing a "confident" result.
_MIN_SCORE_TO_MATCH = 0.6
#: If the runner-up category's score is within this fraction of the
#: winner's, treat the read as ambiguous/mixed and discount confidence
#: hard (section 6: "must gracefully handle ... mixed emotion"). Only
#: ever compared when the runner-up actually scored > 0 (see the
#: `mixed_signal` check below), so this can never affect a text that
#: cleanly matched only one category.
_MIXED_SIGNAL_MARGIN_RATIO = 0.7


class EmotionEstimator:
    """Stateless - mirrors `luno.behavior_tree.emotion.EmotionEstimator`'s
    own "pure function of its input, trivially unit-testable" shape, on
    purpose, even though the two classes estimate different things (see
    module docstring)."""

    @staticmethod
    def estimate_from_text(text: Optional[str]) -> UserEmotionState:
        """Never raises - a `None`/empty/unparseable `text` always yields
        the safe UNKNOWN/0-confidence state rather than an exception,
        per the sprint's "Emotion Engine must be non-critical" rule.
        Also guards against a caller accidentally passing a non-string
        (e.g. `None` is expected/typed for, but an int/list should
        degrade the same way, not raise `AttributeError` on `.strip()`)."""
        if not isinstance(text, str) or not text.strip():
            return _UNKNOWN_STATE

        try:
            scores: Dict[UserEmotion, float] = {}
            for emotion, patterns in _EMOTION_PATTERNS.items():
                total = 0.0
                for pattern, weight in patterns:
                    if pattern.search(text):
                        total += weight
                if total > 0:
                    scores[emotion] = total

            if not scores:
                return _UNKNOWN_STATE

            ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            top_emotion, top_score = ranked[0]
            if top_score < _MIN_SCORE_TO_MATCH:
                return _UNKNOWN_STATE

            runner_up_score = ranked[1][1] if len(ranked) > 1 else 0.0
            mixed_signal = runner_up_score >= top_score * _MIXED_SIGNAL_MARGIN_RATIO and runner_up_score > 0

            # Confidence: rises with how strongly the winning category
            # matched, capped at 1.0, then discounted hard on a mixed
            # signal - never treat a close, ambiguous call as certain.
            confidence = min(1.0, 0.35 + 0.18 * top_score)
            if mixed_signal:
                confidence *= 0.55

            intensity = min(1.0, 0.25 + 0.15 * top_score)
            # Emphatic punctuation/repetition is a genuine intensity
            # signal independent of which category matched.
            if re.search(r"[!?]{2,}", text):
                intensity = min(1.0, intensity + 0.15)
            if re.search(r"\b(\w)\1{2,}\b", text):  # e.g. "capeeek", "yesss"
                intensity = min(1.0, intensity + 0.1)

            base_energy, base_valence = _EMOTION_BASELINES.get(top_emotion, (0.5, 0.0))

            return UserEmotionState(
                emotion=top_emotion,
                intensity=round(intensity, 3),
                confidence=round(confidence, 3),
                energy=round(base_energy, 3),
                valence=round(base_valence, 3),
                timestamp=time.time(),
                source="text_heuristic",
            )
        except Exception:
            # Belt-and-suspenders on top of the "never raises" contract
            # above - a future edit to the pattern tables must never be
            # able to take down conversation handling (section 20).
            return _UNKNOWN_STATE


# ─────────────────────────────────────────────
#  STATE TRACKING (decay / replacement / session boundaries)
# ─────────────────────────────────────────────


class EmotionStateTracker:
    """Holds the CURRENT best guess at the user's emotional state across
    turns, applying section 11/12's decay + "current context takes
    precedence" rules. Instantiated once per `PlannerBridgeModule` (see
    that class's `__init__`) rather than as bare module-level globals -
    unlike `luno/memory.py`'s process-wide globals (an existing,
    unrelated convention this module deliberately does not copy), giving
    each tracker instance is what lets tests construct a fresh one
    without any save/restore-global-state ceremony.

    NOT keyed per-conversation - this codebase's runtime has exactly one
    active conversation/Blackboard at a time (see `ARCHITECTURE_GUARD.md`
    §2/§3), so a single tracker mirrors the same scope every other
    per-turn piece of state in `PlannerBridgeModule` already uses
    (`self.last_plan_id`, `self.decision_engine.affinity`, ...). `reset()`
    is called at the same conversation-boundary hook
    (`_on_conversation_ended`) those already use - see that method's own
    docstring for why that is the correct boundary in this codebase."""

    def __init__(self, decay_seconds: Optional[float] = None) -> None:
        self._decay_seconds = config.EMOTION_DECAY_SECONDS if decay_seconds is None else decay_seconds
        self._state: UserEmotionState = _UNKNOWN_STATE

    def reset(self) -> None:
        """Session-boundary decay concept (section 11/12) - a brand new
        conversation must never inherit emotional context from one that
        ended minutes/hours ago, same reasoning already applied to
        `_last_device_target`/`_pending_env_confirmations` at the same
        call site."""
        self._state = _UNKNOWN_STATE

    def current(self) -> UserEmotionState:
        """Applies TIME decay lazily (on read), so a state built up
        earlier this turn is never invalidated mid-turn by its own
        elapsed-time check - only calls to `current()` from a LATER turn
        can observe the decay."""
        if self._state.emotion == UserEmotion.UNKNOWN:
            return self._state
        elapsed = time.time() - self._state.timestamp
        if elapsed > self._decay_seconds:
            return _UNKNOWN_STATE
        return self._state

    def observe(self, text: Optional[str]) -> UserEmotionState:
        """Estimate from `text` (this turn's fresh evidence) and decide
        whether it REPLACES the tracked state. Never raises (delegates to
        `EmotionEstimator.estimate_from_text`, itself non-raising, and
        this method's own logic is exception-free arithmetic/comparisons
        only - defensively wrapped anyway per section 20)."""
        try:
            fresh = EmotionEstimator.estimate_from_text(text)
            if fresh.emotion != UserEmotion.UNKNOWN:
                # Section 12: "current context takes precedence over old
                # context" - any fresh, non-unknown read always replaces
                # whatever was tracked before, confident or not (a LOW-
                # confidence fresh read still describes THIS turn better
                # than a decayed-relevance old one; the low confidence
                # itself is what keeps it from strongly steering the
                # response policy below).
                self._state = fresh
                return self._state
            # No fresh signal this turn - fall through to whatever the
            # decayed-aware `current()` view says (either the still-
            # valid previous state, or UNKNOWN once it has decayed).
            return self.current()
        except Exception:
            return _UNKNOWN_STATE


# ─────────────────────────────────────────────
#  RESPONSE POLICY (emotion -> behavioral lean, never a personality override)
# ─────────────────────────────────────────────


@dataclass(frozen=True)
class ResponsePolicy:
    """Every dimension is a small signed lean, not an absolute value -
    "-1 / 0 / +1" (nudge down / no change / nudge up), deliberately far
    short of a numeric scale that would invite over-precise prompt
    language ("set warmth to 0.73") the sprint brief explicitly warns
    against ("Do NOT hardcode dozens of phrase templates... should
    influence behavior, not turn Luno into a giant collection of `if
    emotion == X: say Y`").

    `technical_depth` is included for the dataclass shape's own
    completeness/extensibility (the sprint brief lists it as a possible
    dimension) but is NEVER set away from 0 by `derive_response_policy()`
    below - see that function's own docstring and
    `test_emotion_engine.py::test_response_policy_never_reduces_technical_depth`
    for the enforced invariant. Emotion adapts warmth/tone, never
    technical correctness (section 8/10)."""

    verbosity: int = 0
    warmth: int = 0
    humor: int = 0
    energy: int = 0
    formality: int = 0
    initiative: int = 0
    empathy: int = 0
    technical_depth: int = 0

    def is_noop(self) -> bool:
        return not any((
            self.verbosity, self.warmth, self.humor, self.energy,
            self.formality, self.initiative, self.empathy, self.technical_depth,
        ))


_NOOP_POLICY = ResponsePolicy()

#: emotion -> policy deltas. Deliberately small and hand-reviewable (not
#: a learned/weighted model) - every row is a direct, explainable
#: translation of "how would a calm, attentive companion naturally lean
#: if the user seemed to feel this way", matching the worked examples in
#: the sprint brief's own section 9.
_RESPONSE_POLICY_TABLE: Dict[UserEmotion, ResponsePolicy] = {
    UserEmotion.TIRED: ResponsePolicy(verbosity=-1, warmth=1, humor=-1, empathy=1, energy=-1),
    UserEmotion.EXCITED: ResponsePolicy(energy=1, humor=1, warmth=1),
    UserEmotion.HAPPY: ResponsePolicy(warmth=1, humor=1),
    UserEmotion.PLAYFUL: ResponsePolicy(humor=1, energy=1),
    UserEmotion.CURIOUS: ResponsePolicy(initiative=1, energy=1),
    UserEmotion.SURPRISED: ResponsePolicy(energy=1),
    UserEmotion.CONFUSED: ResponsePolicy(verbosity=1, empathy=1, initiative=-1),
    UserEmotion.SAD: ResponsePolicy(warmth=1, empathy=1, humor=-1, initiative=-1),
    UserEmotion.DISAPPOINTED: ResponsePolicy(warmth=1, empathy=1, humor=-1),
    UserEmotion.ANGRY: ResponsePolicy(warmth=1, empathy=1, humor=-1, initiative=-1, formality=-1),
    UserEmotion.FRUSTRATED: ResponsePolicy(warmth=1, empathy=1, humor=-1, initiative=-1),
    UserEmotion.ANXIOUS: ResponsePolicy(warmth=1, empathy=1, verbosity=-1, initiative=-1),
    UserEmotion.STRESSED: ResponsePolicy(warmth=1, empathy=1, verbosity=-1, initiative=-1),
    UserEmotion.NEUTRAL: _NOOP_POLICY,
    UserEmotion.UNKNOWN: _NOOP_POLICY,
}


def derive_response_policy(state: UserEmotionState, threshold: Optional[float] = None) -> ResponsePolicy:
    """Low-confidence gate (section 7): below the configured threshold,
    ALWAYS returns the no-op policy regardless of which emotion was
    (weakly) detected - "low-confidence emotion should have little or no
    effect on Luno behavior" is enforced structurally here, not left to
    prompt wording to hint at.

    `technical_depth` is never touched (see `ResponsePolicy`'s own
    docstring) - every row in `_RESPONSE_POLICY_TABLE` above leaves it at
    the dataclass default (0), and this function does not modify it
    either, so this invariant holds even if a future contributor adds a
    new emotion row and forgets - `test_response_policy_never_reduces_
    technical_depth` iterates the whole table to guard exactly this."""
    if not state.is_confident(threshold):
        return _NOOP_POLICY
    return _RESPONSE_POLICY_TABLE.get(state.emotion, _NOOP_POLICY)


# ─────────────────────────────────────────────
#  LLM PROMPT INTEGRATION (bounded, uncertainty-hedged block)
# ─────────────────────────────────────────────

_POLICY_PHRASES: Dict[str, Tuple[str, str]] = {
    # dimension: (phrase if positive lean, phrase if negative lean)
    "verbosity": ("a little more detail than usual is welcome", "keep it shorter and to the point"),
    "warmth": ("lean a bit warmer/gentler", "no extra warmth needed"),
    "humor": ("a little humor is welcome if it fits", "ease off the jokes/teasing for now"),
    "energy": ("match their energy - a bit more upbeat", "keep your own energy calmer/quieter"),
    "formality": ("a touch more careful/formal", "stay casual, informality is fine"),
    "initiative": ("fine to proactively suggest something", "don't pile on extra suggestions right now"),
    "empathy": ("acknowledge how they might be feeling, briefly and naturally", ""),
}


def _policy_sentence(policy: ResponsePolicy) -> str:
    bits: List[str] = []
    for dim in ("warmth", "empathy", "humor", "verbosity", "energy", "initiative", "formality"):
        delta = getattr(policy, dim)
        if delta == 0:
            continue
        positive, negative = _POLICY_PHRASES.get(dim, ("", ""))
        phrase = positive if delta > 0 else negative
        if phrase:
            bits.append(phrase)
    return "; ".join(bits)


def build_emotional_context_prompt(state: UserEmotionState, policy: ResponsePolicy, threshold: Optional[float] = None) -> str:
    """Pure function, same "" -on-nothing-to-say shape as `luno.
    memory_retrieval.prompt.build_memory_prompt_block()` - a caller can
    always unconditionally try to inject this without a separate
    conditional. Returns "" whenever the state isn't confident enough or
    the derived policy is a no-op, so an ambiguous/neutral turn adds
    NOTHING to the prompt (section 6/16: never manufacture certainty).

    Wording follows section 16's required hedging almost verbatim:
    "may be uncertain... use only as soft conversational guidance... do
    not state the inferred emotion as fact unless the user explicitly
    expressed it" - plus an explicit, standing reminder (mirrors
    `luno.persona._NATURAL_CONVERSATION_INSTRUCTION`'s "floor" pattern)
    that this NEVER outranks technical/factual accuracy or verified tool
    results (section 8/10)."""
    if not state.is_confident(threshold) or policy.is_noop():
        return ""

    sentence = _policy_sentence(policy)
    guidance = f" {sentence}." if sentence else ""

    return (
        f"Inferred emotional context (uncertain - soft conversational guidance only, "
        f"confidence {state.confidence:.2f}): the user's recent message reads as possibly "
        f"{state.emotion.value}. Do not state this as fact or say things like \"I know you're "
        f"{state.emotion.value}\" unless the user actually said so themselves - just let it "
        f"subtly color your tone.{guidance} This NEVER overrides verified facts, tool results, "
        f"safety rules, or technical/factual accuracy - a technical question still gets a "
        f"precise, correct technical answer first, personality and warmth are seasoning on top, "
        f"not a replacement."
    )
