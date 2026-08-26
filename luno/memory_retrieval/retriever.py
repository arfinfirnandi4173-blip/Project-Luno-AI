"""
retriever.py
============

`MemoryRetriever` - the "Memory Retriever" stage from the spec's desired
flow (`Context Builder -> Memory Retriever -> Relevant Memories -> Prompt
Builder -> OpenRouter`). Owns a registry of named, independently
enable-able `MemorySource` callables (see `sources.py`) and exposes
exactly one clean entry point:

    retrieve_memories(user_text) -> List[RelevantMemory]

Callers (Context Builder, `main_runtime_demo.py`'s prompt assembly, or
anything else) never need to know HOW any source works internally - only
that this method returns an already-ranked, already-limited,
already-staleness-annotated list, or an empty list when nothing is
relevant. Swapping `MemoryRetrievalConfig.retrieval_mode` from "keyword"
to a future "semantic"/"hybrid" strategy only changes what
`query.analyze_query()` (or its future sibling) produces - this class's
own ranking/limiting/staleness logic is retrieval-strategy-agnostic and
does not need to change.

Thread safety: `register_source`/`set_source_enabled`/`reload_config` and
`retrieve_memories` all take the same lock around the registry snapshot
they read/write, so concurrent registration and concurrent retrieval from
multiple threads never race on the underlying dict.
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Any, Dict, List, Optional, Tuple

from .models import MemoryRetrievalConfig, RelevantMemory
from .query import analyze_query
from .sources import MemorySource
from .utils import log, utcnow


def _format_age(age: timedelta) -> str:
    total_seconds = max(0.0, age.total_seconds())
    if total_seconds < 60:
        return "less than a minute"
    minutes = int(total_seconds // 60)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = int(hours // 24)
    return f"{days} day{'s' if days != 1 else ''}"


class MemoryRetriever:
    def __init__(self, config: Optional[MemoryRetrievalConfig] = None) -> None:
        self.config = config or MemoryRetrievalConfig.from_env()
        self._sources: Dict[str, MemorySource] = {}
        self._enabled: Dict[str, bool] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Registration / configuration
    # ------------------------------------------------------------------

    def register_source(self, name: str, source: MemorySource, enabled: bool = True) -> None:
        """Register (or replace) a named source. Each source is optional -
        `enabled=False` keeps it registered but skipped, so a caller can
        toggle sources on/off at runtime without re-registering them."""
        with self._lock:
            self._sources[name] = source
            self._enabled[name] = enabled

    def unregister_source(self, name: str) -> None:
        with self._lock:
            self._sources.pop(name, None)
            self._enabled.pop(name, None)

    def set_source_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            if name in self._sources:
                self._enabled[name] = enabled

    def registered_sources(self) -> Dict[str, bool]:
        """Diagnostic snapshot: {name: enabled} for every registered
        source - used by tests and debug commands, never by the
        retrieval logic itself."""
        with self._lock:
            return dict(self._enabled)

    def reload_config(self, config: Optional[MemoryRetrievalConfig] = None) -> None:
        """Swap in a new config (or re-read from the environment) without
        losing registered sources - matches `WakeSessionConfig`/
        `BargeInConfig`'s existing "/reload picks up new config without
        restart" convention elsewhere in this project."""
        with self._lock:
            self.config = config or MemoryRetrievalConfig.from_env()

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def retrieve_memories(self, user_text: str) -> List[RelevantMemory]:
        with self._lock:
            config = self.config
            sources_snapshot: List[Tuple[str, MemorySource]] = [
                (name, fn) for name, fn in self._sources.items() if self._enabled.get(name, True)
            ]

        if not config.enabled:
            return []

        query = analyze_query(user_text)
        log(f"Memory retrieval started - query={user_text!r}", config.debug)

        if not query.has_any_signal:
            # Spec: "Vision Memory should not be queried unnecessarily" -
            # applied generally to every source, not just vision: a turn
            # with zero retrieval signal ("what's 5 + 5?") never reaches
            # into any source's underlying store at all.
            log("No retrieval signal in query - skipping all sources", config.debug)
            return []

        candidates: List[RelevantMemory] = []
        for name, source in sources_snapshot:
            try:
                found = source(query, config) or []
            except Exception as ex:
                log(f"source '{name}' raised (skipped): {ex}", config.debug)
                continue
            log(f"Source: {name}  Query: {query.normalized!r}  Retrieved: {len(found)} memories", config.debug)
            candidates.extend(found)

        now = utcnow()
        for mem in candidates:
            self._apply_recency_and_staleness(mem, now, config)

        deduped = self._deduplicate(candidates)
        ranked = sorted(deduped, key=lambda m: m.score, reverse=True)
        limited = self._apply_limits(ranked, config)

        log(
            f"Injected: {len(limited)} memories  Estimated tokens: {self._estimate_tokens(limited)}",
            config.debug,
        )
        return limited

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _apply_recency_and_staleness(mem: RelevantMemory, now, config: MemoryRetrievalConfig) -> None:
        """Mutates `mem` in place: folds a recency bonus into `.score`
        (fresher = ranked higher, per the spec's ranking preferences) and
        rewrites `.text` with an honest freshness/staleness annotation
        instead of leaving the LLM to assume the memory is current."""
        if mem.timestamp is None:
            return

        age = now - mem.timestamp
        age_seconds = max(0.0, age.total_seconds())
        stale_after_s = max(1.0, config.stale_after_minutes) * 60.0
        decay_horizon_s = stale_after_s * 3.0
        recency_factor = max(0.0, 1.0 - (age_seconds / decay_horizon_s))
        mem.score += recency_factor * 0.3
        mem.stale = age_seconds > stale_after_s

        human_age = _format_age(age)
        base = mem.text[:-1] if mem.text.endswith(".") else mem.text
        if mem.stale:
            mem.text = f"{base}, approximately {human_age} ago - may be outdated."
        elif age_seconds < 60:
            mem.text = f"{base}. Observed moments ago."
        else:
            mem.text = f"{base}. Observed {human_age} ago."

    @staticmethod
    def _deduplicate(candidates: List[RelevantMemory]) -> List[RelevantMemory]:
        """Spec: "Duplicate memories" and "Superseded observations" rank
        lowest / should be discarded - the same (source, underlying
        object identity) appearing twice (e.g. matched by both the object
        source and an event source referencing the same object) is
        collapsed to whichever scored higher.

        Bug fix: keying purely by rendered TEXT (instead of the
        underlying object's own identity) wrongly collapsed genuinely
        DISTINCT objects that happen to render to identical wording (e.g.
        two different tracked cups both "on the desk", observed the same
        minute) down to just one result - `raw.id` (when the source
        attached one, e.g. TrackedObject's own tracked id) is the correct,
        stable de-duplication key; falling back to text only applies to
        sources whose `raw` has no identity of its own."""
        best: Dict[Tuple[str, Any], RelevantMemory] = {}
        for mem in candidates:
            raw_id = getattr(mem.raw, "id", None)
            key = (mem.source, raw_id) if raw_id is not None else (mem.source, mem.text)
            existing = best.get(key)
            if existing is None or mem.score > existing.score:
                best[key] = mem
        return list(best.values())

    @staticmethod
    def _apply_limits(ranked: List[RelevantMemory], config: MemoryRetrievalConfig) -> List[RelevantMemory]:
        capped_by_count = ranked[: max(0, config.max_results)]
        out: List[RelevantMemory] = []
        used_tokens = 0
        for mem in capped_by_count:
            est = MemoryRetriever._estimate_tokens([mem])
            if used_tokens + est > config.max_tokens:
                break
            out.append(mem)
            used_tokens += est
        return out

    @staticmethod
    def _estimate_tokens(memories: List[RelevantMemory]) -> int:
        # Rough, dependency-free estimate (~4 chars/token, a common
        # approximation) - good enough for a soft budget, not meant to
        # match any specific tokenizer exactly.
        return sum(max(1, len(m.text) // 4) for m in memories)
