"""
test_main_bargein.py
=====================

Regression suite for the "add barge-in to luno/main.py's existing
mode_wake_word()" fix - the MINIMAL scope explicitly chosen by the user
over the full Sprint 4 Core Runtime migration (main.py's proven,
daily-driver structure - memory/reminder commands, script confirmations,
pending-action follow-ups, Luno_Brain(), speak(), play_audio() - stays
completely untouched; the only change is an additive concurrency layer
that keeps the mic listening while Luno is thinking/speaking, so saying
"stop"/"batal" actually cuts her off).

`luno/main.py` is the LEGACY, hardware-bound production script (real
mic via sounddevice/speech_recognition, real openWakeWord, real
GPT-SoVITS/F5-TTS TTS server, real OpenAI/OpenRouter client) - none of
those packages are installed in this sandbox and there is no real
hardware to test against here. Rather than skip testing the fix
entirely, or reimplement the logic separately from the real module (a
copy that could silently drift from what actually ships), this suite
stubs ONLY the missing third-party hardware/API packages
(sounddevice, soundfile, speech_recognition, openai, openwakeword) with
minimal fakes and then imports the REAL `luno/main.py` module - so every
assertion below runs against the actual functions/classes that ship,
not a reimplementation. `luno.config` still loads the project's real
`.env` and real `config/*.json` data files exactly like a normal run
(read-only - nothing here writes to them).

What's covered (logic-level, no real audio/GPT calls):
    1. `_barge_in_words()` - default list + BARGE_IN_INTERRUPT_WORDS env
       var override (comma-separated), same convention already used by
       `luno/barge_in/models.py` and `luno/wake_session/models.py`.
    2. `_looks_like_interrupt()` - exact/substring word matching,
       case-insensitivity, non-matches, empty/None input.
    3. `_BargeInState` - thinking/speaking/is_busy() transitions, the
       "speaking set before thinking cleared" no-gap guarantee, and
       `request_interrupt()` setting the event + calling `sd.stop()`.
    4. `process_and_respond_with_bargein()` - exercised end-to-end with
       `Luno_Brain`/`speak` monkeypatched (still real call sites in the
       real function, only the hardware-bound leaves swapped out):
         a. normal turn, no interrupt -> speaks the real reply, returns
            pending-action state exactly like the original
            process_and_respond().
         b. interrupted WHILE THINKING -> the computed reply is dropped
            (never passed to speak()), a short localized ack is spoken
            instead, returns False.
         c. interrupted WHILE SPEAKING -> speak() is still called with
            the real reply (matches "sd.stop() cuts audio already in
            flight", not "skip speaking"), but the function returns
            False so no follow-up chaining happens.
         d. empty input -> returns False immediately, no thread spun up.
    5. `_voice_turn_with_followup()` now calls
       `process_and_respond_with_bargein()` (not the old
       `process_and_respond()`) - confirms the wiring, not just the
       standalone function.
    6. `mode_text_input()` still calls the ORIGINAL `process_and_respond()`
       unchanged - the fix is scoped to voice modes only, exactly per the
       user's "keep everything else in main.py completely untouched"
       instruction.

Run:
    python3 tests/test_main_bargein.py
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import threading
import time
import traceback
import types
from typing import Callable, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ─── Stub the hardware/API packages that aren't installed in this sandbox ───
# (sounddevice, soundfile, speech_recognition, openai - openwakeword is only
# imported lazily inside mode_wake_word() itself, never at module import
# time, so it doesn't need a stub just to import luno.main). Each stub is
# the minimal surface luno/main.py actually touches at import time / in the
# functions under test - NOT a general-purpose fake of the real library.
for _name in ("sounddevice", "soundfile", "speech_recognition", "openai"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

_sd = sys.modules["sounddevice"]
_sd.play = lambda *a, **k: None
_sd.wait = lambda *a, **k: None
_sd.stop = lambda *a, **k: None
_sd.InputStream = object
_sd.OutputStream = object

_sf = sys.modules["soundfile"]
_sf.read = lambda *a, **k: (None, 16000)

_srmod = sys.modules["speech_recognition"]


class _StubRecognizer:
    pass


class _StubMicrophone:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _StubWaitTimeoutError(Exception):
    pass


class _StubAudioData:
    pass


_srmod.Recognizer = _StubRecognizer
_srmod.Microphone = _StubMicrophone
_srmod.WaitTimeoutError = _StubWaitTimeoutError
_srmod.AudioData = _StubAudioData

_openai_mod = sys.modules["openai"]


class _StubOpenAI:
    def __init__(self, *a, **k):
        pass


_openai_mod.OpenAI = _StubOpenAI

import luno.main as m  # noqa: E402  (import AFTER the stubs are installed above)

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _clear_env(*names):
    saved = {n: os.environ.pop(n, None) for n in names}
    return saved


def _restore_env(saved):
    for n, v in saved.items():
        if v is None:
            os.environ.pop(n, None)
        else:
            os.environ[n] = v


# ============================================================================
# 1 - _barge_in_words()
# ============================================================================

@scenario
def test_1_default_interrupt_words_include_the_expected_core_set():
    saved = _clear_env("BARGE_IN_INTERRUPT_WORDS")
    try:
        words = m._barge_in_words()
        for expected in ("stop", "cancel", "wait", "batal", "sudah", "tunggu"):
            assert expected in words, f"expected {expected!r} in default list, got {words}"
    finally:
        _restore_env(saved)


@scenario
def test_2_env_var_overrides_default_list_comma_separated():
    saved = _clear_env("BARGE_IN_INTERRUPT_WORDS")
    try:
        os.environ["BARGE_IN_INTERRUPT_WORDS"] = "freeze, halt , diam"
        words = m._barge_in_words()
        assert words == ["freeze", "halt", "diam"], words
        assert "stop" not in words  # override REPLACES the default, doesn't append
    finally:
        _restore_env(saved)


@scenario
def test_3_blank_env_var_falls_back_to_default():
    saved = _clear_env("BARGE_IN_INTERRUPT_WORDS")
    try:
        os.environ["BARGE_IN_INTERRUPT_WORDS"] = "   "
        words = m._barge_in_words()
        assert "stop" in words
    finally:
        _restore_env(saved)


# ============================================================================
# 2 - _looks_like_interrupt()
# ============================================================================

@scenario
def test_4_exact_interrupt_word_matches():
    # This project's real .env sets BARGE_IN_INTERRUPT_WORDS=stop,cancel
    # (narrower than the built-in default list) - clear it so this checks
    # the DEFAULT word set on its own merits, independent of whatever this
    # deployment happens to have configured right now.
    saved = _clear_env("BARGE_IN_INTERRUPT_WORDS")
    try:
        assert m._looks_like_interrupt("stop")
        assert m._looks_like_interrupt("Stop")
        assert m._looks_like_interrupt("BATAL")
    finally:
        _restore_env(saved)


@scenario
def test_5_interrupt_word_inside_a_longer_phrase_matches():
    saved = _clear_env("BARGE_IN_INTERRUPT_WORDS")
    try:
        assert m._looks_like_interrupt("okay stop now")
        assert m._looks_like_interrupt("tunggu dulu ya")
    finally:
        _restore_env(saved)


@scenario
def test_6_non_interrupt_text_does_not_match():
    assert not m._looks_like_interrupt("turn on the living room light")
    assert not m._looks_like_interrupt("nyalakan lampu kamar")


@scenario
def test_7_empty_or_none_text_does_not_match():
    assert not m._looks_like_interrupt("")
    assert not m._looks_like_interrupt(None)
    assert not m._looks_like_interrupt("   ")


@scenario
def test_8_word_that_merely_contains_an_interrupt_substring_does_not_falsely_match():
    # "waiting" contains "wait" as a substring but is a different word -
    # word-boundary matching must not treat it as a hit.
    assert not m._looks_like_interrupt("I am waiting for the download")


@scenario
def test_8b_interrupt_word_with_trailing_punctuation_from_whisper_still_matches():
    # Regression for the real bug the user hit live: faster-whisper almost
    # always adds punctuation to its transcript ("Stop." not "stop") - the
    # original padded-space matching (f" {w} " in f" {norm} ") silently
    # missed every one of these because there's no space between "stop" and
    # the period. The \b-based matcher must still catch them. Cleared here
    # too, same reasoning as test_4/test_5 above - "wait"/"batal" aren't in
    # this deployment's real (narrower) configured word list.
    saved = _clear_env("BARGE_IN_INTERRUPT_WORDS")
    try:
        assert m._looks_like_interrupt("Stop.")
        assert m._looks_like_interrupt("Stop!")
        assert m._looks_like_interrupt("stop,")
        assert m._looks_like_interrupt("Wait, hold on.")
        assert m._looks_like_interrupt("Batal.")
    finally:
        _restore_env(saved)


# ============================================================================
# 3 - _BargeInState
# ============================================================================

@scenario
def test_9_state_starts_idle_not_busy():
    state = m._BargeInState()
    assert not state.is_busy()
    assert not state.interrupted.is_set()


@scenario
def test_10_thinking_makes_state_busy():
    state = m._BargeInState()
    state.begin_thinking()
    assert state.is_busy()
    state.end_thinking()
    assert not state.is_busy()


@scenario
def test_11_speaking_set_before_thinking_cleared_has_no_not_busy_gap():
    # Mirrors process_and_respond_with_bargein()'s exact call order - this
    # is the same "thinking->speaking gap" class of bug already fixed once
    # in the new architecture's BargeInModule; assert the ordering here
    # keeps is_busy() continuously True across the transition.
    state = m._BargeInState()
    state.begin_thinking()
    assert state.is_busy()
    state.begin_speaking()
    assert state.is_busy()  # both true momentarily - still busy
    state.end_thinking()
    assert state.is_busy()  # speaking alone - still busy, no gap
    state.end_speaking()
    assert not state.is_busy()


@scenario
def test_12_request_interrupt_sets_event_and_calls_sd_stop():
    calls = []
    original_stop = _sd.stop
    _sd.stop = lambda: calls.append(True)
    try:
        state = m._BargeInState()
        state.request_interrupt()
        assert state.interrupted.is_set()
        assert calls == [True]
    finally:
        _sd.stop = original_stop


# ============================================================================
# 3b - _play_on_device() / play_audio() interrupt_event wiring
# ============================================================================
#
# Regression for the SECONDARY_AUDIO_DEVICE bug the user hit live: this
# project's real .env has SECONDARY_AUDIO_DEVICE=10 set, and
# _play_on_device() opens its OWN independent sd.OutputStream (by design -
# see its own docstring) so the primary device's sd.stop() (what
# _BargeInState.request_interrupt() calls) never reached it. The console
# would print "interrupted while speaking" while audio kept playing in
# full on the secondary device. Fixed by threading an optional
# interrupt_event through speak()->play_audio()->_play_on_device(), which
# writes the secondary stream in small chunks and checks the event between
# them instead of one big blocking stream.write(data) call.

import numpy as np  # noqa: E402  (only needed for these two scenarios)


class _FakeOutputStream:
    """Records every chunk passed to write() - lets a test see exactly how
    _play_on_device() sliced up the data, without needing a real audio
    device."""

    def __init__(self, samplerate=None, device=None, channels=None):
        self.written_chunks = []
        self._on_write = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, chunk):
        self.written_chunks.append(chunk)
        if self._on_write:
            self._on_write(self)


@scenario
def test_12b_play_on_device_without_interrupt_event_writes_all_data_at_once():
    # Backward compatibility: every OTHER caller of _play_on_device()
    # (there are none today besides play_audio(), but the contract must
    # hold) that doesn't pass interrupt_event gets byte-for-byte the same
    # behavior as before this fix - one single stream.write(data) call.
    original_output_stream = _sd.OutputStream
    try:
        _sd.OutputStream = _FakeOutputStream
        data = np.zeros((5, 1), dtype="float32")
        # _play_on_device builds its own stream internally - capture it via
        # a small subclass hook since we don't get the instance back directly.
        created = {}
        real_init = _FakeOutputStream.__init__

        def _capturing_init(self, samplerate=None, device=None, channels=None):
            real_init(self, samplerate=samplerate, device=device, channels=channels)
            created["stream"] = self

        _FakeOutputStream.__init__ = _capturing_init
        m._play_on_device(data, 16000, 0, interrupt_event=None)
        assert len(created["stream"].written_chunks) == 1
        assert created["stream"].written_chunks[0] is data
    finally:
        _sd.OutputStream = original_output_stream
        _FakeOutputStream.__init__ = real_init


@scenario
def test_12c_play_on_device_stops_early_once_interrupt_event_is_set():
    original_output_stream = _sd.OutputStream
    try:
        _sd.OutputStream = _FakeOutputStream
        # samplerate=10 -> chunk_size = max(1, int(10*0.1)) = 1 sample/chunk,
        # 5 samples total -> up to 5 write() calls if never interrupted.
        data = np.arange(5, dtype="float32").reshape(5, 1)
        event = threading.Event()
        created = {}
        real_init = _FakeOutputStream.__init__

        def _capturing_init(self, samplerate=None, device=None, channels=None):
            real_init(self, samplerate=samplerate, device=device, channels=channels)
            created["stream"] = self
            # set the interrupt AFTER the 2nd chunk has been written -
            # simulates a real barge-in landing mid-playback.
            self._on_write = lambda s: event.set() if len(s.written_chunks) == 2 else None

        _FakeOutputStream.__init__ = _capturing_init
        m._play_on_device(data, 10, 0, interrupt_event=event)

        assert len(created["stream"].written_chunks) == 2, (
            f"expected exactly 2 chunks written before stopping, got {len(created['stream'].written_chunks)}"
        )
    finally:
        _sd.OutputStream = original_output_stream
        _FakeOutputStream.__init__ = real_init


@scenario
def test_12d_play_audio_passes_interrupt_event_through_to_secondary_thread():
    captured = {}
    original_play_on_device = m._play_on_device
    original_thread = threading.Thread
    original_secondary_device = m.config.SECONDARY_AUDIO_DEVICE

    class _ImmediateThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

        def join(self, timeout=None):
            pass

    def _fake_play_on_device(data, samplerate, device, interrupt_event=None):
        captured["interrupt_event"] = interrupt_event

    try:
        m.config.SECONDARY_AUDIO_DEVICE = "10"  # force the secondary-device branch on regardless of this env's .env
        m._play_on_device = _fake_play_on_device
        threading.Thread = _ImmediateThread
        sentinel_event = threading.Event()

        m.play_audio("fake/path.wav", interrupt_event=sentinel_event)

        assert captured.get("interrupt_event") is sentinel_event
    finally:
        m.config.SECONDARY_AUDIO_DEVICE = original_secondary_device
        m._play_on_device = original_play_on_device
        threading.Thread = original_thread


@scenario
def test_12e_speak_forwards_interrupt_event_to_play_audio():
    captured = {}
    original_play_audio = m.play_audio
    original_tts = m._request_tts_audio
    original_avatar_speaking = m.avatar_dispatch.send_speaking
    original_avatar_speech = m.avatar_dispatch.send_speech
    original_audio_mode = m.config.AUDIO_OUTPUT_MODE

    def _fake_play_audio(path, interrupt_event=None):
        captured["interrupt_event"] = interrupt_event

    try:
        m.config.AUDIO_OUTPUT_MODE = "desktop"
        m._request_tts_audio = lambda speech_text: b"fake-wav-bytes"
        m.play_audio = _fake_play_audio
        m.avatar_dispatch.send_speaking = lambda flag: None
        m.avatar_dispatch.send_speech = lambda *a, **k: None
        sentinel_event = threading.Event()

        m.speak("hello", interrupt_event=sentinel_event)

        assert captured.get("interrupt_event") is sentinel_event
    finally:
        m.config.AUDIO_OUTPUT_MODE = original_audio_mode
        m.play_audio = original_play_audio
        m._request_tts_audio = original_tts
        m.avatar_dispatch.send_speaking = original_avatar_speaking
        m.avatar_dispatch.send_speech = original_avatar_speech


@scenario
def test_12f_speak_with_no_interrupt_event_still_works_default_none():
    # Every OTHER existing caller (reminders, timers, process_and_respond())
    # calls speak(text) with no second argument at all - must still work
    # exactly as before, forwarding interrupt_event=None to play_audio().
    captured = {"called": False}
    original_play_audio = m.play_audio
    original_tts = m._request_tts_audio
    original_avatar_speaking = m.avatar_dispatch.send_speaking
    original_avatar_speech = m.avatar_dispatch.send_speech
    original_audio_mode = m.config.AUDIO_OUTPUT_MODE

    def _fake_play_audio(path, interrupt_event=None):
        captured["called"] = True
        captured["interrupt_event"] = interrupt_event

    try:
        m.config.AUDIO_OUTPUT_MODE = "desktop"
        m._request_tts_audio = lambda speech_text: b"fake-wav-bytes"
        m.play_audio = _fake_play_audio
        m.avatar_dispatch.send_speaking = lambda flag: None
        m.avatar_dispatch.send_speech = lambda *a, **k: None

        m.speak("hello")

        assert captured["called"] is True
        assert captured["interrupt_event"] is None
    finally:
        m.config.AUDIO_OUTPUT_MODE = original_audio_mode
        m.play_audio = original_play_audio
        m._request_tts_audio = original_tts
        m.avatar_dispatch.send_speaking = original_avatar_speaking
        m.avatar_dispatch.send_speech = original_avatar_speech


@scenario
def test_12g_cast_audio_stops_early_and_sends_media_stop_when_interrupted():
    """Regression for the second real bug the user hit live: AUDIO_OUTPUT_MODE=cast
    doesn't use sd.play()/sd.wait() AT ALL (it hands playback off to a Google Cast
    speaker via Home Assistant and just time.sleep()s for the estimated duration) -
    so sd.stop() (what _BargeInState.request_interrupt() calls) had zero effect on
    it. cast_audio() now loops its wait in small steps checking interrupt_event, and
    sends a real media_player.media_stop service call to actually silence the Cast
    speaker once interrupted, instead of sleeping through the full duration."""
    import wave as _wave

    calls = []

    async def _fake_call_service(domain, service, entity_id, data=None):
        calls.append((domain, service, entity_id, data))
        return True

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    original_ha_loop = m.ha_listener.ha_loop
    original_connected = m.ha_client.connected
    original_call_service = m.ha_client.call_service
    original_get_local_ip = m.get_local_ip
    try:
        m.ha_listener.ha_loop = loop
        m.ha_client.connected = True
        m.ha_client.call_service = _fake_call_service
        m.get_local_ip = lambda: "127.0.0.1"

        event = threading.Event()

        def _set_soon():
            time.sleep(0.05)
            event.set()

        threading.Thread(target=_set_soon, daemon=True).start()

        # 5 seconds of silent 16kHz mono PCM - long enough that the OLD
        # (uninterruptible) code would have blocked for ~5s+margin here.
        buf = io.BytesIO()
        with _wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000 * 5)
        audio_bytes = buf.getvalue()

        start = time.time()
        m.cast_audio(audio_bytes, interrupt_event=event)
        elapsed = time.time() - start

        assert elapsed < 4.0, f"cast_audio should return shortly after being interrupted, took {elapsed:.2f}s"
        assert any(
            c[0] == "media_player" and c[1] == "media_stop" and c[2] == m.config.CAST_ENTITY_ID
            for c in calls
        ), f"expected a media_player.media_stop call, got {calls}"
    finally:
        m.ha_listener.ha_loop = original_ha_loop
        m.ha_client.connected = original_connected
        m.ha_client.call_service = original_call_service
        m.get_local_ip = original_get_local_ip
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)


@scenario
def test_12h_cast_audio_without_interrupt_event_behaves_as_before():
    # Backward compatibility: every existing non-barge-in caller of speak()
    # (reminders, timers, process_and_respond()) never passes interrupt_event,
    # so cast_audio's wait must complete in full, exactly like before this fix.
    calls = []

    async def _fake_call_service(domain, service, entity_id, data=None):
        calls.append((domain, service, entity_id, data))
        return True

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    original_ha_loop = m.ha_listener.ha_loop
    original_connected = m.ha_client.connected
    original_call_service = m.ha_client.call_service
    original_get_local_ip = m.get_local_ip
    original_margin = m.config.CAST_PLAYBACK_MARGIN
    try:
        m.ha_listener.ha_loop = loop
        m.ha_client.connected = True
        m.ha_client.call_service = _fake_call_service
        m.get_local_ip = lambda: "127.0.0.1"
        m.config.CAST_PLAYBACK_MARGIN = 0.0

        import wave as _wave
        buf = io.BytesIO()
        with _wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"\x00\x00" * 16000)  # ~1 second
        audio_bytes = buf.getvalue()

        start = time.time()
        m.cast_audio(audio_bytes)  # no interrupt_event at all
        elapsed = time.time() - start

        assert elapsed >= 0.9, f"expected the full ~1s wait to elapse, only took {elapsed:.2f}s"
        assert not any(c[1] == "media_stop" for c in calls), "media_stop should never fire without an interrupt"
    finally:
        m.ha_listener.ha_loop = original_ha_loop
        m.ha_client.connected = original_connected
        m.ha_client.call_service = original_call_service
        m.get_local_ip = original_get_local_ip
        m.config.CAST_PLAYBACK_MARGIN = original_margin
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)


# ============================================================================
# 4 - process_and_respond_with_bargein()
# ============================================================================

class _NullListenerThread:
    """Swap in for threading.Thread inside the function under test so no
    real background thread (which would try to open sr.Microphone()) is
    ever actually started - the listener's own logic is exercised
    separately in scenario 12/the _barge_in_listener scenarios below;
    this section is only about process_and_respond_with_bargein()'s own
    control flow (thinking -> [interrupt check] -> speaking -> [interrupt
    check] -> return)."""

    def __init__(self, target=None, args=(), daemon=None, name=None):
        self._target = target
        self._args = args

    def start(self):
        pass  # deliberately never actually runs _barge_in_listener


@scenario
def test_13_empty_input_returns_false_immediately_no_thread():
    thread_started = []
    original_thread = threading.Thread
    try:
        threading.Thread = lambda *a, **k: thread_started.append(1) or _NullListenerThread(*a, **k)
        result = m.process_and_respond_with_bargein("")
        assert result is False
        assert thread_started == [], "no listener thread should be spun up for empty input"
    finally:
        threading.Thread = original_thread


@scenario
def test_14_normal_turn_no_interrupt_speaks_real_reply_and_returns_pending_state():
    spoken = []
    original_thread = threading.Thread
    original_brain = m.Luno_Brain
    original_speak = m.speak
    original_pending = m.pending_actions.get_pending
    original_avatar_thinking = m.avatar_dispatch.send_thinking
    try:
        threading.Thread = lambda *a, **k: _NullListenerThread(*a, **k)
        m.Luno_Brain = lambda user_input: f"reply to: {user_input}"
        m.speak = lambda text, interrupt_event=None: spoken.append(text)
        m.pending_actions.get_pending = lambda: None
        m.avatar_dispatch.send_thinking = lambda flag: None

        result = m.process_and_respond_with_bargein("turn on the light")

        assert spoken == ["reply to: turn on the light"], spoken
        assert result is False  # no pending action
    finally:
        threading.Thread = original_thread
        m.Luno_Brain = original_brain
        m.speak = original_speak
        m.pending_actions.get_pending = original_pending
        m.avatar_dispatch.send_thinking = original_avatar_thinking


@scenario
def test_15_normal_turn_reports_pending_action_for_followup_chaining():
    original_thread = threading.Thread
    original_brain = m.Luno_Brain
    original_speak = m.speak
    original_pending = m.pending_actions.get_pending
    original_avatar_thinking = m.avatar_dispatch.send_thinking
    try:
        threading.Thread = lambda *a, **k: _NullListenerThread(*a, **k)
        m.Luno_Brain = lambda user_input: "which app do you want?"
        m.speak = lambda text, interrupt_event=None: None
        m.pending_actions.get_pending = lambda: {"kind": "open_app"}
        m.avatar_dispatch.send_thinking = lambda flag: None

        result = m.process_and_respond_with_bargein("open something")

        assert result is True
    finally:
        threading.Thread = original_thread
        m.Luno_Brain = original_brain
        m.speak = original_speak
        m.pending_actions.get_pending = original_pending
        m.avatar_dispatch.send_thinking = original_avatar_thinking


@scenario
def test_16_interrupted_while_thinking_drops_reply_and_speaks_ack_only():
    """Simulates the interrupt landing DURING Luno_Brain() (the blocking,
    non-streaming GPT call) by having the fake Luno_Brain() itself flip the
    interrupt flag before returning - exactly what a real background
    listener thread calling state.request_interrupt() concurrently would
    achieve, just deterministic instead of timing-dependent."""
    spoken = []
    captured_state = {}
    original_thread = threading.Thread
    original_brain = m.Luno_Brain
    original_speak = m.speak
    original_avatar_thinking = m.avatar_dispatch.send_thinking
    original_should_indo = m._should_reply_indo

    def _fake_thread(target=None, args=(), daemon=None, name=None):
        # args[0] is the _BargeInState instance created by the function
        # under test - grab it so this test can flip the SAME flag a real
        # listener thread would flip, instead of only faking Luno_Brain().
        if args:
            captured_state["state"] = args[0]
        return _NullListenerThread(target=target, args=args, daemon=daemon, name=name)

    def _brain_that_gets_interrupted(user_input):
        state = captured_state.get("state")
        if state is not None:
            state.request_interrupt()
        return "this reply must never be spoken"

    try:
        threading.Thread = _fake_thread
        m.Luno_Brain = _brain_that_gets_interrupted
        m.speak = lambda text, interrupt_event=None: spoken.append(text)
        m.avatar_dispatch.send_thinking = lambda flag: None
        m._should_reply_indo = lambda user_lower: False  # force English ack for a deterministic assertion

        result = m.process_and_respond_with_bargein("stop")

        assert result is False
        assert spoken == ["Okay."], f"expected only the short ack to be spoken, got {spoken}"
    finally:
        threading.Thread = original_thread
        m.Luno_Brain = original_brain
        m.speak = original_speak
        m.avatar_dispatch.send_thinking = original_avatar_thinking
        m._should_reply_indo = original_should_indo


@scenario
def test_17_interrupted_while_thinking_uses_indonesian_ack_when_appropriate():
    spoken = []
    captured_state = {}
    original_thread = threading.Thread
    original_brain = m.Luno_Brain
    original_speak = m.speak
    original_avatar_thinking = m.avatar_dispatch.send_thinking
    original_should_indo = m._should_reply_indo

    def _fake_thread(target=None, args=(), daemon=None, name=None):
        if args:
            captured_state["state"] = args[0]
        return _NullListenerThread(target=target, args=args, daemon=daemon, name=name)

    def _brain_that_gets_interrupted(user_input):
        captured_state["state"].request_interrupt()
        return "balasan ini tidak boleh diomongin"

    try:
        threading.Thread = _fake_thread
        m.Luno_Brain = _brain_that_gets_interrupted
        m.speak = lambda text, interrupt_event=None: spoken.append(text)
        m.avatar_dispatch.send_thinking = lambda flag: None
        m._should_reply_indo = lambda user_lower: True

        m.process_and_respond_with_bargein("batal")

        assert spoken == ["Oke."], spoken
    finally:
        threading.Thread = original_thread
        m.Luno_Brain = original_brain
        m.speak = original_speak
        m.avatar_dispatch.send_thinking = original_avatar_thinking
        m._should_reply_indo = original_should_indo


@scenario
def test_18_interrupted_while_speaking_still_speaks_real_reply_but_returns_false():
    """sd.stop() (called by the real listener thread) cuts audio that's
    ALREADY in flight inside speak()/play_audio() - it doesn't prevent
    speak() from being called in the first place. This test's fake speak()
    flips the interrupt flag to simulate "the listener caught an interrupt
    word while this exact speak() call was running" and confirms the
    function still returns False (no follow-up chaining) without needing
    to change what already got passed to speak()."""
    spoken = []
    captured_state = {}
    original_thread = threading.Thread
    original_brain = m.Luno_Brain
    original_speak = m.speak
    original_pending = m.pending_actions.get_pending
    original_avatar_thinking = m.avatar_dispatch.send_thinking

    def _fake_thread(target=None, args=(), daemon=None, name=None):
        if args:
            captured_state["state"] = args[0]
        return _NullListenerThread(target=target, args=args, daemon=daemon, name=name)

    def _speak_that_gets_interrupted(text, interrupt_event=None):
        spoken.append(text)
        # the real speak() call passes interrupt_event=state.interrupted here -
        # confirm the SAME state object's event is what got forwarded.
        assert interrupt_event is captured_state["state"].interrupted
        captured_state["state"].request_interrupt()

    try:
        threading.Thread = _fake_thread
        m.Luno_Brain = lambda user_input: "a longer reply that would normally keep playing"
        m.speak = _speak_that_gets_interrupted
        m.pending_actions.get_pending = lambda: {"kind": "should not matter"}
        m.avatar_dispatch.send_thinking = lambda flag: None

        result = m.process_and_respond_with_bargein("play some music")

        assert spoken == ["a longer reply that would normally keep playing"]
        assert result is False, "must not chain into follow-ups after a mid-speech interrupt"
    finally:
        threading.Thread = original_thread
        m.Luno_Brain = original_brain
        m.speak = original_speak
        m.pending_actions.get_pending = original_pending
        m.avatar_dispatch.send_thinking = original_avatar_thinking


# ============================================================================
# 5/6 - wiring: who calls the barge-in-aware function vs. the original
# ============================================================================

@scenario
def test_19_voice_turn_with_followup_uses_the_bargein_aware_function():
    import inspect
    src = inspect.getsource(m._voice_turn_with_followup)
    assert "process_and_respond_with_bargein(" in src
    assert "process_and_respond(text)" not in src  # the OLD bare call must be gone from this function


@scenario
def test_20_text_mode_still_uses_the_original_unmodified_function():
    import inspect
    src = inspect.getsource(m.mode_text_input)
    assert "process_and_respond(user_input)" in src
    assert "process_and_respond_with_bargein" not in src


@scenario
def test_21_original_process_and_respond_is_completely_unchanged_in_behavior():
    # Same contract as before the fix: no concurrency, no listener thread,
    # just Luno_Brain() -> speak() -> pending check.
    spoken = []
    original_brain = m.Luno_Brain
    original_speak = m.speak
    original_pending = m.pending_actions.get_pending
    original_avatar_thinking = m.avatar_dispatch.send_thinking
    try:
        m.Luno_Brain = lambda user_input: f"plain reply: {user_input}"
        m.speak = lambda text, interrupt_event=None: spoken.append(text)
        m.pending_actions.get_pending = lambda: None
        m.avatar_dispatch.send_thinking = lambda flag: None

        result = m.process_and_respond("hello")

        assert spoken == ["plain reply: hello"]
        assert result is False
    finally:
        m.Luno_Brain = original_brain
        m.speak = original_speak
        m.pending_actions.get_pending = original_pending
        m.avatar_dispatch.send_thinking = original_avatar_thinking


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
