"""
tests/test_p0_8_1_live_verification.py
==========================================

LUNO P0.8.1 (Live Camera -> Home Assistant Light Verification) -
dedicated regression suite for everything in this sprint that CAN be
verified without real camera/Home Assistant hardware: the pre-flight
check logic, the test-light resolution logic, the new `apply_camera_
automation_test_light_override()` scoping guarantees, the `_Snapshot`
delta-counting helper, and static structural/credential-safety proof
for `luno_live_p0_8_1_verification.py` itself.

This file does NOT and CANNOT prove the live hardware walk-test itself
passed - that requires a human, a real Tapo C212, and a real Home
Assistant instance, none of which exist in this sandbox. See `docs/
change_impact/camera_automation_p0_8_1.md` for the honest, actual
pre-flight result this sandbox produced.

Sections:
  A. Static/structural safety checks on the new script (Section 1
     preamble: never print credentials, never call a Home Assistant
     service directly, hard-stop discipline present in source).
  B. `apply_camera_automation_test_light_override()` scoping guarantees
     (Section 2/3 of the brief) - the core of what CAN be proven without
     hardware.
  C. `_resolve_test_light()` / pre-flight critical-check gating logic
     (pure functions, no bootstrap).
  D. `_Snapshot` delta-counting helper (pure logic).
  E. Real-bootstrap end-to-end proof (Section 10-style, mock HA only) -
     the override actually redirects the mocked dispatch to the
     overridden entity, proving the WHOLE P0.8.1 wiring is structurally
     correct even though this sandbox cannot reach real hardware.
  F. Regression safety - the override is a true no-op (byte-identical
     rule/config) whenever the new env var is unset, so every P0.8.0
     test and every prior sprint's own regression run is unaffected.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from typing import Any, Dict

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno_live_p0_8_1_verification as live  # noqa: E402
from luno.automation.engine import AutomationEngine  # noqa: E402
from luno.bootstrap.adapters import (  # noqa: E402
    apply_camera_automation_test_light_override,
    register_all_adapters,
    register_camera_action_ha_state_reader,
)
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.camera_automation import CAMERA_EVENT_TYPE, CameraAutomationConfig, CameraAutomationModule  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.tool_manager.builtin.home_assistant import MockHomeAssistantHandler  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
_LIVE_SCRIPT_PATH = os.path.join(_ROOT, "luno_live_p0_8_1_verification.py")
_RULES_PATH = os.path.join(_ROOT, "config", "automation_rules.json")
_RULE_ID = "camera_test_automation_safety_action"
_ENV_VAR = "CAMERA_AUTOMATION_TEST_LIGHT_ENTITY"


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _build_stack(camera_automation_enabled: bool = True):
    """Same real-bootstrap pattern `test_p0_8_0_camera_action_safety.py`
    already established - MOCK backends throughout, `register_real_
    tool_handlers()` never called."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    cam_module: CameraAutomationModule = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=camera_automation_enabled, cooldown_s=0.0)
    register_camera_action_ha_state_reader(modules, adapters)
    return runtime, modules, adapters, adapter_manager


def _teardown(runtime, adapter_manager) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()


def _mock_ha_handler(modules: Dict[str, Any]) -> MockHomeAssistantHandler:
    tool_manager_module = modules["tool_manager_module"]
    handler = tool_manager_module.manager.registry.get("home_assistant")
    assert isinstance(handler, MockHomeAssistantHandler), (
        f"expected MockHomeAssistantHandler, got {type(handler)!r} - a test in this file would be "
        "silently exercising a REAL Home Assistant call, which this sprint must never do"
    )
    return handler


def _valid_event_data(**overrides: Any) -> Dict[str, Any]:
    data = {
        "camera_id": "tapo_c212", "kind": "human_detected", "available": True,
        "human_present": True, "person_count": 1, "detected_objects": ("person",),
        "detection_error": None,
    }
    data.update(overrides)
    return data


def _publish_camera_event(runtime, **overrides: Any) -> None:
    runtime.event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=_valid_event_data(**overrides)))


# ============================================================================
# A. Static/structural safety checks on the new script
# ============================================================================

def test_00_live_script_never_prints_a_credential_value():
    """Section 1/6's own "never print credentials" constraint, proven
    the same way this project's other live-script tests already prove
    it (static source scan) - TAPO_PASSWORD/TAPO_USERNAME/HA_TOKEN
    values are read only to compute a boolean 'configured' flag, never
    interpolated into a print/f-string."""
    src = _read(_LIVE_SCRIPT_PATH)
    forbidden_patterns = [
        "print(f\"{legacy_config.TAPO_PASSWORD",
        "print(legacy_config.TAPO_PASSWORD",
        "print(f\"{legacy_config.TAPO_USERNAME",
        "print(legacy_config.TAPO_USERNAME",
        "{HA_TOKEN}",
        "print(HA_TOKEN",
        "access_token",
    ]
    for pattern in forbidden_patterns:
        assert pattern not in src, f"forbidden pattern found in live script: {pattern!r}"


def test_01_live_script_never_imports_call_service_or_a_second_ha_client():
    """Section 'WHAT THIS NEVER DOES' - this script only ever reads
    entity STATE (`get_entity_state`) and boots the real, existing
    `AutomationEngine`/`ToolManager` dispatch path - it must never call
    `call_service(` directly itself (that would be a second, bypassing
    HA control path)."""
    src = _read(_LIVE_SCRIPT_PATH)
    assert "call_service(" not in src
    assert "register_real_tool_handlers" not in src
    assert "moveMotor" not in src and "calibrateMotor" not in src and "savePreset" not in src


def test_02_live_script_never_writes_automation_rules_json():
    src = _read(_LIVE_SCRIPT_PATH)
    assert "_persist_rules" not in src
    assert 'open(' not in src or 'automation_rules.json' not in src, (
        "the live script must never open config/automation_rules.json for writing"
    )


def test_03_live_script_pre_flight_treats_every_brief_section_1_check_as_critical():
    """Every one of the brief's own Section 1 bullet points must be in
    the hard-stop critical set - none silently downgraded to advisory."""
    expected = {
        "TAPO_HOST configured", "TAPO_USERNAME configured", "TAPO_PASSWORD configured",
        "Home Assistant reachable", "Home Assistant authentication succeeds",
        "Vision backend = real", "ultralytics (YOLO) importable", "cv2 (OpenCV) importable",
        "CAMERA_VISION_ENABLED", "Camera reachable (TCP 443)", "RTSP reachable (TCP 554)",
        "Camera automation enabled", "P0.8 safety gate enabled",
    }
    assert expected == live._CRITICAL_PREFLIGHT_CHECKS


def test_04_live_script_compiles_cleanly():
    src = _read(_LIVE_SCRIPT_PATH)
    ast.parse(src)  # raises SyntaxError if it doesn't


# ============================================================================
# B. apply_camera_automation_test_light_override() scoping guarantees
# ============================================================================

def test_10_override_is_a_true_noop_when_env_var_unset(monkeypatch):
    monkeypatch.delenv(_ENV_VAR, raising=False)
    runtime, modules, adapters, adapter_manager = _build_stack()
    try:
        runtime.start()  # AutomationEngine.start() is what loads _rules
        engine: AutomationEngine = modules["automation_engine"]
        rule_before = engine._rules[_RULE_ID]
        target_before = rule_before.actions[0].parameters["target"]

        result = apply_camera_automation_test_light_override(modules)

        assert result is None
        assert engine._rules[_RULE_ID].actions[0].parameters["target"] == target_before == "light.test_camera_automation"
    finally:
        _teardown(runtime, adapter_manager)


def test_11_override_applies_only_to_the_live_test_rules_targets(monkeypatch):
    """P0.8.2 update: the override now applies to BOTH the P0.8.0/P0.8.1
    ON rule AND the P0.8.2 OFF rule (`_LIVE_TEST_RULE_IDS`), so a live
    walk-test turns the SAME physical light on and off - this is an
    intentional, documented extension of this test's own original
    'only the one rule' assertion, not a regression (same category of
    update `test_p0_8_0_camera_action_safety.py`'s own test_33/test_34
    also needed for the identical reason)."""
    monkeypatch.setenv(_ENV_VAR, "light.real_test_light")
    runtime, modules, adapters, adapter_manager = _build_stack()
    try:
        runtime.start()  # AutomationEngine.start() is what loads _rules
        engine: AutomationEngine = modules["automation_engine"]
        _OFF_RULE_ID = "camera_test_automation_safety_action_off"
        live_test_rule_ids = {_RULE_ID, _OFF_RULE_ID}
        other_rule_targets_before = {
            rid: [a.parameters.get("target") for a in r.actions]
            for rid, r in engine._rules.items() if rid not in live_test_rule_ids
        }

        result = apply_camera_automation_test_light_override(modules)

        assert result == "light.real_test_light"
        assert engine._rules[_RULE_ID].actions[0].parameters["target"] == "light.real_test_light"
        assert engine._rules[_OFF_RULE_ID].actions[0].parameters["target"] == "light.real_test_light"
        # every OTHER (non-live-test) rule's action targets are untouched
        other_rule_targets_after = {
            rid: [a.parameters.get("target") for a in r.actions]
            for rid, r in engine._rules.items() if rid not in live_test_rule_ids
        }
        assert other_rule_targets_before == other_rule_targets_after
        # the ON rule's own id/name/enabled/trigger/conditions/cooldown are untouched
        rule = engine._rules[_RULE_ID]
        assert rule.id == _RULE_ID
        assert rule.enabled is True
        assert rule.cooldown_seconds == 30.0
        assert len(rule.conditions) == 3
        # the OFF rule's own id/name/enabled/trigger/conditions/cooldown are untouched too
        off_rule = engine._rules[_OFF_RULE_ID]
        assert off_rule.id == _OFF_RULE_ID
        assert off_rule.enabled is True
        assert off_rule.cooldown_seconds == 30.0
        assert len(off_rule.conditions) == 3
        assert off_rule.actions[0].type == "home_assistant.turn_off"
    finally:
        _teardown(runtime, adapter_manager)


def test_12_override_never_writes_the_config_file_on_disk(monkeypatch):
    before = _read(_RULES_PATH)
    monkeypatch.setenv(_ENV_VAR, "light.real_test_light")
    runtime, modules, adapters, adapter_manager = _build_stack()
    try:
        runtime.start()
        apply_camera_automation_test_light_override(modules)
        after = _read(_RULES_PATH)
        assert before == after, "config/automation_rules.json on disk must never change (in-memory override only)"
        # sanity - the file itself still has the ORIGINAL placeholder target
        rules = json.loads(after)
        assert rules[_RULE_ID]["actions"][0]["parameters"]["target"] == "light.test_camera_automation"
    finally:
        _teardown(runtime, adapter_manager)


def test_13_override_returns_none_if_rule_or_engine_missing(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "light.real_test_light")
    assert apply_camera_automation_test_light_override({}) is None

    class _FakeEngine:
        _rules: Dict[str, Any] = {}

    assert apply_camera_automation_test_light_override({"automation_engine": _FakeEngine()}) is None


def test_14_override_strips_whitespace_and_ignores_blank_value(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "   ")
    runtime, modules, adapters, adapter_manager = _build_stack()
    try:
        runtime.start()
        assert apply_camera_automation_test_light_override(modules) is None
    finally:
        _teardown(runtime, adapter_manager)

    monkeypatch.setenv(_ENV_VAR, "  light.spaced  ")
    runtime, modules, adapters, adapter_manager = _build_stack()
    try:
        runtime.start()
        result = apply_camera_automation_test_light_override(modules)
        assert result == "light.spaced"
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# C. _resolve_test_light() / pre-flight critical-check gating
# ============================================================================

def test_20_resolve_test_light_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv(_ENV_VAR, raising=False)
    assert live._resolve_test_light() is None


def test_21_resolve_test_light_returns_the_configured_value(monkeypatch):
    monkeypatch.setenv(_ENV_VAR, "light.kitchen_test")
    assert live._resolve_test_light() == "light.kitchen_test"


def test_22_resolve_test_light_never_guesses_a_fallback(monkeypatch):
    """No hardcoded fallback entity id anywhere in the resolution
    function's own source - the brief's own 'never automatically select
    a random light'."""
    import inspect
    src = inspect.getsource(live._resolve_test_light)
    assert "light." not in src, "test-light resolution must never hardcode a fallback entity id"


def test_23_print_preflight_hard_stops_when_any_critical_check_fails():
    all_pass = {name: {"ok": True, "detail": "ok"} for name in live._CRITICAL_PREFLIGHT_CHECKS}
    assert live._print_preflight(dict(all_pass)) is True

    one_failing = dict(all_pass)
    one_failing["Home Assistant reachable"] = {"ok": False, "detail": "unreachable"}
    assert live._print_preflight(one_failing) is False


def test_24_ha_reachable_check_never_raises_on_unparseable_url(monkeypatch):
    import luno.config as legacy_config
    monkeypatch.setattr(legacy_config, "HA_URL", "not a url::::", raising=False)
    result = live._check_ha_reachable()
    assert result["ok"] is False


def test_25_ha_auth_check_never_raises_and_fails_closed_on_unreachable_host():
    # Uses the real (throwaway) HomeAssistantClient against whatever
    # HA_WS_URL this checkout resolves to - in this sandbox that is
    # always unreachable, so this proves the check FAILS CLOSED (never
    # raises, never reports ok=True) rather than proving connectivity.
    result = live._check_ha_auth()
    assert isinstance(result, dict) and "ok" in result and "detail" in result
    assert result["ok"] is False  # sandbox has no route to any real HA instance


# ============================================================================
# D. _Snapshot delta-counting helper (pure logic)
# ============================================================================

def test_30_snapshot_delta_counts_only_new_events():
    observer = live._LiveObserver()
    observer.raw_vision_event_counts["camera_person_entered"] = 2
    observer.camera_events.append({"kind": "human_detected"})
    snap = live._Snapshot(observer)

    observer.raw_vision_event_counts["camera_person_entered"] = 3
    observer.camera_events.append({"kind": "human_detected"})
    observer.camera_events.append({"kind": "human_cleared"})

    assert snap.delta_raw("camera_person_entered") == 1
    assert snap.delta_kind("human_detected") == 1
    assert snap.delta_kind("human_cleared") == 1
    assert snap.delta_kind("camera_offline") == 0


def test_31_snapshot_new_actions_only_returns_actions_after_the_snapshot():
    observer = live._LiveObserver()
    observer.action_events.append({"elapsed": 1.0, "status": "completed", "code": "ok"})
    snap = live._Snapshot(observer)
    observer.action_events.append({"elapsed": 2.0, "status": "completed", "code": "already_in_desired_state"})

    new = snap.new_actions()
    assert len(new) == 1
    assert new[0]["code"] == "already_in_desired_state"


def test_32_snapshot_delta_tool_requested_and_vision_detection_failed():
    observer = live._LiveObserver()
    observer.tool_requested_count = 1
    observer.vision_detection_failed_count = 0
    snap = live._Snapshot(observer)
    observer.tool_requested_count = 4
    observer.vision_detection_failed_count = 2

    assert snap.delta_tool_requested() == 3
    assert snap.delta_vision_detection_failed() == 2


# ============================================================================
# E. Real-bootstrap end-to-end proof (mock HA only)
# ============================================================================

def test_40_override_plus_real_dispatch_targets_the_overridden_entity_not_the_placeholder(monkeypatch):
    """The strongest proof available without real hardware: apply the
    P0.8.1 override, publish a real camera_automation.camera_event
    through the real Event Bus/AutomationEngine/safety gate, and
    confirm the (mocked) Home Assistant dispatch actually targets the
    OVERRIDDEN entity id, never the shipped placeholder and never any
    other entity - i.e. the whole P0.8.1 wiring is structurally
    correct end to end, only real hardware reachability is unproven."""
    monkeypatch.setenv(_ENV_VAR, "light.real_test_light")
    runtime, modules, adapters, adapter_manager = _build_stack()
    try:
        _mock_ha_handler(modules)  # confirm the mock, not a real client, is in play
        tool_calls: list = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        runtime.start()

        applied = apply_camera_automation_test_light_override(modules)
        assert applied == "light.real_test_light"

        _publish_camera_event(runtime)

        # P0.6.2's own `camera_human_detected_test_action` ALSO matches
        # `human_detected` and ALSO fires independently (targeting
        # "light.wled", a DIFFERENT entity) - same expected co-firing
        # test_p0_8_0_camera_action_safety.py::test_13 already
        # documents. Filtered to THIS rule's own overridden entity only.
        ok = _wait_until(
            lambda: any(c.get("tool") == "home_assistant" and c.get("target") == "light.real_test_light" for c in tool_calls),
            timeout_s=5.0,
        )
        assert ok, "expected a mock Home Assistant call targeting the OVERRIDDEN entity"
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == "light.real_test_light"]
        assert len(ha_calls) == 1
        assert ha_calls[0]["target"] == "light.real_test_light"
        # never the shipped placeholder for THIS rule's own call
        placeholder_calls = [c for c in tool_calls if c.get("target") == "light.test_camera_automation"]
        assert placeholder_calls == [], "the overridden rule must never also target the placeholder"
    finally:
        _teardown(runtime, adapter_manager)


def test_41_without_override_real_dispatch_still_targets_the_shipped_placeholder(monkeypatch):
    """Byte-for-byte P0.8.0 behavior when the new env var is unset -
    the placeholder entity, never a guessed real one."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    runtime, modules, adapters, adapter_manager = _build_stack()
    try:
        applied = apply_camera_automation_test_light_override(modules)
        assert applied is None

        _mock_ha_handler(modules)
        tool_calls: list = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
        runtime.start()
        _publish_camera_event(runtime)

        ok = _wait_until(
            lambda: any(c.get("tool") == "home_assistant" and c.get("target") == "light.test_camera_automation" for c in tool_calls),
            timeout_s=5.0,
        )
        assert ok
        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == "light.test_camera_automation"]
        assert len(ha_calls) == 1
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# F. Regression safety - P0.8.0's own rule/tests are unaffected
# ============================================================================

def test_50_p0_8_0_rule_shape_unchanged_on_disk():
    rules = json.loads(_read(_RULES_PATH))
    rule = rules[_RULE_ID]
    assert rule["enabled"] is True
    assert rule["cooldown_seconds"] == 30.0
    assert len(rule["conditions"]) == 3
    assert rule["actions"][0]["type"] == "home_assistant.turn_on"
    assert rule["actions"][0]["parameters"]["target"] == "light.test_camera_automation"


def test_51_every_bootstrap_helper_in_this_file_uses_the_mock_ha_handler():
    """Same architecture-guard discipline test_p0_8_0_camera_action_
    safety.py's own Section G already establishes - proven BEHAVIORALLY
    here (every test that touches HA calls `_mock_ha_handler()`, which
    itself asserts `isinstance(handler, MockHomeAssistantHandler)` and
    would fail loudly if a real handler were ever wired in) rather than
    by a self-referential source scan of this file's own text (which
    would trivially "find" its own assertion strings)."""
    runtime, modules, adapters, adapter_manager = _build_stack()
    try:
        handler = _mock_ha_handler(modules)
        assert isinstance(handler, MockHomeAssistantHandler)
    finally:
        _teardown(runtime, adapter_manager)
