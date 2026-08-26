"""
tests/test_p0_9_room_occupancy.py
====================================

LUNO P0.9 (Room Occupancy State + Presence Duration) - dedicated
regression suite. See docs/change_impact/room_occupancy_p0_9.md for the
full design writeup; `luno/vision_occupancy.py`'s own module docstring
for the architecture rationale.

`RoomOccupancyModule` is a thin, additive, OBSERVATIONAL Event Bus
subscriber - it never controls Home Assistant/WLED/ToolManager, and
never runs a second detection/inference pipeline. It derives `vacant`/
`occupied` purely from the EXISTING `HumanPresenceConfirmed`/
`CameraPersonLeft` events `VisionAdapter` already publishes (P0.8.5/
P0.8.6, unmodified), and keeps `person_count` fresh from the EXISTING
`VisionFrameProcessed` event's own `human_count` field (Sprint 8,
unmodified) - never a second YOLO call.

Sections:
  A. Initial state (fresh instance = vacant, person_count=0).
  B. First confirmed human -> occupied, person_count=1, occupied_since
     created, room_occupied published exactly once.
  C. Repeated detections while already occupied -> only one room_occupied
     transition ever, occupied_since unchanged.
  D. Presence duration increases correctly while occupied (real
     time.monotonic() elapsing, not a naive datetime subtraction).
  E. Person leaves -> vacant, vacant_since created, person_count=0, final
     presence duration preserved (frozen, does not keep increasing).
  F. Re-entry -> a NEW occupied_since, duration resets to (near) zero -
     never merges with the prior visit.
  G. Multi-person transitions: 0->1, 1->2 (no new room_occupied), 2->1
     (no room_vacant), 1->0, 0->2 (a genuine new occupied period).
  H. Person-count changes never reset occupied_since while occupied.
  I. Repeated human_cleared while already vacant -> no duplicate
     room_vacant events.
  J. Clock correctness - duration uses time.monotonic(), structurally
     (AST) and behaviorally (a wall-clock/utcnow jump does not affect
     duration).
  K. Restart behavior - a fresh instance never fabricates an
     occupied_since; starts vacant until a genuine new confirmation.
  L. Snapshot consistency - get_snapshot() is stable/coherent across
     repeated reads and matches internal transition bookkeeping.
  M. Co-existence with existing WLED automation - a real, full-stack
     bootstrap (register_all_modules) with camera_automation ENABLED
     proves RoomOccupancyModule and the existing WLED ON rule both
     correctly react to the SAME real human_confirmed event, neither
     interfering with the other (existing WLED behavior itself is
     re-verified unmodified by the full P0.8.x suites in this sprint's
     own regression sweep - see the change-impact doc's Section 9 - not
     duplicated wholesale here).
  N. Existing P0.8.x suites remaining green is verified by this sprint's
     regression sweep (change-impact doc Section 9), not duplicated here.
"""

from __future__ import annotations

import ast
import os
import sys
import time
from typing import Any, Dict, List

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.adapters.events import CameraPersonLeft, HumanPresenceConfirmed, VisionFrameProcessed  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.vision_occupancy import (  # noqa: E402
    OCCUPANCY_CHANGED_EVENT_TYPE,
    ROOM_OCCUPIED_EVENT_TYPE,
    ROOM_VACANT_EVENT_TYPE,
    STATE_OCCUPIED,
    STATE_VACANT,
    RoomOccupancyModule,
)

_MODULE_PATH = os.path.join(_ROOT, "luno", "vision_occupancy.py")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class _FakeEventBus:
    """Same minimal convention every camera_automation/automation test
    file in this project already uses - subscribe/publish, synchronous
    dispatch, records every published event for assertions."""

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

    def count(self, event_type: str) -> int:
        return sum(1 for e in self.published if e.type == event_type)

    def data_for(self, event_type: str) -> List[Dict[str, Any]]:
        return [e.data for e in self.published if e.type == event_type]


def _mod() -> "tuple[RoomOccupancyModule, _FakeEventBus]":
    bus = _FakeEventBus()
    mod = RoomOccupancyModule()
    mod.bind_event_bus(bus)
    mod.start()
    return mod, bus


def _confirm(bus: _FakeEventBus) -> None:
    bus.publish(Event(type=HumanPresenceConfirmed.EVENT_TYPE))


def _clear(bus: _FakeEventBus) -> None:
    bus.publish(Event(type=CameraPersonLeft.EVENT_TYPE))


def _frame(bus: _FakeEventBus, human_count: int) -> None:
    bus.publish(Event(type=VisionFrameProcessed.EVENT_TYPE, data={
        "fps": 15.0, "latency_ms": 10.0, "object_count": human_count, "human_count": human_count, "backend": "mock",
    }))


# ============================================================================
# A. Initial state.
# ============================================================================

def test_A1_fresh_instance_is_vacant():
    mod = RoomOccupancyModule()
    snap = mod.get_snapshot()
    assert snap.state == STATE_VACANT
    assert snap.person_count == 0


def test_A2_fresh_instance_has_no_timestamps():
    mod = RoomOccupancyModule()
    snap = mod.get_snapshot()
    assert snap.occupied_since is None
    assert snap.vacant_since is None
    assert snap.last_seen is None
    assert snap.presence_duration_seconds == 0.0


# ============================================================================
# B. First confirmed human.
# ============================================================================

def test_B1_human_confirmed_transitions_to_occupied():
    mod, bus = _mod()
    _confirm(bus)
    snap = mod.get_snapshot()
    assert snap.state == STATE_OCCUPIED
    assert snap.person_count == 1
    assert snap.occupied_since is not None


def test_B2_room_occupied_event_published_exactly_once():
    mod, bus = _mod()
    _confirm(bus)
    assert bus.count(ROOM_OCCUPIED_EVENT_TYPE) == 1
    assert bus.count(OCCUPANCY_CHANGED_EVENT_TYPE) == 1


def test_B3_room_occupied_payload_matches_snapshot():
    mod, bus = _mod()
    _confirm(bus)
    snap = mod.get_snapshot()
    payload = bus.data_for(ROOM_OCCUPIED_EVENT_TYPE)[0]
    assert payload["state"] == "occupied"
    assert payload["person_count"] == 1
    assert payload["occupied_since"] == snap.occupied_since


# ============================================================================
# C. Repeated detections.
# ============================================================================

def test_C1_repeated_human_confirmed_fires_room_occupied_only_once():
    mod, bus = _mod()
    for _ in range(5):
        _confirm(bus)
        _frame(bus, 1)
    assert bus.count(ROOM_OCCUPIED_EVENT_TYPE) == 1


def test_C2_occupied_since_unchanged_across_repeated_confirmations():
    mod, bus = _mod()
    _confirm(bus)
    first = mod.get_snapshot().occupied_since
    for _ in range(10):
        _confirm(bus)
    assert mod.get_snapshot().occupied_since == first


# ============================================================================
# D. Presence duration.
# ============================================================================

def test_D1_duration_increases_while_occupied(monkeypatch):
    import luno.vision_occupancy as occ_mod
    t = [1000.0]
    monkeypatch.setattr(occ_mod.time, "monotonic", lambda: t[0])

    mod, bus = _mod()
    _confirm(bus)
    assert mod.get_snapshot().presence_duration_seconds == pytest.approx(0.0, abs=0.01)

    t[0] += 42.5
    snap = mod.get_snapshot()
    assert snap.presence_duration_seconds == pytest.approx(42.5, abs=0.01)

    t[0] += 100.0
    snap2 = mod.get_snapshot()
    assert snap2.presence_duration_seconds == pytest.approx(142.5, abs=0.01)


# ============================================================================
# E. Person leaves.
# ============================================================================

def test_E1_human_cleared_transitions_to_vacant():
    mod, bus = _mod()
    _confirm(bus)
    _clear(bus)
    snap = mod.get_snapshot()
    assert snap.state == STATE_VACANT
    assert snap.person_count == 0
    assert snap.vacant_since is not None
    assert snap.occupied_since is None


def test_E2_final_presence_duration_preserved(monkeypatch):
    import luno.vision_occupancy as occ_mod
    t = [2000.0]
    monkeypatch.setattr(occ_mod.time, "monotonic", lambda: t[0])

    mod, bus = _mod()
    _confirm(bus)
    t[0] += 184.2
    _clear(bus)
    snap = mod.get_snapshot()
    assert snap.presence_duration_seconds == pytest.approx(184.2, abs=0.01)


def test_E3_duration_does_not_keep_increasing_after_vacant(monkeypatch):
    import luno.vision_occupancy as occ_mod
    t = [3000.0]
    monkeypatch.setattr(occ_mod.time, "monotonic", lambda: t[0])

    mod, bus = _mod()
    _confirm(bus)
    t[0] += 50.0
    _clear(bus)
    frozen = mod.get_snapshot().presence_duration_seconds

    t[0] += 500.0  # a lot of real time passes while vacant
    assert mod.get_snapshot().presence_duration_seconds == pytest.approx(frozen, abs=0.01)


def test_E4_room_vacant_event_published_once():
    mod, bus = _mod()
    _confirm(bus)
    _clear(bus)
    assert bus.count(ROOM_VACANT_EVENT_TYPE) == 1


# ============================================================================
# F. Re-entry semantics.
# ============================================================================

def test_F1_re_entry_creates_new_occupied_since(monkeypatch):
    import luno.vision_occupancy as occ_mod
    t = [5000.0]
    monkeypatch.setattr(occ_mod.time, "monotonic", lambda: t[0])

    mod, bus = _mod()
    _confirm(bus)
    first_since = mod.get_snapshot().occupied_since
    t[0] += 30.0
    _clear(bus)
    t[0] += 20.0
    _confirm(bus)
    second_since = mod.get_snapshot().occupied_since

    assert second_since is not None
    assert second_since != first_since


def test_F2_re_entry_resets_duration_to_zero(monkeypatch):
    import luno.vision_occupancy as occ_mod
    t = [6000.0]
    monkeypatch.setattr(occ_mod.time, "monotonic", lambda: t[0])

    mod, bus = _mod()
    _confirm(bus)
    t[0] += 900.0  # a long first visit
    _clear(bus)
    t[0] += 10.0
    _confirm(bus)  # re-entry

    assert mod.get_snapshot().presence_duration_seconds == pytest.approx(0.0, abs=0.01)


def test_F3_re_entry_fires_a_second_room_occupied_event():
    mod, bus = _mod()
    _confirm(bus)
    _clear(bus)
    _confirm(bus)
    assert bus.count(ROOM_OCCUPIED_EVENT_TYPE) == 2


# ============================================================================
# G. Multi-person transitions.
# ============================================================================

def test_G_full_multiperson_sequence():
    mod, bus = _mod()

    # 0 -> 1
    _confirm(bus)
    _frame(bus, 1)
    assert mod.get_snapshot().state == STATE_OCCUPIED
    assert mod.get_snapshot().person_count == 1
    assert bus.count(ROOM_OCCUPIED_EVENT_TYPE) == 1

    # 1 -> 2 (occupied -> occupied, no new room_occupied)
    _frame(bus, 2)
    assert mod.get_snapshot().state == STATE_OCCUPIED
    assert mod.get_snapshot().person_count == 2
    assert bus.count(ROOM_OCCUPIED_EVENT_TYPE) == 1

    # 2 -> 1 (occupied -> occupied, no room_vacant)
    _frame(bus, 1)
    assert mod.get_snapshot().state == STATE_OCCUPIED
    assert mod.get_snapshot().person_count == 1
    assert bus.count(ROOM_VACANT_EVENT_TYPE) == 0

    # 1 -> 0
    _clear(bus)
    assert mod.get_snapshot().state == STATE_VACANT
    assert mod.get_snapshot().person_count == 0
    assert bus.count(ROOM_VACANT_EVENT_TYPE) == 1

    # 0 -> 2 (a fresh occupied period, straight to 2 people)
    _confirm(bus)
    _frame(bus, 2)
    assert mod.get_snapshot().state == STATE_OCCUPIED
    assert mod.get_snapshot().person_count == 2
    assert bus.count(ROOM_OCCUPIED_EVENT_TYPE) == 2

    # 2 -> 0
    _clear(bus)
    assert mod.get_snapshot().state == STATE_VACANT
    assert mod.get_snapshot().person_count == 0
    assert bus.count(ROOM_VACANT_EVENT_TYPE) == 2


# ============================================================================
# H. Occupied timestamp stability under person-count changes.
# ============================================================================

def test_H_person_count_changes_never_reset_occupied_since():
    mod, bus = _mod()
    _confirm(bus)
    _frame(bus, 1)
    since = mod.get_snapshot().occupied_since

    _frame(bus, 2)
    assert mod.get_snapshot().occupied_since == since

    _frame(bus, 1)
    assert mod.get_snapshot().occupied_since == since

    _frame(bus, 3)
    assert mod.get_snapshot().occupied_since == since


# ============================================================================
# I. Repeated vacant events.
# ============================================================================

def test_I1_repeated_human_cleared_while_already_vacant_no_duplicate_event():
    mod, bus = _mod()
    for _ in range(5):
        _clear(bus)  # never was occupied - every one of these is a no-op
    assert bus.count(ROOM_VACANT_EVENT_TYPE) == 0
    assert mod.get_snapshot().state == STATE_VACANT


def test_I2_repeated_human_cleared_after_a_genuine_transition_no_duplicate():
    mod, bus = _mod()
    _confirm(bus)
    _clear(bus)
    for _ in range(5):
        _clear(bus)
    assert bus.count(ROOM_VACANT_EVENT_TYPE) == 1


# ============================================================================
# J. Clock correctness.
# ============================================================================

def test_J1_source_uses_time_monotonic_for_duration():
    """Structural proof (AST) - `time.monotonic()` must be called
    somewhere in this module; duration must never come from a naive
    `datetime.now() - datetime.now()` subtraction."""
    source = _read(_MODULE_PATH)
    tree = ast.parse(source)
    monotonic_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "monotonic"
    ]
    assert len(monotonic_calls) >= 1, "vision_occupancy.py must call time.monotonic() for duration arithmetic"
    # No `datetime.now() - datetime.now()`/`utcnow() - utcnow()`-style
    # naive subtraction anywhere in the file (a BinOp with Sub whose
    # operands are both Call nodes would be the shape of that mistake).
    bad_subtractions = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)
        and isinstance(n.left, ast.Call) and isinstance(n.right, ast.Call)
    ]
    assert bad_subtractions == [], "duration must never be computed via naive datetime subtraction"


def test_J2_wall_clock_jump_does_not_affect_duration(monkeypatch):
    """Behavioral proof - jumping `utcnow()` (the wall-clock helper this
    module uses ONLY for human-readable timestamps) must not affect
    `presence_duration_seconds`, because that field is computed
    exclusively from `time.monotonic()`, which this monkeypatch does not
    touch."""
    import luno.vision_occupancy as occ_mod
    from datetime import datetime, timedelta, timezone

    mod, bus = _mod()
    _confirm(bus)
    before = mod.get_snapshot().presence_duration_seconds

    real_utcnow = occ_mod.utcnow
    monkeypatch.setattr(occ_mod, "utcnow", lambda: real_utcnow() + timedelta(days=365))
    try:
        after = mod.get_snapshot().presence_duration_seconds
        assert after == pytest.approx(before, abs=0.5), (
            "a wall-clock jump must not affect presence_duration_seconds - only time.monotonic() gates it"
        )
    finally:
        monkeypatch.setattr(occ_mod, "utcnow", real_utcnow)


# ============================================================================
# K. Restart behavior.
# ============================================================================

def test_K1_fresh_instance_after_simulated_restart_is_vacant_no_fake_timestamp():
    """A `RoomOccupancyModule()` has zero persistence of its own (see its
    own module docstring's "Restart behavior" section) - a brand new
    instance IS what a process restart looks like for this module.
    Never a fabricated `occupied_since`."""
    mod = RoomOccupancyModule()
    snap = mod.get_snapshot()
    assert snap.state == STATE_VACANT
    assert snap.occupied_since is None
    assert snap.presence_duration_seconds == 0.0


def test_K2_module_has_no_persistence_imports():
    """Static guard - this module must never import a file/db persistence
    primitive (Section 9: "Do not introduce persistent storage unless
    there is a clear architectural reason" - there isn't one here)."""
    source = _read(_MODULE_PATH)
    for forbidden in ("sqlite3", "json.load", "json.dump", "open(", "atomic_write_json", "pickle"):
        assert forbidden not in source, f"vision_occupancy.py must not use {forbidden!r} - no persistence for this sprint"


# ============================================================================
# L. Snapshot consistency.
# ============================================================================

def test_L1_snapshot_is_immutable_dataclass():
    mod = RoomOccupancyModule()
    snap = mod.get_snapshot()
    with pytest.raises(Exception):
        snap.state = "occupied"  # type: ignore[misc]


def test_L2_repeated_snapshot_reads_are_stable_when_nothing_changed():
    mod, bus = _mod()
    _confirm(bus)
    s1 = mod.get_snapshot()
    s2 = mod.get_snapshot()
    assert s1.state == s2.state
    assert s1.person_count == s2.person_count
    assert s1.occupied_since == s2.occupied_since
    assert s2.presence_duration_seconds >= s1.presence_duration_seconds


def test_L3_to_dict_matches_dataclass_fields():
    mod, bus = _mod()
    _confirm(bus)
    snap = mod.get_snapshot()
    d = snap.to_dict()
    assert d["state"] == snap.state
    assert d["person_count"] == snap.person_count
    assert d["occupied_since"] == snap.occupied_since
    assert d["vacant_since"] == snap.vacant_since
    assert d["last_seen"] == snap.last_seen
    assert d["presence_duration_seconds"] == snap.presence_duration_seconds


# ============================================================================
# M. Co-existence with the existing, real WLED automation - real
#    bootstrap, real bridge, MOCK HA backend only.
# ============================================================================

def test_M_occupancy_and_real_wled_automation_both_react_to_same_confirmation():
    import tempfile
    from luno.automation.engine import AutomationEngine
    from luno.bootstrap.adapters import register_all_adapters
    from luno.bootstrap.launcher_config import LauncherConfig
    from luno.bootstrap.modules import register_all_modules
    from luno.bootstrap.shutdown import ShutdownCoordinator
    from luno.camera_automation import CAMERA_EVENT_TYPE, CameraAutomationConfig, CameraAutomationModule
    from luno.core.config import CoreConfig
    from luno.core.runtime import Runtime
    from luno.tool_manager.builtin.home_assistant import MockHomeAssistantHandler

    fast_cfg = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
    cfg = LauncherConfig()
    runtime = Runtime(fast_cfg)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    cam_module: CameraAutomationModule = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=True, cooldown_s=0.0)
    occupancy_module = modules["room_occupancy_module"]
    assert isinstance(occupancy_module, RoomOccupancyModule)

    rules = {
        "p0_9_wled_on_test_rule": {
            "name": "p0_9_wled_on_test_rule", "enabled": True,
            "trigger": {"type": "event", "parameters": {"event_name": CAMERA_EVENT_TYPE}},
            "conditions": [{"type": "equals", "target": "event.kind", "value": "human_confirmed"}],
            "actions": [{"type": "home_assistant.turn_on", "parameters": {"target": "light.p0_9_test"}}],
            "cooldown_seconds": 0.0,
        },
    }
    engine: AutomationEngine = modules["automation_engine"]
    fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="automation_rules_p0_9_test_")
    os.close(fd)
    import json
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh)
    engine._rules_path = rules_path

    try:
        runtime.start()
        handler = modules["tool_manager_module"].manager.registry.get("home_assistant")
        assert isinstance(handler, MockHomeAssistantHandler), "this test must never exercise a real HA call"

        tool_calls: List[Dict[str, Any]] = []
        runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))

        from luno.adapters.events import HumanPresenceConfirmed as _HPC
        # The REAL VisionAdapter event, published exactly as VisionAdapter
        # itself would from a real detection cycle.
        runtime.event_bus.publish(Event(type=_HPC.EVENT_TYPE))

        def _wled_dispatched() -> bool:
            return any(c.get("tool") == "home_assistant" and c.get("target") == "light.p0_9_test" and c.get("action") == "turn_on" for c in tool_calls)

        deadline = time.time() + 5.0
        while time.time() < deadline and not _wled_dispatched():
            time.sleep(0.02)

        assert _wled_dispatched(), "existing WLED-style automation must still fire on human_confirmed, unaffected by RoomOccupancyModule's presence"
        occ_snapshot = occupancy_module.get_snapshot()
        assert occ_snapshot.state == STATE_OCCUPIED, "RoomOccupancyModule must independently observe the SAME real event"
        assert occ_snapshot.person_count >= 1
    finally:
        ShutdownCoordinator(runtime, adapter_manager).shutdown()
        try:
            os.remove(rules_path)
        except OSError:
            pass


# ============================================================================
# Architecture guard (brief Section 13) - static, source-level proof that
# RoomOccupancyModule stays a pure, observational state layer.
# ============================================================================

def _non_comment_non_docstring_code(path: str) -> str:
    """Strips module/class/function docstrings and `#`-comments before
    scanning, so a prose mention inside a docstring (e.g. this file's own
    extensive rationale about WHY it does not import Home Assistant)
    never produces a false positive - same helper convention used by
    `tests/test_p0_8_7_wled_verification_fix.py`'s own `_non_comment_
    non_docstring_code()` for the identical reason."""
    source = _read(path)
    tree = ast.parse(source)
    docstring_lines: set = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
                doc_node = body[0]
                for ln in range(doc_node.lineno, (doc_node.end_lineno or doc_node.lineno) + 1):
                    docstring_lines.add(ln)
    kept_lines = []
    for i, line in enumerate(source.splitlines(), start=1):
        if i in docstring_lines:
            continue
        code_part = line.split("#", 1)[0]
        kept_lines.append(code_part)
    return "\n".join(kept_lines)


def test_N1_does_not_import_home_assistant():
    code = _non_comment_non_docstring_code(_MODULE_PATH)
    for forbidden in ("home_assistant", "HomeAssistant", "HA_TOKEN"):
        assert forbidden not in code, f"vision_occupancy.py must not reference {forbidden!r} in real code"


def test_N2_does_not_call_tool_manager_or_dispatch_tool_requests():
    code = _non_comment_non_docstring_code(_MODULE_PATH)
    for forbidden in ("ToolManager", "tool_manager", "tool_requested", "ToolRegistry"):
        assert forbidden not in code, f"vision_occupancy.py must not reference {forbidden!r} - it never dispatches tool calls"


def test_N3_does_not_control_wled_or_any_light_entity():
    code = _non_comment_non_docstring_code(_MODULE_PATH)
    for forbidden in ("light.wled", "turn_on", "turn_off", "wled", "WLED"):
        assert forbidden not in code, f"vision_occupancy.py must not reference {forbidden!r} - Occupancy never controls a device"


def test_N4_does_not_perform_a_second_yolo_inference():
    code = _non_comment_non_docstring_code(_MODULE_PATH)
    for forbidden in ("ultralytics", "YOLO(", "torch", "cv2", "VideoCapture", "rtsp", "RTSP", "detect_objects"):
        assert forbidden not in code, f"vision_occupancy.py must not reference {forbidden!r} - it consumes existing tracked data, never runs its own inference"


def test_N5_only_subscribes_to_the_three_documented_event_types():
    """Confirms the module subscribes to exactly the three upstream event
    types its own docstring documents - never a fourth, undocumented
    coupling to some other Vision/HA/automation internal."""
    source = _read(_MODULE_PATH)
    tree = ast.parse(source)
    subscribe_args = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "subscribe":
            if node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Name):
                    subscribe_args.append(arg.id)
    assert set(subscribe_args) == {
        "_HUMAN_CONFIRMED_EVENT_TYPE", "_HUMAN_CLEARED_EVENT_TYPE", "_VISION_FRAME_PROCESSED_EVENT_TYPE",
    }


def test_N6_consumes_existing_confirmed_presence_data_not_raw_yolo():
    """The module must key its STATE transitions off `HumanPresenceConfirmed`/
    `CameraPersonLeft` (the existing, already-hardened confirmation/
    debounce pipeline) - never off a raw per-frame detection event
    (`PersonAppeared`/`ObjectDetected`/etc., which have no confidence
    floor or temporal confirmation of their own)."""
    code = _non_comment_non_docstring_code(_MODULE_PATH)
    assert "HumanPresenceConfirmed" in code
    assert "CameraPersonLeft" in code
    for forbidden in ("PersonAppeared", "ObjectDetected", "on_detections", "person_confidences"):
        assert forbidden not in code, f"vision_occupancy.py must not derive state from {forbidden!r} - that is a second detection algorithm"


def test_N7_single_canonical_occupancy_owner():
    """Guards against a future accidental SECOND occupancy-state owner -
    `occupied_since`/`vacant_since`/`presence_duration_seconds` (this
    module's own distinctive field names) must appear as real, non-test,
    non-doc source in exactly one production file."""
    owners = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, "luno")):
        dirnames[:] = [d for d in dirnames if d not in ("tests", "__pycache__")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            text = _read(full)
            if "occupied_since" in text and "vacant_since" in text and "presence_duration_seconds" in text:
                owners.append(os.path.relpath(full, _ROOT))
    assert owners == ["luno/vision_occupancy.py"], f"expected exactly one canonical occupancy owner, found: {owners}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
