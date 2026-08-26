"""
modules.py
==========

`register_all_modules(runtime, launcher_config)` - the ONLY place
`main.py` needs to call for "Registering modules...". Wires every
Core-level subsystem the spec lists (Core itself + Scheduler + Behavior
Tree + Planner + Tool Manager + Context Builder + Vision Memory +
Memory Retrieval + Wake Session + Barge-In + Health Monitor + Heartbeat
+ Lifecycle Manager + Coordinator) onto one `Runtime` - all of which
already exist as `Runtime` collaborators (Scheduler/Heartbeat/Context
Builder/Health Monitor/Lifecycle Manager/Coordinator are constructed
directly BY `Runtime.__init__` - see `luno/core/runtime.py` - so there
is nothing left for this file to do for those six) or as real,
already-tested `Module` subclasses this file registers.

The four wrapper `Module` subclasses that translate Planner/Tool
Manager/Behavior Tree/Vision Memory into Event-Bus-shaped modules
(`PlannerBridgeModule`, `ToolManagerBridgeModule`, `BehaviorTreeModule`,
`VisionMemoryModule`) live in `main_runtime_demo.py` - the ONLY place
they're currently implemented, already exercised by dozens of passing
regression tests this project. Sprint 6 does not duplicate them (that
would be "rewriting a package that already exists" for no benefit and a
real drift risk); this file imports and reuses them exactly as they
are, precisely the same way `luno/barge_in/__init__.py`'s own "Quick
start" docstring already points every future integrator at
`main_runtime_demo.py` for "the full real wiring".

`Module` wiring here (constructor -> `bind_event_bus()` -> `register_module()`
-> `add_route()` for every event that module needs to see) is a byte-for-
byte mirror of `RuntimeDemoConsole.__init__`'s own wiring in
`main_runtime_demo.py` - kept in sync deliberately, not something a
launcher is free to reinterpret, since that wiring is exactly what the
project's own barge-in/wake-session/memory-retrieval regression suites
already validated turn by turn.

`MemoryRetrievalStatusModule` (defined here, new) is the one genuinely
new piece: Memory Retrieval (`luno.memory_retrieval.MemoryRetriever`)
is explicitly listed as something that must be "automatically
registered" alongside every other subsystem, but it has no lifecycle of
its own today (it's a stateless, on-demand, owned-inline collaborator
of `PlannerBridgeModule` - see that package's own docstring: "never a
live vision model call... cached-state reads"). Rather than inventing a
lifecycle it doesn't need, this thin wrapper gives it the SAME
`ModuleManager`/`HealthMonitor` visibility every other subsystem gets
(so `/health`, `runtime.status()`, and the startup banner can report on
it) without changing one line of `luno/memory_retrieval` or
`PlannerBridgeModule` itself.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, TYPE_CHECKING

from luno.core.events import Event, SpeechRecognized
from luno.core.models import ModuleHealthStatus
from luno.core.module_manager import Module

if TYPE_CHECKING:
    from luno.core.runtime import Runtime
    from .launcher_config import LauncherConfig

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _import_demo_module() -> ModuleType:
    """Imports `main_runtime_demo.py` (the project root's developer
    console file) as a real module, reusing its four wrapper `Module`
    classes (see module docstring). A plain `import main_runtime_demo`
    works whenever the project root is already on `sys.path` (true for
    `python main.py`, since Python always puts the running script's own
    directory on `sys.path[0]`); the explicit `spec_from_file_location`
    fallback below covers every other caller (this package's own tests,
    tools invoked from a different working directory) exactly the same
    way every test in `tests/` already loads root-level scripts."""
    if "main_runtime_demo" in sys.modules:
        return sys.modules["main_runtime_demo"]
    try:
        import main_runtime_demo as demo_mod  # type: ignore[import-not-found]
        return demo_mod
    except ImportError:
        pass

    demo_path = _PROJECT_ROOT / "main_runtime_demo.py"
    spec = importlib.util.spec_from_file_location("main_runtime_demo", str(demo_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load main_runtime_demo.py from {demo_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo"] = module
    spec.loader.exec_module(module)
    return module


class ProductionVisionMemoryModule(Module):
    """A production-safe replacement for `main_runtime_demo.py`'s own
    `VisionMemoryModule` - identical shape and `on_event()` behavior
    (translates the simulated/sensor-style `person_detected`/`motion`/
    `door_open` events into `vm.update(description)` calls, exactly the
    same as the demo version), with exactly ONE deliberate difference:
    `start()` here does NOT call `vm.reset()`.

    Why this can't just reuse `demo.VisionMemoryModule` as-is: that
    class's `start()` wipes Vision Memory's persisted SQLite-backed
    state on every construction - correct and desirable for a developer
    console that wants a clean slate each demo run, actively WRONG for
    a production launcher, where the entire point of Vision Memory being
    SQLite-backed is that it survives process restarts. Sprint 6 does
    not modify `main_runtime_demo.py` (its own demo behavior is
    unchanged and still correct for what it's for) - this is new,
    additive code specific to the production path."""

    name = "vision_memory"
    dependencies: list = []

    def __init__(self) -> None:
        from luno import vision_memory as vm
        self._vm = vm
        self._event_bus = None
        self.last_events: list = []

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def start(self) -> None:
        pass  # deliberately does NOT call vm.reset() - see class docstring

    def stop(self) -> None:
        pass

    def health(self) -> ModuleHealthStatus:
        return ModuleHealthStatus(healthy=True)

    def on_event(self, event: Any) -> None:
        description = {
            "person_detected": "A person just appeared in view.",
            "motion": "Motion was detected in the room.",
            "door_open": "The door was observed opening.",
        }.get(event.type)
        if description is None:
            return
        try:
            events = self._vm.update(description)
        except Exception as ex:
            from luno.core.utils import log
            log(f"vm.update() raised: {ex}", "vision_memory")
            return
        self.last_events = events
        if self._event_bus is None:
            return
        for e in events:
            if "appear" in e.category.value or "new" in e.category.value:
                self._event_bus.publish(Event(type="person_appeared", data={"description": e.description}))


class MemoryRetrievalStatusModule(Module):
    """New (Sprint 6): a thin, lifecycle-free `Module` wrapper purely so
    Memory Retrieval shows up in `ModuleManager`/`HealthMonitor`/the
    startup banner like every other registered subsystem. Owns nothing -
    `retriever` is the SAME `MemoryRetriever` instance
    `PlannerBridgeModule` already built and uses for real; this module
    never calls `retrieve_memories()` itself, never duplicates state."""

    name = "memory_retrieval"
    dependencies = ["planner"]

    def __init__(self, retriever: Any) -> None:
        self.retriever = retriever

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def health(self) -> ModuleHealthStatus:
        try:
            enabled = bool(self.retriever.config.enabled)
            sources = self.retriever.registered_sources()
        except Exception as ex:
            return ModuleHealthStatus(healthy=False, message=f"health() raised: {ex}")
        if not enabled:
            return ModuleHealthStatus(healthy=True, message="memory injection disabled by config")
        if not sources:
            return ModuleHealthStatus(healthy=False, message="no memory sources registered")
        return ModuleHealthStatus(healthy=True, message=f"{len(sources)} source(s) registered")


def register_all_modules(runtime: "Runtime", launcher_config: "LauncherConfig") -> Dict[str, Any]:
    """Registers every Core-level module and wires every route it needs.
    Returns a dict of the constructed module instances, keyed by name -
    `main.py`/`adapters.py`/`console.py` use this to reach
    `planner_module`, `session_manager`, etc. without re-importing or
    re-constructing anything."""
    demo = _import_demo_module()

    from luno.barge_in import BargeInConfig, BargeInModule
    from luno.barge_in import REQUIRED_ROUTES as BARGE_IN_REQUIRED_ROUTES
    from luno.wake_session import CONVERSATION_SPEECH_EVENT, SessionManagerModule, WakeSessionConfig
    from luno.proactive import ProactiveConfig, ProactiveModule

    barge_in_config = BargeInConfig.from_env()
    wake_session_config = WakeSessionConfig.from_env()
    proactive_config = ProactiveConfig.from_env()

    from luno.camera_patrol import CameraPatrolModule
    from luno.tool_manager.builtin.camera_patrol import CameraPatrolToolHandler
    from luno.automation import AutomationEngine
    from luno.tool_manager.builtin.automation import AutomationToolHandler
    from luno.camera_automation import CameraAutomationConfig, CameraAutomationModule, VisionCameraEventBridge
    from luno.vision_occupancy import RoomOccupancyModule

    vision_module = ProductionVisionMemoryModule()
    tool_manager_module = demo.ToolManagerBridgeModule()
    planner_module = demo.PlannerBridgeModule(speaking_mode_config=barge_in_config)
    behavior_tree_module = demo.BehaviorTreeModule()
    session_manager = SessionManagerModule(config=wake_session_config)
    barge_in_module = BargeInModule(config=barge_in_config)
    memory_retrieval_module = MemoryRetrievalStatusModule(planner_module.memory_retriever)

    # Sprint 71 (Camera Patrol) - reuses `tool_manager_module` (already
    # constructed above) for every actual camera movement; see `luno/
    # camera_patrol/controller.py`'s own module docstring for the full
    # "no second PTZ implementation" architecture. Registered into the
    # SAME `ToolManager.registry` every other tool lives in (one at a
    # time, the exact extension point `luno/tool_manager/builtin/
    # __init__.py::register_all()`'s own docstring describes) - not
    # gated behind CAMERA_PTZ_BACKEND, since this handler itself never
    # touches hardware; it only orchestrates calls to the ALREADY-gated
    # `camera_ptz` handler via the normal tool_requested round trip, so
    # it behaves correctly (and safely refuses/fails) whether camera_ptz
    # is the real or mock handler.
    camera_patrol_module = CameraPatrolModule()
    tool_manager_module.manager.registry.register("camera_patrol", CameraPatrolToolHandler(camera_patrol_module))
    # Phase 5 - manual PTZ override always wins: a genuinely manual
    # camera_ptz command stops an active patrol first (see
    # CameraPatrolModule.on_manual_ptz_dispatch's own docstring).
    tool_manager_module.register_pre_dispatch_hook(camera_patrol_module.on_manual_ptz_dispatch)

    # LUNO P0.9 (Room Occupancy State + Presence Duration) - constructed
    # HERE (moved up from its original P0.9 placement further below) so
    # that `automation_engine`'s `state_readers` immediately below can
    # close over the REAL instance rather than a forward reference. This
    # is still the SAME single `RoomOccupancyModule` instance registered
    # into `runtime`/returned in the dict further down - no second
    # instance is created. See `luno/vision_occupancy.py`'s own module
    # docstring for the full architecture ("YOLO detects -> Vision
    # confirms -> Occupancy remembers -> Automation decides -> HA
    # executes").
    room_occupancy_module = RoomOccupancyModule()

    # Sprint 72 (Automation Engine Dasar) - the deterministic
    # TRIGGER -> CONDITION -> ACTION -> VERIFY -> COOLDOWN pipeline. Built
    # entirely on top of the SAME Event Bus / tool_requested dispatch
    # path every other module already uses (see `luno/automation/
    # engine.py`'s own module docstring for the full "no second Event
    # Bus, no second scheduler" architecture). The one real state reader
    # wired in here (`"camera_patrol"`) gives the Condition Engine one
    # genuine, currently-available piece of read-only state to evaluate
    # against - deliberately not fabricating a Home Assistant state
    # reader, since no "get current state" handler exists for that tool
    # yet (honest scope, matches this project's own "no invented
    # capability" convention).
    #
    # LUNO P0.10 (Occupancy-Aware Automation Intelligence) - adds five
    # read-only `"occupancy.*"` state readers, each a zero-arg lambda
    # that calls `room_occupancy_module.get_snapshot()` (the SAME
    # defensive/immutable snapshot API P0.9 built - no second occupancy
    # state machine, no direct device control added to
    # RoomOccupancyModule; it stays purely observational). This is
    # `AutomationEngine`'s EXISTING, already-established context
    # mechanism (`evaluate_condition()`'s `state_readers` lookup - see
    # `luno/automation/conditions.py`) - the identical mechanism
    # `"camera_patrol"` already uses one line above - reused, not
    # reinvented, so rule authors can now write conditions like
    # `occupancy.state == "occupied"` or
    # `occupancy.presence_duration_seconds >= 30`.
    automation_engine = AutomationEngine(state_readers={
        "camera_patrol": lambda: camera_patrol_module.get_status().get("state"),
        "occupancy.state": lambda: room_occupancy_module.get_snapshot().state,
        "occupancy.person_count": lambda: room_occupancy_module.get_snapshot().person_count,
        "occupancy.presence_duration_seconds": lambda: room_occupancy_module.get_snapshot().presence_duration_seconds,
        "occupancy.occupancy_age_seconds": lambda: room_occupancy_module.get_snapshot().occupancy_age_seconds,
        "occupancy.last_transition": lambda: room_occupancy_module.get_snapshot().last_transition,
    })
    tool_manager_module.manager.registry.register("automation", AutomationToolHandler(automation_engine))
    # Phase 5 - "Manual > Automation": a genuinely manual camera_ptz/
    # camera_patrol command (untagged by both _automation_origin and
    # _patrol_origin) opens a short manual-priority window during which
    # any automation-issued camera action is refused rather than
    # dispatched (see AutomationEngine.on_camera_dispatch's own
    # docstring). "Automation > Patrol" needs zero new code here - an
    # automation-issued camera_ptz call is untagged _patrol_origin, so
    # CameraPatrolModule's OWN existing Sprint 71 hook (registered
    # immediately above) already stops an active patrol for it.
    tool_manager_module.register_pre_dispatch_hook(automation_engine.on_camera_dispatch)
    automation_engine.bind_scheduler(runtime.scheduler)

    # LUNO P0 (Camera Automation / Safe Integration & Non-Regression
    # Protocol) - a thin, isolated Event Bus consumer, NOT a second
    # automation engine (see `luno/camera_automation/module.py`'s own
    # docstring). Consumes the ALREADY-EXISTING `device_state_changed`
    # event `HomeAssistantAdapter.on_state_changed()` already publishes
    # unconditionally (zero changes to that adapter, zero changes to the
    # Event Bus), and republishes a distinctly-namespaced `camera_
    # automation.state_changed` event that Sprint 72's own
    # `automation_engine` above can already trigger a rule from (`event`
    # trigger type, arbitrary event_name string) with zero further
    # changes to that engine - the "HA action adapter" implementation-
    # order step is likewise already satisfied by that engine's existing
    # `home_assistant.turn_on`/`home_assistant.turn_off` actions.
    # Disabled by default (`CAMERA_AUTOMATION_ENABLED` unset) - preserves
    # current system behavior exactly until an operator opts in.
    camera_automation_module = CameraAutomationModule(config=CameraAutomationConfig.from_env())

    # LUNO P0.5.3 (Vision Event -> Camera Automation Bridge) - a THIN,
    # additive Module that feeds the EXISTING `vision_module`'s already-
    # published `CameraPersonEntered`/`CameraPersonLeft`/
    # `CameraDisconnected`/`CameraReconnected` Event Bus events (see
    # `luno/adapters/vision.py` - unmodified) into the SAME
    # `camera_automation_module` above via its new `ingest_external_
    # camera_event()` method, which reuses that module's own existing
    # dedupe/cooldown. Zero new computer vision, zero changes to Vision.
    # Disabled means zero footprint (see `VisionCameraEventBridge.
    # start()`'s own check of `camera_automation_module.is_enabled()`) -
    # this line alone changes nothing about current behavior until an
    # operator opts in via the SAME `CAMERA_AUTOMATION_ENABLED` flag P0
    # already established.
    vision_camera_event_bridge = VisionCameraEventBridge(camera_automation=camera_automation_module)

    # LUNO P0.9 (Room Occupancy State + Presence Duration) - `room_
    # occupancy_module` itself was constructed further up (moved there by
    # P0.10 so `automation_engine`'s `state_readers` could close over it -
    # see the comment at that construction site). It is a thin, additive,
    # OBSERVATIONAL Module that subscribes directly to the EXISTING
    # `HumanPresenceConfirmed`/`CameraPersonLeft`/`VisionFrameProcessed`
    # events `vision_module` already publishes (unmodified by this
    # sprint) and derives a canonical occupied/vacant state plus
    # presence-duration tracking - see `luno/vision_occupancy.py`'s own
    # module docstring for the full architecture. Always active (no
    # feature flag - purely observational, never controls a device, so
    # there is no "current system behavior" for it to change by existing)
    # unlike `camera_automation_module` above, which gates a REAL device
    # action and therefore stays opt-in.

    # Sprint 10 - Proactive Intelligence: reuses the SAME Planner instance
    # `planner_module` already owns (never a second Planner/registry -
    # "Planner remains the sole execution engine") and reads every other
    # input as a provider callable bound to the REAL running instances
    # below, never a live object reference (same "hand in a snapshot"
    # discipline `luno.memory_retrieval.sources` already established).
    from luno import memory as legacy_memory
    from luno import vision_memory as vm
    from luno.proactive.habit_memory import HabitMemory

    # Habit-learning store (see luno/proactive/habit_memory.py) - one
    # process-wide instance, shared by reference: `ProactiveModule` both
    # records into it (arrival-window device actions) and reads
    # confirmed patterns out of it. Reusing `planner_module.
    # confirmation_handler` (the SAME instance the Efficient LLM
    # Classifier sprint built - never a second one, per that class's own
    # "works for HA, browser, and future tools" design) and `behavior_
    # tree_module.conversation_id` (the SAME stable id every real
    # `user_utterance` this process publishes carries - see that
    # module's own `__init__`) is what lets a spoken "iya"/"tidak" reply
    # to a habit-automation question actually resolve correctly.
    habit_memory = HabitMemory()

    proactive_module = ProactiveModule(
        planner=planner_module.planner,
        config=proactive_config,
        get_world_state=vm.get_world_state,
        get_recent_vision_events=vm.get_recent_events,
        get_long_term_facts=lambda: [m.get("text", "") for m in legacy_memory.list_memories()],
        get_session_summary_count=lambda: len(legacy_memory.list_session_summaries()),
        get_session_status=session_manager.status_snapshot,
        get_barge_in_status=barge_in_module.status_snapshot,
        get_last_tool_result=lambda: tool_manager_module.last_result,
        get_last_tool_name=lambda: tool_manager_module.last_tool,
        habit_memory=habit_memory,
        confirmation_handler=planner_module.confirmation_handler,
        get_conversation_id=lambda: behavior_tree_module.conversation_id,
    )

    event_bus = runtime.event_bus
    for m in (vision_module, tool_manager_module, planner_module, behavior_tree_module, session_manager,
              barge_in_module, proactive_module, camera_patrol_module, automation_engine,
              camera_automation_module, vision_camera_event_bridge, room_occupancy_module):
        m.bind_event_bus(event_bus)

    runtime.register_module(vision_module)
    runtime.register_module(tool_manager_module)
    runtime.register_module(planner_module)
    runtime.register_module(behavior_tree_module)
    runtime.register_module(session_manager)
    runtime.register_module(barge_in_module)
    runtime.register_module(memory_retrieval_module)
    runtime.register_module(proactive_module)
    runtime.register_module(camera_patrol_module)
    runtime.register_module(automation_engine)
    runtime.register_module(camera_automation_module)
    runtime.register_module(vision_camera_event_bridge)
    runtime.register_module(room_occupancy_module)

    # Route wiring - byte-for-byte the same table `RuntimeDemoConsole.
    # __init__` builds in `main_runtime_demo.py`; see that file for the
    # full rationale behind each line (session_manager sees raw speech
    # FIRST so Sleeping-state speech never reaches Planner/OpenRouter;
    # barge_in independently fans out off the same raw speech so
    # interrupts work mid-turn; behavior_tree only ever sees the
    # session-gated CONVERSATION_SPEECH_EVENT, never raw speech).
    runtime.add_route(SpeechRecognized.EVENT_TYPE, "session_manager")
    runtime.add_route(SpeechRecognized.EVENT_TYPE, "barge_in")
    runtime.add_route("wake_word_detected", "session_manager")
    runtime.add_route("speak_request", "session_manager")
    runtime.add_route("speech_playback_finished", "session_manager")
    runtime.add_route("speech_playback_cancelled", "session_manager")
    runtime.add_route("llm_error", "session_manager")
    runtime.add_route("llm_cancelled", "session_manager")
    runtime.add_route(CONVERSATION_SPEECH_EVENT, "behavior_tree")
    runtime.add_route("llm_cancelled", "behavior_tree")

    for pattern in BARGE_IN_REQUIRED_ROUTES:
        if pattern != SpeechRecognized.EVENT_TYPE:
            runtime.add_route(pattern, "barge_in")

    runtime.add_route("user_utterance", "planner")
    # conversation_ended lifecycle routing fix - PlannerBridgeModule.on_event()
    # has always handled event.type == "conversation_ended" (dispatching to
    # _on_conversation_ended(), main_runtime_demo.py) but no route ever
    # delivered it there in production - only "proactive" was wired (see
    # the immediate-trigger loop below). This is the single missing line;
    # see docs/change_impact/conversation_ended_lifecycle_routing.md for
    # the full root-cause trace. Fan-out is native to the Coordinator
    # (luno/core/coordinator.py) - "proactive" and "planner" both
    # independently receiving conversation_ended is not a conflict.
    runtime.add_route("conversation_ended", "planner")
    # Memory Continuity & Short Follow-up Reference Resolution sprint
    # (Sprint 4) - the SAME kind of missing-route gap as the
    # "conversation_ended lifecycle routing fix" immediately above, found
    # by a live probe through the real routed event path (not assumption):
    # `PlannerBridgeModule.on_event()` has always handled
    # `event.type == "assistant_response"` (dispatching to
    # `_on_assistant_response()`, main_runtime_demo.py - which pairs this
    # turn's reply with its user text for `memory.remember_turn()` AND,
    # as of this sprint, updates the per-conversation active-topic
    # snapshot `_active_topic` used for short-follow-up reference
    # resolution) but no route ever delivered "assistant_response" here in
    # production - `AssistantResponse` is published by
    # `luno/adapters/llm_manager.py`/`openrouter.py` once a reply finishes
    # streaming, with nothing in this route table ever forwarding it to
    # "planner". Without this route, `_on_assistant_response()` was dead
    # code via the real event-routed path: `session_log` (session-summary
    # archiving) and, now, `_active_topic` both silently never received
    # real turn content, even though the code that would have populated
    # them was otherwise correct. See
    # docs/change_impact/memory_continuity_reference_resolution.md for the
    # full evidence trail.
    runtime.add_route("assistant_response", "planner")
    runtime.add_route("tool_requested", "tool_manager")
    for evt_type in demo.INJECTABLE_EVENTS:
        runtime.add_route(evt_type, "behavior_tree")
    for evt_type in ("motion", "person_detected", "door_open"):
        runtime.add_route(evt_type, "vision_memory")

    # Sprint 10 - immediate-trigger events (see ProactiveModule's own
    # docstring: these run an out-of-cycle evaluation right away rather
    # than waiting for the next GOAL_EVALUATION_INTERVAL tick). Kept
    # small and high-signal on purpose.
    for evt_type in ("human_entered", "human_left", "person_appeared", "planner_finished", "wake_word_detected", "conversation_ended"):
        runtime.add_route(evt_type, "proactive")

    # Habit-learning wiring (see luno/proactive/habit_memory.py):
    # "tool_finished" (VERIFIED-success ToolResult only - see
    # ToolManagerBridgeModule._process_event()) feeds arrival-window
    # device-action recording; "proactive_habit_resolved" is how a
    # confirmed/declined voice reply (resolved in main_runtime_demo.py's
    # PlannerBridgeModule) reaches back into ProactiveModule's own
    # HabitMemory - PlannerBridgeModule never holds a direct reference to
    # it, only publishes this event (same Event-Bus-only coupling every
    # other cross-module link in this project already uses).
    runtime.add_route("tool_finished", "proactive")
    runtime.add_route("proactive_habit_resolved", "proactive")

    return {
        "demo_module": demo,
        "vision_module": vision_module,
        "tool_manager_module": tool_manager_module,
        "planner_module": planner_module,
        "behavior_tree_module": behavior_tree_module,
        "session_manager": session_manager,
        "barge_in_module": barge_in_module,
        "memory_retrieval_module": memory_retrieval_module,
        "proactive_module": proactive_module,
        "camera_patrol_module": camera_patrol_module,
        "automation_engine": automation_engine,
        "camera_automation_module": camera_automation_module,
        "vision_camera_event_bridge": vision_camera_event_bridge,
        "room_occupancy_module": room_occupancy_module,
        "barge_in_config": barge_in_config,
        "wake_session_config": wake_session_config,
        "proactive_config": proactive_config,
        "habit_memory": habit_memory,
    }
