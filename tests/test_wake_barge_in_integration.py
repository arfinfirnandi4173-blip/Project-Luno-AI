"""
test_wake_barge_in_integration.py
====================================

Regression suite for the "Wake Session + Barge-In cooperate naturally"
bug fix: wake words are only ever required while SLEEPING; once a
session is open (LISTENING/THINKING/SPEAKING/WAITING_USER, or IDLE in
always-on mode), interrupt phrases must always reach and be acted on by
`BargeInModule` - without repeating the wake word, and without a
silent-drop gap between the LLM finishing and Fish Audio actually
starting to speak.

Unlike `tests/test_barge_in_console.py` (which mostly used
`sleep_enabled=False` for convenience), every scenario here uses the
REAL, default wake-gated config (`WakeSessionConfig()`,
`sleep_enabled=True`) - the exact configuration the bug report was
about - and every "voice" interaction goes through `simulate_speech()`
(publishing `SpeechRecognized`, i.e. a microphone-event simulation),
never `handle_line()`, except for the one scenario that explicitly
verifies the keyboard path still works.

Covers the task's own numbered list:
    1. Wake -> interrupt while speaking
    2. Wake -> interrupt while thinking
    3. Wake -> interrupt without repeating the wake word
    4. Sleeping -> "Stop" is ignored
    5. Sleeping -> "Luno stop" wakes, but never forwards "stop" as a request
    6. Multi-turn conversation, wake word spoken once
    7. Session timeout -> back to Sleeping -> wake word mandatory again
    8. Full console integration via SpeechRecognized (not keyboard)
    9. Keyboard interrupt still works
    10. Stress test: rapid interrupt phrases during streaming

Plus a dedicated regression test for the specific root cause found
during this fix: the gap between `llm_finished` and
`speech_playback_started` used to silently swallow an interrupt.

Run:
    python3 tests/test_wake_barge_in_integration.py
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from typing import Callable, List, Tuple

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
                  session_config=None) -> "demo.RuntimeDemoConsole":
    """Realistic default: wake gating ON (`sleep_enabled=True`, the
    dataclass default) unless a test explicitly overrides it."""
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

    def count(self, event_type: str) -> int:
        with self._lock:
            return sum(1 for t, _ in self.events if t == event_type)


SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _wake(console) -> None:
    """Say the wake word (microphone-event simulation, not a keyboard
    command) and wait until the session is genuinely LISTENING. Takes
    no event recorder on purpose - tests that create their `_EventRecorder`
    AFTER waking (the common case, so the wake ack's own
    `speech_playback_started`/`speech_playback_finished` doesn't get
    mistaken for the real turn's) don't have one yet at this point."""
    _silent(console.simulate_speech, "Luno")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


# ============================================================================
# 1 & 2 - Wake, then interrupt while speaking / while thinking
# ============================================================================

@scenario
def test_1_wake_then_interrupt_while_speaking():
    console = _new_console(
        llm_text="This is a long story about a brave little robot exploring the galaxy",
        chunk_delay_s=0.05, playback_delay_s=1.5,
    )
    _silent(console.start)
    try:
        _wake(console)
        # created AFTER waking - the wake ack's own speech_playback_started/
        # finished for "Yes?" must not be mistaken for this turn's.
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me a long story")
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)
        # voice interrupt - no wake word repeated
        _silent(console.simulate_speech, "Stop")
        assert _wait_until(lambda: rec.any_of("stop_playback"), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 3.0)
        acks = [t for ch, t in console.conversation_log if ch == "LUNO" and t in ("Okay.", "Sure.")]
        assert len(acks) == 1
    finally:
        _silent(console.stop)


@scenario
def test_2_wake_then_interrupt_while_thinking():
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
        assert not rec.any_of("speech_playback_started")  # still mid-stream
        _silent(console.simulate_speech, "cancel")
        assert _wait_until(lambda: rec.any_of("cancel_llm_request"), 3.0)
        assert _wait_until(lambda: rec.any_of("llm_cancelled"), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 3.0)
        assert not rec.any_of("speech_playback_finished")
    finally:
        _silent(console.stop)


# ============================================================================
# 3 - Interrupt without repeating the wake word (two interrupts, one wake)
# ============================================================================

@scenario
def test_3_interrupt_never_requires_repeating_wake_word():
    console = _new_console(
        llm_text="a very long answer that keeps going and going and going on forever",
        chunk_delay_s=0.05, playback_delay_s=1.0,
    )
    _silent(console.start)
    try:
        _wake(console)
        # `wake_count` (a plain attribute on ConversationSession, tracked
        # independently of any event recorder) is the authoritative count
        # of real wake sequences for the rest of this test - avoids the
        # wake ack's own playback events polluting a recorder created
        # before waking.
        assert console.session_manager.session.wake_count == 1

        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me a very long answer")
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)
        _silent(console.simulate_speech, "stop")  # no "Luno" prefix
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 3.0)

        # second turn, still no wake word repeated
        assert _wait_until(lambda: console.session_manager.session.state in
                            (ConversationState.WAITING_USER, ConversationState.LISTENING), 3.0)
        _silent(console.simulate_speech, "tell me another very long answer")
        assert _wait_until(lambda: rec.count("speech_playback_started") >= 2, 5.0)
        _silent(console.simulate_speech, "cancel")  # again, no "Luno" prefix
        assert _wait_until(lambda: rec.count("barge_in_action") >= 2, 3.0)

        # exactly one wake word detection for the whole exchange
        assert console.session_manager.session.wake_count == 1
    finally:
        _silent(console.stop)


# ============================================================================
# 4 & 5 - Sleeping-state behavior
# ============================================================================

@scenario
def test_4_sleeping_stop_is_ignored():
    console = _new_console()
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        assert console.session_manager.session.state == ConversationState.SLEEPING
        _silent(console.simulate_speech, "Stop")
        time.sleep(0.3)
        assert console.session_manager.session.state == ConversationState.SLEEPING
        assert not rec.any_of("conversation_started")
        assert not rec.any_of("barge_in_action")
        assert not rec.any_of("user_utterance")
    finally:
        _silent(console.stop)


@scenario
def test_5_sleeping_luno_stop_wakes_but_never_forwards_the_interrupt_remainder():
    """Bug fix (interrupt routing / request_id correlation): this test
    used to assert the OPPOSITE of what's correct - that the remainder
    ("stop") after a wake word gets forwarded as a brand-new
    conversational request ("nothing was in flight to interrupt at wake
    time, so it is correctly treated as the opening request"). That is
    exactly the reported bug: "Luno stop" would wake normally, then
    "stop" would reach Planner/OpenRouter and come back as a literal
    "you said stop" reply. Interrupt phrases must NEVER become
    conversation requests, wake remainder or not - see
    `luno/wake_session/manager.py`'s `_handle_playback_done()` bug-fix
    note. The wake word itself still always wakes the session; only the
    interrupt-shaped remainder's fate changed."""
    console = _new_console(llm_text="Stopping what exactly? Nothing is running yet.")
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        assert console.session_manager.session.state == ConversationState.SLEEPING
        _silent(console.simulate_speech, "Luno stop")
        assert _wait_until(lambda: rec.any_of("conversation_started"), 3.0)
        assert _wait_until(lambda: console.session_manager.session.state != ConversationState.SLEEPING, 3.0)
        # waking still happens normally...
        assert _wait_until(lambda: console.session_manager.status_snapshot()["state"] == "listening", 5.0)
        # ...but the interrupt-shaped remainder ("stop") must NEVER reach
        # Planner/OpenRouter as a new conversational request.
        assert not rec.any_of("user_utterance")
        assert not rec.any_of("planner_created")
        assert not rec.any_of("need_llm_response")
        # only the wake acknowledgement ("Yes?") should have been spoken.
        luno_lines = [t for ch, t in console.conversation_log if ch == "LUNO"]
        assert luno_lines == [console.session_manager.config.wake_acknowledgement]
    finally:
        _silent(console.stop)


@scenario
def test_5b_stray_interrupt_word_while_listening_is_not_sent_to_the_llm():
    """Interrupt Priority bug fix, the other half: while genuinely awake
    and idle between turns (LISTENING/WAITING_USER), a bare "stop" with
    nothing in flight is BargeInModule's no-op (see test_4's Sleeping
    equivalent) - but `SessionManagerModule` used to ALSO forward it
    onward as a brand-new literal conversational request. It must not:
    `looks_like_interrupt_or_resume()` short-circuits the forward."""
    console = _new_console()
    _silent(console.start)
    try:
        _wake(console)
        rec = _EventRecorder(console.event_bus)
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)
        _silent(console.simulate_speech, "stop")
        time.sleep(0.3)
        assert not rec.any_of("user_utterance")
        assert not rec.any_of("planner_created")
        # still awake and listening - not knocked into any odd state.
        assert console.session_manager.session.state == ConversationState.LISTENING
    finally:
        _silent(console.stop)


# ============================================================================
# 6 - Multi-turn conversation, wake word spoken once
# ============================================================================

@scenario
def test_6_multi_turn_conversation_single_wake_word():
    console = _new_console(llm_text="Sure, here you go.", playback_delay_s=0.02)
    _silent(console.start)
    try:
        _wake(console)
        # created AFTER waking - the wake ack's own playback-finished
        # event for "Yes?" must not count as one of the 3 turns below.
        rec = _EventRecorder(console.event_bus)
        for i in range(3):
            _silent(console.simulate_speech, f"question number {i}")
            assert _wait_until(lambda: rec.count("speech_playback_finished") == i + 1, 5.0)
            assert _wait_until(lambda: console.session_manager.session.state == ConversationState.WAITING_USER, 3.0)
        assert console.session_manager.session.wake_count == 1
        assert rec.count("user_utterance") == 3
    finally:
        _silent(console.stop)


# ============================================================================
# 7 - Session timeout returns to Sleeping; wake word mandatory again
# ============================================================================

@scenario
def test_7_session_timeout_returns_to_sleeping_requires_wake_again():
    console = _new_console(session_config=WakeSessionConfig(session_timeout_s=0.3))
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _wake(console)
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.SLEEPING, 3.0)

        # wake word required again - a bare "stop"/ordinary utterance
        # must be ignored until "Luno" is heard once more.
        _silent(console.simulate_speech, "hello are you there")
        time.sleep(0.2)
        assert console.session_manager.session.state == ConversationState.SLEEPING
        assert not rec.any_of("user_utterance")

        _wake(console)
        assert console.session_manager.session.wake_count == 2
    finally:
        _silent(console.stop)


# ============================================================================
# 8 - explicit confirmation: this whole file drives everything through
#     simulate_speech() (SpeechRecognized), never handle_line(), except
#     scenario 9 below which exists specifically to check the opposite.
# ============================================================================

@scenario
def test_8_console_integration_uses_speech_recognized_not_keyboard():
    console = _new_console()
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "Luno")
        assert _wait_until(lambda: rec.any_of("wake_word_detected"), 3.0)
        # simulate_speech publishes a real SpeechRecognized event - confirm
        # that's genuinely what happened, not some keyboard-command path.
        assert rec.any_of("speech_recognized")
    finally:
        _silent(console.stop)


# ============================================================================
# 9 - keyboard interrupt still works (regression against the OLD shortcut)
# ============================================================================

@scenario
def test_9_keyboard_interrupt_still_works():
    client = MockOpenRouterClient(canned_text="a slow reply that takes a while to stream out", chunk_delay_s=0.25)
    console = _new_console()
    console.openrouter_adapter.client = client  # swap for a slow-streaming one
    console.adapter_manager  # (no-op access, keeps intent explicit)
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
        _silent(console.handle_line, "stop")  # the keyboard shortcut, not simulate_speech
        assert _wait_until(cancelled.is_set, 3.0)
    finally:
        _silent(console.stop)


# ============================================================================
# 10 - stress test: rapid interrupt phrases during streaming, wake-gated
# ============================================================================

@scenario
def test_10_stress_rapid_interrupts_during_streaming_with_wake_gating():
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

        errors: List[BaseException] = []
        phrases = ["stop", "cancel", "wait", "hold on", "enough", "stop"]

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
    finally:
        _silent(console.stop)


# ============================================================================
# Dedicated regression: the thinking->speaking gap itself
# ============================================================================

@scenario
def test_gap_between_llm_finished_and_speech_playback_started_is_not_dropped():
    """Root-cause regression test: previously, an interrupt spoken in the
    window between the LLM finishing and Fish Audio actually starting to
    play was silently swallowed - `BargeInModule` believed nothing was
    in flight (`thinking=False`, `speaking=False`). Against the mock
    pipeline (near-zero latency end to end) that gap is too narrow to
    land in reliably by timing alone - a real Fish Audio/OpenRouter
    backend is exactly where it matters. So this test drives the fully
    real, wired `BargeInModule`/`SessionManagerModule`/`BehaviorTreeModule`
    trio directly with the same two events a real turn publishes
    (`llm_started` then `llm_finished`) for a synthetic request_id,
    with a small controlled pause in between (well inside the fix's
    grace window, unmistakably outside instant-mock-pipeline timing),
    then says "stop" - proving the fix's actual mechanism
    (`_speech_pending_deadline`) rather than hoping to win a timing race."""
    console = _new_console()
    _silent(console.start)
    try:
        _wake(console)
        rec = _EventRecorder(console.event_bus)

        rid = "gap-fix-regression-turn"
        console.event_bus.publish(demo.Event(type="speaking_mode_assigned", data={"request_id": rid, "mode": "free"}))
        console.event_bus.publish(demo.Event(type="llm_started", data={"request_id": rid}))
        assert _wait_until(lambda: console.barge_in_module.thinking is True, 2.0)
        console.event_bus.publish(demo.Event(type="llm_finished", data={"request_id": rid}))
        assert _wait_until(lambda: console.barge_in_module.thinking is False, 2.0)
        # right here: thinking=False, speaking=False - exactly the gap.
        assert console.barge_in_module.speaking is False
        assert console.barge_in_module.status_snapshot()["speech_pending"] is True

        _silent(console.simulate_speech, "stop")
        assert _wait_until(lambda: rec.any_of("cancel_llm_request", lambda d: d.get("request_id") == rid), 3.0)
        assert _wait_until(lambda: rec.any_of("llm_cancelled", lambda d: d.get("request_id") == rid), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 3.0)

        # and BehaviorTreeModule's own suppression bookkeeping picked up
        # the same llm_cancelled for this request_id.
        assert _wait_until(lambda: rid in console.behavior_tree_module._cancelled_request_ids, 2.0)
    finally:
        _silent(console.stop)


@scenario
def test_language_override_reaches_the_real_system_prompt():
    """Bug fix: `config.LUNO_LANGUAGE` (e.g. LUNO_LANGUAGE=english in
    .env) already worked correctly in the legacy `luno/main.py`, but was
    never wired into `main_runtime_demo.py` - the bridge
    `luno/bootstrap/modules.py` actually loads for the real production
    Runtime (`python main.py`). Without it, the LLM had nothing telling
    it to override the language it naturally drifts toward (mirroring
    the user's own message), while the always-included persona block
    still supplied English catchphrases/example lines verbatim -
    producing mixed-language replies like "Lampu utama sudah dimatikan.
    There, happy now?" (Indonesian device-status sentence + English
    persona flourish). This verifies the override note actually reaches
    the real `NeedLLMResponse.system_prompt` for a genuine Indonesian
    utterance, and that persona.py's own former "any language" wording
    (which directly contradicted this override) is gone."""
    import os
    from luno import config as legacy_config

    original_env = os.environ.get("LUNO_LANGUAGE")
    original_module_value = legacy_config.LUNO_LANGUAGE
    os.environ["LUNO_LANGUAGE"] = "english"
    legacy_config.LUNO_LANGUAGE = "english"
    try:
        console = _new_console()
        _silent(console.start)
        try:
            _wake(console)
            rec = _EventRecorder(console.event_bus)
            _silent(console.simulate_speech, "nyalakan lampu kamar")
            assert _wait_until(lambda: rec.any_of("need_llm_response"), 3.0)

            with rec._lock:
                system_prompt = next(
                    d.get("system_prompt") for t, d in rec.events if t == "need_llm_response"
                )
            assert system_prompt, "system_prompt was empty/None"
            assert "ENTIRE reply MUST be written in english" in system_prompt
            assert "any language" not in system_prompt
        finally:
            _silent(console.stop)
    finally:
        if original_env is None:
            os.environ.pop("LUNO_LANGUAGE", None)
        else:
            os.environ["LUNO_LANGUAGE"] = original_env
        legacy_config.LUNO_LANGUAGE = original_module_value


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
