"""
tests/test_p0_camera_automation.py
====================================

LUNO P0 (Camera Automation / Safe Integration & Non-Regression
Protocol) - dedicated regression suite for `luno/camera_automation/`.

Builds on the SAME real bootstrap (`register_all_modules`/
`register_all_adapters`, all-mock backends by default) Sprint 71/72's own
`tests/test_sprint71_camera_patrol.py` / `tests/
test_sprint72_automation_engine.py` already established - no physical
camera/Home Assistant server is ever needed.

Sections:
  A. `CameraAutomationConfig` - pure, no bootstrap.
  B. `CameraAutomationModule` in isolation (fake event bus) - filtering,
     dedupe, cooldown, fail-safe exception isolation, disabled-by-default.
  C. Real bootstrap, E2E - confirms this module is truly OFF by default
     (§10), confirms the EXISTING `HomeAssistantAdapter` inbound/outbound
     behavior is completely unaffected by this module's presence (§17
     regression gate - the exact same assertions `test_adapters.py::
     test_home_assistant_event` makes), and confirms the full
     `device_state_changed -> camera_automation.state_changed ->
     AutomationEngine rule -> home_assistant.turn_on` pipeline works
     end to end with ZERO changes to `AutomationEngine` or
     `HomeAssistantAdapter` (proving the "reuse existing automation
     system / existing HA integration" claim, not just asserting it).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402

from luno.camera_automation import CameraAutomationConfig, CameraAutomationModule, OUTPUT_EVENT_TYPE  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _write_rules(path: str, rules: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh)


def _build_stack(camera_config: Optional[CameraAutomationConfig] = None, rules: Optional[Dict[str, Any]] = None):
    """Real bootstrap - same helper convention `tests/
    test_sprint72_automation_engine.py::_build_stack` already established.
    If `camera_config` is given, it REPLACES the module's config after
    construction but before `runtime.start()` - the same "mutate a private
    attribute between construction and start()" seam that suite's own
    `engine._rules_path = rules_path` already uses; never touches
    `os.environ`, so this suite cannot leak env state into any other
    test/process."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    cam_module: CameraAutomationModule = modules["camera_automation_module"]
    if camera_config is not None:
        cam_module._config = camera_config

    rules_path = None
    if rules is not None:
        engine = modules["automation_engine"]
        fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="automation_rules_p0_test_")
        os.close(fd)
        _write_rules(rules_path, rules)
        engine._rules_path = rules_path

    return runtime, modules, adapters, adapter_manager, rules_path


def _teardown(runtime, adapter_manager, rules_path: Optional[str] = None) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()
    if rules_path is not None:
        try:
            os.remove(rules_path)
        except OSError:
            pass


# ============================================================================
# A. CameraAutomationConfig - pure (no bootstrap)
# ============================================================================

def test_01_default_config_is_disabled_with_empty_allowlist():
    cfg = CameraAutomationConfig()
    assert cfg.enabled is False
    assert cfg.entities == []
    assert cfg.cooldown_s == 10.0


def test_02_from_env_defaults_match_dataclass_defaults(monkeypatch):
    for var in ("CAMERA_AUTOMATION_ENABLED", "CAMERA_AUTOMATION_ENTITIES", "CAMERA_AUTOMATION_COOLDOWN_S"):
        monkeypatch.delenv(var, raising=False)
    cfg = CameraAutomationConfig.from_env()
    assert cfg.enabled is False
    assert cfg.entities == []
    assert cfg.cooldown_s == 10.0


def test_03_from_env_parses_enabled_entities_cooldown(monkeypatch):
    monkeypatch.setenv("CAMERA_AUTOMATION_ENABLED", "true")
    monkeypatch.setenv("CAMERA_AUTOMATION_ENTITIES", "binary_sensor.front_door_motion, camera.driveway")
    monkeypatch.setenv("CAMERA_AUTOMATION_COOLDOWN_S", "5")
    cfg = CameraAutomationConfig.from_env()
    assert cfg.enabled is True
    assert cfg.entities == ["binary_sensor.front_door_motion", "camera.driveway"]
    assert cfg.cooldown_s == 5.0


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_04_from_env_falsy_strings_mean_disabled(monkeypatch, raw):
    monkeypatch.setenv("CAMERA_AUTOMATION_ENABLED", raw)
    assert CameraAutomationConfig.from_env().enabled is False


# ============================================================================
# B. CameraAutomationModule in isolation (fake event bus, no bootstrap)
# ============================================================================

class _FakeEventBus:
    def __init__(self) -> None:
        self.published: List[Event] = []
        self._subs: Dict[str, List[Any]] = {}
        self.sub_count = 0

    def subscribe(self, event_type: str, handler: Any, priority: int = 0) -> str:
        self._subs.setdefault(event_type, []).append(handler)
        self.sub_count += 1
        return f"sub-{self.sub_count}"

    def unsubscribe(self, sub_id: str) -> None:
        pass

    def publish(self, event: Event) -> None:
        self.published.append(event)


def test_05_disabled_module_never_subscribes():
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=False, entities=["binary_sensor.x"]))
    mod.bind_event_bus(bus)
    mod.start()
    assert bus.sub_count == 0


def test_06_enabled_module_subscribes_only_to_device_state_changed():
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.x"]))
    mod.bind_event_bus(bus)
    mod.start()
    assert bus.sub_count == 1
    assert list(bus._subs.keys()) == ["device_state_changed"]


def test_07_non_allowlisted_entity_is_ignored():
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door"]))
    mod.bind_event_bus(bus)
    mod.start()
    mod._on_device_state_changed(Event(type="device_state_changed", data={"entity_id": "light.kitchen", "old_state": "off", "new_state": "on"}))
    assert bus.published == []


def test_08_allowlisted_entity_publishes_camera_automation_event():
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door"], cooldown_s=0.0))
    mod.bind_event_bus(bus)
    mod.start()
    mod._on_device_state_changed(Event(type="device_state_changed", data={"entity_id": "binary_sensor.front_door", "old_state": "off", "new_state": "on"}))
    assert len(bus.published) == 1
    out = bus.published[0]
    assert out.type == OUTPUT_EVENT_TYPE == "camera_automation.state_changed"
    assert out.data == {"entity_id": "binary_sensor.front_door", "old_state": "off", "new_state": "on"}


def test_09_dedupe_suppresses_identical_repeat_state():
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door"], cooldown_s=0.0))
    mod.bind_event_bus(bus)
    mod.start()
    ev = Event(type="device_state_changed", data={"entity_id": "binary_sensor.front_door", "old_state": "off", "new_state": "on"})
    mod._on_device_state_changed(ev)
    mod._on_device_state_changed(ev)  # identical new_state again - no-op re-fire
    assert len(bus.published) == 1


def test_10_cooldown_suppresses_rapid_changes():
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door"], cooldown_s=60.0))
    mod.bind_event_bus(bus)
    mod.start()
    mod._on_device_state_changed(Event(type="device_state_changed", data={"entity_id": "binary_sensor.front_door", "old_state": "off", "new_state": "on"}))
    mod._on_device_state_changed(Event(type="device_state_changed", data={"entity_id": "binary_sensor.front_door", "old_state": "on", "new_state": "off"}))
    assert len(bus.published) == 1  # second change within cooldown window suppressed


def test_11_missing_entity_id_does_not_raise():
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door"]))
    mod.bind_event_bus(bus)
    mod.start()
    mod._on_device_state_changed(Event(type="device_state_changed", data={}))
    assert bus.published == []


def test_12_fail_safe_exception_in_handler_is_isolated(monkeypatch):
    """§11 - a broken camera integration must never propagate. Forces
    `_handle` to raise and asserts the PUBLIC entry point
    (`_on_device_state_changed`, the actual Event Bus subscriber
    callback) swallows it without re-raising."""
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door"]))
    mod.bind_event_bus(bus)
    mod.start()

    def _boom(event):
        raise RuntimeError("simulated camera subsystem failure")

    monkeypatch.setattr(mod, "_handle", _boom)
    mod._on_device_state_changed(Event(type="device_state_changed", data={"entity_id": "binary_sensor.front_door", "old_state": "off", "new_state": "on"}))
    # no exception raised past this point == test passes


def test_13_stop_unsubscribes_cleanly():
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door"]))
    mod.bind_event_bus(bus)
    mod.start()
    mod.stop()  # must not raise even though _FakeEventBus.unsubscribe is a no-op
    assert mod._bus_sub_id is None


def test_14_health_reports_enabled_and_allowlist_size():
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["a", "b"]))
    health = mod.health()
    assert health.healthy is True
    assert "enabled=True" in health.message
    assert "allowlisted=2" in health.message


# ============================================================================
# C. Real bootstrap - E2E (regression + integration)
# ============================================================================

def test_15_disabled_by_default_real_bootstrap_no_subscription_footprint():
    """§10/§17 - with no env var set (the exact state of a fresh
    checkout), `register_all_modules` must produce a `camera_automation_
    module` that is present but inert: starting the real runtime does
    NOT subscribe it to the Event Bus at all."""
    runtime, modules, adapters, adapter_manager, _ = _build_stack()
    try:
        cam_module = modules["camera_automation_module"]
        assert cam_module._config.enabled is False
        runtime.start()
        assert cam_module._bus_sub_id is None
    finally:
        _teardown(runtime, adapter_manager)


def test_16_existing_ha_adapter_inbound_outbound_unaffected():
    """§17 regression gate - byte-for-byte the same assertions `luno/
    adapters/tests/test_adapters.py::test_home_assistant_event` makes,
    run again HERE with the new `camera_automation_module` registered
    and started alongside it, proving the existing HA integration's
    inbound (`device_state_changed`/`automation_triggered`) and outbound
    (`tool_requested` -> `call_service`) behavior is completely
    unaffected by this module's presence, even when this module is
    enabled."""
    cam_config = CameraAutomationConfig(enabled=True, entities=["switch.fan"], cooldown_s=0.0)
    runtime, modules, adapters, adapter_manager, _ = _build_stack(camera_config=cam_config)
    try:
        runtime.start()
        ha_adapter = adapters["home_assistant_adapter"]
        src = ha_adapter.source
        client = ha_adapter.client

        device_events = []
        automation_events = []
        runtime.event_bus.subscribe("device_state_changed", lambda e: device_events.append(e.data))
        runtime.event_bus.subscribe("automation_triggered", lambda e: automation_events.append(e.data))

        src.simulate_state_change("switch.fan", "off", "on")
        src.simulate_automation("night_mode")
        assert _wait_until(lambda: len(device_events) == 1 and len(automation_events) == 1)

        runtime.event_bus.publish(Event(type="tool_requested", data={"tool": "home_assistant", "action": "turn_off", "target": "switch.fan"}))
        assert _wait_until(lambda: len(client.calls) == 1)
    finally:
        _teardown(runtime, adapter_manager)


def test_17_allowlisted_state_change_publishes_camera_automation_event_e2e():
    cam_config = CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door_motion"], cooldown_s=0.0)
    runtime, modules, adapters, adapter_manager, _ = _build_stack(camera_config=cam_config)
    try:
        runtime.start()
        ha_adapter = adapters["home_assistant_adapter"]
        src = ha_adapter.source

        camera_events = []
        runtime.event_bus.subscribe(OUTPUT_EVENT_TYPE, lambda e: camera_events.append(e.data))

        src.simulate_state_change("binary_sensor.front_door_motion", "off", "on")
        assert _wait_until(lambda: len(camera_events) == 1)
        assert camera_events[0]["entity_id"] == "binary_sensor.front_door_motion"
        assert camera_events[0]["new_state"] == "on"

        # A non-allowlisted entity on the SAME real HA adapter must not
        # produce a camera_automation event.
        src.simulate_state_change("light.kitchen", "off", "on")
        time.sleep(0.1)
        assert len(camera_events) == 1
    finally:
        _teardown(runtime, adapter_manager)


def test_18_camera_automation_event_triggers_existing_automation_engine_rule_e2e():
    """Proves the P0 brief's central architectural claim end to end:
    ZERO changes to `AutomationEngine` were needed for it to react to a
    camera-domain event and issue an EXISTING `home_assistant.turn_on`
    action - this rule is plain data, not new engine code."""
    rules = {
        "porch_light_on_motion": {
            "name": "porch_light_on_motion",
            "enabled": True,
            "trigger": {"type": "event", "parameters": {"event_name": OUTPUT_EVENT_TYPE}},
            "conditions": [],
            "actions": [{"type": "home_assistant.turn_on", "parameters": {"target": "light.porch"}}],
            "cooldown_seconds": 0.0,
        }
    }
    cam_config = CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door_motion"], cooldown_s=0.0)
    runtime, modules, adapters, adapter_manager, rules_path = _build_stack(camera_config=cam_config, rules=rules)
    try:
        runtime.start()
        ha_adapter = adapters["home_assistant_adapter"]
        src = ha_adapter.source
        # The AutomationEngine's `home_assistant.turn_on` action dispatches
        # through the REAL `tool_requested` -> ToolManagerBridgeModule ->
        # ToolManager -> "home_assistant" handler round trip (the SAME
        # `MockHomeAssistantHandler` a manual voice command would use, not
        # the adapter's own separate inbound/outbound `MockHomeAssistant
        # Client`) - see `luno/tool_manager/builtin/home_assistant.py`.
        ha_tool_handler = modules["tool_manager_module"].manager.registry.get("home_assistant")

        src.simulate_state_change("binary_sensor.front_door_motion", "off", "on")

        def _fired():
            return ha_tool_handler._state.get("light.porch", {}).get("on") is True

        assert _wait_until(_fired, timeout_s=5.0)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_19_camera_automation_module_registered_and_healthy_in_real_bootstrap():
    runtime, modules, adapters, adapter_manager, _ = _build_stack()
    try:
        cam_module = modules["camera_automation_module"]
        assert isinstance(cam_module, CameraAutomationModule)
        health = cam_module.health()
        assert health.healthy is True
    finally:
        _teardown(runtime, adapter_manager)
