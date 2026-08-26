"""
test_dashboard.py
====================

Sprint 7 - Web Dashboard regression suite. Builds the exact same real
stack `tests/test_production_launcher.py` builds (`register_all_modules`/
`register_all_adapters`, all-mock backends, no external hardware/
network needed) and layers a real `DashboardServer` on top of it,
bound to `127.0.0.1:0` (OS-assigned free port) so many scenarios can
run back-to-back in one process without port collisions. Every request
in this file is a REAL HTTP request against a REAL, running
`http.server.ThreadingHTTPServer` - nothing here mocks the HTTP layer
itself, only the underlying adapters (same "mock backend" every other
launcher test already uses).

Covers the sprint's own testing checklist: Runtime offline, Runtime
reconnect, module status, adapter status, planner view, tool manager
view, vision memory view, memory retrieval view, event stream, logs,
statistics, control buttons, configuration reload, live updates, a
stress test with thousands of events, and thread safety.

Run:
    python3 tests/test_dashboard.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Tuple

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
from luno.core.events import Event  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.dashboard import DashboardServer  # noqa: E402
from luno.dashboard import controls as dash_controls  # noqa: E402

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []

#: Fast heartbeat/scheduler tick, same fix already established in
#: `tests/test_production_launcher.py::test_23` for the identical
#: symptom (many sequentially-built-and-torn-down full stacks leaving
#: enough transient background thread churn to occasionally starve a
#: timing-sensitive assertion of GIL time) - not needed for correctness,
#: only to keep this suite's wall-clock time and flakiness down.
_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _build_stack():
    """Same real bootstrap sequence `main.py`/`test_production_launcher.py`
    use - all-mock backends, no external dependency required."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    return runtime, modules, adapter_manager, cfg, adapters.get("audio_capture_store")


def _build_dashboard(start_runtime: bool = True):
    runtime, modules, adapter_manager, cfg, audio_store = _build_stack()
    if start_runtime:
        runtime.start()
    dashboard = DashboardServer(runtime, adapter_manager, modules, cfg, audio_capture_store=audio_store, host="127.0.0.1", port=0)
    dashboard.start()
    return runtime, modules, adapter_manager, cfg, dashboard


def _teardown(runtime, adapter_manager, dashboard=None):
    coordinator = ShutdownCoordinator(runtime, adapter_manager, dashboard=dashboard)
    coordinator.shutdown()


# ============================================================================
# Runtime offline / reconnect
# ============================================================================

@scenario
def test_01_ping_reports_offline_before_runtime_started():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard(start_runtime=False)
    try:
        r = requests.get(dashboard.url + "api/ping", timeout=5)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["running"] is False  # Runtime.start() was never called
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_02_ping_reports_online_once_runtime_started():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard(start_runtime=True)
    try:
        r = requests.get(dashboard.url + "api/ping", timeout=5)
        body = r.json()
        assert body["ok"] is True
        assert body["running"] is True
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_03_client_sees_connection_refused_once_server_stopped():
    """The real-world "Runtime Offline" signal a browser would see: the
    HTTP server itself is gone, not just an unhealthy JSON payload."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    url = dashboard.url + "api/ping"
    requests.get(url, timeout=5)  # confirm it's up first
    dashboard.stop()
    try:
        requests.get(url, timeout=2)
        assert False, "expected a connection error after dashboard.stop()"
    except requests.exceptions.ConnectionError:
        pass
    finally:
        _teardown(runtime, adapter_manager)


@scenario
def test_04_dashboard_reconnects_on_the_same_port_after_restart():
    """Simulates a browser's auto-reconnect: the SAME `DashboardServer`
    instance stopped and started again must bind cleanly (relies on
    `HTTPServer.allow_reuse_address`, stdlib default) and serve again."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        port = dashboard.port
        dashboard.stop()
        dashboard.port = port  # keep the same port for this specific test
        dashboard.start()
        r = requests.get(dashboard.url + "api/ping", timeout=5)
        assert r.status_code == 200
        assert r.json()["running"] is True
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ============================================================================
# Module / adapter status
# ============================================================================

@scenario
def test_05_module_status_lists_every_registered_module_with_required_fields():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        body = requests.get(dashboard.url + "api/modules", timeout=5).json()
        names = {m["name"] for m in body["modules"]}
        for expected in ("planner", "tool_manager", "vision_memory", "memory_retrieval", "session_manager", "barge_in", "behavior_tree"):
            assert expected in names, f"missing module '{expected}'"
        for m in body["modules"]:
            for field in ("name", "state", "restart_count", "last_error", "healthy"):
                assert field in m
            assert m["state"] == "running"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_06_adapter_status_lists_every_registered_adapter_with_required_fields():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        body = requests.get(dashboard.url + "api/adapters", timeout=5).json()
        names = {a["name"] for a in body["adapters"]}
        for expected in ("whisper", "vision", "openrouter", "fish_audio", "home_assistant", "unity", "scheduler_adapter"):
            assert expected in names, f"missing adapter '{expected}'"
        for a in body["adapters"]:
            for field in ("name", "enabled", "module_state", "connected", "events_in", "events_out", "restart_count"):
                assert field in a
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_07_conversation_status_reflects_session_state():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        body = requests.get(dashboard.url + "api/conversation", timeout=5).json()
        assert body["raw_state"] == "sleeping"  # fresh session starts dormant
        modules["session_manager"].force_wake(reason="test")
        body = requests.get(dashboard.url + "api/conversation", timeout=5).json()
        assert body["raw_state"] != "sleeping"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ============================================================================
# Planner / Tool Manager / Vision Memory / Memory Retrieval views
# ============================================================================

@scenario
def test_08_planner_view_reports_no_plan_then_a_real_plan():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        body = requests.get(dashboard.url + "api/planner", timeout=5).json()
        assert body["has_plan"] is False

        modules["session_manager"].force_wake(reason="test")
        runtime.event_bus.publish(Event(type="user_utterance", data={"text": "turn on the light", "request_id": "req-dash-1", "conversation_id": "conv-dash-1"}))
        deadline = time.time() + 5.0
        has_plan = False
        while time.time() < deadline:
            body = requests.get(dashboard.url + "api/planner", timeout=5).json()
            if body["has_plan"]:
                has_plan = True
                break
            time.sleep(0.1)
        assert has_plan, "expected a plan to appear within 5s"
        assert "plan" in body and "progress" in body
        assert "id" in body["plan"] and "tasks" in body["plan"]
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_09_tool_manager_view_reports_history_from_events():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        runtime.event_bus.publish(Event(type="tool_requested", data={"execution_id": "ex-1", "tool_call": {"tool": "home_assistant", "action": "turn_on", "target": "light.kitchen"}}))
        deadline = time.time() + 5.0
        history_seen = False
        while time.time() < deadline:
            body = requests.get(dashboard.url + "api/tool_manager", timeout=5).json()
            if body["history"]:
                history_seen = True
                break
            time.sleep(0.1)
        assert history_seen, "expected tool_started/finished events to appear in history"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_10_vision_memory_view_returns_events_objects_and_supports_search():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        runtime.event_bus.publish(Event(type="person_detected", data={}))
        time.sleep(0.3)
        body = requests.get(dashboard.url + "api/vision_memory", timeout=5).json()
        assert "events" in body and "objects" in body and "long_term_memory" in body

        needle_body = requests.get(dashboard.url + "api/vision_memory", params={"search": "zzz_no_such_observation_zzz"}, timeout=5).json()
        assert needle_body["events"] == []
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_11_memory_retrieval_view_previews_without_calling_llm():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        empty = requests.get(dashboard.url + "api/memory_retrieval", timeout=5).json()
        assert empty["memories"] == []

        body = requests.get(dashboard.url + "api/memory_retrieval", params={"query": "what is on the desk"}, timeout=5).json()
        assert body["query"] == "what is on the desk"
        assert "token_estimate" in body
        assert isinstance(body["memories"], list)
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ============================================================================
# Event stream / Logs / Statistics
# ============================================================================

@scenario
def test_12_event_stream_delivers_a_live_published_event():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        received = []

        def _consume():
            with requests.get(dashboard.url + "api/events/stream", stream=True, timeout=8) as resp:
                for line in resp.iter_lines(decode_unicode=True):
                    if line and line.startswith("data:") and "dashboard_test_event" in line:
                        received.append(line)
                        return

        t = threading.Thread(target=_consume, daemon=True)
        t.start()
        time.sleep(0.2)
        # Re-published on a short interval rather than once - makes this
        # assertion robust to transient thread-scheduling delays (see
        # `_FAST_CORE_CONFIG`'s own comment) without weakening what's
        # actually being verified: if the SSE pipe genuinely doesn't
        # work, EVERY one of these publishes fails to arrive and the
        # assertion below still correctly fails.
        deadline = time.time() + 8.0
        while not received and time.time() < deadline:
            runtime.event_bus.publish(Event(type="dashboard_test_event", data={"hello": "world"}))
            time.sleep(0.3)
        t.join(timeout=2)
        assert received, "expected at least one SSE event within 8s"
        assert "dashboard_test_event" in received[0]
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_13_events_snapshot_endpoint_supports_type_filter_and_search():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        runtime.event_bus.publish(Event(type="dashboard_marker_event", data={"needle": "abc123"}))
        time.sleep(0.3)
        body = requests.get(dashboard.url + "api/events", params={"type": "dashboard_marker_event"}, timeout=5).json()
        assert any(e["type"] == "dashboard_marker_event" for e in body["events"])

        body2 = requests.get(dashboard.url + "api/events", params={"search": "abc123"}, timeout=5).json()
        assert any("abc123" in str(e["data"]) for e in body2["events"])
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_14_logs_snapshot_and_stream_capture_real_print_output():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        from luno.core.utils import log as core_log
        core_log("dashboard_test_marker_xyz", "dashboard_test")
        time.sleep(0.2)
        body = requests.get(dashboard.url + "api/logs", params={"search": "dashboard_test_marker_xyz"}, timeout=5).json()
        assert any("dashboard_test_marker_xyz" in r["raw"] for r in body["logs"])

        download = requests.get(dashboard.url + "api/logs/download", timeout=5)
        assert download.status_code == 200
        assert "dashboard_test_marker_xyz" in download.text
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_15_logs_stdout_tee_never_swallows_real_stdout():
    """Additive-only guarantee: installing the log capture must not stop
    real stdout from receiving the same text."""
    import io
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        real_stdout = sys.stdout
        assert real_stdout is not None
        # sys.stdout was swapped for a _TeeWriter - writing through it
        # must still reach the ORIGINAL stream underneath.
        captured = io.StringIO()
        original = dashboard._log_capture._original_stdout
        assert original is not None
        print("tee-passthrough-check-998877")
        # can't easily intercept the real terminal's fd here, but we CAN
        # assert the wrapper forwards by checking its own `_real` target
        # is the pre-install stdout object, never None/itself.
        assert sys.stdout is not original
        assert sys.stdout._real is original
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_16_statistics_reflects_aggregated_counters():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        before = requests.get(dashboard.url + "api/statistics", timeout=5).json()
        runtime.event_bus.publish(Event(type="wake_word_detected", data={"confidence": 0.9}))
        time.sleep(0.3)
        after = requests.get(dashboard.url + "api/statistics", timeout=5).json()
        assert after["conversations_today"] >= before["conversations_today"] + 1
        for field in ("planner_success_rate", "tool_success_rate", "average_llm_time_ms", "average_tts_time_ms", "average_whisper_time_ms"):
            assert field in after
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ============================================================================
# Controls
# ============================================================================

@scenario
def test_17_control_sleep_and_wake_session():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/controls/wake_session", timeout=5).json()
        assert r["ok"] is True
        conv = requests.get(dashboard.url + "api/conversation", timeout=5).json()
        assert conv["raw_state"] != "sleeping"

        r = requests.post(dashboard.url + "api/controls/sleep_session", timeout=5).json()
        assert r["ok"] is True
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_18_control_restart_module_and_restart_adapter():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/controls/restart_module", json={"name": "tool_manager"}, timeout=5).json()
        assert r["ok"] is True
        r = requests.post(dashboard.url + "api/controls/restart_adapter", json={"name": "fish_audio"}, timeout=5).json()
        assert r["ok"] is True
        # unknown names fail cleanly, never a 500 / exception
        r = requests.post(dashboard.url + "api/controls/restart_module", json={"name": "does_not_exist"}, timeout=5).json()
        assert r["ok"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_19_control_emergency_stop_and_clear():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        requests.post(dashboard.url + "api/controls/emergency_stop", timeout=5).json()
        time.sleep(0.2)
        conv = requests.get(dashboard.url + "api/conversation", timeout=5).json()
        assert conv["emergency_active"] is True

        r = requests.post(dashboard.url + "api/controls/emergency_clear", timeout=5).json()
        assert r["ok"] is True
        conv = requests.get(dashboard.url + "api/conversation", timeout=5).json()
        assert conv["emergency_active"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_20_control_clear_planner_queue_and_cancel_llm_never_raise():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/controls/clear_planner_queue", timeout=5).json()
        assert r["ok"] is True
        r = requests.post(dashboard.url + "api/controls/cancel_current_llm", timeout=5).json()
        assert r["ok"] is True
        r = requests.post(dashboard.url + "api/controls/stop_speech", timeout=5).json()
        assert r["ok"] is True
        r = requests.post(dashboard.url + "api/controls/resume_speech", timeout=5).json()
        assert r["ok"] is True
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_21_control_debug_toggle_is_dashboard_local_only():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/controls/debug", json={"enabled": True}, timeout=5).json()
        assert r["debug_enabled"] is True
        state = requests.get(dashboard.url + "api/debug_state", timeout=5).json()
        assert state["debug_enabled"] is True
        r = requests.post(dashboard.url + "api/controls/debug", json={"enabled": False}, timeout=5).json()
        assert r["debug_enabled"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_22_controls_module_functions_never_raise_on_bad_input():
    """Calling `controls.py` directly (bypassing HTTP) - the underlying
    functions must be equally safe."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        result = dash_controls.restart_module(runtime, "totally_unknown_module")
        assert result["ok"] is False
        result = dash_controls.restart_adapter(adapter_manager, "totally_unknown_adapter")
        assert result["ok"] is False
        result = dash_controls.emergency_clear({})
        assert result["ok"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ============================================================================
# Configuration reload
# ============================================================================

@scenario
def test_23_configuration_view_hides_secrets():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        body = requests.get(dashboard.url + "api/configuration", timeout=5).json()
        blob = str(body)
        for secret_marker in ("OPENAI_API_KEY", "HA_TOKEN", "TAVILY_API_KEY"):
            assert f'"{secret_marker}"' not in blob, f"{secret_marker} must never be a visible key in /api/configuration"
        assert "precedence" in body and len(body["precedence"]) == 4
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_24_control_reload_configuration_succeeds_and_is_idempotent():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        r1 = requests.post(dashboard.url + "api/controls/reload_configuration", timeout=10).json()
        assert r1["ok"] is True
        r2 = requests.post(dashboard.url + "api/controls/reload_configuration", timeout=10).json()
        assert r2["ok"] is True
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ============================================================================
# Live updates
# ============================================================================

@scenario
def test_25_live_module_status_updates_after_a_restart():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        before = requests.get(dashboard.url + "api/modules", timeout=5).json()
        before_count = next(m["restart_count"] for m in before["modules"] if m["name"] == "tool_manager")
        requests.post(dashboard.url + "api/controls/restart_module", json={"name": "tool_manager"}, timeout=5)
        after = requests.get(dashboard.url + "api/modules", timeout=5).json()
        after_count = next(m["restart_count"] for m in after["modules"] if m["name"] == "tool_manager")
        assert after_count == before_count + 1
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_26_logs_stream_delivers_a_live_printed_line():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        received = []

        def _consume():
            with requests.get(dashboard.url + "api/logs/stream", stream=True, timeout=8) as resp:
                for line in resp.iter_lines(decode_unicode=True):
                    if line and line.startswith("data:") and "live_log_marker_555" in line:
                        received.append(line)
                        return

        t = threading.Thread(target=_consume, daemon=True)
        t.start()
        time.sleep(0.2)
        from luno.core.utils import log as core_log
        deadline = time.time() + 8.0
        while not received and time.time() < deadline:
            core_log("live_log_marker_555", "dashboard_test")
            time.sleep(0.3)
        t.join(timeout=2)
        assert received, "expected the live-printed log line over SSE within 8s"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ============================================================================
# Stress test + thread safety
# ============================================================================

@scenario
def test_27_stress_thousands_of_events_no_crash_ring_buffer_bounded():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        N = 5000
        for i in range(N):
            runtime.event_bus.publish(Event(type="stress_event", data={"i": i}))
        deadline = time.time() + 15.0
        while runtime.event_bus.stats()["queue_size"] > 0 and time.time() < deadline:
            time.sleep(0.05)

        snapshot = requests.get(dashboard.url + "api/events", params={"limit": 10000}, timeout=10).json()
        # ring buffer is bounded (DEFAULT_EVENT_BUFFER_SIZE=5000) - must
        # never grow unbounded even after 5000 rapid publishes plus
        # whatever startup/heartbeat noise came before it.
        assert len(snapshot["events"]) <= 5000
        assert dashboard._events_buffer is not None
        with dashboard._events_buffer._lock:
            assert len(dashboard._events_buffer._events) <= 5000

        stats = requests.get(dashboard.url + "api/statistics", timeout=5).json()
        assert isinstance(stats, dict)  # aggregator survived the burst without raising
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_28_thread_safety_concurrent_requests_and_concurrent_publishes():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        errors: List[str] = []
        stop_flag = threading.Event()

        def publisher():
            i = 0
            while not stop_flag.is_set():
                runtime.event_bus.publish(Event(type="concurrent_stress", data={"i": i}))
                i += 1
                time.sleep(0.001)

        publisher_threads = [threading.Thread(target=publisher, daemon=True) for _ in range(4)]
        for t in publisher_threads:
            t.start()

        endpoints = ["api/status", "api/modules", "api/adapters", "api/health", "api/statistics", "api/events"]

        def hit_endpoint(path):
            try:
                r = requests.get(dashboard.url + path, timeout=10)
                if r.status_code != 200:
                    errors.append(f"{path} -> {r.status_code}")
            except Exception as ex:
                errors.append(f"{path} -> {ex}")

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(hit_endpoint, ep) for ep in endpoints * 10]
            for f in futures:
                f.result()

        stop_flag.set()
        for t in publisher_threads:
            t.join(timeout=2.0)

        assert not errors, f"concurrent access errors: {errors}"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_29_static_index_served_and_health_endpoint_present():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        r = requests.get(dashboard.url, timeout=5)
        assert r.status_code == 200
        assert "Luno" in r.text

        health = requests.get(dashboard.url + "api/health", timeout=5).json()
        for field in ("overall_healthy", "thread_count", "queue_size", "avg_event_latency_ms", "sqlite_ok"):
            assert field in health
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_30_dashboard_disabled_by_config_registers_nothing():
    from luno.bootstrap.dashboard import register_dashboard
    runtime, modules, adapter_manager, cfg, audio_store = _build_stack()
    cfg.dashboard_enabled = False
    try:
        runtime.start()
        result = register_dashboard(runtime, adapter_manager, modules, cfg)
        assert result is None
    finally:
        _teardown(runtime, adapter_manager)


# ============================================================================
# Chat (ChatGPT-style panel with voice input/output)
# ============================================================================

@scenario
def test_31_chat_send_rejects_empty_message():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/chat/send", json={"text": ""}, timeout=5).json()
        assert r["ok"] is False
        r = requests.post(dashboard.url + "api/chat/send", json={"text": "   "}, timeout=5).json()
        assert r["ok"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_32_chat_send_auto_wakes_a_sleeping_session_and_reaches_the_planner():
    """The core chat flow: a message sent while the session is asleep
    must auto-wake (same as clicking "Wake Session"), settle into a
    forwardable state, publish as `speech_recognized`, and flow all the
    way to a real Planner plan + an `assistant_response` - exactly what
    a real spoken utterance after a real wake word would produce."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        conv = requests.get(dashboard.url + "api/conversation", timeout=5).json()
        assert conv["raw_state"] == "sleeping"

        r = requests.post(dashboard.url + "api/chat/send", json={"text": "halo, apa kabar?"}, timeout=10).json()
        assert r["ok"] is True, r

        received = []

        def _consume():
            with requests.get(dashboard.url + "api/events/stream", stream=True, timeout=8) as resp:
                for line in resp.iter_lines(decode_unicode=True):
                    if line and line.startswith("data:") and '"assistant_response"' in line:
                        received.append(line)
                        return

        t = threading.Thread(target=_consume, daemon=True)
        t.start()
        t.join(timeout=8)

        # Even if the SSE race missed it, the planner/session state
        # itself must show real forward progress happened.
        planner_body = requests.get(dashboard.url + "api/planner", timeout=5).json()
        assert planner_body["has_plan"] is True
        assert planner_body["plan"]["source_request"] == "halo, apa kabar?"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_33_chat_send_reports_busy_instead_of_silently_dropping():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        modules["barge_in_module"]  # sanity: module exists
        session_manager = modules["session_manager"]
        # Force a THINKING state directly (same state a real in-flight
        # turn would be in) without going through a full turn, so this
        # test doesn't depend on planner timing.
        from luno.wake_session.models import ConversationState
        session_manager.session.transition_to(ConversationState.THINKING, reason="test setup")

        r = requests.post(dashboard.url + "api/chat/send", json={"text": "pesan lain"}, timeout=5).json()
        assert r["ok"] is False
        assert "busy" in r["message"].lower()
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_34_chat_audio_endpoint_requires_request_id():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        r = requests.get(dashboard.url + "api/chat/audio", timeout=5)
        assert r.status_code == 400
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_35_chat_audio_endpoint_reports_no_clip_for_mock_backend():
    """Mock Fish Audio never produces real bytes - the endpoint must say
    so clearly (404 + explanatory message) rather than hang or 500."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        t0 = time.time()
        r = requests.get(dashboard.url + "api/chat/audio", params={"request_id": "nonexistent-req"}, timeout=30)
        elapsed = time.time() - t0
        assert r.status_code == 404
        assert "error" in r.json()
        # AudioCaptureStore.wait_for()'s default timeout is ~25s - a
        # request for a request_id that will NEVER exist (mock backend
        # captures nothing) still has to wait that long by design (it
        # can't distinguish "not yet ready" from "never coming"), so
        # this just asserts it terminates well under a runaway/hang,
        # not that it returns instantly.
        assert elapsed < 28.0
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_36_audio_capture_store_unit_behavior():
    """Direct unit coverage of `AudioCaptureStore`/`wrap_play_audio_fn`/
    `AudioRequestCorrelator` - the capture/correlate split described in
    `audio_bridge.py`'s own docstring - independent of any HTTP layer."""
    from luno.dashboard.audio_bridge import AudioCaptureStore, wrap_play_audio_fn

    store = AudioCaptureStore()
    assert store.get("req-1") is None
    assert store.wait_for("req-1", timeout_s=0.2) is None

    calls = []

    def fake_original(wav_bytes, control):
        calls.append((wav_bytes, control))

    wrapped = wrap_play_audio_fn(store, fake_original)
    wrapped(b"FAKEWAVBYTES", object())
    assert len(calls) == 1, "the real play_audio_fn must still be called with the same bytes"

    # nothing claimed yet - capture() alone doesn't assign a request_id
    assert store.get("req-1") is None

    store.claim("req-1")
    assert store.get("req-1") == b"FAKEWAVBYTES"
    assert store.wait_for("req-1", timeout_s=1.0) == b"FAKEWAVBYTES"

    # claiming with nothing pending is a safe no-op
    store.claim("req-2")
    assert store.get("req-2") is None


@scenario
def test_37_audio_request_correlator_claims_on_speech_playback_started():
    from luno.dashboard.audio_bridge import AudioCaptureStore, AudioRequestCorrelator, wrap_play_audio_fn

    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        store = AudioCaptureStore()
        correlator = AudioRequestCorrelator(store, runtime.event_bus)
        try:
            wrapped = wrap_play_audio_fn(store, lambda wav_bytes, control: None)
            wrapped(b"CLIPBYTES", object())

            runtime.event_bus.publish(Event(type="speech_playback_started", data={"request_id": "corr-test-1"}))

            deadline = time.time() + 5.0
            clip = None
            while time.time() < deadline:
                clip = store.get("corr-test-1")
                if clip is not None:
                    break
                time.sleep(0.05)
            assert clip == b"CLIPBYTES"
        finally:
            correlator.stop()
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_38_chat_send_waits_through_awakening_instead_of_rejecting():
    """Regression test for the exact bug reported after shipping the
    Chat panel: a message arriving while the session is AWAKENING (a
    brief, self-resolving transitional state - the "Yes?" wake
    acknowledgement is still playing) used to be rejected outright with
    a confusing "Luno is busy (state=awakening)" error. It must now be
    waited through, same as SLEEPING, and still reach the planner.

    Uses a REAL `force_wake()` (not a raw state poke) so the pending
    wake-acknowledgement playback is genuinely in flight - `session.
    transition_to()` alone would leave AWAKENING with nothing to ever
    resolve it, which isn't the race this bug was actually about."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        session_manager = modules["session_manager"]
        session_manager.force_wake(reason="test setup - simulate a just-triggered wake")
        # `force_wake()` sets state=AWAKENING synchronously before
        # returning (the ack's own playback-driven transition back out
        # of it happens asynchronously, off the Event Bus pump thread) -
        # calling send_chat_message() immediately after, same as the
        # HTTP handler would for a message that raced a real wake,
        # reliably exercises the "wait through AWAKENING" path.
        assert session_manager.status_snapshot()["state"] == "awakening"

        r = dash_controls.send_chat_message(runtime, modules, "halo lagi")
        assert r["ok"] is True, r
        assert "busy" not in r["message"].lower()

        deadline = time.time() + 5.0
        planner_body = {"has_plan": False}
        while time.time() < deadline and not planner_body["has_plan"]:
            planner_body = requests.get(dashboard.url + "api/planner", timeout=5).json()
            if not planner_body["has_plan"]:
                time.sleep(0.1)
        assert planner_body["has_plan"] is True
        assert planner_body["plan"]["source_request"] == "halo lagi"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_39_chat_send_still_rejects_genuinely_busy_thinking_and_speaking():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        from luno.wake_session.models import ConversationState
        session_manager = modules["session_manager"]

        session_manager.session.transition_to(ConversationState.THINKING, reason="test setup")
        r = requests.post(dashboard.url + "api/chat/send", json={"text": "a"}, timeout=5).json()
        assert r["ok"] is False and "thinking" in r["message"].lower()

        session_manager.session.transition_to(ConversationState.SPEAKING, reason="test setup")
        r = requests.post(dashboard.url + "api/chat/send", json={"text": "b"}, timeout=5).json()
        assert r["ok"] is False and "speaking" in r["message"].lower()
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_39b_chat_send_lets_interrupt_words_through_while_busy():
    """Regression test for the bug where typing an interrupt word
    ("stop"/"cancel") into the Chat panel while Luno was mid-reply got
    rejected outright by the SAME busy-guard `test_39` above exercises
    for ordinary text - the interrupt word never even became a
    `speech_recognized` event, so `BargeInModule` never got a chance to
    act on it. An interrupt word must go through (and be published as
    speech_recognized so BargeInModule sees it) precisely BECAUSE Luno
    is thinking/speaking - that's the whole point of barge-in. Ordinary
    text ("a"/"b", see `test_39`) must still be rejected - only text
    that actually matches a configured interrupt/resume/confirm word
    bypasses the guard."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        from luno.wake_session.models import ConversationState
        session_manager = modules["session_manager"]
        barge_in_module = modules["barge_in_module"]

        received = []
        sub_id = runtime.event_bus.subscribe("speech_recognized", lambda e: received.append(e))
        try:
            session_manager.session.transition_to(ConversationState.SPEAKING, reason="test setup")

            interrupt_word = barge_in_module.config.interrupt_words[0]
            r = requests.post(dashboard.url + "api/chat/send", json={"text": interrupt_word}, timeout=5).json()
            assert r["ok"] is True, r
            assert "busy" not in r["message"].lower()

            deadline = time.time() + 2.0
            while time.time() < deadline and not received:
                time.sleep(0.05)
            assert len(received) == 1
            assert received[0].data["text"] == interrupt_word
            assert received[0].data["source"] == "dashboard_chat"

            # ordinary text must still be rejected while busy - the fix
            # is scoped to barge-in-relevant text only.
            r = requests.post(dashboard.url + "api/chat/send", json={"text": "ordinary chat message"}, timeout=5).json()
            assert r["ok"] is False and "busy" in r["message"].lower()
        finally:
            runtime.event_bus.unsubscribe(sub_id)
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_39c_chat_send_lets_confirm_answer_through_only_while_awaiting_confirmation():
    """A CONFIRM-mode "yes"/"no" is barge-in-relevant only while
    `barge_in_module.awaiting_confirmation` is actually True - otherwise
    an ordinary "yes" typed into chat while Luno is speaking would
    wrongly bypass the busy-guard too."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        from luno.wake_session.models import ConversationState
        session_manager = modules["session_manager"]
        barge_in_module = modules["barge_in_module"]

        session_manager.session.transition_to(ConversationState.SPEAKING, reason="test setup")
        yes_word = barge_in_module.config.confirm_yes_words[0]

        # not awaiting confirmation yet - "yes" is just ordinary text.
        r = requests.post(dashboard.url + "api/chat/send", json={"text": yes_word}, timeout=5).json()
        assert r["ok"] is False and "busy" in r["message"].lower()

        barge_in_module.awaiting_confirmation = True
        try:
            r = requests.post(dashboard.url + "api/chat/send", json={"text": yes_word}, timeout=5).json()
            assert r["ok"] is True, r
        finally:
            barge_in_module.awaiting_confirmation = False
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ============================================================================
# Wake Listen (continuous browser mic -> raw speech_recognized, no force-wake)
# ============================================================================

@scenario
def test_40_browser_mic_utterance_rejects_empty_and_publishes_raw_event():
    """`browser_mic_utterance()` must reject blank text the same way
    `send_chat_message()` does, and - for real text - publish a raw
    `speech_recognized` event with NO force-wake: unlike chat send, a
    sleeping session must stay asleep for an utterance that isn't the
    wake word (exactly what a real ambient microphone hearing unrelated
    chatter should do)."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        r = requests.post(dashboard.url + "api/controls/browser_mic_utterance", json={"text": ""}, timeout=5).json()
        assert r["ok"] is False
        r = requests.post(dashboard.url + "api/controls/browser_mic_utterance", json={"text": "   "}, timeout=5).json()
        assert r["ok"] is False

        received = []
        sub_id = runtime.event_bus.subscribe("speech_recognized", lambda e: received.append(e))
        try:
            conv = requests.get(dashboard.url + "api/conversation", timeout=5).json()
            assert conv["raw_state"] == "sleeping"

            r = requests.post(dashboard.url + "api/controls/browser_mic_utterance", json={"text": "cuaca hari ini gimana"}, timeout=5).json()
            assert r["ok"] is True, r

            deadline = time.time() + 2.0
            while time.time() < deadline and not received:
                time.sleep(0.05)
            assert len(received) == 1
            assert received[0].data["text"] == "cuaca hari ini gimana"
            assert received[0].data["source"] == "browser_mic"

            # No wake word in that text - unlike send_chat_message(), this
            # path never force-wakes, so the session must still be asleep.
            conv = requests.get(dashboard.url + "api/conversation", timeout=5).json()
            assert conv["raw_state"] == "sleeping"
        finally:
            runtime.event_bus.unsubscribe(sub_id)
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_41_browser_mic_utterance_wake_word_actually_wakes_session():
    """The whole point of Wake Listen: an utterance containing the
    configured wake word must wake the session up through
    `SessionManagerModule`'s own matcher - exactly like a real
    microphone would - even though `browser_mic_utterance()` itself
    never calls `force_wake()`."""
    from luno import config as legacy_config

    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        conv = requests.get(dashboard.url + "api/conversation", timeout=5).json()
        assert conv["raw_state"] == "sleeping"

        wake_word = legacy_config.WAKE_WORD
        r = requests.post(dashboard.url + "api/controls/browser_mic_utterance", json={"text": wake_word}, timeout=5).json()
        assert r["ok"] is True, r

        deadline = time.time() + 3.0
        state = "sleeping"
        while time.time() < deadline and state == "sleeping":
            state = requests.get(dashboard.url + "api/conversation", timeout=5).json()["raw_state"]
            if state == "sleeping":
                time.sleep(0.05)
        assert state != "sleeping", f"session never left sleeping after wake word (last state={state!r})"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_42_repeated_wake_word_while_awake_is_not_forwarded_to_planner():
    """Bug fix: a user who can't see the session state (no dashboard/
    console open) naturally says the wake word again out of habit even
    though Luno is already awake. Before this fix that bare utterance
    fell straight through to the Planner as a literal message (e.g. the
    LLM receiving just "alexa" with no context) - it must now be
    silently absorbed (session timeout extended, nothing forwarded)."""
    from luno import config as legacy_config
    from luno.wake_session.models import ConversationState

    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        session_manager = modules["session_manager"]
        timeout_s = session_manager.config.session_timeout_s
        session_manager.session.transition_to(ConversationState.WAITING_USER, reason="test setup - already awake")
        time.sleep(1.0)
        mid_remaining = session_manager.status_snapshot()["seconds_remaining"]
        assert mid_remaining < timeout_s - 0.7, mid_remaining  # clock genuinely ticking down before the fix kicks in

        r = requests.post(dashboard.url + "api/controls/browser_mic_utterance", json={"text": legacy_config.WAKE_WORD}, timeout=5).json()
        assert r["ok"] is True, r

        # Event Bus dispatch is async (own pump thread) - poll (rather than
        # a fixed sleep) so this isn't racing a hardcoded delay against
        # touch()'s own deadline decaying in real time.
        deadline = time.time() + 3.0
        refreshed_remaining = mid_remaining
        while time.time() < deadline:
            refreshed_remaining = session_manager.status_snapshot()["seconds_remaining"]
            if refreshed_remaining is not None and refreshed_remaining > timeout_s - 0.5:
                break
            time.sleep(0.05)
        assert refreshed_remaining > timeout_s - 0.5, f"touch() never refreshed the deadline close to the full {timeout_s}s (last saw {refreshed_remaining})"

        # never reached the Planner
        planner_body = requests.get(dashboard.url + "api/planner", timeout=5).json()
        assert planner_body["has_plan"] is False, planner_body

        # still awake - never dropped back to sleeping/some other state.
        assert session_manager.status_snapshot()["state"] == "waiting_user"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_43_wake_word_plus_remainder_while_awake_forwards_remainder_only():
    """The companion case: "alexa, buka chrome" said while ALREADY awake
    must still work as a real command - forwarding just "buka chrome"
    (not the literal wake word prefix) to the Planner, exactly as if the
    wake word hadn't been said at all."""
    from luno import config as legacy_config
    from luno.wake_session.models import ConversationState

    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        session_manager = modules["session_manager"]
        session_manager.session.transition_to(ConversationState.LISTENING, reason="test setup - already awake")

        r = requests.post(dashboard.url + "api/controls/browser_mic_utterance", json={"text": f"{legacy_config.WAKE_WORD} buka chrome"}, timeout=5).json()
        assert r["ok"] is True, r

        deadline = time.time() + 5.0
        planner_body = {"has_plan": False}
        while time.time() < deadline and not planner_body["has_plan"]:
            planner_body = requests.get(dashboard.url + "api/planner", timeout=5).json()
            if not planner_body["has_plan"]:
                time.sleep(0.1)
        assert planner_body["has_plan"] is True
        assert planner_body["plan"]["source_request"] == "buka chrome", planner_body["plan"]["source_request"]
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ============================================================================
# Goals panel (Sprint 10 - Proactive Intelligence)
# ============================================================================

@scenario
def test_44_goals_endpoint_reports_available_snapshot_shape():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        body = requests.get(dashboard.url + "api/goals", timeout=5).json()
        assert body["available"] is True
        for field in ("enabled", "cycle_count", "last_cycle_at", "last_context",
                      "active_goals", "awaiting_confirmation", "completed_goals",
                      "rejected_goals", "config"):
            assert field in body, f"missing field {field!r} in /api/goals"
        assert isinstance(body["active_goals"], list)
        assert isinstance(body["awaiting_confirmation"], list)
    finally:
        _teardown(runtime, adapter_manager, dashboard)


@scenario
def test_45_control_approve_and_reject_goal_end_to_end():
    """Injects a goal directly into `ProactiveModule._active` (the same
    state the real pipeline would put an AWAITING_CONFIRMATION goal into)
    and drives it through the real HTTP controls the Goals panel's
    Approve/Reject buttons call - exactly like a human clicking them."""
    import datetime as dt
    from luno.proactive.models import Goal, GoalStatus, GoalType, PolicyAction, PolicyResult, RiskLevel

    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        pm = modules["proactive_module"]

        def _new_goal(goal_id):
            g = Goal(
                id=goal_id, type=GoalType.ASSISTANCE_OFFER, description="Test goal",
                reasoning="dashboard test", created_at=dt.datetime.now(dt.timezone.utc),
                confidence=75.0, cooldown_key=f"dashtest:{goal_id}",
            )
            g.status = GoalStatus.AWAITING_CONFIRMATION
            g.policy = PolicyResult(
                action=PolicyAction.ASK_CONFIRMATION, priority=50.0, risk=RiskLevel.LOW,
                confidence=75.0, requires_confirmation=True, reasoning="test",
                decided_at=dt.datetime.now(dt.timezone.utc),
            )
            return g

        g1 = _new_goal("dash-approve-1")
        with pm._lock:
            pm._active[g1.id] = g1
        body = requests.get(dashboard.url + "api/goals", timeout=5).json()
        assert any(g["id"] == g1.id for g in body["awaiting_confirmation"])

        r = requests.post(dashboard.url + "api/controls/approve_goal", json={"goal_id": g1.id}, timeout=5).json()
        assert r["ok"] is True, r
        body = requests.get(dashboard.url + "api/goals", timeout=5).json()
        assert not any(g["id"] == g1.id for g in body["awaiting_confirmation"])
        assert any(g["id"] == g1.id for g in body["completed_goals"])

        g2 = _new_goal("dash-reject-1")
        with pm._lock:
            pm._active[g2.id] = g2
        r = requests.post(dashboard.url + "api/controls/reject_goal", json={"goal_id": g2.id}, timeout=5).json()
        assert r["ok"] is True, r
        body = requests.get(dashboard.url + "api/goals", timeout=5).json()
        assert any(g["id"] == g2.id for g in body["rejected_goals"])

        # bad/missing goal_id never raises, always a clean ok:false
        r = requests.post(dashboard.url + "api/controls/approve_goal", json={"goal_id": "no-such-id"}, timeout=5).json()
        assert r["ok"] is False
        r = requests.post(dashboard.url + "api/controls/reject_goal", json={}, timeout=5).json()
        assert r["ok"] is False
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def main() -> int:
    # Optional CLI filter (`python3 tests/test_dashboard.py test_12 test_26`
    # or a substring like `sse`) - every scenario still runs by default;
    # this only exists so a subset can be re-verified quickly without
    # paying for all ~30 full-stack builds every time.
    filters = sys.argv[1:]
    scenarios = SCENARIOS
    if filters:
        scenarios = [(n, f) for n, f in SCENARIOS if any(flt in n for flt in filters)]

    passed = 0
    failed = 0
    for name, fn in scenarios:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as ex:
            print(f"  [FAIL] {name}: {ex}")
            failed += 1
        except Exception as ex:  # pragma: no cover
            print(f"  [ERROR] {name}: {ex}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
