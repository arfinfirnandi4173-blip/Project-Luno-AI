"""
test_interrupt_routing_fix.py
================================

Regression suite for the "Interrupt Routing & Request ID Correlation"
bug fix, run against the FULL, real `RuntimeDemoConsole` stack (real
Event Bus, real `SessionManagerModule`, `BargeInModule`,
`BehaviorTreeModule`, `PlannerBridgeModule`; only OpenRouter and Fish
Audio are mocked - no network, no microphone).

Two distinct bugs are covered here:

    1. Interrupt phrases spoken TOGETHER with a wake word in one
       utterance ("Luno stop") used to wake the session normally, but
       then forward the remainder ("stop") straight into
       `_forward_to_conversation()` with no interrupt check at all -
       reaching Planner, then OpenRouter, producing a literal "you said
       stop" reply. Fixed in `luno/wake_session/manager.py`'s
       `_handle_playback_done()` (see that file's own bug-fix note).
    2. The developer keyboard shortcut for "cancel" (typing bare "stop"/
       "cancel" at the console, NOT via `simulate_speech()`) fell back
       to `planner_module.last_plan_id` - a Planner-internal id - as a
       substitute `request_id` whenever nothing was actively streaming,
       so `cancel_llm_request` could never actually match the in-flight
       LLM request. Fixed in `main_runtime_demo.py`'s `_interrupt()` to
       use `barge_in_module.current_request_id` instead - the SAME id
       `OpenRouterAdapter` actually echoes through the whole turn.

Covers the task's own numbered regression list:
    1. "Luno stop" while sleeping
    2. "Stop" during Speaking
    3. "Stop" during Thinking
    4. "Stop" during WaitingUser
    5. Planner is never created for interrupt phrases
    6. OpenRouter never receives interrupt phrases
    7. request_id remains identical through one conversation turn
    8. planner_id is never used as request_id
    9. Keyboard interrupt still works
    10. Stress test with rapid interrupt commands during streaming

Run:
    python3 tests/test_interrupt_routing_fix.py
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from typing import Callable, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import importlib.util

_spec = importlib.util.spec_from_file_location("main_runtime_demo", os.path.join(_ROOT, "main_runtime_demo.py"))
demo = importlib.util.module_from_spec(_spec)
sys.modules["main_runtime_demo"] = demo
_spec.loader.exec_module(demo)

from luno.adapters import MockFishAudioClient, MockOpenRouterClient  # noqa: E402
from luno.wake_session import ConversationState, WakeSessionConfig  # noqa: E402

#: every phrase the task explicitly lists as "must never become a
#: conversation request" - used to sweep item #5/#6 across the full list,
#: not just "stop".
INTERRUPT_PHRASES = ["stop", "cancel", "never mind", "wait", "hold on", "pause"]
RESUME_PHRASES = ["resume", "continue"]


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _silent(fn, *a, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


def _new_console(llm_text="OK, doing that.", chunk_delay_s: float = 0.0, playback_delay_s: float = 0.05,
                  session_config: Optional[WakeSessionConfig] = None) -> "demo.RuntimeDemoConsole":
    """Realistic default: wake gating ON (`sleep_enabled=True`, the
    dataclass default) unless a test explicitly overrides it - same
    convention as `tests/test_wake_barge_in_integration.py`."""
    client = MockOpenRouterClient(canned_text=llm_text, chunk_delay_s=chunk_delay_s)
    fish = MockFishAudioClient(playback_delay_s=playback_delay_s)
    return demo.RuntimeDemoConsole(
        openrouter_client=client, fish_audio_client=fish,
        session_config=session_config or WakeSessionConfig(),
    )


class _EventRecorder:
    def __init__(self, bus) -> None:
        self.events: List[Tuple[str, dict]] = []
        self._lock = threading.Lock()
        self.sub_id = bus.subscribe("*", self._on)

    def _on(self, e) -> None:
        with self._lock:
            self.events.append((e.type, dict(e.data)))

    def any_of(self, event_type: str, pred: Callable[[dict], bool] = lambda d: True) -> bool:
        with self._lock:
            return any(t == event_type and pred(d) for t, d in self.events)

    def count(self, event_type: str, pred: Callable[[dict], bool] = lambda d: True) -> int:
        with self._lock:
            return sum(1 for t, d in self.events if t == event_type and pred(d))

    def all_data(self, event_type: str) -> List[dict]:
        with self._lock:
            return [d for t, d in self.events if t == event_type]


SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _wake(console) -> None:
    """Say the wake word and wait until the session is genuinely
    LISTENING (i.e. the "Yes?" wake-ack has fully finished playing) -
    same convention as `tests/test_wake_barge_in_integration.py`'s own
    `_wake()`, so a recorder created AFTER this never mistakes the ack's
    own events for the turn under test."""
    _silent(console.simulate_speech, "Luno")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 5.0)


# ============================================================================
# 1 - "Luno stop" while sleeping: wakes, but "stop" never becomes a request
# ============================================================================

@scenario
def test_1_luno_stop_while_sleeping_never_reaches_planner_or_openrouter():
    console = _new_console()
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "Luno stop")
        assert _wait_until(lambda: rec.any_of("wake_word_detected"), 3.0)
        # let the wake-ack ("Yes?") fully play out and the remainder get processed
        assert _wait_until(lambda: console.session_manager.status_snapshot()["state"] == "listening", 5.0)

        assert not rec.any_of("planner_created")
        assert not rec.any_of("need_llm_response")
        assert not rec.any_of("user_utterance")
        # the wake ack itself ("Yes?") is the only thing that should have spoken
        luno_lines = [t for ch, t in console.conversation_log if ch == "LUNO"]
        assert luno_lines == [console.session_manager.config.wake_acknowledgement]
    finally:
        _silent(console.stop)


# ============================================================================
# 2, 3, 4 - "stop" during Speaking / Thinking / WaitingUser
# ============================================================================

@scenario
def test_2_stop_during_speaking_is_interrupted_not_forwarded():
    console = _new_console(
        llm_text="this is a long enough reply that playback is still going when we interrupt it",
        chunk_delay_s=0.05, playback_delay_s=1.5,
    )
    _silent(console.start)
    try:
        _wake(console)
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me a long story")
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)
        planner_created_before = rec.count("planner_created")

        _silent(console.simulate_speech, "stop")
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 3.0)
        assert _wait_until(lambda: rec.any_of("speech_playback_cancelled"), 3.0)

        # "stop" itself must never have created a NEW plan/LLM request.
        assert rec.count("planner_created") == planner_created_before
        assert not rec.any_of("need_llm_response", lambda d: "stop" in str(d.get("messages", "")).lower())
    finally:
        _silent(console.stop)


@scenario
def test_3_stop_during_thinking_is_interrupted_not_forwarded():
    console = _new_console(
        llm_text="one two three four five six seven eight nine ten eleven twelve",
        chunk_delay_s=0.2, playback_delay_s=0.05,
    )
    _silent(console.start)
    try:
        _wake(console)
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me something")
        assert _wait_until(lambda: rec.any_of("llm_started"), 3.0)
        assert not rec.any_of("speech_playback_started")  # still thinking
        planner_created_before = rec.count("planner_created")

        _silent(console.simulate_speech, "stop")
        assert _wait_until(lambda: rec.any_of("cancel_llm_request"), 3.0)
        assert _wait_until(lambda: rec.any_of("llm_cancelled"), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 3.0)

        assert rec.count("planner_created") == planner_created_before
        assert not rec.any_of("speech_playback_finished")  # original 12-word reply never fully played
    finally:
        _silent(console.stop)


@scenario
def test_4_stop_during_waiting_user_is_not_forwarded_as_new_request():
    console = _new_console(llm_text="Done.", chunk_delay_s=0.0, playback_delay_s=0.05)
    _silent(console.start)
    try:
        _wake(console)
        _silent(console.simulate_speech, "do something quick")
        assert _wait_until(lambda: console.session_manager.status_snapshot()["state"] == "waiting_user", 5.0)

        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "stop")
        time.sleep(0.3)  # nothing should happen - give any wrong routing a chance to show up

        assert not rec.any_of("planner_created")
        assert not rec.any_of("need_llm_response")
        assert not rec.any_of("wake_word_detected")  # must not be mistaken for a fresh wake either
        # BargeInModule correctly sees "stop" but has nothing in flight to interrupt.
        assert console.session_manager.status_snapshot()["state"] == "waiting_user"
    finally:
        _silent(console.stop)


# ============================================================================
# 5, 6 - sweep every interrupt/resume phrase: Planner/OpenRouter never see them
# ============================================================================

@scenario
def test_5_planner_never_created_for_any_interrupt_phrase():
    console = _new_console()
    _silent(console.start)
    try:
        _wake(console)
        rec = _EventRecorder(console.event_bus)
        for phrase in INTERRUPT_PHRASES:
            before = rec.count("planner_created")
            _silent(console.simulate_speech, phrase)
            time.sleep(0.15)
            after = rec.count("planner_created")
            assert after == before, f"Planner plan was created for interrupt phrase {phrase!r}"
        assert not rec.any_of("planner_created")
        assert not rec.any_of("user_utterance")
    finally:
        _silent(console.stop)


@scenario
def test_6_openrouter_never_receives_interrupt_phrases():
    console = _new_console()
    _silent(console.start)
    try:
        _wake(console)
        rec = _EventRecorder(console.event_bus)
        for phrase in INTERRUPT_PHRASES + RESUME_PHRASES:
            _silent(console.simulate_speech, phrase)
            time.sleep(0.15)

        assert not rec.any_of("need_llm_response")
        for d in rec.all_data("need_llm_response"):  # defensive - should be empty anyway
            messages = d.get("messages") or []
            for m in messages:
                content = str(m.get("content", "")).strip().lower()
                assert content not in [p.lower() for p in INTERRUPT_PHRASES + RESUME_PHRASES]
    finally:
        _silent(console.stop)


# ============================================================================
# 7 - request_id stays identical through one full, ordinary conversation turn
# ============================================================================

@scenario
def test_7_request_id_identical_through_full_turn():
    """Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened) - `ENABLE_LLM_TTS_STREAMING` now defaults
    to `True` (see `luno/config.py`), so a turn's voice dispatch fires
    `speak_stream_chunk` instead of the legacy `speak_request`. Both
    payloads carry `request_id` at the top level (see
    `luno/incremental_speech.py`'s own `SpeakStreamChunk` publish site),
    so this test's actual invariant - one `request_id`, identical across
    every stage of the turn, never a `plan_...` id - is unchanged and
    still fully enforced; it now just accepts either event as evidence
    of the "voice stage" of the turn."""
    console = _new_console(llm_text="Sure, here you go.", chunk_delay_s=0.0, playback_delay_s=0.05)
    _silent(console.start)
    try:
        _wake(console)
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "what's the weather")
        assert _wait_until(lambda: rec.any_of("speech_playback_finished"), 5.0)

        ids_by_stage = {}
        for stage in ("need_llm_response", "llm_started", "llm_finished", "assistant_response",
                      "speak_request", "speech_playback_started", "speech_playback_finished"):
            data_list = rec.all_data(stage)
            if not data_list and stage == "speak_request":
                data_list = rec.all_data("speak_stream_chunk")
                stage = "speak_stream_chunk"
            assert data_list, f"missing event for stage {stage!r}"
            ids_by_stage[stage] = data_list[-1].get("request_id")

        distinct_ids = set(ids_by_stage.values())
        assert len(distinct_ids) == 1, f"request_id diverged across stages: {ids_by_stage}"
        rid = distinct_ids.pop()
        assert rid is not None and rid.startswith("turn-"), f"unexpected request_id shape: {rid!r}"
        assert not rid.startswith("plan_")
    finally:
        _silent(console.stop)


# ============================================================================
# 8 - planner_id is never substituted for request_id (the keyboard-cancel bug)
# ============================================================================

@scenario
def test_8_planner_id_never_used_as_request_id_for_cancel():
    """Reproduces the exact reported bug: the LLM stream has already
    finished (so the console's own `_streaming_request_id` bookkeeping
    is back to None) but the reply is still mid-synthesis/playback, so
    there IS a legitimate turn to cancel. The keyboard "stop" shortcut
    used to fall back to `planner_module.last_plan_id` here - a
    "plan_..." id that can never match anything OpenRouterAdapter
    tracks - instead of the turn's real "turn-..." request_id."""
    console = _new_console(
        llm_text="a reply to keep the pipeline busy long enough to interrupt",
        chunk_delay_s=0.0, playback_delay_s=1.5, session_config=WakeSessionConfig(sleep_enabled=False),
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me something")
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)
        # the stream has already fully finished by now - this is exactly
        # the gap the bug lived in.
        assert console._streaming_request_id is None

        turn_request_id = console.barge_in_module.current_request_id
        plan_id = console.planner_module.last_plan_id
        assert turn_request_id is not None and plan_id is not None
        assert turn_request_id != plan_id

        _silent(console.handle_line, "stop")  # the keyboard shortcut, not simulate_speech
        assert _wait_until(lambda: rec.any_of("cancel_llm_request"), 3.0)

        cancel_events = rec.all_data("cancel_llm_request")
        assert cancel_events
        cancelled_rid = cancel_events[-1].get("request_id")
        assert cancelled_rid == turn_request_id, f"expected {turn_request_id!r}, got {cancelled_rid!r}"
        assert cancelled_rid != plan_id
        assert not str(cancelled_rid).startswith("plan_")
    finally:
        _silent(console.stop)


# ============================================================================
# 9 - keyboard interrupt still works (classic streaming-cancel case)
# ============================================================================

@scenario
def test_9_keyboard_interrupt_still_works():
    client = MockOpenRouterClient(canned_text="a slow reply that takes a while to stream out", chunk_delay_s=0.25)
    console = _new_console()
    console.openrouter_adapter.client = client  # swap for a slow-streaming one
    _silent(console.start)
    try:
        request_id = "kbd-cancel-test"
        console._streaming_request_id = request_id
        cancelled = threading.Event()
        console.event_bus.subscribe("llm_cancelled", lambda e: cancelled.set())
        _silent(console.event_bus.publish, demo.NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "tell me a long story"}], "stream": True, "request_id": request_id,
        }))
        time.sleep(0.3)
        _silent(console.handle_line, "stop")
        assert _wait_until(cancelled.is_set, 3.0)
    finally:
        _silent(console.stop)


# ============================================================================
# 10 - stress test: rapid interrupt commands during streaming
# ============================================================================

@scenario
def test_10_stress_rapid_interrupts_during_streaming_never_create_a_plan():
    console = _new_console(
        llm_text="This reply is long enough to survive several concurrent interrupt attempts easily",
        chunk_delay_s=0.05, playback_delay_s=1.5,
    )
    _silent(console.start)
    try:
        _wake(console)
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me something long")
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)
        planner_created_before = rec.count("planner_created")

        errors: List[BaseException] = []
        phrases = ["stop", "cancel", "wait", "hold on", "pause", "stop", "never mind"]

        def _say(p: str) -> None:
            try:
                console.simulate_speech(p)
            except BaseException as ex:  # pragma: no cover
                errors.append(ex)

        threads = [threading.Thread(target=_say, args=(p,)) for p in phrases]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        assert not errors
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 5.0)
        assert _wait_until(lambda: console.runtime.health().healthy, 3.0)
        # none of the rapid interrupt phrases should have created a NEW plan.
        assert rec.count("planner_created") == planner_created_before
    finally:
        _silent(console.stop)


def main() -> int:
    passed = 0
    failed = 0
    for name, fn in SCENARIOS:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as ex:
            print(f"  [FAIL] {name}: {ex}")
            failed += 1
        except Exception as ex:  # pragma: no cover
            print(f"  [ERROR] {name}: {ex}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
