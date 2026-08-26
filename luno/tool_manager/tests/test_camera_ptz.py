"""
test_camera_ptz.py
=====================

Tests for the pan/tilt camera control tool (Tapo C212 integration) -
both `MockCameraPTZHandler` (camera_ptz.py) and `RealCameraPTZHandler`
(real_camera_ptz.py). The real handler is tested against a small
duck-typed fake client (`moveMotor(x, y)`/`calibrateMotor()`), same
"synthetic client, no real hardware/network needed" approach
`test_real_home_assistant_verification.py`'s own `FakeHAClient` uses.

Run:
    python3 -m pytest luno/tool_manager/tests/test_camera_ptz.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.tool_manager.builtin.camera_ptz import MockCameraPTZHandler  # noqa: E402
from luno.tool_manager.builtin.real_camera_ptz import RealCameraPTZHandler  # noqa: E402
from luno.tool_manager.models import ToolCall  # noqa: E402


class _FakeTapoClient:
    """Minimal duck-typed stand-in for `pytapo.Tapo` - records every call
    so tests can assert direction/degree math without a real camera.
    `_presets` mimics the camera's own firmware-side preset storage
    (`{presetID: name}`), same shape `pytapo.Tapo.getPresets()` returns -
    `savePreset()`/`setPreset()` mutate/read the SAME dict, exactly like
    the real library keeps `self.presets` in sync."""

    def __init__(self, raise_on_move=False, raise_on_calibrate=False, raise_on_get_presets=False,
                 raise_on_save_preset=False, raise_on_set_preset=False, presets=None):
        self.move_calls = []
        self.calibrate_calls = 0
        self._raise_on_move = raise_on_move
        self._raise_on_calibrate = raise_on_calibrate
        self._raise_on_get_presets = raise_on_get_presets
        self._raise_on_save_preset = raise_on_save_preset
        self._raise_on_set_preset = raise_on_set_preset
        self._presets = dict(presets or {})
        self._next_id = max([int(k) for k in self._presets] or [0]) + 1
        self.set_preset_calls = []

    def moveMotor(self, x, y):
        if self._raise_on_move:
            raise RuntimeError("simulated camera offline")
        self.move_calls.append((x, y))

    def calibrateMotor(self):
        if self._raise_on_calibrate:
            raise RuntimeError("simulated camera offline")
        self.calibrate_calls += 1

    def getPresets(self):
        if self._raise_on_get_presets:
            raise RuntimeError("simulated camera offline")
        return dict(self._presets)

    def savePreset(self, name):
        if self._raise_on_save_preset:
            raise RuntimeError("simulated camera offline")
        preset_id = str(self._next_id)
        self._next_id += 1
        self._presets[preset_id] = name
        return True

    def setPreset(self, presetID):
        if self._raise_on_set_preset:
            raise RuntimeError("simulated camera offline")
        self.set_preset_calls.append(presetID)
        return True


def _set_env(**kwargs):
    saved = {}
    for k, v in kwargs.items():
        saved[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# -- MockCameraPTZHandler ------------------------------------------------------

def test_mock_supported_actions():
    handler = MockCameraPTZHandler()
    assert set(handler.supported_actions()) == {
        "pan_left", "pan_right", "tilt_up", "tilt_down", "center", "goto_preset", "save_preset",
    }


def test_mock_pan_and_tilt_accumulate_position():
    handler = MockCameraPTZHandler()
    r1 = handler.execute(ToolCall(tool="camera_ptz", action="pan_right"))
    r2 = handler.execute(ToolCall(tool="camera_ptz", action="pan_right"))
    r3 = handler.execute(ToolCall(tool="camera_ptz", action="tilt_up"))
    assert r1.success and r2.success and r3.success
    assert r3.data["pan"] == 30.0
    assert r3.data["tilt"] == 15.0


def test_mock_center_resets_position():
    handler = MockCameraPTZHandler()
    handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    result = handler.execute(ToolCall(tool="camera_ptz", action="center"))
    assert result.success and result.data == {"pan": 0.0, "tilt": 0.0}


def test_mock_rejects_unsupported_action():
    handler = MockCameraPTZHandler()
    error = handler.validate(ToolCall(tool="camera_ptz", action="zoom_in"))
    assert error is not None and "not supported" in error.lower()


def test_mock_save_and_goto_preset_round_trip():
    handler = MockCameraPTZHandler()
    handler.execute(ToolCall(tool="camera_ptz", action="pan_right"))  # pan=15
    handler.execute(ToolCall(tool="camera_ptz", action="tilt_up"))  # tilt=15
    save_result = handler.execute(ToolCall(tool="camera_ptz", action="save_preset", target="Pintu"))
    assert save_result.success and save_result.data == {"preset": "Pintu", "pan": 15.0, "tilt": 15.0}

    # move elsewhere, then recall - must land back exactly on the saved position
    handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    goto_result = handler.execute(ToolCall(tool="camera_ptz", action="goto_preset", target="pintu"))  # case-insensitive
    assert goto_result.success
    assert goto_result.data == {"preset": "pintu", "pan": 15.0, "tilt": 15.0}


def test_mock_goto_unknown_preset_fails_honestly():
    handler = MockCameraPTZHandler()
    handler.execute(ToolCall(tool="camera_ptz", action="save_preset", target="Pintu"))
    result = handler.execute(ToolCall(tool="camera_ptz", action="goto_preset", target="monitor"))
    assert not result.success
    assert "monitor" in result.message.lower()
    assert "pintu" in result.message.lower()  # lists what IS known


def test_mock_goto_preset_with_none_saved_yet():
    handler = MockCameraPTZHandler()
    result = handler.execute(ToolCall(tool="camera_ptz", action="goto_preset", target="pintu"))
    assert not result.success
    assert "none saved yet" in result.message.lower()


def test_mock_validate_requires_target_for_preset_actions():
    handler = MockCameraPTZHandler()
    assert handler.validate(ToolCall(tool="camera_ptz", action="save_preset")) is not None
    assert handler.validate(ToolCall(tool="camera_ptz", action="goto_preset", target="")) is not None
    assert handler.validate(ToolCall(tool="camera_ptz", action="save_preset", target="pintu")) is None


# -- RealCameraPTZHandler -------------------------------------------------------

def test_real_pan_left_sends_negative_x():
    saved = _set_env(TAPO_PAN_STEP_DEGREES=15)
    try:
        client = _FakeTapoClient()
        handler = RealCameraPTZHandler(client)
        result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
        assert result.success
        assert client.move_calls == [(-15.0, 0.0)]
        assert "left" in result.message.lower()
    finally:
        _restore_env(saved)


def test_real_pan_right_sends_positive_x():
    client = _FakeTapoClient()
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_right"))
    assert result.success and client.move_calls == [(15.0, 0.0)]


def test_real_tilt_up_sends_positive_y():
    client = _FakeTapoClient()
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="tilt_up"))
    assert result.success and client.move_calls == [(0.0, 15.0)]


def test_real_tilt_down_sends_negative_y():
    client = _FakeTapoClient()
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="tilt_down"))
    assert result.success and client.move_calls == [(0.0, -15.0)]


def test_real_center_calls_calibrate_motor():
    client = _FakeTapoClient()
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="center"))
    assert result.success and client.calibrate_calls == 1
    assert "centered" in result.message.lower()


def test_real_custom_step_degrees_from_env():
    saved = _set_env(TAPO_PAN_STEP_DEGREES=30, TAPO_TILT_STEP_DEGREES=5)
    try:
        client = _FakeTapoClient()
        handler = RealCameraPTZHandler(client)
        handler.execute(ToolCall(tool="camera_ptz", action="pan_right"))
        handler.execute(ToolCall(tool="camera_ptz", action="tilt_down"))
        assert client.move_calls == [(30.0, 0.0), (0.0, -5.0)]
    finally:
        _restore_env(saved)


def test_real_invert_pan_flips_direction():
    saved = _set_env(TAPO_INVERT_PAN=True)
    try:
        client = _FakeTapoClient()
        handler = RealCameraPTZHandler(client)
        handler.execute(ToolCall(tool="camera_ptz", action="pan_right"))
        assert client.move_calls == [(-15.0, 0.0)]  # inverted: "right" now sends negative x
    finally:
        _restore_env(saved)


def test_real_invert_tilt_flips_direction():
    saved = _set_env(TAPO_INVERT_TILT=True)
    try:
        client = _FakeTapoClient()
        handler = RealCameraPTZHandler(client)
        handler.execute(ToolCall(tool="camera_ptz", action="tilt_up"))
        assert client.move_calls == [(0.0, -15.0)]  # inverted: "up" now sends negative y
    finally:
        _restore_env(saved)


def test_real_never_claims_a_specific_new_position():
    """HONEST LIMITATION regression guard: pytapo has no readback API, so
    the success message must describe a command being SENT, never a
    confirmed/verified new angle - see module docstring."""
    client = _FakeTapoClient()
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    forbidden = ("verified", "now at", "confirmed the camera is")
    assert not any(p in result.message.lower() for p in forbidden)


def test_real_move_failure_is_reported_honestly():
    client = _FakeTapoClient(raise_on_move=True)
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="pan_left"))
    assert not result.success
    assert result.error_type == "CameraPTZError"
    assert "simulated camera offline" in result.message


def test_real_calibrate_failure_is_reported_honestly():
    client = _FakeTapoClient(raise_on_calibrate=True)
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="center"))
    assert not result.success
    assert "simulated camera offline" in result.message


def test_real_supported_actions_match_mock():
    assert set(RealCameraPTZHandler(_FakeTapoClient()).supported_actions()) == set(MockCameraPTZHandler().supported_actions())


# -- RealCameraPTZHandler: named-target aiming (save_preset/goto_preset) --------

def test_real_save_preset_calls_save_preset_with_name():
    client = _FakeTapoClient()
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="save_preset", target="pintu"))
    assert result.success
    assert "pintu" in result.message.lower()
    assert client._presets == {"1": "pintu"}


def test_real_goto_preset_resolves_name_to_id_case_insensitively():
    client = _FakeTapoClient(presets={"1": "Pintu", "2": "Monitor"})
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="goto_preset", target="monitor"))
    assert result.success
    assert client.set_preset_calls == ["2"]
    assert result.data == {"preset": "monitor", "preset_id": "2"}


def test_real_goto_preset_unknown_name_lists_known_presets_and_fails():
    client = _FakeTapoClient(presets={"1": "Pintu", "2": "Monitor"})
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="goto_preset", target="dapur"))
    assert not result.success
    assert result.error_type == "CameraPTZError"
    assert "dapur" in result.message.lower()
    assert "monitor" in result.message.lower() and "pintu" in result.message.lower()
    assert client.set_preset_calls == []  # never attempted a move to the wrong place


def test_real_save_preset_failure_is_reported_honestly():
    client = _FakeTapoClient(raise_on_save_preset=True)
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="save_preset", target="pintu"))
    assert not result.success
    assert "simulated camera offline" in result.message


def test_real_goto_preset_get_presets_failure_is_reported_honestly():
    client = _FakeTapoClient(raise_on_get_presets=True)
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="goto_preset", target="pintu"))
    assert not result.success
    assert "simulated camera offline" in result.message


def test_real_goto_preset_set_preset_failure_is_reported_honestly():
    client = _FakeTapoClient(presets={"1": "pintu"}, raise_on_set_preset=True)
    handler = RealCameraPTZHandler(client)
    result = handler.execute(ToolCall(tool="camera_ptz", action="goto_preset", target="pintu"))
    assert not result.success
    assert "simulated camera offline" in result.message


def test_real_validate_requires_target_for_preset_actions():
    handler = RealCameraPTZHandler(_FakeTapoClient())
    assert handler.validate(ToolCall(tool="camera_ptz", action="save_preset")) is not None
    assert handler.validate(ToolCall(tool="camera_ptz", action="goto_preset", target="  ")) is not None
    assert handler.validate(ToolCall(tool="camera_ptz", action="save_preset", target="pintu")) is None
