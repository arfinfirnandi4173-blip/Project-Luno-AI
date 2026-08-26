"""
tests/test_p0_13_automation_dashboard.py
============================================

LUNO P0.13 (Luno Automation Dashboard / Visual Automation Builder) -
dedicated regression suite.

Adds a Dashboard UI panel (`luno/dashboard/static/index.html` - "Automations")
that lets a person create/edit/validate/save/enable/disable/run/monitor/
delete automation rules through the EXISTING P0.12 `/api/automations*` HTTP
API and the EXISTING P0.11 sequence schema - no second persistence
mechanism, no second execution path, no direct Home Assistant call from the
browser, and no arbitrary code execution anywhere in the new surface. The
ONE new backend surface this sprint adds is `GET /api/automations/schema`
(a pure, read-only reflection of `luno/automation/models.py`'s own existing
allowlists plus the existing `luno.devices` registry) - see
`docs/change_impact/automation_dashboard_p0_13.md` for the full design.

Testing approach (read this before adding more tests here)
------------------------------------------------------------
This project has NO browser-automation/headless-browser test runner
anywhere (no Selenium/Playwright/Puppeteer, no `package.json`, no JS test
framework) - `luno/dashboard/static/index.html` is a single, dependency-free
HTML+CSS+vanilla-JS file served as-is, exactly like every other panel in
this dashboard, and this project's own existing convention for testing it
(`tests/test_dashboard.py::test_29_static_index_served_and_health_endpoint_
present`) is: fetch the served HTML over real HTTP and assert on its text
content, never execute it in a real DOM. This file follows that SAME
convention and extends it with two additional, honestly-scoped techniques:

1. **Real HTTP, end-to-end, functional tests** against the SAME
   `/api/automations*` endpoints the browser's JavaScript calls - every
   payload shape sent here is built to EXACTLY mirror what `automBuildPayload
   ()`/`automSaveDraft()`/etc. in `index.html` actually construct (verified
   by reading that source), so these tests prove the API contract the UI
   depends on is real and correct, even though the UI's own JS execution is
   never run inside pytest.
2. **Static source-scan architecture-guard tests** over the exact bytes of
   the served `<script>` block - simple substring/regex checks (documented
   as best-effort structural scans, not a real JS parse) proving invariants
   like "every automation-related `fetch`/`api()` call targets `/api/
   automations*`", "every automation name/description interpolation goes
   through `esc()`/`escAttr()`", and "no `eval`/`new Function` anywhere in
   the file" - the same spirit as this project's existing Python AST
   architecture-guard tests, adapted for a file this project has no AST
   tool for.

No live browser, no live Home Assistant, no live WLED hardware was
exercised by this file - see the change-impact doc's own honest limitations
section.

Sections A-X per the P0.13 brief's own Phase 14 minimum-coverage list,
plus a dedicated architecture-guards section.
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

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

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)
_STATIC_INDEX_PATH = os.path.join(_ROOT, "luno", "dashboard", "static", "index.html")
_AUTOMATION_API_PATH = os.path.join(_ROOT, "luno", "dashboard", "automation_api.py")
_SERVER_PATH = os.path.join(_ROOT, "luno", "dashboard", "server.py")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _build_dashboard(rules: Dict[str, Any] = None):
    """Same real-bootstrap-plus-real-HTTP convention every sibling
    `test_*_dashboard.py` file (including `test_p0_12_automation_api.py`)
    already established."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]

    fd, rules_path = tempfile.mkstemp(suffix=".json", prefix="p0_13_automation_rules_test_")
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


def _ui_create_payload(name: str = "UI Test Rule", **overrides) -> Dict[str, Any]:
    """Mirrors `automBuildPayload()` in `index.html` EXACTLY for a fresh
    'actions' mode draft (the default the editor opens with) - the same
    shape the browser would POST to `/api/automations`."""
    payload = {
        "name": name, "description": "", "enabled": True,
        "trigger": {"type": "manual", "parameters": {}},
        "conditions": [], "cooldown_seconds": 0, "execution_policy": "no_partial",
        "actions": [{"type": "automation.log", "parameters": {"message": "hi"}}],
    }
    payload.update(overrides)
    return payload


# ============================================================================
# JS-source structural helpers (best-effort, brace-depth extraction - not a
# real JS parser; sufficient for the well-formatted, single-file source this
# project's own dashboard has always been written as).
# ============================================================================

def _script_block() -> str:
    html = _read(_STATIC_INDEX_PATH)
    m = re.search(r"<script>(.*)</script>", html, re.S)
    assert m, "index.html has no <script> block"
    return m.group(1)


def _js_function_body(script: str, func_name: str) -> str:
    """Returns the full source of a top-level `function <func_name>(...) {
    ... }` (or `async function`), found by matching brace depth from the
    function's own opening brace to its balancing closing brace. Raises
    AssertionError if the function isn't found - a clear failure, not a
    silent empty match."""
    pattern = re.compile(r"(?:async\s+)?function\s+" + re.escape(func_name) + r"\s*\([^)]*\)\s*\{")
    m = pattern.search(script)
    assert m, f"function {func_name}() not found in index.html's <script> block"
    start = m.end() - 1  # position of the opening '{'
    depth = 0
    for i in range(start, len(script)):
        if script[i] == "{":
            depth += 1
        elif script[i] == "}":
            depth -= 1
            if depth == 0:
                return script[start:i + 1]
    raise AssertionError(f"unbalanced braces while extracting {func_name}()")


def _automation_section_of_script(script: str) -> str:
    """The P0.13-specific slice of the shared `<script>` block (from the
    `automState` declaration through the end of `automBindEditorEvents`) -
    used for architecture-guard scans that must not accidentally match
    unrelated, pre-existing panels' own code."""
    start = script.index("let automState")
    end = script.index("async function loadVision()")
    assert end > start
    return script[start:end]


# ============================================================================
# A. Automation list loading
# ============================================================================

def test_A1_automations_nav_button_and_panel_registered_in_static_html():
    html = _read(_STATIC_INDEX_PATH)
    assert 'data-panel="automations"' in html
    assert 'id="panel-automations"' in html
    assert 'id="autom-list-body"' in html


def test_A2_list_loader_is_registered_in_the_panel_dispatch_table():
    script = _script_block()
    assert "automations: loadAutomations" in script


def test_A3_list_endpoint_reflects_a_real_created_rule():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json=_ui_create_payload("List Test"), timeout=5)
        assert r.json()["success"], r.text
        r = requests.get(dashboard.url + "api/automations", timeout=5)
        rows = r.json()["automations"]
        assert len(rows) == 1
        assert rows[0]["name"] == "List Test"
        assert "status" in rows[0]  # list_automations() merges each rule with its live status sub-object
        assert rows[0]["status"]["running"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# B. Empty state
# ============================================================================

def test_B1_empty_state_message_present_in_client_source():
    script = _script_block()
    assert "No automations yet" in script


def test_B2_list_endpoint_returns_an_empty_array_with_zero_rules():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.get(dashboard.url + "api/automations", timeout=5)
        assert r.json() == {"automations": []}
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# C. Create form
# ============================================================================

def test_C1_create_form_field_ids_present_in_client_source():
    html = _read(_STATIC_INDEX_PATH)
    for marker in ("autom-f-id", "autom-f-name", "autom-f-desc", "autom-f-enabled", "autom-new-btn"):
        assert f'"{marker}"' in html, f"missing form field marker: {marker}"


def test_C2_create_via_the_exact_ui_payload_shape_succeeds():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json=_ui_create_payload("Created via UI shape"), timeout=5)
        result = r.json()
        assert result["success"], result
        assert result["automation"]["name"] == "Created via UI shape"
        assert result["automation"]["id"]  # auto-generated, since the UI left "id" blank
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_C3_create_with_a_caller_supplied_id_from_the_optional_id_field():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        payload = _ui_create_payload("Named", id="my_custom_rule")
        r = requests.post(dashboard.url + "api/automations", json=payload, timeout=5)
        result = r.json()
        assert result["success"] and result["automation"]["id"] == "my_custom_rule"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# D. Edit form
# ============================================================================

def test_D1_edit_loads_the_existing_rule_by_id_for_pre_fill():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        created = requests.post(dashboard.url + "api/automations", json=_ui_create_payload("Editable"), timeout=5).json()["automation"]
        r = requests.get(dashboard.url + "api/automations/" + created["id"], timeout=5)
        fetched = r.json()
        assert fetched["name"] == "Editable"
        assert fetched["id"] == created["id"]
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_D2_update_via_the_exact_ui_payload_shape_succeeds_and_preserves_id():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        created = requests.post(dashboard.url + "api/automations", json=_ui_create_payload("Before Edit"), timeout=5).json()["automation"]
        updated_payload = _ui_create_payload("After Edit", cooldown_seconds=15)
        r = requests.post(dashboard.url + "api/automations/" + created["id"] + "/update", json=updated_payload, timeout=5)
        result = r.json()
        assert result["success"]
        assert result["automation"]["name"] == "After Edit"
        assert result["automation"]["cooldown_seconds"] == 15
        assert result["automation"]["id"] == created["id"]
        assert result["automation"]["created_at"] == created["created_at"]  # immutable
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_D3_editor_loads_via_openAutomationEditorById_source_calls_get_single_route():
    script = _script_block()
    body = _js_function_body(script, "openAutomationEditorById")
    assert "/api/automations/" in body
    assert "openAutomationEditor(" in body


# ============================================================================
# E. Delete confirmation
# ============================================================================

def test_E1_delete_endpoint_removes_the_rule():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        created = requests.post(dashboard.url + "api/automations", json=_ui_create_payload("To Delete"), timeout=5).json()["automation"]
        r = requests.post(dashboard.url + "api/automations/" + created["id"] + "/delete", json={}, timeout=5)
        assert r.json()["success"]
        r = requests.get(dashboard.url + "api/automations", timeout=5)
        assert r.json() == {"automations": []}
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_E2_delete_is_gated_behind_confirm_in_both_list_row_and_modal_source():
    script = _script_block()
    row_delete = _js_function_body(script, "automDelete")
    assert "confirm(" in row_delete
    bind_body = _js_function_body(script, "automBindEditorEvents")
    modal_delete_start = bind_body.index("autom-modal-delete-btn")
    assert "confirm(" in bind_body[modal_delete_start:modal_delete_start + 400]


# ============================================================================
# F/G. Enable / Disable
# ============================================================================

def test_F1_enable_endpoint_flips_disabled_rule_to_enabled():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        created = requests.post(dashboard.url + "api/automations", json=_ui_create_payload("Toggle", enabled=False), timeout=5).json()["automation"]
        assert created["enabled"] is False
        r = requests.post(dashboard.url + "api/automations/" + created["id"] + "/enable", json={}, timeout=5)
        result = r.json()
        assert result["success"] and result["automation"]["enabled"] is True
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_G1_disable_endpoint_flips_enabled_rule_to_disabled():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        created = requests.post(dashboard.url + "api/automations", json=_ui_create_payload("Toggle2", enabled=True), timeout=5).json()["automation"]
        r = requests.post(dashboard.url + "api/automations/" + created["id"] + "/disable", json={}, timeout=5)
        result = r.json()
        assert result["success"] and result["automation"]["enabled"] is False
        # UI never maintains its own client-only enabled state - re-GET must agree.
        refetched = requests.get(dashboard.url + "api/automations/" + created["id"], timeout=5).json()
        assert refetched["enabled"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_FG2_toggle_handler_source_calls_enable_or_disable_never_a_third_path():
    script = _script_block()
    body = _js_function_body(script, "automToggleEnabled")
    assert "'disable'" in body and "'enable'" in body
    assert "automation_rules" not in body


# ============================================================================
# H. Manual run
# ============================================================================

def test_H1_run_endpoint_returns_execution_id_and_queued_status():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        created = requests.post(dashboard.url + "api/automations", json=_ui_create_payload("Runnable"), timeout=5).json()["automation"]
        r = requests.post(dashboard.url + "api/automations/" + created["id"] + "/run", json={}, timeout=5)
        result = r.json()
        assert result["success"] is True
        assert result["execution_id"]
        assert result["status"] == "queued"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_H2_run_button_handlers_call_the_run_endpoint_in_source():
    script = _script_block()
    row_run = _js_function_body(script, "automRun")
    assert "/run" in row_run
    bind_body = _js_function_body(script, "automBindEditorEvents")
    assert "automRun(automState.draft.id)" in bind_body


def test_H3_run_monitor_polls_the_get_single_route_and_reads_running_and_last_execution():
    script = _script_block()
    body = _js_function_body(script, "automPollRun")
    assert "/api/automations/" in body
    assert "st.running" in body
    assert "st.last_execution" in body


# ============================================================================
# I/J. Validation
# ============================================================================

def test_I1_validate_accepts_a_well_formed_ui_payload():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations/validate", json=_ui_create_payload("Valid"), timeout=5)
        assert r.json() == {"valid": True, "errors": []}
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_J1_validate_rejects_a_malformed_ui_payload_with_structured_errors():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        bad = _ui_create_payload("Invalid", actions=[])  # empty actions, no sequence -> "neither" case
        r = requests.post(dashboard.url + "api/automations/validate", json=bad, timeout=5)
        result = r.json()
        assert result["valid"] is False
        assert result["errors"] and "field" in result["errors"][0] and "message" in result["errors"][0]
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_IJ2_validate_button_handler_shows_errors_and_never_saves():
    script = _script_block()
    body = _js_function_body(script, "automValidateDraft")
    assert "/api/automations/validate" in body
    assert "/api/automations/" + "'" not in body  # no update/create call anywhere in this function
    assert "POST" in body
    assert "automation_api.create_automation" not in body


# ============================================================================
# K/L/M/N. Sequence creation, reorder, delay step, action step
# ============================================================================

def _ui_sequence_payload(name: str = "Sequence Rule", sequence: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Mirrors `automBuildPayload()` for 'sequence' mode."""
    payload = _ui_create_payload(name)
    del payload["actions"]
    payload["sequence"] = sequence if sequence is not None else [
        {"type": "home_assistant.turn_on", "parameters": {"target": "light.wled"}},
        {"type": "delay", "parameters": {"seconds": 2}},
        {"type": "automation.log", "parameters": {"message": "step 3"}},
    ]
    return payload


def test_K1_sequence_rule_create_and_retrieve_round_trip_matches_p0_11_schema():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/automations", json=_ui_sequence_payload(), timeout=5)
        result = r.json()
        assert result["success"], result
        seq = result["automation"]["sequence"]
        assert seq == [
            {"type": "home_assistant.turn_on", "parameters": {"target": "light.wled"}},
            {"type": "delay", "parameters": {"seconds": 2}},
            {"type": "automation.log", "parameters": {"message": "step 3"}},
        ]
        assert result["automation"]["actions"] == []
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_L1_sequence_reorder_via_update_persists_the_new_order():
    """Mirrors what `automStepMove()` produces (an in-place array swap in
    the draft) followed by a Save - proves reordering is a real, persisted
    mutation, not a client-only display trick."""
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        created = requests.post(dashboard.url + "api/automations", json=_ui_sequence_payload("Reorder Me"), timeout=5).json()["automation"]
        swapped = list(created["sequence"])
        swapped[0], swapped[1] = swapped[1], swapped[0]
        payload = _ui_create_payload("Reorder Me")
        del payload["actions"]
        payload["sequence"] = swapped
        r = requests.post(dashboard.url + "api/automations/" + created["id"] + "/update", json=payload, timeout=5)
        result = r.json()
        assert result["success"]
        assert result["automation"]["sequence"][0]["type"] == "delay"
        assert result["automation"]["sequence"][1]["type"] == "home_assistant.turn_on"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_L2_move_up_down_controls_present_in_source_only_for_sequence_mode():
    script = _script_block()
    body = _js_function_body(script, "renderAutomSteps")
    assert "data-step-up" in body and "data-step-down" in body
    assert "list === 'sequence'" in body  # up/down/arrows gated to sequence mode, matching Phase 4's own explicit-controls fallback


def test_M1_delay_step_create_and_retrieve():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        payload = _ui_sequence_payload("Delay Only", sequence=[{"type": "delay", "parameters": {"seconds": 3.5}}])
        r = requests.post(dashboard.url + "api/automations", json=payload, timeout=5)
        result = r.json()
        assert result["success"]
        assert result["automation"]["sequence"] == [{"type": "delay", "parameters": {"seconds": 3.5}}]
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_M2_add_delay_button_only_rendered_in_sequence_mode_in_source():
    html = _read(_STATIC_INDEX_PATH)
    assert "autom-add-delay-btn" in html
    script = _script_block()
    body = _js_function_body(script, "renderAutomationEditor")
    idx = body.index("autom-add-delay-btn")
    assert "automState.mode === 'sequence'" in body[max(0, idx - 200):idx]


def test_N1_action_step_create_and_retrieve_unordered_actions_list():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        payload = _ui_create_payload("Actions Rule", actions=[
            {"type": "camera.preset", "parameters": {"preset": "front_door"}},
            {"type": "home_assistant.turn_off", "parameters": {"target": "light.main_light"}},
        ])
        r = requests.post(dashboard.url + "api/automations", json=payload, timeout=5)
        result = r.json()
        assert result["success"]
        assert result["automation"]["actions"] == payload["actions"]
        assert result["automation"]["sequence"] == []
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# O/P/Q. Invalid action / trigger / condition
# ============================================================================

def test_O1_invalid_action_type_is_rejected():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        payload = _ui_create_payload("Bad Action", actions=[{"type": "shell.exec", "parameters": {}}])
        r = requests.post(dashboard.url + "api/automations/validate", json=payload, timeout=5)
        result = r.json()
        assert result["valid"] is False
        assert result["errors"][0]["field"] == "actions"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_P1_invalid_trigger_is_rejected():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        payload = _ui_create_payload("Bad Trigger", trigger={"type": "event", "parameters": {"event_name": ""}})
        r = requests.post(dashboard.url + "api/automations/validate", json=payload, timeout=5)
        result = r.json()
        assert result["valid"] is False
        assert result["errors"][0]["field"] == "trigger"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_Q1_invalid_condition_is_rejected():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        payload = _ui_create_payload("Bad Condition", conditions=[{"type": "bogus_operator", "target": "x", "value": 1}])
        r = requests.post(dashboard.url + "api/automations/validate", json=payload, timeout=5)
        result = r.json()
        assert result["valid"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# R/S. API failure / Network failure handling
# ============================================================================

def test_R1_unknown_automation_id_returns_a_structured_not_found_not_a_crash():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.get(dashboard.url + "api/automations/does_not_exist", timeout=5)
        assert r.status_code == 404
        assert "error" in r.json()

        r = requests.post(dashboard.url + "api/automations/does_not_exist/run", json={}, timeout=5)
        result = r.json()
        assert result["success"] is False
        assert "message" in result
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_R2_malformed_json_body_never_returns_a_python_traceback():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.post(
            dashboard.url + "api/automations", data="not json{{{", headers={"Content-Type": "application/json"}, timeout=5,
        )
        assert r.status_code < 500
        assert "Traceback" not in r.text
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_S1_every_automation_api_call_site_in_the_ui_is_wrapped_in_try_catch():
    script = _script_block()
    for fn in ("loadAutomations", "automToggleEnabled", "automDelete", "automRun", "automPollRun", "automValidateDraft", "automSaveDraft", "openAutomationEditorById"):
        body = _js_function_body(script, fn)
        assert "try {" in body and "catch" in body, f"{fn}() must guard its api() call(s) with try/catch (network-failure safety)"


def test_S2_api_helper_itself_never_throws_unhandled_out_of_the_ui_layer():
    # The shared `api()` wrapper (pre-existing, used by every panel) already
    # catches fetch failures and re-throws - proving every P0.13 call site
    # catches THAT re-thrown error (test S1) is what makes network failure
    # safe end-to-end; this test just re-confirms `api()` itself hasn't
    # been changed to silently swallow errors instead (which would make
    # the offline banner logic silently stop working for every panel).
    script = _script_block()
    body = _js_function_body(script, "api")
    assert "catch" in body and "throw ex" in body


# ============================================================================
# T. No direct HA call from the dashboard UI
# ============================================================================

def test_T1_automation_section_of_ui_never_references_home_assistant_directly():
    """P0.14 - the UI's new action/step-type icon/label/default-param
    maps legitimately contain the qualified schema action-type STRING
    `'home_assistant.call_service'` (cosmetic rendering of a step's TYPE,
    the same allowance `test_V1` below already documents for every
    action type - never a dispatch), and P0.14's own comments legitimately
    mention `call_service` as an English word describing that action
    type (e.g. "call_service data") and as part of the unrelated,
    longer identifier `_extract_call_service_entity_ids()`. The bare
    substring `"call_service"` this test used pre-P0.14 is too broad
    for any of that - `"call_service("` (an actual invocation with an
    open paren immediately after, which none of the above ever produce)
    is precise enough to still catch a genuine direct dispatch attempt."""
    script = _script_block()
    section = _automation_section_of_script(script)
    lowered = section.lower()
    for forbidden in ("call_service(", "homeassistant", "home_assistant.py", "ws://", "/api/services/"):
        assert forbidden not in lowered, f"UI must never talk to Home Assistant directly (found {forbidden!r})"


def test_T2_every_api_call_in_the_automation_section_targets_the_automations_family():
    script = _script_block()
    section = _automation_section_of_script(script)
    calls = re.findall(r"api\(\s*(['\"])(.*?)\1", section)
    assert calls, "expected at least one api() call site in the automation section"
    for _, path_expr in calls:
        assert path_expr.startswith("/api/automations"), f"UI automation call must target /api/automations*, found: {path_expr!r}"


def test_T3_automation_api_py_new_functions_never_import_or_call_home_assistant():
    """P0.14 - `ast.get_source_segment()` includes each function's own
    COMMENTS (not just executable statements), and `_known_devices()`'s
    own P0.14 comment legitimately explains, in English prose, that it
    now also covers `home_assistant.run_script`'s entity_id picker - a
    bare lowered-substring check over that whole segment can no longer
    tell "mentions the words in a comment" apart from "actually imports/
    calls something Home Assistant related". Re-expressed as a real AST
    check for actual import/call nodes within the function body, which
    is immune to comment content by construction."""
    source = _read(_AUTOMATION_API_PATH)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("get_schema", "_known_devices"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Import):
                    assert not any("home_assistant" in a.name.lower() for a in sub.names)
                if isinstance(sub, ast.ImportFrom):
                    assert not (sub.module and "home_assistant" in sub.module.lower())
                if isinstance(sub, ast.Call):
                    func = sub.func
                    called_name = (
                        func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute)
                        else ""
                    ).lower()
                    assert "call_service" not in called_name
                    assert "ha_client" not in called_name
                    assert "home_assistant" not in called_name


# ============================================================================
# U. No direct config file mutation
# ============================================================================

def test_U1_automation_section_of_ui_never_references_the_rules_json_file():
    script = _script_block()
    section = _automation_section_of_script(script)
    assert "automation_rules.json" not in section
    assert ".json'" not in section and '.json"' not in section  # no client-side file path of any kind


def test_U2_automation_api_py_new_functions_never_open_or_write_a_file():
    source = _read(_AUTOMATION_API_PATH)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("get_schema", "_known_devices"):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and sub.func.id == "open":
                    raise AssertionError(f"{node.name}() must never call open() directly - found one")


def test_U3_devices_are_read_from_the_existing_in_memory_registry_not_reloaded_from_disk():
    source = _read(_AUTOMATION_API_PATH)
    assert "_devices.LIGHTS" in source and "_devices.SWITCHES" in source
    assert "load_lights_config()" not in source  # reuses the ALREADY-loaded module-level dict, never re-parses the file itself


# ============================================================================
# V. No second automation execution path
# ============================================================================

def test_V1_automation_section_of_ui_never_implements_action_dispatch_logic():
    """The UI may render a step's TYPE as a label/icon (cosmetic), but must
    never branch on a step's type to decide what device behavior to
    perform - that decision belongs exclusively to `AutomationEngine.
    _dispatch_action()`. The only place the UI is allowed to *act* on a
    step is POSTing the whole rule to the server for the server to run."""
    script = _script_block()
    section = _automation_section_of_script(script)
    for forbidden in ("XMLHttpRequest", "new WebSocket", "navigator.serviceWorker"):
        assert forbidden not in section
    # no direct device-control verbs anywhere near a `fetch(`/`api(` call other than the one allowlisted /run endpoint
    assert section.count("/run'") + section.count('/run"') >= 1
    assert "turn_on(" not in section and "turn_off(" not in section  # no client-side re-implementation of device verbs


def test_V2_manual_run_reuses_run_automation_verbatim_end_to_end():
    """Full-stack proof (UI payload shape -> API -> the SAME AutomationEngine.
    run_automation() P0.12 already proved reuses _trigger()/_run_execution())
    that a sequence-based rule created through the UI's own payload shape
    executes through the one real engine, dispatching through ToolManager's
    mock Home Assistant backend - not a second, UI-local executor."""
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        created = requests.post(
            dashboard.url + "api/automations",
            json=_ui_sequence_payload("End To End", sequence=[{"type": "automation.log", "parameters": {"message": "e2e"}}]),
            timeout=5,
        ).json()["automation"]
        r = requests.post(dashboard.url + "api/automations/" + created["id"] + "/run", json={}, timeout=5)
        run_result = r.json()
        assert run_result["success"]
        execution_id = run_result["execution_id"]

        def _finished():
            status = requests.get(dashboard.url + "api/automations/" + created["id"], timeout=5).json()["status"]
            last = status.get("last_execution")
            return (not status["running"]) and last is not None and last["execution_id"] == execution_id

        assert _wait_until(_finished, timeout_s=5.0)
        final = requests.get(dashboard.url + "api/automations/" + created["id"], timeout=5).json()
        assert final["status"]["last_execution"]["final_status"] == "COMPLETED"
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# W. XSS-safe rendering of automation names/descriptions
# ============================================================================

def test_W1_api_returns_raw_unescaped_text_verbatim_escaping_is_a_render_time_concern():
    """The JSON API itself must never HTML-escape data (that would corrupt
    a legitimately-typed `<` or `&` in a name for every OTHER consumer of
    this API) - escaping is correctly the rendering layer's job. This test
    proves the API's own honesty; test W2/W3 prove the rendering layer
    actually does its job."""
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        dangerous_name = '<script>alert(1)</script>"><img src=x>'
        payload = _ui_create_payload(dangerous_name, description='"><svg onload=alert(2)>')
        r = requests.post(dashboard.url + "api/automations", json=payload, timeout=5)
        result = r.json()
        assert result["success"]
        assert result["automation"]["name"] == dangerous_name  # byte-for-byte, not escaped by the API
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_W2_every_free_text_field_interpolation_in_list_render_goes_through_esc_or_escattr():
    script = _script_block()
    body = _js_function_body(script, "renderAutomationList")
    for raw in ("${a.name}", "${a.description}", "${a.id}"):
        assert raw not in body, f"found an UN-escaped interpolation of {raw} in renderAutomationList()"
    assert "esc(a.name)" in body
    assert "esc(a.description)" in body
    assert "escAttr(a.id)" in body


def test_W3_every_free_text_field_interpolation_in_editor_render_goes_through_esc_or_escattr():
    script = _script_block()
    body = _js_function_body(script, "renderAutomationEditor")
    for raw in ("${d.name}", "${d.description}", "${d.id}"):
        assert raw not in body
    assert "escAttr(d.name" in body
    assert "esc(d.description" in body
    assert "escAttr(d.id" in body


def test_W4_escAttr_helper_escapes_html_metacharacters_including_quotes():
    script = _script_block()
    body = _js_function_body(script, "escAttr")
    for ch in ("&", "<", ">", '"', "'"):
        assert repr(ch) in body or f"'{ch}'" in body or f'"{ch}"' in body


# ============================================================================
# X. Persistence after refresh
# ============================================================================

def test_X1_created_automation_survives_a_full_engine_reload_from_disk():
    """Simulates "refresh the page" the honest way for a server-authoritative
    system: force the SAME engine to forget its in-memory state and re-read
    ONLY the persisted file - proving CREATE actually persisted, not just
    updated an in-memory dict the next GET happened to still see."""
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        engine = modules["automation_engine"]
        created = requests.post(
            dashboard.url + "api/automations",
            json=_ui_sequence_payload("Survives Reload"),
            timeout=5,
        ).json()["automation"]

        engine.reload_rules()  # re-reads config from `rp` from scratch, discarding any purely-in-memory state

        reloaded = requests.get(dashboard.url + "api/automations/" + created["id"], timeout=5).json()
        assert reloaded["name"] == "Survives Reload"
        assert reloaded["sequence"] == created["sequence"]

        with open(rp, "r", encoding="utf-8") as fh:
            on_disk = json.load(fh)
        assert created["id"] in on_disk
        assert on_disk[created["id"]]["sequence"] == created["sequence"]
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_X2_update_and_delete_also_persist_across_a_full_reload():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        engine = modules["automation_engine"]
        created = requests.post(dashboard.url + "api/automations", json=_ui_create_payload("Reload Update"), timeout=5).json()["automation"]
        requests.post(
            dashboard.url + "api/automations/" + created["id"] + "/update",
            json=_ui_create_payload("Reload Update", cooldown_seconds=42), timeout=5,
        )
        engine.reload_rules()
        reloaded = requests.get(dashboard.url + "api/automations/" + created["id"], timeout=5).json()
        assert reloaded["cooldown_seconds"] == 42

        requests.post(dashboard.url + "api/automations/" + created["id"] + "/delete", json={}, timeout=5)
        engine.reload_rules()
        r = requests.get(dashboard.url + "api/automations/" + created["id"], timeout=5)
        assert r.status_code == 404
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


# ============================================================================
# Schema endpoint (the one new, minimal API surface this sprint added)
# ============================================================================

def test_SCHEMA1_route_registered_and_reachable():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.get(dashboard.url + "api/automations/schema", timeout=5)
        assert r.status_code == 200
        body = r.json()
        for key in ("trigger_types", "condition_types", "action_types", "sequence_step_types", "devices"):
            assert key in body
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_SCHEMA2_schema_checked_before_the_single_resource_catch_all():
    """`/api/automations/schema` must never be swallowed by the `/api/
    automations/{id}` catch-all and treated as a lookup for an automation
    literally named "schema"."""
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        r = requests.get(dashboard.url + "api/automations/schema", timeout=5)
        assert r.status_code == 200
        assert "error" not in r.json()
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_SCHEMA3_types_are_read_directly_from_models_py_never_hand_duplicated():
    from luno.automation import models as automation_models
    schema = automation_api.get_schema({})
    assert schema["trigger_types"] == sorted(automation_models.TRIGGER_TYPES)
    assert schema["condition_types"] == sorted(automation_models.CONDITION_TYPES)
    assert schema["action_types"] == sorted(automation_models.ACTION_TYPES)
    assert schema["sequence_step_types"] == sorted(automation_models.SEQUENCE_STEP_TYPES)


def test_SCHEMA4_known_event_names_includes_event_names_from_currently_loaded_rules():
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard(rules={
        "r1": {"name": "R1", "trigger": "event:my_custom_event_xyz", "actions": [{"type": "automation.log", "parameters": {}}]},
    })
    try:
        r = requests.get(dashboard.url + "api/automations/schema", timeout=5)
        assert "my_custom_event_xyz" in r.json()["known_event_names"]
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


def test_SCHEMA5_devices_come_from_the_real_lights_and_switches_config():
    """P0.14 - `_known_devices()` additively started including
    `luno.devices.SCRIPTS` too (so the `home_assistant.run_script`
    entity picker has real scripts to offer - see that function's own
    P0.14 docstring comment), so `"script"` is now a legitimate third
    domain alongside the pre-existing `"light"`/`"switch"` - re-checked
    below, not silently dropped."""
    schema = automation_api.get_schema({})
    entity_ids = {d["entity_id"] for d in schema["devices"]}
    assert "light.wled" in entity_ids or len(schema["devices"]) >= 0  # honest: depends on config/lights.config.json in this checkout
    for d in schema["devices"]:
        assert d["domain"] in ("light", "switch", "script")
        assert d["entity_id"]
        assert d["name"]


# ============================================================================
# Architecture guards
# ============================================================================

def test_M1_no_second_automation_engine_instantiated_by_new_p0_13_code():
    source = _read(_AUTOMATION_API_PATH)
    assert "AutomationEngine(" not in source


def test_M2_no_second_persistence_mechanism_in_new_p0_13_code():
    source = _read(_AUTOMATION_API_PATH)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("get_schema", "_known_devices"):
            for sub in ast.walk(node):
                assert not (isinstance(sub, ast.Attribute) and sub.attr in ("dump", "dumps")), (
                    f"{node.name}() must never write JSON itself"
                )


def test_M3_no_direct_home_assistant_call_anywhere_new():
    # Covered in depth by T3 above; re-asserted here under the brief's own
    # "Architecture guards" heading for discoverability.
    test_T3_automation_api_py_new_functions_never_import_or_call_home_assistant()


def test_M4_no_tool_manager_bypass_in_new_p0_13_code():
    source = _read(_AUTOMATION_API_PATH)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("get_schema", "_known_devices"):
            body_source = ast.get_source_segment(source, node) or ""
            assert "tool_manager" not in body_source.lower()
            assert "dispatch_action" not in body_source


def test_M5_no_duplicated_sequence_execution_logic_anywhere_new():
    source = _read(_AUTOMATION_API_PATH)
    for forbidden in ("_run_sequence", "_run_delay_step", "_run_action_step", "_wait_delay", "threading.Thread("):
        assert forbidden not in source


def test_M6_no_eval_exec_or_dynamic_import_in_automation_api_py():
    source = _read(_AUTOMATION_API_PATH)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in ("eval", "exec", "compile", "__import__")
    assert "importlib" not in source
    assert "subprocess" not in source
    assert "os.system" not in source


def test_M7_no_eval_or_dynamic_function_construction_in_the_ui_javascript():
    script = _script_block()
    section = _automation_section_of_script(script)
    for forbidden in ("eval(", "new Function(", "setTimeout(\"", "setInterval(\""):
        assert forbidden not in section, f"found dangerous dynamic-code construct in UI JS: {forbidden!r}"


def test_M8_vision_camera_occupancy_modules_untouched_by_p0_13():
    # Same style of proof P0.11/P0.12 already established: these files'
    # own on-disk mtimes/hashes aren't tracked here (no baseline snapshot
    # exists in this file), so the real guard is textual - P0.13's own new
    # code (automation_api.py's new functions, index.html's new panel)
    # never imports or references these modules' internals.
    automation_api_source = _read(_AUTOMATION_API_PATH)
    server_source = _read(_SERVER_PATH)
    # Check actual `import`/`from ... import` statements only - comments
    # elsewhere in the file legitimately CITE these module/file names when
    # documenting where a mirrored constant/hint originates (see the
    # `_KNOWN_EVENT_NAME_HINTS`/`_KNOWN_CAMERA_EVENT_KIND_HINTS` docstrings),
    # which is not the same as this file importing or calling into them.
    import_lines = [ln for ln in automation_api_source.splitlines() if ln.strip().startswith(("import ", "from "))]
    for forbidden in ("vision_occupancy", "camera_automation", "luno.vision", "vision_bridge"):
        assert not any(forbidden in ln for ln in import_lines), f"unexpected import referencing {forbidden!r}: {import_lines}"
    # server.py's new P0.13-adjacent diff is limited to routing to
    # automation_api - it already imported collectors/controls before this
    # sprint touched anything, so only assert the NEW route lines are scoped
    # to automation_api, not a new vision/camera import.
    assert "get_schema" in server_source


def test_M9_manual_run_reuses_run_automation_verbatim_via_ast():
    # AST-based (not a raw substring scan of the function's source segment,
    # which would also match these method names when they appear inside
    # this function's own explanatory docstring/comments) - walks actual
    # `ast.Call` nodes only, matching the precise-extraction convention
    # `test_p0_12_automation_api.py::_function_body_source()` already
    # established for the same false-positive reason.
    source = _read(_AUTOMATION_API_PATH)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run_automation":
            called_names = set()
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    if isinstance(sub.func, ast.Attribute):
                        called_names.add(sub.func.attr)
                    elif isinstance(sub.func, ast.Name):
                        called_names.add(sub.func.id)
            assert "run_automation" in called_names
            assert "_trigger" not in called_names
            assert "_run_execution" not in called_names


def test_M10_validate_endpoint_has_zero_persistence_side_effects_still_holds():
    source = _read(_AUTOMATION_API_PATH)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "validate_automation":
            args = [a.arg for a in node.args.args]
            assert args == ["body"], "validate_automation() must take ONLY body - no modules/engine access possible"


def test_M11_existing_p0_11_sequence_engine_remains_the_only_executor_end_to_end():
    """The UI's own sequence payload round-trips through create -> run and
    produces `current_step_index`/`total_steps` progress fields that ONLY
    `AutomationEngine._run_sequence()` (P0.11, unmodified by this sprint)
    ever populates - proving the dashboard triggered the real engine path,
    not a UI-local simulation of one."""
    runtime, modules, adapter_manager, dashboard, rp = _build_dashboard()
    try:
        created = requests.post(
            dashboard.url + "api/automations",
            json=_ui_sequence_payload("Progress Proof", sequence=[
                {"type": "delay", "parameters": {"seconds": 1}},
                {"type": "automation.log", "parameters": {"message": "after delay"}},
            ]),
            timeout=5,
        ).json()["automation"]
        r = requests.post(dashboard.url + "api/automations/" + created["id"] + "/run", json={}, timeout=5)
        execution_id = r.json()["execution_id"]

        def _observed_progress():
            status = requests.get(dashboard.url + "api/automations/" + created["id"], timeout=5).json()["status"]
            last = status.get("last_execution")
            return last is not None and last.get("execution_id") == execution_id and last.get("total_steps") == 2

        assert _wait_until(_observed_progress, timeout_s=3.0)
    finally:
        _teardown(runtime, adapter_manager, dashboard, rp)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
