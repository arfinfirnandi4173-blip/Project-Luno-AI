"""
test_llm_dashboard.py
======================

Multi-LLM Provider System sprint - "Dashboard updates" checklist item.
Builds the exact same real bootstrap stack `tests/test_dashboard.py`
already builds (`register_all_modules`/`register_all_adapters`, all-
mock backends) with a real, running `DashboardServer` on top, and
exercises the new `/api/llm` endpoint and `/api/controls/
switch_llm_provider` control over REAL HTTP - proving the LLM Manager's
state (provider/model/health/stats) actually reaches the browser-facing
API this sprint added to `dashboard/collectors.py`/`dashboard/server.py`/
`dashboard/controls.py`, not just that `LLMManagerAdapter.status()`
looks right in isolation (already covered by
`luno/adapters/tests/test_llm_manager.py`).

Run:
    python3 -m pytest tests/test_llm_dashboard.py
"""

from __future__ import annotations

import os
import sys

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
    return runtime, adapter_manager, dashboard


def _teardown(runtime, adapter_manager, dashboard):
    ShutdownCoordinator(runtime, adapter_manager, dashboard=dashboard).shutdown()


def test_api_llm_endpoint_reports_manager_state():
    runtime, adapter_manager, dashboard = _build_dashboard()
    try:
        resp = requests.get(f"{dashboard.url}/api/llm", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["available"] is True
        # Reads the real .env (loaded at `luno.config` import time, not
        # isolated by this test) - OpenAI-Primary/DeepSeek-Fallback sprint
        # set LLM_PROVIDER=openai as this project's configured default.
        assert data["current_provider"] == "openai"
        for key in (
            "current_model", "streaming_enabled", "fallback_enabled", "priority",
            "configured_providers", "health", "capabilities", "stats_by_provider",
        ):
            assert key in data, f"missing '{key}' in /api/llm response"
        assert isinstance(data["priority"], list) and set(data["priority"]) == {
            "openrouter", "openai", "gemini", "anthropic", "local",
        }
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_api_llm_never_leaks_api_keys():
    """Security requirement: 'Never expose API keys. Hide secrets in
    Dashboard.' - `/api/llm` must never echo back a raw provider API
    key anywhere in its JSON body, even though `LLMManagerAdapter`
    itself obviously holds real ones in each provider's `ProviderConfig`."""
    runtime, adapter_manager, dashboard = _build_dashboard()
    try:
        resp = requests.get(f"{dashboard.url}/api/llm", timeout=5)
        body_text = resp.text
        real_key = os.getenv("OPENROUTER_API_KEY", "")
        if real_key:
            assert real_key not in body_text
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_switch_llm_provider_control_over_http():
    runtime, adapter_manager, dashboard = _build_dashboard()
    try:
        resp = requests.post(f"{dashboard.url}/api/controls/switch_llm_provider", json={"provider": "local"}, timeout=5)
        assert resp.status_code == 200
        result = resp.json()
        assert result["ok"] is True

        follow_up = requests.get(f"{dashboard.url}/api/llm", timeout=5).json()
        assert follow_up["current_provider"] == "local"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_switch_llm_provider_rejects_unknown_provider():
    runtime, adapter_manager, dashboard = _build_dashboard()
    try:
        resp = requests.post(f"{dashboard.url}/api/controls/switch_llm_provider", json={"provider": "not-a-provider"}, timeout=5)
        assert resp.status_code == 200
        result = resp.json()
        assert result["ok"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_switch_llm_provider_warns_when_target_has_no_api_key(monkeypatch):
    """Bug fix: a user reported switching to 'openai' but replies kept
    coming from 'openrouter' with zero explanation - `switch_provider()`
    happily reports success for ANY valid provider name even without an
    API key, and the adapter then silently falls through to whichever
    provider IS usable. The control must now say so explicitly instead
    of reporting a bare, misleading success."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    runtime, adapter_manager, dashboard = _build_dashboard()
    try:
        resp = requests.post(f"{dashboard.url}/api/controls/switch_llm_provider", json={"provider": "openai"}, timeout=5)
        assert resp.status_code == 200
        result = resp.json()
        assert result["ok"] is True  # the switch itself is not an error...
        assert result.get("warning") is True  # ...but it must be flagged as not actually usable
        assert "no api key" in result["message"].lower() or "openai" in result["message"].lower()
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_llm_shows_up_in_generic_adapter_status_too():
    """Regression: `LLMManagerAdapter` is still a normal adapter as far
    as the pre-existing `/api/adapters` panel is concerned - registering
    it didn't remove it from the generic adapter table other code/tests
    already rely on."""
    runtime, adapter_manager, dashboard = _build_dashboard()
    try:
        resp = requests.get(f"{dashboard.url}/api/adapters", timeout=5)
        names = [a["name"] for a in resp.json()["adapters"]]
        assert "openrouter" in names
    finally:
        _teardown(runtime, adapter_manager, dashboard)
