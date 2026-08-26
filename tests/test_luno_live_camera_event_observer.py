"""
tests/test_luno_live_camera_event_observer.py
==================================================

LUNO P0.5.4-LIVE (Real Camera Proof-of-Life) - the smallest practical
test file for `luno_live_camera_event_observer.py`'s own non-hardware-
dependent logic (pre-flight formatting, TCP check, the `_LiveObserver`
class's own print/record behavior and - critically - its NEVER-print-
raw-event-data guarantee for the two events whose payload can contain a
credentialed RTSP URL).

This does NOT test against a real Tapo C212 or real network - that is
exactly the live-hardware step this script exists to let a human run
themselves (see that script's own module docstring). What CAN be
verified here, and is: the script never touches `event.data` for the
four raw Vision events (secret-safety, provable without hardware), the
pre-flight PASS/FAIL classification logic, and that the full
`register_all_modules()`/`register_all_adapters()` bootstrap plus a
SIMULATED `camera_person_entered` event (never claimed to be a real
camera event) correctly reaches the observer end to end.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from typing import Any, Dict

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code_only(source: str) -> str:
    """Best-effort strip of triple-quoted docstrings and '#' line
    comments, so static "forbidden string" checks only look at actual
    executable code - not prose that legitimately DISCUSSES a forbidden
    name (e.g. this file's own module docstring explaining what the
    script never does)."""
    import re
    no_docstrings = re.sub(r'"""[\s\S]*?"""', "", source)
    no_comments = "\n".join(line.split("#", 1)[0] for line in no_docstrings.splitlines())
    return no_comments

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno_live_camera_event_observer as obs  # noqa: E402
from luno.core.events import Event  # noqa: E402


# ---------------------------------------------------------------------
# _tcp_check
# ---------------------------------------------------------------------

def test_01_tcp_check_open_port():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        result = obs._tcp_check("127.0.0.1", port, timeout_s=2.0)
        assert result["ok"] is True
        assert result["detail"] == "OPEN"
    finally:
        srv.close()


def test_02_tcp_check_closed_port_reports_failure_not_crash():
    # Port 1 on localhost is essentially guaranteed closed/refused.
    result = obs._tcp_check("127.0.0.1", 1, timeout_s=2.0)
    assert result["ok"] is False
    assert "detail" in result


# ---------------------------------------------------------------------
# pre-flight formatting
# ---------------------------------------------------------------------

def test_03_print_preflight_all_pass_returns_true(capsys):
    results = {
        "TAPO_HOST configured": {"ok": True, "detail": "configured"},
        "Camera reachable (TCP 443)": {"ok": True, "detail": "OPEN"},
    }
    ok = obs._print_preflight(results)
    out = capsys.readouterr().out
    assert ok is True
    assert "PASS" in out
    assert "FAIL" not in out


def test_04_print_preflight_critical_failure_returns_false(capsys):
    results = {
        "TAPO_HOST configured": {"ok": True, "detail": "configured"},
        "Camera reachable (TCP 443)": {"ok": False, "detail": "Network is unreachable"},
    }
    ok = obs._print_preflight(results)
    assert ok is False


def test_05_preflight_never_prints_actual_host_or_credential_values(monkeypatch, capsys):
    """The pre-flight report only ever prints boolean configured-ness,
    never the actual TAPO_HOST/USERNAME/PASSWORD values."""
    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "TAPO_HOST", "192.168.1.4", raising=False)
    monkeypatch.setattr(legacy_config, "TAPO_USERNAME", "SecretUser", raising=False)
    monkeypatch.setattr(legacy_config, "TAPO_PASSWORD", "SuperSecretPassword123", raising=False)

    results = obs._run_preflight()
    obs._print_preflight(results)
    out = capsys.readouterr().out

    assert "192.168.1.4" not in out
    assert "SecretUser" not in out
    assert "SuperSecretPassword123" not in out


# ---------------------------------------------------------------------
# _LiveObserver
# ---------------------------------------------------------------------

def test_06_on_camera_event_prints_and_records_full_safe_payload(capsys):
    observer = obs._LiveObserver()
    event = Event(type="camera_automation.camera_event", data={
        "camera_id": "tapo_c212", "kind": "human_detected", "entity_id": "vision:camera_person_entered",
        "old_state": None, "new_state": None, "confidence": None, "timestamp": 123.0, "source": "vision",
    })
    observer.on_camera_event(event)
    out = capsys.readouterr().out
    assert "kind=human_detected" in out
    assert "camera_id=tapo_c212" in out
    assert len(observer.camera_events) == 1
    assert observer.camera_events[0]["kind"] == "human_detected"


def test_07_on_raw_vision_event_never_prints_event_data(capsys):
    """The core safety guarantee (module docstring's own stated reason
    for this design): CameraDisconnected/CameraReconnected can carry a
    fully-credentialed RTSP URL in data['source']. Prove that even when
    such a value IS present on the event, it is NEVER printed."""
    observer = obs._LiveObserver()
    handler = observer.on_raw_vision_event("camera_disconnected")
    event = Event(type="camera_disconnected", data={
        "source": "rtsp://admin:TotallySecretPassword@192.168.1.4:554/stream1",
        "error": "authentication failed for admin:TotallySecretPassword",
    })
    handler(event)
    out = capsys.readouterr().out

    assert "TotallySecretPassword" not in out
    assert "rtsp://" not in out
    assert "camera_disconnected observed" in out
    assert observer.raw_vision_event_counts["camera_disconnected"] == 1


def test_08_on_raw_vision_event_static_proof_never_reads_event_data():
    """Static proof, not just behavioral: the handler closure's own
    CODE (not its own docstring/comments explaining why) never
    references `event.data` at all."""
    import inspect
    source = _code_only(inspect.getsource(obs._LiveObserver.on_raw_vision_event))
    assert "event.data" not in source
    assert ".data" not in source


def test_09_multiple_raw_events_counted_independently():
    observer = obs._LiveObserver()
    h1 = observer.on_raw_vision_event("camera_person_entered")
    h2 = observer.on_raw_vision_event("camera_person_left")
    h1(Event(type="camera_person_entered"))
    h1(Event(type="camera_person_entered"))
    h2(Event(type="camera_person_left"))
    assert observer.raw_vision_event_counts["camera_person_entered"] == 2
    assert observer.raw_vision_event_counts["camera_person_left"] == 1


# ---------------------------------------------------------------------
# static safety proofs (matches this project's precedent from
# ha_camera_discovery.py / tapo_camera_event_audit.py's own test files)
# ---------------------------------------------------------------------

def test_10_never_opens_any_file_for_writing():
    import inspect
    import re
    source = inspect.getsource(obs)
    write_mode_opens = re.findall(r"""open\([^)]*['"]\s*[wa]\+?['"]""", source)
    assert not write_mode_opens, f"found file-write call(s): {write_mode_opens}"


def test_11_never_calls_a_write_or_control_method():
    """No PTZ, no HA service call, no camera settings change anywhere in
    this script's source. AST-based (not a plain text scan, same
    convention `tests/test_sprint72_automation_engine.py::test_23` and
    this project's other security-boundary tests already use) so this
    keeps working correctly even though, as of P0.6.2, the observer's
    own PRINT/DISPLAY strings legitimately contain the words "turn_on"/
    "call_service" (e.g. `action=home_assistant.turn_on` in a log line,
    or this module's own banner text describing what the REAL runtime's
    automation rule does) - a bare substring scan would now false-
    positive on those. What actually matters, and what this test proves
    instead, is that the script's own CODE never contains a call-shaped
    reference to any of these names (e.g. `x.turn_on(...)`,
    `client.call_service(...)`) and never constructs its own outgoing
    tool/service request."""
    import ast
    import inspect
    source = inspect.getsource(obs)
    tree = ast.parse(source)
    forbidden_attrs = {"moveMotor", "calibrateMotor", "savePreset", "setPreset", "call_service", "turn_on", "turn_off", "toggle"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
            assert name not in forbidden_attrs, f"forbidden write/control call found: {name}"
    # The observer must also never itself publish a `tool_requested`
    # event (it only ever subscribes/reads) - confirmed by inspecting
    # every string literal argument to `Event(type=...)` in the source.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Event":
            for kw in node.keywords:
                if kw.arg == "type" and isinstance(kw.value, ast.Constant):
                    assert kw.value.value != "tool_requested", "observer must never construct its own tool_requested Event"


def test_12_never_writes_camera_automation_json():
    import inspect
    source = _code_only(inspect.getsource(obs))
    assert "camera_automation.json" not in source


# ---------------------------------------------------------------------
# real-bootstrap E2E (simulated event - never claimed to be a real
# camera detection; proves the OBSERVER's own wiring, not hardware)
# ---------------------------------------------------------------------

def test_13_real_bootstrap_simulated_event_reaches_observer(monkeypatch):
    monkeypatch.setenv("CAMERA_AUTOMATION_ENABLED", "true")
    monkeypatch.setenv("CAMERA_AUTOMATION_COOLDOWN_S", "0")

    from luno.bootstrap.adapters import register_all_adapters
    from luno.bootstrap.launcher_config import LauncherConfig
    from luno.bootstrap.modules import register_all_modules
    from luno.bootstrap.shutdown import ShutdownCoordinator
    from luno.core.config import CoreConfig
    from luno.core.runtime import Runtime

    cfg = LauncherConfig()
    runtime = Runtime(CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2))
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    observer = obs._LiveObserver()
    try:
        runtime.start()
        sub_id = runtime.event_bus.subscribe("camera_automation.camera_event", observer.on_camera_event)

        # Simulates exactly what VisionAdapter itself would publish on a
        # real detection - never claimed to be a real hardware event.
        runtime.event_bus.publish(Event(type="camera_person_entered"))

        deadline = time.time() + 3.0
        while time.time() < deadline and not observer.camera_events:
            time.sleep(0.02)

        runtime.event_bus.unsubscribe(sub_id)
    finally:
        ShutdownCoordinator(runtime, adapter_manager).shutdown()

    assert len(observer.camera_events) == 1
    assert observer.camera_events[0]["kind"] == "human_detected"
    assert observer.camera_events[0]["camera_id"] == "tapo_c212"


# ---------------------------------------------------------------------
# P0.5.4-FIX - observer must use the SAME config-loading entry point as
# main.py, not a bare LauncherConfig() that silently ignores .env.
#
# Root cause this sprint found (via direct code tracing of main.py and
# luno/bootstrap/launcher_config.py, not guessed): main.py resolves its
# config with the classmethod `LauncherConfig.load()` (main.py line 66)
# - the ONLY path that calls load_dotenv() and re-derives
# vision_backend from the environment (so `.env`'s VISION_BACKEND=real
# actually takes effect). A bare `LauncherConfig()` constructor never
# reads .env and silently keeps the hardcoded dataclass default
# vision_backend="mock", which makes register_all_adapters() skip
# RealVisionSource() entirely and fall back to MockVisionSource() -
# explaining exactly why the observer previously produced only the
# generic, content-free `scheduled_vision_poll` tick and never a real
# camera event, even though the user's own main.py run (same machine,
# same .env) performs real YOLO detection.
#
# This is a WIRING/config-loading proof only - it does NOT claim to
# have verified real hardware detection (that still requires the user
# to run the script against the real camera and report the result).
# ---------------------------------------------------------------------

def test_14_observer_uses_launcherconfig_load_not_bare_constructor():
    """Static proof that main() calls the same LauncherConfig.load()
    classmethod main.py itself uses (main.py line 66), not a bare
    LauncherConfig() that silently ignores .env's VISION_BACKEND."""
    import inspect
    source = _code_only(inspect.getsource(obs.main))
    assert "LauncherConfig.load()" in source
    assert "= LauncherConfig()" not in source


def test_15_main_py_itself_uses_launcherconfig_load():
    """Confirms the thing we're matching against hasn't drifted: main.py
    must still resolve its config via LauncherConfig.load() for this
    fix's premise to hold."""
    import inspect
    import re
    main_path = os.path.join(_ROOT, "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        main_source = f.read()
    no_docstrings = re.sub(r'"""[\s\S]*?"""', "", main_source)
    no_comments = "\n".join(line.split("#", 1)[0] for line in no_docstrings.splitlines())
    assert "LauncherConfig.load()" in no_comments


def test_16_observer_warns_when_vision_backend_not_real(capsys):
    """If vision_backend ever resolves to anything but 'real' (e.g. .env
    misconfigured, or a future regression re-introduces the bare
    LauncherConfig() bug), the observer must print a visible warning
    instead of silently running against MockVisionSource."""
    import inspect
    source = _code_only(inspect.getsource(obs.main))
    assert 'cfg.vision_backend != "real"' in source
    assert "WARNING" in source
