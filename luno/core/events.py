"""
events.py
=========

`Event` - the one message shape that flows through the whole Event Bus -
plus a structured subclass for every well-known event the spec lists.

Two things both have to be true at once, and this file's design is built
around satisfying both:

1. "Create structured event classes" - so `SpeechRecognized`,
   `ToolFinished`, etc. are real, importable, autocomplete-able types,
   not bare strings scattered through calling code.
2. "Custom events must also be supported" - a future module (Discord,
   Telegram, ...) must be able to publish an event type Core has never
   heard of, without editing this file.

Both fall out of one base class: `Event.type` has a default (`""`,
resolved to the class name in `__post_init__` if left blank), so every
named subclass just overrides that one field with a fixed default -
while `Event(type="discord_message", data={...})` works directly for
anything that doesn't have (or doesn't need) its own subclass.

Payloads intentionally live in the generic `data: dict` field rather than
as typed dataclass fields per event - Core has no business logic (see
package docstring), so it never needs to type-check what's inside; only
the publisher and subscribers need to agree on the shape, which is a
contract between two real subsystems, not something Core should enforce.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Dict, Optional

from .utils import generate_id, utcnow


@dataclass
class Event:
    """Base envelope for everything published on the Event Bus.

    `type` - a short string identifying what kind of event this is.
             Left blank, it defaults to the concrete class name.
    `data` - free-form payload, agreed on between publisher/subscribers.
    `source` - optional name of the module/component that published it.
    `priority` - optional hint consumers MAY use to order their own
                 handling of a batch; the Event Bus itself delivers to
                 *subscribers* in subscriber-priority order (see
                 `event_bus.py`), which is a different concept.
    `timestamp`/`event_id` - always auto-filled, never meant to be set
                 by callers.
    """

    type: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    source: Optional[str] = None
    priority: int = 0
    timestamp: datetime = field(default_factory=utcnow)
    event_id: str = field(default_factory=lambda: generate_id("evt"))

    def __post_init__(self) -> None:
        if not self.type:
            self.type = self.__class__.__name__

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type,
            "data": self.data,
            "source": self.source,
            "priority": self.priority,
            "timestamp": self.timestamp.isoformat(),
            "event_id": self.event_id,
        }


def _named(type_name: str):
    """Returns a dataclass field default for a fixed event type - shared
    by every named subclass below so the repetition is one line each."""
    return field(default=type_name, init=False)


# -- Speech / conversation -----------------------------------------------

@dataclass
class WakeWordDetected(Event):
    type: str = _named("wake_word_detected")
    EVENT_TYPE: ClassVar[str] = "wake_word_detected"


@dataclass
class SpeechStarted(Event):
    type: str = _named("speech_started")
    EVENT_TYPE: ClassVar[str] = "speech_started"


@dataclass
class SpeechFinished(Event):
    type: str = _named("speech_finished")
    EVENT_TYPE: ClassVar[str] = "speech_finished"


@dataclass
class SpeechRecognized(Event):
    """`data["text"]` - transcript. `data["confidence"]` - optional."""
    type: str = _named("speech_recognized")
    EVENT_TYPE: ClassVar[str] = "speech_recognized"


@dataclass
class ConversationStarted(Event):
    type: str = _named("conversation_started")
    EVENT_TYPE: ClassVar[str] = "conversation_started"


@dataclass
class ConversationEnded(Event):
    type: str = _named("conversation_ended")
    EVENT_TYPE: ClassVar[str] = "conversation_ended"


# -- Vision -----------------------------------------------------------------

@dataclass
class VisionUpdated(Event):
    """A new scene description landed (routine update)."""
    type: str = _named("vision_updated")
    EVENT_TYPE: ClassVar[str] = "vision_updated"


@dataclass
class VisionChanged(Event):
    """The scene meaningfully changed (not just a routine poll)."""
    type: str = _named("vision_changed")
    EVENT_TYPE: ClassVar[str] = "vision_changed"


@dataclass
class ObjectAppeared(Event):
    type: str = _named("object_appeared")
    EVENT_TYPE: ClassVar[str] = "object_appeared"


@dataclass
class ObjectDisappeared(Event):
    type: str = _named("object_disappeared")
    EVENT_TYPE: ClassVar[str] = "object_disappeared"


# -- Emotion / behavior -------------------------------------------------------

@dataclass
class EmotionChanged(Event):
    type: str = _named("emotion_changed")
    EVENT_TYPE: ClassVar[str] = "emotion_changed"


@dataclass
class BehaviorChanged(Event):
    """Published when the Behavior Tree's active node/state changes."""
    type: str = _named("behavior_changed")
    EVENT_TYPE: ClassVar[str] = "behavior_changed"


# -- Tools / planning ---------------------------------------------------------

@dataclass
class ToolRequested(Event):
    type: str = _named("tool_requested")
    EVENT_TYPE: ClassVar[str] = "tool_requested"


@dataclass
class ToolStarted(Event):
    type: str = _named("tool_started")
    EVENT_TYPE: ClassVar[str] = "tool_started"


@dataclass
class ToolFinished(Event):
    type: str = _named("tool_finished")
    EVENT_TYPE: ClassVar[str] = "tool_finished"


@dataclass
class ToolFailed(Event):
    type: str = _named("tool_failed")
    EVENT_TYPE: ClassVar[str] = "tool_failed"


@dataclass
class PlannerCreated(Event):
    type: str = _named("planner_created")
    EVENT_TYPE: ClassVar[str] = "planner_created"


@dataclass
class PlannerFinished(Event):
    type: str = _named("planner_finished")
    EVENT_TYPE: ClassVar[str] = "planner_finished"


# -- Home Assistant -----------------------------------------------------------

@dataclass
class HomeAssistantEvent(Event):
    type: str = _named("home_assistant_event")
    EVENT_TYPE: ClassVar[str] = "home_assistant_event"


# -- Browser / computer-use ----------------------------------------------------

@dataclass
class BrowserPageOpened(Event):
    """`data["url"]` - the URL that was opened."""
    type: str = _named("browser_page_opened")
    EVENT_TYPE: ClassVar[str] = "browser_page_opened"


@dataclass
class BrowserNavigationCompleted(Event):
    type: str = _named("browser_navigation_completed")
    EVENT_TYPE: ClassVar[str] = "browser_navigation_completed"


@dataclass
class BrowserResearchCompleted(Event):
    """`data["query"]`/`data["source_count"]` - see
    `luno.browser.research.ResearchAgent`."""
    type: str = _named("browser_research_completed")
    EVENT_TYPE: ClassVar[str] = "browser_research_completed"


@dataclass
class BrowserActionRequested(Event):
    type: str = _named("browser_action_requested")
    EVENT_TYPE: ClassVar[str] = "browser_action_requested"


@dataclass
class BrowserActionCompleted(Event):
    type: str = _named("browser_action_completed")
    EVENT_TYPE: ClassVar[str] = "browser_action_completed"


@dataclass
class BrowserActionFailed(Event):
    type: str = _named("browser_action_failed")
    EVENT_TYPE: ClassVar[str] = "browser_action_failed"


@dataclass
class BrowserPermissionRequired(Event):
    """Published when a Level 2/3 action is blocked pending confirmation
    - see `luno.browser.permissions.PermissionManager`."""
    type: str = _named("browser_permission_required")
    EVENT_TYPE: ClassVar[str] = "browser_permission_required"


@dataclass
class BrowserMonitoringAlert(Event):
    type: str = _named("browser_monitoring_alert")
    EVENT_TYPE: ClassVar[str] = "browser_monitoring_alert"


# -- Server / dashboard monitoring (luno.browser.monitoring) -------------------

@dataclass
class ServerCpuHigh(Event):
    type: str = _named("server_cpu_high")
    EVENT_TYPE: ClassVar[str] = "server_cpu_high"


@dataclass
class ServerMemoryHigh(Event):
    type: str = _named("server_memory_high")
    EVENT_TYPE: ClassVar[str] = "server_memory_high"


@dataclass
class ServerDiskHigh(Event):
    type: str = _named("server_disk_high")
    EVENT_TYPE: ClassVar[str] = "server_disk_high"


@dataclass
class ServerServiceDown(Event):
    type: str = _named("server_service_down")
    EVENT_TYPE: ClassVar[str] = "server_service_down"


@dataclass
class DockerContainerDown(Event):
    type: str = _named("docker_container_down")
    EVENT_TYPE: ClassVar[str] = "docker_container_down"


@dataclass
class HomeAssistantUnavailable(Event):
    type: str = _named("home_assistant_unavailable")
    EVENT_TYPE: ClassVar[str] = "home_assistant_unavailable"


@dataclass
class MonitoringTargetUnreachable(Event):
    type: str = _named("monitoring_target_unreachable")
    EVENT_TYPE: ClassVar[str] = "monitoring_target_unreachable"


# -- System -------------------------------------------------------------------

@dataclass
class SystemStarted(Event):
    type: str = _named("system_started")
    EVENT_TYPE: ClassVar[str] = "system_started"


@dataclass
class SystemStopping(Event):
    type: str = _named("system_stopping")
    EVENT_TYPE: ClassVar[str] = "system_stopping"


@dataclass
class SystemError(Event):
    """`data["module"]` - which module (if any). `data["error"]` -
    string description. Never raised as a Python exception - published
    instead, per the "subsystem crashes must not terminate Runtime"
    requirement."""
    type: str = _named("system_error")
    EVENT_TYPE: ClassVar[str] = "system_error"


@dataclass
class Heartbeat(Event):
    """`data` holds a `HeartbeatStats.to_dict()`-shaped payload - see
    `heartbeat.py`."""
    type: str = _named("heartbeat")
    EVENT_TYPE: ClassVar[str] = "heartbeat"


ALL_EVENT_TYPES = [
    WakeWordDetected, SpeechStarted, SpeechFinished, SpeechRecognized,
    ConversationStarted, ConversationEnded,
    VisionUpdated, VisionChanged, ObjectAppeared, ObjectDisappeared,
    EmotionChanged, BehaviorChanged,
    ToolRequested, ToolStarted, ToolFinished, ToolFailed,
    PlannerCreated, PlannerFinished,
    HomeAssistantEvent,
    BrowserPageOpened, BrowserNavigationCompleted, BrowserResearchCompleted,
    BrowserActionRequested, BrowserActionCompleted, BrowserActionFailed,
    BrowserPermissionRequired, BrowserMonitoringAlert,
    ServerCpuHigh, ServerMemoryHigh, ServerDiskHigh, ServerServiceDown,
    DockerContainerDown, HomeAssistantUnavailable, MonitoringTargetUnreachable,
    SystemStarted, SystemStopping, SystemError, Heartbeat,
]
