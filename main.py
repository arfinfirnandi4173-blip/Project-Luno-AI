"""
main.py
=======

Luno Runtime - the ONE official production entry point.

    python main.py

Everything else happens automatically. This file is a LAUNCHER ONLY -
no business logic, no conversation loop, no OpenRouter calls, no
Whisper loop, no TTS loop, no Planner logic. Every one of those already
exists as a real, already-tested subsystem (see `luno/bootstrap/`'s own
docstring for the full list) - this file's only job is:

    Load environment
      -> Load configuration
      -> Initialize logging
      -> Create Runtime
      -> Create AdapterManager
      -> Register modules
      -> Register adapters
      -> Run health checks
      -> Start Runtime
      -> Wait until shutdown

The previous script-style entry point (procedural, hand-rolled
conversation loop, no Event Bus) is preserved unchanged at
`legacy_main.py` - nothing about it was deleted or rewritten, it's
simply no longer what `python main.py` runs. See that file's own
docstring / `tests/test_root_main_bargein.py` for why it still exists
and is still tested.
"""

from __future__ import annotations

import sys

from luno.bootstrap import LauncherConfig
from luno.bootstrap.adapters import (
    apply_camera_automation_test_light_override,
    register_all_adapters,
    register_camera_action_ha_state_reader,
    register_device_intent_classifier,
    register_intent_classifier,
    register_real_tool_handlers,
    register_session_summary_client,
    register_vision_context_reader,
)
from luno.bootstrap.banner import (
    build_runtime_status,
    print_runtime_status,
    print_startup_banner,
    print_step,
)
from luno.bootstrap.console import ProductionConsole
from luno.bootstrap.dashboard import register_dashboard
from luno.bootstrap.health import run_startup_health_checks
from luno.bootstrap.logging_setup import Timer, configure_logging, log_lifecycle
from luno.bootstrap.modules import register_all_modules
from luno.bootstrap.shutdown import ShutdownCoordinator
from luno.bootstrap.supervisor import Supervisor
from luno.core.runtime import Runtime


def main() -> int:
    # -- Load environment + configuration -----------------------------------
    print_startup_banner()
    print_step("Loading configuration...")
    launcher_config = LauncherConfig.load()
    configure_logging(launcher_config.log_file)
    log_lifecycle("bootstrap", "Configuration loaded", request_id=None)

    # -- Create Runtime -------------------------------------------------------
    print_step("Loading Runtime...")
    with Timer() as t:
        runtime = Runtime()
    log_lifecycle("runtime", "Runtime created", duration_ms=t.ms)

    # -- Register modules -------------------------------------------------------
    print_step("Registering modules...")
    with Timer() as t:
        modules = register_all_modules(runtime, launcher_config)
    log_lifecycle("runtime", f"{len(modules)} module group(s) registered", duration_ms=t.ms)

    # -- Create AdapterManager + register adapters -------------------------------
    print_step("Registering adapters...")
    with Timer() as t:
        adapters = register_all_adapters(runtime, launcher_config)
    adapter_manager = adapters["adapter_manager"]
    log_lifecycle("runtime", f"{len(adapter_manager.registry.list_adapters())} adapter(s) registered", duration_ms=t.ms)

    # Opt-in: swaps the Tool Manager's mock Home Assistant handler for a
    # real one when HOME_ASSISTANT_BACKEND=real - see that function's own
    # docstring for why this can't just happen inside register_all_modules()
    # or register_all_adapters() (it needs output from BOTH). No-ops
    # (leaves the mock in place) if the backend isn't "real" or the real
    # adapter itself failed to connect.
    register_real_tool_handlers(modules, adapters, launcher_config)

    # Opt-in by construction (real OpenRouter client required, no separate
    # env var) - lets Planner ask the LLM to classify device intent when
    # its own fast regex parser can't (typos/paraphrases beyond what a
    # regex was ever going to anticipate). No-ops on mock OpenRouter.
    register_device_intent_classifier(modules, adapters)

    # Opt-in by construction (same real OpenRouter client, no separate
    # env var) - lets PlannerBridgeModule archive a session summary
    # (config/session_summaries.json) when a wake-word conversation ends,
    # or on-demand via "rangkum obrolan ini". No-ops on mock OpenRouter.
    register_session_summary_client(modules, adapters)

    # Opt-in by construction (same real OpenRouter client) AND by config
    # (CLASSIFIER_ENABLED=true in .env, default false) - lets the Decision
    # Engine ask GPT-5.4-nano to classify an utterance ONLY when its own
    # deterministic rules found nothing at all (see
    # luno/routing/decision_engine.py's own "ambiguous gate"). No-ops on
    # mock OpenRouter; even when wired, does nothing unless
    # CLASSIFIER_ENABLED is also set.
    register_intent_classifier(modules, adapters)

    # P0.7 (Vision Context -> Automation Context) - always wired (not
    # gated on a real backend, unlike the LLM-client functions above) -
    # lets camera_automation.camera_event additively carry human_present/
    # person_count/detected_objects/available/detection_error from the
    # SAME Vision status snapshot the dashboard already reads. No-ops
    # harmlessly if vision_camera_event_bridge/adapter_manager aren't
    # both present. See that function's own docstring for why this can't
    # happen inside register_all_modules()/register_all_adapters().
    register_vision_context_reader(modules, adapters)

    # P0.8.0 (Camera Automation -> Home Assistant Action Safety Pipeline)
    # - always wired (not gated on a real backend) - lets the camera
    # action safety gate skip a redundant turn_on/turn_off Home Assistant
    # call when the target entity already reports the requested state,
    # reusing the SAME real RealHomeAssistantClient.get_entity_state()
    # RealHomeAssistantHandler already calls for its own "already ON"
    # shortcut. No-ops harmlessly (AutomationEngine.ha_state_reader stays
    # None, and the safety gate simply skips this one optional sub-check)
    # on mock Home Assistant or when the real backend isn't connected.
    register_camera_action_ha_state_reader(modules, adapters)

    # -- Start Runtime ----------------------------------------------------------
    # (Health checks that need to inspect a LIVE unity/HA adapter -
    # e.g. Unity's `ping()` - run AFTER start(), per the spec's own
    # sequence: "Running health checks..." appears right before "Ready.",
    # which itself is after every module/adapter is already up.)
    print_step("Starting Runtime...")
    with Timer() as t:
        runtime.start()
    log_lifecycle("runtime", "Runtime started", duration_ms=t.ms)

    # P0.8.1 (Live Camera -> Home Assistant Light Verification)
    # - strictly opt-in: no-ops unless the user has explicitly set
    # CAMERA_AUTOMATION_TEST_LIGHT_ENTITY (never guesses/auto-selects a
    # light - P0.8.1 brief Section 2). When set, overrides ONLY the
    # existing P0.8.0 TEST-ONLY rule's (camera_test_automation_safety_
    # action) target, in memory, for this process only - every other
    # rule and every other part of this one rule (id/conditions/
    # cooldown) is untouched, and config/automation_rules.json on disk
    # is never written to. See apply_camera_automation_test_light_
    # override()'s own docstring for the full scoping guarantee. MUST
    # run AFTER runtime.start() - AutomationEngine.start() is what
    # actually populates its own in-memory rule table (reload_rules(),
    # called from Module.start()); calling this any earlier would find
    # no rule loaded yet and silently no-op every time.
    _p0_8_1_test_light = apply_camera_automation_test_light_override(modules)
    if _p0_8_1_test_light:
        log_lifecycle("bootstrap", f"P0.8.1 test-light override applied: {_p0_8_1_test_light}")

    # -- Run health checks ----------------------------------------------------
    # Each check now prints its own result live as it completes (see
    # `run_startup_health_checks`'s own docstring - this used to be a
    # single silent batch, printed only at the very end, which is why a
    # hang in any one check looked like the whole process freezing with
    # zero diagnostic output).
    print_step("Running health checks...")
    with Timer() as t:
        health_report = run_startup_health_checks(runtime, adapter_manager, launcher_config)
    log_lifecycle("health", "Startup health checks complete", status=("ok" if health_report.all_ok else "degraded"), duration_ms=t.ms)

    # -- Supervision (background restart-on-failure) -----------------------------
    supervisor = Supervisor(runtime, adapter_manager, launcher_config)
    supervisor.start()

    # -- Dashboard (Sprint 7) --------------------------------------------------
    # Constructed the same way `ProductionConsole` below is: from the
    # already-running Runtime/AdapterManager/module set, never its own
    # Runtime. `register_dashboard()` returns `None` when
    # DASHBOARD_ENABLED=false - the terminal console keeps working
    # exactly as before either way.
    print_step("Registering dashboard...")
    dashboard = register_dashboard(
        runtime, adapter_manager, modules, launcher_config, supervisor=supervisor,
        audio_capture_store=adapters.get("audio_capture_store"),
    )

    shutdown_coordinator = ShutdownCoordinator(runtime, adapter_manager, supervisor=supervisor, dashboard=dashboard)
    shutdown_coordinator.install_signal_handlers()
    # Sprint 71 (Dashboard Startup & Access Recovery) - bug fix (reproduced
    # live): `dashboard.start()` used to be unguarded here, so a bind
    # failure (most commonly a stale/previous Luno process still holding
    # the port) raised straight out of `main()`, killing the ENTIRE
    # process - voice pipeline, wake word, everything - over a failure
    # that only actually affects the Dashboard's own HTTP listener.
    # `DASHBOARD_ENABLED=false` has always meant "the rest of Luno keeps
    # working with no dashboard" (see `register_dashboard()`'s own
    # docstring); a bind FAILURE now degrades to that exact same
    # already-established, already-tested state instead of taking the
    # whole process down with it.
    dashboard_started = False
    if dashboard is not None:
        dashboard.shutdown_coordinator = shutdown_coordinator
        try:
            with Timer() as t:
                dashboard.start()
            dashboard_started = True
            log_lifecycle("dashboard", f"Dashboard listening at {dashboard.url}", duration_ms=t.ms)
        except OSError as ex:
            log_lifecycle("dashboard", f"Dashboard failed to start (continuing without it): {ex}", status="degraded")
            print(f"WARNING: Dashboard could not start - {ex}")
            print("Luno will continue running without the Dashboard. See the log above for the exact reason.")

    status = build_runtime_status(runtime, adapter_manager, launcher_config)
    print_runtime_status(status)
    if dashboard is not None and dashboard_started:
        print(f"Dashboard   {dashboard.url}")
    print("Ready.")
    log_lifecycle("runtime", "Listening for wake word...")

    console = ProductionConsole(runtime, adapter_manager, modules, launcher_config, shutdown_coordinator, supervisor=supervisor)
    print("Type /help for developer commands, or plain text to simulate speech (Ctrl+C to exit).\n")

    # -- Wait until shutdown ------------------------------------------------------
    _run_console_loop(console, shutdown_coordinator)

    shutdown_coordinator.shutdown()
    return 0


def _run_console_loop(console: ProductionConsole, shutdown_coordinator: ShutdownCoordinator) -> None:
    """The ONLY thing resembling a "loop" in this file - reading
    developer-console input lines and handing them to `ProductionConsole`
    (a thin relay - see that class's own docstring). This is NOT the
    conversation loop (Whisper/OpenRouter/Fish Audio all run on their
    own adapter-owned background threads, driven entirely by the Event
    Bus - see `luno/bootstrap/adapters.py`); a headless deployment with
    no attached terminal (`stdin` not a TTY / EOF immediately) simply
    waits on `shutdown_coordinator`'s signal instead, exactly matching
    "wait until shutdown"."""
    if not sys.stdin or not sys.stdin.isatty():
        shutdown_coordinator.wait_for_shutdown_signal()
        return

    try:
        while shutdown_coordinator.is_accepting_events():
            try:
                line = input("> ")
            except EOFError:
                break
            if shutdown_coordinator._shutdown_event.is_set():
                break
            try:
                if not console.handle_line(line):
                    break
            except Exception as ex:
                print(f"error handling input: {ex}")
    except KeyboardInterrupt:
        shutdown_coordinator.request_shutdown()


if __name__ == "__main__":
    sys.exit(main())
