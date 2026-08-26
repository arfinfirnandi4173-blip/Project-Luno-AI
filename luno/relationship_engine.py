"""
relationship_engine.py
=======================

LUNO Relationship Engine Foundation sprint - a small, additive,
deterministic representation of "what is the current state/history of
Luno's relationship with the user", kept structurally separate from
Memory ("what happened?"), the Emotion Engine ("how does the user
appear to feel right now?"), and Personality ("how should Luno
behave?").

WHY THIS IS A NEW MODULE
-------------------------
The mandatory pre-flight audit for this sprint searched the entire
repository for existing relationship/affection/bond/trust/familiarity/
closeness/social-state/user-profile/interaction-history concepts
(`grep -rniE` across `luno/` and `config/`). Nothing was found beyond
`config/persona.json`/`luno/persona.py`'s flavor text ("AI companion",
"affectionately") - pure prompt content, not state - and unrelated
stray uses of the English word "trust" in code comments about trusting
an HTTP response. There is no existing system to reuse or extend; this
is genuinely new ground, not a duplicate.

ARCHITECTURAL SEPARATION (kept deliberately one-way, no cycles):

    Memory                  "what happened?"           (unchanged, read-only signal)
    Emotion Engine          "how does the user feel right now?"  (unchanged, read-only signal, UNUSED this sprint)
        |
        v
    Relationship Engine     "what is our relationship state/history?"  (THIS MODULE)
        |
        v
    RelationshipContextBuilder   compact, banded, bounded prompt text
        |
        v
    Existing prompt architecture (main_runtime_demo.py's PlannerBridgeModule)
        |
        v
    LLM

This module never imports `luno.memory`, `luno.memory_guard`,
`luno.emotion_engine`, or `luno.persona` - it only ever RECEIVES plain
values (a boolean, a string, an optional `UserEmotionState`) from its
caller, exactly like `luno.emotion_engine` itself never imports
`luno.memory`. There is no path from this module back into any of
those, so no circular dependency is even possible by construction.

WHAT THIS FOUNDATION DELIBERATELY DOES NOT DO
------------------------------------------------
Per the sprint's own explicit deferral list: no jealousy, no
possessiveness, no dependency modeling, no love/romance score, no
sexual behavior, no breakup system, no relationship "levels", no gifts/
dates/dating-simulation mechanics, no complex attachment psychology, no
NPC-style relationship quests. Those belong to later, separately-
reviewed sprints, not fabricated here just because the JSON could
technically hold more fields.

`LONG_GAP_RECONNECTION` is defined in `RelationshipSignal` for
extensibility (the sprint brief lists "long_gap_between_interactions"
as an example update signal), but detecting a real gap and deciding
what - if anything - should happen to relationship state as a result is
explicitly DEFERRED, not implemented here: nothing in this module
currently emits or reacts to it. Documented rather than silently
half-built.

DETERMINISM, NOT LLM-WRITTEN SCORES
--------------------------------------
The LLM is NEVER given a way to write `trust`/`closeness`/`familiarity`
directly. State only ever changes through `RelationshipEngine.apply()`,
a pure function of (current state, a small enum of deterministically-
classified signals, a timestamp) - see `classify_turn()` for the
rule-based (keyword/boolean, same heuristic style as
`luno.emotion_engine`) classification step. A technical/device-command
turn with none of the classified signals present changes NOTHING except
`interaction_count` (a plain activity counter, not an emotional score)
and `last_interaction_timestamp`.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Pattern, Tuple

from . import config
from . import persistence

#: Deliberately typed as `Any`, not imported from `luno.emotion_engine`
#: (see module docstring's "never imports" note) - this module accepts
#: whatever the caller's current Emotion Engine state object is via
#: duck typing only, so it has zero import-time coupling to that module
#: at all, not even for a type hint.
EmotionStateLike = Any


#: Bumped only if the on-disk shape of relationship_state.json ever
#: changes incompatibly. The loader intentionally does NOT attempt
#: migration for a mismatched version - it fails safe to a fresh
#: default state instead (section 15 of this sprint's brief: "fail
#: safely rather than silently interpreting incompatible data").
RELATIONSHIP_SCHEMA_VERSION = 1

#: Defensive upper bound for integer counters loaded from disk - guards
#: against a hand-corrupted file claiming an absurd value; ordinary
#: usage will never come remotely close to this.
_MAX_COUNTER = 1_000_000_000


# ─────────────────────────────────────────────
#  VALUE COERCION / BOUNDS (never trust persisted or caller-supplied values)
# ─────────────────────────────────────────────


def _clamp01(value: Any, default: float = 0.0) -> float:
    """Coerces to float and clamps to [0.0, 1.0]. Non-numeric, NaN, or
    otherwise unparseable input falls back to `default` rather than
    propagating a poisoned value anywhere - `+Infinity`/huge finite
    numbers clamp to 1.0, `-Infinity`/very negative numbers clamp to
    0.0 (finite-but-out-of-range values are clamped, not defaulted, per
    section 8's "clamp or reject" - a value is still meaningful, just
    out of range; only truly non-numeric/NaN input is treated as
    unusable)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f):
        return default
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _clamp_counter(value: Any, default: int = 0) -> int:
    """Same defensive shape as `_clamp01` but for non-negative, bounded
    integer counters."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    i = int(f)
    if i < 0:
        return 0
    if i > _MAX_COUNTER:
        return _MAX_COUNTER
    return i


def _clamp_timestamp(value: Any) -> Optional[float]:
    """`None` is a valid, meaningful value here (no interaction has ever
    been recorded yet) - only reachable/coercible finite numbers are
    accepted, everything else (including NaN/Infinity/garbage) safely
    degrades to `None` rather than poisoning downstream gap/recency
    logic."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f) or f < 0.0:
        return None
    return f


# ─────────────────────────────────────────────
#  STATE MODEL
# ─────────────────────────────────────────────


@dataclass(frozen=True)
class RelationshipState:
    """Compact, DERIVED state only - never raw narrative text (section 7:
    "Avoid storing arbitrary LLM-generated prose as relationship
    state"). Frozen/immutable, same "a new turn always produces a new
    instance" convention `luno.emotion_engine.UserEmotionState` already
    uses - nothing downstream can accidentally hold a stale reference
    and mutate it in place.

    Field choice: the sprint brief's own recommended minimal dimensions
    (familiarity/trust/closeness/interaction_count/
    shared_experience_count/last_interaction_timestamp), since the
    pre-flight audit found no existing project naming convention to
    follow instead. `schema_version` (not `relationship_version` - the
    brief's own §15 supersedes its §5 wording with this exact term) is
    the on-disk compatibility marker."""

    schema_version: int = RELATIONSHIP_SCHEMA_VERSION
    familiarity: float = 0.0
    trust: float = 0.0
    closeness: float = 0.0
    interaction_count: int = 0
    shared_experience_count: int = 0
    last_interaction_timestamp: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "familiarity": self.familiarity,
            "trust": self.trust,
            "closeness": self.closeness,
            "interaction_count": self.interaction_count,
            "shared_experience_count": self.shared_experience_count,
            "last_interaction_timestamp": self.last_interaction_timestamp,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "RelationshipState":
        """Never raises. Two independent layers of defensive handling:

        1. Wrong root type (not a dict at all - e.g. a JSON list, string,
           or number) -> full default state.
        2. Missing/mismatched `schema_version` (including a future,
           currently-unmigratable version, or a hand-edited file that
           never had one) -> full default state, per section 15's
           explicit "fail safely rather than silently interpreting
           incompatible data" rule - this loader does not attempt
           migration.

        Within a matching, valid schema version, each individual field
        is defaulted INDEPENDENTLY via `_clamp01`/`_clamp_counter`/
        `_clamp_timestamp` - a partial file (some fields present, some
        missing) or one with extra/unknown keys loads the fields it DOES
        have correctly rather than being discarded wholesale; unknown
        keys are simply never read (silently dropped on next save, never
        an error)."""
        if not isinstance(data, dict):
            return cls()
        if data.get("schema_version") != RELATIONSHIP_SCHEMA_VERSION:
            return cls()
        return cls(
            schema_version=RELATIONSHIP_SCHEMA_VERSION,
            familiarity=_clamp01(data.get("familiarity"), 0.0),
            trust=_clamp01(data.get("trust"), 0.0),
            closeness=_clamp01(data.get("closeness"), 0.0),
            interaction_count=_clamp_counter(data.get("interaction_count"), 0),
            shared_experience_count=_clamp_counter(data.get("shared_experience_count"), 0),
            last_interaction_timestamp=_clamp_timestamp(data.get("last_interaction_timestamp")),
        )


_DEFAULT_STATE = RelationshipState()


# ─────────────────────────────────────────────
#  PERSISTENCE
# ─────────────────────────────────────────────


class RelationshipStore:
    """Load/save only - no update policy lives here (keeps this class
    small and single-purpose, per section 27's "no god object" rule).
    Mirrors `luno.memory`'s "missing file -> feature simply starts
    fresh, never raises" convention.

    Persistent State Hardening V2 sprint: `save()` now goes through
    `luno.persistence.atomic_write_json()` - the same backup-before-
    write + temp-file + fsync + `os.replace()` contract already proven
    on `config/long_term_memory.json`, generalized (see
    `docs/change_impact/persistent_state_hardening_v2.md`). This
    REPLACES the previous hand-rolled `{path}.tmp` + `os.replace()`
    (which had no fsync and no backup) with the shared, tested helper -
    no behavior change from the caller's point of view (`save()` still
    returns `True`/`False`, never raises; `load()`'s missing/malformed
    fallback to `RelationshipState()` is unchanged), but every write is
    now also backed up first and the write itself is fsync'd before the
    atomic replace."""

    @staticmethod
    def load() -> RelationshipState:
        path = config.RELATIONSHIP_STATE_FILE
        data, _source = persistence.safe_load_json(path, default=None)
        if data is None:
            return RelationshipState()
        return RelationshipState.from_dict(data)

    @staticmethod
    def save(state: RelationshipState) -> bool:
        """Returns True/False rather than raising - a persistence
        failure must never break the turn that triggered it (section 20/
        the whole project's "never let a side-concern crash the runtime"
        convention)."""
        path = config.RELATIONSHIP_STATE_FILE
        if not path:
            return False
        try:
            persistence.atomic_write_json(path, state.to_dict())
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────
#  UPDATE SIGNALS (deterministic classification, never LLM-written scores)
# ─────────────────────────────────────────────


class RelationshipSignal(str, Enum):
    SUCCESSFUL_TASK = "successful_task"
    USER_CORRECTION = "user_correction"
    USER_FEEDBACK_POSITIVE = "user_feedback_positive"
    MEANINGFUL_SHARED_EXPERIENCE = "meaningful_shared_experience"
    #: Defined for extensibility, deliberately UNIMPLEMENTED this sprint
    #: - see module docstring's "what this foundation deliberately does
    #: not do yet".
    LONG_GAP_RECONNECTION = "long_gap_reconnection"


def _p(pattern: str) -> Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


#: Bilingual (ID/EN), same "ordered keyword pattern list" heuristic style
#: as `luno.emotion_engine`'s estimator - deliberately narrow/explicit so
#: an ordinary technical question is never mistaken for a correction or
#: for praise (section 9/18: technical turns must remain relationship-
#: neutral by default - simply not matching any pattern here already
#: guarantees that, no separate "is this technical" gate is needed).
_CORRECTION_PATTERNS: Tuple[Pattern[str], ...] = (
    _p(r"\b(bukan itu|bukan gitu|salah tuh|itu salah|kamu salah)\b"),
    _p(r"\b(not what i meant|that'?s wrong|you got that wrong|that'?s not right|no,? that'?s not it)\b"),
)

_POSITIVE_FEEDBACK_PATTERNS: Tuple[Pattern[str], ...] = (
    _p(r"\b(makasih banyak|terima kasih banyak|kamu emang (keren|membantu)|sangat membantu)\b"),
    _p(r"\b(thank you so much|thanks a lot|you'?re the best|great job|really helpful|you'?re amazing)\b"),
)


def classify_turn(
    text: Optional[str],
    had_successful_tool_call: bool = False,
    explicit_memory_shared: bool = False,
    emotion_state: Optional[EmotionStateLike] = None,
) -> List[RelationshipSignal]:
    """Pure function, never raises. Rule-based/deterministic ONLY - no
    LLM call, no LLM-generated text is ever read as a signal (mirrors
    `luno.memory_guard`'s own "never read the LLM's reply as truth"
    convention, applied here to relationship state instead of device
    facts).

    `emotion_state` is accepted for architectural correctness against
    this sprint's own dependency diagram (Emotion Engine -> Relationship
    Engine is a legitimate READ, never the reverse) but is NOT used to
    derive any signal in this foundation sprint - "a user being sad
    today does not automatically mean relationship.closeness += 0.2"
    (section 11's own example of a valid, unchanged result). Left as a
    real, typed, documented no-op parameter rather than omitted, so a
    future sprint can wire a clearly-justified rule here without
    changing this function's signature or every call site."""
    signals: List[RelationshipSignal] = []
    if had_successful_tool_call:
        signals.append(RelationshipSignal.SUCCESSFUL_TASK)
    if explicit_memory_shared:
        signals.append(RelationshipSignal.MEANINGFUL_SHARED_EXPERIENCE)

    if isinstance(text, str) and text.strip():
        if any(p.search(text) for p in _CORRECTION_PATTERNS):
            signals.append(RelationshipSignal.USER_CORRECTION)
        if any(p.search(text) for p in _POSITIVE_FEEDBACK_PATTERNS):
            signals.append(RelationshipSignal.USER_FEEDBACK_POSITIVE)

    return signals


# ─────────────────────────────────────────────
#  DETERMINISTIC STATE TRANSITION
# ─────────────────────────────────────────────

#: Every delta is small and hand-reviewable (not learned/weighted) - see
#: module docstring "DETERMINISM" section. `technical_depth`-style
#: invariant equivalent here: nothing in this table can ever move
#: `interaction_count`/`last_interaction_timestamp` (those are handled
#: unconditionally in `RelationshipEngine.apply()` itself, once per
#: call, regardless of which signals fired).
_TRUST_DELTA_SUCCESSFUL_TASK = 0.01
_TRUST_DELTA_CORRECTION = -0.02
_TRUST_DELTA_POSITIVE_FEEDBACK = 0.01
_CLOSENESS_DELTA_POSITIVE_FEEDBACK = 0.02
_FAMILIARITY_DELTA_SHARED_EXPERIENCE = 0.03


class RelationshipEngine:
    """Stateless - `apply()` is a pure function of (state, signals, now),
    same "trivially unit-testable, no hidden state" shape as
    `luno.emotion_engine.EmotionEstimator`/`derive_response_policy`."""

    @staticmethod
    def apply(
        state: RelationshipState,
        signals: Iterable[RelationshipSignal],
        now: Optional[float] = None,
    ) -> RelationshipState:
        """One call = one observed turn. `interaction_count` always
        increments by exactly 1 and `last_interaction_timestamp` is
        always set to `now`, regardless of which (if any) signals fired
        - this is deliberate: every turn is "an interaction" (a plain
        activity counter), independent of whether anything emotionally
        meaningful happened (section 9: a bare device command must not
        cause a large trust/closeness change, but it IS still one more
        turn of interaction history).

        Deterministic: identical `(state, signals, now)` always produces
        an identical result - no randomness, no wall-clock reads unless
        the caller omits `now` (tests should always pass it explicitly)."""
        resolved_now = time.time() if now is None else now
        signal_set = set(signals)

        familiarity = state.familiarity
        trust = state.trust
        closeness = state.closeness
        shared_experience_count = state.shared_experience_count

        if RelationshipSignal.SUCCESSFUL_TASK in signal_set:
            trust += _TRUST_DELTA_SUCCESSFUL_TASK
        if RelationshipSignal.USER_CORRECTION in signal_set:
            trust += _TRUST_DELTA_CORRECTION
        if RelationshipSignal.USER_FEEDBACK_POSITIVE in signal_set:
            closeness += _CLOSENESS_DELTA_POSITIVE_FEEDBACK
            trust += _TRUST_DELTA_POSITIVE_FEEDBACK
        if RelationshipSignal.MEANINGFUL_SHARED_EXPERIENCE in signal_set:
            familiarity += _FAMILIARITY_DELTA_SHARED_EXPERIENCE
            shared_experience_count += 1
        # LONG_GAP_RECONNECTION: intentionally a no-op - see module docstring.

        return RelationshipState(
            schema_version=RELATIONSHIP_SCHEMA_VERSION,
            familiarity=_clamp01(familiarity),
            trust=_clamp01(trust),
            closeness=_clamp01(closeness),
            interaction_count=_clamp_counter(state.interaction_count + 1),
            shared_experience_count=_clamp_counter(shared_experience_count),
            last_interaction_timestamp=resolved_now,
        )

    @staticmethod
    def observe_turn(
        state: RelationshipState,
        text: Optional[str],
        had_successful_tool_call: bool = False,
        explicit_memory_shared: bool = False,
        emotion_state: Optional[EmotionStateLike] = None,
        now: Optional[float] = None,
    ) -> RelationshipState:
        """Convenience wrapper: `classify_turn()` + `apply()` in one
        call - what `main_runtime_demo.py`'s call site actually uses.
        Kept as a thin composition of the two pure functions above
        (rather than inlining classification into `apply()`) so each
        stays independently testable, per section 27's "small,
        separated components" rule."""
        signals = classify_turn(
            text,
            had_successful_tool_call=had_successful_tool_call,
            explicit_memory_shared=explicit_memory_shared,
            emotion_state=emotion_state,
        )
        return RelationshipEngine.apply(state, signals, now=now)


# ─────────────────────────────────────────────
#  PROMPT INTEGRATION (compact, banded, bounded - never raw scores)
# ─────────────────────────────────────────────

#: A brand-new relationship says nothing about itself yet - avoids
#: "no fake memory"/section 29's "should not force ... into every
#: interaction" for a user Luno has barely interacted with.
_MIN_INTERACTIONS_FOR_CONTEXT = 5


def _band(value: float, labels: Tuple[str, str, str, str], thresholds: Tuple[float, float, float] = (0.25, 0.5, 0.75)) -> str:
    low, mid, high, top = labels
    if value < thresholds[0]:
        return low
    if value < thresholds[1]:
        return mid
    if value < thresholds[2]:
        return high
    return top


class RelationshipContextBuilder:
    """Pure function wrapped in a class only for naming-convention
    symmetry with `RelationshipState`/`RelationshipEngine`/
    `RelationshipStore` (section 27) - no state of its own."""

    @staticmethod
    def build_prompt_block(state: RelationshipState) -> str:
        """Returns "" whenever there isn't yet enough interaction history
        to say anything meaningful - same "" -on-nothing-to-say shape as
        `luno.emotion_engine.build_emotional_context_prompt()` and
        `luno.memory_retrieval.prompt.build_memory_prompt_block()`, so a
        caller can always unconditionally try to inject this. Reports
        semantic BANDS (never raw floats - section 16: "Do not include
        raw internal scores unless there is a clear reason"), and is
        explicit that this is background grounding only, never a license
        to inject romance/clinginess or to override anything factual."""
        if state.interaction_count < _MIN_INTERACTIONS_FOR_CONTEXT:
            return ""

        familiarity_band = _band(state.familiarity, ("new", "familiar", "established", "close"))
        trust_band = _band(state.trust, ("building", "steady", "solid", "deep"))
        closeness_band = _band(state.closeness, ("distant", "comfortable", "warm", "close"))

        bits = [f"familiarity: {familiarity_band}", f"trust: {trust_band}", f"closeness: {closeness_band}"]
        if state.shared_experience_count > 0:
            bits.append(f"{state.shared_experience_count} shared experience(s) noted")

        return (
            "Relationship context (background grounding only, derived from actual interaction "
            "history - do not announce these labels explicitly or recite them verbatim, just let "
            "them subtly color warmth/familiarity of tone when it naturally fits): "
            + "; ".join(bits) + ". This NEVER overrides verified facts, technical accuracy, or "
            "safety, and never by itself justifies romantic or clingy language - most replies, "
            "especially technical ones, should show no trace of this at all."
        )
