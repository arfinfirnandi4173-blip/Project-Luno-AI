"""
test_root_main_bargein.py
===========================

Same regression suite as `tests/test_main_bargein.py`, but run against the
legacy procedural voice-loop script, `E:\\Luno Evo\\legacy_main.py` -
NOT `luno/main.py`.

Why this file exists: this whole barge-in fix was originally implemented
and tested against `luno/main.py`, on the (wrong) assumption that it was
the file the user actually runs. It turned out there are TWO parallel,
independently-runnable procedural scripts in this project:

    - `E:\\Luno Evo\\legacy_main.py` - has `if __name__ ==
      "__main__"`, and is MORE feature-complete (also wires up
      `vision.py`'s `lihat_kamera` tool and `vnyan_engine_bridge`) than
      the copy inside the package. Until Sprint 6 (the production
      launcher), this file WAS the project root's `main.py` - the one
      actually run via `python main.py`, and the one the user was testing
      against when the first version of this fix appeared to do nothing
      at all. Sprint 6 renamed it to `legacy_main.py` (content byte-for-
      byte preserved) and made `main.py` the new event-driven production
      launcher instead - see `luno/bootstrap/` and `main.py` itself.
    - `luno/main.py` - an older/parallel copy inside the package,
      missing the vision/vnyan_engine features, that happened to get all
      of this fix's early iterations because of a wrong assumption about
      which file was "the real one".

Both files got the identical, independently-applied fix (same 3-bug
history: the interrupt-word/punctuation regex bug, the ambient-noise-
recalibrates-on-Luno's-own-voice bug, and the originally-silent exception
swallowing) - this suite exists to prove the ROOT file (the one that
actually matters) is correct too, not just its sibling.

See `tests/test_main_bargein.py` for the full scenario list/rationale -
this file mirrors it 1:1 against the other module.

Run:
    python3 tests/test_root_main_bargein.py
"""

from __future__ import annotations

import asyncio
import importlib.util
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
# Same set as test_main_bargein.py, PLUS cv2/ultralytics - the root main.py
# additionally imports `luno.vision` (not imported by luno/main.py), which
# needs opencv-python + ultralytics at import time.
for _name in ("sounddevice", "soundfile", "speech_recognition", "openai", "cv2", "ultralytics"):
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

_ultralytics_mod = sys.modules["ultralytics"]


class _StubYOLO:
    def __init__(self, *a, **k):
        pass


_ultralytics_mod.YOLO = _StubYOLO

# Load the legacy voice-loop script under a distinct module name so it
# never collides with `luno.main` (which may already be imported elsewhere
# in a full test sweep) - same file-based loading technique already used by
# tests/test_interrupt_routing_fix.py for main_runtime_demo.py. As of
# Sprint 6 this file lives at `legacy_main.py` (renamed from the old
# `main.py`, content otherwise untouched) - `main.py` itself is now the
# production launcher, see `luno/bootstrap/`.
_spec = importlib.util.spec_from_file_location("root_main", os.path.join(_ROOT, "legacy_main.py"))
m = importlib.util.module_from_spec(_spec)
sys.modules["root_main"] = m
_spec.loader.exec_module(m)  # noqa: E402  (import AFTER the stubs are installed above)

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
        assert "stop" not in words
    finally:
        _restore_env(saved)


# ============================================================================
# 2 - _looks_like_interrupt()
# ============================================================================

@scenario
def test_3_exact_and_punctuated_interrupt_words_match():
    # This project's real .env sets BARGE_IN_INTERRUPT_WORDS=stop,cancel
    # (narrower than the built-in default list) - clear it here so this
    # scenario checks the DEFAULT word set on its own merits, independent
    # of whatever this deployment happens to have configured.
    saved = _clear_env("BARGE_IN_INTERRUPT_WORDS")
    try:
        assert m._looks_like_interrupt("stop")
        assert m._looks_like_interrupt("Stop.")
        assert m._looks_like_interrupt("Stop!")
        assert m._looks_like_interrupt("Batal.")
    finally:
        _restore_env(saved)


@scenario
def test_3b_configured_env_words_still_match_with_punctuation():
    # Whatever this deployment's .env ACTUALLY has configured right now
    # must still work correctly through the punctuation-tolerant matcher -
    # this is what's really running in production, not just the default.
    configured = m._barge_in_words()
    assert configured, "expected at least one configured interrupt word"
    first_word = configured[0]
    assert m._looks_like_interrupt(f"{first_word.title()}.")


@scenario
def test_4_non_interrupt_text_does_not_match():
    assert not m._looks_like_interrupt("turn on the living room light")
    assert not m._looks_like_interrupt("I am waiting for the download")


@scenario
def test_5_empty_or_none_text_does_not_match():
    assert not m._looks_like_interrupt("")
    assert not m._looks_like_interrupt(None)


# ============================================================================
# 3 - _BargeInState
# ============================================================================

@scenario
def test_6_speaking_set_before_thinking_cleared_has_no_not_busy_gap():
    state = m._BargeInState()
    state.begin_thinking()
    state.begin_speaking()
    assert state.is_busy()
    state.end_thinking()
    assert state.is_busy()
    state.end_speaking()
    assert not state.is_busy()


@scenario
def test_7_request_interrupt_sets_event_and_calls_sd_stop():
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

import numpy as np  # noqa: E402


class _FakeOutputStream:
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
def test_7b_play_on_device_without_interrupt_event_writes_all_data_at_once():
    original_output_stream = _sd.OutputStream
    try:
        _sd.OutputStream = _FakeOutputStream
        data = np.zeros((5, 1), dtype="float32")
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
def test_7c_play_on_device_stops_early_once_interrupt_event_is_set():
    original_output_stream = _sd.OutputStream
    try:
        _sd.OutputStream = _FakeOutputStream
        data = np.arange(5, dtype="float32").reshape(5, 1)
        event = threading.Event()
        created = {}
        real_init = _FakeOutputStream.__init__

        def _capturing_init(self, samplerate=None, device=None, channels=None):
            real_init(self, samplerate=samplerate, device=device, channels=channels)
            created["stream"] = self
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
def test_7d_play_audio_passes_interrupt_event_through_to_secondary_thread():
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
        m.config.SECONDARY_AUDIO_DEVICE = "10"
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
def test_7e_speak_forwards_interrupt_event_to_play_audio():
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
def test_7f_speak_with_no_interrupt_event_still_works_default_none():
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
def test_7g_cast_audio_stops_early_and_sends_media_stop_when_interrupted():
    """Regression for the second real bug the user hit live: AUDIO_OUTPUT_MODE=cast
    doesn't use sd.play()/sd.wait() AT ALL - so sd.stop() (what
    _BargeInState.request_interrupt() calls) had zero effect on it. cast_audio()
    now loops its wait in small steps checking interrupt_event, and sends a real
    media_player.media_stop service call to actually silence the Cast speaker."""
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
def test_7h_cast_audio_without_interrupt_event_behaves_as_before():
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
            wf.writeframes(b"\x00\x00" * 16000)
        audio_bytes = buf.getvalue()

        start = time.time()
        m.cast_audio(audio_bytes)
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
    def __init__(self, target=None, args=(), daemon=None, name=None):
        self._target = target
        self._args = args

    def start(self):
        pass


@scenario
def test_8_normal_turn_no_interrupt_speaks_real_reply():
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
        assert result is False
    finally:
        threading.Thread = original_thread
        m.Luno_Brain = original_brain
        m.speak = original_speak
        m.pending_actions.get_pending = original_pending
        m.avatar_dispatch.send_thinking = original_avatar_thinking


@scenario
def test_9_interrupted_while_thinking_drops_reply_and_speaks_ack_only():
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
        return "this reply must never be spoken"

    try:
        threading.Thread = _fake_thread
        m.Luno_Brain = _brain_that_gets_interrupted
        m.speak = lambda text, interrupt_event=None: spoken.append(text)
        m.avatar_dispatch.send_thinking = lambda flag: None
        m._should_reply_indo = lambda user_lower: False

        result = m.process_and_respond_with_bargein("stop")

        assert result is False
        assert spoken == ["Okay."], spoken
    finally:
        threading.Thread = original_thread
        m.Luno_Brain = original_brain
        m.speak = original_speak
        m.avatar_dispatch.send_thinking = original_avatar_thinking
        m._should_reply_indo = original_should_indo


@scenario
def test_10_interrupted_while_speaking_still_speaks_but_returns_false():
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
        assert interrupt_event is captured_state["state"].interrupted
        captured_state["state"].request_interrupt()

    try:
        threading.Thread = _fake_thread
        m.Luno_Brain = lambda user_input: "a longer reply"
        m.speak = _speak_that_gets_interrupted
        m.pending_actions.get_pending = lambda: {"kind": "should not matter"}
        m.avatar_dispatch.send_thinking = lambda flag: None

        result = m.process_and_respond_with_bargein("play some music")

        assert spoken == ["a longer reply"]
        assert result is False
    finally:
        threading.Thread = original_thread
        m.Luno_Brain = original_brain
        m.speak = original_speak
        m.pending_actions.get_pending = original_pending
        m.avatar_dispatch.send_thinking = original_avatar_thinking


# ============================================================================
# 5/6 - wiring
# ============================================================================

@scenario
def test_11_voice_turn_with_followup_uses_the_bargein_aware_function():
    import inspect
    src = inspect.getsource(m._voice_turn_with_followup)
    assert "process_and_respond_with_bargein(" in src
    assert "process_and_respond(text)" not in src


@scenario
def test_12_text_mode_still_uses_the_original_unmodified_function():
    import inspect
    src = inspect.getsource(m.mode_text_input)
    assert "process_and_respond(user_input)" in src
    assert "process_and_respond_with_bargein" not in src


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
