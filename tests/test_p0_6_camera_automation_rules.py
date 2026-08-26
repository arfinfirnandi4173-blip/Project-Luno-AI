"""
tests/test_p0_6_camera_automation_rules.py
===========================================

LUNO P0.6 (Camera Automation Rule Integration + Log-Only) - dedicated
regression suite.

Connects the already-live-verified camera event pipeline

    Tapo C212 -> RTSP/Vision/YOLO -> CameraPersonEntered/CameraPersonLeft
        -> Vision Bridge -> CameraAutomationModule
        -> camera_automation.camera_event

to the EXISTING `luno.automation.AutomationEngine` (Sprint 72) - no new
rule engine, no new action-execution path, no new Event Bus mechanism.
The only genuinely new capability this sprint adds to that engine is a
small, additive condition target prefix, `event.<field>`, letting a
condition read a FIELD of the event that triggered THIS execution (see
`luno/automation/conditions.py`'s own "P0.6 addition" docstring section
for the full architectural rationale - the pre-existing condition
engine could only read externally-registered `state_readers`, never
the triggering event's own payload, which made "match only
kind=='human_detected'" structurally inexpressible before this sprint).

Every test in this file either:
  (a) is pure (no bootstrap, tests `evaluate_condition`/`rule_from_dict`
      directly), or
  (b) uses the SAME real-bootstrap helper convention
      `tests/test_sprint72_automation_engine.py` already established
      (`register_all_modules`/`register_all_adapters`, all-mock
      backends, a TEMPORARY `automation_rules.json`-equivalent file -
      NEVER the real `config/automation_rules.json`, except for the one
      dedicated integration test in Section H that intentionally loads
      the real file to prove the actual P0.6 rule shipped in this repo
      loads and fires correctly end to end).

This suite proves EVENT -> RULE -> MATCH -> LOG. It does not, and must
not, prove or claim any real Home Assistant/PTZ/device action - P0.6's
own explicit prohibition (see Section D "safety" tests below, which
prove the OPPOSITE: that no such action is ever dispatched).
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

from luno.automation.conditions import CONDITION_INVALID, evaluate_condition  # noqa: E402
from luno.automation.engine import AutomationEngine  # noqa: E402
from luno.automation.models import (  # noqa: E402
    AutomationAction,
    AutomationCondition,
    AutomationRule,
    AutomationRuleError,
    AutomationTrigger,
    rule_from_dict,
    validate_rule,
)

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)

_RULE_ID = "camera_human_detected_log"
_REAL_RULES_PATH = os.path.join(_ROOT, "config", "automation_rules.json")


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _camera_rule(enabled: bool = True, target: str = "event.kind", value: str = "human_detected") -> Dict[str, Any]:
    """The P0.6 rule shape, mirroring exactly what ships in
    `config/automation_rules.json` (Section H re-loads that real file
    directly to prove they haven't drifted apart)."""
    return {
        _RULE_ID: {
            "name": "Camera human detected (log only)",
            "enabled": enabled,
            "trigger": "event:camera_automation.camera_event",
            "conditions": [{"type": "equals", "target": target, "value": value}],
            "actions": [{"type": "automation.log", "parameters": {"message": "kind matched (log-only)"}}],
            "cooldown_seconds": 0.0,
        }
    }


def _camera_event_data(kind: str, camera_id: str = "tapo_c212") -> Dict[str, Any]:
    return {
        "camera_id": camera_id, "kind": kind, "entity_id": f"vision:{kind}",
        "old_state": None, "new_state": None, "confidence": None,
        "timestamp": time.time(), "source": "vision",
    }


def _write_rules(path: str, rules: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rules, fh)


def _build_stack(rules: Optional[Dict[str, Any]] = None, rules_path: Optional[str] = None):
    """Real bootstrap, same helper convention as `tests/
    test_sprint72_automation_engine.py::_build_stack`. Always points
    the freshly-constructed `AutomationEngine` at a TEMPORARY rules
    file (never the real `config/automation_rules.json`) unless a
    caller explicitly passes `rules_path=_REAL_RULES_PATH` (Section H
    only)."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    engine = modules["automation_engine"]
    if rules_path is None:
        fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="p0_6_automation_rules_test_")
        os.close(fd)
    engine._rules_path = rules_path
    if rules is not None:
        _write_rules(rules_path, rules)
    engine.reload_rules()

    return runtime, modules, adapter_manager, rules_path


def _teardown(runtime, adapter_manager, rules_path: Optional[str] = None) -> None:
    ShutdownCoordinator(runtime, adapter_manager).shutdown()
    if rules_path is not None and rules_path != _REAL_RULES_PATH:
        try:
            os.remove(rules_path)
        except OSError:
            pass


# ============================================================================
# A. Rule loading
# ============================================================================

def test_01_valid_camera_rule_loads():
    rule = rule_from_dict(_RULE_ID, _camera_rule()[_RULE_ID])
    validate_rule(rule)  # must not raise
    assert rule.trigger.type == "event"
    assert rule.trigger.parameters["event_name"] == "camera_automation.camera_event"
    assert rule.conditions[0].target == "event.kind"
    assert rule.conditions[0].value == "human_detected"
    assert rule.actions[0].type == "automation.log"


def test_02_disabled_camera_rule_loads_but_is_marked_disabled():
    data = _camera_rule(enabled=False)
    rule = rule_from_dict(_RULE_ID, data[_RULE_ID])
    validate_rule(rule)  # loading/validating a disabled rule must not raise
    assert rule.enabled is False


def test_03_malformed_rule_rejected_safely_not_crash():
    """An engine `reload_rules()` over a file containing one malformed
    rule alongside one valid rule must skip the bad one and keep the
    good one - never crash the whole load (existing `_load_rules_from_
    disk()` behavior, exercised here through the real engine, not just
    asserted about in isolation)."""
    runtime, modules, adapter_manager, rules_path = _build_stack(rules={
        "bad_rule": {"name": "bad", "trigger": "not-a-real-trigger", "actions": [{"type": "automation.log"}]},
        **_camera_rule(),
    })
    try:
        engine = modules["automation_engine"]
        assert "bad_rule" not in engine._rules
        assert _RULE_ID in engine._rules
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_04_malformed_rule_alone_raises_at_the_pure_validate_layer():
    with pytest.raises(AutomationRuleError):
        rule = rule_from_dict("bad", {"name": "bad", "trigger": "not-a-real-trigger", "actions": [{"type": "automation.log"}]})
        validate_rule(rule)


# ============================================================================
# B. Event matching (`event.<field>` condition target - the P0.6 addition
#    to the pure, pre-existing `evaluate_condition()`)
# ============================================================================

def test_05_human_detected_matches():
    condition = AutomationCondition(type="equals", target="event.kind", value="human_detected")
    ok, reason = evaluate_condition(condition, {}, event_data=_camera_event_data("human_detected"))
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize("kind", ["human_cleared", "camera_online", "camera_offline"])
def test_06_other_kinds_do_not_match(kind):
    condition = AutomationCondition(type="equals", target="event.kind", value="human_detected")
    ok, reason = evaluate_condition(condition, {}, event_data=_camera_event_data(kind))
    assert ok is False


def test_07_no_event_data_is_invalid_not_a_false_match():
    """A time/manual trigger has no originating event at all - an
    `event.*` condition must fail CLOSED (CONDITION_INVALID), never be
    silently treated as a pass."""
    condition = AutomationCondition(type="equals", target="event.kind", value="human_detected")
    ok, reason = evaluate_condition(condition, {}, event_data=None)
    assert ok is False
    assert reason == CONDITION_INVALID


def test_08_missing_field_on_a_present_event_is_invalid():
    condition = AutomationCondition(type="equals", target="event.nonexistent_field", value="x")
    ok, reason = evaluate_condition(condition, {}, event_data=_camera_event_data("human_detected"))
    assert ok is False
    assert reason == CONDITION_INVALID


def test_09_non_event_prefixed_targets_are_completely_unaffected():
    """Backward-compatibility proof: a target that does NOT start with
    'event.' resolves via state_readers exactly as it did before P0.6 -
    event_data is never consulted for it, even if supplied."""
    condition = AutomationCondition(type="equals", target="camera_patrol", value="idle")
    ok, reason = evaluate_condition(
        condition, {"camera_patrol": lambda: "idle"}, event_data=_camera_event_data("human_detected"),
    )
    assert ok is True
    ok2, reason2 = evaluate_condition(condition, {"camera_patrol": lambda: "idle"})  # no event_data kwarg at all
    assert ok2 is True


def test_10_camera_id_field_can_also_be_matched_generically():
    """Section 10 of the brief: camera-specific conditions must be
    POSSIBLE (via the same generic event.<field> mechanism) without
    being made mandatory anywhere in the shipped rule."""
    condition = AutomationCondition(type="equals", target="event.camera_id", value="tapo_c212")
    ok, _ = evaluate_condition(condition, {}, event_data=_camera_event_data("human_detected", camera_id="tapo_c212"))
    assert ok is True
    ok2, _ = evaluate_condition(condition, {}, event_data=_camera_event_data("human_detected", camera_id="some_other_camera"))
    assert ok2 is False


def test_11_shipped_rule_does_not_hardcode_a_camera_id():
    """The actual rule in config/automation_rules.json must stay
    generic (kind-only) - no invented/unverified camera identity."""
    with open(_REAL_RULES_PATH, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    rule_data = raw[_RULE_ID]
    targets = [c.get("target") for c in rule_data.get("conditions", [])]
    assert "event.camera_id" not in targets
    assert targets == ["event.kind"]


# ============================================================================
# C. Execution (through the real engine + real Event Bus)
# ============================================================================

def test_12_matched_event_executes_log_action_end_to_end():
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_camera_rule())
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: len(completed) == 1)
        assert completed[0]["rule_id"] == _RULE_ID
    finally:
        _teardown(runtime, adapter_manager, rules_path)


@pytest.mark.parametrize("kind", ["human_cleared", "camera_online", "camera_offline"])
def test_13_unmatched_event_does_not_execute_the_log_action(kind):
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_camera_rule())
    completed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.event_bus.subscribe("automation.skipped", lambda e: skipped.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data(kind)))
        assert _wait_until(lambda: len(skipped) == 1)
        assert completed == []
        assert skipped[0]["rule_id"] == _RULE_ID
        assert skipped[0]["reason"] == "condition_failed"
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_14_disabled_rule_never_triggers_at_all():
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_camera_rule(enabled=False))
    completed: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    triggered: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.event_bus.subscribe("automation.skipped", lambda e: skipped.append(e.data))
    runtime.event_bus.subscribe("automation.triggered", lambda e: triggered.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        time.sleep(0.3)  # a disabled rule produces no event at all - nothing to _wait_until for
        assert completed == []
        assert skipped == []
        assert triggered == []
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_15_reenabling_a_previously_disabled_rule_makes_it_fire():
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_camera_rule(enabled=True))
    engine = modules["automation_engine"]
    engine.disable_automation(_RULE_ID)
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        time.sleep(0.3)
        assert completed == []

        engine.enable_automation(_RULE_ID)
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: len(completed) == 1)
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# D. Safety - no Home Assistant / PTZ / device action is ever dispatched
# ============================================================================

def test_16_matched_execution_never_publishes_tool_requested():
    """`automation.log` is in the engine's own `_INTERNAL_ACTION_TYPES`
    allowlist (`luno/automation/models.py`) and `_dispatch_internal_
    action()` (`luno/automation/engine.py`) never calls `_dispatch_
    tool_call()` - structurally, not just by convention, it is
    impossible for this rule's action to reach Home Assistant/PTZ/any
    other tool. This test proves it behaviorally: zero `tool_requested`
    events are published for the ENTIRE window this rule's execution
    runs in."""
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_camera_rule())
    tool_calls: List[Dict[str, Any]] = []
    completed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data))
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: len(completed) == 1)
        assert tool_calls == []
    finally:
        _teardown(runtime, adapter_manager, rules_path)


def test_17_action_allowlist_marks_automation_log_internal_only():
    """Static confirmation of the structural guarantee test_16 exercises
    behaviorally - `automation.log` is in `_INTERNAL_ACTION_TYPES` and
    NOT in `_CAMERA_ACTION_TYPES`, and no HA action type is used by the
    shipped rule."""
    from luno.automation.models import _CAMERA_ACTION_TYPES, _INTERNAL_ACTION_TYPES
    assert "automation.log" in _INTERNAL_ACTION_TYPES
    assert "automation.log" not in _CAMERA_ACTION_TYPES
    with open(_REAL_RULES_PATH, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    action_types = [a.get("type") for a in raw[_RULE_ID].get("actions", [])]
    assert action_types == ["automation.log"]
    assert not any(t.startswith("home_assistant.") for t in action_types)
    assert not any(t.startswith("camera.") for t in action_types)


def test_18_action_exception_is_isolated_engine_and_bus_survive(monkeypatch):
    """A failing log action must not crash the Event Bus, the engine, or
    any other subscriber - `_run_execution()`'s own top-level try/except
    (Sprint 72) already guarantees this. Forces `_dispatch_internal_
    action()` to raise for one execution (the most direct, honest way to
    exercise this path - every JSON-representable `message` value
    already round-trips through `str(...)` without error, so a genuine
    failure has to be injected rather than found "naturally") and proves
    the engine publishes `automation.failed` (not a crash) and keeps
    handling a second, unrelated event normally right after."""
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_camera_rule())
    engine = modules["automation_engine"]

    def _boom(action, execution):
        raise RuntimeError("simulated action failure")

    monkeypatch.setattr(engine, "_dispatch_internal_action", _boom)

    other_seen: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: other_seen.append(("completed", e.data)))
    runtime.event_bus.subscribe("automation.failed", lambda e: other_seen.append(("failed", e.data)))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: len(other_seen) >= 1)
        assert other_seen[0][0] == "failed"

        # Engine/bus still alive - a second, independent event bus publish
        # still reaches every subscriber normally.
        marker: List[bool] = []
        runtime.event_bus.subscribe("marker_event", lambda e: marker.append(True))
        runtime.event_bus.publish(Event(type="marker_event", data={}))
        assert _wait_until(lambda: marker == [True])
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# E. Duplicate events (documented, not a new cooldown system - Section 13)
# ============================================================================

def test_19_three_rapid_duplicate_events_each_produce_their_own_completed_and_do_not_crash():
    """P0.6's shipped rule has `cooldown_seconds: 0.0` (no NEW cooldown
    system invented this sprint - Section 13's own instruction). Three
    rapid human_detected events therefore each independently trigger and
    complete - documented here as the actual current behavior, not
    silently redesigned. The engine's own PRE-EXISTING loop-protection
    safety net (Sprint 72, `_MAX_FIRINGS_IN_WINDOW=3` within 5s) is a
    cycle/runaway-loop guard, not an intentional dedupe mechanism - a
    4th event within the same short window would be refused with
    `automation_cycle_detected`, which this test also documents rather
    than asserting away."""
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=_camera_rule())
    completed: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.event_bus.subscribe("automation.failed", lambda e: failed.append(e.data))
    runtime.start()
    try:
        for _ in range(3):
            runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
            time.sleep(0.05)
        assert _wait_until(lambda: len(completed) == 3, timeout_s=5.0)
        assert failed == []  # 3 within the window is still under _MAX_FIRINGS_IN_WINDOW
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# F. Real config/automation_rules.json integration (the actual shipped file)
# ============================================================================

def test_20_real_shipped_rules_file_loads_the_camera_rule():
    """Loads the ACTUAL `config/automation_rules.json` this sprint
    ships (never a copy/temp file) through the real engine's own
    `_load_rules_from_disk()` - proves the file on disk is valid and
    matches what every other test in this file exercises in isolation."""
    engine = AutomationEngine(rules_path=_REAL_RULES_PATH)
    rules = engine._load_rules_from_disk()
    assert _RULE_ID in rules
    rule = rules[_RULE_ID]
    assert rule.enabled is True
    assert rule.trigger.parameters["event_name"] == "camera_automation.camera_event"


def test_21_full_real_bootstrap_camera_event_reaches_the_real_shipped_rule():
    """Section 15/16's own integration test: CameraAutomation event ->
    Event Bus -> AutomationEngine -> camera_human_detected_log -> log
    action, through the REAL bootstrap AND the REAL, shipped
    `config/automation_rules.json` (not a synthetic copy) - no camera
    hardware required (matches P0.5.4's own already-completed hardware
    verification; this test only proves the NEW rule layer on top of
    it)."""
    runtime, modules, adapter_manager, rules_path = _build_stack(rules=None, rules_path=_REAL_RULES_PATH)
    completed: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("automation.completed", lambda e: completed.append(e.data))
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data))
    runtime.start()
    try:
        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data=_camera_event_data("human_detected")))
        assert _wait_until(lambda: any(c["rule_id"] == _RULE_ID for c in completed))
        matches = [c for c in completed if c["rule_id"] == _RULE_ID]
        assert len(matches) == 1
        # P0.6.2 update (superseded by the P0.8.6 update below): the
        # REAL shipped config/automation_rules.json ALSO contains a
        # second, real-device rule (camera_human_detected_test_action -
        # see tests/test_p0_6_2_camera_ha_action.py for its own
        # dedicated suite), which - AS OF P0.6.2 - issued exactly one
        # tool_requested for this SAME event.
        #
        # P0.8.6 UPDATE (documented, intentional - see docs/change_
        # impact/camera_automation_p0_8_6.md Section 3):
        # camera_human_detected_test_action now requires event.kind ==
        # "human_confirmed" (P0.8.6's own new, stricter, sustained-
        # detection-only signal) PLUS event.available == true /
        # event.detection_error == null. `_camera_event_data()` above
        # publishes `kind="human_detected"` with neither `available` nor
        # `detection_error` set - so as of P0.8.6 that rule's own
        # conditions now correctly fail-closed and refuse to match,
        # contributing ZERO tool_requested calls here. Combined with
        # THIS rule's own structurally-proven invariant (log-only can
        # never reach _dispatch_tool_call - re-confirmed by P0.6.1's own
        # test_09_automation_log_action_cannot_reach_dispatch_
        # tool_call), the fully-attributable total is now zero, not one.
        assert len(tool_calls) == 0
    finally:
        _teardown(runtime, adapter_manager, rules_path)


# ============================================================================
# G. Security scan re-confirmation (P0.6 touched conditions.py/engine.py,
#    both already covered by Sprint 72's own AST-based scan - this is a
#    lightweight, file-local re-assertion, not a duplicate of that suite)
# ============================================================================

def test_22_no_eval_exec_or_dynamic_import_in_the_files_p0_6_touched():
    import ast
    forbidden_calls = {"eval", "exec", "__import__"}
    for rel in ("conditions.py", "engine.py"):
        path = os.path.join(_ROOT, "luno", "automation", rel)
        with open(path, "r", encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else (func.attr if isinstance(func, ast.Attribute) else None)
                assert name not in forbidden_calls, f"forbidden call {name!r} found in {path}"


def test_23_shipped_rule_never_logs_a_credential_shaped_value():
    with open(_REAL_RULES_PATH, "r", encoding="utf-8") as fh:
        raw_text = fh.read()
    lowered = raw_text.lower()
    for forbidden in ("password", "token", "secret", "rtsp://", "authorization"):
        assert forbidden not in lowered
