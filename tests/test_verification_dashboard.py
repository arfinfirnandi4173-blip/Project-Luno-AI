"""
test_verification_dashboard.py
================================

Verified Smart Home Execution sprint - "Dashboard exposes verification
history" checklist item. Same real bootstrap stack `tests/test_dashboard.py`/
`tests/test_llm_dashboard.py`/`tests/test_routing_dashboard.py` already
build (`register_all_modules`/`register_all_adapters`, all-mock backends
by default) with a real, running `DashboardServer` on top - but this
file additionally registers a REAL `RealHomeAssistantHandler` (against a
synthetic fake HA client, no live Home Assistant needed) directly onto
`tool_manager_module.manager.registry`, using the EXACT SAME
`luno.bootstrap.adapters._make_verification_event_publisher` production
wiring function `_register_real_home_assistant_handler` itself calls -
so this proves the real wiring, not a reimplementation of it.

A real `tool_requested` event is then published on the Runtime's own
Event Bus (the same event `PlannerBridgeModule._tool_bridge_handler`
always publishes), driving the exact same `ToolManagerBridgeModule.
on_event` -> `RealHomeAssistantHandler.execute()` path production code
uses - proving `ActionVerificationStarted`/`ActionVerified`/etc. actually
reach `/api/verification` end-to-end, and that `ToolStarted`/
`ToolFinished` keep firing unchanged alongside them (regression guard).

Run:
    python3 -m pytest tests/test_verification_dashboard.py
"""

from __future__ import annotations

import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import requests  # noqa: E402

from luno.adapters.events import ActionVerificationStarted  # noqa: E402
from luno.bootstrap.adapters import _make_verification_event_publisher, register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.dashboard import DashboardServer  # noqa: E402
from luno.tool_manager.builtin.real_home_assistant import RealHomeAssistantHandler  # noqa: E402
from luno.tool_manager.models import ToolCall  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


class _FakeHAClient:
    """Minimal synthetic client - same shape `luno/tool_manager/tests/
    test_real_home_assistant_verification.py::FakeHAClient` already uses
    for the handler's own unit tests, duplicated here (deliberately
    small, no shared import) so this dashboard test has zero dependency
    on that test module's internals changing shape."""

    def __init__(self):
        self.states = {}
        self.state_after_call = {}
        self._called = set()

    def call_service(self, domain, service, entity_id=None, data=None):
        self._called.add(entity_id)
        return {"success": True, "domain": domain, "service": service, "entity_id": entity_id, "data": data or {}}

    def get_entity_state(self, entity_id):
        if entity_id in self._called and entity_id in self.state_after_call:
            self.states[entity_id] = self.state_after_call[entity_id]
        return self.states.get(entity_id)


def _build_dashboard():
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    runtime.start()
    dashboard = DashboardServer(runtime, adapter_manager, modules, cfg, audio_capture_store=adapters.get("audio_capture_store"), host="127.0.0.1", port=0)
    dashboard.start()
    return runtime, adapter_manager, modules, dashboard


def _teardown(runtime, adapter_manager, dashboard):
    ShutdownCoordinator(runtime, adapter_manager, dashboard=dashboard).shutdown()


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.05) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _wire_real_handler(modules, fake_client) -> None:
    """Exercises the SAME production extension point `luno.bootstrap.
    adapters._register_real_home_assistant_handler` uses (registry
    override + `_make_verification_event_publisher`), just skipping that
    function's own "is this really a live HA connection" detection -
    that detection logic is out of scope for this sprint (see
    `luno.tool_manager.tests.test_real_home_assistant_verification` for
    the handler's own from-scratch unit coverage)."""
    tool_manager_module = modules["tool_manager_module"]
    on_verification_event = _make_verification_event_publisher(tool_manager_module)
    handler = RealHomeAssistantHandler(fake_client, on_verification_event=on_verification_event)
    tool_manager_module.manager.registry.register("home_assistant", handler)


def _publish_tool_requested(runtime, tool_call: ToolCall, execution_id: str) -> None:
    runtime.event_bus.publish(Event(type="tool_requested", data={"execution_id": execution_id, "tool_call": tool_call}))


def test_api_verification_reports_unavailable_without_tool_manager_module():
    """Regression guard - mirrors every other `collect_*` function's own
    `modules.get(...)` -> None defensive convention (see
    `test_routing_dashboard.py::test_api_routing_gracefully_reports_unavailable_if_no_decision_engine`)."""
    from luno.dashboard import collectors
    result = collectors.collect_verification_status({}, [])
    assert result == {"available": False}


def test_verification_event_classes_carry_expected_fields():
    ev = ActionVerificationStarted(data={"request_id": "x", "entity_id": "light.demo"})
    assert ev.type == "action_verification_started"
    assert ev.data["entity_id"] == "light.demo"


def test_api_verification_starts_empty():
    runtime, adapter_manager, modules, dashboard = _build_dashboard()
    try:
        resp = requests.get(f"{dashboard.url}/api/verification", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        assert data["history"] == []
        assert data["current_verification"] is None
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_api_verification_reports_a_successful_verified_action_end_to_end():
    runtime, adapter_manager, modules, dashboard = _build_dashboard()
    try:
        fake_client = _FakeHAClient()
        fake_client.states["light.demo"] = "off"
        fake_client.state_after_call["light.demo"] = "on"
        _wire_real_handler(modules, fake_client)

        os.environ["VERIFY_DELAY_MS"] = "10"
        os.environ["VERIFY_TIMEOUT_MS"] = "2000"
        try:
            _publish_tool_requested(
                runtime, ToolCall(tool="home_assistant", action="turn_on", target="light.demo"), "exec-verify-1",
            )

            def _has_row():
                after = requests.get(f"{dashboard.url}/api/verification", timeout=5).json()
                return bool(after["history"])

            assert _wait_until(_has_row, timeout_s=5.0)

            after = requests.get(f"{dashboard.url}/api/verification", timeout=5).json()
            row = after["history"][-1]
            assert row["entity_id"] == "light.demo"
            assert row["service"] == "homeassistant.turn_on"
            assert row["expected_state"] == "on"
            assert row["actual_state"] == "on"
            assert row["verification_status"] == "verified"
            assert row["retry_count"] >= 1
            assert row["elapsed_time_ms"] is not None
            assert row["failure_reason"] is None
            assert "turned on" in (row["final_result"] or "").lower()
            assert after["current_verification"]["request_id"] == row["request_id"]

            # Regression: ToolStarted/ToolFinished still fire unchanged
            # around the same execute() call - the new events are purely
            # additive, not a replacement.
            tm = requests.get(f"{dashboard.url}/api/tool_manager", timeout=5).json()
            assert tm["current_tool"] == "home_assistant"
            assert tm["last_result"]["success"] is True
        finally:
            os.environ.pop("VERIFY_DELAY_MS", None)
            os.environ.pop("VERIFY_TIMEOUT_MS", None)
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_api_verification_reports_a_failed_verification():
    runtime, adapter_manager, modules, dashboard = _build_dashboard()
    try:
        fake_client = _FakeHAClient()
        fake_client.states["light.stuck"] = "off"  # never settles
        _wire_real_handler(modules, fake_client)

        os.environ["VERIFY_DELAY_MS"] = "10"
        os.environ["VERIFY_RETRIES"] = "1"
        os.environ["VERIFY_TIMEOUT_MS"] = "2000"
        try:
            _publish_tool_requested(
                runtime, ToolCall(tool="home_assistant", action="turn_on", target="light.stuck"), "exec-verify-2",
            )

            def _has_failed_row():
                after = requests.get(f"{dashboard.url}/api/verification", timeout=5).json()
                return any(r["entity_id"] == "light.stuck" and r["verification_status"] == "failed" for r in after["history"])

            assert _wait_until(_has_failed_row, timeout_s=5.0)

            after = requests.get(f"{dashboard.url}/api/verification", timeout=5).json()
            row = next(r for r in after["history"] if r["entity_id"] == "light.stuck")
            assert row["verification_status"] == "failed"
            assert row["failure_reason"]
            assert "didn't respond" in (row["final_result"] or "").lower()

            tm = requests.get(f"{dashboard.url}/api/tool_manager", timeout=5).json()
            assert tm["last_result"]["success"] is False
        finally:
            os.environ.pop("VERIFY_DELAY_MS", None)
            os.environ.pop("VERIFY_RETRIES", None)
            os.environ.pop("VERIFY_TIMEOUT_MS", None)
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_api_verification_never_leaks_api_keys():
    runtime, adapter_manager, modules, dashboard = _build_dashboard()
    try:
        resp = requests.get(f"{dashboard.url}/api/verification", timeout=5)
        body_text = resp.text
        real_key = os.getenv("HA_TOKEN", "")
        if real_key:
            assert real_key not in body_text
    finally:
        _teardown(runtime, adapter_manager, dashboard)
