"""
test_barge_in_console.py
==========================

Full end-to-end integration tests for Sprint 3 (barge-in / interruptible
conversation) wired into the real `RuntimeDemoConsole` - real Event Bus,
real `BargeInModule`, `PlannerBridgeModule`, `BehaviorTreeModule`,
`SessionManagerModule`; only OpenRouter and Fish Audio are mocked (no
network, no microphone). Covers the spec's own testing list at the
console level: interrupt while speaking, interrupt while thinking,
interrupt during a fire-and-forget (SOFT) action so the task keeps
running, interrupt during an emergency (CRITICAL, never silently
cancelled), the CONFIRM ask/yes/no flow, pause+resume, streaming
cancellation (no further chunks after cancel), no double-speak /
no duplicate console history, and concurrent interruptions.

Run:
    python3 tests/test_barge_in_console.py
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
from luno.wake_session import WakeSessionConfig  # noqa: E402

# Bug fix (Sprint 5 fallout - env var name collision, NOT a Sprint 5 code
# bug per se): `luno.barge_in.models.BargeInConfig.from_env()` reads
# `BARGE_IN_INTERRUPT_WORDS` - the EXACT same env var name the legacy,
# unrelated `main.py` barge-in feature also uses (see that feature's own
# `.env` entry, `BARGE_IN_INTERRUPT_WORDS=stop,cancel`, added earlier for
# a completely different script). This test file never used to import
# `luno.vision_memory` at all; Sprint 5's memory retrieval sources are
# the first code path here that calls `vm.get_world_state()`, which
# lazily imports `luno.config` (see `luno/vision_memory/api.py`), whose
# own module-level `load_dotenv()` call loads the user's REAL `.env` into
# `os.environ` for the rest of THIS PROCESS - narrowing every
# `BargeInConfig.from_env()` built afterward (in every later scenario) to
# only `["stop", "cancel"]`, silently dropping "wait" and every other
# default interrupt word. `test_confirm_interrupt_then_no_resumes_playback`
# below relies on "wait" being recognized, so it started failing once
# Sprint 5 became the first thing to trigger that lazy import - purely an
# ambient-environment leak between two unrelated systems sharing one env
# var name, not a timing or memory-retrieval correctness issue. Cleared
# for the whole test run, same convention `tests/test_main_bargein.py`
# already uses for the identical reason.
#
# Clearing has to happen AFTER `luno.config`'s `load_dotenv()` has already
# run at least once, or it's pointless: `load_dotenv()` only fires the
# FIRST time `luno.config` is imported (Python caches the module import
# after that), and it happens LAZILY - triggered the first time anything
# (e.g. Sprint 5's `vm.get_world_state()`) actually imports it, which is
# mid-scenario, not at this file's own import time. Clearing before that
# lazy load would just get silently undone the moment it fires later.
# Importing `luno.config` explicitly, right here, forces that one-time
# `load_dotenv()` call to happen NOW - so the clear below actually sticks
# for the rest of this process (later lazy imports of `luno.config`
# elsewhere just hit Python's module cache and never call
# `load_dotenv()` again).
import luno.config  # noqa: E402,F401

_SAVED_BARGE_IN_ENV = {}
for _env_name in (
    "BARGE_IN_INTERRUPT_WORDS", "BARGE_IN_RESUME_WORDS",
    "BARGE_IN_CONFIRM_YES_WORDS", "BARGE_IN_CONFIRM_NO_WORDS",
):
    if _env_name in os.environ:
        _SAVED_BARGE_IN_ENV[_env_name] = os.environ.pop(_env_name)


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


def _new_console(llm_text="OK, doing that.", chunk_delay_s: float = 0.0,
                  playback_delay_s: float = 0.05) -> "demo.RuntimeDemoConsole":
    """Sleep gating turned OFF (`sleep_enabled=False`, Sprint 2's
    "always-on" mode) so every test here can speak straight away without
    first needing a wake word - barge-in itself is entirely orthogonal
    to wake-word gating (see main_runtime_demo.py's own routing
    comments: both subscribe to the same raw SpeechRecognized
    independently)."""
    client = MockOpenRouterClient(canned_text=llm_text, chunk_delay_s=chunk_delay_s)
    fish = MockFishAudioClient(playback_delay_s=playback_delay_s)
    return demo.RuntimeDemoConsole(
        openrouter_client=client, fish_audio_client=fish,
        session_config=WakeSessionConfig(sleep_enabled=False),
    )


class _EventRecorder:
    def __init__(self, bus) -> None:
        self.events: List[Tuple[str, dict]] = []
        self._lock = threading.Lock()
        self.sub_id = bus.subscribe("*", self._on)

    def _on(self, e) -> None:
        with self._lock:
            self.events.append((e.type, dict(e.data)))

    def types(self) -> List[str]:
        with self._lock:
            return [t for t, _ in self.events]

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


# ============================================================================
# FREE mode - interrupt while speaking / while thinking
# ============================================================================

@scenario
def test_free_interrupt_while_speaking_stops_and_acks_once():
    console = _new_console(
        llm_text="This is a slow story about a brave little robot exploring the galaxy",
        chunk_delay_s=0.05, playback_delay_s=1.5,
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me a long story")
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)
        _silent(console.simulate_speech, "stop")
        assert _wait_until(lambda: rec.any_of("stop_playback"), 3.0)
        assert _wait_until(lambda: rec.any_of("speech_playback_cancelled"), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 3.0)
        luno_lines = [t for ch, t in console.conversation_log if ch == "LUNO"]
        # the main reply is logged exactly once (no double-speak), and the
        # ack ("Okay."/"Sure.") is logged exactly once too.
        assert luno_lines.count("This is a slow story about a brave little robot exploring the galaxy") == 1
        acks = [t for t in luno_lines if t in ("Okay.", "Sure.")]
        assert len(acks) == 1
    finally:
        _silent(console.stop)


@scenario
def test_free_interrupt_while_still_thinking_cancels_llm():
    console = _new_console(
        llm_text="one two three four five six seven eight nine ten eleven twelve",
        chunk_delay_s=0.2, playback_delay_s=0.05,
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me something")
        assert _wait_until(lambda: rec.any_of("llm_started"), 3.0)
        # deliberately BEFORE assistant_response/speak_request/playback -
        # this is squarely "interrupt while thinking".
        assert not rec.any_of("speech_playback_started")
        _silent(console.simulate_speech, "cancel")
        assert _wait_until(lambda: rec.any_of("cancel_llm_request"), 3.0)
        assert _wait_until(lambda: rec.any_of("llm_cancelled"), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 3.0)
        # cancelled mid-stream - the full 12-word reply must never fully play.
        assert not rec.any_of("speech_playback_finished")
    finally:
        _silent(console.stop)


# ============================================================================
# SOFT mode - speech stops, the underlying task/LLM call is left alone
# ============================================================================

@scenario
def test_soft_interrupt_during_fire_and_forget_leaves_task_running():
    console = _new_console(
        llm_text="Sure, turning on the lights now",
        chunk_delay_s=0.15, playback_delay_s=0.05,
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        # "turn on the lights" hits `soft_keywords` -> speaking_mode_assigned=soft
        _silent(console.simulate_speech, "turn on the lights")
        assert _wait_until(lambda: rec.any_of("speaking_mode_assigned", lambda d: d.get("mode") == "soft"), 3.0)
        assert _wait_until(lambda: rec.any_of("llm_started"), 3.0)
        _silent(console.simulate_speech, "stop")
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "soft"), 3.0)
        # SOFT must NEVER touch the LLM request - the task keeps running
        # to completion even though the user tried to interrupt.
        assert not rec.any_of("cancel_llm_request")
        assert not rec.any_of("llm_cancelled")
        assert _wait_until(lambda: rec.any_of("llm_finished"), 3.0)
    finally:
        _silent(console.stop)


# ============================================================================
# CONFIRM mode - ask, then yes / no
# ============================================================================

@scenario
def test_confirm_interrupt_then_yes_cancels_everything():
    console = _new_console(
        llm_text="Okay, deleting all your files now, this will take a while",
        chunk_delay_s=0.05, playback_delay_s=1.0,
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "please delete all my files")
        assert _wait_until(lambda: rec.any_of("speaking_mode_assigned", lambda d: d.get("mode") == "confirm"), 3.0)
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)
        turn_rid = console.barge_in_module.current_request_id
        assert turn_rid
        _silent(console.simulate_speech, "stop")
        assert _wait_until(lambda: rec.any_of("pause_playback", lambda d: d.get("request_id") == turn_rid), 3.0)
        assert _wait_until(
            lambda: rec.any_of("speak_request", lambda d: "cancel the operation" in d.get("text", "")), 3.0)
        assert console.barge_in_module.awaiting_confirmation is True
        _silent(console.simulate_speech, "yes")
        # the LLM stream had already finished by the time the user even
        # said "stop" (speech was already playing) - nothing was left to
        # cancel at the LLM level, so `cancel_llm_request` is correctly
        # NOT published here (see the dedicated "while thinking" scenario
        # below for that half of the flow). What confirmed_cancel MUST do
        # here is stop the audio - including the confirm prompt itself,
        # which was still playing concurrently - and the ORIGINAL turn
        # must be genuinely cancelled, never allowed to sneak through to
        # a normal SpeechFinished.
        assert _wait_until(lambda: rec.any_of("stop_playback", lambda d: d.get("request_id") == turn_rid), 3.0)
        assert _wait_until(
            lambda: rec.any_of("speech_playback_cancelled", lambda d: d.get("request_id") == turn_rid), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "confirmed_cancel"), 3.0)
        assert not console.barge_in_module.awaiting_confirmation
        assert not rec.any_of("speech_playback_finished", lambda d: d.get("request_id") == turn_rid)
    finally:
        _silent(console.stop)


@scenario
def test_confirm_interrupt_while_still_thinking_cancels_the_llm_too():
    """A CONFIRM-mode turn where the dangerous action is confirmed to be
    cancelled WHILE the LLM is still generating - this is the half of
    "confirmed_cancel" the previous scenario can't exercise (there, the
    LLM had already finished by the time speech was even playing)."""
    console = _new_console(
        llm_text="deleting one two three four five six seven eight nine ten eleven twelve",
        chunk_delay_s=0.2, playback_delay_s=0.05,
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "please delete everything now")
        assert _wait_until(lambda: rec.any_of("speaking_mode_assigned", lambda d: d.get("mode") == "confirm"), 3.0)
        assert _wait_until(lambda: rec.any_of("llm_started"), 3.0)
        assert not rec.any_of("speech_playback_started")  # still thinking, nothing to pause yet
        _silent(console.simulate_speech, "stop")
        assert _wait_until(lambda: rec.any_of("speak_request", lambda d: "cancel the operation" in d.get("text", "")), 3.0)
        assert console.barge_in_module.awaiting_confirmation is True
        _silent(console.simulate_speech, "confirm")
        assert _wait_until(lambda: rec.any_of("cancel_llm_request"), 3.0)
        assert _wait_until(lambda: rec.any_of("llm_cancelled"), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "confirmed_cancel"), 3.0)
        assert not rec.any_of("speech_playback_finished")
    finally:
        _silent(console.stop)


@scenario
def test_confirm_interrupt_then_no_resumes_playback():
    console = _new_console(
        llm_text="Okay, erasing the memory now, please wait a moment",
        chunk_delay_s=0.05, playback_delay_s=1.0,
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "erase your memory please")
        assert _wait_until(lambda: rec.any_of("speaking_mode_assigned", lambda d: d.get("mode") == "confirm"), 3.0)
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)
        _silent(console.simulate_speech, "wait")
        assert _wait_until(lambda: rec.any_of("pause_playback"), 3.0)
        _silent(console.simulate_speech, "no")
        assert _wait_until(lambda: rec.any_of("resume_playback"), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "declined_cancel"), 3.0)
        # declined - playback must be allowed to actually finish, not be cancelled.
        assert _wait_until(lambda: rec.any_of("speech_playback_finished"), 5.0)
        assert not rec.any_of("speech_playback_cancelled")
        assert not rec.any_of("cancel_llm_request")
    finally:
        _silent(console.stop)


# ============================================================================
# CRITICAL mode - emergency active: pause only, never a silent cancel
# ============================================================================

@scenario
def test_emergency_active_only_pauses_never_cancels():
    console = _new_console(
        llm_text="This is a very long story that keeps going and going and going",
        chunk_delay_s=0.05, playback_delay_s=1.5,
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me a long story")
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)
        # an emergency starts mid-turn...
        console.event_bus.publish(demo.Event(type="smoke_detected", data={"injected": True}))
        assert _wait_until(lambda: console.barge_in_module.emergency_active is True, 2.0)
        _silent(console.simulate_speech, "stop")
        assert _wait_until(lambda: rec.any_of("pause_playback"), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "critical_pause"), 3.0)
        # ...never a hard stop, never a cancelled LLM request, even
        # though the request itself was an ordinary FREE-mode turn.
        assert not rec.any_of("stop_playback")
        assert not rec.any_of("cancel_llm_request")
        assert not rec.any_of("llm_cancelled")
    finally:
        console.barge_in_module.clear_emergency()
        _silent(console.stop)


# ============================================================================
# No double-speak / no duplicate console history for an uninterrupted turn
# ============================================================================

@scenario
def test_uninterrupted_turn_produces_exactly_one_history_line():
    """Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened - see this project's own established
    convention for this class of fix, e.g.
    `docs/change_impact/llm_tts_streaming_activation.md`'s own "3
    pre-existing tests were honestly rewritten"). This test's ORIGINAL
    hard assertion `rec.count("speak_request") == 1` pinned the LEGACY
    (non-streaming) dispatch path specifically - a turn dispatched
    through the now-default streamed path (see
    `luno/incremental_speech.py`) never publishes `speak_request` at all
    (it publishes `speak_stream_chunk`/`is_final` instead, by design -
    see that module's own "RESPONSE-DEPTH-POLICY-SAFE REDESIGN" section),
    so the old assertion would fail for EVERY streamed turn even though
    dedup is working correctly. The actual invariant this test protects -
    "no double-speak, exactly one console history line per turn" - is
    unchanged and is what's still asserted below, now checked against
    WHICHEVER dispatch path actually fired (never assuming which one),
    exactly the same "mode-agnostic, don't hardcode the legacy shape"
    fix `SessionManagerModule._handle_playback_started()` already applied
    for the identical class of gap (see that method's own docstring)."""
    console = _new_console(llm_text="Hello there, nice to meet you", chunk_delay_s=0.0, playback_delay_s=0.05)
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "hi")
        assert _wait_until(lambda: rec.any_of("speech_playback_finished"), 5.0)
        luno_lines = [t for ch, t in console.conversation_log if ch == "LUNO"]
        assert luno_lines.count("Hello there, nice to meet you") == 1
        # exactly one raw conversation record per turn, always - unchanged.
        assert rec.count("assistant_response") == 1
        # exactly one DISPATCH to audio for this turn - via EITHER the
        # legacy single-shot path (`speak_request`) OR the streamed path
        # (one or more `speak_stream_chunk`s sharing the turn's own
        # request_id, the last one carrying `is_final=True`) - never both,
        # never neither.
        dispatched_via_legacy = rec.count("speak_request")
        dispatched_via_stream = rec.count("speak_stream_chunk")
        assert dispatched_via_legacy == 1 or dispatched_via_stream >= 1, (
            f"expected exactly one dispatch path to fire, got speak_request={dispatched_via_legacy} "
            f"speak_stream_chunk={dispatched_via_stream}"
        )
        assert not (dispatched_via_legacy >= 1 and dispatched_via_stream >= 1), "never both dispatch paths for the same turn"
    finally:
        _silent(console.stop)


# ============================================================================
# Concurrent interruptions - must not crash, must end in a coherent state
# ============================================================================

@scenario
def test_concurrent_stop_commands_do_not_crash_the_console():
    console = _new_console(
        llm_text="This is a long enough reply to survive several concurrent interrupt attempts",
        chunk_delay_s=0.05, playback_delay_s=1.5,
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me something long")
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)

        errors: List[BaseException] = []

        def _say_stop():
            try:
                console.simulate_speech("stop")
            except BaseException as ex:  # pragma: no cover
                errors.append(ex)

        threads = [threading.Thread(target=_say_stop) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3.0)

        assert not errors
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 5.0)
        assert _wait_until(lambda: console.runtime.health().healthy, 3.0)
    finally:
        _silent(console.stop)


def main() -> int:
    passed = 0
    failed = 0
    try:
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
    finally:
        # Restore whatever the real environment had (see the
        # `_SAVED_BARGE_IN_ENV` note above) - this module shouldn't leave
        # `os.environ` altered for any test that happens to run after it
        # in the same process.
        for _name in ("BARGE_IN_INTERRUPT_WORDS", "BARGE_IN_RESUME_WORDS",
                       "BARGE_IN_CONFIRM_YES_WORDS", "BARGE_IN_CONFIRM_NO_WORDS"):
            os.environ.pop(_name, None)
        os.environ.update(_SAVED_BARGE_IN_ENV)
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
