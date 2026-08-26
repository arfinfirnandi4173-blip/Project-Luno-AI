"""
test_tts_e2e_pipeline.py
==========================

TTS Chunk Queue & Cancellation sprint - Phase 10 (End-to-end).

Three explicit, real-pipeline scenarios required by the sprint brief,
each run through the ACTUAL production wiring (`main_runtime_demo.py`'s
`RuntimeDemoConsole`: wake session, barge-in, `SessionManagerModule`,
`FishAudioAdapter`) with only the TTS/network boundary faked
(`MockFishAudioClient`) and the LLM boundary faked (`MockOpenRouterClient`)
- exactly the convention already used by `tests/test_barge_in_console.py`
and the barge-in section of `tests/test_tts_cancellation.py`.

These scenarios are intentionally coarser/more end-to-end than the
focused unit-style scenarios in `test_tts_chunking.py`/`test_tts_queue.py`/
`test_tts_cancellation.py` (which already individually cover ordering,
cancellation races, and barge-in state resets in isolation). Where a
scenario here overlaps with an existing focused test, that overlap is
noted rather than treated as redundant - the brief asks for these three
specifically as REAL-PIPELINE, not just adapter-level, checks.

Run:
    python3 -m pytest tests/test_tts_e2e_pipeline.py -q
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from typing import Callable, Dict, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.fish_audio import MockFishAudioClient  # noqa: E402


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo"] = demo
    spec.loader.exec_module(demo)
    return demo


class _ChunkCapture:
    """Voice Output Naturalness & First-Audio Latency sprint: honest
    dispatch-mode-agnostic replacement (never a weakened assertion) for
    reading a turn's `chunks` list directly off a single legacy
    `speak_request` payload. `ENABLE_LLM_TTS_STREAMING` now defaults to
    `True` (see `luno/config.py`), under which a turn's chunks arrive
    ONE AT A TIME via `speak_stream_chunk` (each already a real
    `SpeechChunk.to_dict()`) instead of precomputed in one event -
    there is no single moment "the chunks list" exists as a value the
    way `speak_request.data['chunks']` used to be.

    Subscribe this BEFORE calling `console.simulate_speech(...)` (same
    convention as every other subscriber in this file), then call
    `resolve()` to get `(request_id, chunks)` for the turn - under the
    legacy path this is immediate; under streaming it waits for the
    turn's own chunk arrivals to stop growing for `settle_s` seconds
    (the LLM mock in this file's scenarios has `chunk_delay_s=0.0`, so
    the full reconciled chunk set is enqueued to `FishAudioAdapter`
    within milliseconds of `llm_finished` - well before any of those
    chunks necessarily finish PLAYING, which is exactly the
    distinction Scenario B/C need to detect a barge-in-truncated
    turn)."""

    def __init__(self, console):
        self.speak_requests: List[object] = []
        self.stream_chunks: "Dict[str, List[dict]]" = {}
        self._console = console
        self._subs = [
            console.event_bus.subscribe("speak_request", self._on_request),
            console.event_bus.subscribe("speak_stream_chunk", self._on_stream_chunk),
        ]

    def _on_request(self, e) -> None:
        self.speak_requests.append(e)

    def _on_stream_chunk(self, e) -> None:
        rid = e.data.get("request_id")
        chunk = e.data.get("chunk") or {}
        self.stream_chunks.setdefault(rid, []).append(chunk)

    def unsubscribe(self) -> None:
        for s in self._subs:
            self._console.event_bus.unsubscribe(s)

    def resolve(self, timeout: float = 5.0, settle_s: float = 0.3) -> "Tuple[Optional[str], List[dict]]":
        assert _wait_until(lambda: bool(self.speak_requests) or bool(self.stream_chunks), timeout), (
            "no voice dispatch (speak_request or speak_stream_chunk) ever fired"
        )
        if self.speak_requests:
            req = self.speak_requests[-1]
            return req.data.get("request_id"), list(req.data.get("chunks") or [])
        request_id = next(reversed(self.stream_chunks))  # most recently opened turn
        _wait_for_stream_settle(lambda: len(self.stream_chunks.get(request_id, [])), timeout=timeout, settle_s=settle_s)
        return request_id, list(self.stream_chunks.get(request_id, []))


def _wait_for_stream_settle(get_count: Callable[[], int], timeout: float = 5.0, settle_s: float = 0.3, poll_s: float = 0.02) -> int:
    """Waits until `get_count()` stops increasing for `settle_s`
    seconds (bounded by `timeout`), then returns the final count - used
    to detect "the streaming reconciliation has finished enqueuing this
    turn's chunks" without relying on an explicit signal at the call
    site (mirrors the same "wait for quiet" idiom used elsewhere in
    this sprint's test fixes)."""
    deadline = time.time() + timeout
    last_count = get_count()
    last_change = time.time()
    while time.time() < deadline:
        time.sleep(poll_s)
        current = get_count()
        if current != last_count:
            last_count = current
            last_change = time.time()
        elif time.time() - last_change >= settle_s:
            return last_count
    return last_count


def _wake(console, demo) -> None:
    from luno.wake_session import ConversationState
    console.simulate_speech("alexa")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


# ============================================================================
# Scenario A: long response -> chunk -> queue -> fake TTS -> sequential
# playback -> completion.
#
# NOTE: chunk-ordering itself is already exhaustively covered at the
# adapter level by test_tts_queue.py::test_11 (8 chunks, exact order
# assertion on the fake client's `played` list). This scenario adds the
# REAL-PIPELINE layer on top: real wake session, real BehaviorTreeModule
# building the SpeakRequest via build_dual_response()/build_speech_chunks(),
# real SessionManagerModule state transitions, ending in a clean
# WAITING_USER/IDLE - not just a bare adapter+event-bus harness.
# ============================================================================

def test_e2e_A_long_response_chunks_queue_and_plays_sequentially():
    demo = _load_demo()
    from luno.wake_session import ConversationState

    long_reply = " ".join(f"Ini kalimat panjang nomor {n} untuk memicu banyak chunk suara." for n in range(1, 9))
    console = demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=long_reply, chunk_delay_s=0.0),
        fish_audio_client=MockFishAudioClient(playback_delay_s=0.02),
    )
    console.start()
    try:
        _wake(console, demo)

        capture = _ChunkCapture(console)
        finished = []
        sub2 = console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
        try:
            console.simulate_speech("ceritakan sesuatu yang panjang untuk uji chunking")
            request_id, chunks = capture.resolve(timeout=5.0)

            assert isinstance(chunks, list) and len(chunks) >= 2, f"expected multiple chunks, got {chunks}"
            assert all(isinstance(c, dict) for c in chunks), "chunks must be the SpeechChunk.to_dict() wire format"
            assert request_id, "voice dispatch missing request_id"

            # sequential playback -> completion of the WHOLE request
            assert _wait_until(lambda: finished == [request_id], 5.0), f"finished={finished}"

            # state returns to a normal resting state after speaking, not stuck
            assert _wait_until(
                lambda: console.session_manager.session.state in (
                    ConversationState.WAITING_USER, ConversationState.LISTENING, ConversationState.IDLE,
                ),
                3.0,
            ), f"state stuck at {console.session_manager.session.state}"
        finally:
            capture.unsubscribe()
            console.event_bus.unsubscribe(sub2)
    finally:
        console.stop()


# ============================================================================
# Scenario B: long response -> chunk #1 starts -> barge-in -> cancellation
# -> remaining chunks skipped -> state reset.
#
# NOTE: this is the real-pipeline counterpart already exercised by
# test_tts_cancellation.py::test_25_26_27_...; reproduced here explicitly
# under its Phase-10 scenario name/label per the brief, with an added
# assertion that not all chunks were spoken (remaining chunks skipped).
# ============================================================================

def test_e2e_B_barge_in_mid_response_cancels_and_resets_state():
    demo = _load_demo()
    from luno.wake_session import ConversationState

    long_reply = " ".join(f"Kalimat panjang nomor {n} untuk memastikan ada banyak chunk suara." for n in range(1, 9))
    client = MockFishAudioClient(playback_delay_s=0.15)
    console = demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=long_reply, chunk_delay_s=0.0),
        fish_audio_client=client,
    )
    console.start()
    try:
        _wake(console, demo)

        capture = _ChunkCapture(console)
        cancelled = []
        sub2 = console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e))
        # Voice Output Naturalness & First-Audio Latency sprint: honestly
        # updated (never weakened) - the ORIGINAL "fewer chunks played
        # than the total" check compared `total_chunks` (this turn's
        # own intended count) against `client.played` (the mock's own
        # WHOLE-SESSION play list, which also accumulates the wake
        # acknowledgement's "Yes?" and the barge-in acknowledgement's
        # "Okay." - entries from OTHER turns entirely). That was always
        # slightly imprecise, but harmless while `total_chunks` was
        # comfortably larger than the whole-session count. Streaming's
        # own reconciliation groups this reply's 8 sentences into fewer,
        # larger playback chunks (the same list-item/short-sentence
        # pairing `build_dual_response()` already does elsewhere), so
        # `total_chunks` is now small enough that the 2 extra unrelated
        # entries can tip the whole-session count over it even though
        # THIS turn's own chunks were genuinely cut short. Counting
        # `speech_chunk_playback_finished` events (published by
        # `FishAudioAdapter` once per chunk that was actually attempted
        # - played or retried-then-skipped, but NEVER for a chunk a
        # `PlaybackCancelled` preempted - see that method's own source)
        # scoped to THIS turn's own `request_id` is the precise,
        # turn-scoped signal the original blanket list only
        # approximated.
        chunks_finished_for_turn: List[str] = []
        request_id_holder: List[str] = []

        def _on_chunk_finished(e) -> None:
            if request_id_holder and e.get("request_id") == request_id_holder[0]:
                chunks_finished_for_turn.append(e.get("chunk_id"))

        sub3 = console.event_bus.subscribe("speech_chunk_playback_finished", _on_chunk_finished)
        try:
            console.simulate_speech("ceritakan sesuatu yang panjang sekali")
            request_id, chunks = capture.resolve(timeout=5.0)
            request_id_holder.append(request_id)
            total_chunks = len(chunks)
            assert total_chunks >= 2, f"need a multi-chunk reply for this scenario, got {total_chunks}"

            assert _wait_until(lambda: console.session_manager.session.state == ConversationState.SPEAKING, 3.0)
            # barge-in mid-speech
            console.simulate_speech("stop")

            assert _wait_until(lambda: len(cancelled) >= 1, 3.0), "barge-in never produced a cancellation"
            time.sleep(0.3)  # let any in-flight chunk settle

            assert _wait_until(
                lambda: console.session_manager.session.state in (
                    ConversationState.WAITING_USER, ConversationState.LISTENING, ConversationState.IDLE,
                ),
                3.0,
            ), f"state stuck at {console.session_manager.session.state}"

            # remaining chunks skipped: fewer of THIS TURN's own chunks
            # actually finished than the total it intended to speak.
            assert len(chunks_finished_for_turn) < total_chunks, (
                f"all {total_chunks} chunks of request_id={request_id} finished despite barge-in "
                f"cancellation: {chunks_finished_for_turn}"
            )
        finally:
            capture.unsubscribe()
            console.event_bus.unsubscribe(sub2)
            console.event_bus.unsubscribe(sub3)
    finally:
        console.stop()


# ============================================================================
# Scenario C: cancel a request -> immediately submit a new request -> the
# OLD request produces ZERO stale playback -> the NEW request plays
# normally.
#
# NOTE: the adapter-level version of this race (no console/session-manager
# involved) is already covered by test_tts_cancellation.py::test_23. This
# scenario runs the same shape through the REAL console pipeline: a
# barge-in cancels an in-progress reply, and the very next user utterance
# must produce a clean new reply with none of the old reply's remaining
# chunks leaking into it.
# ============================================================================

def test_e2e_C_cancel_then_new_request_has_zero_stale_playback():
    demo = _load_demo()

    old_reply = " ".join(f"Kalimat lama nomor {n}." for n in range(1, 9))
    client = MockFishAudioClient(playback_delay_s=0.1)
    console = demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=old_reply, chunk_delay_s=0.0),
        fish_audio_client=client,
    )
    console.start()
    try:
        _wake(console, demo)

        # Voice Output Naturalness & First-Audio Latency sprint: honestly
        # updated (never weakened) - `ENABLE_LLM_TTS_STREAMING` now
        # defaults to `True` (see `luno/config.py`), so a turn's own
        # `request_id` is learned from `speak_stream_chunk` (fires
        # possibly many times per turn, one real `SpeechChunk` each) as
        # well as the legacy single `speak_request`. `request_ids_seen`
        # tracks each DISTINCT request_id in first-observed order across
        # BOTH turns in this scenario - the actual invariant this test
        # cares about (two turns, two distinct ids, the old one never
        # resumes) is unaffected by which event carried each id.
        request_ids_seen: List[str] = []

        def _note_request_id(rid: Optional[str]) -> None:
            if rid and (not request_ids_seen or request_ids_seen[-1] != rid):
                request_ids_seen.append(rid)

        cancelled = []
        finished = []
        sub1 = console.event_bus.subscribe("speak_request", lambda e: _note_request_id(e.data.get("request_id")))
        sub1b = console.event_bus.subscribe("speak_stream_chunk", lambda e: _note_request_id(e.data.get("request_id")))
        sub2 = console.event_bus.subscribe("speech_playback_cancelled", lambda e: cancelled.append(e.get("request_id")))
        sub3 = console.event_bus.subscribe("speech_playback_finished", lambda e: finished.append(e.get("request_id")))
        try:
            # old turn: starts speaking, then gets barge-in cancelled
            console.simulate_speech("ceritakan cerita yang sangat panjang")
            assert _wait_until(lambda: len(request_ids_seen) >= 1, 5.0)
            old_request_id = request_ids_seen[-1]
            assert _wait_until(lambda: len(client.played) >= 1, 3.0), "old request never started playing"

            console.simulate_speech("stop")
            assert _wait_until(lambda: old_request_id in cancelled, 3.0), f"old request never cancelled: {cancelled}"
            old_played_count_at_cancel = len(client.played)

            # immediately submit a new turn right after cancellation settles
            console.openrouter_adapter.client.canned_text = "Jawaban baru yang singkat."
            before = len(request_ids_seen)
            console.simulate_speech("apa itu resistor?")
            assert _wait_until(lambda: len(request_ids_seen) > before, 5.0), "new turn never produced a request_id"
            new_request_id = request_ids_seen[-1]
            assert new_request_id != old_request_id

            assert _wait_until(lambda: new_request_id in finished, 5.0), f"new request never finished: finished={finished}"

            # the OLD request must never have resumed playback after being
            # cancelled - no chunk count growth attributable to the old
            # request once cancellation landed, and the old request must
            # never appear in `finished`.
            time.sleep(0.2)
            assert old_request_id not in finished, "stale/cancelled request produced a completion event"
            # every chunk played after the cancellation belongs to the NEW
            # request's text, not leftover old-request text
            old_texts = set(old_reply.split(". "))
            for played_text in client.played[old_played_count_at_cancel:]:
                assert played_text not in old_texts or played_text.strip() == "", (
                    f"stale old-request audio played after cancellation: {played_text!r}"
                )
        finally:
            console.event_bus.unsubscribe(sub1)
            console.event_bus.unsubscribe(sub1b)
            console.event_bus.unsubscribe(sub2)
            console.event_bus.unsubscribe(sub3)
    finally:
        console.stop()
