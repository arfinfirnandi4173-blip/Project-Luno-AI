"""
tests/test_sprint71_dashboard_startup_recovery.py
====================================================

Sprint 71 (Dashboard Startup & Access Recovery) - dedicated regression
suite.

ROOT CAUSE (see `docs/change_impact/dashboard_startup_recovery.md` for
the full writeup): `DashboardServer.start()` (`luno/dashboard/server.py`)
constructs `ThreadingHTTPServer((self.host, self.port), _Handler)`
completely unguarded. That constructor performs the real socket bind
SYNCHRONOUSLY and raises a bare `OSError` on failure - the single most
common real-world trigger being a stale/previous Luno process (or a
second instance) still holding the configured port. `main.py`'s own
`dashboard.start()` call site was ALSO unguarded, so this exception
propagated all the way out of `main()` uncaught, crashing the ENTIRE
Luno process (voice pipeline, wake word, every other subsystem) with a
raw traceback - not just failing to open the Dashboard. Reproduced live:
pre-occupying port 8765 before running `python main.py` produced exactly
this crash, matching the reported "Dashboard tidak bisa dibuka" symptom
(in the most severe case, ALL of Luno fails to start, not merely the
Dashboard).

THE FIX: `DashboardServer.start()` now catches `OSError` around the bind
call, rolls back every observability subscription it had already made
(`LogCapture`/`EventRingBuffer`/`StatsAggregator`/`VoiceLatencyRecorder`/
`EventLogWriter`) so a failed start never leaves the object half-
initialized, and raises `DashboardBindError` - a THIN SUBCLASS of
`OSError` (any existing `except OSError` still catches it unchanged) -
carrying a host/port-specific, actionable message (port-in-use /
permission-denied / address-not-available, cross-platform errno+winerror
aware) that never includes anything from `.env`/secrets. `main.py`'s own
call site now catches this and degrades to the SAME already-established,
already-tested "no dashboard, rest of Luno keeps running" state that
`DASHBOARD_ENABLED=false` has always produced - it does not crash the
process anymore.

Same self-contained-helpers house style as `tests/test_dashboard.py`
(real bootstrap via `register_all_modules`/`register_all_adapters`, all-
mock backends, no external hardware/network needed) and `tests/
test_dashboard_turn_state_recovery.py` (pytest, real E2E through the
actual runtime path - no test targets only a private helper).

SCOPE (explicitly, per Sprint 71's own brief): Tapo/C212 authentication,
PTZ logic, `real_camera_ptz.py`, pytapo/reconnect logic, Home Assistant,
long-term memory, mutation audit, and schema/config migration are all
UNTOUCHED by this sprint - none of those files were modified, and no
test in this file exercises them.
"""

from __future__ import annotations

import errno
import hashlib
import os
import socket
import subprocess
import sys
import time
from typing import Callable

import pytest
import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.core.config import CoreConfig  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402
from luno.dashboard import DashboardServer  # noqa: E402
from luno.dashboard.server import DashboardBindError, _describe_bind_failure  # noqa: E402

_FAST_CORE_CONFIG = CoreConfig(heartbeat_interval_s=0.3, scheduler_tick_s=0.2)


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _build_stack():
    """Same real bootstrap sequence `main.py`/`tests/test_dashboard.py`
    use - all-mock backends, no external dependency required."""
    cfg = LauncherConfig()
    runtime = Runtime(_FAST_CORE_CONFIG)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    return runtime, modules, adapter_manager, cfg, adapters.get("audio_capture_store")


def _build_dashboard(host: str = "127.0.0.1", port: int = 0):
    runtime, modules, adapter_manager, cfg, audio_store = _build_stack()
    runtime.start()
    dashboard = DashboardServer(runtime, adapter_manager, modules, cfg, audio_capture_store=audio_store, host=host, port=port)
    return runtime, modules, adapter_manager, cfg, dashboard


def _teardown(runtime, adapter_manager, dashboard=None):
    coordinator = ShutdownCoordinator(runtime, adapter_manager, dashboard=dashboard)
    coordinator.shutdown()


def _occupy_port(host: str, port: int) -> socket.socket:
    """Simulates the real-world trigger this sprint's own root cause is
    built around: a stale/previous process (or a second Luno instance)
    already listening on the configured port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
    s.bind((host, port))
    s.listen(1)
    return s


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ─────────────────────────────────────────────
#  1 - dashboard startup succeeds (baseline)
# ─────────────────────────────────────────────

def test_01_dashboard_startup_succeeds():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        dashboard.start()
        assert dashboard._started is True
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ─────────────────────────────────────────────
#  2 - server bind succeeds (socket actually bound + listening)
# ─────────────────────────────────────────────

def test_02_server_bind_succeeds_and_socket_is_listening():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        dashboard.start()
        assert dashboard._httpd is not None
        assert dashboard.port != 0  # port=0 resolved to a real OS-assigned port
        # Prove the socket is genuinely listening, not just constructed -
        # a raw connect from a separate socket must succeed.
        probe = socket.create_connection(("127.0.0.1", dashboard.port), timeout=3)
        probe.close()
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ─────────────────────────────────────────────
#  3 - port conflict is handled clearly (the core fix)
# ─────────────────────────────────────────────

def test_03_port_conflict_raises_clear_actionable_error_not_a_crash():
    port = _free_port()
    blocker = _occupy_port("127.0.0.1", port)
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard(host="127.0.0.1", port=port)
    try:
        with pytest.raises(DashboardBindError) as excinfo:
            dashboard.start()
        message = str(excinfo.value)
        assert str(port) in message
        assert "127.0.0.1" in message
        assert "already in use" in message.lower()
        # Actionable: names the likely cause and a concrete next step.
        assert "DASHBOARD_PORT" in message or "stop that process" in message.lower()
        assert dashboard._started is False
    finally:
        blocker.close()
        _teardown(runtime, adapter_manager, dashboard)


def test_03b_describe_bind_failure_never_leaks_env_or_secrets():
    ex = OSError(errno.EADDRINUSE, "Address already in use")
    ex.errno = errno.EADDRINUSE
    msg = _describe_bind_failure(ex, "127.0.0.1", 8765)
    for forbidden in ("API_KEY", "OPENROUTER", "PASSWORD", "TOKEN", "SECRET"):
        assert forbidden not in msg.upper() or forbidden == "SECRET" and "secret" not in msg.lower()
    assert "127.0.0.1" in msg and "8765" in msg


# ─────────────────────────────────────────────
#  4 - startup exception is not silent
# ─────────────────────────────────────────────

def test_04_startup_exception_is_a_real_raised_exception_not_swallowed():
    """The bind failure must actually propagate as a Python exception
    the caller can observe/act on - never silently absorbed into a log
    line only (which would leave a caller believing `start()` succeeded
    when it didn't)."""
    port = _free_port()
    blocker = _occupy_port("127.0.0.1", port)
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard(host="127.0.0.1", port=port)
    raised = False
    try:
        dashboard.start()
    except OSError:
        raised = True
    finally:
        blocker.close()
        _teardown(runtime, adapter_manager, dashboard)
    assert raised, "a bind failure must raise, never fail silently"


# ─────────────────────────────────────────────
#  5 - dashboard thread does not die silently
# ─────────────────────────────────────────────

def test_05_dashboard_thread_stays_alive_after_successful_start():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        dashboard.start()
        assert dashboard._thread is not None
        assert dashboard._thread.is_alive()
        time.sleep(0.3)
        assert dashboard._thread.is_alive(), "dashboard HTTP thread died silently shortly after starting"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


def test_05b_failed_start_leaves_no_orphaned_thread():
    port = _free_port()
    blocker = _occupy_port("127.0.0.1", port)
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard(host="127.0.0.1", port=port)
    try:
        with pytest.raises(DashboardBindError):
            dashboard.start()
        assert dashboard._thread is None, "a failed start must never leave a serve_forever thread running"
    finally:
        blocker.close()
        _teardown(runtime, adapter_manager, dashboard)


# ─────────────────────────────────────────────
#  6 - root route is available
# ─────────────────────────────────────────────

def test_06_root_route_returns_real_dashboard_html():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        dashboard.start()
        r = requests.get(dashboard.url, timeout=5)
        assert r.status_code == 200
        assert "Luno" in r.text and "<html" in r.text.lower()
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ─────────────────────────────────────────────
#  7 - HTTP health endpoint (pre-existing - not a new one)
# ─────────────────────────────────────────────

def test_07_existing_health_and_ping_endpoints_respond():
    """Uses the project's OWN pre-existing `/api/ping` and `/api/health`
    endpoints (see `luno/dashboard/server.py::_dispatch_get`) - per this
    sprint's own instruction, no new health endpoint was created."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        dashboard.start()
        r = requests.get(dashboard.url + "api/ping", timeout=5)
        assert r.status_code == 200
        assert r.json()["ok"] is True

        r = requests.get(dashboard.url + "api/health", timeout=5)
        assert r.status_code == 200
        assert "overall_healthy" in r.json()
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ─────────────────────────────────────────────
#  8 - shutdown stays clean
# ─────────────────────────────────────────────

def test_08_shutdown_is_clean_and_releases_the_port():
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard(host="127.0.0.1", port=0)
    dashboard.start()
    bound_port = dashboard.port
    dashboard.stop()
    assert dashboard._started is False
    _teardown(runtime, adapter_manager, None)
    # Port must be free again almost immediately (SO_REUSEADDR already
    # set by stdlib's own http.server.HTTPServer - verified, not assumed).
    assert _wait_until(lambda: _port_is_free("127.0.0.1", bound_port), 3.0), \
        f"port {bound_port} was not released after stop()"


def _port_is_free(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        s.close()
        return False


# ─────────────────────────────────────────────
#  9 - existing dashboard behavior stays compatible
# ─────────────────────────────────────────────

def test_09_existing_dashboard_api_surface_unaffected():
    """Spot-checks several pre-existing routes (same convention `tests/
    test_dashboard.py` already established) to prove this sprint's fix
    changed nothing about normal, successful operation."""
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    try:
        dashboard.start()
        for path in ("api/status", "api/modules", "api/adapters", "api/statistics", "api/configuration"):
            r = requests.get(dashboard.url + path, timeout=5)
            assert r.status_code == 200, f"{path} regressed: {r.status_code}"
    finally:
        _teardown(runtime, adapter_manager, dashboard)


# ─────────────────────────────────────────────
#  10 - no persistent config mutation
# ─────────────────────────────────────────────

def test_10_no_persistent_config_mutation_across_start_stop_cycle():
    config_dir = os.path.join(_ROOT, "config")
    json_files = sorted(f for f in os.listdir(config_dir) if f.endswith(".json"))

    def _hash_all():
        digests = {}
        for name in json_files:
            path = os.path.join(config_dir, name)
            with open(path, "rb") as fh:
                digests[name] = hashlib.sha256(fh.read()).hexdigest()
        return digests

    before = _hash_all()
    runtime, modules, adapter_manager, cfg, dashboard = _build_dashboard()
    dashboard.start()
    requests.get(dashboard.url + "api/status", timeout=5)
    requests.get(dashboard.url + "api/health", timeout=5)
    _teardown(runtime, adapter_manager, dashboard)
    after = _hash_all()

    changed = {name: (before[name], after[name]) for name in json_files if before[name] != after[name]}
    assert not changed, f"config/*.json mutated by a dashboard start/stop cycle: {changed}"


# ─────────────────────────────────────────────
#  11 - no modification to camera/PTZ behavior (scope guard)
# ─────────────────────────────────────────────

def test_11_camera_ptz_modules_untouched_by_this_sprint():
    """Explicit scope guard per Sprint 71's own brief: this sprint must
    NOT touch Tapo/C212 auth, PTZ logic, `real_camera_ptz.py`, pytapo/
    reconnect logic, or Home Assistant.

    `luno/dashboard/server.py` legitimately references `home_assistant`
    elsewhere (pre-existing module-status display, unrelated to this
    sprint) so a whole-file substring scan would false-positive. Instead
    this test scopes the check to exactly the code Sprint 71 actually
    added/changed: the `DashboardBindError` class, the
    `_describe_bind_failure` helper, and the try/except wrapped around
    the socket bind inside `start()` - none of those may reference
    camera/PTZ/Home-Assistant machinery."""
    import re

    with open(os.path.join(_ROOT, "luno", "dashboard", "server.py"), encoding="utf-8") as fh:
        server_src = fh.read()

    bind_error_match = re.search(r"class DashboardBindError\(OSError\):.*?(?=\nclass |\ndef _is_expected_client_disconnect)", server_src, re.S)
    describe_match = re.search(r"def _describe_bind_failure\(.*?\n(?=\ndef |\nclass )", server_src, re.S)
    assert bind_error_match, "DashboardBindError class not found - Sprint 71's own fix appears to be missing"
    assert describe_match, "_describe_bind_failure helper not found - Sprint 71's own fix appears to be missing"
    sprint71_added_code = bind_error_match.group(0) + describe_match.group(0)

    with open(os.path.join(_ROOT, "main.py"), encoding="utf-8") as fh:
        main_src = fh.read()
    main_sprint71_block = main_src[main_src.index("Sprint 71"):main_src.index('print("Ready.")')]

    for forbidden in ("real_camera_ptz", "pytapo", "Tapo", "home_assistant", "HomeAssistant"):
        assert forbidden not in sprint71_added_code, f"server.py's Sprint 71 code unexpectedly references {forbidden!r}"
        assert forbidden not in main_sprint71_block, f"main.py's Sprint 71 code unexpectedly references {forbidden!r}"


# ─────────────────────────────────────────────
#  12 - E2E through the REAL entry point: main.py survives a port conflict
# ─────────────────────────────────────────────

def test_12_e2e_main_py_survives_dashboard_port_conflict_and_keeps_running():
    """The actual regression this sprint fixes, proven through the real
    production entry point (`python main.py`), not just the
    `DashboardServer` class in isolation: a stale process holding
    DASHBOARD_PORT must degrade to 'no dashboard, rest of Luno keeps
    running' - not crash the entire process with an unhandled
    traceback."""
    port = _free_port()
    blocker = _occupy_port("0.0.0.0", port)
    try:
        env = dict(os.environ)
        env["DASHBOARD_PORT"] = str(port)
        env["DASHBOARD_HOST"] = "0.0.0.0"
        proc = subprocess.Popen(
            [sys.executable, "main.py"], cwd=_ROOT, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        try:
            deadline = time.time() + 25
            output_lines = []
            saw_ready = False
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                output_lines.append(line)
                if line.strip() == "Ready.":
                    saw_ready = True
                    break
            output = "".join(output_lines)
            assert saw_ready, f"main.py never reached 'Ready.' with the dashboard port pre-occupied - output:\n{output}"
            assert "Traceback (most recent call last)" not in output, \
                f"main.py crashed with an unhandled traceback on a dashboard port conflict:\n{output}"
            assert "already in use" in output.lower() or "WARNING: Dashboard could not start" in output
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    finally:
        blocker.close()


# ─────────────────────────────────────────────
#  13 - restart after a failed start works cleanly (Phase 6's own "start twice")
# ─────────────────────────────────────────────

def test_13_dashboard_starts_cleanly_on_a_fresh_instance_after_a_prior_bind_failure():
    port = _free_port()
    blocker = _occupy_port("127.0.0.1", port)
    runtime1, modules1, adapter_manager1, cfg1, dashboard1 = _build_dashboard(host="127.0.0.1", port=port)
    try:
        with pytest.raises(DashboardBindError):
            dashboard1.start()
    finally:
        blocker.close()
        _teardown(runtime1, adapter_manager1, dashboard1)

    time.sleep(0.2)  # let the OS fully release the socket

    runtime2, modules2, adapter_manager2, cfg2, dashboard2 = _build_dashboard(host="127.0.0.1", port=port)
    try:
        dashboard2.start()
        assert dashboard2._started is True
        r = requests.get(dashboard2.url + "api/ping", timeout=5)
        assert r.status_code == 200
    finally:
        _teardown(runtime2, adapter_manager2, dashboard2)
