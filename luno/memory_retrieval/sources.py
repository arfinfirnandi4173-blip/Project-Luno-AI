"""
sources.py
==========

Built-in `MemorySource` factories. A `MemorySource` is just a callable:

    (query: QueryAnalysis, config: MemoryRetrievalConfig) -> List[RelevantMemory]

Nothing here imports `luno.vision_memory`, `luno.planner`, or
`luno.tool_manager` directly - every factory below takes already-bound,
zero/one-arg provider callables (e.g. `vm.get_world_state`), the EXACT
same "hand in a snapshot, not a live dependency" shape `ContextBuilder`
and `PlannerContext`/`Handlers` already use elsewhere in this project.
The real binding (`make_vision_object_source(vm.get_world_state)`) happens
in application wiring code (`main_runtime_demo.py`), not in this package -
so this package stays importable and independently testable with zero
real vision/planner state.

Each source is responsible for its OWN relevance filtering - a source
that finds nothing relevant returns `[]`, it does not fall back to
"return everything" (that would defeat the entire point of retrieval-
based injection). `retriever.MemoryRetriever` only ranks/limits/marks
staleness on whatever candidates the sources already decided were
relevant; it never second-guesses a source's decision to return nothing.

Adding a brand-new source (Emotion Memory, Home Assistant Memory,
Calendar Memory, ...) never requires touching this file or
`retriever.py` - write a new factory (here, or anywhere else - it's just
a plain function) and call `retriever.register_source(name, source_fn)`.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from .models import MemoryRetrievalConfig, QueryAnalysis, RelevantMemory
from .query import token_overlap

MemorySource = Callable[[QueryAnalysis, MemoryRetrievalConfig], List[RelevantMemory]]

#: words that, if present, mean "the user is asking about themselves" -
#: also checked here (in addition to QueryAnalysis.is_self_query) for
#: object-vs-self disambiguation convenience in tests/callers that build
#: a QueryAnalysis by hand.
_ACTIVITY_WORDS = {"typing", "standing", "walking", "sitting", "sleeping", "reading"}


def make_vision_object_source(get_world_state: Callable[[], Any]) -> MemorySource:
    """Answers object/location questions ("where is my cup", "what's on
    my desk") straight from the CACHED `WorldState` - no vision model
    call, ever. A query token matching an object's `label` (e.g. "cup")
    OR its free-text `location` (e.g. "desk") both count as a match -
    that second case is what makes "what is on my desk?" correctly
    return every object located there, not just one keyed by exact
    label."""

    def _source(query: QueryAnalysis, config: MemoryRetrievalConfig) -> List[RelevantMemory]:
        if not query.has_any_signal or query.is_self_query:
            return []
        try:
            world_state = get_world_state()
        except Exception:
            return []

        objects = getattr(world_state, "objects", None) or {}
        results: List[RelevantMemory] = []
        for obj in objects.values():
            status = getattr(obj, "status", None)
            status_value = getattr(status, "value", status)
            if status_value != "present":
                continue

            label = (getattr(obj, "label", "") or "").strip()
            location = getattr(obj, "location", None) or ""
            if not label:
                continue

            label_match = token_overlap(query.tokens, label) or (label.lower() in query.normalized)
            location_match = token_overlap(query.tokens, location)
            if not (label_match or location_match):
                continue

            if location:
                text = f"{label.title()} last seen on {location}." if not location.lower().startswith(("on ", "in ", "at ", "near ", "under ", "behind ")) \
                    else f"{label.title()} last seen {location}."
            else:
                text = f"{label.title()} was last seen, but its location wasn't reported."

            # Match strength (0..1ish) * a crude "confidence" proxy from
            # the only signal TrackedObject actually carries about
            # certainty: whether it's still believed PRESENT. Recency is
            # layered on top centrally by the retriever, not here - every
            # source would otherwise have to duplicate "now" handling.
            match_strength = 0.5
            if label_match:
                match_strength += 0.35
            if location_match:
                match_strength += 0.15

            results.append(RelevantMemory(
                text=text,
                source="vision_memory",
                score=match_strength,
                timestamp=getattr(obj, "last_seen", None),
                raw=obj,
            ))
        return results

    return _source


def make_vision_human_source(get_world_state: Callable[[], Any]) -> MemorySource:
    """Answers self/user questions ("am I wearing headphones?", "what am
    I doing?") with whatever the LATEST tracked human record actually
    holds (`activity`/`pose`/`emotion`) - honestly limited to what
    `TrackedHuman` models, never fabricating detail the data model
    doesn't have (e.g. specific worn items aren't tracked - the response
    is deliberately phrased as the closest true observation available,
    matching the spec's "latest observation about the user")."""

    def _source(query: QueryAnalysis, config: MemoryRetrievalConfig) -> List[RelevantMemory]:
        if not query.is_self_query:
            return []
        try:
            world_state = get_world_state()
        except Exception:
            return []

        humans = getattr(world_state, "humans", None) or {}
        if not humans:
            return []

        latest = max(humans.values(), key=lambda h: getattr(h, "last_seen", None) or 0)
        parts: List[str] = []
        activity = getattr(latest, "activity", None)
        activity_value = getattr(activity, "value", activity)
        if activity_value and activity_value != "unknown":
            parts.append(f"you were {activity_value.replace('_', ' ')}")
        if getattr(latest, "pose", None):
            parts.append(f"pose was {latest.pose}")
        if getattr(latest, "emotion", None):
            parts.append(f"mood seemed {latest.emotion}")

        if not parts:
            return []

        text = "Latest observation about you: " + ", ".join(parts) + "."
        return [RelevantMemory(
            text=text, source="vision_memory", score=0.85,
            timestamp=getattr(latest, "last_seen", None), raw=latest,
        )]

    return _source


def make_vision_event_source(get_recent_events: Callable[..., Any], limit: int = 20) -> MemorySource:
    """Lower-priority source over recent, already-scored `EventRecord`s
    (e.g. "did anyone come in?", "what happened earlier?") - keyword
    overlap against each event's own description, same discipline as the
    object source: no match, no query result, never a live vision call."""

    def _source(query: QueryAnalysis, config: MemoryRetrievalConfig) -> List[RelevantMemory]:
        if not query.has_any_signal:
            return []
        try:
            events = get_recent_events(limit=limit)
        except Exception:
            return []

        results: List[RelevantMemory] = []
        for ev in events or []:
            description = getattr(ev, "description", "") or ""
            if not token_overlap(query.tokens, description):
                continue
            text = description if description.endswith(".") else description + "."
            importance = getattr(ev, "importance", 3) or 3
            results.append(RelevantMemory(
                text=text, source="vision_memory_events",
                score=0.25 + min(0.2, importance / 50.0),
                timestamp=getattr(ev, "timestamp", None), raw=ev,
            ))
        return results

    return _source


def make_long_term_memory_source(get_long_term_memory: Callable[[], Any]) -> MemorySource:
    """Matches the user's long-term memory statements (habits/patterns
    already promoted by Vision Memory's own logic) against the query -
    `LongTermMemoryRecord.confidence` is used directly as this source's
    confidence factor, since that field already means exactly that."""

    def _source(query: QueryAnalysis, config: MemoryRetrievalConfig) -> List[RelevantMemory]:
        if not query.has_any_signal:
            return []
        try:
            records = get_long_term_memory()
        except Exception:
            return []

        results: List[RelevantMemory] = []
        for rec in records or []:
            statement = getattr(rec, "statement", "") or ""
            if not token_overlap(query.tokens, statement):
                continue
            text = statement if statement.endswith(".") else statement + "."
            confidence = getattr(rec, "confidence", 0.5) or 0.5
            results.append(RelevantMemory(
                text=text, source="long_term_memory",
                score=0.5 * max(0.1, min(1.0, confidence)),
                timestamp=getattr(rec, "updated_at", None), raw=rec,
            ))
        return results

    return _source


def make_planner_state_source(get_planner_state: Callable[[], Any]) -> MemorySource:
    """Optional, minimal source over whatever `planner_state` snapshot the
    caller provides (e.g. `{"last_plan_id": "..."}`) - only surfaces
    anything for turns that plausibly ask about recent actions/tasks, so
    it stays silent on ordinary conversation the way every other source
    does."""

    # Checked against the RAW normalized text, not `query.tokens` - "did"
    # (as in "what did you just do?") is a generic stopword `query.py`
    # already strips for every other source, so it never survives into
    # `query.tokens`; this source specifically cares about it, so it
    # looks at the untouched text instead.
    _RELEVANT_WORDS = ("plan", "planning", "doing", "task", "action", "did")

    def _source(query: QueryAnalysis, config: MemoryRetrievalConfig) -> List[RelevantMemory]:
        if not any(w in query.normalized for w in _RELEVANT_WORDS):
            return []
        try:
            state = get_planner_state()
        except Exception:
            return []
        if not state:
            return []
        last_plan_id = state.get("last_plan_id") if isinstance(state, dict) else None
        if not last_plan_id:
            return []
        return [RelevantMemory(
            text=f"Last plan executed: {last_plan_id}.",
            source="planner_state", score=0.3, raw=state,
        )]

    return _source


def make_tool_execution_source(get_tool_results: Callable[[], Any]) -> MemorySource:
    """Optional source over whatever tool-execution results the caller
    provides - each result is expected to already carry a short,
    human-readable summary (e.g. `{"summary": "turned on the RGB
    strip"}`); anything without one is skipped rather than guessed at."""

    def _source(query: QueryAnalysis, config: MemoryRetrievalConfig) -> List[RelevantMemory]:
        try:
            results = get_tool_results()
        except Exception:
            return []
        if not results:
            return []

        out: List[RelevantMemory] = []
        for r in results:
            text = r.get("summary") if isinstance(r, dict) else None
            if not text:
                continue
            if query.tokens and not token_overlap(query.tokens, text):
                continue
            rendered = text if text.endswith(".") else text + "."
            out.append(RelevantMemory(text=rendered, source="tool_execution", score=0.3, raw=r))
        return out

    return _source
