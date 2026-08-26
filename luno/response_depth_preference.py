"""
response_depth_preference.py
===============================

Persistent Adaptive Response Depth Preference sprint - a tiny, bounded,
CROSS-SESSION extension of the conversation-scoped, in-memory-only
adaptive depth preference `luno/response_policy.py` already implements
(`DepthPreference` / `detect_depth_feedback()` / `apply_depth_feedback()`
- ALL UNCHANGED by this sprint, still pure, still zero I/O; see that
module's own long-standing purity guard,
`tests/test_response_policy.py::test_response_policy_module_imports_no_memory_or_persistence_modules`,
which explicitly forbids `luno.persistence` from ever being imported
there - still enforced, still passing, still un-modified).

This module is the ONE place any I/O for this feature happens.
Dependency direction (matches `luno/persistence.py`'s own stated rule -
"domain module -> persistence -> filesystem", never the reverse):

    main_runtime_demo.py -> this module -> luno.persistence -> filesystem
    this module -> luno.response_policy (reads DEPTH_BIAS_MIN/MAX only)

`luno.response_policy` never imports this module back - the dependency
is strictly one-directional, so the pure depth-decision function stays
completely decoupled from any persistence concern.

NOT a memory/truth/relationship-trust store. `PersistedDepthPreference`
stores exactly one bounded signed integer ("Vinn tends to prefer
shorter/more detailed replies") plus a bounded sample count - nothing
else. No raw feedback text, no transcript, no query history, no response
history are ever written here (see
docs/change_impact/persistent_adaptive_response_depth.md's "Privacy/
trust boundary" section for the full audit).

WHY A NEW FILE, NOT AN EXISTING STORE (Phase 1 audit conclusion)
--------------------------------------------------------------------
Every existing persistent store was read before this decision was made:
`long_term_memory.json` (facts about the user), `verified_facts.json`
(tool-verified ground truth), `episodic_memory.json` (shared
experiences), `relationship_state.json` (familiarity/trust/closeness -
close in SHAPE to this feature, but a semantically different concept: a
relational-trust signal, not a verbosity preference - conflating the two
would make relationship trust accidentally swing response length, or
vice versa, which is a coupling this sprint's brief explicitly forbids),
`habit_memory.json` (device-automation patterns). None of them is an
appropriate home for "does the user prefer short or detailed spoken
replies" - a dedicated, minimal file is the correct, minimal-footprint
choice here, exactly matching this codebase's own established one-
concept-per-file convention (six distinct small JSON stores already
exist for six distinct small concepts).

WHY NOT REBUILD BACKUP/ATOMIC-WRITE LOGIC
--------------------------------------------
`luno.persistence.atomic_write_json()`/`safe_load_json()` (Persistent
State Hardening V2 sprint) already provide everything Phase 2 of this
sprint's brief asks for - atomic write, pre-write backup, corruption-
safe loading, bounded backup retention, pytest-isolation refusal. This
module is a THIN domain wrapper around those two functions, the same
shape `luno/relationship_engine.py`'s own `RelationshipStore` already
uses for its (structurally near-identical) load/save pair - no new
backup/atomic-write primitive was written for this sprint.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

from . import config
from . import persistence
from .response_policy import DEPTH_BIAS_MAX, DEPTH_BIAS_MIN

#: On-disk compatibility marker - same "mismatched/missing version -> full
#: default, no migration attempted" convention
#: `luno.relationship_engine.RelationshipState`/`RELATIONSHIP_SCHEMA_VERSION`
#: already established (Phase 15 of that sprint's own brief: "fail safely
#: rather than silently interpreting incompatible data").
SCHEMA_VERSION = 1

#: A generous ceiling, not a meaningful behavioral bound (mirrors
#: `luno.relationship_engine._MAX_COUNTER`'s own "defensive against
#: corrupted/malicious input, not a real usage limit" role) - realistic
#: usage over the life of this project will never remotely approach this.
MAX_SAMPLE_COUNT = 100_000

#: A conversation's LOCAL preference (see `luno.response_policy.DepthPreference`)
#: must accumulate at least this many depth-feedback events before it is
#: allowed to influence the PERSISTENT baseline at all - "one feedback
#: does not immediately permanently persist" (the sprint brief's own
#: scenario G). Checked via `should_persist()` below, evaluated once per
#: turn (never a background timer/thread) at the same point the
#: conversation-local preference itself is already being updated.
PERSIST_MIN_SAMPLES = 3

#: How much a single merge event may move the persisted baseline TOWARD
#: the conversation's current local bias - a weighted blend, never a
#: direct overwrite, so one conversation (however consistent) can never
#: instantly replace weeks of prior evidence, and CONFLICTING evidence
#: from a later conversation pulls the baseline back toward neutral
#: gradually rather than swinging it to the opposite extreme (the
#: brief's own scenario J).
PERSIST_BLEND_WEIGHT = 0.3


@dataclass
class PersistedDepthPreference:
    """The entire on-disk schema for
    `config/response_depth_preference.json`. Deliberately minimal - see
    module docstring's "NOT a memory/truth/relationship-trust store"
    section. `bias` is a signed integer in [`DEPTH_BIAS_MIN`,
    `DEPTH_BIAS_MAX`] (the SAME bounds `luno.response_policy`'s
    conversation-local `DepthPreference` already uses - one shared
    contract, not two independently-chosen ranges). `sample_count` is a
    plain observability counter (how many times this baseline has ever
    been merged/updated), bounded by `MAX_SAMPLE_COUNT`, never itself
    read by `compute_response_policy()`."""

    schema_version: int = SCHEMA_VERSION
    bias: int = 0
    sample_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"schema_version": self.schema_version, "bias": self.bias, "sample_count": self.sample_count}

    @classmethod
    def from_dict(cls, data: Any) -> "PersistedDepthPreference":
        """Never raises. Same two-layer defensive shape
        `RelationshipState.from_dict()` already established: (1) wrong
        root type (not a dict) -> full default; (2) missing/mismatched
        `schema_version` -> full default, no migration attempted. Within
        a matching schema version, `bias`/`sample_count` are each
        independently clamped - a partial or hand-edited file loads what
        it validly can rather than being discarded wholesale."""
        if not isinstance(data, dict):
            return cls()
        if data.get("schema_version") != SCHEMA_VERSION:
            return cls()
        return cls(
            schema_version=SCHEMA_VERSION,
            bias=_clamp_bias(data.get("bias")),
            sample_count=_clamp_sample_count(data.get("sample_count")),
        )


def _clamp_bias(value: Any) -> int:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    if f != f or f in (float("inf"), float("-inf")):  # NaN/Infinity guard, no math import needed
        return 0
    return max(DEPTH_BIAS_MIN, min(DEPTH_BIAS_MAX, round(f)))


def _clamp_sample_count(value: Any) -> int:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0
    if f != f or f in (float("inf"), float("-inf")):
        return 0
    i = int(f)
    if i < 0:
        return 0
    if i > MAX_SAMPLE_COUNT:
        return MAX_SAMPLE_COUNT
    return i


class DepthPreferenceStore:
    """Load/save only - no update/merge policy lives here (same "no god
    object" split `RelationshipStore` already models - update policy is
    `merge_conversation_into_persistent()` below, a separate, pure
    function). Static methods, same shape as `RelationshipStore`."""

    @staticmethod
    def load() -> PersistedDepthPreference:
        path = config.RESPONSE_DEPTH_PREFERENCE_FILE
        data, _source = persistence.safe_load_json(path, default=None)
        if data is None:
            return PersistedDepthPreference()
        return PersistedDepthPreference.from_dict(data)

    @staticmethod
    def save(preference: PersistedDepthPreference) -> bool:
        """Returns True/False rather than raising - a persistence
        failure here must never break the turn that triggered it, same
        convention as `RelationshipStore.save()`."""
        path = config.RESPONSE_DEPTH_PREFERENCE_FILE
        if not path:
            return False
        try:
            persistence.atomic_write_json(path, preference.to_dict())
            return True
        except Exception:
            return False


def should_persist(local_feedback_count: int) -> bool:
    """True once every `PERSIST_MIN_SAMPLES` LOCAL feedback events within
    ONE conversation - `local_feedback_count` is
    `luno.response_policy.DepthPreference.feedback_count`, read directly
    by the caller (`main_runtime_demo.py`), never recomputed here. `0` (a
    conversation with no feedback yet) is always `False` - matches the
    hard "silence is never feedback" rule by construction (there is
    nothing to persist if nothing was ever detected)."""
    return local_feedback_count > 0 and local_feedback_count % PERSIST_MIN_SAMPLES == 0


def merge_conversation_into_persistent(
    persisted: PersistedDepthPreference, local_bias: int,
) -> PersistedDepthPreference:
    """Pure - returns a NEW `PersistedDepthPreference`, never mutates
    `persisted`. Blends `local_bias` (a conversation's CURRENT local
    `DepthPreference.bias`) into the persisted baseline via a
    conservative weighted average (`PERSIST_BLEND_WEIGHT`), never a
    direct overwrite - satisfies "the user must never become permanently
    stuck in SHORT or DETAILED because of a handful of comments" by
    construction: even a maximally-biased local conversation
    (`local_bias == DEPTH_BIAS_MAX`) merged against a neutral persisted
    baseline only moves it by `PERSIST_BLEND_WEIGHT * DEPTH_BIAS_MAX`
    (7-8 points) in ONE merge event, not to the extreme in one step."""
    blended = persisted.bias * (1 - PERSIST_BLEND_WEIGHT) + local_bias * PERSIST_BLEND_WEIGHT
    new_bias = max(DEPTH_BIAS_MIN, min(DEPTH_BIAS_MAX, round(blended)))
    new_count = min(MAX_SAMPLE_COUNT, persisted.sample_count + 1)
    return PersistedDepthPreference(schema_version=SCHEMA_VERSION, bias=new_bias, sample_count=new_count)
