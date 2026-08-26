"""
tests/test_p0_6_1_live_log_verification.py
==============================================

LUNO P0.6.1 (Live Camera -> Automation Log-Only Verification) - dedicated
test file for the NEW logic P0.6.1 added to `luno_live_camera_event_
observer.py` (the SAME script P0.5.4-LIVE/P0.5.4-FIX already shipped -
this sprint reuses it rather than building a second observer, per its
own brief's explicit instruction).

What P0.6.1 added to that script, and what this file verifies:
  1. A rule-loaded/enabled pre-check via `AutomationEngine.get_
     automation_status()` (a pre-existing, public, read-only Sprint 72
     accessor - not a new mechanism).
  2. `_LiveObserver.on_automation_event(outcome)` - counts Sprint 72's
     own `automation.<outcome>` events, filtered to ONLY `_TRACKED_
     RULE_ID` ("camera_human_detected_log" - the one rule this sprint
     line ships; no second rule was created).
  3. `_LiveObserver.on_tool_requested()` - the device-action safety
     count (Section 12).

This file does NOT and cannot exercise real Tapo C212 hardware - same
structural limitation documented in every prior sprint in this line
(this agent's own tool execution has no network route to the camera).
Every test here is either pure/static, or a real-bootstrap test using a
SIMULATED event through the real Event Bus - proving the observer's own
NEW wiring is correct, never presented as hardware evidence. The actual
live human-detection walk-test remains the user's own action, per
Section 8 of this sprint's brief.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code_only(source: str) -> str:
    """Same helper `test_luno_live_camera_event_observer.py` already
    established - strips triple-quoted docstrings/comments so static
    "forbidden string" checks only look at actual executable code."""
    import re
    no_docstrings = re.sub(r'"""[\s\S]*?"""', "", source)
    no_comments = "\n".join(line.split("#", 1)[0] for line in no_docstrings.splitlines())
    return no_comments


if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno_live_camera_event_observer as obs  # noqa: E402
from luno.core.events import Event  # noqa: E402


# ---------------------------------------------------------------------
# Static shape / constant checks
# ---------------------------------------------------------------------

def test_01_tracked_rule_id_matches_the_actual_shipped_p0_6_rule():
    """This script must track outcomes for the SAME rule id P0.6
    actually shipped in config/automation_rules.json - not a
    typo/second rule."""
    import json
    with open(os.path.join(_ROOT, "config", "automation_rules.json"), "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    assert obs._TRACKED_RULE_ID in raw


def test_02_live_observer_has_the_new_p0_6_1_attributes():
    observer = obs._LiveObserver()
    assert observer.automation_event_counts == {}
    assert observer.tool_requested_count == 0


# ---------------------------------------------------------------------
# on_automation_event - filtering by rule_id
# ---------------------------------------------------------------------

def test_03_on_automation_event_counts_only_the_tracked_rule():
    observer = obs._LiveObserver()
    handler = observer.on_automation_event("completed")
    handler(Event(type="automation.completed", data={"rule_id": obs._TRACKED_RULE_ID, "execution_id": "e1"}))
    handler(Event(type="automation.completed", data={"rule_id": "some_other_rule", "execution_id": "e2"}))
    assert observer.automation_event_counts["completed"] == 1


def test_04_on_automation_event_counts_each_outcome_independently():
    observer = obs._LiveObserver()
    for outcome in ("triggered", "condition_passed", "completed", "skipped", "failed"):
        handler = observer.on_automation_event(outcome)
        handler(Event(type=f"automation.{outcome}", data={"rule_id": obs._TRACKED_RULE_ID}))
    assert observer.automation_event_counts == {
        "triggered": 1, "condition_passed": 1, "completed": 1, "skipped": 1, "failed": 1,
    }


def test_05_on_automation_event_prints_reason_when_present(capsys):
    observer = obs._LiveObserver()
    handler = observer.on_automation_event("skipped")
    handler(Event(type="automation.skipped", data={"rule_id": obs._TRACKED_RULE_ID, "reason": "condition_failed"}))
    out = capsys.readouterr().out
    assert "condition_failed" in out
    assert obs._TRACKED_RULE_ID in out


def test_06_on_automation_event_never_prints_for_an_untracked_rule(capsys):
    observer = obs._LiveObserver()
    handler = observer.on_automation_event("completed")
    handler(Event(type="automation.completed", data={"rule_id": "unrelated_rule"}))
    out = capsys.readouterr().out
    assert out == ""
    assert observer.automation_event_counts == {}


# ---------------------------------------------------------------------
# on_tool_requested - device-action safety count
# ---------------------------------------------------------------------

def test_07_on_tool_requested_counts_and_never_prints_target_or_action(capsys):
    """P0.6.2 update: `on_tool_requested` now legitimately reads and
    prints the TOOL NAME only (e.g. "home_assistant") to classify HA vs
    PTZ vs other device actions (Section 19 of that sprint's brief) -
    this is a deliberate, safe change (a tool name is not a secret).
    What must still NEVER be printed is the actual target entity/action
    - the full nested `tool_call` payload."""
    observer = obs._LiveObserver()
    observer.on_tool_requested(Event(type="tool_requested", data={
        "tool_call": {"tool": "home_assistant", "action": "turn_on", "target": "light.living_room"},
    }))
    out = capsys.readouterr().out
    assert observer.tool_requested_count == 1
    assert "light.living_room" not in out
    assert "home_assistant" in out  # P0.6.2 - tool name IS now printed, deliberately


def test_08_on_tool_requested_static_proof_never_reads_target_or_action():
    """P0.6.2 update: the handler now legitimately reads `event.data`
    (to classify by tool name), so the old "never reads event.data at
    all" guarantee no longer applies verbatim - re-scoped to what still
    must hold: the handler's own code never reads `tool_call["target"]`/
    `tool_call["action"]`/`tool_call["parameters"]` (the fields that
    could carry an entity id or, in principle, other call detail)."""
    import inspect
    source = _code_only(inspect.getsource(obs._LiveObserver.on_tool_requested))
    for forbidden in ('"target"', "'target'", '"action"', "'action'", '"parameters"', "'parameters'"):
        assert forbidden not in source, f"on_tool_requested must not read {forbidden}"


# ---------------------------------------------------------------------
# Structural safety guarantee this whole sprint rests on
# ---------------------------------------------------------------------

def test_09_automation_log_action_cannot_reach_dispatch_tool_call():
    """Re-confirms (does not merely assume) the structural claim this
    sprint's evidence format leans on: `_dispatch_internal_action()` -
    the function `automation.log` always routes through - never calls
    `_dispatch_tool_call()` anywhere in its own source."""
    import inspect
    from luno.automation.engine import AutomationEngine
    source = _code_only(inspect.getsource(AutomationEngine._dispatch_internal_action))
    assert "_dispatch_tool_call" not in source


def test_10_shipped_rule_action_is_automation_log_only():
    import json
    with open(os.path.join(_ROOT, "config", "automation_rules.json"), "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    actions = raw[obs._TRACKED_RULE_ID]["actions"]
    assert [a["type"] for a in actions] == ["automation.log"]


# ---------------------------------------------------------------------
# Real-bootstrap wiring proof (simulated events, real Event Bus/engine -
# never claimed as hardware evidence)
# ---------------------------------------------------------------------

def test_11_real_bootstrap_rule_status_reports_loaded_and_enabled(monkeypatch):
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

    runtime.start()
    try:
        engine = modules["automation_engine"]
        status = engine.get_automation_status(obs._TRACKED_RULE_ID)
        assert status is not None
        assert status["enabled"] is True
    finally:
        ShutdownCoordinator(runtime, adapter_manager).shutdown()


def test_12_real_bootstrap_simulated_human_detected_produces_matched_and_completed(monkeypatch):
    """Simulated event (never claimed as real hardware evidence) proving
    the observer's new automation-outcome tracking correctly attaches to
    the REAL engine/REAL Event Bus/REAL shipped rule end to end -
    matches Section 10's own worked example shape (triggered/matched/
    executed counted, not assumed equal to the raw event count)."""
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
    runtime.start()
    sub_ids = []
    try:
        for outcome in ("triggered", "condition_passed", "completed", "skipped", "failed"):
            sub_ids.append(runtime.event_bus.subscribe(f"automation.{outcome}", observer.on_automation_event(outcome)))
        sub_ids.append(runtime.event_bus.subscribe("tool_requested", observer.on_tool_requested))

        runtime.event_bus.publish(Event(type="camera_automation.camera_event", data={
            "camera_id": "tapo_c212", "kind": "human_detected", "entity_id": "vision:x",
            "old_state": None, "new_state": None, "confidence": None, "timestamp": time.time(), "source": "vision",
        }))

        deadline = time.time() + 3.0
        while time.time() < deadline and observer.automation_event_counts.get("completed", 0) < 1:
            time.sleep(0.02)

        for s in sub_ids:
            runtime.event_bus.unsubscribe(s)
    finally:
        ShutdownCoordinator(runtime, adapter_manager).shutdown()

    assert observer.automation_event_counts.get("triggered") == 1
    assert observer.automation_event_counts.get("condition_passed") == 1
    assert observer.automation_event_counts.get("completed") == 1
    assert observer.automation_event_counts.get("skipped", 0) == 0
    # P0.6.2 update (superseded by the P0.8.6 update below): the REAL
    # shipped config/automation_rules.json this test loads by default
    # ALSO contains P0.6.2's own real-device rule
    # (camera_human_detected_test_action), which - AS OF P0.6.2 - issued
    # its own tool_requested for the SAME human_detected event this test
    # publishes.
    #
    # P0.8.6 UPDATE (documented, intentional - see docs/change_impact/
    # camera_automation_p0_8_6.md Section 3): camera_human_detected_
    # test_action now requires event.kind == "human_confirmed" (P0.8.6's
    # own new, stricter, sustained-detection-only signal - a single raw
    # human_detected frame must never directly turn on a real physical
    # light, the exact bug P0.8.6 fixes) PLUS event.available == true /
    # event.detection_error == null. The raw event this test publishes
    # is `kind="human_detected"` with neither `available` nor
    # `detection_error` set at all - so as of P0.8.6 that rule's own
    # conditions now correctly refuse to match (fail-closed on the
    # missing/wrong fields), and it contributes ZERO tool_requested
    # calls here. Combined with the LOG-ONLY rule tracked here
    # (_TRACKED_RULE_ID) also contributing zero (test_09's own
    # structural proof - automation.log can never reach
    # _dispatch_tool_call), the fully-attributable total is now zero,
    # not one. camera_human_detected_test_action's OWN "does it still
    # fire on a genuinely confirmed event" behavior remains covered by
    # tests/test_p0_6_2_camera_ha_action.py's own (P0.8.6-updated)
    # test_18.
    assert observer.tool_requested_count == 0


def test_13_real_bootstrap_simulated_human_cleared_is_skipped_not_matched(monkeypatch):
    """Section 11's negative test - human_cleared must not match the
    rule (condition is event.kind == 'human_detected' only)."""
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
    runtime.start()
    sub_ids = []
    try:
        for outcome in ("triggered", "condition_passed", "completed", "skipped", "failed"):
            sub_ids.append(runtime.event_bus.subscribe(f"automation.{outcome}", observer.on_automation_event(outcome)))

        for kind in ("human_cleared", "camera_online", "camera_offline"):
            runtime.event_bus.publish(Event(type="camera_automation.camera_event", data={
                "camera_id": "tapo_c212", "kind": kind, "entity_id": f"vision:{kind}",
                "old_state": None, "new_state": None, "confidence": None, "timestamp": time.time(), "source": "vision",
            }))

        deadline = time.time() + 3.0
        while time.time() < deadline and observer.automation_event_counts.get("skipped", 0) < 3:
            time.sleep(0.02)

        for s in sub_ids:
            runtime.event_bus.unsubscribe(s)
    finally:
        ShutdownCoordinator(runtime, adapter_manager).shutdown()

    assert observer.automation_event_counts.get("skipped") == 3
    assert observer.automation_event_counts.get("completed", 0) == 0


# ---------------------------------------------------------------------
# Safety / diff-relevant static checks (same convention as the P0.5.4-
# LIVE test file's own Section)
# ---------------------------------------------------------------------

def test_14_never_calls_a_write_or_control_method():
    """AST-based (not a plain text scan) - as of P0.6.2 the observer's
    own PRINT/DISPLAY strings legitimately contain "turn_on"/
    "call_service" as plain text (e.g. logging
    `action=home_assistant.turn_on`), so a bare substring scan would
    false-positive. What must hold, and what this proves, is that the
    script's own CODE never contains a call-shaped reference to any of
    these names."""
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


def test_15_never_writes_automation_rules_json_or_camera_automation_json():
    import inspect
    source = _code_only(inspect.getsource(obs))
    assert "automation_rules.json" not in source or "open(" not in source
    assert "camera_automation.json" not in source
    import re
    write_mode_opens = re.findall(r"""open\([^)]*['"]\s*[wa]\+?['"]""", inspect.getsource(obs))
    assert not write_mode_opens, f"found file-write call(s): {write_mode_opens}"
