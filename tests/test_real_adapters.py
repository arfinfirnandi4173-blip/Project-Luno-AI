"""
test_real_adapters.py
========================

Unit tests for the Sprint 6 "real" adapter wrapper classes
(`luno/adapters/real_home_assistant.py`, `real_unity.py`, `real_vision.py`,
`real_whisper.py`) - the pure translation/mapping logic each one adds on
top of an EXISTING, unmodified legacy module (`luno.ha_client`,
`luno.vnyan_engine_bridge`/`luno.vnyan_bridge`, `luno.vision`, and
`legacy_main.py`'s `transcribe_audio()`).

These never touch real hardware/network - every legacy dependency is
replaced with a small fake object exposing the exact same public
surface the wrapper actually calls, so what's under test is genuinely
the WRAPPER's own logic (event-shape translation, config-driven
selection, honest-mapping choices), not whether a microphone/camera/
websocket happens to be available in this sandbox (real reachability is
covered by `luno/bootstrap/health.py`'s own checks - see
`tests/test_production_launcher.py`).

Run:
    python3 tests/test_real_adapters.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import traceback
from typing import Callable, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


# ============================================================================
# real_home_assistant.py
# ============================================================================

class _FakeHAClient:
    """Mirrors `luno.ha_client.HomeAssistantClient`'s public async
    surface exactly - same method names/signatures/return shapes
    documented in that file (list-of-state-dicts from `get_states()`,
    plain bool from `call_service()`)."""

    def __init__(self) -> None:
        self.connected = True
        self.service_calls: list = []
        self.disconnected = False

    async def connect(self):
        return True

    async def subscribe_to_events(self):
        return None

    async def listen_and_dispatch(self, callback):
        # Fires exactly one fake state_changed event, then returns -
        # simulates a single message before disconnect, matching the
        # real client's own "loop until self.connected is False" shape
        # closely enough for the wrapper's own dispatch logic to be
        # exercised end to end.
        await callback({"entity_id": "light.kitchen", "old_state": {"state": "off"}, "new_state": {"state": "on"}})

    async def get_states(self):
        return [{"entity_id": "light.kitchen", "state": "off"}, {"entity_id": "switch.fan", "state": "on"}]

    async def call_service(self, domain, service, entity_id, data=None):
        self.service_calls.append((domain, service, entity_id, dict(data or {})))
        return True

    async def disconnect(self):
        self.disconnected = True


class _RecordingListener:
    def __init__(self) -> None:
        self.state_changes: list = []

    def on_state_changed(self, entity_id, old_state, new_state) -> None:
        self.state_changes.append((entity_id, old_state, new_state))

    def on_automation_triggered(self, name, data=None) -> None:
        pass


@scenario
def test_real_ha_source_dispatches_state_changed_to_listener():
    from luno.adapters.real_home_assistant import RealHomeAssistantSource

    fake_client = _FakeHAClient()
    source = RealHomeAssistantSource(ha_client=fake_client)
    listener = _RecordingListener()

    source.start(listener)
    deadline = time.time() + 3.0
    while not listener.state_changes and time.time() < deadline:
        time.sleep(0.02)
    source.stop()

    assert listener.state_changes, "expected at least one on_state_changed call"
    entity_id, old_state, new_state = listener.state_changes[0]
    assert entity_id == "light.kitchen"
    assert old_state == "off"
    assert new_state == "on"


@scenario
def test_real_ha_client_wraps_call_service_result_as_dict():
    from luno.adapters.real_home_assistant import RealHomeAssistantClient, RealHomeAssistantSource

    fake_client = _FakeHAClient()
    source = RealHomeAssistantSource(ha_client=fake_client)
    source.start(_RecordingListener())
    deadline = time.time() + 3.0
    while source.loop is None and time.time() < deadline:
        time.sleep(0.02)
    assert source.loop is not None, "source never got its asyncio loop running"

    client = RealHomeAssistantClient(source)
    result = client.call_service("light", "turn_on", entity_id="light.kitchen", data={"brightness": 200})
    source.stop()

    assert result["success"] is True
    assert fake_client.service_calls == [("light", "turn_on", "light.kitchen", {"brightness": 200})]


@scenario
def test_real_ha_client_reports_failure_before_source_connects():
    from luno.adapters.real_home_assistant import RealHomeAssistantClient, RealHomeAssistantSource

    source = RealHomeAssistantSource(ha_client=_FakeHAClient())  # never started
    client = RealHomeAssistantClient(source)
    result = client.call_service("light", "turn_on", entity_id="light.kitchen")
    assert result["success"] is False
    assert "error" in result


# ============================================================================
# real_unity.py
# ============================================================================

class _FakeEngineBridge:
    def __init__(self) -> None:
        self.thinking = None
        self.speaking = None
        self.expressions: list = []

    def set_thinking(self, value):
        self.thinking = value

    def set_speaking(self, value):
        self.speaking = value

    def send_expression(self, tag):
        self.expressions.append(tag)


@scenario
def test_real_unity_client_maps_thinking_animation_to_engine_flags():
    from luno.adapters.real_unity import RealUnityClient

    client = RealUnityClient.__new__(RealUnityClient)  # bypass __init__'s luno.config/bridge import
    fake_bridge = _FakeEngineBridge()
    client._bridge = fake_bridge
    client._config = type("cfg", (), {"VNYAN_OSC_HOST": "127.0.0.1", "VNYAN_OSC_PORT": 39540})()

    client.send_animation("thinking")
    assert fake_bridge.thinking is True
    assert fake_bridge.speaking is False

    client.send_animation("speaking")
    assert fake_bridge.thinking is False
    assert fake_bridge.speaking is True

    client.send_animation("idle")
    assert fake_bridge.thinking is False
    assert fake_bridge.speaking is False


@scenario
def test_real_unity_client_set_emotion_sends_expression():
    from luno.adapters.real_unity import RealUnityClient

    client = RealUnityClient.__new__(RealUnityClient)
    fake_bridge = _FakeEngineBridge()
    client._bridge = fake_bridge
    client._config = type("cfg", (), {})()

    client.set_emotion("happy")
    assert fake_bridge.expressions == ["happy"]


@scenario
def test_real_unity_client_ping_false_when_unconfigured():
    from luno.adapters.real_unity import RealUnityClient

    client = RealUnityClient.__new__(RealUnityClient)
    client._bridge = _FakeEngineBridge()
    client._config = type("cfg", (), {"VNYAN_OSC_HOST": None, "VNYAN_OSC_PORT": None})()
    assert client.ping() is False


# ============================================================================
# real_vision.py
# ============================================================================

class _FakeVisionModule:
    def __init__(self) -> None:
        self._labels = (["cup", "laptop"], 1.2)
        self._description = ("A cup sits on the desk.", 2.5)
        self.watch_started = False
        self.vision_watch_started = False
        self.released = False

    def start_watch(self):
        self.watch_started = True

    def stop_watch(self):
        self.watch_started = False

    def start_vision_watch(self):
        self.vision_watch_started = True

    def stop_vision_watch(self):
        self.vision_watch_started = False

    def release_camera(self):
        self.released = True

    def last_detections(self):
        return self._labels

    def last_vision_description(self):
        return self._description

    def last_presence_detection_error(self):
        """P0.6.3 - `RealVisionSource._poll_once()` now checks this
        additive getter (see `luno/vision.py`) to distinguish a genuine
        detector failure from "nothing detected"; this fake stand-in
        reports no failure by default, matching this test's own
        "detections/description forwarded normally" scenario."""
        return None


@scenario
def test_real_vision_source_forwards_detections_and_description_once():
    from luno.adapters.real_vision import RealVisionSource

    source = RealVisionSource.__new__(RealVisionSource)
    fake_vision = _FakeVisionModule()
    source._vision = fake_vision
    source._config = type("cfg", (), {"CAMERA_VISION_WATCH_ENABLED": False, "CAMERA_WATCH_INTERVAL_S": 5.0})()
    source._poll_interval_s = 100.0  # never re-poll during this test
    source._listener = None
    source._stop_flag = threading.Event()
    source._poll_thread = None
    source._last_seen_description = None

    seen_detections = []
    seen_descriptions = []

    class _Listener:
        def on_detections(self, detections):
            seen_detections.append(detections)

        def on_scene_description(self, description):
            seen_descriptions.append(description)

        def on_frame(self, frame_meta=None):
            pass

    source._listener = _Listener()
    source._poll_once()

    assert seen_detections == [[{"label": "cup"}, {"label": "laptop"}]]
    assert seen_descriptions == ["A cup sits on the desk."]

    # calling _poll_once() again with the SAME description must not
    # re-publish it (dedup by "did the description actually change") -
    # detections have no such dedup (the adapter's own label-diffing
    # already handles that, see vision.py's on_detections()).
    source._poll_once()
    assert seen_descriptions == ["A cup sits on the desk."]
    assert len(seen_detections) == 2


@scenario
def test_real_vision_source_stop_releases_camera():
    from luno.adapters.real_vision import RealVisionSource

    source = RealVisionSource.__new__(RealVisionSource)
    fake_vision = _FakeVisionModule()
    source._vision = fake_vision
    source._stop_flag = threading.Event()
    source._poll_thread = None
    source._listener = object()

    source.stop()
    assert fake_vision.released is True
    assert source._listener is None


# ============================================================================
# real_whisper.py
# ============================================================================

@scenario
def test_real_whisper_source_calls_listener_in_order_for_nonempty_text():
    from luno.adapters.real_whisper import RealWhisperSource

    calls = []

    class _Listener:
        def on_speech_started(self):
            calls.append("started")

        def on_speech_recognized(self, text, confidence=None):
            calls.append(("recognized", text))

        def on_speech_finished(self):
            calls.append("finished")

    source = RealWhisperSource.__new__(RealWhisperSource)

    class _FakeMicCtx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeSr:
        WaitTimeoutError = TimeoutError

        def Microphone(self):
            return _FakeMicCtx()

    class _FakeRecognizer:
        def listen(self, source_, timeout=None, phrase_time_limit=None):
            return "fake-audio-data"

    class _FakeLegacy:
        def transcribe_audio(self, audio):
            assert audio == "fake-audio-data"
            return "  hello luno  "

    source._sr = _FakeSr()
    source._legacy = _FakeLegacy()
    source._recognizer = _FakeRecognizer()
    source._listener = _Listener()

    source._listen_and_transcribe_once()

    assert calls == ["started", ("recognized", "hello luno"), "finished"]


@scenario
def test_real_whisper_source_skips_empty_transcription():
    from luno.adapters.real_whisper import RealWhisperSource

    calls = []

    class _Listener:
        def on_speech_started(self):
            calls.append("started")

        def on_speech_recognized(self, text, confidence=None):
            calls.append(("recognized", text))

        def on_speech_finished(self):
            calls.append("finished")

    source = RealWhisperSource.__new__(RealWhisperSource)

    class _FakeMicCtx:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _FakeSr:
        WaitTimeoutError = TimeoutError

        def Microphone(self):
            return _FakeMicCtx()

    class _FakeRecognizer:
        def listen(self, source_, timeout=None, phrase_time_limit=None):
            return "silence"

    class _FakeLegacy:
        def transcribe_audio(self, audio):
            return "   "

    source._sr = _FakeSr()
    source._legacy = _FakeLegacy()
    source._recognizer = _FakeRecognizer()
    source._listener = _Listener()

    source._listen_and_transcribe_once()

    assert calls == [], "empty/whitespace-only transcriptions must not publish anything"


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
