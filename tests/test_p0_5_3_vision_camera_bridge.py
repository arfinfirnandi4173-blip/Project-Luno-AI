"""
tests/test_p0_5_3_vision_camera_bridge.py
=============================================

LUNO P0.5.3 (Vision Event -> Camera Automation Bridge) - dedicated test
suite for `luno.camera_automation.vision_bridge.VisionCameraEventBridge`
and the two small, additive methods it relies on
(`CameraAutomationModule.is_enabled()`/`ingest_external_camera_event()`).

Sections:
  A. Event mapping (person enter/leave, camera disconnect/reconnect).
  B. Unknown event handling.
  C. Confidence (always None - never fabricated).
  D. Camera ID (default + override, never derived from a fake HA id).
  E. Failure isolation (bridge exception never propagates/crashes).
  F. Feature flag (disabled => zero subscription, zero processing).
  G. No motion fabrication.
  H. `CameraAutomationModule.ingest_external_camera_event()`/`is_enabled()`
     direct coverage (reuses existing dedupe/cooldown - Section 9 proof).
  I. Real bootstrap E2E (register_all_modules) - confirms the bridge is
     wired, confirms disabled-by-default (§10/§12), confirms a live
     Vision event genuinely reaches `camera_automation.camera_event` end
     to end with zero changes to VisionAdapter/AutomationEngine.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.events import CameraDisconnected, CameraPersonEntered, CameraPersonLeft, CameraReconnected, HumanPresenceConfirmed, HumanPresenceUnconfirmed  # noqa: E402
from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.camera_automation import CAMERA_EVENT_TYPE, CameraAutomationConfig, CameraAutomationModule  # noqa: E402
from luno.camera_automation.cameras import CameraEvent  # noqa: E402
from luno.camera_automation.vision_bridge import VisionCameraEventBridge  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


class _FakeEventBus:
    """Same small local convention `tests/test_p0_camera_automation.py`'s
    own `_FakeEventBus` already established - self-contained per test
    file, records subscriptions/publishes, dispatches synchronously."""

    def __init__(self) -> None:
        self.published: List[Event] = []
        self._subs: Dict[str, List[Any]] = {}
        self.sub_count = 0
        self.unsub_calls: List[str] = []

    def subscribe(self, event_type: str, handler: Any, priority: int = 0) -> str:
        self._subs.setdefault(event_type, []).append(handler)
        self.sub_count += 1
        return f"sub-{self.sub_count}"

    def unsubscribe(self, sub_id: str) -> None:
        self.unsub_calls.append(sub_id)

    def publish(self, event: Event) -> None:
        self.published.append(event)

    def fire(self, event_type: str, event: Event) -> None:
        for handler in self._subs.get(event_type, []):
            handler(event)


def _enabled_camera_automation_module() -> CameraAutomationModule:
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=0.0))
    mod.bind_event_bus(_FakeEventBus())
    return mod


# ============================================================================
# A. Event mapping
# ============================================================================

def test_01_person_entered_maps_to_human_detected():
    cam = _enabled_camera_automation_module()
    bridge = VisionCameraEventBridge(camera_automation=cam, camera_id="tapo_c212")
    bridge.bind_event_bus(_FakeEventBus())
    bridge.start()

    bridge._on_person_entered(Event(type=CameraPersonEntered.EVENT_TYPE))

    published = cam._event_bus.published
    assert len(published) == 1
    assert published[0].type == CAMERA_EVENT_TYPE
    assert published[0].data["kind"] == "human_detected"
    assert published[0].data["camera_id"] == "tapo_c212"
    assert published[0].data["source"] == "vision"


def test_02_person_left_maps_to_human_cleared():
    cam = _enabled_camera_automation_module()
    bridge = VisionCameraEventBridge(camera_automation=cam, camera_id="tapo_c212")
    bridge.bind_event_bus(_FakeEventBus())
    bridge.start()

    bridge._on_person_left(Event(type=CameraPersonLeft.EVENT_TYPE))

    published = cam._event_bus.published
    assert len(published) == 1
    assert published[0].data["kind"] == "human_cleared"


def test_03_camera_disconnected_maps_to_camera_offline():
    cam = _enabled_camera_automation_module()
    bridge = VisionCameraEventBridge(camera_automation=cam, camera_id="tapo_c212")
    bridge.bind_event_bus(_FakeEventBus())
    bridge.start()

    bridge._on_camera_disconnected(Event(type=CameraDisconnected.EVENT_TYPE, data={"source": "rtsp://x", "error": "timeout"}))

    published = cam._event_bus.published
    assert len(published) == 1
    assert published[0].data["kind"] == "camera_offline"


def test_04_camera_reconnected_maps_to_camera_online():
    cam = _enabled_camera_automation_module()
    bridge = VisionCameraEventBridge(camera_automation=cam, camera_id="tapo_c212")
    bridge.bind_event_bus(_FakeEventBus())
    bridge.start()

    bridge._on_camera_reconnected(Event(type=CameraReconnected.EVENT_TYPE, data={"source": "rtsp://x"}))

    published = cam._event_bus.published
    assert len(published) == 1
    assert published[0].data["kind"] == "camera_online"


# ============================================================================
# B. Unknown event - ignored safely
# ============================================================================

def test_05_unknown_event_type_never_reaches_bridge_and_is_ignored():
    """The bridge only ever subscribes to known event type strings - an
    unrelated event published on the same bus (e.g. `object_detected`)
    is never routed to it at all, by construction of the Event Bus's own
    type-keyed dispatch. Confirms the bridge does not accidentally
    subscribe to anything broader (e.g. a wildcard).

    P0.7 (Vision Context -> Automation Context) update: the bridge now
    ALSO subscribes to the existing, generic `system_error` event (to
    track Vision detector failures for `VisionContext.detection_error` -
    see `vision_bridge.py`'s own `_SYSTEM_ERROR_EVENT_TYPE` docstring),
    on top of the original four. This is an intentional, additive
    change - `system_error` is a real, already-existing Event Bus event
    type, not a wildcard, so the "does not subscribe to anything
    broader" guarantee this test checks still holds; only the expected
    set grew by one known, named entry.

    P0.8.6 update: the bridge now ALSO subscribes to the two NEW
    `HumanPresenceConfirmed`/`HumanPresenceUnconfirmed` events (see
    `luno/adapters/events.py`'s own docstrings) - again a real, already-
    named, additive Event Bus event pair, not a wildcard. The expected
    set grows by two more known, named entries."""
    bus = _FakeEventBus()
    cam = _enabled_camera_automation_module()
    bridge = VisionCameraEventBridge(camera_automation=cam, camera_id="tapo_c212")
    bridge.bind_event_bus(bus)
    bridge.start()

    bus.fire("object_detected", Event(type="object_detected", data={"label": "chair"}))
    bus.fire("pose_changed", Event(type="pose_changed", data={}))

    assert cam._event_bus.published == []
    assert set(bus._subs.keys()) == {
        CameraPersonEntered.EVENT_TYPE, CameraPersonLeft.EVENT_TYPE,
        CameraDisconnected.EVENT_TYPE, CameraReconnected.EVENT_TYPE,
        "system_error",
        HumanPresenceConfirmed.EVENT_TYPE, HumanPresenceUnconfirmed.EVENT_TYPE,
    }


# ============================================================================
# C. Confidence - always None, never fabricated
# ============================================================================

@pytest.mark.parametrize("handler_name,event_type,kind", [
    ("_on_person_entered", CameraPersonEntered.EVENT_TYPE, "human_detected"),
    ("_on_person_left", CameraPersonLeft.EVENT_TYPE, "human_cleared"),
    ("_on_camera_disconnected", CameraDisconnected.EVENT_TYPE, "camera_offline"),
    ("_on_camera_reconnected", CameraReconnected.EVENT_TYPE, "camera_online"),
])
def test_06_confidence_always_none(handler_name, event_type, kind):
    cam = _enabled_camera_automation_module()
    bridge = VisionCameraEventBridge(camera_automation=cam, camera_id="tapo_c212")
    bridge.bind_event_bus(_FakeEventBus())
    bridge.start()

    getattr(bridge, handler_name)(Event(type=event_type))

    published = cam._event_bus.published
    assert published[0].data["kind"] == kind
    assert published[0].data["confidence"] is None


# ============================================================================
# D. Camera ID
# ============================================================================

def test_07_default_camera_id_is_tapo_c212(monkeypatch):
    monkeypatch.delenv("CAMERA_AUTOMATION_VISION_CAMERA_ID", raising=False)
    bridge = VisionCameraEventBridge(camera_automation=_enabled_camera_automation_module())
    assert bridge._camera_id == "tapo_c212"


def test_08_camera_id_overridable_via_constructor():
    cam = _enabled_camera_automation_module()
    bridge = VisionCameraEventBridge(camera_automation=cam, camera_id="back_porch_cam")
    bridge.bind_event_bus(_FakeEventBus())
    bridge.start()

    bridge._on_person_entered(Event(type=CameraPersonEntered.EVENT_TYPE))

    assert cam._event_bus.published[0].data["camera_id"] == "back_porch_cam"


def test_09_camera_id_overridable_via_env(monkeypatch):
    monkeypatch.setenv("CAMERA_AUTOMATION_VISION_CAMERA_ID", "garage_cam")
    bridge = VisionCameraEventBridge(camera_automation=_enabled_camera_automation_module())
    assert bridge._camera_id == "garage_cam"


def test_10_entity_id_never_looks_like_a_fabricated_ha_entity():
    """Section 6/8 - the bridge must never invent something that LOOKS
    like a real HA entity_id (e.g. 'camera.tapo_c212' or
    'binary_sensor.tapo_c212_motion') for a Vision-sourced event."""
    cam = _enabled_camera_automation_module()
    bridge = VisionCameraEventBridge(camera_automation=cam, camera_id="tapo_c212")
    bridge.bind_event_bus(_FakeEventBus())
    bridge.start()

    bridge._on_person_entered(Event(type=CameraPersonEntered.EVENT_TYPE))

    entity_id = cam._event_bus.published[0].data["entity_id"]
    assert entity_id.startswith("vision:")
    assert not entity_id.startswith("camera.")
    assert not entity_id.startswith("binary_sensor.")


# ============================================================================
# E. Failure isolation
# ============================================================================

def test_11_ingest_exception_does_not_propagate():
    """If CameraAutomationModule.ingest_external_camera_event() itself
    raises for any reason, the bridge's own handler must swallow it -
    never let it propagate into the Event Bus's dispatch loop (which
    would risk unrelated subscribers/Vision itself)."""
    class _ExplodingCameraAutomation:
        def is_enabled(self):
            return True

        def ingest_external_camera_event(self, camera_event):
            raise RuntimeError("boom - simulated failure deep in camera automation")

    bridge = VisionCameraEventBridge(camera_automation=_ExplodingCameraAutomation(), camera_id="tapo_c212")
    bridge.bind_event_bus(_FakeEventBus())
    # start() only calls is_enabled()/subscribe - safe with the fake above.
    bridge.start()

    # Must not raise.
    bridge._on_person_entered(Event(type=CameraPersonEntered.EVENT_TYPE))
    bridge._on_camera_disconnected(Event(type=CameraDisconnected.EVENT_TYPE))


def test_12_other_subscribers_on_same_bus_are_unaffected():
    """A real Event Bus (not the fake) isolates subscribers from each
    other's exceptions already - this proves the BRIDGE ITSELF adds a
    second layer, so even a bus without that guarantee would be safe."""
    bus = _FakeEventBus()

    class _ExplodingCameraAutomation:
        def is_enabled(self):
            return True

        def ingest_external_camera_event(self, camera_event):
            raise RuntimeError("boom")

    bridge = VisionCameraEventBridge(camera_automation=_ExplodingCameraAutomation(), camera_id="tapo_c212")
    bridge.bind_event_bus(bus)
    bridge.start()

    received_by_other = []
    bus.subscribe(CameraPersonEntered.EVENT_TYPE, lambda e: received_by_other.append(e))

    bus.fire(CameraPersonEntered.EVENT_TYPE, Event(type=CameraPersonEntered.EVENT_TYPE))

    assert len(received_by_other) == 1  # the other subscriber still ran


# ============================================================================
# F. Feature flag
# ============================================================================

def test_13_disabled_camera_automation_means_bridge_never_subscribes():
    bus = _FakeEventBus()
    cam = CameraAutomationModule(config=CameraAutomationConfig(enabled=False))
    cam.bind_event_bus(_FakeEventBus())
    bridge = VisionCameraEventBridge(camera_automation=cam, camera_id="tapo_c212")
    bridge.bind_event_bus(bus)

    bridge.start()

    assert bus.sub_count == 0


def test_14_disabled_ingest_returns_false_and_publishes_nothing():
    cam = CameraAutomationModule(config=CameraAutomationConfig(enabled=False))
    fake_bus = _FakeEventBus()
    cam.bind_event_bus(fake_bus)

    camera_event = CameraEvent(camera_id="tapo_c212", kind="human_detected", entity_id="vision:x", old_state=None, new_state=None)
    result = cam.ingest_external_camera_event(camera_event)

    assert result is False
    assert fake_bus.published == []


def test_15_existing_vision_behavior_unchanged_when_disabled():
    """Even with camera_automation disabled, a Vision event fired
    directly at a bridge that WAS somehow still subscribed (defense in
    depth - should never happen per test_13) must not itself break
    anything else on the bus; and more importantly, nothing about how
    VisionAdapter itself publishes is touched by any of this (proven by
    construction - vision_bridge.py never imports luno.adapters.vision)."""
    import luno.camera_automation.vision_bridge as vb_module
    import inspect
    source = inspect.getsource(vb_module)
    # Only actual import statements matter here - the module docstring
    # legitimately DISCUSSES VisionAdapter in prose (explaining which
    # existing events it subscribes to) without ever importing it.
    import_lines = [line.strip() for line in source.splitlines() if line.strip().startswith(("import ", "from "))]
    assert not any("adapters.vision" in line and "adapters.vision_memory" not in line for line in import_lines)
    assert not any("VisionAdapter" in line for line in import_lines)
    assert not hasattr(vb_module, "VisionAdapter")


# ============================================================================
# G. No motion fabrication
# ============================================================================

def test_16_no_motion_fabrication_from_human_events():
    cam = _enabled_camera_automation_module()
    bridge = VisionCameraEventBridge(camera_automation=cam, camera_id="tapo_c212")
    bridge.bind_event_bus(_FakeEventBus())
    bridge.start()

    bridge._on_person_entered(Event(type=CameraPersonEntered.EVENT_TYPE))
    bridge._on_person_left(Event(type=CameraPersonLeft.EVENT_TYPE))
    bridge._on_camera_disconnected(Event(type=CameraDisconnected.EVENT_TYPE))
    bridge._on_camera_reconnected(Event(type=CameraReconnected.EVENT_TYPE))

    kinds = {e.data["kind"] for e in cam._event_bus.published}
    assert "motion_detected" not in kinds
    assert "motion_cleared" not in kinds
    assert kinds == {"human_detected", "human_cleared", "camera_offline", "camera_online"}


def test_17_bridge_has_no_motion_handler_at_all():
    """Static proof, not just behavioral: there is no code path in this
    module that could ever publish a motion_* kind."""
    import inspect
    source = inspect.getsource(VisionCameraEventBridge)
    assert "motion_detected" not in source
    assert "motion_cleared" not in source


# ============================================================================
# H. CameraAutomationModule additions - direct coverage
# ============================================================================

def test_18_is_enabled_reflects_config():
    assert CameraAutomationModule(config=CameraAutomationConfig(enabled=True)).is_enabled() is True
    assert CameraAutomationModule(config=CameraAutomationConfig(enabled=False)).is_enabled() is False


def test_19_ingest_external_camera_event_reuses_existing_dedupe_cooldown():
    """Section 9 proof - two identical CameraEvents in a row (same
    camera_id/kind) must be suppressed by the SAME dedupe the HA-sourced
    path already uses, not a second implementation."""
    cam = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=999.0))
    fake_bus = _FakeEventBus()
    cam.bind_event_bus(fake_bus)

    ev = CameraEvent(camera_id="tapo_c212", kind="human_detected", entity_id="vision:x", old_state=None, new_state=None)
    assert cam.ingest_external_camera_event(ev) is True
    assert cam.ingest_external_camera_event(ev) is True  # returns True (accepted), but...
    assert len(fake_bus.published) == 1  # ...only published ONCE (cooldown/dedupe suppressed the repeat)


def test_20_ingest_external_camera_event_never_raises():
    cam = CameraAutomationModule(config=CameraAutomationConfig(enabled=True))
    cam.bind_event_bus(_FakeEventBus())  # a real bus so publish() actually runs and hits the broken to_dict()

    class _BrokenEvent:
        camera_id = "tapo_c212"
        kind = "human_detected"

        def to_dict(self):
            raise RuntimeError("simulated to_dict() failure")

    result = cam.ingest_external_camera_event(_BrokenEvent())
    assert result is False


# ============================================================================
# I. Real bootstrap E2E
# ============================================================================

def _build_stack():
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    return runtime, modules, adapters, adapters["adapter_manager"]


def _teardown(runtime, adapter_manager) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()


def test_21_bridge_is_registered_by_real_bootstrap():
    runtime, modules, adapters, adapter_manager = _build_stack()
    try:
        assert "vision_camera_event_bridge" in modules
        assert isinstance(modules["vision_camera_event_bridge"], VisionCameraEventBridge)
    finally:
        _teardown(runtime, adapter_manager)


def test_22_disabled_by_default_bridge_never_subscribes_real_bootstrap():
    """§10/§12 - camera_automation is disabled by default (unchanged
    since P0), so the bridge must have zero subscriptions after a real
    runtime.start(), exactly like CameraAutomationModule itself."""
    runtime, modules, adapters, adapter_manager = _build_stack()
    try:
        assert modules["camera_automation_module"].is_enabled() is False
        runtime.start()
        bridge = modules["vision_camera_event_bridge"]
        assert bridge._sub_ids == []
    finally:
        _teardown(runtime, adapter_manager)


def test_23_enabled_e2e_vision_event_reaches_camera_automation_camera_event():
    """The core "definition of done" proof (§18): with camera_automation
    enabled, a real Vision event published on the REAL Event Bus reaches
    `camera_automation.camera_event` end to end through the bridge, with
    zero changes to VisionAdapter (never even constructed/imported by
    this test) or AutomationEngine."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    cam_module: CameraAutomationModule = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=True, cooldown_s=0.0)

    try:
        runtime.start()

        received: List[Event] = []
        runtime.event_bus.subscribe(CAMERA_EVENT_TYPE, lambda e: received.append(e))

        # Simulates VisionAdapter's own real publish call (see
        # luno/adapters/vision.py::_update_person_presence) - this test
        # never imports VisionAdapter itself, only reuses the SAME real
        # Event Bus and the SAME real event type string.
        runtime.event_bus.publish(Event(type=CameraPersonEntered.EVENT_TYPE))

        assert _wait_until(lambda: len(received) == 1)
        assert received[0].data["kind"] == "human_detected"
        assert received[0].data["camera_id"] == "tapo_c212"
    finally:
        _teardown(runtime, adapter_manager)
