"""
Adapter Layer
=============

The translation boundary between external systems and Luno's internal,
event-driven architecture. Adapters contain integration logic ONLY -
no planning, no AI reasoning, no business logic. Each one either:

    (a) receives data from an external system and converts it into
        internal Events, or
    (b) receives internal Events and calls an external system,

and nothing more. `luno.core`, `luno.vision_memory`, `luno.behavior_tree`,
`luno.planner`, and `luno.tool_manager` are treated as stable libraries
here - this package only calls their existing public APIs
(`BaseAdapter` subclasses `core.module_manager.Module`; `VisionAdapter`
calls `vision_memory.update()`) and never modifies or redesigns them.

    Whisper / Vision / Home Assistant  --events-->  Event Bus  --events-->  OpenRouter / Fish Audio / Unity
                    ^                                                                    |
                    |                                                                    v
             Scheduler Adapter <---------------------------------------------- (AI-logic Modules: Behavior
                                                                                  Tree / Planner / Tool Manager,
                                                                                  wired in separately as Core
                                                                                  Modules - not part of this
                                                                                  package)

Quick start (standalone - every external system mocked)
------------------------------------------------------------
    from luno.adapters.manager import AdapterManager
    from luno.adapters.whisper import WhisperAdapter
    from luno.adapters.openrouter import OpenRouterAdapter, MockOpenRouterClient

    manager = AdapterManager.standalone()

    whisper = WhisperAdapter()          # MockWhisperSource by default
    manager.register(whisper)

    openrouter = OpenRouterAdapter(client=MockOpenRouterClient(canned_text="Hi!"))
    manager.register(openrouter)

    manager.start_all()

    from luno.adapters.events import NeedLLMResponse
    manager.event_bus.publish(NeedLLMResponse(data={
        "model": "anthropic/claude-3.5-sonnet",
        "messages": [{"role": "user", "content": "hello"}],
    }))
    ...
    manager.stop_all()

Enabling/disabling adapters via config
------------------------------------------
    from luno.adapters.models import AdapterConfig

    manager.register(unity_adapter, AdapterConfig(name="unity", enabled=False))
    ...
    manager.enable("unity")   # starts it and wires its event routes
    manager.disable("unity")  # stops it and tears routes back down

Swapping a mock for a real implementation later
----------------------------------------------------
    from luno.adapters.whisper import WhisperSource, WhisperListener

    class RealWhisperSource(WhisperSource):
        def start(self, listener: WhisperListener):
            # wire up the actual local/streaming Whisper pipeline here,
            # calling listener.on_speech_recognized(...) etc. as it produces results
            ...
        def stop(self): ...

    manager.register(WhisperAdapter(source=RealWhisperSource()))

Nothing else changes - not `AdapterManager`, not any other adapter, not
`luno.core`. This is the entire point of each adapter's `*Source`/
`*Client` interface (see each adapter module's own docstring).

Architecture (see each file's own docstring for the full story)
-------------------------------------------------------------------
    events.py            29 new structured events the spec calls for, on top of core.events' 23
                          (23 from Sprint 1 + 6 Fish Audio/barge-in events from Sprint 3)
    models.py                AdapterConfig, EventMapping/RouteRule (configurable routing, no hardcoding)
    exceptions.py                 every exception this package raises on purpose
    base.py                          BaseAdapter - template method start/stop/restart/on_event/publish/status/health
    registry.py                          thread-safe name -> (adapter, config) bookkeeping, enabled or not
    manager.py                               AdapterManager - thin facade over core's ModuleManager/Lifecycle/Coordinator
    utils.py                                    id/time/logging helpers
    whisper.py / vision.py / openrouter.py /      one file per external system - interfaces + mocks + translation only
    fish_audio.py / unity.py / home_assistant.py /
    scheduler.py
"""

from .base import BaseAdapter
from .events import (
    ADAPTER_EVENT_TYPES,
    AnimationFinished,
    AnimationRequest,
    AssistantResponse,
    AutomationTriggered,
    AvatarReady,
    CancelLLMRequest,
    ConversationReset,
    DeviceStateChanged,
    ExpressionRequest,
    LLMCancelled,
    LLMChunk,
    LLMError,
    LLMFailed,
    LLMFinished,
    LLMStarted,
    LLMStreaming,
    NeedLLMResponse,
    PausePlayback,
    PersonAppeared,
    PersonDisappeared,
    ReloadModel,
    ResumePlayback,
    SpeakRequest,
    SpeechPlaybackCancelled,
    SpeechPlaybackFinished,
    SpeechPlaybackPaused,
    SpeechPlaybackResumed,
    SpeechPlaybackStarted,
    StopPlayback,
)
from .exceptions import (
    AdapterAlreadyRegisteredError,
    AdapterConfigError,
    AdapterDisabledError,
    AdapterError,
    AdapterNotFoundError,
)
from .fish_audio import FishAudioAdapter, FishAudioClient, MockFishAudioClient, PlaybackCancelled
from .fish_audio_real import RealFishAudioClient, RealFishAudioConfig, TTSSynthesisError
from .home_assistant import HomeAssistantAdapter, HomeAssistantClient, HomeAssistantSource, MockHomeAssistantClient, MockHomeAssistantSource
from .manager import AdapterManager
from .models import AdapterConfig, DEFAULT_ADAPTER_EVENT_MAPPING, EventMapping, RouteRule
from .openrouter import (
    LLMResponse,
    MockOpenRouterClient,
    OpenRouterAdapter,
    OpenRouterAPIError,
    OpenRouterAuthError,
    OpenRouterClient,
    OpenRouterConfig,
    OpenRouterInvalidRequestError,
    OpenRouterNetworkError,
    OpenRouterRateLimitError,
    OpenRouterServerError,
    OpenRouterStreamError,
    OpenRouterTimeoutError,
    RequestsOpenRouterClient,
    StreamChunk,
)
from .registry import AdapterRegistry
from .scheduler import DEFAULT_SCHEDULED_JOBS, SchedulerAdapter
from .unity import MockUnityClient, UnityAdapter, UnityClient
from .vision import MockVisionSource, VisionAdapter, VisionSource
from .whisper import MockWhisperSource, WhisperAdapter, WhisperSource

__all__ = [
    "BaseAdapter",
    "AdapterManager", "AdapterRegistry",
    "AdapterConfig", "EventMapping", "RouteRule", "DEFAULT_ADAPTER_EVENT_MAPPING",
    "AdapterError", "AdapterNotFoundError", "AdapterAlreadyRegisteredError",
    "AdapterDisabledError", "AdapterConfigError",
    # events
    "ADAPTER_EVENT_TYPES",
    "PersonAppeared", "PersonDisappeared",
    "NeedLLMResponse", "CancelLLMRequest", "ReloadModel", "ConversationReset",
    "LLMStarted", "LLMStreaming", "LLMChunk", "LLMFinished", "LLMCancelled", "LLMError", "LLMFailed",
    "AssistantResponse", "SpeakRequest",
    "SpeechPlaybackStarted", "SpeechPlaybackFinished", "SpeechPlaybackCancelled",
    "PausePlayback", "ResumePlayback", "StopPlayback", "SpeechPlaybackPaused", "SpeechPlaybackResumed",
    "AnimationRequest", "ExpressionRequest", "AnimationFinished", "AvatarReady",
    "DeviceStateChanged", "AutomationTriggered",
    # adapters
    "WhisperAdapter", "WhisperSource", "MockWhisperSource",
    "VisionAdapter", "VisionSource", "MockVisionSource",
    "OpenRouterAdapter", "OpenRouterClient", "MockOpenRouterClient", "RequestsOpenRouterClient",
    "OpenRouterConfig", "LLMResponse", "StreamChunk",
    "OpenRouterAPIError", "OpenRouterAuthError", "OpenRouterInvalidRequestError",
    "OpenRouterRateLimitError", "OpenRouterServerError", "OpenRouterTimeoutError",
    "OpenRouterNetworkError", "OpenRouterStreamError",
    "FishAudioAdapter", "FishAudioClient", "MockFishAudioClient", "PlaybackCancelled",
    "RealFishAudioClient", "RealFishAudioConfig", "TTSSynthesisError",
    "UnityAdapter", "UnityClient", "MockUnityClient",
    "HomeAssistantAdapter", "HomeAssistantSource", "HomeAssistantClient",
    "MockHomeAssistantSource", "MockHomeAssistantClient",
    "SchedulerAdapter", "DEFAULT_SCHEDULED_JOBS",
]
