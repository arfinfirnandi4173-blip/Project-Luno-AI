"""
test_real_fish_audio_console.py
==================================

The master proof for the "real GPT-SoVITS/F5-TTS adapter lifecycle"
bug fix: wires a `RealFishAudioClient` - with only its two I/O
boundaries faked (`session` for the HTTP synthesis call, `play_audio_fn`
for actual audio output; everything else is the client's own real
logic) - into the FULL `RuntimeDemoConsole` stack (real Event Bus, real
`SessionManagerModule`/Wake Session, real `BargeInModule`, real
`BehaviorTreeModule`), exactly mirroring `tests/test_barge_in_console.py`
and `tests/test_wake_barge_in_integration.py`'s own conventions but
swapping `MockFishAudioClient` for the real adapter's client.

The whole point: Wake Session, Barge-In, and Runtime status must behave
IDENTICALLY regardless of which TTS backend is plugged in. Every
scenario below has a direct sibling already passing against
`MockFishAudioClient` in the two files above - this file re-runs the
same shape of check with the real client's I/O faked, proving nothing
downstream needed to change.

Covers the task's regression list, items #2-#5 specifically (the parts
that require the full console stack, not just the adapter in
isolation - see `luno/adapters/tests/test_fish_audio_real.py` for the
adapter-only proof of items #1, #6, #7, #10, #11, #12):
    2. Runtime /status reports Talking=True while audio is actually
       playing - see the dedicated note in
       `test_session_speaking_state_tracks_real_playback_window` below
       on exactly what "Talking" means here.
    3. Conversation Session enters Speaking during real playback.
    4. Interrupt ("stop") spoken during real playback now succeeds.
    5. Keyboard interrupt continues to work.

Run:
    python3 tests/test_real_fish_audio_console.py
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from typing import Any, Callable, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import importlib.util

_spec = importlib.util.spec_from_file_location("main_runtime_demo", os.path.join(_ROOT, "main_runtime_demo.py"))
demo = importlib.util.module_from_spec(_spec)
sys.modules["main_runtime_demo"] = demo
_spec.loader.exec_module(demo)

from luno.adapters import MockOpenRouterClient, RealFishAudioClient, RealFishAudioConfig  # noqa: E402
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


# ============================================================================
# Fakes - the exact same technique as luno/adapters/tests/test_fish_audio_real.py.
# `synthesis_delay_s` and `playback_delay_s` are independently controllable
# so tests can tell the two phases apart in time.
# ============================================================================

class _FakeResponse:
    def __init__(self, content: bytes = b"FAKE-WAV-BYTES"):
        self.status_code = 200
        self.content = content

    def json(self) -> dict:
        return {}


class _FakeSession:
    def __init__(self, delay_s: float = 0.05):
        self.delay_s = delay_s
        self.calls = 0

    def post(self, url: str, json: Any = None, timeout: Any = None):
        self.calls += 1
        time.sleep(self.delay_s)
        return _FakeResponse()


def _make_fake_player(duration_s: float = 0.05):
    from luno.adapters.fish_audio import PlaybackCancelled

    def _play_audio(wav_bytes: bytes, control) -> None:
        control.on_playback_start()
        slept = 0.0
        step = 0.005
        while slept < duration_s:
            if control.cancel.is_set():
                raise PlaybackCancelled("playback cancelled")
            if control.pause.is_set():
                time.sleep(step)
                continue
            time.sleep(step)
            slept += step

    return _play_audio


def _new_real_console(llm_text="OK, doing that.", chunk_delay_s: float = 0.0,
                       synthesis_delay_s: float = 0.05, playback_delay_s: float = 0.3,
                       session_config: Optional[WakeSessionConfig] = None) -> "demo.RuntimeDemoConsole":
    """Same shape as `_new_console()` in the two sibling test files, but
    the Fish Audio side is a REAL `RealFishAudioClient` with only its
    HTTP session and audio-output function faked - everything else
    (synthesis polling, cancel-during-synthesis, per-call playback
    control, threading) is the client's own genuine code path."""
    or_client = MockOpenRouterClient(canned_text=llm_text, chunk_delay_s=chunk_delay_s)
    fish_client = RealFishAudioClient(
        config=RealFishAudioConfig(reference_audio="ref.wav", reference_text="hi"),
        session=_FakeSession(delay_s=synthesis_delay_s),
        play_audio_fn=_make_fake_player(playback_delay_s),
    )
    return demo.RuntimeDemoConsole(
        openrouter_client=or_client, fish_audio_client=fish_client,
        session_config=session_config or WakeSessionConfig(sleep_enabled=False),
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
    """Mirrors `tests/test_wake_barge_in_integration.py`'s own `_wake()`
    exactly - waits for the session to genuinely reach LISTENING (i.e.
    for the "Yes?" wake-ack's own real-backend synthesis+playback to
    fully finish) before returning, so a test's own `_EventRecorder`
    (created AFTER calling this) never mistakes the ack's
    `speech_playback_started`/`finished` for the real turn's."""
    _silent(console.simulate_speech, "Luno")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 5.0)


# ============================================================================
# 1 (console-level restatement) - full lifecycle through the real console
# ============================================================================

@scenario
def test_normal_turn_full_lifecycle_through_real_console():
    console = _new_real_console()
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "hi")
        assert _wait_until(lambda: rec.any_of("speech_playback_finished"), 5.0)
        assert rec.count("speech_playback_started") == 1
        assert rec.count("speech_playback_finished") == 1
        assert not rec.any_of("speech_playback_cancelled")
        luno_lines = [t for ch, t in console.conversation_log if ch == "LUNO"]
        assert luno_lines.count("OK, doing that.") == 1
    finally:
        _silent(console.stop)


# ============================================================================
# 3 - Conversation Session enters Speaking during REAL playback, and only
#     for the actual duration of playback (not before synthesis finishes,
#     not after playback ends) - this is the exact mechanism the bug
#     report's "Conversation session : sleeping ... while Luno is audibly
#     speaking" symptom is about, proven here with a real client's I/O
#     boundaries faked instead of the mock's instant no-synthesis path.
# ============================================================================

@scenario
def test_session_enters_speaking_only_once_real_playback_actually_starts():
    """Wake Session's own design (`wake_session/manager.py`, on this
    task's do-not-modify list) keys the THINKING -> SPEAKING transition
    off `speak_request` itself (the moment a reply is committed to being
    spoken), not off `SpeechPlaybackStarted` - so "speaking" correctly
    covers the ENTIRE synthesis+playback window, a superset of "audio is
    actually coming out of the speaker" rather than an exact match. That
    is by design and is unaffected by which TTS backend is plugged in.
    What this test actually proves - the thing that WOULD break under
    the original bug - is that the session never falls OUT of "speaking"
    (back to sleeping/idle) while real audio is still playing, and that
    it correctly leaves "speaking" once playback genuinely finishes.

    Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened) - `ENABLE_LLM_TTS_STREAMING` now defaults
    to `True` (see `luno/config.py`), under which a turn never publishes
    `speak_request` at all (see `wake_session/manager.py`'s own
    "Production-Safe LLM -> TTS Streaming Activation sprint" docstring
    note - a PRE-EXISTING, already-shipped fix from a prior sprint, not
    something this sprint introduces). That module's own
    `_handle_playback_started()` already and intentionally makes the
    THINKING/IDLE -> SPEAKING transition at `speech_playback_started`
    instead for such a turn, "a harmless no-op for the legacy path"
    (its own docstring's words) since the legacy path is already
    SPEAKING by then. That means the two-stage proof this test
    originally made - "speaking" holds both at commit-to-speak time
    (before synthesis even starts) AND separately at playback-start time
    - only has two DISTINCT checkpoints under the legacy path; under
    streaming the earlier checkpoint doesn't exist; both collapse to
    the single `speech_playback_started` moment. This is proven directly
    from `manager.py`'s own source above, not guessed. The invariant
    this test actually cares about - never falls out of "speaking" while
    real audio plays, always correctly leaves "speaking" once playback
    genuinely finishes - is unchanged and still fully verified below."""
    console = _new_real_console(synthesis_delay_s=0.3, playback_delay_s=0.3)
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me something")

        assert _wait_until(lambda: rec.any_of("speak_request") or rec.any_of("speech_playback_started"), 3.0)
        committed_state = console.session_manager.status_snapshot()["state"]
        assert committed_state == "speaking", f"expected speaking once the turn committed to speak, got {committed_state}"

        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 3.0)
        mid_playback_state = console.session_manager.status_snapshot()["state"]
        assert mid_playback_state == "speaking", f"expected speaking during real playback, got {mid_playback_state}"

        assert _wait_until(lambda: rec.any_of("speech_playback_finished"), 3.0)
        after_state = console.session_manager.status_snapshot()["state"]
        assert after_state != "speaking", f"still speaking after real playback finished: {after_state}"
        assert after_state != "sleeping"  # sleep_enabled=False - never sleeps mid-session
    finally:
        _silent(console.stop)


@scenario
def test_session_speaking_state_tracks_real_playback_window():
    """Polls session state repeatedly across the whole real-playback
    window (unlike the scenario above, which only samples at the
    boundaries) - proves "speaking" is held for the actual duration,
    not a single instantaneous tick. This is the console-level
    equivalent of `Runtime /status reports Talking=True while audio is
    actually playing` (item #2): the field the console's own /status
    panel and this whole architecture use as the authoritative "is Luno
    currently producing audio" signal is Wake Session's
    ConversationState (printed as `Conversation session : speaking`),
    not the Behavior Tree's own `talking` flag - confirmed by direct
    inspection to be near-instantaneous by design (fires and clears
    within the same tick) EVEN with the original, already-correct
    `MockFishAudioClient`, since `BehaviorTreeModule._speak()` is a
    fire-and-forget publish rather than a blocking wait for playback to
    finish. That characteristic is unchanged by, and unrelated to,
    which TTS backend is plugged in, and Behavior Tree is on this task's
    explicit do-not-modify list - so this test targets the field that
    actually is meant to track real playback duration end-to-end."""
    console = _new_real_console(synthesis_delay_s=0.05, playback_delay_s=0.5)
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me something")
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 3.0)

        samples = []
        finished = threading.Event()
        console.event_bus.subscribe("speech_playback_finished", lambda e: finished.set())
        deadline = time.time() + 1.0
        while time.time() < deadline and not finished.is_set():
            samples.append(console.session_manager.status_snapshot()["state"])
            time.sleep(0.03)

        assert samples, "never sampled session state during playback"
        speaking_fraction = sum(1 for s in samples if s == "speaking") / len(samples)
        assert speaking_fraction > 0.7, f"session state was 'speaking' for only {speaking_fraction:.0%} of the real playback window: {samples}"
    finally:
        _silent(console.stop)


# ============================================================================
# 4 - voice interrupt during REAL playback, without repeating the wake word
# ============================================================================

@scenario
def test_voice_interrupt_during_real_playback_succeeds_without_repeating_wake_word():
    console = _new_real_console(
        llm_text="this is a long enough reply that real playback is still going when we interrupt it",
        synthesis_delay_s=0.05, playback_delay_s=1.5,
        session_config=WakeSessionConfig(),  # real wake-gated default (sleep_enabled=True)
    )
    _silent(console.start)
    try:
        _wake(console)
        wake_count_after_waking = console.session_manager.session.wake_count
        # created AFTER waking - the wake ack's own speech_playback_started/
        # finished for "Yes?" must not be mistaken for this turn's (same
        # pitfall already documented in tests/test_wake_barge_in_integration.py).
        rec = _EventRecorder(console.event_bus)

        _silent(console.simulate_speech, "tell me a long story")
        assert _wait_until(lambda: rec.any_of("speech_playback_started"), 5.0)
        assert console.session_manager.status_snapshot()["state"] == "speaking"

        # no wake word here - a bare interrupt phrase, same as any other
        # turn once a session is open.
        _silent(console.simulate_speech, "stop")
        assert _wait_until(lambda: rec.any_of("stop_playback"), 3.0)
        assert _wait_until(lambda: rec.any_of("speech_playback_cancelled"), 3.0)
        assert _wait_until(lambda: rec.any_of("barge_in_action", lambda d: d.get("action") == "free"), 3.0)

        # confirm the interrupt was NOT mistaken for a fresh wake event.
        assert console.session_manager.session.wake_count == wake_count_after_waking
        assert not rec.any_of("wake_word_detected")
    finally:
        _silent(console.stop)


@scenario
def test_voice_interrupt_while_still_synthesizing_real_speech_succeeds():
    """The gap this whole bug fix exists to close: an interrupt spoken
    while the real backend is still mid-synthesis (HTTP round trip in
    flight, nothing audible yet) must still be honored - proving
    `RealFishAudioClient`'s cancel-during-synthesis path (see
    `luno/adapters/fish_audio_real.py`) is reachable end-to-end through
    the full console/barge-in stack, not just in the adapter's own unit
    tests.

    Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened) - `ENABLE_LLM_TTS_STREAMING` now defaults
    to `True` (see `luno/config.py`), so this single-sentence reply
    dispatches via `speak_stream_chunk`, never `speak_request` (see
    `luno/incremental_speech.py`'s own docstring - a short reply that
    settles entirely as the first early-dispatched sentence never
    reaches the `build_dual_response()` reconciliation step that would
    otherwise fire a second event). The actual thing under test -
    "commit to speak" has happened, synthesis is genuinely still in
    flight, and interrupting now must cancel cleanly before any audio
    plays - is unaffected by which event name carried that commitment,
    so this now accepts either."""
    console = _new_real_console(
        llm_text="a reply that will be interrupted before synthesis even completes",
        synthesis_delay_s=1.0, playback_delay_s=0.2,
        session_config=WakeSessionConfig(sleep_enabled=False),
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "tell me something slow to synthesize")
        assert _wait_until(lambda: rec.any_of("speak_request") or rec.any_of("speak_stream_chunk"), 3.0)
        assert not rec.any_of("speech_playback_started")  # still inside the 1.0s fake synthesis window

        _silent(console.simulate_speech, "stop")
        assert _wait_until(lambda: rec.any_of("speech_playback_cancelled"), 3.0)
        # never should have started, since it was cancelled during synthesis
        assert not rec.any_of("speech_playback_started")
        assert not rec.any_of("speech_playback_finished")
    finally:
        _silent(console.stop)


# ============================================================================
# 5 - keyboard interrupt continues to work, unaffected by the TTS backend
# ============================================================================

@scenario
def test_keyboard_interrupt_still_works_with_real_backend():
    client = MockOpenRouterClient(canned_text="a slow reply that takes a while to stream out", chunk_delay_s=0.25)
    console = _new_real_console()
    console.openrouter_adapter.client = client  # swap for a slow-streaming one, same as the sibling test
    _silent(console.start)
    try:
        request_id = "kbd-cancel-test-real"
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
# Consecutive replies do not overlap (#8), streaming still works (#9) -
# console-level restatement using the real client's I/O faked.
# ============================================================================

@scenario
def test_consecutive_real_playback_turns_do_not_overlap():
    console = _new_real_console(synthesis_delay_s=0.02, playback_delay_s=0.1)
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        for i in range(5):
            _silent(console.simulate_speech, f"turn number {i}")
            assert _wait_until(lambda: rec.count("speech_playback_finished") == i + 1, 3.0)
        # every started/finished pair is 1:1, in order, no overlap left dangling.
        assert rec.count("speech_playback_started") == 5
        assert rec.count("speech_playback_finished") == 5
        assert not rec.any_of("speech_playback_cancelled")
    finally:
        _silent(console.stop)


@scenario
def test_streaming_reply_still_speaks_through_real_backend():
    console = _new_real_console(
        llm_text="one two three four five six seven eight", chunk_delay_s=0.03,
        synthesis_delay_s=0.02, playback_delay_s=0.1,
    )
    _silent(console.start)
    try:
        rec = _EventRecorder(console.event_bus)
        _silent(console.simulate_speech, "count for me")
        assert _wait_until(lambda: rec.any_of("llm_started"), 3.0)
        assert _wait_until(lambda: rec.count("llm_chunk") >= 2, 3.0)
        assert _wait_until(lambda: rec.any_of("llm_finished"), 3.0)
        assert _wait_until(lambda: rec.any_of("speech_playback_finished"), 5.0)
        luno_lines = [t for ch, t in console.conversation_log if ch == "LUNO"]
        assert "one two three four five six seven eight" in luno_lines
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
