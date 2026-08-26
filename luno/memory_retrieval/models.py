"""
models.py
=========

Typed data shared across this package - `RelevantMemory` (the one output
shape every source produces and `retrieve_memories()` returns),
`MemoryRetrievalConfig` (env-only, reloadable, same convention as
`WakeSessionConfig`/`BargeInConfig`), and `QueryAnalysis` (the one input
shape `query.analyze_query()` produces and every source consumes - so a
source never has to re-tokenize the user's text itself).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional


@dataclass
class RelevantMemory:
    """One retrieved memory, already rendered to a human-readable sentence
    - the ONLY thing `prompt.build_memory_prompt_block()` and the final
    LLM prompt ever see. `source`/`score`/`raw` exist for ranking, logging,
    and debugging - never rendered into the prompt themselves.

    `stale` is set by `retriever.MemoryRetriever` (not by individual
    sources) once it knows the configured staleness threshold - sources
    just report their own `timestamp` honestly and let the retriever
    decide what counts as old."""

    text: str
    source: str
    score: float
    timestamp: Optional[datetime] = None
    stale: bool = False
    #: the original source record (TrackedObject, LongTermMemoryRecord,
    #: etc.) - purely for introspection/debugging/tests, NEVER serialized
    #: into the prompt.
    raw: Any = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "source": self.source,
            "score": round(self.score, 4),
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "stale": self.stale,
        }


@dataclass
class QueryAnalysis:
    """Output of `query.analyze_query(user_text)` - computed ONCE per
    `retrieve_memories()` call and handed to every registered source, so
    the same tokenization/classification is never redone per-source and
    can never silently drift between sources."""

    raw_text: str
    normalized: str
    tokens: List[str] = field(default_factory=list)
    #: True for turns that are clearly ABOUT the user themselves
    #: ("am I ...", "what am I doing", "how do I look") rather than about
    #: an object/location in the room.
    is_self_query: bool = False
    #: True if the text contains an explicit time reference ("yesterday",
    #: "this morning", "just now", "an hour ago", ...) - available for
    #: sources that want to reason about time windows; the built-in
    #: sources mainly use plain recency/staleness instead of hard time
    #: filtering, since "keyword matching is acceptable initially" per
    #: the spec and hard time-window filtering is easy to get wrong.
    mentions_time: bool = False
    #: False for turns with no object/location/self/event signal at all
    #: (e.g. "what's 5 + 5?") - sources should treat this as "don't even
    #: query the underlying store", not just "return nothing after
    #: querying" - see the spec's "Vision Memory should not be queried
    #: unnecessarily."
    has_any_signal: bool = False

    def contains_word(self, word: str) -> bool:
        return word.strip().lower() in self.tokens


def _split_words(raw: str) -> List[str]:
    return [w.strip() for w in raw.split(",") if w.strip()]


@dataclass
class MemoryRetrievalConfig:
    """Every knob Sprint 5 calls out, env-var only, `from_env()` is the
    only supported way to build a non-default one - mirrors
    `WakeSessionConfig.from_env()`/`BargeInConfig.from_env()`'s pattern
    exactly (this project's established convention for per-package,
    reloadable configuration)."""

    enabled: bool = True
    max_results: int = 5
    max_tokens: int = 400
    stale_after_minutes: float = 30.0
    #: "keyword" today; "semantic"/"hybrid" are recognized values reserved
    #: for a future retrieval strategy swap (see `query.py`'s module
    #: docstring) - an unrecognized value falls back to "keyword" rather
    #: than raising, since this is a soft feature flag, not a hard
    #: dependency.
    retrieval_mode: str = "keyword"
    #: gates the "[MemoryRetrieval] ..." debug logs in retriever.py - off
    #: by default so normal runs stay quiet, per the spec's "these logs
    #: should only appear in debug mode."
    debug: bool = False

    @classmethod
    def from_env(cls) -> "MemoryRetrievalConfig":
        def _bool(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            return raw.strip().lower() in ("1", "true", "yes", "on")

        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return int(raw)
            except ValueError:
                return default

        def _float(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return float(raw)
            except ValueError:
                return default

        mode = os.getenv("MEMORY_RETRIEVAL_MODE", "keyword").strip().lower() or "keyword"
        if mode not in ("keyword", "semantic", "hybrid"):
            mode = "keyword"

        return cls(
            enabled=_bool("MEMORY_INJECTION_ENABLED", True),
            max_results=_int("MAX_MEMORY_RESULTS", 5),
            max_tokens=_int("MAX_MEMORY_TOKENS", 400),
            stale_after_minutes=_float("MEMORY_STALE_AFTER_MINUTES", 30.0),
            retrieval_mode=mode,
            debug=_bool("MEMORY_RETRIEVAL_DEBUG", False),
        )
