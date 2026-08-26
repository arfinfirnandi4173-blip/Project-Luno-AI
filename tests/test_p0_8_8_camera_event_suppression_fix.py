"""
tests/test_p0_8_8_camera_event_suppression_fix.py
======================================================

LUNO P0.8.8 (Fix the Confirmed Camera Automation Event Suppression Bug) -
dedicated regression suite. See docs/change_impact/camera_automation_
p0_8_8.md for the full root-cause trace.

Context: after P0.8.7 fixed the WLED verification-freshness gap, the
user asked why the WLED still would not turn on despite the person
clearly being detected repeatedly. Direct inspection of the SAME real
production log used for P0.8.7 (`logs/runtime/2026-08-23.log`) proved
the raw Vision-level `camera_person_entered` event fired 14 times across
the session, yet the classified `camera_automation.camera_event (kind=
human_detected, ...)` that `AutomationEngine`'s WLED-triggering rule
actually listens to was published only TWICE total, both within the
first ~6 minutes - after 02:22:36, zero `camera_automation.camera_event`
lines and zero `automation.triggered` lines appear for the rest of the
2.5+ hour log, despite dozens more `human_entered`/`human_presence_
confirmed` detections. The rule was never even being asked to fire.

Root cause, confirmed by directly exercising the real production
`CameraAutomationModule.ingest_external_camera_event()` (no mocked
helper): `_publish_if_not_suppressed()`'s dedupe check is `if self.
_last_state.get(key) == state: return`. For the two classified-
`CameraEvent` call sites (`_handle()`'s `_entity_role_index` branch,
`ingest_external_camera_event()`), the call is `key=(camera_id, kind),
state=kind` - `state` is PART OF `key` itself, so for a FIXED key,
`state` is a compile-time constant. After the very first successful
publish for a given `(camera_id, kind)` pair, `_last_state[key]` is set
to that exact string, and EVERY subsequent call with the same key has
`state` equal to that same string - the equality check is trivially
True forever, so the `_cooldown_until` check below it (the actual,
intended, time-based anti-spam mechanism) is unreachable dead code for
every classified event. `camera_human_detected_test_action` (the real
WLED rule) could therefore only ever be reached ONCE per `camera_id`
per process lifetime for each event kind - explaining why only a
process/module restart (which happened to coincide, in the production
log, with a camera disconnect/reconnect cycle that recreates the
in-memory Vision pipeline state) ever "un-stuck" it. This is proven
directly in Section C below by exercising the exact real function, not
a re-implementation.

The legacy raw-relay path (`_handle()`'s `self._config.entities`
branch, `key=entity_id, state=new_state`) does NOT have this bug -
`new_state` is a genuinely independent, continuously-varying value, so
the equality check correctly distinguishes "nothing changed" from "a
real transition happened", and the existing pinned tests (`tests/
test_p0_camera_automation.py::test_09`/`test_10`) already lock in its
correct behavior. This sprint's fix is therefore surgical: a new
`dedupe_identical: bool = True` parameter on `_publish_if_not_suppressed()`
- default `True` preserves the legacy relay path's exact prior behavior
byte-for-byte (Section I/J below re-confirm this), and the two
classified call sites now pass `dedupe_identical=False`, making
suppression for THOSE calls purely `_cooldown_until`-based (a real,
resettable, monotonic-time deadline) instead of an accidental permanent
lock - `_last_state[key]` is still recorded for observability
(`health()`'s `n_tracked` count) but is simply never consulted as a
gate for these two call sites.

Sections:
  A. First event publishes.
  B. Identical event during cooldown is suppressed.
  C. Identical event after cooldown publishes again (THE bug fix,
     exercised directly against the real, unmodified-signature
     `ingest_external_camera_event()` - the exact reproduction shape
     the brief specifies: call1 published, wait cooldown, call2 MUST
     publish, wait another cooldown, call3 MUST publish).
  D. Third event after another cooldown publishes again.
  E. Different camera keys do not interfere.
  F. Different event/kind values do not interfere.
  G. No camera disconnect/reconnect/module recreation is needed to
     reset suppression - a single long-lived module instance handles
     repeated detections purely via cooldown elapsing.
  H. Real production call path: `VisionCameraEventBridge._on_person_
     entered()` -> `CameraAutomationModule.ingest_external_camera_
     event()` -> real Event Bus -> real `AutomationEngine` -> a real
     rule fires MULTIPLE times across multiple detections separated by
     cooldown (not a mocked helper - the actual bridge object, the
     actual module, the actual engine).
  I. Existing legacy-relay anti-spam behavior remains 100% intact
     (re-locks `test_09`/`test_10`'s own scenarios inside this file too,
     plus the "genuine transition still rate-limited within cooldown"
     case those tests establish).
  J. Existing HA-sourced classified-path (non-Vision) suppression
     behavior remains intact for the parts that were ALREADY correct
     (within-cooldown suppression of a repeat) - re-locks `tests/
     test_p0_5_camera_integration.py::test_28`'s own scenario - while
     additionally proving THIS sprint's fix (a repeat after cooldown
     expires now correctly publishes again, which `test_28` never
     exercised since it never waited out the cooldown).
  K. No duplicate events are ever produced by a single call (never 2
     events from 1 publish decision).
  L. The cooldown uses `time.monotonic()`, not wall-clock `time.time()`
     - a wall-clock jump (backward or forward) must never affect
     suppression - structural + behavioral proof.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import time
from typing import Any, Dict, List

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.events import CameraPersonEntered  # noqa: E402
from luno.automation.engine import AutomationEngine  # noqa: E402
from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.camera_automation import CAMERA_EVENT_TYPE, CameraAutomationConfig, CameraAutomationModule  # noqa: E402
from luno.camera_automation.cameras import CameraEvent, build_entity_role_index, load_camera_profiles  # noqa: E402
from luno.camera_automation.vision_bridge import VisionCameraEventBridge  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.tool_manager.builtin.home_assistant import MockHomeAssistantHandler  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
_MODULE_PATH = os.path.join(_ROOT, "luno", "camera_automation", "module.py")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class _FakeEventBus:
    """Same small local convention every prior camera_automation test
    file already established - records publishes, dispatches
    synchronously via `.fire()`."""

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
        for handler in self._subs.get(event.type, []):
            handler(event)


def _mk_event(camera_id: str = "tapo_c212", kind: str = "human_detected") -> CameraEvent:
    return CameraEvent(
        camera_id=camera_id, kind=kind, entity_id=f"vision:{kind}",
        old_state=None, new_state=None, confidence=None,
        timestamp=time.time(), source="vision",
    )


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


# ============================================================================
# A-D. Core reproduction: first publish, suppressed-during-cooldown,
#      publishes-again-after-cooldown, and a third cycle.
# ============================================================================

def test_A_first_event_publishes():
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=0.1))
    mod._event_bus = _FakeEventBus()
    assert mod.ingest_external_camera_event(_mk_event()) is True
    assert len(mod._event_bus.published) == 1
    assert mod._event_bus.published[0].data["kind"] == "human_detected"


def test_B_identical_event_during_cooldown_is_suppressed():
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=60.0))
    mod._event_bus = _FakeEventBus()
    mod.ingest_external_camera_event(_mk_event())
    mod.ingest_external_camera_event(_mk_event())  # immediately after - well within the 60s cooldown
    assert len(mod._event_bus.published) == 1


def test_C_identical_event_after_cooldown_publishes_again():
    """THE core bug-fix proof - the brief's own exact reproduction
    sequence, run against the real, unmodified-call-shape `ingest_
    external_camera_event()`:
        call #1: human_detected(camera=X) => published
        wait until cooldown expires
        call #2: human_detected(camera=X) => MUST publish
    Before this sprint's fix, call #2 was silently suppressed forever
    (proven separately, historically, via the same reproduction in this
    sprint's own investigation - the OLD code's `if self._last_state.
    get(key) == state: return` had no time bound at all)."""
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=0.1))
    mod._event_bus = _FakeEventBus()
    assert mod.ingest_external_camera_event(_mk_event()) is True
    assert len(mod._event_bus.published) == 1
    time.sleep(0.15)  # past the 0.1s cooldown
    assert mod.ingest_external_camera_event(_mk_event()) is True
    assert len(mod._event_bus.published) == 2, (
        "call #2 (identical kind, AFTER cooldown expired) MUST publish - "
        "this is the exact bug this sprint fixes"
    )


def test_D_third_event_after_another_cooldown_publishes_again():
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=0.1))
    mod._event_bus = _FakeEventBus()
    mod.ingest_external_camera_event(_mk_event())
    time.sleep(0.15)
    mod.ingest_external_camera_event(_mk_event())
    time.sleep(0.15)
    mod.ingest_external_camera_event(_mk_event())
    assert len(mod._event_bus.published) == 3, "a third, later cycle must also publish - not just a one-time unstick"


# ============================================================================
# E-F. Independence of camera/kind keys.
# ============================================================================

def test_E_different_camera_keys_do_not_interfere():
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=60.0))
    mod._event_bus = _FakeEventBus()
    mod.ingest_external_camera_event(_mk_event(camera_id="tapo_c212"))
    mod.ingest_external_camera_event(_mk_event(camera_id="back_porch_cam"))
    assert len(mod._event_bus.published) == 2, "a different camera_id must never be suppressed by another camera's cooldown"


def test_F_different_kind_values_do_not_interfere():
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=60.0))
    mod._event_bus = _FakeEventBus()
    mod.ingest_external_camera_event(_mk_event(kind="human_detected"))
    mod.ingest_external_camera_event(_mk_event(kind="human_cleared"))
    mod.ingest_external_camera_event(_mk_event(kind="human_confirmed"))
    mod.ingest_external_camera_event(_mk_event(kind="camera_online"))
    assert len(mod._event_bus.published) == 4, "distinct kinds for the same camera must never suppress one another"


# ============================================================================
# G. No disconnect/reconnect/module recreation needed - a single,
#    long-lived instance handles repeated detections via cooldown alone.
# ============================================================================

def test_G_no_reconnect_or_module_recreation_needed_to_reset_suppression():
    """Directly refutes the production symptom's own apparent
    workaround: the SAME `CameraAutomationModule` INSTANCE (never
    replaced, never `.stop()`/`.start()`-cycled, and no `camera_offline`/
    `camera_online` event of any kind is ever published in this test)
    must still un-suppress itself purely by cooldown elapsing."""
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=0.1))
    mod._event_bus = _FakeEventBus()
    for _ in range(5):
        mod.ingest_external_camera_event(_mk_event())
        time.sleep(0.15)
    assert len(mod._event_bus.published) == 5, (
        "5 detections, each separated by more than the cooldown, on the SAME module instance with "
        "NO disconnect/reconnect/restart of any kind, must all publish"
    )


# ============================================================================
# H. Real production call path: VisionCameraEventBridge -> Camera
#    AutomationModule -> real Event Bus -> real AutomationEngine, a real
#    rule firing multiple times across cooldown-separated detections.
#    This is the exact "person enters camera -> camera_person_entered ->
#    human_detected -> AutomationEngine receives event -> WLED automation
#    executes" chain from the brief's own success criteria (stages A-D),
#    proven with the real bridge object and real module - not a mocked
#    helper standing in for either.
# ============================================================================

def _build_stack(cooldown_s: float, rule_cooldown_s: float = 0.0):
    """Real bootstrap, MOCK HA backend throughout (never `register_real_
    tool_handlers()` - same hard safety constraint every prior P0.8.x
    end-to-end suite in this project already follows)."""
    import json
    import tempfile

    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    cam_module: CameraAutomationModule = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=True, cooldown_s=cooldown_s)

    rules = {
        "p0_8_8_wled_test_rule": {
            "name": "p0_8_8_wled_test_rule",
            "enabled": True,
            "trigger": {"type": "event", "parameters": {"event_name": CAMERA_EVENT_TYPE}},
            "conditions": [{"type": "equals", "target": "event.kind", "value": "human_detected"}],
            "actions": [{"type": "home_assistant.turn_on", "parameters": {"target": "light.p0_8_8_test"}}],
            "cooldown_seconds": rule_cooldown_s,
        }
    }
    engine: AutomationEngine = modules["automation_engine"]
    fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="automation_rules_p0_8_8_test_")
    os.close(fd)
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh)
    engine._rules_path = rules_path

    bridge: VisionCameraEventBridge = modules["vision_camera_event_bridge"]
    return runtime, modules, adapter_manager, bridge, rules_path


def _teardown(runtime, adapter_manager, rules_path) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()
    try:
        os.remove(rules_path)
    except OSError:
        pass


def test_H_real_bridge_to_real_automation_engine_fires_multiple_times_across_cooldown():
    """The production call path, end to end, real objects throughout:
    `bridge._on_person_entered(Event(type=CameraPersonEntered.EVENT_TYPE))`
    is EXACTLY what `VisionAdapter` publishing a real `CameraPersonEntered`
    would trigger (the Event Bus dispatches the SAME way) - not a
    stand-in helper. A person "enters" three times, each separated by
    more than both the module's own cooldown and the rule's own
    cooldown; the real `AutomationEngine` must complete the real rule
    (dispatching a real, mocked `home_assistant.turn_on`) all three
    times, never just once."""
    runtime, modules, adapter_manager, bridge, rules_path = _build_stack(cooldown_s=0.15, rule_cooldown_s=0.0)
    try:
        runtime.start()
        completed: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))

        handler = modules["tool_manager_module"].manager.registry.get("home_assistant")
        assert isinstance(handler, MockHomeAssistantHandler), "this test must never exercise a real HA call"

        def _rule_completions() -> int:
            return sum(1 for d in completed if d.get("rule_id") == "p0_8_8_wled_test_rule")

        # Detection #1
        bridge._on_person_entered(Event(type=CameraPersonEntered.EVENT_TYPE))
        assert _wait_until(lambda: _rule_completions() >= 1, timeout_s=5.0), "stage C failed: AutomationEngine never received/completed the first event"

        time.sleep(0.25)  # past both the module cooldown (0.15s) and the rule cooldown (0.0s, but AutomationEngine's own dedupe still needs the module to republish)

        # Detection #2 - the exact scenario that was broken in production.
        bridge._on_person_entered(Event(type=CameraPersonEntered.EVENT_TYPE))
        assert _wait_until(lambda: _rule_completions() >= 2, timeout_s=5.0), (
            "stage C failed on the SECOND detection: AutomationEngine did not receive a NEW event after "
            "cooldown - this is exactly the production bug (WLED not turning on for a second/later "
            "detection despite the person clearly being detected again)"
        )

        time.sleep(0.25)

        # Detection #3 - proves it is not a one-time unstick.
        bridge._on_person_entered(Event(type=CameraPersonEntered.EVENT_TYPE))
        assert _wait_until(lambda: _rule_completions() >= 3, timeout_s=5.0), "stage C failed on the THIRD detection"

        ha_calls = [c for c in tool_calls if c.get("tool") == "home_assistant" and c.get("target") == "light.p0_8_8_test"]
        assert len(ha_calls) >= 3, (
            f"stage D failed: expected at least 3 real (mocked) home_assistant.turn_on dispatches, got {len(ha_calls)}"
        )
        assert all(c.get("action") == "turn_on" for c in ha_calls)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# I. Existing legacy-relay anti-spam behavior remains 100% intact.
# ============================================================================

def test_I1_legacy_relay_identical_repeat_still_suppressed_even_with_zero_cooldown():
    """Re-locks `tests/test_p0_camera_automation.py::test_09`'s own
    scenario inside this sprint's own suite - the legacy relay path
    (`dedupe_identical=True`, the default, UNCHANGED) must still treat a
    truly-identical repeated `new_state` as a genuine no-op, even with
    `cooldown_s=0.0`."""
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door"], cooldown_s=0.0))
    mod.bind_event_bus(bus)
    mod.start()
    ev = Event(type="device_state_changed", data={"entity_id": "binary_sensor.front_door", "old_state": "off", "new_state": "on"})
    mod._on_device_state_changed(ev)
    mod._on_device_state_changed(ev)
    assert len(bus.published) == 1


def test_I2_legacy_relay_genuine_transition_still_suppressed_within_cooldown():
    """Re-locks `test_10_cooldown_suppresses_rapid_changes`'s own
    scenario - a GENUINE state transition (on -> off) arriving before a
    long cooldown has elapsed is STILL suppressed for the legacy relay
    path (this is intentional rate-limiting, not a bug, and this
    sprint's fix must never change it - `dedupe_identical` defaults to
    `True` for exactly this call site, unmodified)."""
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door"], cooldown_s=60.0))
    mod.bind_event_bus(bus)
    mod.start()
    mod._on_device_state_changed(Event(type="device_state_changed", data={"entity_id": "binary_sensor.front_door", "old_state": "off", "new_state": "on"}))
    mod._on_device_state_changed(Event(type="device_state_changed", data={"entity_id": "binary_sensor.front_door", "old_state": "on", "new_state": "off"}))
    assert len(bus.published) == 1


def test_I3_legacy_relay_identical_state_stays_suppressed_forever_by_design():
    """The legacy relay path's OWN, DIFFERENT, intentional contract
    (never touched by this sprint): a truly IDENTICAL repeated `new_
    state` (e.g. "on" fired twice with nothing in between) is treated as
    a genuine no-op and stays suppressed regardless of how much time
    passes - because for a continuously-tracked HA entity state, "the
    same value again" genuinely means "nothing changed". This is
    DIFFERENT from the classified-path bug this sprint fixes (Section C)
    - there, `state` is not an independent value at all (it IS part of
    the key), so every occurrence is its own meaningful event, not a
    continuously-tracked value. `dedupe_identical=True` (the default,
    unmodified for this call site) preserves this exactly."""
    bus = _FakeEventBus()
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, entities=["binary_sensor.front_door"], cooldown_s=0.1))
    mod.bind_event_bus(bus)
    mod.start()
    ev = Event(type="device_state_changed", data={"entity_id": "binary_sensor.front_door", "old_state": "off", "new_state": "on"})
    mod._on_device_state_changed(ev)
    time.sleep(0.15)
    mod._on_device_state_changed(ev)
    assert len(bus.published) == 1, "an identical repeated new_state stays suppressed by design - only a genuine transition (a different new_state) publishes again"


# ============================================================================
# J. Existing HA-sourced classified-path (non-Vision) suppression -
#    within-cooldown behavior re-locked, PLUS this sprint's own fix
#    proven for the SAME call site (never exercised by the pre-existing
#    test_28, which only checked the within-cooldown case).
# ============================================================================

def _write_cameras_json(path: str, cameras: Dict[str, Any]) -> None:
    import json
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"cameras": cameras}, fh)


def test_J1_ha_sourced_classified_repeat_still_suppressed_within_cooldown():
    """Re-locks `tests/test_p0_5_camera_integration.py::test_28`'s own
    scenario exactly (same fixture shape, same assertion) - proves this
    sprint's `dedupe_identical=False` change did NOT remove the
    within-cooldown suppression for the HA-sourced classified branch
    (`_handle()`'s `_entity_role_index` branch), only the AFTER-cooldown
    permanent-lock bug."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".json", prefix="camera_automation_p0_8_8_test_")
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
        assert len(bus.published) == 1
    finally:
        os.remove(path)


def test_J2_ha_sourced_classified_repeat_after_cooldown_now_publishes_again():
    """THE fix, proven for the OTHER classified call site (`_handle()`'s
    branch, sourced from a real HA `device_state_changed` event, not
    Vision) - `test_28` never waited out the cooldown, so it never
    caught this. Same bug, same fix, same call-site pattern
    (`key=(camera_id, kind), state=kind`)."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".json", prefix="camera_automation_p0_8_8_test_")
    os.close(fd)
    try:
        _write_cameras_json(path, {"tapo_c212": {"motion_entity": "binary_sensor.tapo_c212_motion"}})
        bus = _FakeEventBus()
        cfg = CameraAutomationConfig(enabled=True, cameras_path=path, cooldown_s=0.1)
        mod = CameraAutomationModule(config=cfg)
        mod.bind_event_bus(bus)
        mod.start()
        ev = Event(type="device_state_changed", data={"entity_id": "binary_sensor.tapo_c212_motion", "old_state": "off", "new_state": "on"})
        mod._on_device_state_changed(ev)
        assert len(bus.published) == 1
        time.sleep(0.15)
        mod._on_device_state_changed(ev)
        assert len(bus.published) == 2, "an identical HA-sourced classified transition after cooldown must publish again"
    finally:
        os.remove(path)


# ============================================================================
# K. No duplicate events are ever produced by a single publish decision.
# ============================================================================

def test_K_single_publish_decision_never_produces_more_than_one_event():
    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=0.1))
    mod._event_bus = _FakeEventBus()
    for _ in range(3):
        before = len(mod._event_bus.published)
        mod.ingest_external_camera_event(_mk_event())
        after = len(mod._event_bus.published)
        assert after - before <= 1, "a single ingest_external_camera_event() call must never publish more than one event"
        time.sleep(0.15)


# ============================================================================
# L. time.monotonic() semantics - never wall-clock-based.
# ============================================================================

def test_L1_source_uses_time_monotonic_not_time_time_for_cooldown():
    """Structural proof: `_publish_if_not_suppressed()`'s own cooldown
    arithmetic must reference `time.monotonic()`, never `time.time()` -
    a wall-clock adjustment (NTP sync, DST, manual clock change) must
    never affect suppression. Parses the actual function's AST rather
    than a naive substring search, since `time.time()` appears
    elsewhere in this module (event publish timestamps) and must not
    cause a false failure."""
    source = _read(_MODULE_PATH)
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_publish_if_not_suppressed":
            target = node
            break
    assert target is not None, "_publish_if_not_suppressed() not found in module.py"
    calls = [
        f"{ast.dump(n.func)}"
        for n in ast.walk(target)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("monotonic", "time")
    ]
    monotonic_calls = [c for c in calls if "monotonic" in c]
    time_time_calls = [c for c in calls if "attr='time'" in c]
    assert len(monotonic_calls) >= 1, "_publish_if_not_suppressed() must call time.monotonic()"
    assert len(time_time_calls) == 0, "_publish_if_not_suppressed() must never call time.time() (wall-clock) for cooldown arithmetic"


def test_L2_wall_clock_jump_does_not_affect_suppression(monkeypatch):
    """Behavioral proof: even if `time.time()` is monkeypatched to jump
    wildly (simulating an NTP correction or manual clock change),
    `_publish_if_not_suppressed()`'s cooldown decision is unaffected,
    because it never reads `time.time()` at all - only `luno.camera_
    automation.module`'s own imported `time.monotonic()`, which
    monkeypatching `time.time` does not touch."""
    import luno.camera_automation.module as module_mod

    mod = CameraAutomationModule(config=CameraAutomationConfig(enabled=True, cooldown_s=60.0))
    mod._event_bus = _FakeEventBus()
    mod.ingest_external_camera_event(_mk_event())
    assert len(mod._event_bus.published) == 1

    # Simulate a huge wall-clock jump forward (e.g. NTP sync after being
    # offline) - if the module used time.time() for cooldown, this would
    # incorrectly make the cooldown appear expired.
    real_time_time = module_mod.time.time
    monkeypatch.setattr(module_mod.time, "time", lambda: real_time_time() + 10_000)
    try:
        mod.ingest_external_camera_event(_mk_event())
        assert len(mod._event_bus.published) == 1, (
            "a wall-clock jump must not affect cooldown - suppression is still correctly active "
            "because time.monotonic() (unaffected by the monkeypatch) is what actually gates it"
        )
    finally:
        monkeypatch.setattr(module_mod.time, "time", real_time_time)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
