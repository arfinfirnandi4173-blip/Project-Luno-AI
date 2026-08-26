"""
test_wake_session_console.py
==============================

Integration tests for Sprint 2 (wake word + conversation session
management) wired into the real `RuntimeDemoConsole` - Event Bus, real
`SessionManagerModule`, real `BehaviorTreeModule`/`PlannerBridgeModule`,
mocked OpenRouter/Fish Audio only (no network, no microphone). Covers
exactly the spec's testing list: valid wake word, invalid wake word,
conversation timeout, conversation extension, multiple follow-up
questions, manual sleep, manual wake, configuration reload, concurrent
speech, stress testing.

Run:
    python3 tests/test_wake_session_console.py
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

from luno.adapters import MockOpenRouterClient  # noqa: E402
from luno.adapters.fish_audio import MockFishAudioClient  # noqa: E402
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


def _new_console(session_config=None, llm_text="OK, doing that.") -> "demo.RuntimeDemoConsole":
    """Voice Output Naturalness & First-Audio Latency sprint bug fix:
    this helper never passed an explicit `fish_audio_client`, so it
    silently fell back to `main_runtime_demo._default_fish_audio_client()`
    - which honors the repo's own `.env`'s `FISH_AUDIO_BACKEND=real`
    (set for a prior, unrelated "Real TTS adapter" sprint's own testing)
    and constructs a REAL client pointed at a self-hosted GPT-SoVITS
    server (`http://127.0.0.1:9880`) that does not exist in this
    sandbox - every synthesis call fails outright.

    This file's own tests are entirely about session timeout/extension
    state-machine timing, not TTS backend choice, and every OTHER test
    file in this suite already passes an explicit `MockFishAudioClient`
    for exactly this reason. It was merely latent here before this
    sprint: the LEGACY `speak_request` path transitions THINKING ->
    SPEAKING unconditionally the moment a reply is committed to being
    spoken, regardless of whether synthesis later succeeds - masking a
    broken TTS backend entirely. Now that `ENABLE_LLM_TTS_STREAMING`
    defaults to `True` (see `luno/config.py`), that same transition
    instead happens on `speech_playback_started` (see `wake_session/
    manager.py::_handle_playback_started()`'s own docstring), which
    only fires once synthesis genuinely SUCCEEDS - so a synthesis
    backend that always fails now leaves the session stuck at THINKING
    forever, exactly the kind of stuck-state Phase 5 of this sprint set
    out to audit. Passing an explicit `MockFishAudioClient` (never
    touching the network, always succeeding) is the correct fix here:
    it makes this file test what it always meant to test, not the
    availability of a TTS server nothing in this repo runs locally."""
    client = MockOpenRouterClient(canned_text=llm_text, chunk_delay_s=0.0)
    return demo.RuntimeDemoConsole(
        openrouter_client=client,
        fish_audio_client=MockFishAudioClient(playback_delay_s=0.02),
        session_config=session_config or WakeSessionConfig(),
    )


SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


# ============================================================================
# Valid / invalid wake word
# ============================================================================

@scenario
def test_valid_wake_word_starts_a_session_and_speaks_acknowledgement():
    console = _new_console()
    _silent(console.start)
    try:
        assert console.session_manager.session.state == ConversationState.SLEEPING
        started = threading.Event()
        console.event_bus.subscribe("conversation_started", lambda e: started.set())
        _silent(console.simulate_speech, "Luno")
        assert _wait_until(started.is_set, 2.0)
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)
        assert "Yes?" in [t for ch, t in console.conversation_log if ch == "LUNO"]
    finally:
        _silent(console.stop)


@scenario
def test_invalid_wake_word_is_silently_ignored_no_llm_no_planner():
    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        planner_created = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())
        console.event_bus.subscribe("planner_created", lambda e: planner_created.set())
        _silent(console.simulate_speech, "what time is it")
        time.sleep(0.3)
        assert console.session_manager.session.state == ConversationState.SLEEPING
        assert not need_llm.is_set()
        assert not planner_created.is_set()
    finally:
        _silent(console.stop)


@scenario
def test_low_confidence_wake_word_is_rejected_not_accepted():
    console = _new_console(session_config=WakeSessionConfig(wake_confidence=0.9))
    _silent(console.start)
    try:
        rejected = threading.Event()
        console.event_bus.subscribe("wake_word_rejected", lambda e: rejected.set())
        _silent(console.simulate_speech, "luno", 0.2)
        assert _wait_until(rejected.is_set, 2.0)
        assert console.session_manager.session.state == ConversationState.SLEEPING
    finally:
        _silent(console.stop)


@scenario
def test_combined_wake_and_command_in_one_utterance_forwards_remainder():
    console = _new_console()
    _silent(console.start)
    try:
        planner_created = threading.Event()
        console.event_bus.subscribe("planner_created", lambda e: planner_created.set())
        _silent(console.simulate_speech, "Luno, open chrome")
        assert _wait_until(planner_created.is_set, 3.0)
    finally:
        _silent(console.stop)


# ============================================================================
# Timeout / extension
# ============================================================================

@scenario
def test_conversation_timeout_returns_to_sleeping():
    console = _new_console(session_config=WakeSessionConfig(session_timeout_s=0.4))
    _silent(console.start)
    try:
        _silent(console.simulate_speech, "Luno")
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)
        timed_out = threading.Event()
        console.event_bus.subscribe("conversation_timeout", lambda e: timed_out.set())
        assert _wait_until(timed_out.is_set, 2.0)
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.SLEEPING, 2.0)
    finally:
        _silent(console.stop)


@scenario
def test_conversation_extension_valid_speech_resets_timeout():
    console = _new_console(session_config=WakeSessionConfig(session_timeout_s=0.6))
    _silent(console.start)
    try:
        _silent(console.simulate_speech, "Luno")
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)

        # Speak again just before the timeout would fire - this must
        # push the deadline out, not let it lapse.
        time.sleep(0.35)
        _silent(console.simulate_speech, "hello again")
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.WAITING_USER, 3.0)
        # total elapsed since the SECOND utterance is well under the
        # timeout - session must still be awake.
        assert console.session_manager.session.state != ConversationState.SLEEPING
    finally:
        _silent(console.stop)


@scenario
def test_luno_speaking_alone_does_not_extend_session_indefinitely():
    """A single reply's own SPEAKING duration must not, by itself,
    perpetually push the deadline forward - the fresh window granted on
    entering WAITING_USER must still expire normally if the user never
    replies again."""
    console = _new_console(session_config=WakeSessionConfig(session_timeout_s=0.4))
    _silent(console.start)
    try:
        _silent(console.simulate_speech, "Luno")
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)
        _silent(console.simulate_speech, "hello")
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.WAITING_USER, 3.0)
        # No further user speech - must still time out on schedule.
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.SLEEPING, 2.0)
    finally:
        _silent(console.stop)


# ============================================================================
# Multi-turn conversation
# ============================================================================

@scenario
def test_multiple_followup_questions_need_no_repeated_wake_word():
    console = _new_console(session_config=WakeSessionConfig(session_timeout_s=5.0))
    _silent(console.start)
    try:
        _silent(console.simulate_speech, "Luno")
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)

        turns_seen = []
        console.event_bus.subscribe("conversation_speech", lambda e: turns_seen.append(e.get("text")))

        for text in ("what is unity", "who created it", "is it free"):
            before = len(turns_seen)
            _silent(console.simulate_speech, text)
            # Session may already legitimately be sitting in WAITING_USER
            # from the previous turn - waiting on that state alone would
            # pass trivially without this turn ever being processed.
            # Wait for the actual forward (a new conversation_speech
            # entry) first, THEN for the round trip to finish.
            assert _wait_until(lambda: len(turns_seen) > before, 2.0), f"'{text}' was never forwarded"
            assert _wait_until(lambda: console.session_manager.session.state == ConversationState.WAITING_USER, 3.0)

        assert turns_seen == ["what is unity", "who created it", "is it free"]
        # never went back to sleep between turns
        assert console.session_manager.session.wake_count == 1
    finally:
        _silent(console.stop)


# ============================================================================
# Manual /sleep, /wake
# ============================================================================

@scenario
def test_manual_sleep_forces_sleeping_regardless_of_state():
    console = _new_console()
    _silent(console.start)
    try:
        _silent(console.simulate_speech, "Luno")
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)
        ended = threading.Event()
        console.event_bus.subscribe("conversation_ended", lambda e: ended.set())
        _silent(console.handle_line, "/sleep")
        assert console.session_manager.session.state == ConversationState.SLEEPING
        assert _wait_until(ended.is_set, 1.0)
    finally:
        _silent(console.stop)


@scenario
def test_manual_wake_runs_the_real_wake_sequence():
    console = _new_console()
    _silent(console.start)
    try:
        assert console.session_manager.session.state == ConversationState.SLEEPING
        started = threading.Event()
        console.event_bus.subscribe("conversation_started", lambda e: started.set())
        _silent(console.handle_line, "/wake")
        assert _wait_until(started.is_set, 2.0)
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)
        assert console.session_manager.session.wake_count == 1
    finally:
        _silent(console.stop)


@scenario
def test_session_command_reports_state_without_crashing():
    console = _new_console()
    _silent(console.start)
    try:
        keep_going, out = _silent(console.handle_line, "/session")
        assert keep_going is True
        assert "sleeping" in out
        assert "wake_words" in out
    finally:
        _silent(console.stop)


# ============================================================================
# Configuration reload
# ============================================================================

@scenario
def test_reload_picks_up_new_wake_words_and_timeout_without_restart():
    console = _new_console()
    _silent(console.start)
    try:
        os.environ["WAKE_WORDS"] = "computer,hey computer"
        os.environ["SESSION_TIMEOUT"] = "42"
        try:
            _silent(console.handle_line, "/reload")
        finally:
            del os.environ["WAKE_WORDS"]
            del os.environ["SESSION_TIMEOUT"]

        snap = console.session_manager.status_snapshot()
        assert snap["config"]["wake_words"] == ["computer", "hey computer"]
        assert snap["config"]["session_timeout_s"] == 42.0

        # old wake word "luno" must no longer work
        _silent(console.simulate_speech, "luno")
        time.sleep(0.2)
        assert console.session_manager.session.state == ConversationState.SLEEPING

        # new wake word does work
        _silent(console.simulate_speech, "computer")
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)
    finally:
        _silent(console.stop)


@scenario
def test_reload_does_not_drop_an_in_flight_conversation():
    console = _new_console()
    _silent(console.start)
    try:
        _silent(console.simulate_speech, "Luno")
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)
        _silent(console.handle_line, "/reload")
        # a bare config reload must not itself force the session back to sleep
        assert console.session_manager.session.state == ConversationState.LISTENING
    finally:
        _silent(console.stop)


# ============================================================================
# Concurrent speech / stress
# ============================================================================

@scenario
def test_concurrent_speech_from_multiple_threads_no_crash_no_crosstalk():
    console = _new_console(session_config=WakeSessionConfig(session_timeout_s=10.0))
    _silent(console.start)
    try:
        _silent(console.simulate_speech, "Luno")
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)

        errors: List[Exception] = []

        def _speak(n: int) -> None:
            try:
                console.simulate_speech(f"concurrent utterance {n}")
            except Exception as ex:  # pragma: no cover
                errors.append(ex)

        threads = [threading.Thread(target=_speak, args=(i,)) for i in range(15)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        time.sleep(0.5)

        assert not errors, errors
        assert console.runtime.health().healthy
        # session must have landed in a real, valid state - not corrupted
        assert console.session_manager.session.state in (
            ConversationState.THINKING, ConversationState.SPEAKING,
            ConversationState.WAITING_USER, ConversationState.LISTENING,
        )
    finally:
        _silent(console.stop)


@scenario
def test_stress_many_wake_sleep_cycles():
    console = _new_console(session_config=WakeSessionConfig(session_timeout_s=10.0))
    _silent(console.start)
    try:
        t0 = time.time()
        for _ in range(50):
            console.session_manager.force_wake()
            assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 2.0)
            console.session_manager.force_sleep()
            assert console.session_manager.session.state == ConversationState.SLEEPING
        elapsed = time.time() - t0
        assert elapsed < 15.0, f"50 wake/sleep cycles took {elapsed:.2f}s - too slow"
        assert console.runtime.health().healthy
    finally:
        _silent(console.stop)


@scenario
def test_stress_many_low_confidence_rejections_do_not_leak_or_crash():
    console = _new_console(session_config=WakeSessionConfig(wake_confidence=0.9))
    _silent(console.start)
    try:
        for i in range(200):
            console.event_bus.publish(demo.Event(type="speech_recognized", data={"text": "luno", "confidence": 0.1}))
        time.sleep(0.5)
        assert console.session_manager.session.state == ConversationState.SLEEPING
        assert console.runtime.health().healthy
    finally:
        _silent(console.stop)


@scenario
def test_no_lingering_non_daemon_threads_after_stop():
    before = {t.ident for t in threading.enumerate()}
    console = _new_console(session_config=WakeSessionConfig(session_timeout_s=0.3))
    _silent(console.start)
    _silent(console.simulate_speech, "Luno")
    time.sleep(0.5)
    _silent(console.stop)
    time.sleep(0.3)
    leaked = [t for t in threading.enumerate() if t.ident not in before and not t.daemon and t is not threading.main_thread()]
    assert not leaked, f"non-daemon threads leaked: {[t.name for t in leaked]}"


# ============================================================================
# Architecture rule
# ============================================================================

@scenario
def test_sleeping_speech_never_reaches_planner_or_llm():
    """The single most important behavioral guarantee of this sprint:
    while Sleeping, nothing but wake-word matching happens - no Planner,
    no Tool Manager, no OpenRouter call, at all."""
    console = _new_console()
    _silent(console.start)
    try:
        events_seen = []
        for etype in ("planner_created", "tool_requested", "need_llm_response"):
            console.event_bus.subscribe(etype, lambda e, t=etype: events_seen.append(t))
        for text in ("open chrome", "what is the weather", "play some music", "turn off the lights"):
            _silent(console.simulate_speech, text)
        time.sleep(0.3)
        assert events_seen == [], f"business-logic events leaked through while Sleeping: {events_seen}"
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
    print(f"\n{passed} passed, {failed} failed, {len(SCENARIOS)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
