"""
tests/test_tapo_camera_event_audit.py
========================================

LUNO P0.5.2 (Tapo C212 Event Source Audit) - the smallest practical test
file for `tapo_camera_event_audit.py`'s own pure/near-pure logic
(`_safe_call`, `_classify_config_capability`, `_build_report`), per that
sprint's own Section 15 ("Create tests for the diagnostic logic where
practical... Do NOT require physical camera hardware for unit tests.
Use mocks/fixtures for library behavior.").

No real `pytapo.Tapo` connection or hardware is used anywhere in this
file - every "client" below is a tiny fake object exposing only the
handful of read-only methods the script itself calls
(`getBasicInfo`/`getMotionDetection`/`getPersonDetection`/
`getAlertEventType`/`getEvents`), which is exactly the same "duck-typed,
no pytapo import required" approach `tests/test_sprint69_tapo_c212_auth.py`
and `tests/test_sprint70_tapo_live_recovery.py` already established for
`real_camera_ptz.py`'s own tests.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import tapo_camera_event_audit as audit  # noqa: E402


class _FakeClient:
    """A minimal object exposing only the methods under test - never a
    real `pytapo.Tapo` instance, never imports `pytapo` itself."""

    def __init__(self, **overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            value = self._overrides[name]
            if callable(value):
                return value
            return lambda *a, **k: value
        raise AttributeError(name)


# ---------------------------------------------------------------------
# _safe_call
# ---------------------------------------------------------------------

def test_01_safe_call_available_api_succeeds():
    client = _FakeClient(getBasicInfo=lambda: {"model": "C212"})
    probe = audit._safe_call(client, "getBasicInfo")
    assert probe["ok"] is True
    assert probe["result"] == {"model": "C212"}
    assert probe["error"] is None


def test_02_safe_call_unavailable_api_method_missing():
    client = _FakeClient()  # no getPersonDetection at all
    probe = audit._safe_call(client, "getPersonDetection")
    assert probe["ok"] is False
    assert probe["error_class"] == "NOT_IMPLEMENTED"


def test_03_safe_call_connection_failure_classified():
    def _raise(*a, **k):
        raise ConnectionError("Connection refused")
    client = _FakeClient(getBasicInfo=_raise)
    probe = audit._safe_call(client, "getBasicInfo")
    assert probe["ok"] is False
    assert probe["error_class"] in ("PORT_UNREACHABLE", "HOST_UNREACHABLE")


def test_04_safe_call_authentication_failure_classified():
    def _raise(*a, **k):
        raise Exception("Invalid authentication data")
    client = _FakeClient(getBasicInfo=_raise)
    probe = audit._safe_call(client, "getBasicInfo")
    assert probe["ok"] is False
    assert probe["error_class"] == "AUTH_FAILED"


def test_05_safe_call_timeout_classified():
    def _raise(*a, **k):
        raise TimeoutError("timed out")
    client = _FakeClient(getEvents=_raise)
    probe = audit._safe_call(client, "getEvents")
    assert probe["ok"] is False
    assert probe["error_class"] == "HOST_UNREACHABLE"


def test_06_safe_call_never_raises():
    """No matter what the underlying call does, _safe_call must never
    propagate an exception - the whole probe script depends on this to
    stay read-only-diagnostic (never crash mid-audit)."""
    def _raise(*a, **k):
        raise RuntimeError("something totally unexpected")
    client = _FakeClient(getBasicInfo=_raise)
    probe = audit._safe_call(client, "getBasicInfo")
    assert probe["ok"] is False
    assert probe["error_class"] == "UNKNOWN"


# ---------------------------------------------------------------------
# secret masking
# ---------------------------------------------------------------------

def test_07_secret_masking_redacts_configured_password(monkeypatch):
    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "TAPO_PASSWORD", "SuperSecret123", raising=False)
    monkeypatch.setattr(legacy_config, "TAPO_USERNAME", "camuser", raising=False)

    def _raise(*a, **k):
        raise Exception("auth failed for user camuser with password SuperSecret123")
    client = _FakeClient(getBasicInfo=_raise)
    probe = audit._safe_call(client, "getBasicInfo")
    assert "SuperSecret123" not in probe["error"]
    assert "camuser" not in probe["error"]
    assert "REDACTED" in probe["error"]


# ---------------------------------------------------------------------
# _classify_config_capability
# ---------------------------------------------------------------------

def test_08_classify_config_capability_confirmed():
    probe = {"ok": True, "result": {"enabled": "on"}, "error": None, "error_class": None}
    assert audit._classify_config_capability(probe) == audit.CONFIRMED


def test_09_classify_config_capability_unknown_on_connection_failure():
    probe = {"ok": False, "result": None, "error": "unreachable", "error_class": "HOST_UNREACHABLE"}
    assert audit._classify_config_capability(probe) == audit.UNKNOWN


def test_10_classify_config_capability_not_available_on_api_rejection():
    probe = {"ok": False, "result": None, "error": "unsupported feature", "error_class": "API_REJECTED"}
    assert audit._classify_config_capability(probe) == audit.NOT_AVAILABLE


# ---------------------------------------------------------------------
# _build_report - end to end (still no real client)
# ---------------------------------------------------------------------

def test_11_build_report_not_connected_all_unknown():
    result = audit._build_report(
        False, {"error_class": "IMPORT_FAILED", "error": "no module"},
        None, None, None, None, None, None, None, 30.0,
    )
    assert result["pytapo_reachable"] is False
    for cap in result["capabilities"].values():
        assert cap["result"] == audit.UNKNOWN


def test_12_build_report_full_success_confirms_events_and_availability():
    basic_info = {"ok": True, "result": {"model": "C212"}, "error": None, "error_class": None}
    motion_cfg = {"ok": True, "result": {"enabled": "on", "digital_sensitivity": 50}, "error": None, "error_class": None}
    person_cfg = {"ok": True, "result": {"enabled": "off"}, "error": None, "error_class": None}
    alert_types = {"ok": True, "result": [{"name": "motion", "enabled": "on"}], "error": None, "error_class": None}
    events_before = {"ok": True, "result": [{"start_time": 1000, "end_time": 1010}], "error": None, "error_class": None}
    events_after = {"ok": True, "result": [{"start_time": 1000, "end_time": 1010}, {"start_time": 2000, "end_time": 2010}], "error": None, "error_class": None}

    result = audit._build_report(
        True, None, basic_info, motion_cfg, person_cfg, alert_types,
        events_before, events_after, 1500.0, 30.0,
    )
    assert result["capabilities"]["camera_connection"]["result"] == audit.CONFIRMED
    assert result["capabilities"]["camera_status"]["result"] == audit.CONFIRMED
    assert result["capabilities"]["motion"]["result"] == audit.CONFIRMED
    assert result["capabilities"]["human_detection"]["result"] == audit.CONFIRMED
    assert result["capabilities"]["events"]["result"] == audit.CONFIRMED
    assert result["capabilities"]["availability"]["result"] == audit.CONFIRMED
    # exactly one NEW event (start_time 2000) observed during the window
    assert len(result["live_observation"]["events_observed"]) == 1
    assert result["live_observation"]["events_observed"][0]["start_time"] == 2000
    assert result["live_observation"]["events_not_observed"] is False


def test_13_build_report_no_new_event_during_window():
    basic_info = {"ok": True, "result": {"model": "C212"}, "error": None, "error_class": None}
    events_before = {"ok": True, "result": [{"start_time": 1000, "end_time": 1010}], "error": None, "error_class": None}
    events_after = {"ok": True, "result": [{"start_time": 1000, "end_time": 1010}], "error": None, "error_class": None}

    result = audit._build_report(
        True, None, basic_info, None, None, None,
        events_before, events_after, 1500.0, 30.0,
    )
    assert result["live_observation"]["events_not_observed"] is True
    assert result["live_observation"]["events_observed"] == []


def test_14_build_report_malformed_event_missing_start_time_does_not_crash():
    """A malformed/unexpected event shape (missing start_time) from a
    real device response must never crash the audit - it's simply never
    counted as a 'new' event (nothing to diff against)."""
    basic_info = {"ok": True, "result": {"model": "C212"}, "error": None, "error_class": None}
    events_before = {"ok": True, "result": [{"start_time": 1000}], "error": None, "error_class": None}
    events_after = {"ok": True, "result": [{"start_time": 1000}, {"weird": "no start_time field"}], "error": None, "error_class": None}

    result = audit._build_report(
        True, None, basic_info, None, None, None,
        events_before, events_after, 1500.0, 30.0,
    )
    # the malformed entry (start_time=None, not in before-set) is reported
    # as a new event rather than silently dropped or crashing - honest,
    # not fabricated.
    assert len(result["live_observation"]["events_observed"]) == 1
    assert result["live_observation"]["events_observed"][0] == {"weird": "no start_time field"}


def test_15_build_report_events_call_failure_recorded_as_error_not_crash():
    basic_info = {"ok": True, "result": {"model": "C212"}, "error": None, "error_class": None}
    events_before = {"ok": False, "result": None, "error": "getEvents failed: timed out", "error_class": "HOST_UNREACHABLE"}
    events_after = {"ok": False, "result": None, "error": "getEvents failed: timed out", "error_class": "HOST_UNREACHABLE"}

    result = audit._build_report(
        True, None, basic_info, None, None, None,
        events_before, events_after, 1500.0, 30.0,
    )
    assert len(result["live_observation"]["errors"]) == 2
    assert result["capabilities"]["events"]["result"] == audit.UNKNOWN


def test_16_same_physical_camera_always_unknown_never_fabricated():
    result = audit._build_report(False, {"error": "x"}, None, None, None, None, None, None, None, 30.0)
    assert result["same_physical_camera_vs_home_assistant"]["status"] == "UNKNOWN"


# ---------------------------------------------------------------------
# no production config modification (Section 13/14/16)
# ---------------------------------------------------------------------

def test_17_never_opens_any_file_for_writing():
    import inspect
    import re

    source = inspect.getsource(audit)
    write_mode_opens = re.findall(r"""open\([^)]*['"]\s*[wa]\+?['"]""", source)
    assert not write_mode_opens, f"found file-write call(s): {write_mode_opens}"


def test_18_never_calls_a_write_or_control_method_on_the_client():
    """Static proof: the script's source never calls any known
    write/control pytapo method name (setMotionDetection,
    setPersonDetection, setAlarm, playAlarm, startManualAlarm,
    stopManualAlarm, moveMotor, calibrateMotor, savePreset, setPreset)."""
    import inspect

    source = inspect.getsource(audit)
    forbidden = (
        "setMotionDetection", "setPersonDetection", "setAlarm", "playAlarm",
        "startManualAlarm", "stopManualAlarm", "moveMotor", "calibrateMotor",
        "savePreset", "setPreset", "setAlertEventType",
    )
    for name in forbidden:
        assert f'"{name}"' not in source and f"'{name}'" not in source, f"forbidden write/control call found: {name}"
