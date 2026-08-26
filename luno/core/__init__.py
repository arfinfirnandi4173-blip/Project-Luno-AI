"""
Core Integration Layer
=======================

The central nervous system of Luno: routes events between subsystems,
manages module lifecycle, and builds shared execution context - while
containing NO AI logic itself. Every decision about WHAT to do (Behavior
Tree), HOW to do it (Planner), or what a tool call actually DOES (Tool
Manager) still lives in those packages; Core only wires them together.

    Whisper / Gemini Vision / Vision Memory / Home Assistant
                          |
                      Event Bus
                          |
                      Coordinator  (routing only)
                          |
                    Behavior Tree -> Planner -> Execution Queue -> Tool Manager
                          |
                     Tool Results
                          |
                    Context Builder
                          |
                       OpenRouter -> Fish Audio -> Unity Avatar

Every subsystem talks to every other subsystem ONLY through the Event
Bus - never a direct method call across package boundaries. This
package never imports `luno.vision_memory`, `luno.behavior_tree`,
`luno.planner`, or `luno.tool_manager` - it integrates them the same way
it integrates anything else: through the generic `Module` interface
(`module_manager.py`) and the Event Bus, so it is fully testable with
plain fake modules and requires no hardware, no OpenRouter key, and no
running Home Assistant instance.

Quick start (standalone)
-------------------------
    from luno.core import Runtime, Module

    class EchoModule(Module):
        name = "echo"
        def start(self): print("echo started")
        def stop(self): print("echo stopped")
        def on_event(self, event): print("echo got", event.type, event.data)

    runtime = Runtime()
    runtime.register_module(EchoModule())
    runtime.add_route("speech_recognized", "echo")
    runtime.start()

    from luno.core.events import SpeechRecognized
    runtime.event_bus.publish(SpeechRecognized(data={"text": "hello"}))

    print(runtime.status())
    print(runtime.health())
    runtime.stop()

Wrapping an existing standalone package as a Module (real wiring, later)
--------------------------------------------------------------------------
    from luno.core import Module
    from luno import behavior_tree as bt

    class BehaviorTreeModule(Module):
        name = "behavior_tree"
        dependencies = ["vision_memory"]

        def __init__(self):
            self.blackboard = bt.Blackboard()
            self.tree = None

        def start(self):
            self.tree = bt.BehaviorTree(self.blackboard, ...)
            self.scheduler = bt.Scheduler(self.tree, ...)
            self.scheduler.start()

        def stop(self):
            self.scheduler.stop()

        def on_event(self, event):
            if event.type == "speech_recognized":
                self.blackboard.conversation.pending_transcript = event.get("text")

Nothing about `luno.behavior_tree` itself changes - this is the entire
point of the `Module` interface.

Architecture (see each file's own docstring for the full story)
-------------------------------------------------------------------
    events.py            Event envelope + 23 structured event types + custom events
    event_bus.py             publish/subscribe/unsubscribe, priority, wildcards, once, dead-subscriber cleanup
    dispatcher.py                 background/priority/delayed task execution, never blocks
    scheduler.py                      periodic/one-shot/predicate ("cron-like") jobs, built on Dispatcher
    module_manager.py                     Module interface + ModuleManager (register/start/stop/restart/dependency order)
    lifecycle.py                              fault-tolerant startup/shutdown/restart-failed, in dependency order
    health.py                                     healthy()/report()/last_errors() - aggregates module + bus health
    heartbeat.py                                      periodic uptime/CPU/RAM/throughput snapshot -> Heartbeat event
    context_builder.py                                    pure data assembly for the LLM layer, no LLM calls
    coordinator.py                                            event -> module routing table, no AI reasoning
    config.py                                                     CoreConfig: JSON/YAML/env, reload()
    runtime.py                                                        Runtime - the entry point, owns everything above
"""

from .config import CoreConfig
from .context_builder import ContextBuilder, LLMContext
from .coordinator import Coordinator
from .dispatcher import Dispatcher
from .event_bus import EventBus, Subscription
from .events import (
    ALL_EVENT_TYPES,
    BehaviorChanged,
    ConversationEnded,
    ConversationStarted,
    EmotionChanged,
    Event,
    Heartbeat,
    HomeAssistantEvent,
    ObjectAppeared,
    ObjectDisappeared,
    PlannerCreated,
    PlannerFinished,
    SpeechFinished,
    SpeechRecognized,
    SpeechStarted,
    SystemError,
    SystemStarted,
    SystemStopping,
    ToolFailed,
    ToolFinished,
    ToolRequested,
    ToolStarted,
    VisionChanged,
    VisionUpdated,
    WakeWordDetected,
)
from .exceptions import (
    ConfigError,
    CoreError,
    DependencyCycleError,
    EventBusError,
    ModuleAlreadyRegisteredError,
)
from .exceptions import ModuleNotFoundError as CoreModuleNotFoundError
from .exceptions import ModuleStartError, ModuleStopError
from .health import HealthMonitor
from .heartbeat import HeartbeatMonitor
from .lifecycle import LifecycleManager
from .models import HealthReport, HeartbeatStats, ModuleHealthStatus, ModuleState
from .module_manager import Module, ModuleManager, ModuleRecord
from .runtime import Runtime
from .scheduler import ScheduledJob, Scheduler

__all__ = [
    "Runtime", "CoreConfig",
    "Module", "ModuleManager", "ModuleRecord", "ModuleState", "ModuleHealthStatus",
    "LifecycleManager",
    "EventBus", "Subscription",
    "Event", "ALL_EVENT_TYPES",
    "WakeWordDetected", "SpeechStarted", "SpeechFinished", "SpeechRecognized",
    "ConversationStarted", "ConversationEnded",
    "VisionUpdated", "VisionChanged", "ObjectAppeared", "ObjectDisappeared",
    "EmotionChanged", "BehaviorChanged",
    "ToolRequested", "ToolStarted", "ToolFinished", "ToolFailed",
    "PlannerCreated", "PlannerFinished",
    "HomeAssistantEvent",
    "SystemStarted", "SystemStopping", "SystemError", "Heartbeat",
    "Dispatcher",
    "Scheduler", "ScheduledJob",
    "HealthMonitor", "HealthReport",
    "HeartbeatMonitor", "HeartbeatStats",
    "ContextBuilder", "LLMContext",
    "Coordinator",
    "CoreError", "CoreModuleNotFoundError", "ModuleAlreadyRegisteredError",
    "DependencyCycleError", "ModuleStartError", "ModuleStopError", "EventBusError", "ConfigError",
]
