"""
habit_memory.py
=================

`HabitMemory` - notices which Home Assistant devices Vinn manually turns
on/off shortly after arriving home (`human_entered`/`person_appeared`),
and promotes a REPEATED pattern into a "learned habit" only after real
repetition across multiple distinct days - never from a single
occurrence, never a guess (spec-of-the-day: "pulang kerja sore ->
otomatis nyalain AC + lampu meja" - the whole point is Luno learns this
FROM ACTUAL BEHAVIOR, not from being told once).

A promoted pattern is proposed ONCE, by voice, via the exact same
`luno.routing.ConfirmationHandler` `PlannerBridgeModule` already uses for
the browser-fallback/routing-classifier confirmations (see
`main_runtime_demo.py`'s wiring) - no new state machine, no LLM call to
phrase the question (deterministic template, same "no extra LLM call
just to ask" rule this whole project already enforces everywhere else).
It only ever becomes a live automation (`goal_generator.py::
_rule_learned_habit`) after Vinn explicitly confirms it - never auto-
executes on its own say-so.

NOT `luno.vision_memory`'s `LongTermMemoryRecord` - that promotes vision-
observed ACTIVITY DESCRIPTIONS ("Usually: Vinn started typing in the
morning."), a completely different signal. This is specifically
"presence-arrival -> which devices got manually turned on/off in the
following few minutes," tied to concrete, replayable `(action, target)`
pairs a Goal can actually execute - not a free-text sentence.

Thread-safety / Event Bus safety: `ProactiveModule.on_event()` runs
`_maybe_trigger_immediate_cycle()`/tool-result recording synchronously
on the Event Bus pump thread (see that module's own docstring) - every
method here is therefore kept to plain in-memory dict/list mutation
under one lock plus an occasional small JSON write (same "small file,
synchronous write" convention `luno/memory.py`/`luno/memory_guard.py`
already use), never anything that could meaningfully block.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .. import persistence

#: How long after an arrival trigger a manually-turned-on/off device
#: still counts as "part of the arrival routine" - long enough to cover
#: "walks in, takes off shoes, THEN turns on the AC," short enough that
#: an unrelated action an hour later never gets attributed to arrival.
ARRIVAL_WINDOW_S = 600.0  # 10 minutes

#: A pattern is promoted from "observing" to "proposed" (asked about
#: once) only after BOTH of these hold. `MIN_OCCURRENCES` mirrors
#: `vision_memory.memory.LONG_TERM_PROMOTION_COUNT`'s own "5" for
#: consistency; `MIN_DISTINCT_DAYS` is this module's own addition -
#: vision_memory only requires a time SPAN, which 5 occurrences in one
#: single evening would already satisfy. A daily arrival routine is
#: specifically about recurring ACROSS DAYS, so this requires that
#: explicitly rather than reusing span alone.
MIN_OCCURRENCES = 5
MIN_DISTINCT_DAYS = 4

#: How long a "asked, no answer yet" pattern stays parked before it's
#: made askable again (the next time a matching arrival happens) -
#: `ConfirmationHandler`'s own TTL (default 60s) governs whether a LATE
#: reply to the ORIGINAL question still counts; this is a much longer,
#: separate window governing whether Luno tries asking again at all on
#: a LATER day (never nags every single cycle - see `_revert_stale_asks`).
_ASK_RETRY_AFTER_S = 20 * 3600.0  # ~20h - effectively "try again next day"


@dataclass
class HabitPattern:
    time_bucket: str
    action: str        # "turn_on" | "turn_off"
    target: str         # device slug, e.g. "ac_kamar" - same form IntentParser._slugify() already produces
    days_seen: List[str] = field(default_factory=list)   # ISO date strings, deduped
    count: int = 0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    asked_at: Optional[str] = None
    #: observing (still counting) -> proposed (crossed threshold, not yet
    #: asked) -> asked (voice question sent, awaiting reply) -> confirmed
    #: (live automation) | declined (never ask about this one again).
    status: str = "observing"

    def key(self) -> Tuple[str, str, str]:
        return (self.time_bucket, self.action, self.target)

    def to_dict(self) -> Dict[str, object]:
        return {
            "time_bucket": self.time_bucket, "action": self.action, "target": self.target,
            "days_seen": list(self.days_seen), "count": self.count,
            "first_seen": self.first_seen, "last_seen": self.last_seen,
            "asked_at": self.asked_at, "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, object]) -> "HabitPattern":
        return cls(
            time_bucket=str(d.get("time_bucket", "")), action=str(d.get("action", "")), target=str(d.get("target", "")),
            days_seen=list(d.get("days_seen") or []), count=int(d.get("count") or 0),
            first_seen=d.get("first_seen"), last_seen=d.get("last_seen"),
            asked_at=d.get("asked_at"), status=str(d.get("status", "observing")),
        )


@dataclass
class HabitProposal:
    """Every freshly-promoted pattern for ONE `time_bucket`, bundled
    into a single voice question ("mau aku nyalain AC Kamar dan Lampu
    Meja tiap kali pulang jam segini?") rather than asking once per
    device - much less tiring for Vinn than a separate question per
    appliance."""
    time_bucket: str
    items: List[Tuple[str, str]]   # [(action, target), ...]


class HabitMemory:
    """One process-wide instance, constructed once in
    `luno/bootstrap/modules.py` and shared (by reference) between
    `ProactiveModule` (records + reads + asks) - `PlannerBridgeModule`
    deliberately does NOT get a direct reference (see that module's own
    "communicates only via Planner reference + Event Bus" convention,
    confirmed unchanged by this feature): resolving a confirm/decline
    reply publishes a `proactive_habit_resolved` event instead, which
    `ProactiveModule.on_event()` picks up and applies here."""

    def __init__(self, path: Optional[str] = None) -> None:
        if path is None:
            try:
                from .. import config as _config
                path = getattr(_config, "HABIT_MEMORY_FILE", None)
            except Exception:
                path = None
        self._path = path or os.path.join("config", "habit_memory.json")
        self._lock = threading.Lock()
        self._patterns: Dict[Tuple[str, str, str], HabitPattern] = {}
        self._window_open_until: Optional[datetime] = None
        self._window_time_bucket: Optional[str] = None
        self._load()

    # -- arrival window -------------------------------------------------------

    def open_arrival_window(self, time_bucket: str, now: datetime) -> None:
        """Called once per real arrival trigger (`human_entered`/
        `person_appeared`) - every VERIFIED home_assistant turn_on/
        turn_off recorded while this window is open counts toward this
        `time_bucket`'s pattern."""
        with self._lock:
            self._window_open_until = now + timedelta(seconds=ARRIVAL_WINDOW_S)
            self._window_time_bucket = time_bucket

    def _window_active_bucket(self, now: datetime) -> Optional[str]:
        with self._lock:
            if self._window_open_until is None or now > self._window_open_until:
                return None
            return self._window_time_bucket

    # -- recording --------------------------------------------------------------

    def record_verified_action(self, action: Optional[str], target: Optional[str], now: datetime) -> None:
        """Call ONLY for a VERIFIED (`ToolResult.success is True`)
        `home_assistant` `turn_on`/`turn_off` result - see
        `ProactiveModule.on_event()`'s `tool_finished`-only subscription
        (that event type is published ONLY on success - see
        `ToolManagerBridgeModule._process_event()` - `tool_failed`
        never reaches this method at all)."""
        if action not in ("turn_on", "turn_off") or not target:
            return
        bucket = self._window_active_bucket(now)
        if bucket is None:
            return  # outside any open arrival window - not an arrival-routine signal
        target = target.strip().lower()
        if not target:
            return
        day = now.date().isoformat()
        key = (bucket, action, target)
        with self._lock:
            pattern = self._patterns.get(key)
            if pattern is None:
                pattern = HabitPattern(time_bucket=bucket, action=action, target=target)
                self._patterns[key] = pattern
            pattern.count += 1
            pattern.last_seen = now.isoformat()
            if pattern.first_seen is None:
                pattern.first_seen = now.isoformat()
            if day not in pattern.days_seen:
                pattern.days_seen.append(day)
            if (
                pattern.status == "observing"
                and pattern.count >= MIN_OCCURRENCES
                and len(pattern.days_seen) >= MIN_DISTINCT_DAYS
            ):
                pattern.status = "proposed"
            self._save_locked()

    # -- proposing ----------------------------------------------------------------

    def pop_pending_proposal(self, time_bucket: str) -> Optional[HabitProposal]:
        """Bundles every `status == "proposed"` pattern for this
        `time_bucket` into ONE proposal and marks them `"asked"` so the
        same day's later cycles don't ask again immediately. If the
        question goes unanswered for a while (`_ASK_RETRY_AFTER_S`),
        `_revert_stale_asks` (run at the top of this method) puts them
        back to `"proposed"` so a LATER arrival tries again - never
        stuck silently forever just because one reply was missed."""
        with self._lock:
            self._revert_stale_asks_locked()
            matches = [p for p in self._patterns.values() if p.time_bucket == time_bucket and p.status == "proposed"]
            if not matches:
                return None
            now_iso = datetime.now(timezone.utc).isoformat()
            for p in matches:
                p.status = "asked"
                p.asked_at = now_iso
            self._save_locked()
            return HabitProposal(time_bucket=time_bucket, items=[(p.action, p.target) for p in matches])

    def _revert_stale_asks_locked(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_ASK_RETRY_AFTER_S)
        for p in self._patterns.values():
            if p.status != "asked" or not p.asked_at:
                continue
            try:
                asked = datetime.fromisoformat(p.asked_at)
            except ValueError:
                continue
            if asked < cutoff:
                p.status = "proposed"

    def confirm(self, items: List[Tuple[str, str]], time_bucket: str) -> None:
        with self._lock:
            for action, target in items:
                p = self._patterns.get((time_bucket, action, target))
                if p is not None:
                    p.status = "confirmed"
            self._save_locked()

    def decline(self, items: List[Tuple[str, str]], time_bucket: str) -> None:
        with self._lock:
            for action, target in items:
                p = self._patterns.get((time_bucket, action, target))
                if p is not None:
                    p.status = "declined"
            self._save_locked()

    # -- reading (for goal_generator._rule_learned_habit) --------------------------

    def active_habits_for(self, time_bucket: str) -> List[Tuple[str, str]]:
        with self._lock:
            return [
                (p.action, p.target) for p in self._patterns.values()
                if p.time_bucket == time_bucket and p.status == "confirmed"
            ]

    # -- introspection (Dashboard/tests) -------------------------------------------

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {"pattern_count": len(self._patterns), "patterns": [p.to_dict() for p in self._patterns.values()]}

    # -- persistence -------------------------------------------------------------------

    def _load(self) -> None:
        """Persistent State Hardening V2 sprint: now loaded via
        `luno.persistence.safe_load_json()` - missing/empty path or
        non-dict root falls back to `{"patterns": []}`, exactly matching
        this method's previous "missing/corrupt file = fresh start"
        behavior. The per-entry parsing loop keeps its own broad
        try/except, unchanged - a malformed individual entry must still
        never prevent the rest of `__init__` from completing."""
        data, _source = persistence.safe_load_json(
            self._path, default={"patterns": []}, validate=lambda d: isinstance(d, dict),
        )
        try:
            for entry in data.get("patterns", []):
                p = HabitPattern.from_dict(entry)
                self._patterns[p.key()] = p
        except Exception:
            pass  # missing/corrupt file = fresh start, same convention every store in this project follows

    def _save_locked(self) -> None:
        # Caller already holds self._lock. Persistent State Hardening V2
        # sprint: now written via `luno.persistence.atomic_write_json()`
        # - backup-before-write + temp-file + fsync + `os.replace()`,
        # replacing the previous naive direct write. Failure still
        # silently swallowed - persistence failure must never break
        # recording/proposing.
        if not self._path:
            return
        try:
            persistence.atomic_write_json(
                self._path, {"patterns": [p.to_dict() for p in self._patterns.values()]},
            )
        except Exception:
            pass  # persistence failure must never break recording/proposing
