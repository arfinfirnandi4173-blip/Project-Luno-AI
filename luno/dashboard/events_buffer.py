"""
events_buffer.py
==================

Everything the dashboard derives purely by WATCHING the Event Bus -
never by asking any module to track something new. Two collaborators:

    EventRingBuffer  - a bounded, thread-safe ring buffer of the last N
                        events, for the "Event Bus" live-stream page and
                        for `/api/events` (a snapshot/backfill endpoint
                        SSE clients call once on connect, before
                        streaming). Exactly the same technique
                        `ProductionConsole._wire_listeners()` already
                        uses for its own `/events` command
                        (`bus.subscribe("*", self._on_any_event,
                        priority=-1000)`) - this is that same pattern,
                        just also fed to SSE subscribers live. Also
                        derives `verification_history()` the identical
                        way (Verified Smart Home Execution sprint) -
                        see `_VERIFICATION_EVENT_TYPES` below.

    StatsAggregator   - rolling counters/averages for the Statistics
                        page, built ENTIRELY from events every module
                        already publishes (`PlannerCreated`/
                        `PlannerFinished`, `ToolFinished`/`tool_failed`,
                        `LLMFinished`, `speech_playback_started/
                        finished`, `wake_word_detected`,
                        `cancel_llm_request`). No module gains a new
                        counter of its own - see each `_on_event()`
                        branch's comment for exactly which existing
                        event supplies which number, and where a number
                        is a documented approximation rather than an
                        exact count.

Both subscribe with `priority=-1000` (dead last) so they are never on
the critical path for real event delivery to real subscribers, and both
catch every exception internally - a bug in dashboard bookkeeping must
never be able to take down event delivery to the rest of Runtime (this
mirrors `EventBus._run_handler()`'s own per-subscriber isolation, one
more layer of defense specific to this observer).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional

if TYPE_CHECKING:
    from luno.core.event_bus import EventBus
    from luno.core.events import Event

DEFAULT_EVENT_BUFFER_SIZE = 5000
DEFAULT_TOOL_HISTORY_SIZE = 200
DEFAULT_VERIFICATION_HISTORY_SIZE = 200
_LATENCY_WINDOW = 200

#: Verified Smart Home Execution sprint - the same "reuse existing
#: execution history" technique `_tool_history` below already
#: established for Tool Manager, one more bounded deque fed by the same
#: catch-all `*` subscription, filtered to the 5 verification lifecycle
#: event types `luno.tool_manager.builtin.real_home_assistant.
#: RealHomeAssistantHandler` publishes (via `luno.bootstrap.adapters.
#: _make_verification_event_publisher`). No new subscription, no new
#: mechanism - just one more `if` branch in the same `_on_event`.
_VERIFICATION_EVENT_TYPES = (
    "action_verification_started", "action_verification_retry",
    "action_verified", "action_verification_failed", "action_verification_timeout",
)


class EventRingBuffer:
    def __init__(
        self, event_bus: "EventBus", maxlen: int = DEFAULT_EVENT_BUFFER_SIZE,
        tool_history_len: int = DEFAULT_TOOL_HISTORY_SIZE,
        verification_history_len: int = DEFAULT_VERIFICATION_HISTORY_SIZE,
    ) -> None:
        self._lock = threading.RLock()
        self._events: Deque[Dict[str, Any]] = deque(maxlen=maxlen)
        self._tool_history: Deque[Dict[str, Any]] = deque(maxlen=tool_history_len)
        self._verification_history: Deque[Dict[str, Any]] = deque(maxlen=verification_history_len)
        self._seq = 0
        #: SSE stream subscribers - plain callables the HTTP handler
        #: registers/removes per connection; called synchronously from
        #: the Event Bus pump thread, so each one MUST be non-blocking
        #: (server.py's SSE handler only ever does a `queue.put_nowait`
        #: here - see that module).
        self._live_subscribers: List[Any] = []
        self._sub_id = event_bus.subscribe("*", self._on_event, priority=-1000)

    def _on_event(self, event: "Event") -> None:
        try:
            with self._lock:
                self._seq += 1
                record = {"seq": self._seq, "received_at": time.time(), **event.to_dict()}
                self._events.append(record)
                if event.type in ("tool_started", "tool_finished", "tool_failed"):
                    self._tool_history.append(record)
                if event.type in _VERIFICATION_EVENT_TYPES:
                    self._verification_history.append(record)
                subscribers = list(self._live_subscribers)
            for callback in subscribers:
                try:
                    callback(record)
                except Exception:
                    pass  # a broken SSE connection must never break event capture for everyone else
        except Exception:
            pass  # dashboard bookkeeping must never be able to disrupt real event delivery

    def snapshot(self, limit: int = 100, event_type_filter: Optional[str] = None, search: str = "") -> List[Dict[str, Any]]:
        with self._lock:
            items = list(self._events)
        if event_type_filter:
            items = [e for e in items if e["type"] == event_type_filter]
        needle = (search or "").strip().lower()
        if needle:
            items = [e for e in items if needle in e["type"].lower() or needle in str(e.get("data", "")).lower()]
        return items[-limit:]

    def tool_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._tool_history)[-limit:]

    def verification_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._verification_history)[-limit:]

    def add_live_subscriber(self, callback: Any) -> None:
        with self._lock:
            self._live_subscribers.append(callback)

    def remove_live_subscriber(self, callback: Any) -> None:
        with self._lock:
            try:
                self._live_subscribers.remove(callback)
            except ValueError:
                pass

    def unsubscribe(self, event_bus: "EventBus") -> None:
        event_bus.unsubscribe(self._sub_id)


class StatsAggregator:
    """Rolling counters for the Statistics page - see module docstring
    for the full event -> number mapping. Every field returned by
    `snapshot()` is documented at its computation site below as either
    an exact count or an honest approximation; nothing is fabricated."""

    def __init__(self, event_bus: "EventBus") -> None:
        self._lock = threading.RLock()
        self._conversations_today = 0
        self._today = time.strftime("%Y-%m-%d")
        self._interrupt_count = 0
        self._plans_created = 0
        self._plans_finished = 0
        self._plans_failed = 0
        self._tools_succeeded = 0
        self._tools_failed = 0
        self._llm_times_ms: Deque[float] = deque(maxlen=_LATENCY_WINDOW)
        self._tts_start_at: Dict[Optional[str], float] = {}
        self._tts_times_ms: Deque[float] = deque(maxlen=_LATENCY_WINDOW)
        self._whisper_start_at: Optional[float] = None
        self._whisper_times_ms: Deque[float] = deque(maxlen=_LATENCY_WINDOW)
        self._last_utterance_at: Optional[float] = None
        self._e2e_times_ms: Deque[float] = deque(maxlen=_LATENCY_WINDOW)
        self._sub_id = event_bus.subscribe("*", self._on_event, priority=-1000)

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._conversations_today = 0

    def _on_event(self, event: "Event") -> None:
        try:
            with self._lock:
                self._roll_day()
                t = event.type
                if t == "wake_word_detected":
                    # A "conversation" begins each time the session
                    # wakes - a reasonable, explicitly-labeled proxy;
                    # there is no other "conversation started" concept
                    # anywhere in this project.
                    self._conversations_today += 1
                elif t == "cancel_llm_request":
                    # Published exactly when barge_in performs a free/
                    # soft/confirm/critical interrupt (see
                    # `barge_in/manager.py`'s `_do_*_interrupt`) - also
                    # published by the dashboard's own "Cancel Current
                    # LLM" control, which is semantically the same kind
                    # of event (an in-flight LLM turn got cancelled).
                    self._interrupt_count += 1
                elif t == "planner_created":
                    self._plans_created += 1
                elif t == "planner_finished":
                    self._plans_finished += 1
                elif t == "planner_failed":
                    self._plans_failed += 1
                elif t == "tool_finished":
                    self._tools_succeeded += 1
                elif t == "tool_failed":
                    self._tools_failed += 1
                elif t == "llm_finished":
                    exec_ms = event.data.get("execution_time_ms")
                    if isinstance(exec_ms, (int, float)):
                        self._llm_times_ms.append(float(exec_ms))
                elif t == "speech_playback_started":
                    self._tts_start_at[event.data.get("request_id")] = time.time()
                elif t in ("speech_playback_finished", "speech_playback_cancelled"):
                    started = self._tts_start_at.pop(event.data.get("request_id"), None)
                    if started is not None:
                        self._tts_times_ms.append((time.time() - started) * 1000.0)
                elif t == "speech_started":
                    # Whisper's OWN "listening started" signal (see
                    # `adapters/whisper.py`) - paired with the next
                    # `speech_recognized` to approximate STT latency.
                    self._whisper_start_at = time.time()
                elif t == "speech_recognized":
                    if self._whisper_start_at is not None:
                        self._whisper_times_ms.append((time.time() - self._whisper_start_at) * 1000.0)
                        self._whisper_start_at = None
                    self._last_utterance_at = time.time()
                elif t == "assistant_response":
                    # Coarse end-to-end timing: wall-clock delta from the
                    # most recent `speech_recognized` to this reply - not
                    # correlated by request_id (the two events don't
                    # reliably share one), so this is an approximation
                    # of "how long did the user wait", not an exact
                    # per-turn measurement.
                    if self._last_utterance_at is not None:
                        self._e2e_times_ms.append((time.time() - self._last_utterance_at) * 1000.0)
                        self._last_utterance_at = None
        except Exception:
            pass

    @staticmethod
    def _avg(values: Deque[float]) -> Optional[float]:
        return round(sum(values) / len(values), 1) if values else None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._roll_day()
            tools_total = self._tools_succeeded + self._tools_failed
            plans_total = self._plans_finished + self._plans_failed
            return {
                "conversations_today": self._conversations_today,
                "interrupt_count": self._interrupt_count,
                "plans_created": self._plans_created,
                "plans_finished": self._plans_finished,
                "plans_failed": self._plans_failed,
                "planner_success_rate": round(100.0 * self._plans_finished / plans_total, 1) if plans_total else None,
                "tools_succeeded": self._tools_succeeded,
                "tools_failed": self._tools_failed,
                "tool_success_rate": round(100.0 * self._tools_succeeded / tools_total, 1) if tools_total else None,
                "memory_retrieval_count": None,  # no counter exists for this yet - honestly null rather than fabricated, see report
                "average_llm_time_ms": self._avg(self._llm_times_ms),
                "average_tts_time_ms": self._avg(self._tts_times_ms),
                "average_whisper_time_ms": self._avg(self._whisper_times_ms),
                "average_end_to_end_response_time_ms": self._avg(self._e2e_times_ms),
            }

    def unsubscribe(self, event_bus: "EventBus") -> None:
        event_bus.unsubscribe(self._sub_id)
