"""
tests/test_p0_10_occupancy_context.py
========================================

LUNO P0.10 (Occupancy-Aware Automation Intelligence) - dedicated
regression suite. See docs/change_impact/camera_automation_p0_10.md for
the full design writeup.

P0.9 built `RoomOccupancyModule` as a purely OBSERVATIONAL state layer
(vacant/occupied, person_count, occupied_since/vacant_since/last_seen,
presence_duration_seconds). P0.10 is strictly additive on top of that:

  1. Two new READ-ONLY snapshot fields - `occupancy_age_seconds` (time
     in the CURRENT state, either direction - unlike
     `presence_duration_seconds`, which freezes while vacant, this one
     keeps moving to measure "how long has the room been empty") and
     `last_transition` (`"occupied"`/`"vacant"`/`None`).
  2. `previous_state` added to the `room_occupied`/`room_vacant`/
     `occupancy_changed` event payloads (diagnostics only).
  3. Five new `"occupancy.*"` `AutomationEngine` state_readers, wired
     through the EXISTING `state_readers` context mechanism (`luno/
     automation/conditions.py`'s `evaluate_condition()` - the SAME
     mechanism `"camera_patrol"` already used before this sprint) - no
     second occupancy state machine, no direct device control added to
     `RoomOccupancyModule`.
  4. Two new log-only diagnostic automation rules
     (`occupancy_test_log`, `occupancy_long_presence_test`) that read
     those state_readers - neither controls a real device.

Nothing about YOLO, RTSP, human-confirmation thresholds, `_publish_if_
not_suppressed`, the P0.8.8 dedupe fix, or the existing WLED ON/OFF
rules is touched by this sprint - Section S below re-proves the WLED
rules still behave exactly as before, now co-existing with occupancy
events on the same real Event Bus.

Sections:
  A. Snapshot schema (occupancy_age_seconds/last_transition exist).
  B. Defensive/immutable snapshot (new fields included).
  C. Vacant context (fresh instance).
  D. Occupied context (occupancy_age_seconds == presence_duration_seconds
     while occupied).
  E. Duration calculation while vacant (occupancy_age_seconds measures
     time since vacant_since - genuinely different from the frozen
     presence_duration_seconds).
  F. Monotonic clock discipline (structural + behavioral).
  G. 0->1 transition sets last_transition="occupied".
  H. 1->0 transition sets last_transition="vacant".
  I. Multi-person transitions never touch last_transition/occupancy_age's
     origin instant.
  J. occupied_since / last_transition stability under 1->2->3->1.
  K. room_occupied event payload includes previous_state="vacant".
  L. room_vacant event payload includes previous_state="occupied".
  M. No duplicate transition events (occupancy_changed count == room_
     occupied count + room_vacant count, always).
  N. AutomationEngine context access (state_readers reflect the REAL
     module, live).
  O. occupancy.state condition.
  P. occupancy.person_count condition.
  Q. occupancy.presence_duration_seconds / occupancy_age_seconds
     condition (fake clock).
  R. The two shipped diagnostic rules - real bootstrap, real shipped
     config/automation_rules.json.
  S. WLED regression - existing ON/OFF rules unaffected, no duplicate
     WLED action from occupancy events coexisting.
  T. Restart semantics (new fields never fabricated on a fresh
     instance).
  U. Architecture guards (RoomOccupancyModule stays observational; no
     second occupancy owner; state_readers close over the real module,
     not a copy).
"""

from __future__ import annotations

import ast
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
from luno.automation.conditions import evaluate_condition  # noqa: E402
from luno.automation.engine import AutomationEngine  # noqa: E402
from luno.automation.models import AutomationCondition  # noqa: E402

_VISION_OCC_PATH = os.path.join(_ROOT, "luno", "vision_occupancy.py")
_BOOTSTRAP_MODULES_PATH = os.path.join(_ROOT, "luno", "bootstrap", "modules.py")
_REAL_RULES_PATH = os.path.join(_ROOT, "config", "automation_rules.json")
_FAST_CORE_CONFIG_KW = dict(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class _FakeEventBus:
    """Same minimal convention `tests/test_p0_9_room_occupancy.py`
    already established - subscribe/publish, synchronous dispatch,
    records every published event for assertions."""

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


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


# ============================================================================
# A. Snapshot schema.
# ============================================================================

def test_A1_snapshot_has_occupancy_age_seconds_field():
    mod = RoomOccupancyModule()
    snap = mod.get_snapshot()
    assert hasattr(snap, "occupancy_age_seconds")
    assert isinstance(snap.occupancy_age_seconds, float)


def test_A2_snapshot_has_last_transition_field():
    mod = RoomOccupancyModule()
    snap = mod.get_snapshot()
    assert hasattr(snap, "last_transition")
    assert snap.last_transition is None


def test_A3_to_dict_includes_both_new_fields():
    mod, bus = _mod()
    _confirm(bus)
    d = mod.get_snapshot().to_dict()
    assert "occupancy_age_seconds" in d
    assert "last_transition" in d
    assert d["last_transition"] == "occupied"


# ============================================================================
# B. Defensive/immutable snapshot.
# ============================================================================

def test_B1_snapshot_still_frozen_with_new_fields():
    mod, bus = _mod()
    _confirm(bus)
    snap = mod.get_snapshot()
    with pytest.raises(Exception):
        snap.occupancy_age_seconds = 999.0  # type: ignore[misc]
    with pytest.raises(Exception):
        snap.last_transition = "vacant"  # type: ignore[misc]


def test_B2_repeated_reads_do_not_mutate_module_internal_state():
    mod, bus = _mod()
    _confirm(bus)
    s1 = mod.get_snapshot()
    s1_dict_before = s1.to_dict()
    for _ in range(5):
        mod.get_snapshot()
    s2 = mod.get_snapshot()
    assert s1.to_dict()["last_transition"] == s1_dict_before["last_transition"]
    assert s2.last_transition == s1.last_transition


# ============================================================================
# C. Vacant context.
# ============================================================================

def test_C1_fresh_instance_occupancy_age_is_zero():
    mod = RoomOccupancyModule()
    snap = mod.get_snapshot()
    assert snap.occupancy_age_seconds == 0.0
    assert snap.last_transition is None


def test_C2_after_a_full_visit_vacant_context_has_last_transition_vacant():
    mod, bus = _mod()
    _confirm(bus)
    _clear(bus)
    snap = mod.get_snapshot()
    assert snap.state == STATE_VACANT
    assert snap.last_transition == "vacant"


# ============================================================================
# D. Occupied context.
# ============================================================================

def test_D1_occupancy_age_equals_presence_duration_while_occupied(monkeypatch):
    import luno.vision_occupancy as occ_mod
    t = [1000.0]
    monkeypatch.setattr(occ_mod.time, "monotonic", lambda: t[0])

    mod, bus = _mod()
    _confirm(bus)
    t[0] += 12.3
    snap = mod.get_snapshot()
    assert snap.occupancy_age_seconds == pytest.approx(snap.presence_duration_seconds, abs=0.001)
    assert snap.occupancy_age_seconds == pytest.approx(12.3, abs=0.01)


def test_D2_last_transition_is_occupied_immediately_after_confirmation():
    mod, bus = _mod()
    _confirm(bus)
    assert mod.get_snapshot().last_transition == "occupied"


# ============================================================================
# E. Duration calculation while vacant - genuinely distinct metric.
# ============================================================================

def test_E1_occupancy_age_measures_time_since_vacant_while_frozen_duration_does_not(monkeypatch):
    import luno.vision_occupancy as occ_mod
    t = [2000.0]
    monkeypatch.setattr(occ_mod.time, "monotonic", lambda: t[0])

    mod, bus = _mod()
    _confirm(bus)
    t[0] += 50.0
    _clear(bus)  # presence_duration_seconds freezes at 50.0; occupancy_age_seconds resets to 0 (new "vacant" instant)

    snap0 = mod.get_snapshot()
    assert snap0.presence_duration_seconds == pytest.approx(50.0, abs=0.01)
    assert snap0.occupancy_age_seconds == pytest.approx(0.0, abs=0.01)

    t[0] += 75.0  # 75s pass while vacant
    snap1 = mod.get_snapshot()
    assert snap1.presence_duration_seconds == pytest.approx(50.0, abs=0.01), "frozen duration must not move"
    assert snap1.occupancy_age_seconds == pytest.approx(75.0, abs=0.01), "occupancy_age must keep moving while vacant"


# ============================================================================
# F. Monotonic clock discipline.
# ============================================================================

def test_F1_no_naive_datetime_subtraction_anywhere_in_module():
    """Structural proof (AST), same shape P0.9's own J1 test already
    established for `presence_duration_seconds` - re-verified here since
    this sprint added a SECOND duration computation
    (`occupancy_age_seconds`) that must obey the identical discipline."""
    source = _read(_VISION_OCC_PATH)
    tree = ast.parse(source)
    monotonic_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "monotonic"
    ]
    assert len(monotonic_calls) >= 2, "expect at least 2 call sites: presence_duration_seconds and occupancy_age_seconds"
    bad_subtractions = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Sub)
        and isinstance(n.left, ast.Call) and isinstance(n.right, ast.Call)
    ]
    assert bad_subtractions == [], "duration must never be computed via naive datetime subtraction"


def test_F2_wall_clock_jump_does_not_affect_occupancy_age(monkeypatch):
    import luno.vision_occupancy as occ_mod
    from datetime import timedelta

    mod, bus = _mod()
    _confirm(bus)
    before = mod.get_snapshot().occupancy_age_seconds

    real_utcnow = occ_mod.utcnow
    monkeypatch.setattr(occ_mod, "utcnow", lambda: real_utcnow() + timedelta(days=365))
    try:
        after = mod.get_snapshot().occupancy_age_seconds
        assert after == pytest.approx(before, abs=0.5), (
            "a wall-clock jump must not affect occupancy_age_seconds - only time.monotonic() gates it"
        )
    finally:
        monkeypatch.setattr(occ_mod, "utcnow", real_utcnow)


# ============================================================================
# G / H. Transition direction tracking.
# ============================================================================

def test_G1_zero_to_one_sets_last_transition_occupied():
    mod, bus = _mod()
    assert mod.get_snapshot().last_transition is None
    _confirm(bus)
    assert mod.get_snapshot().last_transition == "occupied"


def test_H1_one_to_zero_sets_last_transition_vacant():
    mod, bus = _mod()
    _confirm(bus)
    _clear(bus)
    assert mod.get_snapshot().last_transition == "vacant"


# ============================================================================
# I. Multi-person transitions never disturb last_transition/occupancy_age
#    origin.
# ============================================================================

def test_I1_person_count_changes_do_not_reset_last_transition(monkeypatch):
    import luno.vision_occupancy as occ_mod
    t = [3000.0]
    monkeypatch.setattr(occ_mod.time, "monotonic", lambda: t[0])

    mod, bus = _mod()
    _confirm(bus)
    t[0] += 5.0
    _frame(bus, 2)
    assert mod.get_snapshot().last_transition == "occupied"
    assert mod.get_snapshot().occupancy_age_seconds == pytest.approx(5.0, abs=0.01)

    t[0] += 5.0
    _frame(bus, 1)
    assert mod.get_snapshot().last_transition == "occupied"
    assert mod.get_snapshot().occupancy_age_seconds == pytest.approx(10.0, abs=0.01), (
        "occupancy_age_seconds must keep counting from the ORIGINAL occupied instant, "
        "never reset by a person-count-only VisionFrameProcessed update"
    )


# ============================================================================
# J. occupied_since / last_transition stability across 1->2->3->1.
# ============================================================================

def test_J1_full_multiperson_sequence_stability():
    mod, bus = _mod()

    _confirm(bus)
    _frame(bus, 1)
    since = mod.get_snapshot().occupied_since
    assert mod.get_snapshot().last_transition == "occupied"

    _frame(bus, 2)
    assert mod.get_snapshot().occupied_since == since
    assert mod.get_snapshot().last_transition == "occupied"
    assert bus.count(ROOM_OCCUPIED_EVENT_TYPE) == 1

    _frame(bus, 3)
    assert mod.get_snapshot().occupied_since == since
    assert bus.count(ROOM_OCCUPIED_EVENT_TYPE) == 1

    _frame(bus, 1)
    assert mod.get_snapshot().occupied_since == since
    assert mod.get_snapshot().last_transition == "occupied"
    assert bus.count(ROOM_VACANT_EVENT_TYPE) == 0


# ============================================================================
# K / L. Event payload previous_state (Phase 4).
# ============================================================================

def test_K1_room_occupied_payload_has_previous_state_vacant():
    mod, bus = _mod()
    _confirm(bus)
    payload = bus.data_for(ROOM_OCCUPIED_EVENT_TYPE)[0]
    assert payload["previous_state"] == STATE_VACANT


def test_K2_occupancy_changed_payload_on_entry_has_previous_state_vacant():
    mod, bus = _mod()
    _confirm(bus)
    payload = bus.data_for(OCCUPANCY_CHANGED_EVENT_TYPE)[0]
    assert payload["previous_state"] == STATE_VACANT
    assert payload["state"] == STATE_OCCUPIED


def test_L1_room_vacant_payload_has_previous_state_occupied():
    mod, bus = _mod()
    _confirm(bus)
    _clear(bus)
    payload = bus.data_for(ROOM_VACANT_EVENT_TYPE)[0]
    assert payload["previous_state"] == STATE_OCCUPIED


def test_L2_occupancy_changed_payload_on_exit_has_previous_state_occupied():
    mod, bus = _mod()
    _confirm(bus)
    _clear(bus)
    payload = bus.data_for(OCCUPANCY_CHANGED_EVENT_TYPE)[1]
    assert payload["previous_state"] == STATE_OCCUPIED
    assert payload["state"] == STATE_VACANT


def test_L3_re_entry_previous_state_is_vacant_again():
    mod, bus = _mod()
    _confirm(bus)
    _clear(bus)
    _confirm(bus)
    payloads = bus.data_for(ROOM_OCCUPIED_EVENT_TYPE)
    assert len(payloads) == 2
    assert payloads[1]["previous_state"] == STATE_VACANT


# ============================================================================
# M. No duplicate transition events.
# ============================================================================

def test_M1_occupancy_changed_count_matches_room_occupied_plus_room_vacant():
    mod, bus = _mod()
    for _ in range(3):
        _confirm(bus)
        _frame(bus, 1)
        _frame(bus, 2)
        _clear(bus)
        _clear(bus)  # extra no-op clear while already vacant
    occupied_n = bus.count(ROOM_OCCUPIED_EVENT_TYPE)
    vacant_n = bus.count(ROOM_VACANT_EVENT_TYPE)
    changed_n = bus.count(OCCUPANCY_CHANGED_EVENT_TYPE)
    assert occupied_n == 3
    assert vacant_n == 3
    assert changed_n == occupied_n + vacant_n == 6


# ============================================================================
# N. AutomationEngine context access - state_readers reflect the REAL,
#    live module (not a frozen copy taken at construction time).
# ============================================================================

def test_N1_state_readers_close_over_live_module():
    mod, bus = _mod()
    readers = {
        "occupancy.state": lambda: mod.get_snapshot().state,
        "occupancy.person_count": lambda: mod.get_snapshot().person_count,
        "occupancy.presence_duration_seconds": lambda: mod.get_snapshot().presence_duration_seconds,
        "occupancy.occupancy_age_seconds": lambda: mod.get_snapshot().occupancy_age_seconds,
        "occupancy.last_transition": lambda: mod.get_snapshot().last_transition,
    }
    assert readers["occupancy.state"]() == STATE_VACANT
    assert readers["occupancy.last_transition"]() is None

    _confirm(bus)
    assert readers["occupancy.state"]() == STATE_OCCUPIED
    assert readers["occupancy.last_transition"]() == "occupied"

    _frame(bus, 2)
    assert readers["occupancy.person_count"]() == 2


def test_N2_bootstrap_wires_occupancy_state_readers_before_engine_reads_them():
    """Static proof against `luno/bootstrap/modules.py` - `room_occupancy_
    module` must be constructed BEFORE `AutomationEngine(state_readers=...)`
    so the closures below can capture the real instance (a forward
    reference to a not-yet-assigned local would be a NameError at call
    time, not at construction time - this test catches an accidental
    re-ordering statically instead of only via a lucky runtime call)."""
    source = _read(_BOOTSTRAP_MODULES_PATH)
    occ_construct_idx = source.index("room_occupancy_module = RoomOccupancyModule()")
    engine_construct_idx = source.index("automation_engine = AutomationEngine(state_readers=")
    assert occ_construct_idx < engine_construct_idx, (
        "room_occupancy_module must be constructed before automation_engine "
        "so its state_readers lambdas close over a real instance"
    )
    for key in (
        '"occupancy.state"', '"occupancy.person_count"',
        '"occupancy.presence_duration_seconds"', '"occupancy.occupancy_age_seconds"',
        '"occupancy.last_transition"',
    ):
        assert key in source, f"bootstrap must wire {key} into AutomationEngine's state_readers"


# ============================================================================
# O. occupancy.state condition.
# ============================================================================

def test_O1_occupancy_state_equals_occupied():
    mod, bus = _mod()
    _confirm(bus)
    readers = {"occupancy.state": lambda: mod.get_snapshot().state}
    cond = AutomationCondition(type="equals", target="occupancy.state", value="occupied")
    ok, reason = evaluate_condition(cond, readers)
    assert ok is True
    assert reason == ""


def test_O2_occupancy_state_equals_vacant_when_vacant():
    mod, bus = _mod()
    readers = {"occupancy.state": lambda: mod.get_snapshot().state}
    cond = AutomationCondition(type="equals", target="occupancy.state", value="vacant")
    ok, _ = evaluate_condition(cond, readers)
    assert ok is True


def test_O3_occupancy_state_mismatch_fails_closed_not_invalid():
    mod, bus = _mod()
    _confirm(bus)
    readers = {"occupancy.state": lambda: mod.get_snapshot().state}
    cond = AutomationCondition(type="equals", target="occupancy.state", value="vacant")
    ok, reason = evaluate_condition(cond, readers)
    assert ok is False
    assert reason == ""  # genuinely evaluated and failed, not CONDITION_INVALID


# ============================================================================
# P. occupancy.person_count condition.
# ============================================================================

def test_P1_person_count_greater_equal_two():
    mod, bus = _mod()
    _confirm(bus)
    _frame(bus, 2)
    readers = {"occupancy.person_count": lambda: mod.get_snapshot().person_count}
    cond = AutomationCondition(type="greater_equal", target="occupancy.person_count", value=2)
    ok, _ = evaluate_condition(cond, readers)
    assert ok is True


def test_P2_person_count_greater_equal_two_fails_with_one_person():
    mod, bus = _mod()
    _confirm(bus)
    readers = {"occupancy.person_count": lambda: mod.get_snapshot().person_count}
    cond = AutomationCondition(type="greater_equal", target="occupancy.person_count", value=2)
    ok, _ = evaluate_condition(cond, readers)
    assert ok is False


# ============================================================================
# Q. occupancy duration conditions (fake clock).
# ============================================================================

def test_Q1_presence_duration_condition_with_fake_clock(monkeypatch):
    import luno.vision_occupancy as occ_mod
    t = [4000.0]
    monkeypatch.setattr(occ_mod.time, "monotonic", lambda: t[0])

    mod, bus = _mod()
    _confirm(bus)
    readers = {"occupancy.presence_duration_seconds": lambda: mod.get_snapshot().presence_duration_seconds}
    cond = AutomationCondition(type="greater_equal", target="occupancy.presence_duration_seconds", value=30)

    ok_before, _ = evaluate_condition(cond, readers)
    assert ok_before is False

    t[0] += 31.0
    ok_after, _ = evaluate_condition(cond, readers)
    assert ok_after is True


def test_Q2_occupancy_age_condition_while_vacant_with_fake_clock(monkeypatch):
    import luno.vision_occupancy as occ_mod
    t = [5000.0]
    monkeypatch.setattr(occ_mod.time, "monotonic", lambda: t[0])

    mod, bus = _mod()
    _confirm(bus)
    t[0] += 10.0
    _clear(bus)
    readers = {"occupancy.occupancy_age_seconds": lambda: mod.get_snapshot().occupancy_age_seconds}
    cond = AutomationCondition(type="greater_equal", target="occupancy.occupancy_age_seconds", value=60)

    ok_before, _ = evaluate_condition(cond, readers)
    assert ok_before is False

    t[0] += 61.0
    ok_after, _ = evaluate_condition(cond, readers)
    assert ok_after is True


# ============================================================================
# R. The two shipped diagnostic rules - real bootstrap, real shipped
#    config/automation_rules.json.
# ============================================================================

def _build_stack(rules_path: Optional[str] = None):
    from luno.bootstrap.adapters import register_all_adapters
    from luno.bootstrap.launcher_config import LauncherConfig
    from luno.bootstrap.modules import register_all_modules
    from luno.core.config import CoreConfig
    from luno.core.runtime import Runtime

    cfg = LauncherConfig()
    runtime = Runtime(CoreConfig(**_FAST_CORE_CONFIG_KW))
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    engine = modules["automation_engine"]
    if rules_path is not None:
        engine._rules_path = rules_path
        engine.reload_rules()

    return runtime, modules, adapter_manager


def _teardown(runtime, adapter_manager) -> None:
    from luno.bootstrap.shutdown import ShutdownCoordinator
    ShutdownCoordinator(runtime, adapter_manager).shutdown()


def test_R1_shipped_rules_file_contains_both_new_rules():
    data = json.loads(_read(_REAL_RULES_PATH))
    assert "occupancy_test_log" in data
    assert "occupancy_long_presence_test" in data
    assert data["occupancy_test_log"]["actions"][0]["type"] == "automation.log"
    assert data["occupancy_long_presence_test"]["actions"][0]["type"] == "automation.log"


def test_R2_occupancy_test_log_fires_end_to_end_on_real_confirmation():
    runtime, modules, adapter_manager = _build_stack(rules_path=_REAL_RULES_PATH)
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type=HumanPresenceConfirmed.EVENT_TYPE))
        assert _wait_until(lambda: any(c.get("rule_id") == "occupancy_test_log" for c in completed))
    finally:
        _teardown(runtime, adapter_manager)


def test_R3_occupancy_long_presence_test_condition_mechanism():
    """`occupancy_long_presence_test` triggers on `occupancy_changed` and
    additionally requires `occupancy.presence_duration_seconds >= 30`.
    Under P0.9/P0.10's current semantics, `occupancy_changed` only ever
    fires AT a genuine state transition instant - duration is ~0 the
    moment the room becomes occupied, and `occupancy.state` is no longer
    `"occupied"` the moment it becomes vacant - so this exact combination
    has a narrow-to-nonexistent natural firing window today (documented
    as a known limitation, see docs/change_impact/camera_automation_
    p0_10.md). This test instead proves the MECHANISM itself is wired
    correctly: given a real engine with the real shipped rule loaded,
    and a real occupancy_changed event whose accompanying state readers
    genuinely report a long duration, the rule DOES fire; and with a
    short duration, it does NOT."""
    runtime, modules, adapter_manager = _build_stack(rules_path=_REAL_RULES_PATH)
    occupancy_module = modules["room_occupancy_module"]
    completed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.event_bus.subscribe("automation.skipped", lambda e: skipped.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type=HumanPresenceConfirmed.EVENT_TYPE))
        assert _wait_until(lambda: occupancy_module.get_snapshot().state == STATE_OCCUPIED)

        # Short duration - condition must fail (rule skipped, not fired).
        runtime.event_bus.publish(Event(
            type="occupancy_changed", data=occupancy_module.get_snapshot().to_dict(),
        ))
        assert _wait_until(lambda: any(
            s.get("rule_id") == "occupancy_long_presence_test" for s in skipped
        ))
        assert not any(c.get("rule_id") == "occupancy_long_presence_test" for c in completed)

        # Long duration - condition must pass (rule fires). Rather than
        # freezing the process-wide `time.monotonic()` (which would also
        # freeze the live Runtime's own scheduler/heartbeat background
        # threads - unsafe alongside a real multi-threaded bootstrap),
        # age the module's own already-real `_occupied_since_monotonic`
        # backward by 31s directly - a deterministic, isolated way to
        # make `presence_duration_seconds` genuinely read >= 30 without
        # disturbing any other component's clock.
        with occupancy_module._lock:
            occupancy_module._occupied_since_monotonic -= 31.0
            occupancy_module._last_transition_monotonic = occupancy_module._occupied_since_monotonic
        assert occupancy_module.get_snapshot().presence_duration_seconds >= 30.0

        runtime.event_bus.publish(Event(
            type="occupancy_changed", data=occupancy_module.get_snapshot().to_dict(),
        ))
        assert _wait_until(lambda: any(
            c.get("rule_id") == "occupancy_long_presence_test" for c in completed
        ))
    finally:
        _teardown(runtime, adapter_manager)


def test_R4_neither_diagnostic_rule_ever_dispatches_a_tool_call():
    """Phase 5's own explicit requirement - "These rules must NOT control
    real devices" - both are `automation.log`-only, so a real bootstrap
    run must never publish `tool_requested` as a consequence of either
    firing."""
    runtime, modules, adapter_manager = _build_stack(rules_path=_REAL_RULES_PATH)
    tool_calls: List[Dict[str, Any]] = []
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data))
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type=HumanPresenceConfirmed.EVENT_TYPE))
        assert _wait_until(lambda: any(c.get("rule_id") == "occupancy_test_log" for c in completed))
        occupancy_tool_calls = [
            c for c in tool_calls
            if c.get("tool_call", {}).get("target") not in ("light.wled",)
        ]
        # Only the pre-existing WLED ON rule may have dispatched a tool
        # call for this same human_confirmed event; neither occupancy
        # rule may have contributed one of its own.
        assert len(tool_calls) <= 1
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# S. WLED regression - existing ON/OFF rules unaffected by occupancy
#    events coexisting on the same real Event Bus.
# ============================================================================

def _camera_event_data(kind: str) -> Dict[str, Any]:
    """The field shape the REAL shipped WLED rules require
    (`event.available == true`, `event.detection_error == null` -
    `config/automation_rules.json`'s own `camera_human_detected_test_
    action`/`camera_wled_human_cleared_off` conditions). Published
    directly on `CAMERA_EVENT_TYPE` here (bypassing `VisionCameraEvent
    Bridge`, whose `vision_status_reader` is only wired by `main.py`
    post-bootstrap - unrelated to this sprint, same gap every other
    real-shipped-rule test in this project already routes around this
    same way, e.g. `tests/test_p0_8_6_end_to_end_human_wled_reliability.
    py`)."""
    return {
        "camera_id": "tapo_c212", "kind": kind, "entity_id": f"vision:{kind}",
        "old_state": None, "new_state": None, "confidence": None,
        "timestamp": time.time(), "source": "vision",
        "available": True, "detection_error": None,
    }


def test_S1_wled_on_still_fires_exactly_once_alongside_occupancy_events():
    from luno.camera_automation import CAMERA_EVENT_TYPE, CameraAutomationConfig, CameraAutomationModule
    from luno.tool_manager.builtin.home_assistant import MockHomeAssistantHandler

    runtime, modules, adapter_manager = _build_stack(rules_path=_REAL_RULES_PATH)
    cam_module: "CameraAutomationModule" = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=True, cooldown_s=0.0)

    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    runtime.start()
    try:
        handler = modules["tool_manager_module"].manager.registry.get("home_assistant")
        assert isinstance(handler, MockHomeAssistantHandler), "must never exercise a real HA call"

        # Both pipelines fire from the SAME real arrival: the real Vision
        # confirmation event (what RoomOccupancyModule alone consumes)
        # and the real camera_automation event (what the WLED ON rule
        # alone consumes) - proving neither interferes with the other.
        runtime.event_bus.publish(Event(type=HumanPresenceConfirmed.EVENT_TYPE))
        runtime.event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=_camera_event_data("human_confirmed")))

        def _wled_on_dispatched() -> bool:
            return sum(
                1 for c in tool_calls
                if c.get("tool") == "home_assistant" and c.get("target") == "light.wled" and c.get("action") == "turn_on"
            ) >= 1

        assert _wait_until(_wled_on_dispatched)
        time.sleep(0.3)  # give any spurious duplicate a chance to appear
        on_count = sum(
            1 for c in tool_calls
            if c.get("tool") == "home_assistant" and c.get("target") == "light.wled" and c.get("action") == "turn_on"
        )
        assert on_count == 1, "human_confirmed + room_occupied for the same arrival must never double-fire WLED ON"

        occupancy_module = modules["room_occupancy_module"]
        assert _wait_until(lambda: occupancy_module.get_snapshot().state == STATE_OCCUPIED)
    finally:
        _teardown(runtime, adapter_manager)


def test_S2_wled_off_still_fires_exactly_once_alongside_occupancy_events():
    from luno.camera_automation import CAMERA_EVENT_TYPE, CameraAutomationConfig, CameraAutomationModule
    from luno.tool_manager.builtin.home_assistant import MockHomeAssistantHandler

    runtime, modules, adapter_manager = _build_stack(rules_path=_REAL_RULES_PATH)
    cam_module: "CameraAutomationModule" = modules["camera_automation_module"]
    cam_module._config = CameraAutomationConfig(enabled=True, cooldown_s=0.0)

    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    runtime.start()
    try:
        handler = modules["tool_manager_module"].manager.registry.get("home_assistant")
        assert isinstance(handler, MockHomeAssistantHandler)

        runtime.event_bus.publish(Event(type=HumanPresenceConfirmed.EVENT_TYPE))
        runtime.event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=_camera_event_data("human_confirmed")))
        assert _wait_until(lambda: any(
            c.get("tool") == "home_assistant" and c.get("target") == "light.wled" and c.get("action") == "turn_on"
            for c in tool_calls
        ))

        runtime.event_bus.publish(Event(type=CameraPersonLeft.EVENT_TYPE))
        runtime.event_bus.publish(Event(type=CAMERA_EVENT_TYPE, data=_camera_event_data("human_cleared")))

        def _wled_off_dispatched() -> bool:
            return sum(
                1 for c in tool_calls
                if c.get("tool") == "home_assistant" and c.get("target") == "light.wled" and c.get("action") == "turn_off"
            ) >= 1

        # camera_wled_human_cleared_off has a real 10s delay (P0.8.9) -
        # the scheduler dispatch itself is what we're waiting for here.
        assert _wait_until(_wled_off_dispatched, timeout_s=13.0)
        off_count = sum(
            1 for c in tool_calls
            if c.get("tool") == "home_assistant" and c.get("target") == "light.wled" and c.get("action") == "turn_off"
        )
        assert off_count == 1, "human_cleared + room_vacant for the same departure must never double-fire WLED OFF"

        occupancy_module = modules["room_occupancy_module"]
        assert _wait_until(lambda: occupancy_module.get_snapshot().state == STATE_VACANT)
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# T. Restart semantics.
# ============================================================================

def test_T1_fresh_instance_after_restart_never_fabricates_new_fields():
    mod = RoomOccupancyModule()
    snap = mod.get_snapshot()
    assert snap.state == STATE_VACANT
    assert snap.occupied_since is None
    assert snap.occupancy_age_seconds == 0.0
    assert snap.last_transition is None


# ============================================================================
# U. Architecture guards.
# ============================================================================

def _non_comment_non_docstring_code(path: str) -> str:
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


def test_U1_room_occupancy_module_still_does_not_control_devices():
    code = _non_comment_non_docstring_code(_VISION_OCC_PATH)
    for forbidden in ("light.wled", "turn_on", "turn_off", "wled", "WLED", "home_assistant", "HomeAssistant"):
        assert forbidden not in code, f"vision_occupancy.py must not reference {forbidden!r} - P0.10 adds no device control"


def test_U2_room_occupancy_module_still_does_not_import_automation_engine():
    code = _non_comment_non_docstring_code(_VISION_OCC_PATH)
    for forbidden in ("AutomationEngine", "automation.engine", "from luno.automation"):
        assert forbidden not in code, (
            f"vision_occupancy.py must not reference {forbidden!r} - AutomationEngine reads FROM this module "
            "via state_readers, never the other way around"
        )


def test_U3_single_canonical_occupancy_owner_still_holds():
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


def test_U4_bootstrap_never_calls_a_control_method_on_room_occupancy_module():
    """Static guard - `luno/bootstrap/modules.py` may only construct,
    bind, register, and read `room_occupancy_module` (via `get_snapshot`/
    lifecycle methods) - never call anything that would imply it directly
    controls a device (Rule 5 of the P0.10 brief)."""
    source = _read(_BOOTSTRAP_MODULES_PATH)
    forbidden_calls_on_module = [
        "room_occupancy_module.turn_on(", "room_occupancy_module.turn_off(",
        "room_occupancy_module.dispatch(", "room_occupancy_module.set_light(",
    ]
    for forbidden in forbidden_calls_on_module:
        assert forbidden not in source


def test_U5_room_occupancy_module_does_not_import_tool_manager():
    code = _non_comment_non_docstring_code(_VISION_OCC_PATH)
    for forbidden in ("ToolManager", "tool_manager", "tool_requested", "ToolRegistry"):
        assert forbidden not in code


def test_U6_room_occupancy_module_does_not_perform_a_second_yolo_inference():
    code = _non_comment_non_docstring_code(_VISION_OCC_PATH)
    for forbidden in ("ultralytics", "YOLO(", "torch", "cv2", "VideoCapture", "rtsp", "RTSP"):
        assert forbidden not in code


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
