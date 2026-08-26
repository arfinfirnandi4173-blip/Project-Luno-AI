"""
tests/test_p0_5_camera_integration.py
========================================

LUNO P0.5 (Real Camera Integration) - dedicated regression suite for
`luno/camera_automation/cameras.py` and the new classification branch it
adds to `CameraAutomationModule._handle`.

Sections:
  A. Entity mapping (`build_entity_role_index`) - pure, no bootstrap.
  B. Event conversion (`classify_state_change`) - pure, no bootstrap.
  C. `load_camera_profiles` - config-driven mapping, malformed-input
     safety.
  D. Metadata (`CameraEvent.to_dict()`).
  E. `CameraAutomationModule` in isolation (fake event bus) - the new
     branch, shared dedupe/cooldown with the legacy P0 relay, unknown-
     entity handling, disabled-by-default remains inert.
  F. Real bootstrap, E2E - HA state change -> CameraEvent -> Event Bus,
     full pipeline through to an existing AutomationEngine rule, and
     isolation (malformed event / HA-adjacent failure never crashes
     Luno).

This file does NOT modify or re-run `tests/test_p0_camera_automation.py`
- that suite is this sprint's own "baseline" and is run UNCHANGED as
part of the regression gate (see `docs/change_impact/
camera_automation_p0_5.md`).
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

from luno.camera_automation import (  # noqa: E402
    CAMERA_EVENT_TYPE,
    CameraAutomationConfig,
    CameraAutomationModule,
    CameraEvent,
    CameraProfile,
    OUTPUT_EVENT_TYPE,
    build_entity_role_index,
    classify_state_change,
    load_camera_profiles,
)

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _tapo_profile(**overrides) -> CameraProfile:
    defaults = dict(
        camera_id="tapo_c212",
        camera_entity="camera.tapo_c212",
        motion_entity="binary_sensor.tapo_c212_motion",
        human_entity="binary_sensor.tapo_c212_person",
        availability_entity="binary_sensor.tapo_c212_connectivity",
    )
    defaults.update(overrides)
    return CameraProfile(**defaults)


def _write_cameras_json(path: str, cameras: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"cameras": cameras}, fh)


# ============================================================================
# A. Entity mapping (pure)
# ============================================================================

def test_01_valid_camera_motion_human_entities_all_indexed():
    profile = _tapo_profile()
    index = build_entity_role_index([profile])
    assert index["camera.tapo_c212"] == (profile, "camera")
    assert index["binary_sensor.tapo_c212_motion"] == (profile, "motion")
    assert index["binary_sensor.tapo_c212_person"] == (profile, "human")
    assert index["binary_sensor.tapo_c212_connectivity"] == (profile, "availability")


def test_02_unknown_entity_not_in_index():
    index = build_entity_role_index([_tapo_profile()])
    assert "camera.random_camera" not in index
    assert "light.kitchen" not in index


def test_03_missing_entity_configuration_profile_contributes_nothing():
    """A CameraProfile with every role left unconfigured (exactly what
    config/camera_automation.json ships with by default) must not
    register any entity at all."""
    profile = CameraProfile(camera_id="tapo_c212")
    index = build_entity_role_index([profile])
    assert index == {}
    assert profile.entities() == []


def test_04_profile_with_only_motion_entity_is_valid():
    profile = CameraProfile(camera_id="cam2", motion_entity="binary_sensor.cam2_motion")
    index = build_entity_role_index([profile])
    assert list(index.keys()) == ["binary_sensor.cam2_motion"]


# ============================================================================
# B. Event conversion (pure)
# ============================================================================

def test_05_motion_on_classifies_motion_detected():
    index = build_entity_role_index([_tapo_profile()])
    ev = classify_state_change(index, "binary_sensor.tapo_c212_motion", "off", "on")
    assert ev.kind == "motion_detected"
    assert ev.camera_id == "tapo_c212"


def test_06_motion_off_classifies_motion_cleared():
    index = build_entity_role_index([_tapo_profile()])
    ev = classify_state_change(index, "binary_sensor.tapo_c212_motion", "on", "off")
    assert ev.kind == "motion_cleared"


def test_07_human_on_classifies_human_detected():
    index = build_entity_role_index([_tapo_profile()])
    ev = classify_state_change(index, "binary_sensor.tapo_c212_person", "off", "on")
    assert ev.kind == "human_detected"


def test_08_human_off_classifies_human_cleared():
    index = build_entity_role_index([_tapo_profile()])
    ev = classify_state_change(index, "binary_sensor.tapo_c212_person", "on", "off")
    assert ev.kind == "human_cleared"


def test_09_availability_on_classifies_camera_online():
    index = build_entity_role_index([_tapo_profile()])
    ev = classify_state_change(index, "binary_sensor.tapo_c212_connectivity", "off", "on")
    assert ev.kind == "camera_online"


def test_10_availability_off_classifies_camera_offline():
    index = build_entity_role_index([_tapo_profile()])
    ev = classify_state_change(index, "binary_sensor.tapo_c212_connectivity", "on", "off")
    assert ev.kind == "camera_offline"


def test_11_generic_unavailable_state_is_camera_offline_not_no_motion():
    """Section 12 - "camera unavailable" must be distinct from "no
    motion", never collapsed into a false motion_cleared."""
    index = build_entity_role_index([_tapo_profile()])
    ev = classify_state_change(index, "binary_sensor.tapo_c212_motion", "on", "unavailable")
    assert ev is None  # deferred to the dedicated availability_entity - never a false motion_cleared


def test_12_unavailable_without_dedicated_availability_entity_falls_back_to_offline():
    profile = CameraProfile(camera_id="cam2", motion_entity="binary_sensor.cam2_motion")
    index = build_entity_role_index([profile])
    ev = classify_state_change(index, "binary_sensor.cam2_motion", "on", "unavailable")
    assert ev.kind == "camera_offline"
    assert ev.camera_id == "cam2"


def test_13_malformed_event_missing_new_state_does_not_raise():
    index = build_entity_role_index([_tapo_profile()])
    ev = classify_state_change(index, "binary_sensor.tapo_c212_motion", "off", None)
    assert ev is None  # not "on"/"off" - not classified, no exception


def test_14_unknown_entity_returns_none():
    index = build_entity_role_index([_tapo_profile()])
    assert classify_state_change(index, "camera.random_camera", "off", "on") is None


def test_15_camera_entity_ordinary_transition_classifies_to_nothing():
    index = build_entity_role_index([_tapo_profile()])  # has a dedicated availability_entity
    ev = classify_state_change(index, "camera.tapo_c212", "idle", "recording")
    assert ev is None


# ============================================================================
# C. load_camera_profiles - config-driven, malformed-input safety
# ============================================================================

def test_16_load_camera_profiles_valid_file():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        _write_cameras_json(path, {"tapo_c212": {
            "camera_entity": "camera.tapo_c212", "motion_entity": "binary_sensor.tapo_c212_motion",
            "human_entity": None, "availability_entity": None,
        }})
        profiles = load_camera_profiles(path)
        assert len(profiles) == 1
        assert profiles[0].camera_id == "tapo_c212"
        assert profiles[0].camera_entity == "camera.tapo_c212"
        assert profiles[0].human_entity is None
    finally:
        os.remove(path)


def test_17_load_camera_profiles_missing_file_returns_empty():
    assert load_camera_profiles("/nonexistent/path/does_not_exist.json") == []


def test_18_load_camera_profiles_malformed_json_returns_empty_not_raises():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        with open(path, "w") as fh:
            fh.write("{not valid json")
        assert load_camera_profiles(path) == []
    finally:
        os.remove(path)


def test_19_load_camera_profiles_default_shipped_file_has_no_real_entities():
    """config/camera_automation.json (this project's own shipped
    default) must ship with every role null - no fabricated entity ids
    (P0.5 brief Section 4/6)."""
    profiles = load_camera_profiles("config/camera_automation.json")
    index = build_entity_role_index(profiles)
    assert index == {}  # zero real entities configured out of the box


# ============================================================================
# D. Metadata
# ============================================================================

def test_20_camera_event_confidence_is_always_none():
    index = build_entity_role_index([_tapo_profile()])
    ev = classify_state_change(index, "binary_sensor.tapo_c212_person", "off", "on")
    assert ev.confidence is None  # never invented (Section 10)


def test_21_camera_event_carries_camera_id_entity_id_timestamp_source():
    index = build_entity_role_index([_tapo_profile()])
    before = time.time()
    ev = classify_state_change(index, "binary_sensor.tapo_c212_motion", "off", "on")
    after = time.time()
    assert ev.camera_id == "tapo_c212"
    assert ev.entity_id == "binary_sensor.tapo_c212_motion"
    assert ev.source == "tapo_c212"
    assert before <= ev.timestamp <= after


def test_22_camera_id_is_stable_not_derived_from_entity_id():
    """Section 7 - camera_id must never be a transient HA event id."""
    profile = CameraProfile(camera_id="front_door_cam", motion_entity="binary_sensor.xyz123_motion")
    index = build_entity_role_index([profile])
    ev = classify_state_change(index, "binary_sensor.xyz123_motion", "off", "on")
    assert ev.camera_id == "front_door_cam"
    assert ev.camera_id != ev.entity_id


# ============================================================================
# E. CameraAutomationModule in isolation (fake event bus, no bootstrap)
# ============================================================================

class _FakeEventBus:
    def __init__(self) -> None:
        self.published: List[Event] = []
        self._subs: Dict[str, List[Any]] = {}

    def subscribe(self, event_type: str, handler: Any, priority: int = 0) -> str:
        self._subs.setdefault(event_type, []).append(handler)
        return f"sub-{len(self._subs)}"

    def unsubscribe(self, sub_id: str) -> None:
        pass

    def publish(self, event: Event) -> None:
        self.published.append(event)


def _module_with_cameras(cameras: Dict[str, Any], **config_overrides) -> "tuple[CameraAutomationModule, _FakeEventBus, str]":
    fd, path = tempfile.mkstemp(suffix=".json", prefix="camera_automation_p0_5_test_")
    os.close(fd)
    _write_cameras_json(path, cameras)
    bus = _FakeEventBus()
    cfg = CameraAutomationConfig(enabled=True, cameras_path=path, cooldown_s=0.0, **config_overrides)
    mod = CameraAutomationModule(config=cfg)
    mod.bind_event_bus(bus)
    mod.start()
    return mod, bus, path


def test_23_classified_motion_event_published_as_camera_event_type():
    mod, bus, path = _module_with_cameras({"tapo_c212": {"motion_entity": "binary_sensor.tapo_c212_motion"}})
    try:
        mod._on_device_state_changed(Event(type="device_state_changed", data={
            "entity_id": "binary_sensor.tapo_c212_motion", "old_state": "off", "new_state": "on",
        }))
        assert len(bus.published) == 1
        assert bus.published[0].type == CAMERA_EVENT_TYPE
        assert bus.published[0].data["kind"] == "motion_detected"
    finally:
        os.remove(path)


def test_24_legacy_flat_allowlist_relay_still_works_unchanged():
    """P0's own path, exercised again here with cameras.py present but
    NOT covering this entity - proves zero interference."""
    mod, bus, path = _module_with_cameras({}, entities=["switch.unrelated"])
    try:
        mod._on_device_state_changed(Event(type="device_state_changed", data={
            "entity_id": "switch.unrelated", "old_state": "off", "new_state": "on",
        }))
        assert len(bus.published) == 1
        assert bus.published[0].type == OUTPUT_EVENT_TYPE
        assert bus.published[0].data == {"entity_id": "switch.unrelated", "old_state": "off", "new_state": "on"}
    finally:
        os.remove(path)


def test_25_camera_profile_entity_never_falls_through_to_legacy_relay():
    """An entity covered by a CameraProfile must never ALSO be treated
    via the flat entities allowlist, even if it happens to appear in
    both (classification takes exclusive precedence)."""
    mod, bus, path = _module_with_cameras(
        {"tapo_c212": {"motion_entity": "binary_sensor.tapo_c212_motion"}},
        entities=["binary_sensor.tapo_c212_motion"],
    )
    try:
        mod._on_device_state_changed(Event(type="device_state_changed", data={
            "entity_id": "binary_sensor.tapo_c212_motion", "old_state": "off", "new_state": "on",
        }))
        assert len(bus.published) == 1
        assert bus.published[0].type == CAMERA_EVENT_TYPE  # NOT OUTPUT_EVENT_TYPE
    finally:
        os.remove(path)


def test_26_camera_profile_entity_with_no_event_worthy_transition_publishes_nothing():
    mod, bus, path = _module_with_cameras({"tapo_c212": {"camera_entity": "camera.tapo_c212",
                                                           "availability_entity": "binary_sensor.tapo_c212_connectivity"}})
    try:
        mod._on_device_state_changed(Event(type="device_state_changed", data={
            "entity_id": "camera.tapo_c212", "old_state": "idle", "new_state": "recording",
        }))
        assert bus.published == []
    finally:
        os.remove(path)


def test_27_unknown_entity_publishes_nothing_and_does_not_raise():
    mod, bus, path = _module_with_cameras({"tapo_c212": {"motion_entity": "binary_sensor.tapo_c212_motion"}})
    try:
        mod._on_device_state_changed(Event(type="device_state_changed", data={
            "entity_id": "camera.random_camera", "old_state": "off", "new_state": "on",
        }))
        assert bus.published == []
    finally:
        os.remove(path)


def test_28_shared_dedupe_cooldown_suppresses_repeat_classified_event():
    fd, path = tempfile.mkstemp(suffix=".json", prefix="camera_automation_p0_5_test_")
    os.close(fd)
    try:
        _write_cameras_json(path, {"tapo_c212": {"motion_entity": "binary_sensor.tapo_c212_motion"}})
        bus = _FakeEventBus()
        cfg = CameraAutomationConfig(enabled=True, cameras_path=path, cooldown_s=60.0)
        mod = CameraAutomationModule(config=cfg)
        mod.bind_event_bus(bus)
        mod.start()
        ev = Event(type="device_state_changed", data={"entity_id": "binary_sensor.tapo_c212_motion", "old_state": "off", "new_state": "on"})
        mod._on_device_state_changed(ev)
        mod._on_device_state_changed(ev)
        assert len(bus.published) == 1  # second within cooldown suppressed - same mechanism as P0's own test_10
    finally:
        os.remove(path)


def test_29_disabled_module_ignores_camera_profiles_entirely():
    fd, path = tempfile.mkstemp(suffix=".json", prefix="camera_automation_p0_5_test_")
    os.close(fd)
    try:
        _write_cameras_json(path, {"tapo_c212": {"motion_entity": "binary_sensor.tapo_c212_motion"}})
        bus = _FakeEventBus()
        mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=False, cameras_path=path))
        mod.bind_event_bus(bus)
        mod.start()
        assert mod._bus_sub_id is None
        assert mod._camera_profiles == []  # reload_cameras() is never called when disabled
    finally:
        os.remove(path)


def test_30_malformed_cameras_file_does_not_prevent_module_start():
    fd, path = tempfile.mkstemp(suffix=".json", prefix="camera_automation_p0_5_test_")
    os.close(fd)
    try:
        with open(path, "w") as fh:
            fh.write("{not valid json")
        bus = _FakeEventBus()
        mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cameras_path=path))
        mod.bind_event_bus(bus)
        mod.start()  # must not raise
        assert mod._bus_sub_id is not None  # still subscribes - degrades to zero cameras, not a crash
    finally:
        os.remove(path)


def test_31_exception_during_classification_is_isolated(monkeypatch):
    mod, bus, path = _module_with_cameras({"tapo_c212": {"motion_entity": "binary_sensor.tapo_c212_motion"}})
    try:
        import luno.camera_automation.module as module_mod

        def _boom(*a, **kw):
            raise RuntimeError("simulated classification failure")

        monkeypatch.setattr(module_mod, "classify_state_change", _boom)
        mod._on_device_state_changed(Event(type="device_state_changed", data={
            "entity_id": "binary_sensor.tapo_c212_motion", "old_state": "off", "new_state": "on",
        }))
        # no exception raised past this point == isolated (Section 16/§11)
    finally:
        os.remove(path)


# ============================================================================
# F. Real bootstrap - E2E (integration + isolation)
# ============================================================================

def _build_stack(camera_config: Optional[CameraAutomationConfig] = None, rules: Optional[Dict[str, Any]] = None):
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
        fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="automation_rules_p0_5_test_")
        os.close(fd)
        with open(rules_path, "w", encoding="utf-8") as fh:
            json.dump(rules, fh)
        engine._rules_path = rules_path

    return runtime, modules, adapters, adapter_manager, rules_path


def _teardown(runtime, adapter_manager, rules_path: Optional[str] = None) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()
    if rules_path is not None:
        try:
            os.remove(rules_path)
        except OSError:
            pass


def test_32_ha_motion_event_through_real_adapter_becomes_camera_event_e2e():
    """Section 18's own "Integration" test: HA Event -> Adapter ->
    CameraEvent -> existing P0 module, exercised through the REAL
    `HomeAssistantAdapter`/`MockHomeAssistantSource`, not a fake bus."""
    fd, cameras_path = tempfile.mkstemp(suffix=".json", prefix="camera_automation_p0_5_test_")
    os.close(fd)
    try:
        _write_cameras_json(cameras_path, {"tapo_c212": {
            "motion_entity": "binary_sensor.tapo_c212_motion", "human_entity": "binary_sensor.tapo_c212_person",
        }})
        cam_config = CameraAutomationConfig(enabled=True, cameras_path=cameras_path, cooldown_s=0.0)
        runtime, modules, adapters, adapter_manager, _ = _build_stack(camera_config=cam_config)
        try:
            runtime.start()
            ha_adapter = adapters["home_assistant_adapter"]
            src = ha_adapter.source

            camera_events = []
            runtime.event_bus.subscribe(CAMERA_EVENT_TYPE, lambda e: camera_events.append(e.data))

            src.simulate_state_change("binary_sensor.tapo_c212_motion", "off", "on")
            assert _wait_until(lambda: len(camera_events) == 1)
            assert camera_events[0]["kind"] == "motion_detected"
            assert camera_events[0]["camera_id"] == "tapo_c212"

            src.simulate_state_change("binary_sensor.tapo_c212_person", "off", "on")
            assert _wait_until(lambda: len(camera_events) == 2)
            assert camera_events[1]["kind"] == "human_detected"
        finally:
            _teardown(runtime, adapter_manager)
    finally:
        os.remove(cameras_path)


def test_33_camera_event_triggers_existing_automation_engine_rule_e2e():
    """Full pipeline: HA motion -> CameraAutomationModule -> camera_
    automation.camera_event -> existing AutomationEngine rule ->
    existing home_assistant.turn_on action - zero engine changes,
    mirroring P0's own test_18 one layer further upstream."""
    fd, cameras_path = tempfile.mkstemp(suffix=".json", prefix="camera_automation_p0_5_test_")
    os.close(fd)
    try:
        _write_cameras_json(cameras_path, {"tapo_c212": {"motion_entity": "binary_sensor.tapo_c212_motion"}})
        rules = {
            "porch_light_on_motion": {
                "name": "porch_light_on_motion", "enabled": True,
                "trigger": {"type": "event", "parameters": {"event_name": CAMERA_EVENT_TYPE}},
                "conditions": [], "actions": [{"type": "home_assistant.turn_on", "parameters": {"target": "light.porch"}}],
                "cooldown_seconds": 0.0,
            }
        }
        cam_config = CameraAutomationConfig(enabled=True, cameras_path=cameras_path, cooldown_s=0.0)
        runtime, modules, adapters, adapter_manager, rules_path = _build_stack(camera_config=cam_config, rules=rules)
        try:
            runtime.start()
            ha_adapter = adapters["home_assistant_adapter"]
            src = ha_adapter.source
            ha_tool_handler = modules["tool_manager_module"].manager.registry.get("home_assistant")

            src.simulate_state_change("binary_sensor.tapo_c212_motion", "off", "on")

            def _fired():
                return ha_tool_handler._state.get("light.porch", {}).get("on") is True

            assert _wait_until(_fired, timeout_s=5.0)
        finally:
            _teardown(runtime, adapter_manager, rules_path)
    finally:
        os.remove(cameras_path)


def test_34_unknown_entity_never_triggers_automation_e2e():
    fd, cameras_path = tempfile.mkstemp(suffix=".json", prefix="camera_automation_p0_5_test_")
    os.close(fd)
    try:
        _write_cameras_json(cameras_path, {"tapo_c212": {"motion_entity": "binary_sensor.tapo_c212_motion"}})
        cam_config = CameraAutomationConfig(enabled=True, cameras_path=cameras_path, cooldown_s=0.0)
        runtime, modules, adapters, adapter_manager, _ = _build_stack(camera_config=cam_config)
        try:
            runtime.start()
            ha_adapter = adapters["home_assistant_adapter"]
            src = ha_adapter.source

            camera_events = []
            runtime.event_bus.subscribe(CAMERA_EVENT_TYPE, lambda e: camera_events.append(e.data))

            src.simulate_state_change("camera.random_camera", "off", "on")
            time.sleep(0.1)
            assert camera_events == []
        finally:
            _teardown(runtime, adapter_manager)
    finally:
        os.remove(cameras_path)


def test_35_disabled_camera_automation_remains_inert_e2e():
    runtime, modules, adapters, adapter_manager, _ = _build_stack()  # no camera_config override - default disabled
    try:
        cam_module = modules["camera_automation_module"]
        assert cam_module._config.enabled is False
        runtime.start()
        assert cam_module._bus_sub_id is None
        assert cam_module._camera_profiles == []

        ha_adapter = adapters["home_assistant_adapter"]
        src = ha_adapter.source
        camera_events = []
        runtime.event_bus.subscribe(CAMERA_EVENT_TYPE, lambda e: camera_events.append(e.data))
        src.simulate_state_change("binary_sensor.tapo_c212_motion", "off", "on")
        time.sleep(0.1)
        assert camera_events == []
    finally:
        _teardown(runtime, adapter_manager)


def test_36_existing_ha_adapter_behavior_unaffected_with_p0_5_active():
    """Section 19/§17 regression gate, run one more time with the new
    classification branch actually configured and active - the exact
    same assertions P0's own test_16 makes."""
    fd, cameras_path = tempfile.mkstemp(suffix=".json", prefix="camera_automation_p0_5_test_")
    os.close(fd)
    try:
        _write_cameras_json(cameras_path, {"tapo_c212": {"motion_entity": "binary_sensor.tapo_c212_motion"}})
        cam_config = CameraAutomationConfig(enabled=True, cameras_path=cameras_path, cooldown_s=0.0)
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
    finally:
        os.remove(cameras_path)
