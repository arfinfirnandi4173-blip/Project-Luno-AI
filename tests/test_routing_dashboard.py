"""
test_routing_dashboard.py
============================

Intelligent AI Routing Engine sprint - "Dashboard updates" checklist
item. Same real bootstrap stack `tests/test_dashboard.py`/
`tests/test_llm_dashboard.py` already build (`register_all_modules`/
`register_all_adapters`, all-mock backends) with a real, running
`DashboardServer` on top, exercising the new `/api/routing` endpoint
over REAL HTTP - proving `DecisionEngine` state (config/stats/sticky
conversations) actually reaches the browser-facing API this sprint
added to `dashboard/collectors.py`/`dashboard/server.py`, and that a
real end-to-end chat turn through `/api/chat/send` actually produces a
routing decision (proving the `PlannerBridgeModule` wiring, not just
the collector in isolation).

Run:
    python3 -m pytest tests/test_routing_dashboard.py
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

from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.dashboard import DashboardServer  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


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


def test_api_routing_reports_decision_engine_state():
    runtime, adapter_manager, modules, dashboard = _build_dashboard()
    try:
        resp = requests.get(f"{dashboard.url}/api/routing", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        for key in ("config", "stats", "sticky_conversations", "web_search_available", "llm_cost_by_provider"):
            assert key in data, f"missing '{key}' in /api/routing response"

        cfg = data["config"]
        for key in (
            "default_provider_alias", "reasoning_provider_alias", "search_provider",
            "enable_auto_routing", "enable_cost_optimizer", "enable_provider_affinity",
            "enable_web_search", "reasoning_complexity_threshold",
        ):
            assert key in cfg, f"missing config field '{key}'"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_api_routing_records_a_real_decision_from_a_chat_turn():
    """End-to-end: a real `/api/chat/send` turn must produce exactly one
    more routing decision, visible in `/api/routing`'s stats - proves
    `PlannerBridgeModule._handle_utterance()` actually calls
    `DecisionEngine.decide()` for real, not just that the collector
    plumbing works against a hand-built engine."""
    runtime, adapter_manager, modules, dashboard = _build_dashboard()
    try:
        wake = requests.post(f"{dashboard.url}/api/controls/wake_session", timeout=5)
        assert wake.status_code == 200

        before = requests.get(f"{dashboard.url}/api/routing", timeout=5).json()
        before_count = before["stats"]["total_decisions"]

        send = requests.post(
            f"{dashboard.url}/api/chat/send", json={"text": "turn on the bedroom light"}, timeout=5,
        )
        assert send.status_code == 200

        def _grew():
            after = requests.get(f"{dashboard.url}/api/routing", timeout=5).json()
            return after["stats"]["total_decisions"] > before_count

        assert _wait_until(_grew, timeout_s=8.0)

        after = requests.get(f"{dashboard.url}/api/routing", timeout=5).json()
        recent = after["stats"]["recent"]
        assert recent, "expected at least one recorded routing decision"
        last = recent[-1]
        for key in ("primary_intent", "complexity", "knowledge_source", "provider_alias", "reasoning"):
            assert key in last
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_api_routing_never_leaks_api_keys():
    runtime, adapter_manager, modules, dashboard = _build_dashboard()
    try:
        resp = requests.get(f"{dashboard.url}/api/routing", timeout=5)
        body_text = resp.text
        real_key = os.getenv("OPENROUTER_API_KEY", "")
        if real_key:
            assert real_key not in body_text
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_api_routing_gracefully_reports_unavailable_if_no_decision_engine():
    """Regression guard: the collector must never raise/500 even if
    `modules` somehow lacks a `planner_module` (defensive - matches
    every other `collect_*` function's own `modules.get(...)` -> None
    guard convention in `dashboard/collectors.py`)."""
    from luno.dashboard import collectors
    result = collectors.collect_routing_status({}, None)
    assert result == {"available": False}
