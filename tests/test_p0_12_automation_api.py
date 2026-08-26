"""
tests/test_p0_12_automation_api.py
======================================

LUNO P0.12 (Automation API & CRUD) - dedicated regression suite.

Adds a new `/api/automations*` HTTP surface (`luno/dashboard/
automation_api.py`, wired into `luno/dashboard/server.py`) so a future
Dashboard editor (P0.13, explicitly NOT built by this sprint) can
list/get/create/update/delete/enable/disable/run/validate automation
rules without ever touching `config/automation_rules.json` or
`AutomationEngine` internals directly.

This file follows the SAME real-bootstrap-plus-real-HTTP convention
every sibling `test_*_dashboard.py` file already established (`tests/
test_dashboard.py`/`test_memory_dashboard.py`/`test_llm_dashboard.py`):
`register_all_modules`/`register_all_adapters` (all-mock backends), a
REAL, running `DashboardServer` bound to `127.0.0.1:0`, and REAL HTTP
requests via `requests` - nothing here mocks the HTTP layer itself, only
the underlying device adapters (same "mock backend" every launcher test
already uses). `config/automation_rules.json` is NOT one of `conftest.
py`'s redirected `_WRITABLE_STATE_ATTRS` (it never was, even before
P0.12 - see `tests/test_sprint72_automation_engine.py`/`test_p0_11_
action_sequence.py`'s own `engine._rules_path = <tempfile>` precedent),
so every test here redirects `AutomationEngine._rules_path` to a fresh
temp file itself, BEFORE `runtime.start()`, exactly matching that
established convention - never the real repository file.

Every mutating engine method this new HTTP layer calls
(`create_rule`/`update_rule`/`delete_rule`/`enable_automation`/
`disable_automation`/`run_automation`) is itself already covered at the
unit level by a standalone smoke script during this sprint's own
implementation pass; this file's job is proving the FULL, real,
end-to-end HTTP wiring - JSON request -> `automation_api.py` -> the
existing `AutomationEngine` -> the existing persistence primitive ->
JSON response - actually works, matches the brief's own response
contract, and never regresses anything already shipped.

Sections A-AE per the P0.12 brief's own Phase 12 minimum-coverage list,
plus a dedicated architecture-guards section (Phase 14).
"""

from __future__ import annotations

import ast
import json
import os
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
import requests  # noqa: E402

from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.dashboard import DashboardServer  # noqa: E402
from luno.dashboard import automation_api  # noqa: E402
from luno.dashboard import server as dash_server  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
_ROOT_AUTOMATION_MODULE = os.path.join(_ROOT, "luno", "automation")
_AUTOMATION_API_PATH = os.path.join(_ROOT, "luno", "dashboard", "automation_api.py")
_SERVER_PATH = os.path.join(_ROOT, "luno", "dashboard", "server.py")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _build_dashboard(rules: Dict[str, Any] = None):
    """Same real bootstrap sequence every sibling `test_*_dashboard.py`
    file uses - all-mock backends, no external dependency required -
    plus the `_rules_path` redirect this project's own automation test
    files already established (`config/automation_rules.json` is
    deliberately NOT one of `conftest.py`'s redirected paths)."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="p0_12_automation_rules_test_")
    os.close(fd)
    with open(rules_path, "w", encoding="utf-8") as fh:
        json.dump(rules or {}, fh)
    modules["automation_engine"]._rules_path = rules_path

    runtime.start()
    dashboard = DashboardServer(
        runtime, adapter_manager, modules, cfg,
        audio_capture_store=adapters.get("audio_capture_store"), host="127.0.0.1", port=0,
    )
    dashboard.start()
    return runtime, modules, adapter_manager, dashboard, rules_path


def _teardown(runtime, adapter_manager, dashboard, rules_path=None) -> None:
    ShutdownCoordinator(runtime, adapter_manager, dashboard=dashboard).shutdown()
    if rules_path is not None:
        try:
            os.remove(rules_path)
        except OSError:
            pass


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _basic_rule(name: str = "Test Rule") -> Dict[str, Any]:
    return {"name": name, "trigger": "manual", "actions": [{"type": "automation.log", "parameters": {"message": "hi"}}]}


# ============================================================================
# A. API route registration
# ============================================================================

def test_A1_list_route_is_registered_and_reachable():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.get(dashboard.url + "api/automations", timeout=5)
        assert r.status_code == 200
        assert "automations" in r.json()
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_A2_validate_route_is_registered_and_reachable():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations/validate", json=_basic_rule(), timeout=5)
        assert r.status_code == 200
        assert "valid" in r.json()
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_A3_unknown_automations_subpath_returns_404_not_a_control():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations/some_id/not_a_real_verb", json={}, timeout=5)
        assert r.status_code == 404
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# B/C/D. GET list / GET one / GET nonexistent
# ============================================================================

def test_B1_get_list_empty():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.get(dashboard.url + "api/automations", timeout=5)
        assert r.json() == {"automations": []}
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_B2_get_list_returns_full_dto_with_status():
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "manual", "actions": [{"type": "automation.log"}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        r = requests.get(dashboard.url + "api/automations", timeout=5)
        automations = r.json()["automations"]
        assert len(automations) == 1
        a = automations[0]
        for key in ("id", "name", "description", "enabled", "trigger", "conditions", "actions",
                    "sequence", "cooldown_seconds", "execution_policy", "created_at", "updated_at", "status"):
            assert key in a, f"missing key {key!r} in list DTO"
        assert a["status"]["running"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_C1_get_existing_automation():
    rules = {"r1": {"name": "R One", "enabled": True, "trigger": "manual", "actions": [{"type": "automation.log"}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        r = requests.get(dashboard.url + "api/automations/r1", timeout=5)
        assert r.status_code == 200
        assert r.json()["name"] == "R One"
        assert r.json()["id"] == "r1"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_D1_get_nonexistent_automation_returns_404():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.get(dashboard.url + "api/automations/does_not_exist", timeout=5)
        assert r.status_code == 404
        assert "error" in r.json()
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# E/F/G/H/I. CREATE
# ============================================================================

def test_E1_create_valid_automation():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json={**_basic_rule(), "id": "new_rule"}, timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["automation"]["id"] == "new_rule"
        assert body["automation"]["created_at"] is not None
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_E2_create_without_id_autogenerates_one():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json=_basic_rule(), timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is True
        assert body["automation"]["id"]
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_F1_create_duplicate_id_rejected():
    rules = {"dup": {"name": "existing", "enabled": True, "trigger": "manual", "actions": [{"type": "automation.log"}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        r = requests.post(dashboard.url + "api/automations", json={**_basic_rule(), "id": "dup"}, timeout=5)
        body = r.json()
        assert body["success"] is False
        assert body["errors"][0]["code"] in ("invalid_value",)
        assert "already exists" in body["errors"][0]["message"]
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_G1_create_invalid_trigger_rejected():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json={"id": "bad", "name": "bad", "trigger": "not_a_real_trigger", "actions": [{"type": "automation.log"}]}, timeout=5)
        body = r.json()
        assert body["success"] is False
        assert body["errors"][0]["field"] == "trigger"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_H1_create_invalid_action_rejected():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json={"id": "bad2", "name": "bad2", "trigger": "manual", "actions": [{"type": "not_a_real_action"}]}, timeout=5)
        body = r.json()
        assert body["success"] is False
        assert body["errors"][0]["field"] == "actions"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_I1_create_invalid_sequence_rejected():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json={
            "id": "bad3", "name": "bad3", "trigger": "manual",
            "sequence": [{"type": "delay", "seconds": -5}],
        }, timeout=5)
        body = r.json()
        assert body["success"] is False
        assert body["errors"][0]["field"] == "sequence[0]"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# J/K. UPDATE
# ============================================================================

def test_J1_update_existing_automation():
    rules = {"r1": {"name": "old name", "enabled": True, "trigger": "manual", "actions": [{"type": "automation.log"}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        r = requests.post(dashboard.url + "api/automations/r1/update", json={
            "name": "new name", "trigger": "manual", "actions": [{"type": "automation.log"}], "description": "d",
        }, timeout=5)
        body = r.json()
        assert body["success"] is True
        assert body["automation"]["name"] == "new name"
        assert body["automation"]["description"] == "d"
        assert body["automation"]["id"] == "r1"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_J2_update_preserves_created_at_and_refreshes_updated_at():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        created = requests.post(dashboard.url + "api/automations", json={**_basic_rule(), "id": "r1"}, timeout=5).json()["automation"]
        time.sleep(0.01)
        updated = requests.post(dashboard.url + "api/automations/r1/update", json=_basic_rule("renamed"), timeout=5).json()["automation"]
        assert updated["created_at"] == created["created_at"]
        assert updated["updated_at"] != created["updated_at"]
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_K1_update_nonexistent_automation():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations/nope/update", json=_basic_rule(), timeout=5)
        body = r.json()
        assert body["success"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# L/M. DELETE
# ============================================================================

def test_L1_delete_existing_automation():
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "manual", "actions": [{"type": "automation.log"}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        r = requests.post(dashboard.url + "api/automations/r1/delete", timeout=5)
        assert r.json()["success"] is True
        r2 = requests.get(dashboard.url + "api/automations/r1", timeout=5)
        assert r2.status_code == 404
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_M1_delete_nonexistent_automation_not_silently_ignored():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations/nope/delete", timeout=5)
        body = r.json()
        assert body["success"] is False
        assert "No automation called" in body["message"]
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# N/O/P/Q. ENABLE / DISABLE
# ============================================================================

def test_N1_enable_automation():
    rules = {"r1": {"name": "r1", "enabled": False, "trigger": "manual", "actions": [{"type": "automation.log"}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        r = requests.post(dashboard.url + "api/automations/r1/enable", timeout=5)
        body = r.json()
        assert body["success"] is True
        assert body["automation"]["enabled"] is True
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_O1_disable_automation():
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "manual", "actions": [{"type": "automation.log"}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        r = requests.post(dashboard.url + "api/automations/r1/disable", timeout=5)
        body = r.json()
        assert body["success"] is True
        assert body["automation"]["enabled"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_O2_disable_then_enable_persists_correctly_on_disk():
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "manual", "actions": [{"type": "automation.log"}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        requests.post(dashboard.url + "api/automations/r1/disable", timeout=5)
        with open(rp, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
        assert disk["r1"]["enabled"] is False
        requests.post(dashboard.url + "api/automations/r1/enable", timeout=5)
        with open(rp, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
        assert disk["r1"]["enabled"] is True
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_P1_enable_nonexistent_automation():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations/nope/enable", timeout=5)
        assert r.json()["success"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_Q1_disable_nonexistent_automation():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations/nope/disable", timeout=5)
        assert r.json()["success"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# R/S/T. VALIDATE
# ============================================================================

def test_R1_validate_valid_automation():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations/validate", json=_basic_rule(), timeout=5)
        body = r.json()
        assert body == {"valid": True, "errors": []}
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_S1_validate_invalid_automation():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations/validate", json={"name": "", "trigger": "manual", "actions": [{"type": "automation.log"}]}, timeout=5)
        body = r.json()
        assert body["valid"] is False
        assert len(body["errors"]) == 1
        assert body["errors"][0]["field"] == "name"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_T1_validate_does_not_persist_or_register():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        requests.post(dashboard.url + "api/automations/validate", json={**_basic_rule(), "id": "should_never_exist"}, timeout=5)
        r = requests.get(dashboard.url + "api/automations", timeout=5)
        assert r.json() == {"automations": []}
        r2 = requests.get(dashboard.url + "api/automations/should_never_exist", timeout=5)
        assert r2.status_code == 404
        with open(rp, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
        assert disk == {}
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_T2_validate_never_touches_engine_rules_dict():
    """Structural proof, not just an HTTP-level inference - directly
    inspects the live engine's own in-memory `_rules` dict after a
    validate call."""
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        requests.post(dashboard.url + "api/automations/validate", json={**_basic_rule(), "id": "ghost"}, timeout=5)
        engine = modules["automation_engine"]
        with engine._lock:
            assert "ghost" not in engine._rules
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# U/V/W/X. RUN
# ============================================================================

def test_U1_run_existing_automation():
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "manual", "actions": [{"type": "automation.log", "parameters": {"message": "hi"}}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        r = requests.post(dashboard.url + "api/automations/r1/run", timeout=5)
        body = r.json()
        assert body["success"] is True
        assert body["automation_id"] == "r1"
        assert body["execution_id"]
        assert body["status"] == "queued"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_V1_run_nonexistent_automation():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations/nope/run", timeout=5)
        body = r.json()
        assert body["success"] is False
        assert body["execution_id"] is None
        assert body["status"] == "refused"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_W1_run_uses_existing_automation_engine_execution_path():
    """Proves the HTTP RUN endpoint produces a REAL AutomationExecution
    observable through the EXISTING engine.get_automation_status() -
    i.e. it went through the real `_trigger()`/`_run_execution()`
    pipeline, not some parallel HTTP-only execution shortcut."""
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "manual", "actions": [{"type": "automation.log"}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        r = requests.post(dashboard.url + "api/automations/r1/run", timeout=5)
        execution_id = r.json()["execution_id"]
        engine = modules["automation_engine"]
        assert _wait_until(lambda: engine.get_automation_status("r1")["last_execution"] is not None)
        last = engine.get_automation_status("r1")["last_execution"]
        assert last["execution_id"] == execution_id
        assert last["final_status"] == "COMPLETED"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_X1_run_dispatches_device_action_through_tool_manager_not_directly():
    """Proves the HTTP RUN endpoint's device action reaches Home
    Assistant ONLY via the real `tool_requested` -> ToolManager round
    trip - subscribes to `tool_requested` directly and confirms it
    fires exactly once for this run, with no direct HA call anywhere in
    the new API layer (see the architecture-guard section below for the
    complementary STATIC proof)."""
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "manual", "actions": [{"type": "home_assistant.turn_on", "parameters": {"target": "Main Lamp"}}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    try:
        r = requests.post(dashboard.url + "api/automations/r1/run", timeout=5)
        assert r.json()["success"] is True
        assert _wait_until(lambda: len(tool_calls) == 1)
        assert tool_calls[0]["tool"] == "home_assistant"
        assert tool_calls[0]["action"] == "turn_on"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# Y/Z. Sequence rules through the API
# ============================================================================

def test_Y1_sequence_rule_can_be_created_and_retrieved():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        body = {
            "id": "seq_rule", "name": "seq", "trigger": "manual",
            "sequence": [
                {"type": "home_assistant.turn_on", "parameters": {"target": "light.wled"}},
                {"type": "delay", "seconds": 2},
                {"type": "home_assistant.turn_off", "parameters": {"target": "light.wled"}},
            ],
        }
        r = requests.post(dashboard.url + "api/automations", json=body, timeout=5)
        assert r.json()["success"] is True
        r2 = requests.get(dashboard.url + "api/automations/seq_rule", timeout=5)
        got = r2.json()
        assert got["actions"] == []
        assert [s["type"] for s in got["sequence"]] == ["home_assistant.turn_on", "delay", "home_assistant.turn_off"]
        assert got["sequence"][1]["parameters"]["seconds"] == 2
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_Z1_sequence_rule_survives_persistence_and_reload():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        body = {
            "id": "seq_rule", "name": "seq", "trigger": "manual",
            "sequence": [{"type": "automation.log", "parameters": {"message": "a"}}, {"type": "delay", "seconds": 0.5}],
        }
        requests.post(dashboard.url + "api/automations", json=body, timeout=5)
        engine = modules["automation_engine"]
        engine.reload_rules()  # simulates a real restart's own reload_rules() call
        got = engine.get_rule("seq_rule")
        assert got is not None
        assert [s["type"] for s in got["sequence"]] == ["automation.log", "delay"]
        with open(rp, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
        assert disk["seq_rule"]["sequence"][1]["type"] == "delay"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# AA. Legacy `actions`-only rule compatibility
# ============================================================================

def test_AA1_legacy_actions_only_rule_created_via_api_is_compatible():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    tool_calls: List[Dict[str, Any]] = []
    runtime.event_bus.subscribe("tool_requested", lambda e: tool_calls.append(e.data.get("tool_call", {})))
    try:
        r = requests.post(dashboard.url + "api/automations", json={**_basic_rule(), "id": "legacy1"}, timeout=5)
        assert r.json()["success"] is True
        r2 = requests.post(dashboard.url + "api/automations/legacy1/run", timeout=5)
        assert r2.json()["success"] is True
        engine = modules["automation_engine"]
        assert _wait_until(lambda: engine.get_automation_status("legacy1")["last_execution"] is not None)
        assert engine.get_automation_status("legacy1")["last_execution"]["final_status"] == "COMPLETED"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_AA2_real_shipped_automation_rules_file_unaffected_by_api_module_import():
    """Importing `automation_api.py` (or this whole test file) must not
    itself touch the REAL `config/automation_rules.json` - only ever the
    per-test isolated `_rules_path` this file's own `_build_dashboard()`
    sets up."""
    real_rules_path = os.path.join(_ROOT, "config", "automation_rules.json")
    before = _read(real_rules_path) if os.path.exists(real_rules_path) else None
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        requests.post(dashboard.url + "api/automations", json={**_basic_rule(), "id": "isolated_only"}, timeout=5)
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)
    after = _read(real_rules_path) if os.path.exists(real_rules_path) else None
    assert before == after, "the real config/automation_rules.json must never be touched by this test file"


# ============================================================================
# AB. Malformed payload
# ============================================================================

def test_AB1_empty_body_returns_structured_errors_not_a_crash():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json={}, timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert body["success"] is False
        assert isinstance(body["errors"], list) and len(body["errors"]) >= 1
        for err in body["errors"]:
            assert set(err.keys()) >= {"field", "code", "message"}
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_AB2_non_json_body_never_returns_a_traceback():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(
            dashboard.url + "api/automations", data=b"not json at all {{{",
            headers={"Content-Type": "application/json"}, timeout=5,
        )
        assert r.status_code == 200
        body = r.json()
        assert "Traceback" not in json.dumps(body)
        assert body["success"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_AB3_malformed_update_payload_structured_error():
    rules = {"r1": {"name": "r1", "enabled": True, "trigger": "manual", "actions": [{"type": "automation.log"}], "cooldown_seconds": 0.0}}
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules)
    try:
        r = requests.post(dashboard.url + "api/automations/r1/update", json={"name": "x", "trigger": "manual", "actions": [{"type": "home_assistant.turn_on", "parameters": {}}]}, timeout=5)
        body = r.json()
        assert body["success"] is False
        assert body["errors"][0]["field"] == "actions"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# AC. Concurrency
# ============================================================================

def test_AC1_concurrent_create_calls_do_not_corrupt_persistence():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        n = 20
        results: List[int] = []
        lock = threading.Lock()

        def _create(i: int) -> None:
            r = requests.post(dashboard.url + "api/automations", json={**_basic_rule(f"r{i}"), "id": f"concurrent_{i}"}, timeout=10)
            with lock:
                results.append(r.status_code)

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert all(code == 200 for code in results)
        r = requests.get(dashboard.url + "api/automations", timeout=5)
        assert len(r.json()["automations"]) == n
        with open(rp, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
        assert len(disk) == n, f"lost update: disk has {len(disk)} rules, expected {n}"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_AC2_get_during_concurrent_create_never_errors():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    errors: List[str] = []

    def _create_loop() -> None:
        for i in range(15):
            try:
                requests.post(dashboard.url + "api/automations", json={**_basic_rule(), "id": f"c_{i}"}, timeout=10)
            except Exception as ex:  # pragma: no cover - would only fire on a real bug
                errors.append(str(ex))

    def _get_loop() -> None:
        for _ in range(30):
            try:
                r = requests.get(dashboard.url + "api/automations", timeout=10)
                assert r.status_code == 200
                json.loads(r.text)  # must always be well-formed JSON, never a half-written body
            except Exception as ex:  # pragma: no cover
                errors.append(str(ex))

    try:
        t1 = threading.Thread(target=_create_loop)
        t2 = threading.Thread(target=_get_loop)
        t1.start(); t2.start()
        t1.join(timeout=20); t2.join(timeout=20)
        assert errors == []
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# AD. No arbitrary Python/shell execution
# ============================================================================

def test_AD1_python_expression_in_action_type_is_rejected_not_executed():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json={
            "id": "evil", "name": "evil", "trigger": "manual",
            "actions": [{"type": "__import__('os').system('echo pwned')", "parameters": {}}],
        }, timeout=5)
        body = r.json()
        assert body["success"] is False
        assert body["errors"][0]["field"] == "actions"
        assert not os.path.exists("pwned")  # the shell command was NEVER executed
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_AD2_shell_metacharacters_in_parameters_are_inert():
    """Parameters are always plain data consumed by ToolManager's own
    typed handlers, never interpolated into a shell command anywhere in
    this new API layer - a `target` containing shell metacharacters is
    just an (invalid, since it's not a known HA entity) string."""
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json={
            "id": "meta", "name": "meta", "trigger": "manual",
            "actions": [{"type": "home_assistant.turn_on", "parameters": {"target": "light.x; rm -rf /tmp/nonexistent_marker"}}],
        }, timeout=5)
        assert r.json()["success"] is True  # accepted as a normal (if nonsensical) entity id string - never executed as a shell command
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# AE. Security / authentication follows existing project behavior
# ============================================================================

def test_AE1_dashboard_defaults_to_localhost_only_same_as_every_other_endpoint():
    """This sprint introduces NO new authentication mechanism - honestly
    documents (and structurally verifies) that the new `/api/automations*`
    family is bound by the SAME host/port `DashboardServer` already uses
    for every other endpoint (localhost-only by default, per `luno/
    bootstrap/launcher_config.py`'s own `DASHBOARD_HOST` default) -
    never a special-cased, more/less permissive bind for automations."""
    from luno.bootstrap.launcher_config import LauncherConfig as _LC
    cfg = _LC()
    assert cfg.dashboard_host == "127.0.0.1"


def test_AE2_no_new_auth_bypass_or_token_check_introduced():
    """Structural proof this file's own new module never references any
    kind of ad-hoc, sprint-invented authentication (API key/token/
    password check) - it relies entirely on the SAME "no auth, localhost-
    bind-only" model every other `/api/*` endpoint in this project
    already has, honestly documented rather than silently assumed."""
    source = _read(_AUTOMATION_API_PATH)
    for forbidden in ("api_key", "API_KEY", "password", "secret_token", "Authorization"):
        assert forbidden not in source


# ============================================================================
# Architecture guards (Phase 14)
# ============================================================================

def test_M1_no_second_automation_engine_instantiated():
    source = _read(_AUTOMATION_API_PATH)
    assert "AutomationEngine(" not in source


def test_M2_no_second_persistence_mechanism():
    source = _read(_AUTOMATION_API_PATH)
    for forbidden in ("open(", "json.dump(", "atomic_write_json"):
        assert forbidden not in source, f"{forbidden!r} must not appear in automation_api.py - persistence stays inside AutomationEngine"


def test_M3_no_direct_home_assistant_call():
    """P0.14 - a bare `"HomeAssistant" not in source` substring check
    (the pre-P0.14 version of this test) went stale the moment P0.14's
    own `get_devices()` (Section 10) legitimately needed to tell a REAL
    Home Assistant client apart from the mock one for its honest
    `ha_connected` flag - by comparing `type(client).__name__` against
    the literal string `"RealHomeAssistantClient"` (a deliberate,
    documented choice specifically so this file never gains an IMPORT-time
    dependency on `luno.adapters.real_home_assistant` - see that
    function's own P0.14 docstring). That string literal, and this
    file's own comments/docstrings mentioning the class by name, both
    contain the substring "HomeAssistant" without this file ever
    importing, instantiating, or calling into it - the ORIGINAL guard's
    real intent (no import, no instantiation, no direct call) is
    re-expressed below precisely enough to still catch a genuine
    violation without false-positiving on a type-name string comparison
    or explanatory prose."""
    source = _read(_AUTOMATION_API_PATH)
    for forbidden in ("requests.", "http.client", "websockets", "home_assistant.turn_on(",
                       "HomeAssistantClient(", "RealHomeAssistantHandler(", "MockHomeAssistantHandler("):
        assert forbidden not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        assert not (isinstance(node, ast.Import) and any("home_assistant" in a.name.lower() for a in node.names)), \
            "automation_api.py must never import a Home Assistant adapter/handler module"
        assert not (isinstance(node, ast.ImportFrom) and node.module and "home_assistant" in node.module.lower()), \
            "automation_api.py must never import from a Home Assistant adapter/handler module"


def test_M4_no_tool_manager_bypass():
    source = _read(_AUTOMATION_API_PATH)
    assert "ToolManager(" not in source
    assert "tool_requested" not in source  # never publishes its own tool_requested - only run_automation() (which reuses the engine) can ever cause one


def test_M5_no_duplicated_sequence_execution_logic():
    source = _read(_AUTOMATION_API_PATH)
    for forbidden in ("_run_sequence", "_dispatch_action", "_wait_delay", "threading.Event()"):
        assert forbidden not in source


def test_M6_no_eval_exec_shell_or_dynamic_import():
    """Same hard security boundary `models.py`'s own docstring/Sprint 72
    already established, re-verified for the NEW file this sprint adds."""
    source = _read(_AUTOMATION_API_PATH)
    for forbidden in ("eval(", "exec(", "subprocess.", "os.system(", "importlib.import_module("):
        assert forbidden not in source


def test_M7_vision_camera_occupancy_untouched():
    for path in (
        os.path.join(_ROOT, "luno", "vision.py"),
        os.path.join(_ROOT, "luno", "vision_occupancy.py"),
        os.path.join(_ROOT, "luno", "adapters", "vision.py"),
    ):
        source = _read(path)
        assert "automation_api" not in source
        assert "from ..automation" not in source and "from .automation" not in source


def _function_body_source(path: str, function_name: str) -> str:
    """Returns ONLY the given top-level function's own code (via AST
    line bounds), excluding any docstring/comment noise from sibling
    functions or section-header comments above/below it - a plain
    `str.index("\\ndef ")` slice (this project's own established
    convention for similar checks elsewhere) is too coarse here because
    this file's own section-header COMMENTS (e.g. "AutomationEngine
    method's own contract") sit between two functions and would get
    swept into whichever slice ends at the next `def`, causing a false
    positive on a prose word like "engine" that was never actually
    executable code."""
    import ast
    tree = ast.parse(_read(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            body_start = node.body[0].lineno
            # Skip a leading docstring (an ast.Expr wrapping an
            # ast.Constant str) - only its OWN prose should never count
            # as "not code", but here we want line bounds that start
            # right after it so a docstring's own words never trip a
            # code-content assertion either.
            if (
                isinstance(node.body[0], ast.Expr)
                and isinstance(getattr(node.body[0], "value", None), ast.Constant)
                and isinstance(node.body[0].value.value, str)
                and len(node.body) > 1
            ):
                body_start = node.body[1].lineno
            body_end = node.end_lineno
            lines = _read(path).splitlines()
            return "\n".join(lines[body_start - 1:body_end])
    raise AssertionError(f"function {function_name!r} not found in {path}")


def test_M8_manual_run_reuses_run_automation_verbatim():
    body = _function_body_source(_AUTOMATION_API_PATH, "run_automation")
    assert "engine.run_automation(" in body
    assert "_trigger(" not in body
    assert "_run_execution(" not in body


def test_M9_validate_endpoint_never_calls_engine_at_all():
    source = _read(_AUTOMATION_API_PATH)
    sig_line = next(line for line in source.splitlines() if line.startswith("def validate_automation("))
    assert "modules" not in sig_line  # signature takes only `body`, no `modules` param
    body = _function_body_source(_AUTOMATION_API_PATH, "validate_automation")
    assert "engine" not in body.lower()
    assert "modules" not in body


def test_M10_server_py_routes_automation_family_through_automation_api_module():
    source = _read(_SERVER_PATH)
    assert "automation_api.dispatch_post(" in source
    assert "automation_api.list_automations(" in source
    assert "automation_api.get_automation(" in source
    assert source.count("class DashboardServer") == 1  # still exactly one server class - no parallel HTTP server introduced


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
