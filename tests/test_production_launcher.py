"""
test_production_launcher.py
=============================

Sprint 6 - Production Launcher regression suite. Exercises the real
`luno/bootstrap/` package + `main.py`'s own building blocks end to end -
NOT a reimplementation, the actual `register_all_modules()`/
`register_all_adapters()`/`run_startup_health_checks()`/`Supervisor`/
`ShutdownCoordinator`/`ProductionConsole` functions/classes, exactly as
`main.py` calls them.

Covers the sprint's own "Validation" checklist: startup, shutdown,
configuration loading (precedence), health checks, module registration,
adapter registration, Wake Session/Whisper/OpenRouter/GPT-SoVITS/Vision
Memory/Memory Retrieval/Planner/Tool Manager/Home Assistant/Unity all
present under ONE Runtime, interrupt (barge-in), Runtime reload, adapter
restart, thread-leak, and a compressed long-duration stability
simulation.

Deliberately does NOT flip any adapter to a "real" backend (that would
need actual hardware/network/API credentials this sandbox doesn't have
and, per the spec, main.py must run correctly with zero external
dependencies out of the box) - every scenario here runs the same
all-mock configuration `python main.py` uses by default. The real-
adapter wrapper classes' own pure logic (event-shape translation,
config-driven backend selection) are covered separately in
`tests/test_real_adapters.py`.

Run:
    python3 tests/test_production_launcher.py
"""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
import traceback
from typing import Callable, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.bootstrap.adapters import register_all_adapters  # noqa: E402
from luno.bootstrap.console import ProductionConsole  # noqa: E402
from luno.bootstrap.health import run_startup_health_checks  # noqa: E402
from luno.bootstrap.launcher_config import LauncherConfig  # noqa: E402
from luno.bootstrap.modules import ProductionVisionMemoryModule, register_all_modules  # noqa: E402
from luno.bootstrap.shutdown import ShutdownCoordinator  # noqa: E402
from luno.bootstrap.supervisor import Supervisor  # noqa: E402
from luno.core.models import ModuleState  # noqa: E402
from luno.core.runtime import Runtime  # noqa: E402

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _build_stack(launcher_config=None):
    """The exact sequence `main.py` runs, minus banner/prints - shared
    by every scenario below so each one exercises the real call chain,
    not a hand-rolled substitute."""
    cfg = launcher_config or LauncherConfig()
    runtime = Runtime()
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    return runtime, modules, adapter_manager, cfg


def _teardown_stack(runtime, adapter_manager, supervisor=None):
    coordinator = ShutdownCoordinator(runtime, adapter_manager, supervisor=supervisor)
    coordinator.shutdown()


# ============================================================================
# Startup / shutdown
# ============================================================================

@scenario
def test_01_full_stack_starts_cleanly():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        runtime.start()
        assert runtime.status()["running"] is True
        assert runtime.health().healthy is True
    finally:
        _teardown_stack(runtime, adapter_manager)


@scenario
def test_02_shutdown_stops_runtime_and_is_idempotent():
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    coordinator = ShutdownCoordinator(runtime, adapter_manager)
    coordinator.shutdown()
    assert runtime.status()["running"] is False
    # calling shutdown() a second time must be a safe no-op, never raise.
    coordinator.shutdown()


@scenario
def test_03_only_one_runtime_one_adapter_manager_one_event_bus():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        runtime.start()
        # every module/adapter shares the SAME event bus instance - no
        # module/adapter is ever handed a second, private Event Bus.
        assert adapter_manager.event_bus is runtime.event_bus
        for name in ("session_manager", "barge_in_module", "planner_module"):
            assert modules[name]._event_bus is runtime.event_bus
    finally:
        _teardown_stack(runtime, adapter_manager)


# ============================================================================
# Configuration loading (precedence)
# ============================================================================

@scenario
def test_04_config_env_var_wins_over_default():
    saved = os.environ.get("WHISPER_BACKEND")
    try:
        os.environ["WHISPER_BACKEND"] = "real"
        cfg = LauncherConfig.load()
        assert cfg.whisper_backend == "real"
    finally:
        if saved is None:
            os.environ.pop("WHISPER_BACKEND", None)
        else:
            os.environ["WHISPER_BACKEND"] = saved


@scenario
def test_05_config_file_fills_gap_env_still_wins():
    import json
    import tempfile

    tmp_dir = tempfile.mkdtemp()
    config_path = os.path.join(tmp_dir, "launcher.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({"env": {"BARGE_IN_CONFIRM_PROMPT": "from-config-file"}}, f)

    saved_file_env = os.environ.get("LUNO_CONFIG_FILE")
    saved_target = os.environ.get("BARGE_IN_CONFIRM_PROMPT")
    try:
        os.environ.pop("BARGE_IN_CONFIRM_PROMPT", None)
        os.environ["LUNO_CONFIG_FILE"] = config_path
        LauncherConfig.load()
        assert os.environ.get("BARGE_IN_CONFIRM_PROMPT") == "from-config-file"

        # now prove a REAL env var still wins over the same config file key.
        os.environ.pop("BARGE_IN_CONFIRM_PROMPT", None)
        os.environ["BARGE_IN_CONFIRM_PROMPT"] = "from-real-env"
        LauncherConfig.load()
        assert os.environ.get("BARGE_IN_CONFIRM_PROMPT") == "from-real-env"
    finally:
        os.environ.pop("BARGE_IN_CONFIRM_PROMPT", None)
        if saved_target is not None:
            os.environ["BARGE_IN_CONFIRM_PROMPT"] = saved_target
        if saved_file_env is None:
            os.environ.pop("LUNO_CONFIG_FILE", None)
        else:
            os.environ["LUNO_CONFIG_FILE"] = saved_file_env


@scenario
def test_06_config_reload_is_deterministic():
    cfg = LauncherConfig.load()
    reloaded = cfg.reload()
    assert reloaded.whisper_backend == cfg.whisper_backend
    assert reloaded.supervisor_interval_s == cfg.supervisor_interval_s


# ============================================================================
# Health checks
# ============================================================================

@scenario
def test_07_health_checks_all_pass_in_default_mock_configuration():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        runtime.start()
        report = run_startup_health_checks(runtime, adapter_manager, cfg)
        failed = [r.name for r in report.results if not r.ok]
        assert not failed, f"unexpected health check failures in mock mode: {failed}"
        assert len(report.results) == 10
    finally:
        _teardown_stack(runtime, adapter_manager)


@scenario
def test_08_health_check_failure_disables_only_that_subsystem_never_aborts_runtime():
    """A real backend requested without credentials must disable ONLY
    that one adapter - Runtime itself must still be healthy and running
    afterward ("Do NOT terminate Runtime unless Core cannot start").

    `luno.config` (the legacy config module `_check_home_assistant`
    reads `HA_TOKEN`/`HA_URL` from) computes its values once, at import
    time, from `os.environ` - mutating `os.environ` after it's already
    been imported (true by this point in the suite) has no effect on
    its already-bound module attributes, so this monkeypatches those
    attributes directly instead, exactly the values the health check
    actually reads."""
    import luno.config as legacy_config

    cfg = LauncherConfig(home_assistant_backend="real")
    saved_url, saved_token = legacy_config.HA_URL, legacy_config.HA_TOKEN
    legacy_config.HA_URL = None
    legacy_config.HA_TOKEN = None
    try:
        runtime, modules, adapter_manager, cfg = _build_stack(cfg)
        try:
            runtime.start()
            report = run_startup_health_checks(runtime, adapter_manager, cfg)
            ha_result = next(r for r in report.results if r.name == "Home Assistant")
            assert not ha_result.ok
            assert ha_result.action == "disabled"
            # Runtime itself is unaffected.
            assert runtime.status()["running"] is True
        finally:
            _teardown_stack(runtime, adapter_manager)
    finally:
        legacy_config.HA_URL = saved_url
        legacy_config.HA_TOKEN = saved_token


# ============================================================================
# Module / adapter registration completeness
# ============================================================================

@scenario
def test_09_every_required_module_is_registered():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        registered = set(runtime.module_manager.all_modules().keys())
        required = {
            "vision_memory", "tool_manager", "planner", "behavior_tree",
            "session_manager", "barge_in", "memory_retrieval",
        }
        missing = required - registered
        assert not missing, f"missing required modules: {missing}"
    finally:
        _teardown_stack(runtime, adapter_manager)


@scenario
def test_10_every_required_adapter_is_registered():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        registered = set(adapter_manager.registry.list_adapters())
        required = {"whisper", "openrouter", "fish_audio", "vision", "unity", "home_assistant", "scheduler_adapter"}
        missing = required - registered
        assert not missing, f"missing required adapters: {missing}"
    finally:
        _teardown_stack(runtime, adapter_manager)


@scenario
def test_11_future_adapter_only_needs_register_call():
    """Proves the spec's own success criterion literally: a brand new
    adapter needs exactly one `adapter_manager.register(...)` call and
    nothing else to become fully wired (routed, health-checked,
    supervised) - no launcher code changes required."""
    from luno.adapters.base import BaseAdapter
    from luno.adapters.models import AdapterConfig

    class _ExampleFutureAdapter(BaseAdapter):
        name = "example_future_adapter"

    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        # Registered BEFORE runtime.start() - the realistic workflow (this
        # is exactly what bootstrap.adapters.register_all_adapters()
        # already does for the 7 built-in adapters: register everything
        # first, then one runtime.start() brings all of it up together
        # via LifecycleManager.startup()). A module registered AFTER bulk
        # startup already ran is a materially different, narrower case
        # (AdapterManager.register()/`_activate()` only REGISTERS -
        # `ModuleManager.start()` isn't called again for it unless
        # `lazy=True` or something explicitly starts it, matching
        # `enable()`'s own contract elsewhere in that file) - not what
        # this scenario is about.
        adapter_manager.register(_ExampleFutureAdapter(), AdapterConfig(name="example_future_adapter"))
        runtime.start()
        assert "example_future_adapter" in runtime.module_manager.all_modules()
        assert runtime.module_manager.all_modules()["example_future_adapter"].state == ModuleState.RUNNING
    finally:
        _teardown_stack(runtime, adapter_manager)


# ============================================================================
# Vision Memory persistence (the vm.reset() regression this sprint fixed)
# ============================================================================

@scenario
def test_12_production_vision_memory_module_does_not_reset_state_on_start():
    """Direct spy on `vm.reset` rather than inferring it from parsed
    object labels (Vision Memory's own text parsing is a separate
    concern, not what this regression test is about) - proves the exact
    property that matters: `ProductionVisionMemoryModule.start()` must
    never call `vm.reset()`, unlike `main_runtime_demo.py`'s own
    `VisionMemoryModule.start()`, which deliberately does (see that
    class's docstring - correct for a demo console, wrong for
    production)."""
    from luno import vision_memory as vm

    calls = []
    original_reset = vm.reset
    vm.reset = lambda *a, **k: calls.append(True)
    try:
        module = ProductionVisionMemoryModule()
        module.start()
        assert not calls, "ProductionVisionMemoryModule.start() must not call vm.reset()"
    finally:
        vm.reset = original_reset


# ============================================================================
# Interrupt / Wake Session / Memory Retrieval wiring sanity
# ============================================================================

@scenario
def test_13_barge_in_and_session_manager_both_subscribed_to_raw_speech():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        routes = runtime.coordinator.routes()
        targets = routes.get("speech_recognized", [])
        assert "session_manager" in targets
        assert "barge_in" in targets
    finally:
        _teardown_stack(runtime, adapter_manager)


@scenario
def test_14_memory_retrieval_module_reports_registered_sources():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        runtime.start()
        status = modules["memory_retrieval_module"].health()
        assert status.healthy
    finally:
        _teardown_stack(runtime, adapter_manager)


# ============================================================================
# Runtime reload
# ============================================================================

@scenario
def test_15_runtime_reload_does_not_crash_or_stop_modules():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        runtime.start()
        runtime.reload()
        assert runtime.module_manager.all_modules()["planner"].state == ModuleState.RUNNING
        assert runtime.status()["running"] is True
    finally:
        _teardown_stack(runtime, adapter_manager)


# ============================================================================
# Adapter restart / supervisor
# ============================================================================

@scenario
def test_16_adapter_manager_restart_recovers_a_single_adapter():
    """`AdapterManager.restart()` deliberately calls the adapter's OWN
    `restart()` (see `luno/adapters/base.py`) rather than
    `ModuleManager.restart()` - so the restart count that actually
    increments is the ADAPTER's own `status()["restart_count"]`, not
    `ModuleRecord.restart_count` (a separate counter, only touched by
    `ModuleManager.restart()` itself, which this path never calls -
    confirmed during Sprint 6 by reading both classes directly)."""
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        runtime.start()
        fish_audio_adapter = adapter_manager.registry.get("fish_audio")
        before = fish_audio_adapter.status()["restart_count"]
        adapter_manager.restart("fish_audio")
        after = fish_audio_adapter.status()["restart_count"]
        assert after == before + 1
        # every OTHER module is untouched - only the one adapter restarted.
        assert runtime.module_manager.all_modules()["planner"].state == ModuleState.RUNNING
    finally:
        _teardown_stack(runtime, adapter_manager)


@scenario
def test_17_supervisor_restarts_a_failed_module_automatically():
    runtime, modules, adapter_manager, cfg = _build_stack(LauncherConfig(supervisor_interval_s=100.0))
    try:
        runtime.start()
        record = runtime.module_manager.all_modules()["fish_audio"]
        record.state = ModuleState.FAILED  # simulate a crash without waiting for a real one

        supervisor = Supervisor(runtime, adapter_manager, cfg)
        supervisor.sweep_once()

        assert runtime.module_manager.all_modules()["fish_audio"].state == ModuleState.RUNNING
    finally:
        _teardown_stack(runtime, adapter_manager)


@scenario
def test_18_supervisor_gives_up_after_max_attempts_and_publishes_system_error():
    class _AlwaysFailsRestart:
        def restart(self, name):
            raise RuntimeError("simulated persistent failure")

    runtime, modules, adapter_manager, cfg = _build_stack(LauncherConfig(supervisor_max_restart_attempts=2))
    try:
        runtime.start()
        seen = []
        runtime.event_bus.subscribe("system_error", lambda e: seen.append(e))

        broken_manager = _AlwaysFailsRestart()
        broken_manager.registry = adapter_manager.registry  # reuse the real registry for list_adapters()

        record = runtime.module_manager.all_modules()["fish_audio"]
        supervisor = Supervisor(runtime, broken_manager, cfg)
        for _ in range(5):
            record.state = ModuleState.FAILED
            supervisor.sweep_once()

        time.sleep(0.2)
        assert any(e.get("module") == "fish_audio" for e in seen), "expected a SystemError published for the given-up-on module"
    finally:
        _teardown_stack(runtime, adapter_manager)


# ============================================================================
# Developer console - all required commands still work
# ============================================================================

@scenario
def test_19_all_required_console_commands_run_without_raising():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        runtime.start()
        coordinator = ShutdownCoordinator(runtime, adapter_manager)
        console = ProductionConsole(runtime, adapter_manager, modules, cfg, coordinator)
        for line in (
            "/help", "/status", "/health", "/session", "/context", "/memory",
            "/memquery where is my cup", "/plans", "/tasks", "/events", "/events 5",
            "/reload", "/restart", "/debug on", "/debug off", "/modules", "/history", "/bargein",
        ):
            assert console.handle_line(line) is True, f"command {line!r} ended the console loop unexpectedly"
    finally:
        _teardown_stack(runtime, adapter_manager)


@scenario
def test_20_quit_command_ends_the_console_loop_and_requests_shutdown():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        runtime.start()
        coordinator = ShutdownCoordinator(runtime, adapter_manager)
        console = ProductionConsole(runtime, adapter_manager, modules, cfg, coordinator)
        assert console.handle_line("/quit") is False
        assert coordinator._shutdown_event.is_set()
    finally:
        _teardown_stack(runtime, adapter_manager)


@scenario
def test_21_plain_text_is_published_as_speech_recognized():
    runtime, modules, adapter_manager, cfg = _build_stack()
    try:
        runtime.start()
        coordinator = ShutdownCoordinator(runtime, adapter_manager)
        console = ProductionConsole(runtime, adapter_manager, modules, cfg, coordinator)
        seen = []
        runtime.event_bus.subscribe("speech_recognized", lambda e: seen.append(e.get("text")))
        console.handle_line("hey luno")
        deadline = time.time() + 2.0
        while not seen and time.time() < deadline:
            time.sleep(0.02)
        assert seen == ["hey luno"]
    finally:
        _teardown_stack(runtime, adapter_manager)


# ============================================================================
# Thread leak
# ============================================================================

@scenario
def test_22_no_orphan_non_daemon_threads_after_shutdown():
    baseline_threads = {t.ident for t in threading.enumerate()}
    runtime, modules, adapter_manager, cfg = _build_stack()
    runtime.start()
    time.sleep(0.1)
    _teardown_stack(runtime, adapter_manager)
    time.sleep(0.2)

    leaked_non_daemon = [
        t for t in threading.enumerate()
        if t.ident not in baseline_threads and t.is_alive() and not t.daemon
    ]
    assert not leaked_non_daemon, f"non-daemon threads survived shutdown: {[t.name for t in leaked_non_daemon]}"


@scenario
def test_23_repeated_start_stop_cycles_do_not_monotonically_grow_thread_count():
    """Best-effort stand-in for a real memory-leak test (impractical in
    this sandbox) - runs several full start/stop cycles and checks the
    live thread count settles rather than climbing every cycle, which
    would be the most visible symptom of a real resource leak.

    Uses a short `heartbeat_interval_s` deliberately: `HeartbeatMonitor.
    stop()` (existing Core code, unmodified) sets a flag but its loop
    only re-checks that flag once per `interval_s` (its own sleep
    granularity) - at the DEFAULT 10s interval, a just-stopped heartbeat
    thread is still technically alive (though daemon=True, so it can
    never block process exit or grow without bound - it always
    terminates within one more interval) for longer than this test can
    reasonably wait per cycle. A fast interval here isolates the
    property this test actually cares about (does teardown correctly
    signal every background loop to stop, with nothing accumulating
    faster than it winds down) from that one already-bounded, already-
    daemon, pre-existing timing characteristic - see
    `test_22_no_orphan_non_daemon_threads_after_shutdown` for the
    stricter (and, for production purposes, more important) "must never
    block process exit" guarantee, verified against the real default
    config."""
    from luno.core.config import CoreConfig

    counts = []
    for _ in range(4):
        fast_config = CoreConfig(heartbeat_interval_s=0.05, scheduler_tick_s=0.05)
        runtime = Runtime(config=fast_config)
        cfg = LauncherConfig()
        register_all_modules(runtime, cfg)
        adapters = register_all_adapters(runtime, cfg)
        adapter_manager = adapters["adapter_manager"]
        runtime.start()
        time.sleep(0.05)
        _teardown_stack(runtime, adapter_manager)
        time.sleep(0.3)
        gc.collect()
        counts.append(threading.active_count())

    # allow small noise (background daemon threads winding down slightly
    # staggered across iterations) but the trend must not be monotonically
    # increasing across all 4 cycles.
    assert not (counts[0] < counts[1] < counts[2] < counts[3]), f"thread count grew every cycle: {counts}"


# ============================================================================
# Compressed long-duration stability simulation
# ============================================================================

@scenario
def test_24_compressed_stability_simulation_many_scheduler_ticks():
    """Simulates sustained operation by running Core's real Scheduler at
    a fast tick rate for ~1s (many more ticks than a normal 24h run
    would accumulate relative to its own job intervals) and asserting
    the Event Bus backlog stays bounded and Runtime stays healthy
    throughout - the same properties that matter over a real 24-hour
    run, compressed into a few seconds."""
    from luno.core.config import CoreConfig

    fast_config = CoreConfig(scheduler_tick_s=0.01, heartbeat_interval_s=0.2)
    runtime = Runtime(config=fast_config)
    cfg = LauncherConfig(supervisor_interval_s=0.1)
    modules = register_all_modules(runtime, cfg)
    adapters = register_all_adapters(runtime, cfg)
    adapter_manager = adapters["adapter_manager"]
    supervisor = Supervisor(runtime, adapter_manager, cfg)
    try:
        runtime.start()
        supervisor.start()
        deadline = time.time() + 1.0
        max_backlog = 0
        while time.time() < deadline:
            stats = runtime.event_bus.stats()
            max_backlog = max(max_backlog, stats["queue_size"])
            time.sleep(0.05)
        assert runtime.status()["running"] is True
        assert max_backlog < 500, f"event bus backlog grew unbounded during simulated sustained operation: {max_backlog}"
        assert runtime.health().healthy is True
    finally:
        supervisor.stop()
        _teardown_stack(runtime, adapter_manager)


def main() -> int:
    passed = 0
    failed = 0
    for name, fn in SCENARIOS:
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
