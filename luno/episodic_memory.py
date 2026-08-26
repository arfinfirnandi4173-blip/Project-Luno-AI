"""
episodic_memory.py
===================

LUNO Shared Experience & Episodic Memory Layer sprint - a small, additive,
deterministic representation of "what meaningful things have actually
happened between Luno and the user", kept structurally separate from:

    Memory (luno.memory)         "what facts/topics exist?"
        - long-term FACTS ("user is allergic to peanuts") - no category,
          no outcome, no provenance, not an event.
        - session SUMMARIES - an LLM-generated 1-3 sentence recap of an
          ENTIRE session, triggered unconditionally at session end, with
          NO meaningfulness filter, NO dedup, NOT retrieved through the
          relevance-scored memory_retrieval pipeline (injected every turn
          regardless of query - see build_session_summary_prompt()). This
          is the closest existing neighbor to "episodic memory" and was
          seriously considered as a base to extend; it was NOT reused
          because doing so would mean giving up per-event meaningfulness
          filtering, structured category/provenance, and content-based
          dedup - all explicitly required by this sprint - without being
          able to touch session_summaries' own protected shape (used
          elsewhere, unconditionally injected by design).
    THIS MODULE                  "what specific meaningful thing happened,
                                   when, and how do we know it's real?"
    Relationship Engine          "what does this history imply about the
                                   relationship?" (reads a boolean signal
                                   from this module, never the reverse)
    Emotion Engine                "how does the user feel right now?"
                                   (independent, never consulted here)

WHY THIS IS A NEW MODULE
-------------------------
The mandatory pre-flight audit for this sprint searched the entire
repository (`grep -rniE "episodic|shared.?experience|experience_id|
event_id|fingerprint"` across `luno/` and `tests/`) and found no existing
system that stores individual, structured, deduplicated, meaningfulness-
filtered event records. `luno.memory`'s `_memories` list is a flat
`{"id", "text", "created_at"}` shape with no category/outcome/provenance
fields and no dedup beyond fuzzy substring matching against ALL existing
facts (fine for short facts, wrong tool for longer event summaries);
extending it would mean changing its existing, protected contract. This
module is new ground, built on top of (never duplicating) the existing
Memory Retrieval package for the RETRIEVAL side - see
`make_episodic_experience_source()` below, which is just one more
`MemorySource` registered the same way `long_term_memory`/`vision_objects`/
etc. already are; all bounding (`max_results`/`max_tokens`), temporal
wording ("Observed X ago"/"approximately X ago - may be outdated"), and
deduplication-of-retrieved-candidates already happen for free inside
`luno.memory_retrieval.retriever.MemoryRetriever` once this source is
registered - nothing here reimplements any of that.

ARCHITECTURAL SEPARATION (kept deliberately one-way, no cycles):

    Conversation turn (text, had_successful_tool_call)
        |
        v
    detect_candidate_experience()   deterministic, regex-based, ZERO LLM -
        |                            never invents content, every summary is
        |                            a bounded slice of the REAL turn text
        v
    _build_experience()              validation (length bound, non-empty) +
        |                            content-fingerprint experience_id
        v
    EpisodicMemoryStore.load/save    dedup-by-fingerprint, bounded (FIFO),
        |                            atomic write (same tmp+os.replace
        |                            pattern as luno.relationship_engine)
        v
    observe_turn() -> (is_new: bool, entry)
        |
        +--> main_runtime_demo.py: OR'd into RelationshipEngine's existing
        |    `explicit_memory_shared` signal (Episodic Memory -> Relationship
        |    Engine, one-way, exactly per this sprint's own dependency rule -
        |    this module never imports luno.relationship_engine)
        |
        +--> make_episodic_experience_source(): registered as one more
             luno.memory_retrieval MemorySource - retrieved the SAME way
             every other memory is, injected through the SAME existing
             `memory_block` prompt slot, no parallel "experience prompt".

This module never imports `luno.memory`, `luno.emotion_engine`,
`luno.persona`, or `luno.relationship_engine` - it only ever RECEIVES plain
values (a string, two booleans) from its caller, exactly like
`luno.relationship_engine` itself never imports `luno.memory`/
`luno.emotion_engine`/`luno.persona`. It DOES import
`luno.memory_retrieval.models`/`.query` (read-only type/helper reuse for
the retrieval side) - that package never imports this module back, so no
cycle is possible by construction.

WHAT COUNTS AS AN EXPERIENCE (and what does not)
--------------------------------------------------
Per the sprint brief's own examples: a completed significant feature, a
solved persistent technical problem, a configured HA device, a completed
milestone, or an explicitly user-declared important moment DO count.
"berapa 1+1?", "halo", "nyalakan lampu", "berapa suhu CPU?", ordinary
device commands, and small talk do NOT - even though many of those ALSO
produce `had_successful_tool_call=True` (turning on a light is a
successful tool call too). This is why `had_successful_tool_call` is
NEVER, by itself, sufficient to create a memory here - it is only ever
corroborating provenance metadata layered on top of an explicit textual
signal (see `detect_candidate_experience()`). No text pattern match, no
memory, regardless of tool success. This mirrors `luno.relationship_engine`'s
own "a bare device command must not cause a large trust/closeness change"
discipline, applied to memory creation instead of relationship deltas.

GROUNDING / NO HALLUCINATION
------------------------------
Every stored `summary` is a bounded, whitespace-normalized SLICE of the
actual conversation turn text that triggered detection - never LLM
paraphrase, never invented detail. There is no LLM call anywhere in this
module. `source` records exactly which real signals corroborated the
entry (e.g. "conversation+tool_verified", "conversation+explicit_user_statement")
so every stored memory is auditable back to a real event.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Pattern, Tuple

from . import config
from . import persistence
from .memory_retrieval.models import MemoryRetrievalConfig, QueryAnalysis, RelevantMemory
from .memory_retrieval.query import token_overlap

#: Bumped only if the on-disk shape of episodic_memory.json ever changes
#: incompatibly - same "fail safe to skipping the entry, never attempt
#: migration" convention as luno.relationship_engine.RELATIONSHIP_SCHEMA_VERSION.
EPISODIC_SCHEMA_VERSION = 1

#: Bounded summary length (section 16 of the sprint brief: "maximum summary
#: length"). Long turns are truncated with an ellipsis rather than rejected
#: outright - a shorter-but-real memory is preferable to no memory at all.
_MAX_SUMMARY_CHARS = 280


# ─────────────────────────────────────────────
#  EXPERIENCE MODEL
# ─────────────────────────────────────────────


class ExperienceCategory(str, Enum):
    """Small, controlled set - directly justified by the sprint brief's own
    "what counts as an experience" list. Deliberately NOT a freeform string
    (an LLM-writable category field would reopen exactly the "unrestricted
    authority to persist arbitrary facts" problem section 8 warns against)."""

    TECHNICAL_PROBLEM_SOLVED = "technical_problem_solved"
    DEVICE_CONFIGURED = "device_configured"
    MILESTONE = "milestone"
    MEANINGFUL_MOMENT = "meaningful_moment"


@dataclass(frozen=True)
class EpisodicExperience:
    """Immutable record - a new turn always produces a new instance, same
    convention as `RelationshipState`/`UserEmotionState`. Field choice:
    only fields this module's own detection/dedup/retrieval/grounding logic
    actually uses (section 6: "do not blindly implement all fields"). No
    `participants` field - Luno is a single-user assistant, there is no
    existing multi-user concept anywhere in this repo to populate it with."""

    schema_version: int = EPISODIC_SCHEMA_VERSION
    experience_id: str = ""
    timestamp: float = 0.0
    category: str = ExperienceCategory.MEANINGFUL_MOMENT.value
    summary: str = ""
    source: str = "conversation"

    @property
    def id(self) -> str:
        """Alias for `experience_id` - `luno.memory_retrieval.retriever
        .MemoryRetriever._deduplicate()` keys retrieval-time dedup on
        `getattr(mem.raw, "id", None)`; every other source's `raw` object
        (TrackedObject, LongTermMemoryRecord, ...) already exposes `.id`,
        so this property lets episodic entries participate in that exact
        same existing mechanism for free instead of a parallel one."""
        return self.experience_id

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experience_id": self.experience_id,
            "timestamp": self.timestamp,
            "category": self.category,
            "summary": self.summary,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["EpisodicExperience"]:
        """Returns `None` (never raises) if `data` isn't a well-formed
        experience record - unlike `RelationshipState.from_dict` (which
        defaults a single object's missing fields), a LIST of experiences
        can safely just DROP one malformed entry and keep the rest (section
        19: "No malformed memory file may crash Luno startup or normal
        conversation processing" + partial/duplicate entries must be
        handled safely) - defaulting a missing `summary` to "" would create
        a fake, empty memory, which is worse than simply not loading it."""
        if not isinstance(data, dict):
            return None
        if data.get("schema_version") != EPISODIC_SCHEMA_VERSION:
            return None

        experience_id = data.get("experience_id")
        if not isinstance(experience_id, str) or not experience_id.strip():
            return None

        timestamp = data.get("timestamp")
        try:
            timestamp = float(timestamp)
        except (TypeError, ValueError):
            return None
        if math.isnan(timestamp) or math.isinf(timestamp) or timestamp < 0:
            return None

        category = data.get("category")
        valid_categories = {c.value for c in ExperienceCategory}
        if category not in valid_categories:
            return None

        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            return None

        source = data.get("source")
        if not isinstance(source, str) or not source.strip():
            source = "conversation"

        return cls(
            schema_version=EPISODIC_SCHEMA_VERSION,
            experience_id=experience_id,
            timestamp=timestamp,
            category=category,
            summary=summary[:_MAX_SUMMARY_CHARS],
            source=source,
        )


# ─────────────────────────────────────────────
#  PERSISTENCE
# ─────────────────────────────────────────────


class EpisodicMemoryStore:
    """Load/save only - no detection/validation policy lives here, same
    "small, single-purpose" split `luno.relationship_engine.RelationshipStore`
    already uses.

    Persistent State Hardening V2 sprint: `save()` now goes through
    `luno.persistence.atomic_write_json()` - the same backup-before-
    write + temp-file + fsync + `os.replace()` contract already proven
    on `config/long_term_memory.json` (see
    `docs/change_impact/persistent_state_hardening_v2.md`). This
    REPLACES the previous hand-rolled `{path}.tmp` + `os.replace()`
    (no fsync, no backup) - no behavior change from the caller's point
    of view (`save()` still returns `True`/`False`, never raises;
    `load()`'s missing/malformed/non-list-root fallback to `[]`, and
    per-entry validation via `EpisodicExperience.from_dict()`, are both
    unchanged)."""

    @staticmethod
    def load() -> List[EpisodicExperience]:
        path = config.EPISODIC_MEMORY_FILE
        data, _source = persistence.safe_load_json(
            path, default=[], validate=lambda d: isinstance(d, list),
        )
        out: List[EpisodicExperience] = []
        for item in data:
            entry = EpisodicExperience.from_dict(item)
            if entry is not None:
                out.append(entry)
        return out

    @staticmethod
    def save(experiences: List[EpisodicExperience]) -> bool:
        """Returns True/False rather than raising - a persistence failure
        must never break the turn that triggered it (same convention as
        RelationshipStore.save)."""
        path = config.EPISODIC_MEMORY_FILE
        if not path:
            return False
        try:
            persistence.atomic_write_json(path, [e.to_dict() for e in experiences])
            return True
        except Exception:
            return False


# ─────────────────────────────────────────────
#  DETECTION (deterministic, regex-based, ZERO LLM - see module docstring)
# ─────────────────────────────────────────────


def _p(pattern: str) -> Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


#: Bilingual (ID/EN) word-level detection, same "narrow, deterministic,
#: keyword-based" heuristic PRINCIPLE as `luno.relationship_engine`'s
#: `_CORRECTION_PATTERNS`/`_POSITIVE_FEEDBACK_PATTERNS` and
#: `luno.emotion_engine`'s estimator, but built from word-SET membership
#: (checked against tokens/regex substrings) rather than one single rigid
#: phrase-adjacency regex per category - natural speech puts a lot of
#: words between "masalah" and "kelar" ("masalah dockernya AKHIRNYA kelar
#: JUGA setelah 3 jam"), so a single strict adjacency regex badly
#: under-matches. Deliberately conservative in a different way instead:
#: requires an explicit, NON-NEGATED outcome/completion word (`_outcome_signal`)
#: PLUS a topic anchor (technical/device keyword, or - for the generic
#: milestone fallback with no specific topic - an explicit "we/kita"
#: collaboration cue), so "udah selesai makan siang" (finished lunch, no
#: topic anchor, no collaboration cue) correctly does NOT qualify.
_WORD_RE = re.compile(r"[a-zA-Z']+")


def _tokenize(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


_OUTCOME_WORDS = {
    "kelar", "selesai", "beres", "teratasi", "fixed", "fix", "solved",
    "resolved", "berhasil", "done", "working", "completed", "finished",
    "configured", "connected", "installed", "nyambung", "terhubung",
}
_NEGATION_WORDS = {
    "belum", "blm", "gak", "ga", "nggak", "enggak", "tidak", "not", "no",
    "never", "hasn't", "haven't", "isn't", "wasn't", "cancel", "batal",
}
_COLLAB_WORDS = {"kita", "kami", "we", "together", "bareng", "berdua"}
_TECHNICAL_TOPIC_RE = _p(r"\b(?:masalah|bug|error|issue|problem)\b")
_DEVICE_TOPIC_RE = _p(r"\b(?:esp32|home\s*assistant|sensor|smart\s*device|perangkat)\b")
_MILESTONE_TOPIC_RE = _p(r"\b(?:milestone|pencapaian|project|proyek|fitur|feature)\b")
_MEANINGFUL_MOMENT_PATTERNS: Tuple[Pattern[str], ...] = (
    _p(r"\b(?:ini\s+penting\s+banget|momen\s+penting|hari\s+yang\s+penting|momen\s+berharga)\b"),
    _p(r"\bthis\s+(?:was|is)\s+(?:a\s+)?(?:big|important|meaningful)\s+(?:moment|day|milestone)\b"),
    _p(r"\bthis\s+means\s+a\s+lot\b"),
)


def _outcome_signal(tokens: List[str]) -> bool:
    """True if an outcome/completion word appears in `tokens` without an
    immediately-preceding (within 3 words) negation word - "masalahnya
    belum kelar" (not yet resolved) correctly does NOT signal an outcome."""
    for i, tok in enumerate(tokens):
        if tok in _OUTCOME_WORDS:
            window_start = max(0, i - 3)
            if not any(t in _NEGATION_WORDS for t in tokens[window_start:i]):
                return True
    return False


def _classify_category(text: str, tokens: List[str]) -> Optional[ExperienceCategory]:
    """Priority order: an explicit importance declaration always counts
    (MEANINGFUL_MOMENT, no outcome-word requirement - "ini momen penting
    banget" doesn't need a completion verb). Otherwise requires
    `_outcome_signal` PLUS a topic anchor, checked most-specific first."""
    if any(p.search(text) for p in _MEANINGFUL_MOMENT_PATTERNS):
        return ExperienceCategory.MEANINGFUL_MOMENT
    if not _outcome_signal(tokens):
        return None
    if _TECHNICAL_TOPIC_RE.search(text):
        return ExperienceCategory.TECHNICAL_PROBLEM_SOLVED
    if _DEVICE_TOPIC_RE.search(text):
        return ExperienceCategory.DEVICE_CONFIGURED
    if _MILESTONE_TOPIC_RE.search(text) or any(w in tokens for w in _COLLAB_WORDS):
        return ExperienceCategory.MILESTONE
    return None


@dataclass(frozen=True)
class _CandidateExperience:
    """Intermediate, pre-validation result of detection - not yet bounded
    (`summary` may exceed `_MAX_SUMMARY_CHARS`), not yet fingerprinted, not
    yet persisted. Kept as its own tiny type (rather than a tuple) so
    `detect_candidate_experience()` and `_build_experience()` stay
    independently testable, per the "small, separated components"
    convention already used across this project's other engines."""

    category: ExperienceCategory
    raw_summary: str
    source: str


def detect_candidate_experience(
    text: Optional[str],
    had_successful_tool_call: bool = False,
    explicit_memory_shared: bool = False,
) -> Optional[_CandidateExperience]:
    """Pure function, never raises. Returns `None` for the overwhelming
    majority of ordinary turns ("berapa 1+1?", "halo", "nyalakan lampu",
    "berapa suhu CPU?", small talk, ordinary device commands) - a match
    requires an EXPLICIT textual signal in `text` itself; `had_successful_tool_call`/
    `explicit_memory_shared` are NEVER, by themselves, sufficient (see
    module docstring's "WHAT COUNTS AS AN EXPERIENCE" section) - they only
    ever enrich `source` (provenance) on an already-detected candidate."""
    if not isinstance(text, str) or not text.strip():
        return None

    category = _classify_category(text, _tokenize(text))
    if category is None:
        return None

    provenance_bits = ["conversation"]
    if had_successful_tool_call:
        provenance_bits.append("tool_verified")
    if explicit_memory_shared:
        provenance_bits.append("explicit_user_statement")

    return _CandidateExperience(
        category=category,
        raw_summary=text.strip(),
        source="+".join(provenance_bits),
    )


def _fingerprint(category: str, summary: str) -> str:
    """Content-based, deterministic, restart-safe dedup key - section 13:
    "Do not blindly use timestamp-based uniqueness if the same event can be
    replayed." The SAME (category, normalized summary) always produces the
    SAME id, whether detected today or after a process restart, so
    replaying the exact same event twice (same turn processed twice, or the
    same accomplishment described again in near-identical words) collapses
    to one stored entry instead of two."""
    normalized = re.sub(r"\s+", " ", summary.strip().lower())
    digest = hashlib.sha256(f"{category}|{normalized}".encode("utf-8")).hexdigest()
    return digest[:16]


def _build_experience(candidate: _CandidateExperience, now: float) -> EpisodicExperience:
    summary = candidate.raw_summary[:_MAX_SUMMARY_CHARS]
    experience_id = _fingerprint(candidate.category.value, summary)
    return EpisodicExperience(
        schema_version=EPISODIC_SCHEMA_VERSION,
        experience_id=experience_id,
        timestamp=now,
        category=candidate.category.value,
        summary=summary,
        source=candidate.source,
    )


def observe_turn(
    text: Optional[str],
    had_successful_tool_call: bool = False,
    explicit_memory_shared: bool = False,
    now: Optional[float] = None,
) -> Tuple[bool, Optional[EpisodicExperience]]:
    """One call = one observed turn - the single entry point
    `main_runtime_demo.py` uses. Composes detect -> validate -> deduplicate
    -> persist, mirroring `RelationshipEngine.observe_turn()`'s own
    "thin composition of pure steps" shape. Returns
    `(is_newly_persisted_experience, entry_or_existing_entry_or_None)`:

        (False, None)   - no candidate detected this turn (the common case).
        (False, entry)  - a candidate WAS detected, but an identical
                           experience (same fingerprint) already exists -
                           this is the deduplication path, `entry` is the
                           EXISTING stored record, nothing was written.
        (True, entry)   - a genuinely NEW experience was detected, validated,
                           and persisted.

    Does not itself try/except - like `RelationshipEngine.observe_turn()`,
    the caller (`main_runtime_demo.py`) already wraps every note-producing
    call site in try/except-and-log-skip; duplicating that here would just
    hide failures from the SAME log call site the rest of that method
    uses."""
    candidate = detect_candidate_experience(text, had_successful_tool_call, explicit_memory_shared)
    if candidate is None:
        return False, None

    resolved_now = time.time() if now is None else now
    entry = _build_experience(candidate, resolved_now)

    existing_list = EpisodicMemoryStore.load()
    for existing in existing_list:
        if existing.experience_id == entry.experience_id:
            return False, existing

    existing_list.append(entry)
    if len(existing_list) > config.EPISODIC_MEMORY_MAX_ENTRIES:
        # Bounded growth (section 16/17) - oldest dropped first (FIFO).
        # Simple append + trim, no compression/consolidation - section 15
        # explicitly defers memory consolidation for this sprint.
        existing_list = existing_list[-config.EPISODIC_MEMORY_MAX_ENTRIES:]
    EpisodicMemoryStore.save(existing_list)
    return True, entry


# ─────────────────────────────────────────────
#  RETRIEVAL (registers as one more luno.memory_retrieval MemorySource)
# ─────────────────────────────────────────────


def make_episodic_experience_source(
    get_experiences: Callable[[], List[EpisodicExperience]],
) -> Callable[[QueryAnalysis, MemoryRetrievalConfig], List[RelevantMemory]]:
    """Same factory shape as every built-in source in
    `luno.memory_retrieval.sources` (`make_long_term_memory_source`,
    `make_vision_object_source`, ...): takes a zero-arg provider callable
    (application wiring binds `EpisodicMemoryStore.load` in
    `main_runtime_demo.py`, exactly like `make_long_term_memory_source
    (vm.get_long_term_memory)`), returns a closure that early-returns `[]`
    on no query signal or provider failure, and never fabricates - "no
    match, no query result." All bounding/ranking/temporal-freshness
    wording/retrieval-time-dedup already happens for free once this is
    registered via `MemoryRetriever.register_source()` - nothing here
    duplicates any of that."""

    def _source(query: QueryAnalysis, retrieval_config: MemoryRetrievalConfig) -> List[RelevantMemory]:
        if not query.has_any_signal:
            return []
        try:
            experiences = get_experiences()
        except Exception:
            return []
        if not experiences:
            return []

        results: List[RelevantMemory] = []
        for exp in experiences:
            haystack = f"{exp.category} {exp.summary}"
            if not token_overlap(query.tokens, haystack):
                continue

            text = exp.summary if exp.summary.endswith((".", "!", "?")) else exp.summary + "."
            text = f"Shared experience with the user: {text}"

            # Same rough score scale as make_long_term_memory_source (0.5
            # base) - a small bonus for turns that explicitly reference time
            # ("kemarin kita benerin apa?") since that is exactly the kind
            # of query episodic memory exists to answer.
            score = 0.5 + (0.1 if query.mentions_time else 0.0)

            timestamp = None
            if exp.timestamp:
                try:
                    timestamp = datetime.fromtimestamp(exp.timestamp, tz=timezone.utc)
                except (OverflowError, OSError, ValueError):
                    timestamp = None

            results.append(RelevantMemory(
                text=text,
                source="episodic_memory",
                score=score,
                timestamp=timestamp,
                raw=exp,
            ))
        return results

    return _source
