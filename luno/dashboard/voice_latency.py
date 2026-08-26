"""
voice_latency.py
==================

Luno Brain Debugger - Memory & Voice Observability Dashboard sprint,
Phase 6/7 (Voice Pipeline Observability + Latency Timeline).

Same discipline as `events_buffer.py`'s own `StatsAggregator` (see that
module's own docstring, which this one directly extends the pattern
from, not replaces): a bounded, thread-safe structure built ENTIRELY by
passively watching the Event Bus - `VoiceLatencyRecorder` subscribes
`"*"` at `priority=-1000` (dead last, never on the critical path for
real event delivery) and reads only `data["request_id"]`/timestamps off
events every adapter in this project ALREADY publishes
(`NeedLLMResponse`/`LLMStarted`/`LLMStreaming`/`LLMChunk`/`LLMFinished`/
`AssistantResponse`/`SpeakRequest`/`SpeakStreamChunk`/
`SpeechPlaybackStarted`/`SpeechPlaybackFinished`/`SpeechPlaybackCancelled`/
`SpeechPlaybackPaused`/`SpeechPlaybackResumed` - see `luno/adapters/
events.py` and `luno/adapters/openrouter.py`/`fish_audio.py`'s own real
`self.publish(...)` call sites, verified against the actual source, not
assumed). No adapter is modified to add this - every timestamp captured
here is "when did this ALREADY-PUBLISHED event cross the bus", not a new
measurement point inside TTS/LLM code.

Derived intervals (`llm_latency_ms`, `first_audio_latency_ms`, ...) are
plain subtraction between two timestamps this recorder already captured
- the exact same arithmetic `StatsAggregator._on_event()` already does
for `average_tts_time_ms` (`(time.time() - started) * 1000.0`), not a
second computation of anything ranking/retrieval/response-selection
decided. This module never re-runs synthesis, never re-plays audio,
never blocks a real event handler, and never raises out of its own
subscriber (every exception is swallowed, exactly like `events_buffer.py`'s
own `_on_event()` methods) - a bug in this file can never break a real
conversation turn.

Fine-grained PER-CHUNK timing (synthesis time for chunk 2, inter-chunk
gap, ...) is deliberately NOT captured by this class - that granularity
already exists, fully computed, in `luno/adapters/fish_audio.py`'s own
structured `log()` lines (`ChunkAudioStart`/`ChunkFinished`/...,
`chunk_synthesis_time_s=`/`total_s=`/`playback_s=` already embedded as
key=value pairs) and the dashboard's existing `LogCapture` already
captures and filters those by `request_id` (see `/api/logs?request_id=`,
already built, already tested). Re-deriving the SAME numbers a second way
here would be exactly the "recompute instead of reuse" anti-pattern this
sprint's own Phase 9 forbids - `parse_chunk_timeline_from_logs()` below
instead PARSES those already-structured, already-computed numbers out of
the log text (regex over `key=value` tokens the log lines already
contain), never resynthesizes or re-times anything.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Optional

if TYPE_CHECKING:
    from luno.core.event_bus import EventBus
    from luno.core.events import Event

DEFAULT_VOICE_TIMELINE_MAX = 200

#: Event types this recorder listens for - every one independently
#: confirmed (Phase 0 reconnaissance) to be genuinely published by the
#: real, non-mock adapters, not just defined in `events.py`.
_LLM_EVENTS = frozenset({"need_llm_response", "llm_started", "llm_streaming", "llm_chunk", "llm_finished", "llm_error", "llm_cancelled", "assistant_response"})
_VOICE_EVENTS = frozenset({"speak_request", "speak_stream_chunk", "speech_playback_started", "speech_playback_finished", "speech_playback_cancelled", "speech_playback_paused", "speech_playback_resumed"})


def _ms(delta: Optional[float]) -> Optional[float]:
    return round(delta * 1000.0, 1) if delta is not None else None


class VoiceLatencyRecorder:
    """Bounded, thread-safe, per-`request_id` voice-pipeline timeline.
    Mirrors `events_buffer.EventRingBuffer`'s own bounded-dict-plus-
    eviction-order technique (that class bounds a `deque` of whole event
    records; this one bounds a `dict` of per-request_id timelines, using
    a parallel `deque` of ids purely to know which dict entry to evict
    next - the two are always kept in sync inside the same lock)."""

    def __init__(self, event_bus: "EventBus", maxlen: int = DEFAULT_VOICE_TIMELINE_MAX) -> None:
        self._lock = threading.RLock()
        self._maxlen = maxlen
        self._order: Deque[str] = deque()
        self._timelines: Dict[str, Dict[str, Any]] = {}
        self._sub_id = event_bus.subscribe("*", self._on_event, priority=-1000)

    def _get_or_create(self, request_id: str) -> Dict[str, Any]:
        entry = self._timelines.get(request_id)
        if entry is not None:
            return entry
        if len(self._order) >= self._maxlen:
            oldest = self._order.popleft()
            self._timelines.pop(oldest, None)
        entry = {
            "request_id": request_id,
            "conversation_id": None,
            "streaming_enabled": None,
            "pipelined_playback_enabled": None,
            "cancelled": False,
            "pause_events": [],
            "resume_events": [],
            "chunk_count": None,
        }
        self._timelines[request_id] = entry
        self._order.append(request_id)
        return entry

    def _on_event(self, event: "Event") -> None:
        try:
            t = event.type
            if t not in _LLM_EVENTS and t not in _VOICE_EVENTS:
                return
            data = event.data or {}
            request_id = data.get("request_id")
            if not request_id:
                return
            now = time.time()
            with self._lock:
                entry = self._get_or_create(request_id)
                if data.get("conversation_id") is not None:
                    entry["conversation_id"] = data.get("conversation_id")

                if t == "need_llm_response":
                    entry["t_submitted"] = entry.get("t_submitted", now)
                    entry["streaming_enabled"] = bool(data.get("stream", True))
                elif t == "llm_started":
                    entry.setdefault("t_llm_started", now)
                elif t == "llm_streaming":
                    entry.setdefault("t_llm_streaming", now)
                elif t == "llm_chunk":
                    entry.setdefault("t_first_chunk", now)
                    idx = data.get("index")
                    if isinstance(idx, int):
                        entry["llm_chunk_count"] = max(entry.get("llm_chunk_count") or 0, idx)
                elif t == "llm_finished":
                    entry.setdefault("t_llm_finished", now)
                    exec_ms = data.get("execution_time_ms")
                    if isinstance(exec_ms, (int, float)):
                        entry["llm_execution_time_ms"] = float(exec_ms)
                elif t in ("llm_error", "llm_cancelled"):
                    entry.setdefault("t_llm_finished", now)
                    entry["llm_failed"] = True
                elif t == "assistant_response":
                    entry.setdefault("t_assistant_response", now)
                elif t == "speak_request":
                    entry.setdefault("t_speak_request", now)
                    entry["pipelined_playback_enabled"] = bool(data.get("chunks"))
                elif t == "speak_stream_chunk":
                    entry.setdefault("t_speak_request", now)
                    entry["pipelined_playback_enabled"] = True
                    chunk = data.get("chunk") or {}
                    seq = chunk.get("sequence")
                    if isinstance(seq, int):
                        entry["chunk_count"] = max(entry.get("chunk_count") or 0, seq + 1)
                elif t == "speech_playback_started":
                    entry.setdefault("t_first_audio", now)
                elif t == "speech_playback_finished":
                    entry.setdefault("t_playback_end", now)
                elif t == "speech_playback_cancelled":
                    entry.setdefault("t_playback_end", now)
                    entry["cancelled"] = True
                elif t == "speech_playback_paused":
                    entry["pause_events"].append(now)
                elif t == "speech_playback_resumed":
                    entry["resume_events"].append(now)
        except Exception:
            pass  # a bug in observability must never break a real conversation turn

    def snapshot_for(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Returns the raw captured timestamps PLUS derived, honestly-
        labeled millisecond intervals - `None` for any interval whose two
        endpoints weren't both captured (e.g. a turn with no voice output
        at all has no `first_audio_latency_ms`), never a fabricated 0."""
        with self._lock:
            entry = self._timelines.get(request_id)
            if entry is None:
                return None
            e = dict(entry)  # shallow copy - safe to compute on outside the lock

        t_submitted = e.get("t_submitted")
        t_first_chunk = e.get("t_first_chunk")
        t_assistant_response = e.get("t_assistant_response")
        t_first_audio = e.get("t_first_audio")
        t_playback_end = e.get("t_playback_end")

        e["llm_first_token_latency_ms"] = _ms(t_first_chunk - t_submitted) if (t_submitted and t_first_chunk) else None
        e["llm_total_latency_ms"] = _ms(t_assistant_response - t_submitted) if (t_submitted and t_assistant_response) else (
            _ms(e.get("t_llm_finished", 0) - t_submitted) if (t_submitted and e.get("t_llm_finished")) else None
        )
        e["first_audio_latency_ms"] = _ms(t_first_audio - t_submitted) if (t_submitted and t_first_audio) else None
        e["playback_duration_ms"] = _ms(t_playback_end - t_first_audio) if (t_first_audio and t_playback_end) else None
        e["total_turn_latency_ms"] = _ms(t_playback_end - t_submitted) if (t_submitted and t_playback_end) else None
        e["pause_count"] = len(e.get("pause_events") or [])
        e["resume_count"] = len(e.get("resume_events") or [])
        return e

    def list_recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            ids = list(self._order)[-limit:]
        return [self.snapshot_for(rid) for rid in reversed(ids) if rid in self._timelines]

    def unsubscribe(self, event_bus: "EventBus") -> None:
        event_bus.unsubscribe(self._sub_id)


# ─────────────────────────────────────────────
# Per-chunk detail - parsed from already-structured log() lines, never
# re-timed. See module docstring for why this lives here instead of a
# second measurement point inside `fish_audio.py`.
# ─────────────────────────────────────────────

_KV_RE = re.compile(r"(\w+)=([^\s]+)")
_CHUNK_MESSAGE_TYPES = ("SpeechStarted", "ChunkAudioStart", "ChunkFinished", "SpeechCancelled", "SpeechFinished", "ChunkRetry", "ChunkSkipped", "SpeechError")


def _parse_kv(message: str) -> Dict[str, str]:
    return {k: v for k, v in _KV_RE.findall(message)}


def parse_chunk_timeline_from_logs(log_entries: List[Dict[str, Any]], request_id: str) -> List[Dict[str, Any]]:
    """Turns `LogCapture.snapshot(request_id=...)`'s own already-filtered,
    already-structured log records into a per-chunk timeline - PARSING
    numbers `FishAudioAdapter` already computed and logged
    (`chunk_synthesis_time_s`, `total_s`, `playback_s`), never re-timing
    anything. Inter-chunk gap is the one genuinely DERIVED value here -
    the wall-clock delta between consecutive `ChunkAudioStart` log
    records' own `wall_time` (already captured by `LogCapture` when the
    line was printed), the same "subtract two already-captured
    timestamps" arithmetic `VoiceLatencyRecorder` above already uses, not
    a new timer."""
    events: List[Dict[str, Any]] = []
    for record in log_entries:
        message = record.get("message") or record.get("raw") or ""
        kind = message.split(" ", 1)[0] if message else ""
        if kind not in _CHUNK_MESSAGE_TYPES:
            continue
        if record.get("request_id") != request_id:
            continue
        kv = _parse_kv(message)
        events.append({"kind": kind, "wall_time": record.get("wall_time"), **kv})

    chunk_starts = [e for e in events if e["kind"] == "ChunkAudioStart"]
    chunks: List[Dict[str, Any]] = []
    prev_start_wall_time: Optional[float] = None
    for start in chunk_starts:
        idx = start.get("chunk_index")
        finish = next((e for e in events if e["kind"] == "ChunkFinished" and e.get("chunk_index") == idx), None)
        gap_ms = None
        if prev_start_wall_time is not None and isinstance(start.get("wall_time"), (int, float)):
            gap_ms = round((start["wall_time"] - prev_start_wall_time) * 1000.0, 1)
        if isinstance(start.get("wall_time"), (int, float)):
            prev_start_wall_time = start["wall_time"]
        chunks.append({
            "chunk_index": idx,
            "chunk_synthesis_time_s": start.get("chunk_synthesis_time_s"),
            "playback_s": finish.get("playback_s") if finish else None,
            "total_s": finish.get("total_s") if finish else None,
            "gap_before_ms": gap_ms,
        })
    return chunks
