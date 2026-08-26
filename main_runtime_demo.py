"""
main_runtime_demo.py
=====================

Luno Developer Runtime Console - the primary DEVELOPMENT entry point for
Luno. This is NOT the final production runtime (that's `main.py`, which
talks to real hardware). This file's only job is to stand up every
already-built subsystem (`luno.core`, `luno.adapters`, `luno.behavior_tree`,
`luno.planner`, `luno.tool_manager`, `luno.vision_memory`) together, wired
through the real Event Bus exactly the way they'll run for real, so a
developer can watch the whole event-driven pipeline work end to end before
any microphone/camera/ESP32/Unity is connected.

Architecture rule this file exists to honor: NOTHING in this file (or in
any class it defines) calls OpenRouter, Tool Manager, Fish Audio, Planner,
or Behavior Tree directly. Every cross-subsystem interaction happens by
publishing an `Event` onto `runtime.event_bus` and, where a synchronous
answer is genuinely needed (see `_ModuleBridge.wait_for` below), by
subscribing for the correlated response - the same pattern every adapter
in `luno/adapters/` already uses. The small `*Module` wrapper classes below
are integration glue (translate Event <-> the wrapped package's own real
API), not business logic - they make zero decisions; every decision
(what to do, how to plan it, what to say) still lives inside the wrapped
packages themselves, completely unmodified.

    "Luno, open chrome"
      -> SpeechRecognized                              (console publishes - simulated Whisper)
      -> SessionManagerModule (Sleeping -> wake match?)  (luno.wake_session, Sprint 2)
      -> WakeWordDetected / ConversationStarted / "Yes?" (ack spoken straight through Fish Audio,
                                                           via SpeakRequest - see below)
      -> "conversation_speech" (Listening, awake)         (only forwarded once genuinely awake)
      -> BehaviorTreeModule                                (luno.behavior_tree, untouched)
      -> "user_utterance"                                   (BehaviorTreeModule's generate_reply publishes)
      -> PlannerBridgeModule                                 (luno.planner, untouched)
      -> "speaking_mode_assigned"                             (Sprint 3 - classify_speaking_mode(),
                                                                rule-based, BEFORE NeedLLMResponse)
      -> "response_depth_assigned"                             (Chat/Voice Dual Output sprint - the SAME
                                                                 once-per-turn ResponsePolicy.depth already
                                                                 computed above, published so BehaviorTreeModule
                                                                 can reuse it without a second classification)
      -> ToolRequested -> ToolManagerBridgeModule             (luno.tool_manager, untouched)
      -> ToolFinished
      -> NeedLLMResponse -> OpenRouterAdapter                  (luno.adapters.openrouter, untouched)
      -> LLMChunk* (streaming) -> LLMFinished -> AssistantResponse  (conversation record - raw text,
                                                                      history/context/display/Chat - UNCHANGED)
      -> BehaviorTreeModule._speak()                            (build_dual_response() - see
                                                                   luno/response_output.py - then publishes
                                                                   SpeakRequest with voice_text (full string,
                                                                   "text") AND voice_chunks ("chunks" - TTS
                                                                   Chunking/Streaming sprint), never chat_text)
      -> FishAudioAdapter                                        (luno.adapters.fish_audio, Sprint 3 added
                                                                   pause/resume - own executor; TTS Chunking
                                                                   sprint added sequential per-chunk playback -
                                                                   "chunks" absent degrades to the pre-chunking
                                                                   single-block behavior, unchanged)
      -> SpeechPlaybackFinished -> WaitingUser (still awake, inactivity timeout running)
      -> (no more speech within session_timeout_s) -> ConversationTimeout -> ConversationEnded -> Sleeping

    Meanwhile, at any point after "AssistantResponse" starts playing:
    "stop" / "cancel" / "pause" / "wait" / "hold on" / "enough" / "batal" / "sudah" / ...
      -> SpeechRecognized (Whisper keeps listening even while Luno talks - never blocked)
      -> BargeInModule (luno.barge_in, Sprint 3 - decides FREE/SOFT/CONFIRM/CRITICAL)
      -> CancelLLMRequest (if thinking) / StopPlayback or PausePlayback (if speaking)
      -> (FREE mode only) a short, hardcoded "Okay."/"Sure." via SpeakRequest - never from the LLM

Run it:
    python3 main_runtime_demo.py

Everything is mocked by default (no OpenRouter API key, no real TTS, no
camera) - see `SETUP_F5TTS.md`-style honesty: this is a developer console,
not a way to skip real integration later.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from luno.core import (  # noqa: E402
    Event,
    HomeAssistantEvent,
    Module,
    ModuleHealthStatus,
    Runtime,
    SpeechRecognized,
)
from luno.core.utils import generate_id, log  # noqa: E402
from luno import vision_memory as vm  # noqa: E402
from luno import memory  # noqa: E402
from luno import memory_context  # noqa: E402
from luno.memory_turn_trace import build_turn_trace  # noqa: E402
from luno.memory_guard import VerifiedFactStore  # noqa: E402
from luno.world_model import WorldModel  # noqa: E402
from luno.routing import ConfirmationHandler, DecisionEngine, Intent as RoutingIntent, RoutingConfig  # noqa: E402
from luno import config as legacy_config  # noqa: E402
from luno.persona import PERSONA, build_persona_prompt  # noqa: E402
from luno.emotion_engine import (  # noqa: E402
    EmotionStateTracker,
    build_emotional_context_prompt,
    derive_response_policy,
)
from luno.response_policy import (  # noqa: E402
    DEPTH_NORMAL,
    DepthPreference,
    ResponsePolicy,
    apply_depth_feedback,
    build_depth_instruction,
    compute_response_policy,
    detect_depth_feedback,
)
from luno.response_depth_preference import (  # noqa: E402
    DepthPreferenceStore,
    merge_conversation_into_persistent,
    should_persist,
)
from luno.response_output import build_dual_response  # noqa: E402
from luno.speech_chunk import build_speech_chunks  # noqa: E402
from luno.voice_output_mode import (  # noqa: E402
    DEFAULT_VOICE_OUTPUT_MODE,
    VOICE_OUTPUT_MODE_ALL,
    VOICE_OUTPUT_MODE_SHORT,
    is_valid_voice_output_mode,
    match_voice_output_mode_command,
    resolve_voice_output_mode,
)
from luno.incremental_speech import StreamingSpeechCoordinator  # noqa: E402
from luno.relationship_engine import (  # noqa: E402
    RelationshipContextBuilder,
    RelationshipEngine,
    RelationshipStore,
)
from luno import episodic_memory  # noqa: E402
from luno.environment_intent import (  # noqa: E402
    ENV_TRIGGERS,
    build_confirmation_command,
    classify_confirmation_reply,
    classify_environmental_cue,
)
from luno.memory_retrieval import (  # noqa: E402
    MemoryRetriever,
    MemoryRetrievalConfig,
    build_memory_prompt_block,
    make_long_term_memory_source,
    make_planner_state_source,
    make_vision_event_source,
    make_vision_human_source,
    make_vision_object_source,
)
from luno.behavior_tree import (  # noqa: E402
    BehaviorTree,
    Blackboard,
    HAEvent,
    HAEventSeverity,
    Handlers,
)
from luno.behavior_tree import Scheduler as BTScheduler  # noqa: E402
from luno.behavior_tree.state_machine import LunoState  # noqa: E402
from luno.planner import Planner as TaskPlanner  # noqa: E402
from luno.planner import PlanOptions  # noqa: E402
from luno.planner import PlanStatus  # noqa: E402
from luno.planner.parser import IntentParser  # noqa: E402
from luno.planner.task import Task  # noqa: E402
from luno.tool_manager import ToolManager  # noqa: E402
from luno.tool_manager.builtin import register_all as register_builtin_tools  # noqa: E402
from luno.adapters import (  # noqa: E402
    AdapterConfig,
    AssistantResponse,
    CancelLLMRequest,
    ConversationReset,
    DEFAULT_ADAPTER_EVENT_MAPPING,
    EventMapping,
    FishAudioAdapter,
    LLMCancelled,
    LLMChunk,
    LLMError,
    LLMFinished,
    LLMStarted,
    LLMStreaming,
    MockFishAudioClient,
    MockOpenRouterClient,
    NeedLLMResponse,
    OpenRouterAdapter,
    OpenRouterConfig,
    PausePlayback,
    RealFishAudioClient,
    RealFishAudioConfig,
    ReloadModel,
    RequestsOpenRouterClient,
    ResumePlayback,
    SpeakRequest,
    SpeechPlaybackPaused,
    SpeechPlaybackResumed,
    StopPlayback,
)
from luno.adapters.events import RoutingDecisionMade  # noqa: E402
from luno.adapters.manager import AdapterManager  # noqa: E402
from luno.wake_session import (  # noqa: E402
    CONVERSATION_SPEECH_EVENT,
    ConversationState,
    SessionManagerModule,
    WakeSessionConfig,
)
from luno.barge_in import (  # noqa: E402
    BargeInConfig,
    BargeInModule,
    REQUIRED_ROUTES as BARGE_IN_REQUIRED_ROUTES,
    SpeakingMode,
    classify_speaking_mode,
)


# ============================================================================
# Terminal colors
# ============================================================================

class Colors:
    """Raw ANSI codes, no third-party dependency. Disabled automatically
    when stdout isn't a real terminal (piped output, CI, redirected to a
    file) or when NO_COLOR is set, per the spec's "use colors when
    supported" - never assumed."""
    enabled = sys.stdout.isatty() and os.getenv("NO_COLOR") is None

    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GREY = "\033[90m"

    @classmethod
    def wrap(cls, text: str, *codes: str) -> str:
        if not cls.enabled or not text:
            return text
        return "".join(codes) + text + cls.RESET


def c(text: str, *codes: str) -> str:
    return Colors.wrap(text, *codes)


# ============================================================================
# Small shared utilities
# ============================================================================

def wait_for_event(
    event_bus: Any, pattern: str, match_fn: Callable[[Event], bool], timeout_s: float,
) -> Optional[Event]:
    """The ONE mechanism every wrapper below uses to get a synchronous
    answer out of a purely event-driven exchange, WITHOUT ever calling
    the other side directly - subscribe, wait, unsubscribe. Used for
    request/response correlation (by request_id/execution_id) across
    module boundaries, exactly like a real RPC-over-events client would."""
    done = threading.Event()
    box: Dict[str, Event] = {}

    def _handler(event: Event) -> None:
        if match_fn(event):
            box["event"] = event
            done.set()

    sub_id = event_bus.subscribe(pattern, _handler)
    try:
        done.wait(timeout_s)
        return box.get("event")
    finally:
        event_bus.unsubscribe(sub_id)


def utcnow_str() -> str:
    from luno.core.utils import utcnow
    return utcnow().strftime("%H:%M:%S")


# ============================================================================
# Event Timeline / History - always recording, read by /events and the
# live "Event Timeline" panel. Debug Mode (see DebugMonitor below) is a
# SEPARATE, opt-in firehose printer - this recorder never prints anything
# itself and never changes runtime behavior, it only remembers.
# ============================================================================

@dataclass
class EventRecord:
    seq: int
    at: str
    type: str
    source: Optional[str]
    data_preview: str
    latency_ms: Optional[float] = None


class EventHistory:
    def __init__(self, event_bus: Any, max_len: int = 500) -> None:
        self.event_bus = event_bus
        self._records: Deque[EventRecord] = deque(maxlen=max_len)
        self._seq = 0
        self._lock = threading.Lock()
        #: request_id -> monotonic time of its *_started event, so a
        #: *_finished/*_error/*_cancelled for the same id can report
        #: latency without needing any change to the events themselves.
        self._started_at: Dict[str, float] = {}
        self._sub_id: Optional[str] = None

    def start(self) -> None:
        self._sub_id = self.event_bus.subscribe("*", self._on_event, priority=-1000)

    def stop(self) -> None:
        if self._sub_id:
            self.event_bus.unsubscribe(self._sub_id)
            self._sub_id = None

    def _on_event(self, event: Event) -> None:
        rid = event.get("request_id") or event.get("execution_id") or event.get("plan_id")
        latency_ms = None
        if event.type.endswith("_started") and rid:
            with self._lock:
                self._started_at[rid] = time.time()
        elif rid and event.type.split("_")[-1] in ("finished", "error", "cancelled", "failed"):
            with self._lock:
                t0 = self._started_at.pop(rid, None)
            if t0 is not None:
                latency_ms = (time.time() - t0) * 1000.0

        preview = {k: v for k, v in list(event.data.items())[:4]}
        with self._lock:
            self._seq += 1
            self._records.append(EventRecord(
                seq=self._seq, at=utcnow_str(), type=event.type, source=event.source,
                data_preview=str(preview)[:120], latency_ms=latency_ms,
            ))

    def recent(self, limit: int = 20) -> List[EventRecord]:
        with self._lock:
            return list(self._records)[-limit:]

    def resize(self, max_len: int) -> None:
        with self._lock:
            self._records = deque(self._records, maxlen=max_len)


class DebugMonitor:
    """`/debug on` - a second, independent wildcard subscriber that PRINTS
    every event live (type, source, subscriber count, queue size) the
    moment it's delivered. Purely observational: it never mutates an
    Event, never blocks the bus (subscriber priority/async unaffected),
    and toggling it on/off never changes what any other subscriber sees -
    satisfies "Debug mode must not modify runtime behavior" exactly."""

    def __init__(self, event_bus: Any) -> None:
        self.event_bus = event_bus
        self._sub_id: Optional[str] = None
        self.enabled = False

    def on(self) -> None:
        if self.enabled:
            return
        self.enabled = True
        self._sub_id = self.event_bus.subscribe("*", self._print, priority=-2000)

    def off(self) -> None:
        self.enabled = False
        if self._sub_id:
            self.event_bus.unsubscribe(self._sub_id)
            self._sub_id = None

    def _print(self, event: Event) -> None:
        stats = self.event_bus.stats()
        line = (
            f"{c('[DEBUG]', Colors.GREY)} {utcnow_str()} {c(event.type, Colors.MAGENTA)} "
            f"src={event.source or '-'} subs={stats['subscriber_count']} "
            f"queue={stats['queue_size']} avg_latency={stats['avg_latency_ms']:.2f}ms "
            f"threads={threading.active_count()}"
        )
        print(line)


# ============================================================================
# Module: Vision Memory bridge
# ============================================================================

class VisionMemoryModule(Module):
    """Thin translation layer over `luno.vision_memory`'s module-level
    facade - identical shape to `luno.adapters.vision.VisionAdapter`, just
    driven by the demo's injectable events instead of a camera. Makes zero
    decisions: it hands whatever text it's given to `vision_memory.update()`
    (that package's own event-detector/importance scoring decides what, if
    anything, is worth remembering) and republishes whatever comes back."""

    name = "vision_memory"
    dependencies: List[str] = []

    def __init__(self) -> None:
        self._event_bus = None
        self.last_events: List[Any] = []

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def start(self) -> None:
        vm.reset()

    def stop(self) -> None:
        pass

    def health(self) -> ModuleHealthStatus:
        return ModuleHealthStatus(healthy=True)

    def on_event(self, event: Event) -> None:
        description = {
            "person_detected": "A person just appeared in view.",
            "motion": "Motion was detected in the room.",
            "door_open": "The door was observed opening.",
        }.get(event.type)
        if description is None:
            return
        try:
            events = vm.update(description)
        except Exception as ex:
            print(c(f"[vision_memory] update() raised: {ex}", Colors.RED))
            return
        self.last_events = events
        if self._event_bus is None:
            return
        for e in events:
            if "appear" in e.category.value or "new" in e.category.value:
                self._event_bus.publish(Event(type="person_appeared", data={"description": e.description}))


# ============================================================================
# Module: Tool Manager bridge
# ============================================================================

class ToolManagerBridgeModule(Module):
    """Listens for `ToolRequested`, runs it through a real
    `luno.tool_manager.ToolManager` (builtin mock handlers by default -
    no hardware needed, per the spec's testing requirement), publishes
    `ToolStarted`/`ToolFinished`/`ToolFailed`. This is the ONLY place in
    the whole demo that touches `ToolManager` directly - everything
    upstream (Planner) only ever publishes `ToolRequested` and waits.

    Architecture audit fix (C1): `self.manager.execute(tool_call)` used
    to run INLINE inside `on_event()`, which the Coordinator calls
    synchronously on the Event Bus's single delivery ("pump") thread
    (`Coordinator.add_route()` subscribes with the default
    `async_mode=False` - see `core/coordinator.py`). A slow tool call
    (a real Home Assistant device that's slow to respond, a Playwright
    browser action, a flaky network call) therefore blocked delivery of
    EVERY other event system-wide for its whole duration - including a
    barge-in interrupt's own `speech_recognized` event, directly
    undermining barge-in's purpose of interrupting Luno immediately.

    Fixed the exact way `luno/adapters/base.py`'s `BaseAdapter` already
    solves the identical problem for every adapter (see that module's
    own docstring, which names this failure mode explicitly): a
    dedicated single-worker `ThreadPoolExecutor`. `on_event()` now only
    ever validates + submits to it and returns immediately; the actual
    work moved, unchanged, into `_process_event()`. One worker keeps
    tool executions strictly FIFO (same ordering guarantee as before -
    this was never meant to run tools concurrently), while no longer
    holding up the Event Bus or any other module."""

    name = "tool_manager"
    dependencies: List[str] = []

    def __init__(self, extra_tool_handlers: Optional[Dict[str, Any]] = None) -> None:
        self.manager = ToolManager()
        register_builtin_tools(self.manager.registry)
        # Real handlers override their mock counterpart AFTER
        # register_builtin_tools() - the exact sanctioned extension point
        # `luno/tool_manager/builtin/__init__.py::register_all()`'s own
        # docstring describes ("real deployments will typically call
        # registry.register('home_assistant', RealHandler()) ... instead,
        # one at a time"). `luno/bootstrap/modules.py` is the only caller
        # that ever passes a non-empty dict here (e.g.
        # {"home_assistant": RealHomeAssistantHandler(...)} when
        # HOME_ASSISTANT_BACKEND=real) - this file's own demo/test usage
        # never does, so mock-everything stays the default everywhere else.
        for tool_name, handler in (extra_tool_handlers or {}).items():
            self.manager.registry.register(tool_name, handler)
        self._event_bus = None
        self.last_result: Optional[Dict[str, Any]] = None
        self.last_tool: Optional[str] = None
        self._lock = threading.Lock()
        #: Single-worker pool - see the class docstring's "Architecture
        #: audit fix (C1)" note. Created in `start()`/torn down in
        #: `stop()`, mirroring `BaseAdapter.start()`/`stop()` exactly.
        self._worker_pool: Optional[ThreadPoolExecutor] = None
        #: Sprint 71 (Camera Patrol) - optional, empty-by-default list of
        #: `(tool_call_dict) -> None` callables invoked right before a
        #: tool call actually executes (see `_process_event()` below).
        #: Added ONLY so `CameraPatrolModule` can enforce its own Phase 5
        #: ownership rule (stop an active patrol before a genuinely
        #: MANUAL camera_ptz command executes) WITHOUT this class needing
        #: to know anything about patrol/cameras specifically - every
        #: other tool call is completely unaffected (empty list, no-op
        #: loop). A hook that raises is logged and ignored - it must
        #: never block or fail a tool call the user is actively waiting
        #: on. See `register_pre_dispatch_hook()`.
        self._pre_dispatch_hooks: List[Callable[[Dict[str, Any]], None]] = []

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def register_pre_dispatch_hook(self, hook: Callable[[Dict[str, Any]], None]) -> None:
        """Sprint 71 (Camera Patrol) - registers `hook` to be called with
        a plain `{"tool", "action", "target", "parameters"}` dict right
        before EVERY tool call executes (see `_process_event()`).
        Additive, backward compatible: no caller registers anything by
        default, so this is a pure no-op for every tool/test that
        existed before this sprint."""
        self._pre_dispatch_hooks.append(hook)

    @staticmethod
    def _tool_call_as_dict(tool_call: Any) -> Dict[str, Any]:
        """Normalizes `tool_call` (a plain dict from `RuntimeDemoConsole.
        _execute_tool`, or a `luno.planner.models.ToolCall`-shaped object
        from `PlannerBridgeModule._tool_bridge_handler`) into a plain
        dict - same duck-typing approach `luno.tool_manager.models.
        ToolCall.from_any()` already uses for the identical problem."""
        if isinstance(tool_call, dict):
            return tool_call
        parameters = getattr(tool_call, "parameters", None)
        if parameters is None:
            parameters = getattr(tool_call, "params", None) or {}
        return {
            "tool": getattr(tool_call, "tool", None),
            "action": getattr(tool_call, "action", None),
            "target": getattr(tool_call, "target", None),
            "parameters": parameters,
        }

    def start(self) -> None:
        self._worker_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="luno-tool-manager")

    def stop(self) -> None:
        pool, self._worker_pool = self._worker_pool, None
        if pool is not None:
            pool.shutdown(wait=False)

    def health(self) -> ModuleHealthStatus:
        return ModuleHealthStatus(healthy=True)

    def on_event(self, event: Event) -> None:
        """Never blocks the caller (the Event Bus's delivery thread, via
        `Coordinator`) - see the class docstring. Submits to this
        module's own single-worker pool and returns immediately; the
        actual execution happens in `_process_event()`."""
        if event.type != "tool_requested" or self._event_bus is None:
            return
        pool = self._worker_pool
        if pool is None:
            log(f"tool_manager.on_event('{event.type}') dropped - module is not started", "tool_manager")
            return
        try:
            pool.submit(self._process_event, event)
        except RuntimeError:
            # pool was shut down between the None-check and submit() (a
            # stop() raced this call) - drop the event rather than crash,
            # same defensive pattern as BaseAdapter.on_event().
            log(f"tool_manager.on_event('{event.type}') dropped - module stopped mid-dispatch", "tool_manager")

    def _process_event(self, event: Event) -> None:
        execution_id = event.get("execution_id") or event.event_id
        tool_call = event.get("tool_call")
        if tool_call is None:
            self._event_bus.publish(Event(type="tool_failed", data={"execution_id": execution_id, "error": "no tool_call given"}))
            return

        from luno.core.events import ToolFinished, ToolStarted

        tool_name = getattr(tool_call, "tool", None) or (tool_call.get("tool") if isinstance(tool_call, dict) else "?")
        with self._lock:
            self.last_tool = tool_name
        self._event_bus.publish(ToolStarted(data={"execution_id": execution_id, "tool": tool_name}))
        if self._pre_dispatch_hooks:
            tool_call_dict = self._tool_call_as_dict(tool_call)
            for hook in self._pre_dispatch_hooks:
                try:
                    hook(tool_call_dict)
                except Exception as ex:
                    log(f"pre-dispatch hook raised (ignored): {ex}", "tool_manager")
        t0 = time.time()
        try:
            result = self.manager.execute(tool_call)
        except Exception as ex:
            self._event_bus.publish(Event(type="tool_failed", data={
                "execution_id": execution_id, "tool": tool_name, "error": str(ex),
            }))
            return
        elapsed_ms = (time.time() - t0) * 1000.0
        payload = result.to_dict()
        payload["execution_id"] = execution_id
        payload["execution_time_ms"] = payload.get("execution_time_ms") or elapsed_ms
        with self._lock:
            self.last_result = payload
        if result.success:
            self._event_bus.publish(ToolFinished(data=payload))
        else:
            self._event_bus.publish(Event(type="tool_failed", data=payload))


def _ttl_cached(fn, ttl_s: float = 1.0):
    """Wrap a zero-arg callable so repeated calls within `ttl_s` seconds
    reuse the last result instead of re-querying. Sprint 5's memory
    retrieval registers TWO sources (`vision_objects`, `vision_human`)
    that each independently call `vm.get_world_state()` once per turn -
    on this project's real (SQLite-backed) Vision Memory store that was
    measured at ~56ms per call, so a naive double-call was adding real,
    avoidable latency to every single turn for no benefit (the world
    state can't meaningfully change between those two calls, which
    happen microseconds apart in the same `retrieve_memories()` pass).
    A short TTL - not permanent caching - keeps this safe for actual
    production use, where the world state DOES need to reflect fresh
    vision observations turn to turn."""
    _cache: Dict[str, Any] = {"value": None, "at": 0.0}

    def _wrapped():
        now = time.monotonic()
        if now - _cache["at"] > ttl_s:
            _cache["value"] = fn()
            _cache["at"] = now
        return _cache["value"]

    return _wrapped


def build_verified_action_notes(real_tasks: List[Task], user_text: str) -> List[str]:
    """Reliability Sprint (Never Assume Success) - turns a plan's non-
    'unknown' tasks into the LLM-facing notes that report what actually,
    verifiably happened - never a blanket "confirm this succeeded".

    `task.result` (set only on `TaskStatus.COMPLETED` - see
    `luno/planner/executor.py::TaskExecutor._safe_run`) is exactly
    `_tool_bridge_handler()`'s return value, which is exactly
    `ToolFinished.data` = `ToolResult.to_dict()` (see
    `PlannerBridgeModule._tool_bridge_handler` and `ToolManagerModule.
    on_event` below) - so `task.result["message"]` is already the
    Reliability Sprint's own honest, VERIFIED phrasing ("I've turned on
    Bedroom Light." / "I tried to turn on Bedroom Light, but it didn't
    respond."). `task.error` (set on `TaskStatus.FAILED`) is
    `_tool_bridge_handler`'s raised exception's message - same
    underlying `ToolResult.message`/`error_type` text, just reached via
    the raise-on-failure path instead of a return value (this is also
    what makes the Planner's own `TaskStatus` already correctly FAILED
    rather than COMPLETED whenever `ToolResult.success` is False -
    `_tool_bridge_handler` raises instead of returning in exactly that
    case, so `TaskExecutor` never has to guess). `executor.py`'s
    `_handle_failure` additionally preserves the full failed payload as
    `task.result` too (not just the string in `task.error`) when the
    handler provided one, so `data` (expected_state/actual_state/...)
    survives even for a failed task.

    BUG this fixes: only `completed` tasks ever got a note before this
    function existed, built from the task's LABEL alone, telling the LLM
    to "confirm this succeeded" regardless of what the verified
    `ToolResult.message` actually said - and FAILED tasks got no note at
    all, leaving the LLM with no information to contradict its own
    default assumption that a request it was just asked to fulfil went
    fine.

    TODO(World Model): this project has no dedicated World Model module
    yet (only `luno/proactive/context_evaluator.py` reads a small ad-hoc
    snapshot). Once one exists, the correct hook is right where
    `completed_lines` is built below: update the World Model with
    `data["entity_id"] = data["actual_state"]` (from `task.result["data"]`)
    for each completed entry - and deliberately NOT for anything in
    `failed_lines`, so the World Model never drifts from the real,
    verified device state.
    """
    notes: List[str] = []

    completed_lines: List[str] = []
    for task in real_tasks:
        if task.status.value != "completed":
            continue
        label = task.label or f"{task.tool_call.tool}.{task.tool_call.action}"
        message = task.result.get("message") if isinstance(task.result, dict) else None
        completed_lines.append(f"{label}: {message}" if message else f"{label}: succeeded")
    if completed_lines:
        notes.append(
            f"The user said: \"{user_text}\". You already ran the following action(s) and these are the "
            f"VERIFIED results (already confirmed against the real device/service state) - report "
            f"these facts naturally and briefly, do not add any success claim beyond what's stated "
            f"here, and do not ask the user to do anything, it is already done:\n"
            + "\n".join(completed_lines)
        )

    failed_lines: List[str] = []
    for task in real_tasks:
        # BUG FIX (reported): a SKIPPED task (see scheduler.py's
        # `_apply_failure_policy`/`_cascade_skip_blocked` - "an earlier
        # step in this plan failed, so this one never ran") used to be
        # invisible here entirely: not in `completed_lines` (never ran),
        # and excluded from this tuple too, so it appeared in NEITHER
        # note. The LLM had zero information about that action and would
        # often default to an implicitly-positive summary for the whole
        # request - a real hallucinated-success case this whole "never
        # hallucinate device control" effort exists to prevent, just via
        # omission rather than a fabricated ToolResult. "skipped" now
        # gets the same honest, mandatory-negative treatment as
        # "failed"/"cancelled".
        if task.status.value not in ("failed", "cancelled", "skipped"):
            continue
        label = task.label or f"{task.tool_call.tool}.{task.tool_call.action}"
        if task.status.value == "skipped":
            detail = task.error or "skipped because an earlier step in this request didn't complete"
        else:
            detail = task.error or "failed"
        # Surface expected/actual state too when present (now available
        # even on failure - see executor.py's _handle_failure), since
        # "verification failed, wanted X, saw Y" is more useful to the
        # LLM than the message string alone.
        data = task.result.get("data") if isinstance(task.result, dict) else None
        if isinstance(data, dict) and data.get("expected_state") is not None:
            detail += f" (expected_state={data.get('expected_state')}, actual_state={data.get('actual_state')})"
        failed_lines.append(f"{label}: {detail}")
    if failed_lines:
        notes.append(
            "IMPORTANT - the following action(s) did NOT succeed. Never say or imply they worked. "
            "Tell the user honestly, in your own natural words, that it didn't work, using these "
            "facts (do not invent a different reason than what's given):\n"
            + "\n".join(failed_lines)
        )

    return notes


# ============================================================================
# Module: Planner bridge
# ============================================================================

class PlannerBridgeModule(Module):
    """Listens for `"user_utterance"` (published by `BehaviorTreeModule`'s
    `generate_reply` handler - see below), turns it into a `luno.planner`
    `Plan` (unmodified, real heuristic parsing/validation/execution),
    drives any tasks to completion PURELY by publishing `ToolRequested`
    and waiting for the correlated `ToolFinished`/`ToolFailed` (never
    calling `ToolManager` directly - see `_tool_bridge_handler`), then
    publishes `NeedLLMResponse` so OpenRouter can phrase the actual reply.
    `luno.planner` itself never needed to know any of this - the only
    integration point is `Planner.registry.register()`, exactly the
    "future Tool Manager" seam that package's own docstring describes."""

    name = "planner"
    dependencies: List[str] = ["tool_manager", "openrouter"]

    #: every REAL tool name IntentParser.parse() can currently produce -
    #: see luno/planner/parser.py. Registering the SAME bridging handler
    #: under all of them keeps this list the only place that needs to
    #: track that vocabulary.
    #:
    #: Deliberately does NOT include "unknown" - that's IntentParser's
    #: own synthetic sentinel for "this utterance isn't a device command
    #: at all" (plain conversation, a question, ...), not a real tool
    #: with a Tool Manager handler behind it. It used to be registered
    #: here anyway (bridged through to the Tool Manager, which has never
    #: had an "unknown" handler), producing a harmless but noisy
    #: "No handler registered for tool 'unknown'" failure on EVERY plain-
    #: conversation turn. `_handle_utterance()` below now skips calling
    #: `self.planner.execute(plan)` entirely whenever a plan has no REAL
    #: (non-"unknown") tasks, so that round-trip - and this registration -
    #: is no longer needed. See that method's own comment at the execute
    #: gate for the one remaining edge case (a MIXED utterance with both
    #: a real command and an unrelated clause in the same sentence).
    KNOWN_TOOLS = (
        "windows", "browser", "home_assistant", "spotify", "vision", "unity",
        "camera_ptz", "llm_mode", "dummy",
    )

    def __init__(self, tool_timeout_s: float = 15.0, speaking_mode_config: Optional[BargeInConfig] = None,
                 turn_settle_timeout_s: float = 2.0) -> None:
        self.planner = TaskPlanner()
        for tool_name in self.KNOWN_TOOLS:
            self.planner.registry.register(tool_name, handler=self._tool_bridge_handler)
        self._event_bus = None
        self.tool_timeout_s = tool_timeout_s
        #: Conversation_end Race Safety sprint - bounded wait (same
        #: "configurable timeout, same shape as `tool_timeout_s` above"
        #: convention already established in this class) for
        #: `_on_conversation_ended()` to give an in-flight turn's
        #: feedback-relevant processing a chance to "settle" before the
        #: final adaptive-preference merge reads `_depth_preference`. See
        #: `_wait_for_turn_to_settle()`/`_mark_turn_settled()` below and
        #: docs/change_impact/conversation_end_race_safety.md.
        self.turn_settle_timeout_s = turn_settle_timeout_s
        self.last_plan_id: Optional[str] = None
        #: Sprint 3 - rule-based FREE/SOFT/CONFIRM classification (never
        #: CRITICAL here; that's an emergency override BargeInModule
        #: applies itself at interrupt time, using its OWN independently
        #: tracked emergency state - see luno/barge_in/manager.py).
        self._speaking_mode_config = speaking_mode_config or BargeInConfig.from_env()

        # Sprint 5 - Smart Memory Injection. Standalone, self-contained
        # here (doesn't need to live on ContextBuilder/the console - see
        # luno/memory_retrieval's own docstring for why): every source is
        # bound to the SAME `vm` module-level facade already imported
        # above, read-only, no live vision model call ever happens from
        # any of these (get_world_state/get_recent_events/
        # get_long_term_memory are all cached-state reads - see
        # luno/vision_memory/api.py). `_handle_utterance()` calls
        # `retrieve_memories(text)` once per turn, right before building
        # this turn's system_prompt/messages for NeedLLMResponse.
        self.memory_retriever = MemoryRetriever(MemoryRetrievalConfig.from_env())
        # Memory Guard Sprint - "Memory stores verified facts, not
        # generated language." Standalone, same reasoning as
        # `memory_retriever` above: doesn't need to live on
        # ContextBuilder/the console. Never reads LLM output, never
        # calls Home Assistant or an LLM - see luno/memory_guard.py's
        # own docstring.
        self.memory_guard = VerifiedFactStore()
        # World Model Sprint - "the Single Source of Truth" for current
        # device state. Built entirely on what already exists (Event
        # Bus, verified ToolResult, HA's own state_changed) - see
        # luno/world_model.py's own docstring. `event_bus` wired in
        # `bind_event_bus()` below (not yet available here in __init__).
        self.world_model = WorldModel()
        # Intelligent AI Routing Engine sprint - the Decision Engine.
        # Standalone, same reasoning as memory_retriever/memory_guard/
        # world_model above: doesn't need to live on ContextBuilder/the
        # console. Reads NOTHING itself - every knowledge source it
        # consults below (`relevant_memories_early`, `self.world_model`,
        # this turn's own verified tool results) was ALREADY fetched by
        # this same method for other reasons; this only ever adds a
        # `provider`/`model` hint plus an optional extra context note to
        # the `NeedLLMResponse` this method was already about to publish
        # - Planner/Behavior Tree/Tool Manager/World Model/Memory
        # Retrieval/the LLM Manager itself are never bypassed or
        # modified (see `luno/routing/__init__.py`'s own docstring).
        self.decision_engine = DecisionEngine(RoutingConfig.from_env())
        # Efficient LLM Classifier sprint - generic, tool-agnostic pending-
        # confirmation store for the "classifier is only MEDIUM confidence"
        # case (`RoutingDecision.needs_confirmation` - see
        # `decision_engine.py`'s own docstring). `_handle_utterance` checks
        # this BEFORE normal processing (same tier as the browser-
        # permission/environmental-intent confirmation checks below) and
        # populates it AFTER `decision_engine.decide()` returns
        # `needs_confirmation=True` - see both call sites below for the
        # full flow. `luno.routing.confirmation.ConfirmationHandler`'s own
        # docstring covers the full design (deterministic/template
        # prompts, one-shot, TTL-bounded, never a browser_permissions
        # fork).
        self.confirmation_handler = ConfirmationHandler()
        # Emotion Engine sprint - user-emotion-aware conversational
        # adaptation (see luno/emotion_engine.py's own module docstring
        # for the full design + why this is NOT the same thing as
        # luno/behavior_tree/emotion.py). Standalone, same reasoning as
        # memory_retriever/memory_guard/world_model/decision_engine/
        # confirmation_handler above: one instance per bridge, observed
        # and injected inline in `_handle_utterance` below, reset at the
        # same conversation-boundary hook `_on_conversation_ended`
        # already uses for `_last_device_target`/`_pending_env_
        # confirmations`. Fully additive - if this ever raised (it is
        # designed not to), the try/except around its call site in
        # `_handle_utterance` degrades to "no emotional-context note this
        # turn", never a broken turn.
        self.emotion_tracker = EmotionStateTracker()
        # Relationship Engine Foundation sprint - persistent, deterministic
        # relationship state (see luno/relationship_engine.py's own module
        # docstring for the full design + dependency-direction rationale:
        # Memory/Emotion Engine -> Relationship Engine -> Relationship
        # Context Builder -> this same "notes" prompt pipeline, never the
        # reverse). Loaded once at startup (mirrors `luno.persona.PERSONA`'s
        # own "load once, keep as a value" convention - simpler than
        # `luno.memory`'s bare module-level globals, and avoids any cross-
        # test global-state leakage since it lives on this instance).
        # Updated + persisted once per turn in `_handle_utterance` below;
        # a failure anywhere in that path degrades to "no relationship-
        # context note this turn, state simply doesn't advance" - never a
        # broken turn, same try/except convention as every other note.
        self.relationship_state = RelationshipStore.load()
        # Shared TTL-cached wrapper (see `_ttl_cached` above): both vision
        # sources below call this same wrapped callable rather than
        # `vm.get_world_state` directly, so a single `retrieve_memories()`
        # pass costs at most ONE real SQLite query instead of two.
        _cached_get_world_state = _ttl_cached(vm.get_world_state, ttl_s=1.0)
        self.memory_retriever.register_source("vision_objects", make_vision_object_source(_cached_get_world_state))
        self.memory_retriever.register_source("vision_human", make_vision_human_source(_cached_get_world_state))
        self.memory_retriever.register_source("vision_events", make_vision_event_source(vm.get_recent_events))
        self.memory_retriever.register_source("long_term_memory", make_long_term_memory_source(vm.get_long_term_memory))
        self.memory_retriever.register_source("planner_state", make_planner_state_source(lambda: {"last_plan_id": self.last_plan_id}))
        # Shared Experience & Episodic Memory sprint - one more
        # `MemorySource`, registered exactly like every source above (see
        # luno/episodic_memory.py's own module docstring for why this is
        # NOT a parallel retrieval/bounding/temporal-wording system: all of
        # that already happens for free once registered here). Provider is
        # `EpisodicMemoryStore.load` itself (zero-arg, reads
        # config.EPISODIC_MEMORY_FILE fresh each call) - same "hand in a
        # snapshot-producing callable" shape as `make_long_term_memory_source
        # (vm.get_long_term_memory)` just above.
        self.memory_retriever.register_source(
            "episodic_memory",
            episodic_memory.make_episodic_experience_source(episodic_memory.EpisodicMemoryStore.load),
        )
        # Manual Memory Management sprint - one more `MemorySource`,
        # registered exactly like every source above (see
        # `luno/memory.py`'s `make_manual_memory_source()` docstring).
        # Provider is `memory.list_memories` itself (zero-arg, reads the
        # SAME `_memories` global `_handle_explicit_memory_command()`
        # below reads/writes - no second store). Registered as
        # "manual_memory", NOT "long_term_memory" - that name is already
        # taken by Vision Memory's own internal long-term-habits source
        # two lines above (see that source's own docstring for the
        # naming-collision note).
        self.memory_retriever.register_source(
            "manual_memory",
            memory.make_manual_memory_source(memory.list_memories),
        )

        # Device-intent classifier (opt-in, AI-assisted fallback for when
        # IntentParser's fast regex parser can't classify the utterance at
        # all - see `_classify_device_intent()` below for the full design
        # rationale). `None` by default - a launcher wires this in AFTER
        # both modules and adapters exist (same reason `RealHomeAssistantHandler`
        # is wired in post-hoc in `luno/bootstrap/adapters.py::
        # register_real_tool_handlers()`: the OpenRouter adapter this
        # needs isn't built yet when `PlannerBridgeModule` is constructed).
        # `main_runtime_demo.py`'s own demo/test usage never sets these, so
        # the feature is fully inert (zero behavior change, zero extra LLM
        # calls) unless a launcher opts in.
        self.device_intent_client: Optional[Any] = None
        self.device_intent_model: Optional[str] = None

        # Session summaries (config/session_summaries.json via
        # luno/memory.py) - same opt-in-by-construction pattern as
        # device_intent_client above: `None` until a launcher wires in a
        # real OpenRouter client post-hoc (see `register_session_summary_
        # client()` in luno/bootstrap/adapters.py). Fully inert otherwise.
        self.session_summary_client: Optional[Any] = None
        self.session_summary_model: Optional[str] = None
        # request_id -> (user_text, conversation_id) for turns still
        # awaiting their `assistant_response` (see `_on_assistant_response`
        # below, which pairs the two up and calls `memory.remember_turn()`
        # so session_log actually accumulates real turns for later
        # summarization). Bounded (see `_remember_pending_turn`) so a
        # turn that never gets a reply (e.g. cancelled mid-flight) can't
        # leak memory across a long-running process. `conversation_id` was
        # added to the stored tuple by Sprint 4 (Memory Continuity) so
        # `_on_assistant_response` can key `_active_topic` (below) by the
        # same conversation the turn belongs to, without needing the
        # `assistant_response` event itself to carry `conversation_id`.
        self._pending_turns: Dict[str, Tuple[str, Optional[str]]] = {}
        self._pending_turns_max = 50

        # Environmental intent inference ("hawanya panas nih" -> propose
        # the AC) - see `_handle_environmental_intent()` below and
        # `luno/environment_intent.py`'s own docstring for the full
        # design. Keyed on conversation_id (a fixed sentinel for a
        # caller that never sets one - see `_ENV_CONFIRMATION_KEY`
        # below), value is a dict with "command"/"cue"/"decline_ack"/
        # "cue_description"/"expires_at" - see `_handle_environmental_
        # intent()` for the exact shape. One entry per live
        # conversation, never grows unboundedly (each entry is
        # consumed - popped - by the very next turn in that
        # conversation, confirmed/declined/expired either way).
        self._pending_env_confirmations: Dict[str, Dict[str, Any]] = {}

        # Browser/computer-use permission system (spec-mandated - see
        # luno/browser/permissions.py's own docstring). Cheap, pure-
        # stdlib construction (no Playwright/browser dependency at all
        # here) - safe to always build, independent of whether
        # BROWSER_ENABLED is even on; `PermissionManager` only ever
        # reasons about action names/params, never touches a real
        # browser itself.
        from luno.browser.permissions import PermissionManager as _BrowserPermissionManager
        from luno.browser.config import BrowserConfig as _BrowserConfig
        self.browser_permissions = _BrowserPermissionManager(
            require_confirmation_for_low_risk=_BrowserConfig.from_env().require_confirmation
        )

        # Short-term device-context memory ("aktifkan lampu kamar" ...
        # then, next turn, "sekarang matikan" with NO device named at all
        # - understood as "matikan lampu kamar", the same device just
        # talked about) - see `_apply_device_context()` below for the
        # full mechanism. Keyed on conversation_id (same
        # `_ENV_CONFIRMATION_KEY` sentinel fallback as the environmental-
        # intent state above), value is `{tool_name: last_target}` - a
        # SEPARATE slot per tool, so a camera preset target never gets
        # reused for a light command or vice versa. Reset per-
        # conversation in `_on_conversation_ended` below (this is
        # genuinely SHORT-term - it must not leak into a brand new
        # conversation later) and bounded here too (same `_pending_
        # turns_max` precedent) so a long-running process serving many
        # distinct conversation_ids over time can't grow this dict
        # unboundedly even if `_on_conversation_ended` is never reached
        # for some of them (e.g. the process is killed mid-conversation).
        #
        # Sprint 57 (Contextual Home Assistant References) - the
        # `"home_assistant"` slot's own value was widened from a bare
        # target string to a small dict, `{"target": str, "turn_seq":
        # int, "entity_id": Optional[str], "domain": Optional[str]}` -
        # the SAME concept, just enough metadata to (1) let a bounded
        # number of turns pass before a remembered device goes stale
        # within one long-running conversation (`turn_seq`, checked
        # against `_device_context_turn_seq` - NOT a second clock, see
        # that dict's own docstring), and (2) let a home_assistant tool
        # call that later fails be traced back to the SAME remembered
        # device and un-remembered (see `_invalidate_device_context_on_
        # failure()`) - "a failed or ambiguous HA command must not
        # become a strong contextual target." The `"camera_ptz"` slot is
        # UNCHANGED (still a bare `True` marker - camera_ptz has no
        # device identity to track, there is only ever the one camera).
        self._last_device_target: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._last_device_target_max = 50
        #: Sprint 57 (Contextual Home Assistant References) - per-
        #: conversation monotonic "how many turns has this conversation
        #: had" counter, bumped once per `_apply_device_context()` call.
        #: The ONLY use is `_last_device_target`'s own per-tool
        #: `"turn_seq"` field (see that dict's own updated docstring
        #: below) - a bounded FRESHNESS signal ("was this device
        #: mentioned recently, or several turns ago in a long-running
        #: conversation") layered on top of the pre-existing per-
        #: conversation reset, not a replacement for it. Same bounding/
        #: reset discipline as `_last_device_target` itself (popped in
        #: `_on_conversation_ended()`, bounded by the same `_last_device_
        #: target_max`) - deliberately NOT a new standalone dict with its
        #: own lifecycle; it is reset/evicted in lockstep with the
        #: dict it exists to annotate.
        self._device_context_turn_seq: Dict[str, int] = {}
        #: Sprint 57 - `threading.local()` slot holding "which
        #: conversation_id is the CURRENTLY-RUNNING `_handle_utterance()`
        #: call, on THIS thread, processing" - set once at the top of
        #: `_handle_utterance()`, read only by `_invalidate_device_
        #: context_on_failure()` (see that method's own docstring for
        #: why: `_tool_bridge_handler()` learns a tool call's real
        #: success/failure synchronously, deep in the same call stack as
        #: `_handle_utterance()`, but `luno.planner.ToolCall` itself
        #: carries no conversation identity - see Sprint 57's own
        #: reconnaissance note on `ToolCall`'s independence from the
        #: Planner). `on_event()` spawns a fresh `luno-planner-turn`
        #: thread per utterance (see that method's own docstring), so a
        #: `threading.local()` slot - not a bare instance attribute -
        #: keeps two conversations processed concurrently on their own
        #: threads from ever cross-contaminating each other's device
        #: memory here, matching every other per-conversation dict in
        #: this class.
        self._tool_bridge_local = threading.local()

        # Memory Learning & Feedback Loop sprint - "session feedback
        # target" (spec Section 13): the id of the single manual-memory
        # entry this conversation most recently, unambiguously surfaced
        # through `self.memory_retriever.retrieve_memories()` - the target
        # a LATER "iya benar"/"itu salah" reply resolves against (see
        # `_handle_memory_feedback_command()`/`_update_session_feedback_target()`
        # below). Same scoping/reset/bounding convention as
        # `_last_device_target` immediately above (keyed on conversation_id,
        # falls back to `_ENV_CONFIRMATION_KEY` for a caller that never sets
        # one, reset per-conversation in `_on_conversation_ended`, bounded
        # here too) - a deliberately SEPARATE dict, not reused, since a
        # "last device commanded" and "last memory surfaced" are different
        # concepts that can legitimately disagree turn to turn. One entry
        # per conversation (a single memory_id or absent, never a list) -
        # kept intentionally unambiguous: if more than one memory was
        # surfaced this turn, there is no single target at all (see
        # `_update_session_feedback_target()`), so this dict never needs to
        # store more than one id per conversation.
        self._session_feedback_target: Dict[str, str] = {}
        self._session_feedback_target_max = 50

        # Memory Retrieval & Decision Quality sprint (Phase 2 - Topic
        # Continuity) - THIS conversation's bounded, deterministic "topic
        # terms" snapshot from its most recent turn
        # (`memory_context.extract_topic_terms()` - reuses the EXISTING
        # tokenizer, capped to a small fixed size, never appended to,
        # always fully REPLACED). Read at the start of the NEXT turn (see
        # `_handle_utterance()`) to give a `continuation_of_topic`-
        # classified turn ("lanjut coding Luno yang tadi") a small,
        # bounded ranking preference toward context related to the
        # PREVIOUS turn's topic - never a relevance override (see
        # `memory_context._apply_decision_quality_bonus()`). Same exact
        # scoping/reset/bounding convention as `_session_feedback_target`
        # immediately above: keyed on conversation_id (`_ENV_CONFIRMATION_KEY`
        # sentinel fallback), popped in `_on_conversation_ended()` (so
        # Conversation A's topic can never leak into Conversation B, and a
        # new conversation never inherits a stale topic from one that
        # ended minutes/hours ago), bounded here too. Purely in-memory,
        # transient, per-process runtime state - NEVER persisted to disk,
        # and NEVER holds raw utterance text, only a bounded token set
        # (Phase 2's own explicit "do not persist raw user text" / "no
        # global topic state" requirements).
        self._last_topic_terms: Dict[str, frozenset] = {}
        self._last_topic_terms_max = 50

        # Memory Continuity & Short Follow-up Reference Resolution sprint
        # (Sprint 4) - a SEPARATE, additive, per-conversation "what is this
        # conversation actively about right now" snapshot
        # (`memory_context.ActiveTopicSnapshot`), deliberately NOT merged
        # into `_last_topic_terms` immediately above. Phase 0's audit found
        # `_last_topic_terms` is read only when `query_intent ==
        # "continuation_of_topic"` - a narrow intent none of this sprint's
        # target short-follow-up phrases ("yang lain?", "terus?", "other
        # option?", ...) ever produce - so `_last_topic_terms` genuinely
        # cannot serve this sprint's purpose without widening that gate
        # (which would change an already-tested contract other sprints
        # pinned). This dict instead persists across a RUN of short
        # follow-ups (see `memory_context.update_active_topic()`'s own
        # replace-vs-preserve rule) rather than being unconditionally
        # replaced every turn. Written in `_on_assistant_response()` below
        # (the one place both this turn's user text AND its finalized
        # reply text are simultaneously available), read in
        # `_handle_utterance()`. Keyed on conversation_id (same
        # `_ENV_CONFIRMATION_KEY` sentinel fallback as `_last_topic_terms`),
        # popped in `_on_conversation_ended()`, bounded here too. Purely
        # in-memory, transient, per-process runtime state - NEVER persisted
        # to disk, and NEVER holds raw utterance/reply text, only bounded
        # token sets (same "no raw conversation dump" requirement as
        # `_last_topic_terms`).
        self._active_topic: Dict[str, "memory_context.ActiveTopicSnapshot"] = {}
        self._active_topic_max = 50

        # Memory Topic Retention & Recall Reliability sprint - a SEPARATE,
        # ADDITIVE bounded HISTORY of recent `ActiveTopicSnapshot`s per
        # conversation (`List[...]`, most-recent-first, capped by
        # `memory_context._TOPIC_HISTORY_MAX_ENTRIES`), deliberately kept
        # alongside `_active_topic` above rather than replacing it -
        # `_active_topic` (single slot) is Sprint 4's own, already-tested
        # mechanism for TRUE elliptical follow-ups and is left completely
        # unmodified; this dict exists only to answer a DIFFERENT
        # question Sprint 4 never needed to answer: "does an OLDER,
        # displaced topic (not just the single most recent one) still
        # match what this grammatically-complete turn is asking about?"
        # (see `memory_context.select_topic_candidates()`). Same
        # guarantees as `_active_topic`: purely in-memory, transient,
        # per-process runtime state, keyed on conversation_id, bounded in
        # count here and popped in `_on_conversation_ended()`, never
        # holds raw utterance/reply text - only bounded token sets.
        self._topic_history: Dict[str, List["memory_context.ActiveTopicSnapshot"]] = {}
        self._topic_history_max = 50

        # Memory Decision Quality & Adaptive Retrieval sprint - THIS
        # conversation's query context category (`luno.memory.
        # classify_query_context_category()`) at the moment
        # `_session_feedback_target` was last set - so a LATER "iya
        # benar"/"itu salah" reply can attribute its outcome evidence to
        # the SAME context the memory was originally surfaced in, not the
        # (often context-less, e.g. "iya benar" itself) confirmation
        # text. Always kept in lockstep with `_session_feedback_target`
        # (set together, popped together, at every one of that dict's own
        # call sites) - same bounded/reset convention, a deliberately
        # separate dict rather than folding a tuple into
        # `_session_feedback_target` so that dict's own existing,
        # already-tested `Dict[str, str]` typing/tests are untouched.
        self._session_feedback_context: Dict[str, str] = {}

        # Memory Outcome Telemetry & Closed-Loop Learning sprint - THIS
        # conversation's most recent `MemoryTurnTrace` (see
        # `luno/memory_turn_trace.py`). Same scoping/reset/bounding
        # convention as `_session_feedback_target` immediately above - one
        # entry per conversation (the trace is fully REPLACED each turn,
        # never appended to), reset in `_on_conversation_ended()`, capped
        # here too. Deliberately holds ONLY the transient `MemoryTurnTrace`
        # object (ids/scores/short reason strings - see that class's own
        # docstring for why it never carries message text) - this dict is
        # never written to disk, satisfying hard constraint #16/#17
        # (bounded telemetry, no persisted transcript) by construction:
        # even in the worst case (every one of `_max` conversations at
        # once), this holds at most `_max` small trace objects, never a
        # growing log.
        self._last_turn_trace: Dict[str, "Any"] = {}
        self._last_turn_trace_max = 50

        # Memory & Voice Observability Dashboard sprint - a SEPARATE,
        # additive, bounded ring buffer of the most recent
        # `MemoryTurnTrace`s ACROSS every conversation (not one-per-
        # conversation like `_last_turn_trace` above, which the Memory
        # Outcome Telemetry sprint deliberately designed to be REPLACED
        # each turn - a bound this sprint does not touch). This one
        # exists purely so the dashboard's turn-level inspector (Phase 5)
        # can browse the last N turns, not just each conversation's
        # single most-recent one - `deque(maxlen=...)` is a hard, fixed
        # bound (never grows unbounded regardless of session length,
        # same guarantee `_last_turn_trace` already has via its own
        # while-loop eviction). Holds the exact same small, transient
        # `MemoryTurnTrace` objects `_last_turn_trace` already holds (no
        # new object type, no raw conversation text, no persistence) -
        # this is a second REFERENCE to data already being built, not a
        # second memory system.
        self._turn_trace_history: Deque["Any"] = deque(maxlen=100)

        # Response Depth Policy sprint - THIS conversation's last
        # resolved response-depth SCORE (an int, never the full
        # `ResponsePolicy` object - only the one number
        # `compute_response_policy()`'s own `previous_score` parameter
        # needs), so a short follow-up turn doesn't jarringly reset to
        # SHORT. Same bounded/reset convention as `_session_feedback_target`/
        # `_last_turn_trace` above - one entry per conversation, popped in
        # `_on_conversation_ended()`, capped here too. Never persisted to
        # disk, never a new memory system - purely in-memory, transient,
        # per-process runtime state, exactly like its siblings above.
        self._response_depth_context: Dict[str, int] = {}
        self._response_depth_context_max = 50

        # Adaptive Response Depth Learning sprint - THIS conversation's
        # bounded, adaptive depth-PREFERENCE signal (see
        # `luno.response_policy.DepthPreference`/`apply_depth_feedback()`).
        # Same exact scoping/reset/bounding convention as
        # `_response_depth_context` immediately above (and every other
        # per-conversation dict in this class): one entry per
        # conversation, popped in `_on_conversation_ended()`, capped here
        # too. NEVER persisted to disk, NEVER a new memory system - purely
        # in-memory, transient, per-process runtime state. Deliberately a
        # SEPARATE dict from `_response_depth_context` (which holds the
        # last resolved SCORE, consumed for a different purpose -
        # conversational continuation) even though both are read at the
        # same call site, so the two concepts (continuation vs. adaptive
        # preference) never get entangled into one ambiguous value - see
        # docs/change_impact/adaptive_response_depth.md.
        self._depth_preference: Dict[str, "DepthPreference"] = {}
        self._depth_preference_max = 50

        # Voice Output Mode sprint - THIS conversation's sticky ALL/SHORT
        # voice output mode. Same exact per-conversation bounded-dict
        # convention as `_response_depth_context`/`_depth_preference`
        # immediately above/below: one entry per conversation_id, popped
        # in `_on_conversation_ended()`, capped here too, NEVER persisted
        # to disk, NEVER a new memory system, NEVER shared/global across
        # conversations (brief's own explicit Phase 4 requirement) -
        # purely in-memory, transient, per-process runtime state.
        # Deliberately SEPARATE from `_depth_preference` (a different
        # concept - PER-TURN depth compression bias vs. this STICKY,
        # explicitly-toggled voice-pipeline mode) even though both are
        # read/written from the same `_handle_utterance()` call site, so
        # the two never get entangled into one ambiguous value - same
        # separation discipline `_depth_preference`'s own docstring above
        # already establishes for itself vs. `_response_depth_context`.
        # A conversation_id with no entry yet resolves to
        # `DEFAULT_VOICE_OUTPUT_MODE` ("SHORT") via `get_voice_output_mode()`
        # below - existing behavior is completely unchanged until a real
        # mode-switch command (or explicit `set_voice_output_mode()` call)
        # ever touches this dict for that conversation.
        self._voice_output_mode: Dict[str, str] = {}
        self._voice_output_mode_max = 50

        # Persistent Adaptive Response Depth Preference sprint - the ONE
        # cross-session baseline (see `luno/response_depth_preference.py`),
        # loaded once at startup exactly like `self.relationship_state =
        # RelationshipStore.load()` below. Missing/corrupt file -> neutral
        # default (`bias=0`), so a fresh install behaves identically to
        # before this sprint existed. `_persistent_depth_preference_lock`
        # guards the read-merge-write sequence in `_update_depth_preference()`
        # and `_on_conversation_ended()` against concurrently-active
        # conversations' background turn threads racing on the same
        # in-memory value and on-disk file - the per-conversation
        # `_depth_preference` dict above stays completely unguarded/
        # unshared as before, since each entry is only ever touched by its
        # own conversation's turn thread.
        self._persistent_depth_preference = DepthPreferenceStore.load()
        self._persistent_depth_preference_lock = threading.Lock()
        # A FROZEN snapshot of the bias loaded above, taken once here and
        # never updated again for the rest of this process's lifetime -
        # deliberately separate from the live, mutable
        # `_persistent_depth_preference` above. This snapshot (not the
        # live value) is what seeds a brand-new conversation's local
        # preference (see the two seeding call sites below). Without this
        # split, one conversation's mid-process learning could leak into
        # a DIFFERENT, concurrently-open conversation started later in the
        # very same process run the moment a threshold-triggered merge
        # updated the live value - exactly the "do not create a global
        # mutable preference shared between simultaneous conversations"
        # hard constraint, and exactly what
        # `tests/test_adaptive_response_depth.py::test_e2e_5_preference_does_not_leak_across_conversations`
        # (a pre-existing Sprint 2 regression test, unmodified by this
        # sprint) already enforces. Cross-SESSION learning is unaffected:
        # the next time this process restarts, `DepthPreferenceStore.load()`
        # picks up everything merged and saved during this run.
        self._depth_preference_startup_bias = self._persistent_depth_preference.bias

        # Response Depth Policy sprint (Phase 4 - debug/inspection).
        # Existing dashboard collectors (`collect_routing_status`,
        # `collect_context_preview`, ...) each back a DEDICATED existing
        # page/endpoint in `luno/dashboard/server.py` - there is no
        # existing page this per-turn, ephemeral decision naturally
        # belongs on, and the brief explicitly forbids adding a new
        # dashboard page/UI feature for this sprint. Per the brief's own
        # fallback ("keep the data internal and testable"), the full
        # last-resolved `ResponsePolicy` (depth/score/reasons/explicit/
        # task_type) is kept here instead - bounded, in-memory, never
        # persisted, same cap/reset convention as `_response_depth_context`
        # above - so it stays inspectable (via `get_last_response_policy()`
        # below and the `log()` line in `_handle_utterance`) and testable
        # (Phase 5's integration tests read it directly) without any new
        # UI surface.
        self._last_response_policy: Dict[str, dict] = {}

        # Conversation_end Race Safety sprint - the smallest per-
        # conversation synchronization needed to close the race
        # documented in docs/change_impact/persistent_adaptive_response_depth.md
        # and docs/change_impact/conversation_ended_lifecycle_routing.md's
        # own "Known limitations": `_handle_utterance()` runs on its own
        # background "luno-planner-turn" thread (spawned by `on_event()`
        # below) and does a few synchronous, non-trivial-latency reads
        # (memory retrieval, etc.) BEFORE it reaches
        # `_update_depth_preference()` - if `conversation_ended` for that
        # SAME conversation is processed on a different thread (the
        # Event Bus pump/dispatcher thread, via `SessionManagerModule`'s
        # own inactivity-timeout timer) while that gap is still open,
        # the final merge in `_on_conversation_ended()` could run BEFORE
        # this turn's feedback was ever recorded into `_depth_preference` -
        # silently losing that one turn's contribution, and (worse)
        # leaving the entry the turn re-creates afterward orphaned in
        # the dict forever (never flushed to disk, since
        # `_on_conversation_ended()` already had its one chance to merge
        # it). ONE new lock/condition pair, purpose-built for this ONE
        # narrow concern (deliberately NOT reusing
        # `_persistent_depth_preference_lock` above, which guards a
        # different critical section) - not a second Event Bus, not
        # polling, not a global runtime-wide lock (it only ever guards
        # this small dict + set, held very briefly per operation).
        self._active_turn_lock = threading.Lock()
        self._active_turn_cv = threading.Condition(self._active_turn_lock)
        #: conversation_id -> count of turns for that conversation
        #: currently between "started" (`on_event()`, below) and
        #: "settled" (`_mark_turn_settled()`, called from
        #: `_handle_utterance()` right after the feedback-relevant
        #: portion of a turn completes - see that call site). Almost
        #: always 0 or 1; >1 only if a conversation genuinely has more
        #: than one utterance in flight at once (rapid consecutive
        #: speech/barge-in). NEVER persisted, NEVER holds conversation
        #: text - an int per conversation_id, nothing else.
        self._active_turn_counts: Dict[str, int] = {}
        #: conversation_ids currently inside `_on_conversation_ended()` -
        #: sole purpose is to make `on_event()` refuse to start a NEW
        #: turn for a conversation that is already in the middle of
        #: ending (Phase 4's "prevent new work from being accepted").
        #: Added and removed under `_active_turn_lock` so the
        #: check-and-increment in `on_event()` and the
        #: mark-ending-and-wait in `_on_conversation_ended()` can never
        #: interleave unsafely.
        self._ending_conversations: set = set()

    def _mark_turn_settled(self, conversation_id: Optional[str]) -> None:
        """Called from a `luno-planner-turn` thread once THIS turn's
        feedback-relevant processing (`_update_depth_preference()`) has
        run - never for the rest of the turn (LLM streaming/TTS), which
        `_on_conversation_ended()` has no reason to wait for. Safe to
        call even if the count is already 0 (defensive - never goes
        negative), which can legitimately happen after a
        `_wait_for_turn_to_settle()` timeout already force-cleared the
        entry (see that method)."""
        if not conversation_id:
            return
        with self._active_turn_cv:
            remaining = max(0, self._active_turn_counts.get(conversation_id, 0) - 1)
            if remaining:
                self._active_turn_counts[conversation_id] = remaining
            else:
                self._active_turn_counts.pop(conversation_id, None)
            self._active_turn_cv.notify_all()

    def _wait_for_turn_to_settle(self, conversation_id: Optional[str]) -> None:
        """Called from `_on_conversation_ended()`, BEFORE the final
        adaptive-preference merge reads `_depth_preference`. Marks
        `conversation_id` as ending (so `on_event()` stops accepting new
        turns for it) and blocks, bounded by `turn_settle_timeout_s`,
        until every turn already in flight for this conversation has
        called `_mark_turn_settled()`. A hung/crashed worker that never
        settles does not deadlock the runtime - the wait always returns
        by the deadline, the timeout is logged, and the stale count is
        force-cleared so a later `_mark_turn_settled()` call (or a
        duplicate `conversation_ended` for the same id) never blocks or
        goes negative. No-op for a falsy `conversation_id` - mirrors
        every other per-conversation dict in this class."""
        if not conversation_id:
            return
        deadline = time.monotonic() + self.turn_settle_timeout_s
        with self._active_turn_cv:
            self._ending_conversations.add(conversation_id)
            while self._active_turn_counts.get(conversation_id, 0) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    log(
                        f"conversation_ended (session={conversation_id}) - timed out after "
                        f"{self.turn_settle_timeout_s}s waiting for an in-flight turn to settle; "
                        "proceeding with final merge/cleanup using whatever state is already recorded.",
                        "planner_bridge",
                    )
                    self._active_turn_counts.pop(conversation_id, None)
                    break
                self._active_turn_cv.wait(timeout=remaining)

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus
        # World Model Sprint Bagian 3 - update straight from HA's own
        # state_changed WebSocket, no waiting on the Planner. Only
        # meaningful when a real HA backend is wired in (see
        # luno/adapters/home_assistant.py::HomeAssistantAdapter.
        # on_state_changed) - harmless no-op subscription otherwise,
        # since nothing ever publishes "device_state_changed" without
        # it.
        self.world_model.bind_event_bus(event_bus)
        event_bus.subscribe("device_state_changed", self.world_model.update_from_state_changed)

    def start(self) -> None:
        pass

    def stop(self) -> None:
        self.planner.shutdown(wait=False)

    def health(self) -> ModuleHealthStatus:
        return ModuleHealthStatus(healthy=True)

    def can_skip_action(self, tool_call: Any) -> Optional[bool]:
        """World Model Sprint Bagian 5 - read-only helper, NOT wired
        into `_tool_bridge_handler`/actual task execution (that would
        change the Planner's real behavior, which this sprint
        explicitly says not to do unless it's a small, obviously safe
        change - re-running a `turn_on` HA already considers a no-op is
        cheap, and NOT calling it risks masking a device that's
        silently gone unresponsive since the World Model was last
        updated, which the Reliability Sprint's whole point was to
        catch). Only meaningful for simple on/off-style actions with a
        clear expected state (`turn_on`/`turn_off`) - returns `True` if
        the World Model already shows that state, `False` if it shows
        the opposite, `None` if unknown/not applicable (entity not yet
        in the World Model, or an action with no single expected state
        like `run_script`).

        TODO: if/when this gets wired into the real flow, the entity_id
        used for the World Model lookup has to be resolved the SAME way
        `RealHomeAssistantHandler._resolve_entity_id()` already does
        (device name/alias -> entity_id via `luno.devices`) -
        `tool_call.target` alone (as parsed by
        `luno.planner.parser.IntentParser`) is a slug like
        "bedroom_light", not a real `entity_id` like
        "light.bedroom_light", so a naive lookup here would always miss.
        """
        action = getattr(tool_call, "action", None)
        expected = {"turn_on": "on", "turn_off": "off"}.get(action)
        if expected is None:
            return None
        entity_id = getattr(tool_call, "target", None)
        if not entity_id or not self.world_model.exists(entity_id):
            return None
        return self.world_model.get(entity_id) == expected

    # -- bridging: Planner wants a tool run -> publish ToolRequested, wait ----

    def _tool_bridge_handler(self, tool_call: Any) -> Dict[str, Any]:
        if self._event_bus is None:
            raise RuntimeError("planner tool bridge: not bound to an event bus")
        execution_id = generate_id("exec")

        # Subscribe to BOTH outcomes before publishing, exactly like
        # BehaviorTreeModule._generate_reply does for assistant_response/
        # llm_error - ToolManagerBridgeModule handles tool_requested
        # synchronously and may publish tool_failed almost immediately,
        # so listening for tool_finished first and only checking
        # tool_failed afterwards would miss it and report a bogus timeout.
        done = threading.Event()
        box: Dict[str, Event] = {}

        def _on_finished(e: Event) -> None:
            if e.get("execution_id") == execution_id:
                box["finished"] = e
                done.set()

        def _on_failed(e: Event) -> None:
            if e.get("execution_id") == execution_id:
                box["failed"] = e
                done.set()

        sub_ok = self._event_bus.subscribe("tool_finished", _on_finished)
        sub_err = self._event_bus.subscribe("tool_failed", _on_failed)
        try:
            self._event_bus.publish(Event(type="tool_requested", data={"execution_id": execution_id, "tool_call": tool_call}))
            got = done.wait(self.tool_timeout_s)
        finally:
            self._event_bus.unsubscribe(sub_ok)
            self._event_bus.unsubscribe(sub_err)

        if not got:
            # Sprint 57 (Contextual Home Assistant References) - a
            # timeout is a failure too ("a failed or ambiguous HA
            # command must NOT become a strong contextual target").
            self._invalidate_device_context_on_failure(tool_call)
            raise TimeoutError(f"tool '{getattr(tool_call, 'tool', '?')}' timed out waiting for Tool Manager")
        if "failed" in box:
            err = RuntimeError(box["failed"].get("error") or box["failed"].get("message") or "tool failed")
            # Reliability Sprint - "ToolResult diteruskan utuh": a raised
            # exception can only carry a string via `task.error` (see
            # `luno/planner/executor.py`'s `TaskExecutor._handle_failure`),
            # which would otherwise drop `data` (verification_attempts,
            # actual_state, expected_state, failure_reason, ...) on the
            # floor for a failed task. Attaching the full payload here lets
            # `TaskExecutor` preserve it as `task.result` even on failure,
            # additive-only (doesn't change either function's signature).
            err.tool_result = dict(box["failed"].data)  # type: ignore[attr-defined]
            # Sprint 57 - un-remember this device if the failed call's
            # target is what this conversation currently has remembered
            # (see `_invalidate_device_context_on_failure`'s own
            # docstring). Best-effort, never masks the real failure.
            self._invalidate_device_context_on_failure(tool_call)
            raise err
        return box["finished"].data

    # -- AI-assisted device intent (typo/paraphrase tolerance beyond regex) --

    _DEVICE_INTENT_RE = re.compile(r"^\s*ACTION=(turn_on|turn_off)\s+DEVICE=(.+?)\s*$", re.IGNORECASE)

    def _known_device_names(self) -> List[str]:
        """Every light/switch name+alias currently configured - see
        `luno/devices.py`'s own docstring for the JSON file formats.
        Scripts are deliberately excluded: IntentParser has no "run
        script" phrasing to fall back to today (only turn_on/turn_off),
        so classifying a script request here would have nowhere correct
        to route it - out of scope for this first pass."""
        from luno import devices
        names: List[str] = []
        for key, cfg in devices.LIGHTS.items():
            names.append(key)
            names.extend(cfg.get("aliases") or [])
        names.extend(devices.SWITCHES.keys())
        return names

    def _classify_device_intent(self, text: str) -> Optional[str]:
        """Last-resort fallback for when `IntentParser`'s fast regex
        parser can't classify `text` as anything at all (produces only
        the synthetic "unknown.unknown" task - see `luno/planner/
        parser.py`). Regex parsing only ever tolerates typos/phrasings
        someone explicitly anticipated (e.g. the "trun" fix); this asks
        the LLM instead - genuinely general, catches ANY typo or
        paraphrase of a real device command, at the cost of one extra
        LLM round-trip (only paid on the already-slow "parser gave up"
        path, never on a normal correctly-phrased command).

        Deliberately does NOT use OpenRouter's streaming/event-bus path
        (`NeedLLMResponse` -> ... -> `AssistantResponse`) - that's the
        FINAL spoken reply's path, and this classification prompt's raw
        "ACTION=... DEVICE=..." output must never itself be spoken.
        Instead calls `self.device_intent_client.chat_completion()`
        directly and synchronously - safe here because
        `_handle_utterance` already runs on its own per-turn background
        thread (see `on_event` below), never the Event Bus pump thread.

        Returns a canonical "turn on/off <device>" phrase (guaranteed to
        re-parse cleanly through IntentParser) on a confident match, or
        `None` for "not a device command" / classifier unavailable /
        anything at all going wrong - always fails closed, silently, so
        a classification hiccup degrades to today's plain-chat behavior
        rather than ever blocking or crashing a turn."""
        if self.device_intent_client is None:
            return None
        device_names = self._known_device_names()
        if not device_names:
            return None  # nothing configured to control - don't bother asking

        device_list = "\n".join(f"- {name}" for name in dict.fromkeys(device_names))  # de-duplicated, order-preserving
        classifier_prompt = (
            "You classify whether a message is a smart-home device command. "
            "Known devices (the user may refer to one by typo, nickname, or a different phrasing):\n"
            f"{device_list}\n\n"
            "If the message clearly wants one of these devices turned on or off, reply with "
            "EXACTLY one line, nothing else: ACTION=turn_on DEVICE=<exact device name from the list above> "
            "or ACTION=turn_off DEVICE=<exact device name from the list above>. "
            "If it's not a device command (casual conversation, a question, anything else), "
            "reply with EXACTLY: NONE"
        )
        try:
            response = self.device_intent_client.chat_completion(
                model=self.device_intent_model, messages=[{"role": "user", "content": text}],
                system_prompt=classifier_prompt, temperature=0.0, max_tokens=32,
            )
        except Exception as ex:
            log(f"device intent classifier call failed (falling back to plain chat): {ex}", "planner_bridge")
            return None

        raw = (getattr(response, "text", None) or "").strip()
        match = self._DEVICE_INTENT_RE.match(raw)
        if not match:
            return None  # includes the "NONE" case and any malformed/unexpected output

        action, spoken_device = match.group(1).lower(), match.group(2).strip()
        wanted = spoken_device.strip().lower()
        matched_name = next((name for name in device_names if name.strip().lower() == wanted), None)
        if matched_name is None:
            # Model didn't stick to a name actually in the list - fail
            # closed rather than guess at a device that isn't configured.
            log(f"device intent classifier named an unconfigured device {spoken_device!r} (ignored)", "planner_bridge")
            return None

        verb = "on" if action == "turn_on" else "off"
        return f"turn {verb} {matched_name}"

    # -- vision intent (camera questions, e.g. "ada apa di kamera") ----------

    # Rule-based, not LLM-based - same "never guess wrong, fail closed"
    # spirit as IntentParser itself (see that module's own docstring).
    # Classification itself lives in `luno/vision_intent.py` (same
    # architecture as `luno/environment_intent.py`'s
    # `classify_environmental_cue()` - see that module's own docstring
    # for why keyword/regex matching, not an LLM call, is the right
    # trade-off here) - this class only owns the "is the feature even
    # enabled, and what do we DO with a match" parts, which are specific
    # to this bridge, not to classification.
    #
    # `luno/vision.py` already ships a real, working `ask_vision()` - but
    # THIS architecture never does live LLM function-calling at all
    # (`NeedLLMResponse` carries no `tools` list; web search/memory/
    # environmental-intent all work by classifying the utterance FIRST
    # and injecting pre-fetched context into the prompt, see
    # `decision.search_context`/`explicit_memory_block` below). This
    # gives vision questions that exact same treatment instead of trying
    # to bolt on function-calling.

    def _classify_vision_intent(self, text: str) -> Optional[str]:
        """Returns `text` unchanged if it looks like a "look through the
        camera" question, else `None`. Also `None` (never even runs the
        classifier) when `CAMERA_VISION_ENABLED` is off - same master
        switch `luno.vision.is_configured()` already gates everything
        else in that module behind, so this never tries to open a camera
        the user never opted into."""
        import luno.vision as vision_module  # local import: optional hardware dependency, mirrors luno/vision.py's own pattern
        if not vision_module.is_configured():
            return None
        from luno.vision_intent import classify_vision_intent
        intent = classify_vision_intent(text)
        return intent.question if intent.is_vision_request else None

    def _handle_vision_intent(self, text: str, request_id: str) -> Optional[str]:
        """Classify `text`, and if it matches, actually call
        `luno.vision.ask_vision()` (camera frame + YOLO hint + Gemini 2.0
        Flash) synchronously right here - `_handle_utterance` already
        runs on its own per-turn background thread (see `on_event`), so
        blocking it for the vision round-trip is the same accepted
        trade-off `self.memory_retriever.retrieve_memories()` above and
        `DecisionEngine.decide()`'s own web search already make; nothing
        else is waiting on this thread.

        Returns a note to append to the LLM's system context describing
        what the camera actually saw (or that the check failed and why),
        or `None` if this utterance isn't a vision question at all - never
        raises, a camera/Gemini failure just means no camera note for
        this turn rather than a broken turn."""
        matched = self._classify_vision_intent(text)
        if matched is None:
            return None
        try:
            import luno.vision as vision_module
            result = vision_module.ask_vision(matched)
        except Exception as ex:
            log(f"request_id={request_id} - ask_vision() raised (skipped): {ex}", "planner_bridge")
            return None
        if "description" in result:
            log(f"request_id={request_id} - vision intent matched {text!r} -> {result['description'][:80]!r}", "planner_bridge")
            return f"[Camera] {result['description']}"
        if "error" in result:
            log(f"request_id={request_id} - vision intent matched {text!r} but ask_vision() failed: {result['error']}", "planner_bridge")
            return f"[Camera] Couldn't check the camera just now: {result['error']}"
        return None

    def _classify_screen_intent(self, text: str) -> Optional[str]:
        """Same shape as `_classify_vision_intent()` above, but for the
        DESKTOP screenshot feature (`luno.screen_vision`) - independent
        master switch (`SCREEN_VISION_ENABLED`, checked via
        `luno.screen_vision.is_configured()`), independent classifier
        vocabulary (`luno.screen_intent`, "layar"/"screen" words, not
        "kamera"), so a user can opt into one without the other."""
        import luno.screen_vision as screen_vision_module  # local import: optional OS-level dependency (Pillow ImageGrab), mirrors _classify_vision_intent's own pattern
        if not screen_vision_module.is_configured():
            return None
        from luno.screen_intent import classify_screen_intent
        intent = classify_screen_intent(text)
        return intent.question if intent.is_screen_request else None

    def _handle_screen_intent(self, text: str, request_id: str) -> Optional[str]:
        """Classify `text`, and if it matches, actually call
        `luno.screen_vision.ask_screen()` (screenshot + vision provider)
        synchronously right here - same "blocking the per-turn thread is
        an accepted trade-off" reasoning as `_handle_vision_intent()`
        above. Returns a note to append to the LLM's system context
        describing what the screenshot actually showed (or that the
        check failed and why), or `None` if this utterance isn't a
        screen-diagnosis request at all - never raises."""
        matched = self._classify_screen_intent(text)
        if matched is None:
            return None
        try:
            import luno.screen_vision as screen_vision_module
            result = screen_vision_module.ask_screen(matched)
        except Exception as ex:
            log(f"request_id={request_id} - ask_screen() raised (skipped): {ex}", "planner_bridge")
            return None
        if "description" in result:
            log(f"request_id={request_id} - screen intent matched {text!r} -> {result['description'][:80]!r}", "planner_bridge")
            return f"[Screen] {result['description']}"
        if "error" in result:
            log(f"request_id={request_id} - screen intent matched {text!r} but ask_screen() failed: {result['error']}", "planner_bridge")
            return f"[Screen] Couldn't check the screen just now: {result['error']}"
        return None

    # -- browser: research / monitoring / computer-use ------------------------

    def _get_browser_provider(self) -> Optional[Any]:
        """Returns a live `BrowserProvider` only if `BROWSER_ENABLED` is
        on - callers degrade gracefully otherwise (research falls back
        to Tavily-snippet-only, monitoring skips visual dashboard
        inspection, computer-use reports unavailable) rather than
        raising. Never imports Playwright unless browsing is actually
        enabled - mirrors `_classify_vision_intent`'s own
        `CAMERA_VISION_ENABLED` master-switch gate."""
        try:
            from luno.browser.config import BrowserConfig
            if not BrowserConfig.from_env().enabled:
                return None
            from luno.browser.provider import get_browser_provider
            return get_browser_provider()
        except Exception:
            return None

    def _handle_browser_research_intent(self, text: str, request_id: str) -> Optional[str]:
        """Classify + (if matched) open a search in Vinn's REAL system
        browser (chrome.exe if registered in config/apps.json, else the
        OS default) - same mechanism as `_handle_image_search_intent`,
        and deliberately so.

        Two reported gaps this addresses together:

        1. "campur" (mixed) responses - asking "cek harga RTX3060" used
           to get an LLM-guessed answer at the same time text-research
           results were being read/synthesized, so it looked like Luno
           was making the number up rather than actually checking. Per
           Vinn's explicit choice ("Cukup bukain aja, gak usah jawab" -
           just open it, no need to answer), the LLM's ONLY job here is
           picking the query (done by the classifier below); the note
           below explicitly tells the LLM not to answer/guess `query`
           from its own knowledge at all - just acknowledge it's open.

        2. "kok chromium bukan chrome.exe" - this used to route through
           `luno.browser.provider.get_visible_browser_provider()`
           (Playwright's OWN bundled Chromium build, not Vinn's real,
           logged-in Chrome, and requiring `playwright install
           chromium`). Vinn confirmed (AskUserQuestion) he wants the
           pre-Playwright behavior back: `luno.desktop_control.
           open_url()` - the exact same allowlisted, subprocess-based
           mechanism `open_app()`/the old `luno/main.py::search_browser`
           already used reliably. Since this handler never reads the
           page back (see point 1), there's no automation/DOM need that
           would justify Playwright here at all - so this is NOT gated
           on `BROWSER_ENABLED` any more (that flag now only governs
           the genuinely Playwright-dependent features: computer-use
           and monitoring's visual dashboard inspection)."""
        from luno.browser.intent import classify_research_intent
        query = classify_research_intent(text)
        if not query:
            return None
        from urllib.parse import quote_plus
        from luno import desktop_control
        ok, message = desktop_control.open_url(f"https://www.google.com/search?q={quote_plus(query)}")
        if not ok:
            log(f"request_id={request_id} - browser research intent matched {text!r} but opening the browser failed: {message}", "planner_bridge")
            return (
                f"[Browser] Tried to open a search for \"{query}\" but it failed: {message}. Tell the user "
                f"honestly that it didn't open - do not answer \"{query}\" from your own knowledge instead."
            )
        log(f"request_id={request_id} - browser research intent matched {text!r} -> opened system browser search for {query!r}", "planner_bridge")
        if self._event_bus is not None:
            from luno.core.events import BrowserResearchCompleted
            self._event_bus.publish(BrowserResearchCompleted(data={
                "request_id": request_id, "query": query, "source_count": 0,
            }))
        return (
            f"[Browser] I've opened a browser search for \"{query}\" - it's on screen now, not "
            f"summarized here. IMPORTANT: do not answer or guess anything about \"{query}\" yourself, and do "
            f"not state any facts about it from your own knowledge - just briefly tell the user you've pulled "
            f"it up on screen for them to look at themselves."
        )

    def _handle_image_search_intent(self, text: str, request_id: str) -> Optional[str]:
        """Classify + (if matched) actually open an image search in
        Vinn's REAL system browser, so he sees the results himself on
        screen - see `luno/browser/intent.py::classify_image_search_
        intent`'s own docstring for why this is deliberately a
        different mechanism from `_handle_browser_research_intent`
        (that one reads/summarizes; this one is about SHOWING). Uses
        `luno.desktop_control.open_url()` - real chrome.exe (or OS
        default browser), not Playwright's bundled Chromium - same
        "no automation needed, so no Playwright dependency" reasoning
        as the research handler above; not gated on `BROWSER_ENABLED`
        for the same reason (that flag is reserved for the genuinely
        Playwright-dependent features)."""
        from luno.browser.intent import classify_image_search_intent
        query = classify_image_search_intent(text)
        if not query:
            return None
        from urllib.parse import quote_plus
        from luno import desktop_control
        ok, message = desktop_control.open_url(f"https://www.google.com/search?tbm=isch&q={quote_plus(query)}")
        if not ok:
            log(f"request_id={request_id} - image search intent matched {text!r} but opening the browser failed: {message}", "planner_bridge")
            return f"[Image search] Tried to open an image search for '{query}' but it failed: {message}"
        log(f"request_id={request_id} - image search intent matched {text!r} -> opened system browser for {query!r}", "planner_bridge")
        return f"[Image search] I've opened a browser window with image search results for '{query}' - it's on screen now, take a look."

    def _handle_monitoring_intent(self, text: str, request_id: str) -> Optional[str]:
        """Classify + (if matched) actually check every configured
        `MonitorTarget` plus local system metrics - see
        `luno/browser/monitoring.py::MonitoringService`. Debounced
        alert events (`server_cpu_high`, `monitoring_target_unreachable`,
        ...) are published as a side effect of `check_all()` itself,
        independent of whatever note text ends up here."""
        from luno.browser.intent import classify_monitoring_intent
        if not classify_monitoring_intent(text):
            return None
        from luno.browser.config import load_monitor_targets
        from luno.browser.monitoring import MonitoringService
        targets = load_monitor_targets()
        service = MonitoringService(event_bus=self._event_bus, browser_provider=self._get_browser_provider())
        statuses = service.check_all(targets)
        metrics = service.local_metrics()
        note = MonitoringService.format_note(statuses, metrics)
        log(f"request_id={request_id} - monitoring intent matched {text!r} ({len(targets)} target(s))", "planner_bridge")
        return note

    def _handle_computer_use_intent(self, text: str, request_id: str, conversation_id: Optional[str]) -> Optional[str]:
        """Classify + (if matched) run the bounded observe/reason/act/
        verify loop - see `luno/browser/computer_use.py::
        ComputerUseAgent`. Returns a note describing either the
        completed outcome, a `STUCK`/step-limit stop, or a permission
        prompt/refusal - `ComputerUseAgent.run()` itself already halts
        rather than acting past a Level 2/3 decision (see that class's
        own docstring)."""
        from luno.browser.intent import classify_computer_use_intent
        task = classify_computer_use_intent(text)
        if not task:
            return None
        provider = self._get_browser_provider()
        if provider is None:
            return "[Computer-use] Browser/computer-use isn't enabled right now (BROWSER_ENABLED is off)."
        from luno.browser.computer_use import ComputerUseAgent
        from luno.browser.config import BrowserConfig
        key = conversation_id or self._ENV_CONFIRMATION_KEY
        agent = ComputerUseAgent(provider, self.browser_permissions, max_steps=BrowserConfig.from_env().max_steps)
        result = agent.run(task, conversation_key=key)
        log(f"request_id={request_id} - computer-use intent matched {text!r} -> completed={result.completed} ({len(result.steps)} step(s))", "planner_bridge")
        return f"[Computer-use] {result.final_note}"

    def _handle_browser_confirmation(self, text: str, request_id: str, conversation_id: Optional[str]) -> Optional[str]:
        """Two-turn confirm-first release for Level 2 (SENSITIVE) browser
        actions - same shape as `_handle_environmental_intent()`: a
        pending action was recorded (by `RealBrowserHandler`/
        `ComputerUseAgent`, via `self.browser_permissions`) on an
        EARLIER turn; this turn's reply either releases it (affirmative -
        the action is now actually performed, through the exact same
        `_tool_bridge_handler` -> `ToolRequested` -> verified-execution
        path any explicit command uses) or drops it (negative/unrelated -
        `text` then falls through to be handled as a normal fresh turn).
        Returns `None` whenever there's nothing pending, or the reply
        wasn't a yes/no answer at all."""
        key = conversation_id or self._ENV_CONFIRMATION_KEY
        if not self.browser_permissions.has_pending(key):
            return None
        reply = classify_confirmation_reply(text)
        # Always resolve (pop) regardless of what `reply` was - same
        # "consumed either way, one-shot" rule `_handle_environmental_
        # intent()` follows for its own pending confirmation, so a
        # stale pending action can never be released by a much-later
        # unrelated "yes".
        pending = self.browser_permissions.resolve_confirmation(key, reply is True)
        if reply is None:
            return None  # neither yes nor no - already popped above, treat text as a fresh turn
        if pending is None:
            log(f"request_id={request_id} - browser confirmation declined", "planner_bridge")
            return "The user just declined a pending browser action. Acknowledge briefly and naturally - do not do anything."
        try:
            from luno.planner.models import ToolCall as _PlannerToolCall
            tool_call = _PlannerToolCall(
                tool="browser", action=pending.action, target=pending.target,
                params={**pending.params, "confirmed": True},
            )
            result = self._tool_bridge_handler(tool_call)
            message = result.get("message") if isinstance(result, dict) else None
            log(f"request_id={request_id} - browser confirmation released, action={pending.action!r} -> {message!r}", "planner_bridge")
            return (
                f'The user just confirmed - you already performed "{pending.action}" and this is the '
                f'VERIFIED result: {message or "done"}. Report this fact naturally and briefly, do not '
                f"ask again, it is already done."
            )
        except Exception as ex:
            log(f"request_id={request_id} - browser confirmation release failed: {ex}", "planner_bridge")
            return f'The user just confirmed, but performing "{pending.action}" failed: {ex}. Report this honestly.'

    # -- environmental intent inference (implicit cues, e.g. "it's hot") -----

    #: How long a pending "mau aku nyalain AC?" confirmation stays valid -
    #: the user's VERY NEXT utterance in the same conversation is the
    #: only chance to confirm/decline (see `_handle_environmental_intent`);
    #: this is just a safety net for the case where that next utterance
    #: never arrives at all (session idles out, wake session ends, etc.)
    #: so a stale entry can't sit in `_pending_env_confirmations` forever.
    _ENV_CONFIRMATION_TTL_S = 120.0
    #: Sentinel key for callers that never set a real `conversation_id`
    #: (e.g. a single-shot text console) - keeps the feature working
    #: without requiring a real session concept, at the cost of sharing
    #: one pending confirmation across every conversation_id-less caller,
    #: which is exactly the "only one real conversation happening"
    #: scenario that situation implies anyway.
    _ENV_CONFIRMATION_KEY = "_default_"

    def _handle_environmental_intent(self, text: str, request_id: str, conversation_id: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """Implicit/environmental smart-home intent inference ("hawanya
        panas nih" -> propose turning the AC on) - see
        `luno/environment_intent.py`'s own module docstring for the
        cue classifier and why it never acts without asking first.

        Returns `(effective_text_override, system_note)`, exactly one
        of which is ever non-None (mirrors how the caller already
        threads `explicit_memory_note` through):

          - `(None, None)`               - nothing to do, handle `text`
                                            as an ordinary turn.
          - `(None, note)`                - either a NEW cue was just
                                            detected (note asks the
                                            trigger's own confirmation
                                            question) or a PENDING
                                            confirmation was just
                                            declined (note acknowledges
                                            that) - either way, nothing
                                            gets planned/executed this
                                            turn.
          - `(canonical_command, None)`   - a PENDING confirmation was
                                            just accepted - the caller
                                            feeds this into
                                            `effective_text` so it goes
                                            through the EXACT SAME
                                            `IntentParser` -> `Planner`
                                            -> verified-execution
                                            pipeline as any explicit
                                            command, with the exact same
                                            "never claim success unless
                                            HA actually confirms it"
                                            guarantee.

        Two-turn state machine, keyed on `conversation_id`:

          Turn N   - no pending confirmation for this conversation yet.
                     `classify_environmental_cue(text)` matches a cue
                     AND a trigger is actually configured for it -> a
                     pending confirmation is recorded (with a TTL) and
                     this returns the "ask" note. `effective_text` is
                     deliberately left untouched by the caller in this
                     case, so `Planner.create_plan()` still runs on the
                     user's own original words, finds nothing
                     home-assistant-shaped, and harmlessly does nothing
                     - the exact same "unknown task, ignored" pattern
                     `_handle_explicit_memory_command`'s callers already
                     rely on.
          Turn N+1 - a pending confirmation exists (and hasn't expired).
                     It is POPPED immediately (one-shot: consumed
                     either way, so it can never double-fire):
                       - affirmative reply -> returns the stored
                         canonical command.
                       - negative reply -> returns a decline-
                         acknowledgment note.
                       - neither (an unrelated new utterance) -> the
                         pending confirmation is simply gone now, and
                         `text` falls through to be classified FRESH
                         below, exactly like Turn N.

        Always fails closed: any error loading triggers/classifying
        just returns `(None, None)` - a hiccup here degrades to "the
        implicit remark was heard as plain conversation," never a
        crash, never an unconfirmed action."""
        key = conversation_id or self._ENV_CONFIRMATION_KEY

        try:
            pending = self._pending_env_confirmations.get(key)
            if pending is not None and time.time() > pending["expires_at"]:
                pending = None
                self._pending_env_confirmations.pop(key, None)

            if pending is not None:
                self._pending_env_confirmations.pop(key, None)  # one-shot
                reply = classify_confirmation_reply(text)
                if reply is True:
                    log(
                        f"request_id={request_id} - environmental intent: user confirmed "
                        f"'{pending['cue']}' -> {pending['command']!r}", "planner_bridge",
                    )
                    return pending["command"], None
                if reply is False:
                    log(f"request_id={request_id} - environmental intent: user declined '{pending['cue']}'", "planner_bridge")
                    decline_ack = pending.get("decline_ack") or "Okay, never mind."
                    return None, (
                        f'The user said: "{text}", declining your offer to {pending["cue"]} (you had asked: '
                        f'"{pending["cue_description"]}"). Acknowledge briefly and naturally (something '
                        f'like "{decline_ack}") - do not do anything.'
                    )
                # neither affirmative nor negative - already popped above,
                # so it can never linger to catch a much-later unrelated
                # "iya" - fall through and classify `text` fresh below.

            cue = classify_environmental_cue(text)
            if cue is None:
                return None, None
            trigger = ENV_TRIGGERS.get(cue)
            if trigger is None:
                # Cue recognized, but nothing configured for it (see
                # config/environment_triggers.json) - stay silent rather
                # than asking about a device that doesn't exist.
                return None, None

            command = build_confirmation_command(trigger)
            self._pending_env_confirmations[key] = {
                "cue": cue,
                "command": command,
                "decline_ack": trigger.decline_ack,
                "cue_description": trigger.ask,
                "expires_at": time.time() + self._ENV_CONFIRMATION_TTL_S,
            }
            log(
                f"request_id={request_id} - environmental intent: cue={cue!r} detected from {text!r} "
                f"- asking to confirm ({command!r})", "planner_bridge",
            )
            return None, (
                f'The user just said something implying "{cue}" ("{text}") - this is NOT an explicit '
                f'command. Ask them, naturally and briefly, exactly this question (translate/adapt the '
                f'phrasing/tone to match your own but keep the meaning): "{trigger.ask}" - do NOT claim '
                f'you already did it, you are only ASKING.'
            )
        except Exception as ex:
            log(f"request_id={request_id} - environmental intent handling raised (skipped): {ex}", "planner_bridge")
            return None, None

    # -- short-term device-context memory ("sekarang matikan") ---------------

    #: home_assistant actions IntentParser can produce that need a target
    #: at all - "run_script" is deliberately excluded (it already
    #: resolves against `luno.devices.SCRIPTS` by a completely different
    #: mechanism - see `_RUN_SCRIPT_RE` in parser.py - and re-using a
    #: light/switch target for it would be actively wrong).
    #:
    #: This is the FILL set - which actions a missing/filler target may
    #: be REWRITTEN INTO ("sekarang matikan" -> "turn off <remembered>").
    #: Deliberately narrow and unchanged since before Sprint 57: an
    #: on/off rewrite is always safe and unambiguous to phrase as plain
    #: text that re-parses identically. See `_CONTEXT_REMEMBER_ACTIONS`
    #: immediately below for the (broader) set of actions that REFRESH
    #: the memory in the first place - a real, separate concept Sprint 57
    #: split out (a "Setel RGB komputer ke biru." turn should still prime
    #: the memory for a LATER "Matikan.", even though "set to blue"
    #: itself is not something a bare "Matikan." could ever be rewritten
    #: into).
    _CONTEXT_FILLABLE_ACTIONS = {"turn_on": "on", "turn_off": "off"}

    #: Sprint 57 (Contextual Home Assistant References) - every
    #: home_assistant action IntentParser can produce that names a real
    #: target device, broadened beyond `_CONTEXT_FILLABLE_ACTIONS` (the
    #: original set only covered on/off) so a turn like "Setel RGB
    #: komputer ke biru." (set_color) or "Redupkan lampu kamar."
    #: (set_brightness) also refreshes this conversation's "what device
    #: are we talking about" memory for a LATER "Matikan." to read - the
    #: sprint's own explicit second worked example. `run_script` is
    #: still excluded (same reasoning as `_CONTEXT_FILLABLE_ACTIONS`'s
    #: own comment - scripts aren't even in `_known_device_names()`, so
    #: `_is_known_home_assistant_device()` already returns False for one
    #: and this set change alone would be a no-op for it anyway).
    _CONTEXT_REMEMBER_ACTIONS = frozenset({"turn_on", "turn_off", "set_color", "set_brightness", "set_value"})

    #: Sprint 57 - bounded FRESHNESS window, in turns (see
    #: `_device_context_turn_seq`'s own docstring): a remembered device
    #: older than this many turns is treated as stale and is never used
    #: to fill a missing target, even though it is still sitting in
    #: `_last_device_target` (harmless - either a later real mention
    #: overwrites it, or the conversation itself eventually ends and
    #: `_on_conversation_ended` clears it). Deliberately small - this is
    #: a "did we JUST talk about this" signal, not a long-term memory;
    #: `_ACTIVE_TOPIC_MAX_AGE_TURNS` in `luno/memory_context.py` (a
    #: completely separate subsystem - see this sprint's own
    #: reconnaissance note on Tool Manager/Planner independence) uses
    #: the same small-bounded-turn-count SHAPE of freshness policy, not
    #: a shared value - reused as a design PATTERN, not as literal
    #: coupled state.
    _CONTEXT_MAX_TURN_AGE = 6

    #: Pure grammatical filler `_TURN_ON_RE`/`_TURN_OFF_RE`'s "capture
    #: everything after the verb" shape can pick up as if it were a
    #: device name ("matiin aja" -> target="aja", "nyalakan lagi" ->
    #: target="lagi") - deliberately NOT the same set as `luno.devices.
    #: _LIGHT_NAME_FILLERS` (that one also strips generic words like
    #: "lampu"/"light"/"rgb" while hunting for a specific name WITHIN a
    #: longer phrase; this one decides whether an ENTIRE captured target
    #: is nothing but filler, so it must NOT include a word that could
    #: itself be part of a genuine, if unregistered, device name -
    #: "lampu dapur" must still fail honestly as "device not found",
    #: never get silently swapped for a different remembered device).
    _CONTEXT_FILLER_WORDS = {
        "aja", "lagi", "dong", "deh", "dulu", "nya", "ya", "yah", "nih",
        "please", "now", "again", "it", "that", "itu", "ini",
        # Sprint 57 (Contextual Home Assistant References) - "yang" and
        # "tadi" added so purely-referential phrases ("yang itu" = "the
        # aforementioned one", "yang tadi" = "the one from before") are
        # recognized as "named no real device" (eligible for context
        # fill) rather than as an unrecognized-looking device name.
        # Neither word appears in any real configured device name in
        # this checkout, and adding them only ever WIDENS what counts
        # as "no real target" - it can never cause a genuine device
        # name to be misclassified as filler.
        "yang", "tadi",
    }

    def _is_filler_only_target(self, slugified_target: str) -> bool:
        words = [w for w in slugified_target.split("_") if w]
        return bool(words) and all(w in self._CONTEXT_FILLER_WORDS for w in words)

    def _is_known_home_assistant_device(self, slugified_target: str) -> bool:
        """Guards the "remember" step below against filler words
        `_TURN_ON_RE`/`_TURN_OFF_RE`'s own "capture everything after the
        verb" shape can pick up as if they were a device name (e.g.
        "nyalakan lagi" - "again" - slugifies to a target of "lagi",
        which isn't a real device but IS a non-empty string). Only a
        target that resolves to an ACTUALLY configured light/switch
        keeps this conversation's short-term device memory from being
        silently poisoned with nonsense for the next turn - reuses
        `_known_device_names()` (already gathered for
        `_classify_device_intent` above) and the SAME `_slugify()`
        IntentParser itself used to produce `slugified_target` in the
        first place, so the comparison is guaranteed consistent. Fails
        closed (an import/config problem is treated as "not known") -
        same convention as `parser.py`'s own `_is_known_script`."""
        try:
            from luno.planner.parser import _slugify as _parser_slugify
            return any(_parser_slugify(name) == slugified_target for name in self._known_device_names())
        except Exception:
            return False

    #: Sprint 57 (Contextual Home Assistant References) - HA domains a
    #: remembered device may safely be filled into a turn_on/turn_off
    #: FILL rewrite for. Mirrors Home Assistant's own generic
    #: `homeassistant.turn_on`/`turn_off` services, which genuinely
    #: support all of these domains - NOT a guess. `"script"` is
    #: deliberately absent: a script is a one-shot action, not a
    #: stateful thing with a stable "on"/"off" a later "Matikan." should
    #: ever target (and, structurally, a script can never even reach
    #: this check in the first place - `_known_device_names()` excludes
    #: scripts entirely, so `_is_known_home_assistant_device()` already
    #: returns False for one). This checkout's real registry today only
    #: ever populates `light`/`switch` (see `config/lights.config.json`/
    #: `switches.config.json`) - `fan`/`climate`/`media_player` are
    #: included for correctness/forward-compatibility, not because a
    #: real configured example of either exists in this checkout (tests
    #: exercise them directly against an engineered fixture, the same
    #: "no natural example exists yet, prove the gate anyway" precedent
    #: Sprint 52's own `test_T` already established).
    _CONTEXT_FILL_COMPATIBLE_DOMAINS = frozenset({"light", "switch", "fan", "climate", "media_player"})

    def _device_context_entity_info(self, slugified_target: str) -> Tuple[Optional[str], Optional[str]]:
        """Sprint 57 - resolves an already-known-good slug (already
        passed through `_is_known_home_assistant_device()`) to its real
        `entity_id` and HA domain (the `light`/`switch`/... prefix), so
        a remembered device can be checked for domain compatibility
        (`_CONTEXT_FILL_COMPATIBLE_DOMAINS`) before being used to fill a
        missing target, and so a failed execution can be matched back to
        the memory it should invalidate (see `_invalidate_device_
        context_on_failure()`). Mirrors `_is_known_home_assistant_
        device()`'s own lookup shape exactly (same registries, same
        `_slugify()` import) - not a new resolution mechanism, just
        returns more than a boolean. Fails closed: `(None, None)` for
        anything not found or on any import/config problem, same
        convention as `_is_known_home_assistant_device()`."""
        try:
            from luno import devices
            from luno.planner.parser import _slugify as _parser_slugify

            def _domain_of(entity_id: Optional[str], fallback: str) -> str:
                if entity_id and "." in entity_id:
                    return entity_id.split(".", 1)[0]
                return fallback

            for name, cfg in devices.LIGHTS.items():
                names_to_check = [name, *(cfg.get("aliases") or [])]
                if any(_parser_slugify(n) == slugified_target for n in names_to_check):
                    entity_id = cfg.get("entity_id")
                    return entity_id, _domain_of(entity_id, "light")
            for name, entity_id in devices.SWITCHES.items():
                if _parser_slugify(name) == slugified_target:
                    return entity_id, _domain_of(entity_id, "switch")
        except Exception:
            pass
        return None, None

    def _remember_device_target(self, key: str, tool: str, slugified_target: str, turn_seq: int) -> None:
        """Sprint 57 - the single write path for a CONFIRMED-good
        `home_assistant` target (already passed `_is_known_home_
        assistant_device()`), used by both `_apply_device_context()`'s
        own REMEMBER step and (indirectly, by never being called, see
        that method's own docstring) left alone by the invalidation
        path. Pulled into its own method only so the entity_id/domain
        lookup isn't duplicated at the one call site that needs it."""
        memory = self._last_device_target.setdefault(key, {})
        entity_id, domain = self._device_context_entity_info(slugified_target)
        memory[tool] = {
            "target": slugified_target, "turn_seq": turn_seq,
            "entity_id": entity_id, "domain": domain,
        }

    def _invalidate_device_context_on_failure(self, tool_call: Any) -> None:
        """Sprint 57 (Contextual Home Assistant References) - "a failed
        or ambiguous HA command must NOT become a strong contextual
        target" (the sprint's own explicit example: "Nyalain lampu X."
        fails to resolve/execute -> a LATER "Matikan." must not magically
        target lampu X). `_apply_device_context()`'s own REMEMBER step
        runs at PARSE time, before this tool call's real HA execution
        result is known - a device whose name was merely RECOGNIZED
        (matched a real, configured device by exact name) still gets
        remembered optimistically. This is the one place that later
        corrects that optimism once the real outcome is known: called
        from `_tool_bridge_handler()` only on the FAILURE path - if the
        tool call that just failed was a `home_assistant` call for the
        SAME target this conversation currently has remembered,
        un-remember it, so a LATER context-fill never targets a device
        whose most recent command just failed.

        Best-effort, matching this class's own existing single-flight-
        per-turn discipline (see `_wait_for_turn_to_settle()`'s own
        docstring) - reads `self._tool_bridge_local.conversation_id`, a
        `threading.local()` slot set once per spawned `luno-planner-
        turn` thread at the top of `_handle_utterance()` (see that
        attribute's own docstring in `__init__`), so two conversations
        processed concurrently on their own threads can never cross-
        contaminate each other's device memory here. A missing/unset
        thread-local (a caller that never went through `_handle_
        utterance` - e.g. a direct unit test of `_tool_bridge_handler`
        alone) is treated as "no conversation to invalidate for", a
        safe no-op. Never raises: a memory-hygiene bug here must never
        be able to mask the real tool failure `_tool_bridge_handler` is
        about to raise."""
        try:
            if getattr(tool_call, "tool", None) != "home_assistant":
                return
            target = getattr(tool_call, "target", None)
            if not target:
                return
            conversation_id = getattr(self._tool_bridge_local, "conversation_id", None)
            key = conversation_id or self._ENV_CONFIRMATION_KEY
            memory = self._last_device_target.get(key)
            if not memory:
                return
            remembered = memory.get("home_assistant")
            if isinstance(remembered, dict) and remembered.get("target") == target:
                memory.pop("home_assistant", None)
        except Exception:
            pass

    def _apply_device_context(self, text: str, conversation_id: Optional[str]) -> str:
        """Short-term device-context resolution: "aktifkan lampu kamar"
        (turn on the bedroom light) followed, on a LATER turn, by
        "sekarang matikan" (now turn it off) - with NO device named at
        all - is understood as "matikan lampu kamar" (turn off the
        bedroom light), the same device just talked about, rather than
        failing with `RealHomeAssistantHandler`'s honest-but-unhelpful
        "requires a target" validation error every single time the user
        doesn't repeat the device name.

        Two things happen here, every turn, regardless of whether this
        conversation has any remembered device yet:

          1. REMEMBER: every clause `IntentParser` resolves to a
             concrete `home_assistant` target for one of
             `_CONTEXT_REMEMBER_ACTIONS` (a REAL device name was
             mentioned) updates this conversation's "last device" for
             that tool, stamped with the CURRENT turn sequence number
             (`_device_context_turn_seq`) and the device's real
             `entity_id`/domain (`_remember_device_target`) - so even a
             turn that itself needed no context-filling still refreshes
             the memory for the NEXT turn. Sprint 57 (Contextual Home
             Assistant References) hardening: if a SINGLE turn names
             more than one DISTINCT real device for the same tool
             ("nyalain lampu kamar dan lampu dapur"), that is itself
             ambiguous evidence about "the device this conversation is
             about" - the existing memory is cleared rather than
             letting whichever clause happened to be last silently win
             ("most-recent-wins" would be a guess, which the brief's
             ambiguity rule forbids).
          2. FILL: if `text` is a SINGLE clause that IntentParser
             resolves to `home_assistant` `turn_on`/`turn_off` with NO
             target at all, and this conversation has a remembered
             device that is still FRESH (`_CONTEXT_MAX_TURN_AGE`) and
             DOMAIN-COMPATIBLE (`_CONTEXT_FILL_COMPATIBLE_DOMAINS`)
             with a plain on/off operation, `text` is rewritten into
             the exact same canonical "turn on/off <device>" phrasing
             `_classify_device_intent` already produces - so it
             re-parses identically and gets the EXACT same Planner ->
             verified-execution -> honest-result treatment as any
             explicit command. Multi-clause utterances are left
             untouched (deliberately conservative - see this method's
             own single-clause check below). A stale or domain-
             incompatible remembered device is treated the same as "no
             remembered device" - falls through unchanged, letting the
             normal (still-to-be-hardened) "no target" refusal handle
             it honestly rather than guessing.

        Always fails closed: any parsing error just returns `text`
        unchanged (this turn proceeds exactly as it would have before
        this feature existed) - a hiccup here never blocks or crashes a
        turn, it just means context wasn't applied this one time."""
        key = conversation_id or self._ENV_CONFIRMATION_KEY
        try:
            steps = IntentParser.parse(text)
        except Exception:
            return text

        memory = self._last_device_target.setdefault(key, {})
        while len(self._last_device_target) > self._last_device_target_max:
            oldest = next(iter(self._last_device_target))
            self._last_device_target.pop(oldest, None)

        # Sprint 57 - bounded, monotonic per-conversation turn counter,
        # used only to measure "how many turns old is this remembered
        # device" (freshness) - never reset except when the whole
        # conversation's memory is (ConversationEnded, see
        # `_on_conversation_ended`), matching `_last_device_target`'s
        # own lifecycle exactly.
        current_turn_seq = self._device_context_turn_seq.get(key, 0) + 1
        self._device_context_turn_seq[key] = current_turn_seq
        while len(self._device_context_turn_seq) > self._last_device_target_max:
            oldest = next(iter(self._device_context_turn_seq))
            self._device_context_turn_seq.pop(oldest, None)

        remember_targets: List[str] = []
        for step in steps:
            if (
                step.tool == "home_assistant"
                and step.action in self._CONTEXT_REMEMBER_ACTIONS
                and step.target
                and self._is_known_home_assistant_device(step.target)
            ):
                if step.target not in remember_targets:
                    remember_targets.append(step.target)
            # camera_ptz has no "device name" to remember (there's only
            # ever the one camera) - just a marker that this
            # conversation was just talking to it, for the FILL step
            # below.
            if step.tool == "camera_ptz":
                memory["camera_ptz"] = True

        if len(remember_targets) == 1:
            self._remember_device_target(key, "home_assistant", remember_targets[0], current_turn_seq)
        elif len(remember_targets) > 1:
            # Same-turn ambiguity about "which device is this
            # conversation about" - do not guess by keeping whichever
            # clause happened to be last. Un-remember rather than pick.
            memory.pop("home_assistant", None)
        # len == 0: no real device named this turn - leave any existing
        # memory exactly as it was (a turn that named no device at all
        # says nothing about whether the old memory is still right).

        if len(steps) == 1:
            step = steps[0]
            if step.tool == "home_assistant" and step.action in self._CONTEXT_FILLABLE_ACTIONS:
                # "No real target" covers both the empty-target case
                # ("sekarang matikan") AND a target that's nothing but
                # grammatical filler ("matiin aja", "nyalakan lagi") -
                # but NOT an unrecognized-yet-genuine-looking device name
                # ("lampu dapur" must still fail honestly as "device not
                # found", never get silently swapped for a different
                # remembered device - see `_CONTEXT_FILLER_WORDS`'s own
                # comment on exactly this distinction).
                no_real_target = not step.target or self._is_filler_only_target(step.target)
                if no_real_target:
                    remembered = memory.get("home_assistant")
                    resolved_target: Optional[str] = None
                    refusal_reason: Optional[str] = None
                    age: Optional[int] = None
                    if isinstance(remembered, dict):
                        age = current_turn_seq - remembered.get("turn_seq", current_turn_seq)
                        is_fresh = 0 <= age <= self._CONTEXT_MAX_TURN_AGE
                        is_compatible = remembered.get("domain") in self._CONTEXT_FILL_COMPATIBLE_DOMAINS
                        if is_fresh and is_compatible:
                            verb = self._CONTEXT_FILLABLE_ACTIONS[step.action]
                            target = remembered.get("target")
                            resolved_target = target
                            log(
                                f"device context: {text!r} named no real device - reusing the last one "
                                f"this conversation talked about ({target!r}, turn age {age})", "planner_bridge",
                            )
                        elif not is_fresh:
                            refusal_reason = "stale"
                            log(
                                f"device context: {text!r} named no real device - remembered device "
                                f"{remembered.get('target')!r} is stale (turn age {age}), refusing to guess",
                                "planner_bridge",
                            )
                        elif not is_compatible:
                            refusal_reason = "incompatible_domain"
                            log(
                                f"device context: {text!r} named no real device - remembered device "
                                f"{remembered.get('target')!r} (domain {remembered.get('domain')!r}) isn't "
                                f"compatible with a plain on/off, refusing to guess", "planner_bridge",
                            )
                    else:
                        refusal_reason = "no_memory"

                    # Sprint 57 (Contextual Home Assistant References) -
                    # OBSERVABILITY ONLY, same `self._event_bus.publish(
                    # Event(...))` pattern Sprint 50 already established
                    # for `memory_reference_classified` just above in this
                    # class - bounded classification labels and a device
                    # SLUG only, never the raw utterance text. A publish
                    # failure (or no event bus bound at all, e.g. a direct
                    # unit-test call) must never break a turn.
                    try:
                        if self._event_bus is not None:
                            self._event_bus.publish(Event(type="device_context_resolution", data={
                                "conversation_id": conversation_id,
                                "attempted": True,
                                "resolved": resolved_target is not None,
                                "candidate_count": 1 if isinstance(remembered, dict) else 0,
                                "target": resolved_target,
                                "refusal_reason": refusal_reason,
                                "turn_age": age,
                            }))
                    except Exception:
                        pass  # telemetry must never be able to break a turn

                    if resolved_target is not None:
                        verb = self._CONTEXT_FILLABLE_ACTIONS[step.action]
                        return f"turn {verb} {resolved_target}"

            # "sekarang arahkan ke komputer" right after "arahkan kamera
            # ke tengah" - `_classify_camera_ptz` (deliberately, to avoid
            # false positives on unrelated sentences) hard-requires the
            # word "kamera"/"camera" to even consider a clause, so this
            # never reaches camera_ptz at all and instead falls all the
            # way through to "unknown" - which then gives the LLM zero
            # tool grounding and it freely improvises (previously
            # misread as "help me operate the PC"). Only fill in when
            # the raw text still has the exact "<move verb> ke/to
            # <target>" shape camera_ptz itself looks for (reusing
            # `_CAMERA_TARGET_RE`/`_CAMERA_WORDS` straight off
            # `luno.planner.parser` - not a hand-copied duplicate - same
            # local-import convention as `_is_known_home_assistant_
            # device()` above) AND this conversation just talked to the
            # camera. Rewritten into the same canonical "point the
            # camera at X" phrasing camera_ptz's own target-extraction
            # regex produces, so a fixed direction word ("kiri"/"left")
            # still resolves to a pan/tilt action, not a bogus preset
            # literally named "left".
            if step.tool == "unknown" and memory.get("camera_ptz"):
                from luno.planner.parser import _CAMERA_TARGET_RE as _camera_target_re
                from luno.planner.parser import _CAMERA_WORDS as _camera_words
                lower_text = text.lower()
                has_camera_word = any(re.search(rf"\b{re.escape(w)}\b", lower_text) for w in _camera_words)
                if not has_camera_word:
                    match = _camera_target_re.search(lower_text)
                    if match:
                        target = match.group(1).strip()
                        if target:
                            log(
                                f"device context: {text!r} looks like a follow-up camera move with "
                                f"no 'camera'/'kamera' word - reusing the camera this conversation "
                                f"just talked to (target={target!r})", "planner_bridge",
                            )
                            return f"point the camera at {target}"

        return text

    # -- Sprint 58 (HA Multi-Entity & Group Commands) -------------------------
    #
    # Phase 0 reconnaissance (this sprint's own explicit precondition) found:
    #   - `_CLAUSE_SPLIT_RE` in `luno.planner.parser` already splits "A dan B"
    #     into two raw clauses, but natural verb ellipsis ("nyalain lampu
    #     kamar dan lampu ruang tamu" - the verb is not repeated for B) means
    #     the SECOND clause has no verb of its own and `_clause_to_step`
    #     correctly falls through to `tool="unknown"` for it, per that
    #     module's own "never guess" discipline - it is NOT a parser bug,
    #     just a parser that (correctly, conservatively) doesn't understand
    #     compound targets. Two clauses that EACH repeat their own verb
    #     ("nyalakan rgb strip, lalu nyalakan rgb komputer") already parse
    #     into two independent `home_assistant` steps today and are
    #     completely untouched by anything below.
    #   - "semua lampu" is not a recognized keyword at all today - it
    #     parses as ONE `home_assistant` step whose (unresolvable) target is
    #     literally the slug "semua_lampu", which the existing resolver
    #     honestly (if unhelpfully) reports as an unknown device.
    #   - Zero area/room/zone metadata exists anywhere in this project's
    #     registry (`config/lights.config.json`/`switches.config.json`/
    #     `luno/devices.py`'s loading code - confirmed by direct read and a
    #     project-wide grep) - "semua lampu di kamar"-shaped commands are
    #     therefore DELIBERATELY refused with an honest "not supported yet"
    #     explanation rather than either silently expanding to every light
    #     (wrong) or guessing which lights are "in" an area that doesn't
    #     exist in any config (also wrong). Documented, minimal-safe-
    #     foundation choice per this sprint's own STOP CONDITION, not an
    #     oversight - see docs/change_impact/ha_multi_entity_commands.md.
    #   - A "contextual group" ("Nyalain lampu kamar." then "Matikan
    #     semuanya.") is ALSO deliberately not implemented: Sprint 57's own
    #     `_last_device_target` is a single-slot memory (one remembered
    #     device per tool, by design - see that sprint's own ambiguity
    #     rule), so there is no "group of previously-referenced devices" to
    #     fall back to without a structural memory redesign this sprint's
    #     own invariants explicitly forbid ("no second memory system", "no
    #     unnecessary persistent state"). "semuanya" alone also doesn't
    #     unambiguously mean "all lights" specifically - guessing would
    #     violate the ambiguity rule. Nothing below recognizes this shape at
    #     all; it falls straight through to the existing, unmodified
    #     Sprint 57 `_apply_device_context()` path exactly as before.
    #
    # Design: detection + resolution both happen HERE, entirely inside this
    # pre-Planner text layer, using a throwaway, `client=None`
    # `RealHomeAssistantHandler` instance whose `._resolve_entity_tiered()`
    # is called PURELY for its (confirmed pure/client-free during
    # resolution - see that method's own docstring) resolution logic, never
    # `.execute()`. This is NOT a second resolver - same category of direct,
    # ordinary method call as this class's own pre-existing
    # `IntentParser.parse()` and `luno.devices` usage elsewhere in this
    # file. Once every target in a detected group is confirmed resolvable
    # (and, for turn_on/turn_off, domain-compatible - reusing Sprint 57's
    # own `_CONTEXT_FILL_COMPATIBLE_DOMAINS`, not a new gate), the whole
    # group is rewritten into the exact same repeated "turn on/off
    # <device>, turn on/off <device>, ..." canonical phrasing
    # `_apply_device_context()`'s own FILL step already produces for a
    # single target - the UNMODIFIED `IntentParser`/Planner/Tool Manager
    # pipeline then handles it exactly like any hand-typed multi-command
    # sentence, with zero parser changes. If ANY target fails (ambiguous,
    # unknown, wrong domain) or the shape is a recognized-but-unsupported
    # variant (area-qualified, or zero eligible lights), the ENTIRE turn's
    # effective text becomes the empty string instead -
    # `IntentParser.parse("")` produces zero steps, so `_handle_utterance`'s
    # own `real_task_count > 0` guard skips `self.planner.execute()`
    # entirely: this is the mechanism that guarantees ZERO HA API calls for
    # ANY target in the group, not just the failing one, and it applies
    # BEFORE the first HA action is ever sent - resolution for every target
    # completes first, unconditionally, before any rewrite happens at all.

    #: Bilingual "all/every" + "light(s)" co-occurrence, same discipline as
    #: `_CAMERA_WORDS`'s own co-occurrence check in `luno.planner.parser` -
    #: both words must appear (anywhere in the clause) rather than one
    #: brittle combined regex, so "lampu semua nyala" and "semua lampu"
    #: both match without needing every possible word order spelled out.
    _GROUP_ALL_WORD_RE = re.compile(r"\b(?:semua|all|every)\b", re.IGNORECASE)
    _GROUP_LIGHT_WORD_RE = re.compile(r"\blampu\b|\blights?\b", re.IGNORECASE)
    #: Sprint 62 (Multi-Domain Area Group Control) - Phase 0/1 evaluated
    #: whether `_GROUP_LIGHT_WORD_RE` should be widened to also recognize
    #: other HA domain words ("switch"/"saklar", "AC"/"kipas", ...) so an
    #: area-qualified group command for a non-light domain could resolve
    #: the same way "lampu"/"light(s)" already does. Conclusion: `switch`
    #: is the only other domain with a real registry+resolver+execution
    #: path in this checkout (`devices.SWITCHES`), but that registry's
    #: loader (`load_switches_config()`) only ever produces a FLAT
    #: `name -> entity_id` string mapping - no dict-format entries, no
    #: `"aliases"`, and structurally no way to carry an `"area"` field at
    #: all (confirmed against both the loader source and the real, on-disk
    #: `config/switches.config.json`). `fan`/`climate`/`media_player` have
    #: no registry/config loader of their own whatsoever (see Sprint 57's
    #: own `_CONTEXT_FILL_COMPATIBLE_DOMAINS` docstring - included there
    #: only for forward-compatibility, never backed by real data). Per this
    #: sprint's own STOP CONDITION 1 ("domain registry tidak memiliki
    #: struktur aman untuk area metadata"), widening this regex was NOT
    #: done - `light` remains the only supported domain, and this stays
    #: untouched. A command using a different domain word ("matikan semua
    #: switch di kamar") simply never matches this regex, so `command_kind`
    #: stays `None` here and the ALREADY-EXISTING, unmodified single-target
    #: pipeline handles it - proven (not just asserted) to fail safely with
    #: zero HA calls via `RealHomeAssistantHandler.execute()`'s own
    #: `target and entity_id is None -> _unknown_device_result()` guard,
    #: which returns before the `with self._lock:` block that would ever
    #: reach `self._client.call_service(...)`. See `docs/change_impact/
    #: multi_domain_area_groups.md` for the full Phase 0/1 evaluation and
    #: `tests/test_sprint62_multi_domain_area_groups.py` (scenario D) for
    #: the regression proof.
    #: "lampu di kamar" / "lights in the bedroom" - an AREA qualifier right
    #: after the light word. Deliberately narrow (only fires immediately
    #: after "lampu"/"light(s)", not anywhere in the sentence) so it can't
    #: false-positive on an unrelated "di"/"in" elsewhere in a longer
    #: utterance.
    _GROUP_AREA_RE = re.compile(r"\b(?:lampu|lights?)\b\s+(?:di|in)\s+(\w+)", re.IGNORECASE)

    #: Sprint 59 (Single-Room Home Assistant Group Control) ORIGINALLY put a
    #: hardcoded `_SINGLE_ROOM_NAME = "kamar"` constant and an `_is_single_
    #: room_word()` method here, because Phase 0 reconnaissance at the time
    #: found zero STRUCTURED area/room/zone field anywhere in this
    #: project's config - only converging TEXTUAL evidence (Main Lamp's own
    #: `entity_id` containing "kamar_tidur", `environment_triggers.json`'s
    #: pre-existing "sleepy" trigger already grouping all 3 lights, zero
    #: second-room evidence anywhere) that the ENTIRE registry lived in one
    #: identifiable room.
    #:
    #: Sprint 60 (Structured Room/Area Schema Foundation) closed that gap -
    #: `config/lights.config.json` entries can now carry a real, structured
    #: `"area"` field (`devices.get_device_area()`/`get_devices_by_area()`),
    #: and this project's registry was migrated: Main Lamp/RGB Strip/RGB
    #: Computer all now carry `"area": "kamar"`.
    #:
    #: Sprint 61 (Generalized Area-Aware Home Assistant Group Command)
    #: REMOVES `_SINGLE_ROOM_NAME`/`_is_single_room_word()` entirely -
    #: `_apply_ha_group_resolution()` below now treats `area_word` (any
    #: value captured by `_GROUP_AREA_RE`, not just "kamar") generically:
    #: it exact-matches (case-insensitively, never fuzzily) against
    #: `devices.get_devices_by_area()`, which is now the ONLY source of
    #: truth for room/area membership. This makes "kamar" work through the
    #: SAME general mechanism every other area word does - not a special
    #: case anymore - while producing byte-for-byte the same result for
    #: this project's real, already-migrated "kamar" set (proved by
    #: `tests/test_sprint61_generalized_area_groups.py`, not just
    #: asserted). Confirmed via `grep` that neither symbol has any other
    #: consumer anywhere in this codebase before removal - see `docs/
    #: change_impact/generalized_area_groups.md` for the full Phase 0/9
    #: writeup.

    #: See `_ha_explicit_multi_target_shape()`'s own "SCOPE GUARD" docstring
    #: section - these two together restrict explicit multi-target
    #: detection to Indonesian "dan"-only phrasing, deliberately excluding
    #: comma/"and"/"then" (this parser's existing GENERAL, unrelated-clause
    #: separators) to avoid misreading a mixed command+unrelated-clause
    #: utterance as a device group.
    _MULTI_TARGET_DAN_RE = re.compile(r"\bdan\b", re.IGNORECASE)
    _MULTI_TARGET_DISQUALIFYING_RE = re.compile(r",|\band\b|\bthen\b", re.IGNORECASE)

    def _ha_group_all_lights_shape(self, text: str) -> Optional[Tuple[str, Optional[str]]]:
        """Sprint 58 - detects the "semua lampu"/"all lights" GROUP-ALL
        shape directly off the raw utterance text (independent of
        `IntentParser`, which has no vocabulary for this shape at all
        today - see this section's own reconnaissance note above). Returns
        `None` if this utterance isn't this shape at all (the overwhelming
        common case - falls through unchanged), INCLUDING when it's a
        compound utterance with other clauses alongside the "semua lampu"
        phrase (`IntentParser.parse(text)` producing more than one step at
        all is treated as "not clean enough to safely rewrite" - never
        silently drop whatever the other clause(s) asked for). Otherwise
        returns `(action, area_word)`: `action` is `"turn_on"`/`"turn_off"`
        (reusing the exact same verb regexes `IntentParser` itself uses -
        `_TURN_ON_RE`/`_TURN_OFF_RE` - imported, never re-derived, so
        "which verbs count" can never silently drift between the two);
        `area_word` is the captured area name if this is the area-qualified
        variant ("semua lampu di kamar"/"semua lampu di dapur"/...), else
        `None` for the plain "semua lampu" group-all ("every configured
        light", unconditionally).

        Sprint 61 (Generalized Area-Aware Home Assistant Group Command) -
        `area_word` itself is captured EXACTLY as it always was (this
        method, and `_GROUP_AREA_RE`, are UNTOUCHED by Sprint 61 - only
        WHAT HAPPENS with an already-captured `area_word`, in
        `_apply_ha_group_resolution()` below, was generalized from "must
        equal the one hardcoded room name" to "must exact-match some
        light's structured `area` metadata, whatever that value is").
        Note `_GROUP_AREA_RE` still only captures an area word when "di"/
        "in" immediately follows "lampu"/"light(s)" - "semua lampu kamar"
        (no preposition) still never captures one at all, and is still
        always treated as the unqualified "every light" shape, exactly as
        Sprint 58/59 already established (deliberately preserved, not
        reconsidered, by Sprint 61 - see `docs/change_impact/
        generalized_area_groups.md`)."""
        if not self._GROUP_ALL_WORD_RE.search(text) or not self._GROUP_LIGHT_WORD_RE.search(text):
            return None
        try:
            steps = IntentParser.parse(text)
        except Exception:
            return None
        if len(steps) != 1:
            return None
        from luno.planner.parser import _TURN_ON_RE, _TURN_OFF_RE
        if _TURN_ON_RE.search(text):
            action = "turn_on"
        elif _TURN_OFF_RE.search(text):
            action = "turn_off"
        else:
            return None
        area_match = self._GROUP_AREA_RE.search(text)
        area_word = area_match.group(1).strip() if area_match else None
        return action, area_word

    def _ha_explicit_multi_target_shape(self, text: str) -> Optional[Tuple[str, List[str]]]:
        """Sprint 58 - detects the "A dan B [dan C ...]" ELIDED-VERB
        explicit-multi-target shape by walking `IntentParser.parse(text)`'s
        own real output, deliberately NOT by re-deriving clause boundaries
        with a second, independently-maintained regex pass (lower drift
        risk than re-implementing clause splitting). Returns `None` for
        anything that isn't exactly this one narrow shape: a single
        `home_assistant` turn_on/turn_off clause as the VERY FIRST clause
        (with a real, non-filler target of its own), followed by one or
        more clauses that ALL failed to parse as anything at all
        (`tool="unknown"` - i.e. clauses with no verb of their own, the
        natural-language verb-ellipsis case this method exists for).

        Deliberately conservative, matching this sprint's own safety-first
        priority: an anchor that isn't the first clause (so no candidate
        target can ever be silently dropped from BEFORE it - this
        structurally rules out ever discarding a real clause the way a
        "just take whatever's after the first real step" approach could),
        a trailing clause that DID parse as some other real tool/action
        (including a second, independent `home_assistant` clause - already
        correctly handled, untouched, by the existing pipeline), or no
        anchor at all - all fall through as `None`, leaving `text`
        completely untouched rather than guessing at a shape this method
        isn't confident about.

        SCOPE GUARD (fixes a real, confirmed regression - see this
        method's own `_MULTI_TARGET_DAN_RE`/`_MULTI_TARGET_DISQUALIFYING_RE`
        docstring): comma/"and"/"then" are ALL already-established GENERAL
        clause separators for entirely UNRELATED actions in this parser
        (the spec's own multi-command example is comma-separated: "open
        Chrome, turn on the bedroom light, ... then play Spotify"), and a
        pre-existing regression test - `test_runtime_demo.py::test_mixed_
        utterance_real_command_still_succeeds_despite_unknown_clause` -
        depends on "turn on the lights and bagaimana cuaca hari ini" NOT
        being treated as a 2-target group (the second clause is an
        unrelated QUESTION, not a second device - lexical similarity
        scoring alone cannot reliably tell "an unregistered/typo'd device
        name" apart from "a completely unrelated sentence fragment", so
        this method does not try). Every one of THIS sprint's own worked
        examples uses Indonesian "dan" exclusively - detection is
        therefore scoped to ONLY that shape: the raw text must contain
        "dan" and must NOT also contain a comma, "and", or "then". A
        deliberate, documented scope reduction (STOP CONDITION: safety
        and backward compatibility over functionality), not an
        oversight - anything wider always falls through completely
        untouched to the existing, unmodified single-target pipeline."""
        if not self._MULTI_TARGET_DAN_RE.search(text) or self._MULTI_TARGET_DISQUALIFYING_RE.search(text):
            return None
        try:
            steps = IntentParser.parse(text)
        except Exception:
            return None
        if len(steps) < 2:
            return None
        anchor = steps[0]
        if anchor.tool != "home_assistant" or anchor.action not in ("turn_on", "turn_off"):
            return None
        if not anchor.target or self._is_filler_only_target(anchor.target):
            return None

        trailing = steps[1:]
        if any(s.tool != "unknown" for s in trailing):
            return None  # a real action elsewhere (incl. a 2nd independent HA verb clause) - leave alone

        from luno.planner.parser import _slugify as _parser_slugify
        target_slugs = [anchor.target]
        for s in trailing:
            raw_clause = ((s.params or {}).get("raw_text") or s.label or "").strip()
            if not raw_clause:
                return None  # shouldn't happen (parse() already drops empty clauses) - fail closed
            slug = _parser_slugify(raw_clause)
            if not slug or self._is_filler_only_target(slug):
                return None  # trailing clause is pure filler, not a device name - not this shape
            target_slugs.append(slug)

        return anchor.action, target_slugs

    def _resolve_ha_group_targets(
        self, target_slugs: List[str],
    ) -> Tuple[List[Tuple[str, str, Optional[str], Optional[str]]], int, int]:
        """Sprint 58 - resolves every slug in `target_slugs` through the
        EXACT SAME resolver Sprint 52 already built and Sprint 57 already
        reuses (`RealHomeAssistantHandler._resolve_entity_tiered()`) - a
        throwaway, `client=None` instance is used PURELY so this ordinary
        Python method call can happen without ever constructing a real
        Adapter Layer client; that method is confirmed pure/client-free
        during resolution (never touches `self._client`/`self._lock` - only
        `self._resolve_entity_id()` and the module-level
        `_score_candidates()`, both pure registry/difflib lookups).

        Returns `(resolved, ambiguous_count, unresolved_count)` where
        `resolved` is one `(raw_slug, resolution_method, entity_id, domain)`
        tuple per target (`entity_id`/`domain` are `None` for a target that
        failed to resolve, INCLUDING one that resolved to a real entity in
        a domain `turn_on`/`turn_off` was never meant for - see the
        `domain_mismatch` branch below, reusing Sprint 57's own
        `_CONTEXT_FILL_COMPATIBLE_DOMAINS` gate rather than a new one).
        Callers must treat any nonzero `ambiguous_count`/`unresolved_count`
        as "refuse the WHOLE group" - this method itself never refuses or
        rewrites anything, it only reports what it found."""
        from luno.tool_manager.builtin.real_home_assistant import RealHomeAssistantHandler
        handler = RealHomeAssistantHandler(client=None)
        resolved: List[Tuple[str, str, Optional[str], Optional[str]]] = []
        ambiguous_count = 0
        unresolved_count = 0
        for slug in target_slugs:
            result = handler._resolve_entity_tiered(slug)
            if not result.executable or not result.resolved_entity:
                if result.resolution_method == "ambiguous":
                    ambiguous_count += 1
                else:
                    unresolved_count += 1
                resolved.append((slug, result.resolution_method, None, None))
                continue
            entity_id = result.resolved_entity
            domain = entity_id.split(".", 1)[0] if entity_id and "." in entity_id else None
            if domain not in self._CONTEXT_FILL_COMPATIBLE_DOMAINS:
                unresolved_count += 1
                resolved.append((slug, "domain_mismatch", entity_id, domain))
                continue
            resolved.append((slug, result.resolution_method, entity_id, domain))
        return resolved, ambiguous_count, unresolved_count

    def _apply_ha_group_resolution(self, text: str, conversation_id: Optional[str]) -> Tuple[str, Optional[str]]:
        """Sprint 58 (HA Multi-Entity & Group Commands) - the single entry
        point `_handle_utterance()` calls, BEFORE Sprint 57's own
        `_apply_device_context()` (see that call site's own comment for why
        group commands are checked first: they're fully self-contained in
        `text` and never depend on conversation memory, unlike a contextual
        single-target reference - see scenario P in
        `tests/test_sprint58_ha_multi_entity_commands.py` for the proof
        that a plain contextual reference can never be misdetected as a
        group here). Returns `(effective_text, refusal_note)`:

          - `(text, None)` unchanged - NOT a detected group/multi-target
            shape at all (the overwhelming common case). The caller falls
            through to the existing, unmodified single-target pipeline
            exactly as before this sprint existed.
          - `(rewritten_text, None)` - a group WAS detected and EVERY
            target resolved cleanly; `rewritten_text` is the canonical
            "turn on/off <device>[, turn on/off <device> ...]" phrasing,
            deduplicated by resolved `entity_id` (so a literal duplicate
            target, or the same device named via two different aliases, is
            only ever actioned once), safe to hand to the unmodified
            `IntentParser`/Planner.
          - `("", refusal_note)` - a group WAS detected but at least one
            target failed (ambiguous, unresolved, wrong domain) OR this is
            a recognized-but-deliberately-unsupported variant (area-
            qualified, or a "semua lampu" whose registry has zero eligible
            lights). `effective_text` is always the empty string here -
            `IntentParser.parse("")` produces zero steps, so the caller's
            own `real_task_count > 0` guard skips `self.planner.execute()`
            entirely: this is the mechanism that guarantees ZERO HA API
            calls for ANY target in the group, not just the failing one."""
        command_kind: Optional[str] = None
        action: Optional[str] = None
        target_slugs: List[str] = []
        area_word: Optional[str] = None

        try:
            group_all = self._ha_group_all_lights_shape(text)
        except Exception:
            group_all = None
        if group_all is not None:
            action, area_word = group_all
            command_kind = "group_all_light"
        else:
            try:
                explicit = self._ha_explicit_multi_target_shape(text)
            except Exception:
                explicit = None
            if explicit is not None:
                action, target_slugs = explicit
                command_kind = "explicit_multi_target"

        if command_kind is None:
            return text, None  # not a group/multi-target shape at all - untouched

        detected_count = 0
        resolved_count = 0
        ambiguous_count = 0
        unresolved_count = 0
        duplicate_count = 0
        final_decision = "refused_unresolved"
        effective_text = ""
        refusal_note: Optional[str] = None
        area_recognized: Optional[bool] = None  # only meaningful for command_kind == "group_all_light"
        structured_area_metadata_present: Optional[bool] = None  # only meaningful for command_kind == "group_all_light"

        if command_kind == "group_all_light":
            from luno import devices
            from luno.planner.parser import _slugify as _parser_slugify
            verb = "on" if action == "turn_on" else "off"

            # Sprint 61 (Generalized Area-Aware Home Assistant Group
            # Command) - `devices.get_devices_by_area()` (Sprint 60) is now
            # the ONLY source of truth for room/area membership, for ANY
            # area word `_GROUP_AREA_RE` captured - not just the formerly-
            # hardcoded "kamar". Exact, case-insensitive match ONLY (that
            # helper's own normalization) - never fuzzy, never a guess at
            # "closest" area. `area_word is None` (plain "semua lampu", no
            # room word at all) is left 100% untouched, exactly as Sprint
            # 58/59/60 always did - unconditionally "every configured
            # light" - this sprint's own PHASE 5/9 instruction ("SINGLE
            # ROOM MUST REMAIN IDENTICAL"/preserve existing semantics).
            any_structured_area = any(
                isinstance(cfg, dict) and cfg.get("area") for cfg in devices.LIGHTS.values()
            )
            structured_area_metadata_present = any_structured_area

            if area_word is None:
                allowed_names: Optional[set] = None  # None = every configured light, unconditionally
                area_recognized = True
            else:
                matched_names = devices.get_devices_by_area(area_word)
                allowed_names = set(matched_names)
                area_recognized = bool(matched_names)

            if area_word is not None and not area_recognized:
                # Sprint 61 - PHASE 8's own safety rule: an unrecognized
                # area word (no light anywhere carries that exact,
                # normalized `"area"` value - whether because it's a typo,
                # a genuinely different room this project has zero
                # configured information about, or because the registry
                # was never migrated to carry ANY area metadata at all)
                # ALWAYS refuses - zero HA calls, never a fallback to
                # "every light", never a guess at the "closest" area name.
                known_areas = sorted({
                    cfg.get("area") for cfg in devices.LIGHTS.values()
                    if isinstance(cfg, dict) and cfg.get("area")
                })
                final_decision = "refused_unsupported_area"
                refusal_note = (
                    f'The user said: "{text}". Area/room-scoped Home Assistant group commands for '
                    f'"{area_word}" are not supported - no configured light is tagged with that area '
                    f"(known area(s) right now: {', '.join(known_areas) if known_areas else 'none configured'}), "
                    "so honestly explain that you can't target that area without guessing which lights "
                    "(if any) might be there."
                )
            else:
                # `area_word is None` (every light) OR `area_word` exact-
                # matched at least one light's structured `"area"` field -
                # expand to exactly that set, reusing Sprint 58's own
                # canonical-phrasing/dedup/fail-safe enumeration unchanged.
                seen_entities: List[str] = []
                clauses: List[str] = []
                for name, cfg in devices.LIGHTS.items():
                    if allowed_names is not None and name not in allowed_names:
                        continue  # Sprint 60/61 - structured area metadata says this light isn't in the requested area
                    entity_id = cfg.get("entity_id") if isinstance(cfg, dict) else None
                    if not entity_id:
                        continue  # fail-safe: never assume a registry entry is controllable without a real entity_id
                    domain = entity_id.split(".", 1)[0] if "." in entity_id else None
                    if domain != "light":
                        continue  # Sprint 61 PHASE 2/4 - GROUP DOMAIN = light only, defensively re-checked here too
                    slug = _parser_slugify(name)
                    if not slug:
                        continue
                    detected_count += 1
                    if entity_id in seen_entities:
                        duplicate_count += 1
                        continue
                    seen_entities.append(entity_id)
                    clauses.append(f"turn {verb} {slug}")
                resolved_count = len(clauses)
                if not clauses:
                    final_decision = "empty_no_op"
                    refusal_note = (
                        f'The user said: "{text}". There are no lights configured in this system at all '
                        f"right now, so there is nothing to turn {verb}. Explain this honestly rather than "
                        f"claiming anything was turned {verb}."
                    )
                else:
                    final_decision = "executed"
                    effective_text = ", ".join(clauses)
        else:  # explicit_multi_target
            detected_count = len(target_slugs)
            resolved, ambiguous_count, unresolved_count = self._resolve_ha_group_targets(target_slugs)
            if ambiguous_count or unresolved_count:
                final_decision = "refused_ambiguous" if ambiguous_count else "refused_unresolved"
                refusal_note = (
                    f'The user said: "{text}". This command names more than one device, and at least one '
                    "of them could not be safely determined (either ambiguous between multiple real "
                    "devices, not found in the registry, or not a device 'turn on/off' applies to). To "
                    "avoid guessing and controlling the wrong device, NONE of the devices in this command "
                    "were touched - not even the ones that WERE clear. Ask which exact device was meant "
                    "before trying again."
                )
            else:
                verb = "on" if action == "turn_on" else "off"
                seen_entities = []
                clauses = []
                for slug, method, entity_id, domain in resolved:
                    if entity_id in seen_entities:
                        duplicate_count += 1
                        continue
                    seen_entities.append(entity_id)
                    clauses.append(f"turn {verb} {slug}")
                resolved_count = len(clauses)
                final_decision = "executed"
                effective_text = ", ".join(clauses)

        try:
            if self._event_bus is not None:
                self._event_bus.publish(Event(type="ha_group_command_resolution", data={
                    "conversation_id": conversation_id,
                    "command_kind": command_kind,
                    "detected_target_count": detected_count,
                    "resolved_target_count": resolved_count,
                    "ambiguous_target_count": ambiguous_count,
                    "unresolved_target_count": unresolved_count,
                    "duplicate_target_count": duplicate_count,
                    "area_recognized": area_recognized,
                    "structured_area_metadata_present": structured_area_metadata_present,
                    "final_decision": final_decision,
                }))
        except Exception:
            pass  # telemetry must never be able to break a turn

        return effective_text, refusal_note

    # -- explicit long-term memory (config/long_term_memory.json) ------------

    def _handle_explicit_memory_command(self, text: str, request_id: str) -> Optional[str]:
        """`luno/memory.py`'s EXPLICIT remember/forget fact store
        (`config/long_term_memory.json`, e.g. "inget ya, aku alergi kacang")
        - a completely different system from the `MemoryRetriever`
        `"long_term_memory"` source registered in `__init__` above, which
        reads vision-based observations from `luno.vision_memory` instead.
        That naming collision is real but harmless: this method never
        touches `self.memory_retriever` at all, and the vision-based
        source keeps its own name unchanged.

        Detected and acted on directly here, BEFORE planning, because
        these are meta conversation commands ("remember that...",
        "forget that...", "forget everything") - not smart-home tool
        calls `IntentParser` has any vocabulary for, and shouldn't cost a
        Planner/device-intent-classifier round trip (see the caller,
        which skips `_classify_device_intent` entirely when this method
        already handled the turn).

        Returns a system_note describing what was ACTUALLY done (so the
        LLM confirms honestly - matching this whole project's "never
        claim success for something that didn't happen" convention), or
        `None` if `text` wasn't one of these commands at all, in which
        case the caller falls through to normal planning exactly as
        before."""
        lower = text.lower()

        if memory.is_clear_everything_command(lower):
            memory.clear_all_long_term()
            memory.clear_short_term()
            log(f"request_id={request_id} - explicit memory: cleared everything", "planner_bridge")
            return (
                f'The user said: "{text}". You just permanently erased ALL long-term memory '
                "(every remembered fact) as requested. Confirm this briefly."
            )

        if memory.is_clear_short_term_command(lower):
            memory.clear_short_term()
            log(f"request_id={request_id} - explicit memory: cleared short-term history", "planner_bridge")
            return (
                f'The user said: "{text}". You just cleared this conversation\'s short-term '
                "history as requested (long-term facts are untouched). Confirm this briefly."
            )

        # Memory Conflict Resolution sprint - "tampilkan konflik memory"
        # (read-only, lists every unresolved AMBIGUOUS_CONFLICT group) and
        # "memory X yang benar" (explicit resolution, checked next). Both
        # are anchored, narrow patterns, checked here alongside every
        # other meta-command in this method for the same reason the
        # docstring above already gives.
        if memory.detect_show_conflicts_command(text):
            groups = memory.list_conflicts()
            log(
                f"request_id={request_id} - explicit memory: show conflicts -> {len(groups)} group(s)",
                "planner_bridge",
            )
            if not groups:
                return (
                    f'The user said: "{text}", asking to see memory conflicts. Tell them honestly '
                    "there are no unresolved memory conflicts right now."
                )
            summary_parts = []
            for group in groups:
                sides = " vs. ".join(f'"{m["text"]}"' for m in group)
                summary_parts.append(sides)
            return (
                f'The user said: "{text}". Here are the unresolved memory conflicts: '
                f"{'; '.join(summary_parts)}. Describe these briefly and naturally (don't just recite "
                "the list verbatim), and mention they can say something like \"memory <topic> yang benar\" "
                "to resolve one."
            )

        resolve_topic = memory.detect_resolve_conflict_command(text)
        if resolve_topic:
            status, entry = memory.resolve_conflict_by_topic(resolve_topic)
            log(
                f"request_id={request_id} - explicit memory: resolve conflict topic={resolve_topic!r} -> status={status}",
                "planner_bridge",
            )
            if status == "resolved":
                return (
                    f'The user said: "{text}". You just confirmed which memory was correct: '
                    f"\"{entry['text']}\". The other, superseded side was kept in its history, not deleted. "
                    "Confirm this briefly."
                )
            if status == "ambiguous":
                return (
                    f'The user said: "{text}". Multiple unresolved memory conflicts could match "{resolve_topic}" - '
                    "you did NOT resolve anything (avoiding a guess about which one they meant). "
                    "Ask them, briefly, to be more specific."
                )
            return (
                f'The user said: "{text}". They tried to resolve a memory conflict about "{resolve_topic}", but '
                "nothing currently unresolved matches that topic - tell them honestly you didn't find a "
                "matching conflict, so nothing was changed."
            )

        # Memory Lifecycle & Maintenance sprint (Step 12) - deterministic,
        # explicit-only maintenance commands, checked here alongside every
        # other meta memory command in this method. Ordinary conversation
        # never reaches these branches - only these exact anchored
        # patterns do, per Step 12/15's own "only explicit commands may
        # mutate state, never ordinary conversation" rule. Health/preview
        # are read-only; only the "run maintenance"/archive-by-id/
        # unarchive-last branches ever call `apply_maintenance_plan()`/
        # `archive_memory_by_id()`/`unarchive_last_memory()`.
        if memory.detect_memory_health_command(text):
            report = memory.memory_health_report()
            log(f"request_id={request_id} - explicit memory: health report (total={report['total']})", "planner_bridge")
            return (
                f'The user said: "{text}", asking for a memory health check. Here is the current state: '
                f"{memory.format_memory_health_report(report)}. Summarize this naturally and briefly for them - "
                "don't just recite every number verbatim."
            )

        if memory.detect_memory_maintenance_preview_command(text):
            preview = memory.preview_maintenance_text()
            log(f"request_id={request_id} - explicit memory: maintenance preview", "planner_bridge")
            return (
                f'The user said: "{text}", asking for a memory maintenance analysis/preview. This is ANALYSIS '
                f"ONLY - nothing was changed yet. Here is the plan: {preview}. Summarize this naturally and "
                'briefly, and mention they can say "jalankan maintenance memory" to actually apply it.'
            )

        if memory.detect_memory_maintenance_run_command(text):
            plan = memory.analyze_memory_maintenance()
            results = memory.apply_maintenance_plan(plan)
            applied = [r for r in results if r["status"] == "applied"]
            log(
                f"request_id={request_id} - explicit memory: maintenance executed "
                f"({len(applied)}/{len(results)} applied)", "planner_bridge",
            )
            if not applied:
                return (
                    f'The user said: "{text}", asking you to run memory maintenance. Nothing needed to change - '
                    "tell them honestly everything is already in good shape (no memories were archived, "
                    "reinforced, or consolidated)."
                )
            summary = "; ".join(f"{r['action']} memory {r['memory_id']}" for r in applied)
            return (
                f'The user said: "{text}". You just ran memory maintenance and applied these changes: {summary}. '
                "Nothing was deleted - archived memories are just hidden from normal recall, and any consolidated "
                "memory's old wording was preserved in its history. Confirm this briefly and naturally."
            )

        archive_id = memory.detect_archive_memory_by_id_command(text)
        if archive_id:
            status, entry = memory.archive_memory_by_id(archive_id)
            log(f"request_id={request_id} - explicit memory: archive id={archive_id!r} -> status={status}", "planner_bridge")
            if status == "archived":
                return (
                    f'The user said: "{text}". You just archived long-term memory "{entry["text"]}" - it is '
                    "hidden from normal recall now, but NOT deleted; still recoverable. Confirm this briefly."
                )
            if status == "protected":
                return (
                    f'The user said: "{text}", asking to archive memory {archive_id}, but that memory is '
                    "protected (it's a core/important memory or part of an unresolved conflict) - tell them "
                    "honestly you did NOT archive it, and why."
                )
            return (
                f'The user said: "{text}". They asked you to archive memory id "{archive_id}", but no memory '
                "with that id exists - tell them honestly you couldn't find it, so nothing was changed."
            )

        if memory.detect_unarchive_last_memory_command(text):
            entry = memory.unarchive_last_memory()
            log(
                f"request_id={request_id} - explicit memory: unarchive last -> {'ok' if entry else 'no-op'}",
                "planner_bridge",
            )
            if entry:
                return (
                    f'The user said: "{text}". You just un-archived their most recent long-term memory '
                    f"(\"{entry['text']}\") - it's back in normal recall now. Confirm this briefly."
                )
            return (
                f'The user said: "{text}", asking you not to archive the last memory, but there is either no '
                "long-term memory yet or it wasn't archived in the first place - tell them honestly nothing "
                "needed to change."
            )

        # Manual Memory Management sprint - explicit update ("ubah memory
        # GPU jadi RTX 5070") and delete-by-id/delete-by-topic ("hapus
        # memory nomor 12" / "hapus memory tentang GPU lamaku"), checked
        # here alongside the pre-existing remember/forget meta-commands
        # below (same "meta conversation command, no IntentParser
        # vocabulary, don't cost a Planner round trip" reasoning this
        # whole method's docstring already gives). By-id is checked BEFORE
        # by-topic per `detect_delete_memory_by_id_command`'s own
        # docstring (a numeric-id phrase would otherwise also match the
        # topic pattern's catch-all group).
        update_match = memory.detect_update_memory_command(text)
        if update_match:
            topic_query, new_text = update_match
            status, entry = memory.update_memory_by_topic(topic_query, new_text)
            log(
                f"request_id={request_id} - explicit memory: update topic={topic_query!r} -> status={status}",
                "planner_bridge",
            )
            if status == "updated":
                return (
                    f'The user said: "{text}". You just updated an existing long-term memory to: '
                    f"\"{entry['text']}\". Confirm this briefly and naturally."
                )
            if status == "ambiguous":
                return (
                    f'The user said: "{text}". Multiple existing memories could match "{topic_query}" - '
                    "you did NOT update anything (avoiding a guess that could destroy the wrong memory). "
                    "Ask them, briefly, to be more specific about which one they mean."
                )
            return (
                f'The user said: "{text}". They asked you to update a memory about "{topic_query}", but '
                "nothing in long-term memory matched that topic - tell them honestly you didn't find "
                "anything like that saved, so nothing was changed."
            )

        # Memory Intelligence sprint - optional Step 14 commands, checked
        # BEFORE delete-by-id/delete-by-topic below: both are whole-message
        # anchored patterns ("Memory ini penting." / "Lupakan memory ini.")
        # that would otherwise partially overlap with the more general
        # delete-by-topic catch-all ("hapus memory ini" -> topic_query="ini").
        # Both act on "the most-recently touched memory" (see
        # `mark_last_memory_important`/`forget_last_memory` docstrings in
        # luno/memory.py for why that's the safest deterministic target).
        if memory.detect_mark_important_command(text):
            entry = memory.mark_last_memory_important()
            log(
                f"request_id={request_id} - explicit memory: mark important -> {'ok' if entry else 'no memory yet'}",
                "planner_bridge",
            )
            if entry:
                return (
                    f'The user said: "{text}". You just marked their most recent long-term memory '
                    f"(\"{entry['text']}\") as important/permanent. Confirm this briefly."
                )
            return (
                f'The user said: "{text}", asking you to mark a memory as important, but there is no '
                "long-term memory saved yet - tell them honestly there's nothing to mark."
            )

        if memory.detect_forget_last_memory_command(text):
            removed_text = memory.forget_last_memory()
            log(
                f"request_id={request_id} - explicit memory: forget last memory -> removed={bool(removed_text)}",
                "planner_bridge",
            )
            if removed_text:
                return (
                    f'The user said: "{text}". You just deleted their most recent long-term memory: '
                    f'"{removed_text}". Confirm this briefly.'
                )
            return (
                f'The user said: "{text}", asking you to forget the last memory, but there is no '
                "long-term memory saved yet - tell them honestly there's nothing to forget."
            )

        # Memory Learning & Feedback Loop sprint - explicit "memory ini
        # berguna/tidak berguna/benar/salah" commands, checked alongside
        # the mark-important/forget-last commands directly above (same
        # "most-recently-touched memory" target, same anchored-whole-
        # message discipline). These are the EXPLICIT counterpart to the
        # conversational "iya benar"/"itu salah" feedback handled
        # separately by `_handle_memory_feedback_command()` (session-target-
        # based, checked later in `_handle_utterance()` - see that
        # method's own docstring for why the two need different target
        # resolution).
        if memory.detect_mark_memory_useful_command(text) or memory.detect_mark_memory_correct_command(text):
            entry = memory.mark_last_memory_useful() if memory.detect_mark_memory_useful_command(text) else memory.mark_last_memory_correct()
            log(
                f"request_id={request_id} - explicit memory: positive feedback -> {'ok' if entry else 'no memory yet'}",
                "planner_bridge",
            )
            if entry:
                # Memory Evaluation & Self-Calibration sprint (Step 3/8) -
                # every place feedback is applied also records the
                # (purely observational) feedback-event count and
                # recalibrates `evaluation_score` from the now-updated
                # evidence, synchronously, in the same turn - never a
                # background job (see `calibrate_memory()`'s own
                # docstring).
                # Memory Outcome Telemetry sprint (Step 7) - an explicit
                # mark-useful/correct command is itself a `"positive"`
                # outcome (in fact a HIGHER-priority one than a bare
                # conversational confirmation - Step 6's own priority
                # list), so it earns the same retrieval-evidence bump a
                # conversational positive outcome does.
                memory.record_outcome_evidence(entry["id"], "positive")
                memory.record_feedback_event(entry["id"])
                memory.calibrate_memory(entry["id"])
                return (
                    f'The user said: "{text}". You just recorded positive feedback on their most recent '
                    f"long-term memory (\"{entry['text']}\"). Confirm this briefly."
                )
            return (
                f'The user said: "{text}", giving positive feedback on a memory, but there is no '
                "long-term memory saved yet - tell them honestly there's nothing to give feedback on."
            )

        if memory.detect_mark_memory_not_useful_command(text) or memory.detect_mark_memory_incorrect_command(text):
            entry = memory.mark_last_memory_not_useful() if memory.detect_mark_memory_not_useful_command(text) else memory.mark_last_memory_incorrect()
            log(
                f"request_id={request_id} - explicit memory: negative feedback -> {'ok' if entry else 'no memory yet'}",
                "planner_bridge",
            )
            if entry:
                # Memory Evaluation & Self-Calibration sprint (Step 3/8) -
                # same synchronous record-then-recalibrate pattern as the
                # positive branch above.
                # Memory Outcome Telemetry sprint (Step 7) - mirror of the
                # positive branch above.
                memory.record_outcome_evidence(entry["id"], "negative")
                memory.record_feedback_event(entry["id"])
                memory.calibrate_memory(entry["id"])
                return (
                    f'The user said: "{text}". You just recorded negative feedback on their most recent '
                    f"long-term memory (\"{entry['text']}\"). You did NOT delete or change it - confirm "
                    "this briefly and, if it matters, ask what the correct value is."
                )
            return (
                f'The user said: "{text}", giving negative feedback on a memory, but there is no '
                "long-term memory saved yet - tell them honestly there's nothing to give feedback on."
            )

        delete_id = memory.detect_delete_memory_by_id_command(text)
        if delete_id:
            removed_text = memory.delete_memory_by_id(delete_id)
            log(
                f"request_id={request_id} - explicit memory: delete id={delete_id!r} -> removed={bool(removed_text)}",
                "planner_bridge",
            )
            if removed_text:
                return (
                    f'The user said: "{text}". You just deleted this from long-term memory: '
                    f'"{removed_text}". Confirm this briefly.'
                )
            return (
                f'The user said: "{text}". They asked you to delete memory id "{delete_id}", but no memory '
                "with that id exists - tell them honestly you couldn't find it, so nothing was deleted."
            )

        delete_topic = memory.detect_delete_memory_by_topic_command(text)
        if delete_topic:
            # Topic-based DELETE reuses the existing substring-based
            # `remove_memory()` (same one `detect_forget_fact_command`'s
            # handling below already calls) rather than a second deletion
            # mechanism - "hapus memory tentang X" and "lupakan X" share
            # one real removal path.
            removed = memory.remove_memory(delete_topic.lower())
            log(
                f"request_id={request_id} - explicit memory: delete topic={delete_topic!r} -> removed={removed}",
                "planner_bridge",
            )
            if removed:
                return (
                    f'The user said: "{text}". You just deleted this from long-term memory: '
                    f"{'; '.join(removed)}. Confirm this briefly."
                )
            return (
                f'The user said: "{text}". They asked you to delete a memory about "{delete_topic}", but '
                "nothing in long-term memory matched it - tell them honestly you didn't find anything "
                "like that saved."
            )

        # Checked before detect_remember_command below - matches
        # luno/memory.py's own documented ordering requirement (a general
        # "forget everything" phrase must never be captured above as a
        # literal fact name to delete).
        forget_query = memory.detect_forget_fact_command(text)
        if forget_query:
            removed = memory.remove_memory(forget_query.lower())
            log(f"request_id={request_id} - explicit memory: forget {forget_query!r} -> removed={removed}", "planner_bridge")
            if removed:
                return (
                    f'The user said: "{text}". You just deleted this from long-term memory: '
                    f"{'; '.join(removed)}. Confirm this briefly."
                )
            return (
                f'The user said: "{text}". They asked you to forget something, but nothing in '
                "long-term memory matched it - tell them honestly you didn't find anything like that saved."
            )

        remember_text = memory.detect_remember_command(text)
        if remember_text:
            entry = memory.add_memory(remember_text)
            log(
                f"request_id={request_id} - explicit memory: remember {remember_text!r} -> "
                f"{'saved' if entry else 'duplicate/skipped'}", "planner_bridge",
            )
            if entry:
                return (
                    f'The user said: "{text}". You just saved this to long-term memory: '
                    f"\"{entry['text']}\". Confirm this briefly and naturally - don't just recite it back verbatim."
                )
            return (
                f'The user said: "{text}". They asked you to remember something you already knew - '
                "let them know you've already got that noted, briefly."
            )

        return None

    # -- memory learning & feedback loop ---------------------------------------

    def _handle_memory_feedback_command(self, text: str, request_id: str, conversation_id: Optional[str]) -> Optional[str]:
        """Memory Learning & Feedback Loop sprint - CONVERSATIONAL feedback
        ("iya benar" / "itu salah" / "yang tadi salah, sekarang X"), as
        opposed to the explicit "memory ini berguna/salah" commands handled
        alongside every other meta memory command in
        `_handle_explicit_memory_command()` above. Checked from
        `_handle_utterance()` ONLY after the browser/environmental-intent/
        routing pending-confirmation checks have already had their chance
        and found nothing pending for this turn - see that call site's own
        comment for why this ordering matters (a real pending "iya"-shaped
        confirmation for one of those flows must never be stolen by this
        method).

        Target resolution (Section 13): the session's single, most
        recently surfaced manual-memory id
        (`self._session_feedback_target`, maintained by
        `_update_session_feedback_target()` below) - NOT
        `memory._most_recently_touched_memory()` (a global "last memory
        touched by ANY means" helper the explicit "memory ini ..."
        commands use instead; conflating the two would let an unrelated
        dashboard edit or a different conversation's save become the
        silent target of THIS conversation's "iya benar" reply). If no
        target is set (nothing was surfaced last turn, or more than one
        candidate was surfaced so the target was cleared as ambiguous),
        this method does nothing and returns `None` - Section 6/7's own
        explicit "jika target ambiguous: jangan modify memory" - the turn
        falls through to ordinary planning exactly as if this method
        didn't exist.

        Returns a system_note describing what was ACTUALLY done (same
        honesty convention as `_handle_explicit_memory_command()`), or
        `None` if `text` wasn't a recognized feedback shape at all, or a
        recognized shape had no valid target.

        Memory Outcome Telemetry & Closed-Loop Learning sprint: dispatch
        is now driven by `memory.classify_context_outcome(text)` - THE
        single, canonical source of "what happened this turn" (Step 6:
        "hubungkan function yang sudah ada ke actual production
        lifecycle" - this is that connection; previously this method
        independently re-derived the same classification via three
        separate `detect_*` calls with no shared, testable, reusable
        label). The actual dispatch behavior below is otherwise
        unchanged from before this sprint - same target resolution, same
        "no target -> no mutation" safety, same correction/positive/
        negative bodies - this sprint only added the `outcome ==` framing
        plus, per Step 7's evidence-mapping table, one new
        `memory.record_outcome_evidence()` call in the positive/negative
        branches (bumping `retrieval_success_count`/`retrieval_miss_count`
        as ADDITIONAL evidence alongside the pre-existing
        `usefulness_score`/`*_feedback_count` mutation `apply_positive_feedback()`/
        `apply_negative_feedback()` already perform - see that function's
        own docstring for why these are deliberately two separate calls,
        not one combined one). `outcome in ("neutral", "unknown")` falls
        through to the final `return None` with NO mutation of any kind -
        Step 7's own explicit "neutral -> no strong evidence change" /
        "unknown -> no evidence mutation"."""
        key = conversation_id or self._ENV_CONFIRMATION_KEY
        target_id = self._session_feedback_target.get(key)
        # Memory Decision Quality & Adaptive Retrieval sprint - the query
        # context category captured (by `_update_session_feedback_target()`)
        # at the moment `target_id` was originally surfaced, NOT this
        # turn's own text (which is often a context-less "iya benar"/
        # "itu salah" with no retrieval signal of its own). `None` when
        # unavailable (e.g. a pre-sprint session, or the target was set
        # with no query text) - every `record_outcome_evidence()` call
        # below already treats a falsy `context_category` as "skip the
        # context-scoped bump, keep the global one", so this is fully
        # additive.
        context_category = self._session_feedback_context.get(key)
        outcome = memory.classify_context_outcome(text)

        # Correction feedback (Section 8) - checked FIRST by
        # `classify_context_outcome()`'s own priority order, since it is a
        # more specific case of "that's wrong" that also supplies a
        # replacement value. Reuses the EXISTING `update_memory()`
        # correction/history path (Section 8's own "gunakan sistem
        # correction yang sudah ada. Jangan membuat correction engine
        # baru.") - never a second update mechanism.
        if outcome == "correction":
            correction_text = memory.detect_memory_feedback_correction(text)
            if correction_text is None:
                # Should not normally happen (`classify_context_outcome()`
                # and `detect_memory_feedback_correction()` share the same
                # regex) - fail safe if they ever disagree: no captured
                # replacement text, no mutation.
                return None
            if not target_id:
                log(f"request_id={request_id} - memory feedback: correction with no session target (ignored)", "planner_bridge")
                return None
            entry = memory.get_memory(target_id)
            if entry is None:
                self._session_feedback_target.pop(key, None)
                self._session_feedback_context.pop(key, None)
                return None
            updated = memory.update_memory(target_id, correction_text, reason="correction")
            # The OLD wording was just disputed by the user (that's what
            # "yang tadi salah" means) - recording a negative feedback
            # event on top of the correction keeps the feedback metadata
            # truthful (Section 8's "usefulness/feedback metadata ikut
            # diperbarui secara truthful"), without this being a second
            # mutation path: `apply_negative_feedback()` only ever touches
            # `usefulness_score`/`negative_feedback_count`, never `text`/
            # `history` (those were already correctly updated by
            # `update_memory()` just above). Step 7's evidence-mapping
            # table deliberately does NOT list a `retrieval_success_count`/
            # `retrieval_miss_count` bump for `correction` -
            # `correction_count` (bumped inside `update_memory()` itself)
            # is that outcome's own, sufficient evidence.
            if updated is not None:
                memory.apply_negative_feedback(target_id, reason="user_correction")
                memory.record_feedback_event(target_id)
                memory.calibrate_memory(target_id)
            self._session_feedback_target.pop(key, None)
            self._session_feedback_context.pop(key, None)
            log(
                f"request_id={request_id} - memory feedback: outcome=correction target={target_id!r} -> "
                f"{'updated' if updated else 'not_found'}", "planner_bridge",
            )
            if updated is None:
                return None
            return (
                f'The user just corrected a memory you referenced. It now says: "{updated["text"]}". '
                "The old wording was preserved in its history, not deleted. Confirm this briefly and naturally."
            )

        if outcome == "negative":
            if not target_id:
                log(f"request_id={request_id} - memory feedback: outcome=negative with no session target (ignored)", "planner_bridge")
                return None
            entry = memory.apply_negative_feedback(target_id, reason="user_disputed")
            self._session_feedback_target.pop(key, None)
            self._session_feedback_context.pop(key, None)
            log(f"request_id={request_id} - memory feedback: outcome=negative target={target_id!r} -> {'ok' if entry else 'not_found'}", "planner_bridge")
            if entry is None:
                return None
            memory.record_outcome_evidence(target_id, "negative", context_category=context_category)
            memory.record_feedback_event(target_id)
            memory.calibrate_memory(target_id)
            return (
                f'The user just said a memory you referenced ("{entry["text"]}") is wrong or no longer true. '
                "You did NOT delete or change it - acknowledge honestly, and ask them what the correct "
                "current value is if it matters. Do not claim you fixed anything."
            )

        if outcome == "positive":
            if not target_id:
                log(f"request_id={request_id} - memory feedback: outcome=positive with no session target (ignored)", "planner_bridge")
                return None
            entry = memory.apply_positive_feedback(target_id, reason="user_confirmed")
            self._session_feedback_target.pop(key, None)
            self._session_feedback_context.pop(key, None)
            log(f"request_id={request_id} - memory feedback: outcome=positive target={target_id!r} -> {'ok' if entry else 'not_found'}", "planner_bridge")
            if entry is None:
                return None
            memory.record_outcome_evidence(target_id, "positive", context_category=context_category)
            memory.record_feedback_event(target_id)
            memory.calibrate_memory(target_id)
            return (
                f'The user just confirmed a memory you referenced ("{entry["text"]}") was correct/useful. '
                "Acknowledge briefly and naturally - don't repeat the fact back verbatim unless it flows naturally."
            )

        # outcome in ("neutral", "unknown") - Step 7's own explicit rule:
        # no evidence mutation of any kind. Silence, small talk, or an
        # unrecognized reply is NEVER treated as positive (hard
        # constraint #10) and never nudges any memory's evidence.
        return None

    def _update_session_feedback_target(self, conversation_id: Optional[str], relevant_memories: List[Any],
                                         query_text: Optional[str] = None) -> None:
        """Memory Learning & Feedback Loop sprint (Section 13) - recomputes
        THIS conversation's feedback target from THIS turn's already-
        computed `relevant_memories_early` (no second retrieval pass -
        same "reuse this turn's already-computed result" discipline
        `memory_context.assemble_context()`'s own
        `precomputed_relevant_memories` parameter already established).
        Called ONCE per turn, AFTER `_handle_memory_feedback_command()`
        above has already had a chance to read the PREVIOUS turn's target
        - see the call site in `_handle_utterance()` for why this ordering
        matters (reading and writing the same turn would make a memory
        feedback command target itself).

        Deliberately conservative: exactly ONE distinct manual-memory id
        surfaced this turn -> that becomes the new target (unambiguous,
        satisfies Section 13's own "tidak ambigu"). Zero or more than one
        -> the target is cleared entirely, never left stale from an
        earlier turn (Section 13's own "reset setelah relevan" - "relevan"
        here reads as "once this turn's own relevance picture is known,
        the target reflects ONLY that", not "only after being consumed by
        feedback") - a later "iya benar" with no clear single memory just
        surfaced correctly finds no target and does nothing, rather than
        risking a guess about which of several, or which stale, memory
        the user means."""
        key = conversation_id or self._ENV_CONFIRMATION_KEY
        manual_ids = set()
        for rm in (relevant_memories or []):
            if getattr(rm, "source", None) != "manual_memory":
                continue
            raw = getattr(rm, "raw", None)
            if isinstance(raw, dict) and raw.get("id"):
                manual_ids.add(raw["id"])
        if len(manual_ids) == 1:
            self._session_feedback_target[key] = next(iter(manual_ids))
            # Memory Decision Quality & Adaptive Retrieval sprint - kept
            # in lockstep with the target above: set together (using
            # THIS turn's own query text, the same one that surfaced the
            # memory), popped together everywhere the target is popped.
            if query_text:
                self._session_feedback_context[key] = memory.classify_query_context_category(query_text)
            else:
                self._session_feedback_context.pop(key, None)
            while len(self._session_feedback_target) > self._session_feedback_target_max:
                oldest = next(iter(self._session_feedback_target))
                self._session_feedback_target.pop(oldest, None)
                self._session_feedback_context.pop(oldest, None)
        else:
            self._session_feedback_target.pop(key, None)
            self._session_feedback_context.pop(key, None)

    def get_voice_output_mode(self, conversation_id: Optional[str]) -> str:
        """Voice Output Mode sprint - returns this conversation's current
        sticky mode (`"ALL"`/`"SHORT"`), or `DEFAULT_VOICE_OUTPUT_MODE`
        ("SHORT") when `conversation_id` is falsy or has no entry yet -
        i.e. every conversation that has never touched this feature sees
        exactly the pre-sprint default. Read-only, no side effects - safe
        to call from a dashboard collector, a test, or `_handle_utterance()`
        itself without perturbing state."""
        if not conversation_id:
            return DEFAULT_VOICE_OUTPUT_MODE
        return self._voice_output_mode.get(conversation_id, DEFAULT_VOICE_OUTPUT_MODE)

    def set_voice_output_mode(self, conversation_id: Optional[str], mode: str) -> str:
        """Voice Output Mode sprint (Phase 4) - the minimal internal
        mechanism Luno (or a test, or a future dashboard control) can
        call to switch a conversation's sticky voice output mode at
        runtime, no restart required. `mode` is resolved through
        `resolve_voice_output_mode()` (never raises - an invalid value
        silently falls back to `"SHORT"`, logged here so the fallback is
        never silent from an operator's point of view). No
        `conversation_id` -> a no-op that still returns the resolved
        default, mirroring every other per-conversation dict in this
        class (`_update_depth_preference()`'s own "no conversation_id, no
        meaningful place to store it" guard) - there is deliberately no
        global fallback slot a conversation-less caller could pollute.

        Takes effect starting the NEXT turn this conversation_id is seen
        in `_handle_utterance()` (which reads the OLD value into a local
        BEFORE calling this - see that method's own call site) - never
        retroactively changes how a reply already in flight is spoken,
        per the brief's own explicit "berlaku untuk turn berikutnya"
        requirement."""
        resolved = resolve_voice_output_mode(mode)
        if not is_valid_voice_output_mode(mode):
            log(f"conversation_id={conversation_id} - invalid voice output mode {mode!r} requested, falling back to {resolved!r}", "planner_bridge")
        if not conversation_id:
            return resolved
        previous = self._voice_output_mode.get(conversation_id, DEFAULT_VOICE_OUTPUT_MODE)
        self._voice_output_mode[conversation_id] = resolved
        while len(self._voice_output_mode) > self._voice_output_mode_max:
            oldest = next(iter(self._voice_output_mode))
            self._voice_output_mode.pop(oldest, None)
        if previous != resolved:
            log(f"conversation_id={conversation_id} - voice output mode changed: {previous} -> {resolved} (applies next turn)", "planner_bridge")
        return resolved

    def _update_depth_preference(self, conversation_id: Optional[str], text: str) -> None:
        """Adaptive Response Depth Learning sprint - classifies `text`
        (THIS turn's own utterance) for DEPTH feedback
        (`luno.response_policy.detect_depth_feedback()`) and, if it is
        one, folds it into this conversation's bounded
        `DepthPreference` (`luno.response_policy.apply_depth_feedback()`).
        A no-op (leaves `_depth_preference` completely untouched) for
        anything that isn't depth feedback - `detect_depth_feedback()`
        returning `None` covers silence, ordinary conversation, content-
        only feedback ("itu salah" - handled entirely separately by
        `_handle_memory_feedback_command()`/`luno.memory`, never touched
        here), and explicit depth REQUESTS for the current turn (those
        already went through `compute_response_policy()`'s own explicit-
        phrase short-circuit above and need no separate preference
        nudge).

        No `conversation_id` -> no-op entirely (mirrors every other
        conversation-scoped dict in this class - `_response_depth_context`/
        `_session_feedback_target`/`_last_turn_trace` all have this same
        guard) - there is no meaningful place to store a preference
        without a conversation to scope it to, and a global,
        conversation-less preference would risk leaking one user's
        feedback into an unrelated later exchange (the sprint's own hard
        "tidak boleh membuat preference global" rule)."""
        if not conversation_id:
            return
        feedback = detect_depth_feedback(text)
        if feedback is None:
            return
        current = self._depth_preference.get(conversation_id)
        if current is None and self._depth_preference_startup_bias:
            # Persistent Adaptive Response Depth Preference sprint - this
            # conversation's FIRST real feedback event starts from the
            # frozen cross-session baseline (see `__init__`) instead of
            # true neutral, so the very first "kepanjangan" in a new
            # conversation blends with what was already learned rather
            # than discarding it. Only affects the seed value passed into
            # `apply_depth_feedback()` below - still a completely normal,
            # single, real feedback-triggered dict insertion, so
            # `_depth_preference`'s own "only real feedback ever creates
            # an entry" invariant is untouched.
            current = DepthPreference(bias=self._depth_preference_startup_bias)
        updated = apply_depth_feedback(current, feedback)
        self._depth_preference[conversation_id] = updated
        while len(self._depth_preference) > self._depth_preference_max:
            oldest = next(iter(self._depth_preference))
            self._depth_preference.pop(oldest, None)

        # Persistent Adaptive Response Depth Preference sprint - threshold-
        # gated merge into the cross-session baseline. `should_persist()`
        # only returns True once every `PERSIST_MIN_SAMPLES` (3) LOCAL
        # feedback events *within this conversation* - "do NOT save after
        # every turn" from the brief - and `merge_conversation_into_persistent()`
        # is a conservative weighted blend, never an overwrite, so one
        # conversation can only nudge the baseline, never instantly replace
        # it. This is the PRIMARY persistence trigger (reliable - fires from
        # `_handle_utterance()`, which every routed "user_utterance" event
        # reaches); `_on_conversation_ended()` below performs a secondary,
        # best-effort final merge, since that hook is not currently wired
        # into real event routing (see
        # docs/change_impact/persistent_adaptive_response_depth.md's
        # "Known limitations" section for the full explanation of this
        # discovered gap). A persistence failure here must never break the
        # turn that triggered it - `DepthPreferenceStore.save()` already
        # swallows its own exceptions and returns False.
        if should_persist(updated.feedback_count):
            with self._persistent_depth_preference_lock:
                self._persistent_depth_preference = merge_conversation_into_persistent(
                    self._persistent_depth_preference, updated.bias,
                )
                DepthPreferenceStore.save(self._persistent_depth_preference)

    # -- manual "summarize this session" command ------------------------------

    def _handle_manual_summarize_command(self, text: str, request_id: str) -> Optional[str]:
        """`luno/memory.py`'s `is_manual_summarize_command()` ("rangkum
        obrolan ini" / "summarize this conversation") - manual trigger
        for the SAME `summarize_and_archive_session()` call
        `_on_conversation_ended` fires automatically at the end of a
        wake-word conversation. Checked alongside
        `_handle_explicit_memory_command` (before planning, before the
        device-intent classifier - same reasoning: this is a meta
        conversation command with no IntentParser vocabulary at all).

        Fails closed and HONESTLY when no real client is wired (never
        pretends a summary was saved when it wasn't - see
        `session_summary_client`'s own docstring for why this can be
        `None`), matching this whole project's "don't claim success for
        something that didn't happen" convention."""
        if not memory.is_manual_summarize_command(text.lower()):
            return None

        if self.session_summary_client is None:
            log(f"request_id={request_id} - manual summarize requested but no real LLM client is wired (staying mock)", "planner_bridge")
            return (
                f'The user said: "{text}", asking you to summarize this conversation. Tell them honestly '
                "you can't do that right now (no real LLM connection is configured for summaries)."
            )

        try:
            summary = memory.summarize_and_archive_session(self.session_summary_client, model=self.session_summary_model)
        except Exception as ex:
            log(f"request_id={request_id} - manual summarize failed: {ex}", "planner_bridge")
            return (
                f'The user said: "{text}", asking you to summarize this conversation, but it failed '
                "internally. Tell them honestly it didn't work."
            )

        if summary is None:
            return (
                f'The user said: "{text}", asking you to summarize this conversation, but there isn\'t '
                "enough conversation yet to summarize. Tell them honestly."
            )
        return (
            f'The user said: "{text}". You just summarized and archived this conversation: "{summary}". '
            "Confirm this briefly."
        )

    # -- entry point: a user utterance needs planning + a spoken reply ------

    def on_event(self, event: Event) -> None:
        if self._event_bus is None:
            return
        if event.type == "user_utterance":
            # Conversation_end Race Safety sprint - check-and-increment
            # under the SAME lock `_wait_for_turn_to_settle()` uses to
            # mark a conversation as ending, so the two can never
            # interleave unsafely: either this runs first (the turn is
            # counted, so a concurrent `_on_conversation_ended()` will
            # wait for it) or the ending-mark runs first (this utterance
            # is refused, never silently started only to be ignored by
            # a cleanup that already ran). A conversation with no
            # `conversation_id` at all is never tracked (nothing for
            # `_on_conversation_ended()` to key state on either), so it
            # always proceeds exactly as before this sprint.
            conversation_id = event.get("conversation_id")
            if conversation_id:
                with self._active_turn_lock:
                    if conversation_id in self._ending_conversations:
                        log(
                            f"user_utterance dropped - conversation {conversation_id} is already ending",
                            "planner_bridge",
                        )
                        return
                    self._active_turn_counts[conversation_id] = self._active_turn_counts.get(conversation_id, 0) + 1
            threading.Thread(target=self._run_utterance_turn_safely, args=(event,), daemon=True, name="luno-planner-turn").start()
        elif event.type == "assistant_response":
            self._on_assistant_response(event)
        elif event.type == "llm_cancelled":
            # Turn never got a real reply - drop its pending entry
            # rather than ever recording a fabricated/empty turn into
            # session_log (same "never claim something happened that
            # didn't" convention as everywhere else in this bridge).
            request_id = event.get("request_id")
            if request_id:
                self._pending_turns.pop(request_id, None)
        elif event.type == "conversation_ended":
            self._on_conversation_ended(event)

    def _remember_pending_turn(self, request_id: str, text: str, conversation_id: Optional[str] = None) -> None:
        self._pending_turns[request_id] = (text, conversation_id)
        while len(self._pending_turns) > self._pending_turns_max:
            oldest = next(iter(self._pending_turns))
            self._pending_turns.pop(oldest, None)

    def _on_assistant_response(self, event: Event) -> None:
        """Pairs this reply up with the user text (and conversation_id)
        recorded for the same request_id (see the end of
        `_handle_utterance`) and feeds both into `luno.memory.
        remember_turn()` - the ONLY thing that actually populates
        `session_log`, which `summarize_and_archive_session()` (see
        `_on_conversation_ended` below) reads from. Silently no-ops for
        any request_id this bridge didn't itself originate (there
        currently are none, but failing closed here costs nothing).

        Also (Sprint 4 - Memory Continuity) updates THIS conversation's
        `_active_topic` snapshot from the SAME turn's user text + finalized
        reply text - the only place in this class both are simultaneously
        available together for the same turn, and the same class that owns
        `_active_topic`/`_last_topic_terms`. See `memory_context.
        update_active_topic()`'s own docstring for the replace-vs-preserve
        rule this relies on."""
        request_id = event.get("request_id")
        if not request_id:
            return
        pending = self._pending_turns.pop(request_id, None)
        if pending is None:
            return
        user_text, conversation_id = pending
        reply_text = event.get("text", "")
        try:
            memory.remember_turn(user_text, reply_text)
        except Exception as ex:
            log(f"request_id={request_id} - remember_turn failed (this turn won't be in the next session summary): {ex}", "planner_bridge")

        try:
            _topic_key = conversation_id or self._ENV_CONFIRMATION_KEY
            # `is_pure_reference_followup()`, NOT `needs_topic_context()` -
            # deliberately the stricter, narrower classifier here (see that
            # function's own docstring): a "comparison"/"negation" turn
            # ("Kalau WLED gimana?", "kalau tanpa MQTT?") carries its own
            # real entity and must REPLACE the snapshot (Phase 6 branch
            # switching depends on this), even though `needs_topic_context()`
            # would also say `True` for it (a DIFFERENT question - Phase 4's
            # "does THIS turn's own retrieval benefit from expansion").
            #
            # Context-Aware Comparison Topic Preservation sprint - fetch
            # the EXISTING snapshot BEFORE classifying (order swapped from
            # before this sprint), so its own terms can be handed to
            # `is_pure_reference_followup()` - a comparison turn whose own
            # residual entity is ALREADY part of the current active topic
            # (e.g. "Kalau mikrofonnya gimana?" when INMP441/mic is already
            # the active topic) now preserves instead of replacing; a
            # comparison turn naming something genuinely new (e.g. "Kalau
            # Bluetooth-nya gimana?") still replaces exactly as before -
            # see `is_pure_reference_followup()`'s own updated docstring.
            existing_snapshot = self._active_topic.get(_topic_key)
            is_followup = memory.is_pure_reference_followup(
                user_text,
                active_topic_terms=existing_snapshot.terms if existing_snapshot else None,
            )
            # Conversation Reference Resolution sprint (Sprint 38) - a
            # THIRD update behavior alongside replace/preserve, for
            # REPAIR_REFERENCE ("eh maksudku ESP32-S3") and
            # ATTRIBUTE_REFERENCE ("kalau yang wireless?") turns: MERGE
            # the new term(s) into the existing snapshot rather than
            # replacing it (would lose the parent topic) or preserving it
            # unchanged (would silently drop the correction/attribute
            # itself). See `memory.is_merge_reference_followup()`'s own
            # docstring. `is_followup`/`is_merge` are mutually exclusive
            # by construction (each turn classifies to exactly one
            # `reference_type`), and `update_active_topic()`/
            # `update_topic_history()` both give `is_merge` precedence if
            # a caller somehow set both.
            # Sprint 44 (Entity & Concept Continuity, Phase 2) - an
            # ADDITIVE second merge trigger alongside `is_merge_
            # reference_followup()`: a turn `classify_reference_type()`
            # still calls `"unknown"` (unchanged, preserves every
            # existing classifier test/precedent - see `memory_context.
            # is_sparse_unknown_followup()`'s own docstring) but which
            # carries almost no standalone content of its own (a genuine
            # single-real-word elliptical fragment, e.g. "Kalau
            # koneksinya?") is merge-worthy for the SAME reason an
            # ATTRIBUTE_REFERENCE turn already is - too sparse to
            # legitimately replace the established entity identity, but
            # not classifier-pattern-matched as a reference either. See
            # that function's own docstring for the full live-reproduced
            # entity-erosion bug this closes.
            # Sprint 47 (Semantic Entity Memory & Reference Graph) - a
            # THIRD, additive merge trigger: `memory_context.is_
            # demonstrative_anchored_followup()` (see its own docstring)
            # closes a differently-shaped entity-erosion gap `is_sparse_
            # unknown_followup()`'s `<= 1`-token bound does not cover -
            # an "unknown"-classified turn whose own 2nd word is a
            # mid-sentence demonstrative ("Board itu RAM-nya berapa?").
            is_merge = (
                memory.is_merge_reference_followup(user_text)
                or memory_context.is_sparse_unknown_followup(user_text)
                or memory_context.is_demonstrative_anchored_followup(user_text)
            )
            # Sprint 40 (Memory Confidence & Conflict Resolution) - reuses
            # the SAME public detector `_handle_utterance()` already calls
            # (above, to decide whether to `add_memory()`) rather than
            # re-deriving a second signal. Tells `update_active_topic()`/
            # `update_topic_history()` to suppress their new
            # `source_sentence` verbatim-quote field for this turn - an
            # explicit "ingat ..." command's fact is already fully owned
            # and rendered by the PERSISTENT `manual_memory` layer, so
            # quoting it AGAIN here would duplicate the same fact across
            # two independently rendered context blocks (proven via live
            # E2E reproduction - see `update_active_topic()`'s own
            # docstring for the full account).
            is_remember_command = bool(memory.detect_remember_command(user_text))
            self._active_topic[_topic_key] = memory_context.update_active_topic(
                existing_snapshot, user_text, reply_text, is_followup=is_followup, is_merge=is_merge,
                is_remember_command=is_remember_command,
            )
            while len(self._active_topic) > self._active_topic_max:
                oldest = next(iter(self._active_topic))
                self._active_topic.pop(oldest, None)
        except Exception as ex:
            log(f"request_id={request_id} - active-topic snapshot update failed (skipped): {ex}", "planner_bridge")
            # Defensive fallback (Sprint 38) - guarantees `is_followup`/
            # `is_merge` are always bound before the topic-history
            # try/except block below reads them, even if the exception
            # above happened before either was assigned.
            is_followup = False
            is_merge = False
            is_remember_command = False

        try:
            # Memory Topic Retention & Recall Reliability sprint - kept in
            # its own try/except, independent of the single-slot update
            # immediately above, so a failure in one can never affect the
            # other. Same `is_followup`/`is_merge` gates reused (not
            # recomputed) - a pure reference follow-up pushes nothing new
            # onto the history either, for the same reason
            # `update_active_topic()` doesn't replace on one.
            existing_history = self._topic_history.get(_topic_key)
            self._topic_history[_topic_key] = memory_context.update_topic_history(
                existing_history, user_text, reply_text, is_followup=is_followup, is_merge=is_merge,
                is_remember_command=is_remember_command,
            )
            while len(self._topic_history) > self._topic_history_max:
                oldest = next(iter(self._topic_history))
                self._topic_history.pop(oldest, None)
        except Exception as ex:
            log(f"request_id={request_id} - topic-history update failed (skipped): {ex}", "planner_bridge")

    def _on_conversation_ended(self, event: Event) -> None:
        """Fires when `SessionManagerModule` ends a wake-word conversation
        (inactivity timeout or manual sleep - see `luno/wake_session/
        manager.py`'s `ConversationEnded` publishes) - the natural
        per-conversation boundary for archiving a session summary, same
        role legacy `main.py`'s "Luno ditutup" moment played for the old
        single-session-per-process design. No-ops entirely (leaves
        session_log to keep accumulating) if no real client has been
        wired in - see `session_summary_client`'s own docstring."""
        session_id = event.get("session_id")
        reason = event.get("reason")
        # Conversation_end Race Safety sprint - the FIRST thing that
        # happens: mark `session_id` as ending (so `on_event()` refuses
        # any new "user_utterance" for it from this point on) and give
        # any turn already in flight for it a bounded chance to reach
        # its own "settled" point before anything below reads/clears
        # per-conversation state. See that method's own docstring for
        # the exact guarantee and the bounded-timeout fallback.
        self._wait_for_turn_to_settle(session_id)
        # Intelligent AI Routing Engine sprint - a new conversation must
        # never inherit a previous one's sticky reasoning-provider
        # affinity (see `luno/routing/affinity.py`). Safe even if
        # `session_id` was never used as this decision engine's own
        # `conversation_id` for a request (`.reset()` is a no-op for an
        # unknown key) and independent of `session_summary_client` below.
        self.decision_engine.affinity.reset(session_id)
        # Short-term device-context memory ("sekarang matikan") is
        # genuinely short-term - a brand new conversation must not
        # inherit "the light" from a conversation that ended minutes/
        # hours ago. `.pop(..., None)` is a safe no-op if this
        # conversation never actually set any device context (or
        # `session_id` was never used as a conversation_id here at all).
        self._pending_env_confirmations.pop(session_id, None)
        self._last_device_target.pop(session_id, None)
        # Sprint 57 (Contextual Home Assistant References) - the turn-
        # sequence counter backing device-context freshness must reset
        # in lockstep with `_last_device_target` itself, else a brand
        # new conversation reusing the same `session_id` would start
        # its device memory empty but its turn counter non-zero (never
        # unsafe - an empty memory has nothing to be "fresh" about -
        # but pop it anyway to keep the two dicts' lifecycles identical
        # and avoid unbounded growth of a key no longer in use).
        self._device_context_turn_seq.pop(session_id, None)
        # Memory Learning & Feedback Loop sprint - same "a brand new
        # conversation must not inherit state from one that ended minutes/
        # hours ago" reasoning as the two pops immediately above; a memory
        # surfaced in a conversation that already ended must never become
        # the silent target of a feedback reply in an unrelated, later
        # conversation (Section 13's own "tidak bocor antar user/session").
        self._session_feedback_target.pop(session_id, None)
        # Memory Decision Quality & Adaptive Retrieval sprint - kept in
        # lockstep with `_session_feedback_target` immediately above.
        self._session_feedback_context.pop(session_id, None)
        # Memory Retrieval & Decision Quality sprint (Phase 2 - Topic
        # Continuity) - same reasoning as `_session_feedback_target`/
        # `_session_feedback_context` immediately above: a topic snapshot
        # from a conversation that already ended must never influence a
        # later, unrelated conversation's retrieval ranking.
        self._last_topic_terms.pop(session_id, None)
        # Voice Output Mode sprint - same "a brand new conversation must
        # not inherit state from one that already ended" reasoning as
        # every other pop in this method. A fresh conversation always
        # starts at `DEFAULT_VOICE_OUTPUT_MODE` ("SHORT"), never at
        # whatever mode a prior, unrelated conversation happened to be
        # left in.
        self._voice_output_mode.pop(session_id, None)
        # Memory Continuity & Short Follow-up Reference Resolution sprint
        # (Sprint 4) - same reasoning as `_last_topic_terms` immediately
        # above: an active-topic snapshot from a conversation that already
        # ended must never anchor a short follow-up in a later, unrelated
        # conversation.
        self._active_topic.pop(session_id, None)
        # Memory Topic Retention & Recall Reliability sprint - same
        # reasoning as `_active_topic` immediately above, applied to the
        # bounded topic HISTORY list rather than the single slot.
        self._topic_history.pop(session_id, None)
        # Memory Outcome Telemetry & Closed-Loop Learning sprint - same
        # reasoning: a `MemoryTurnTrace` from a conversation that already
        # ended must never be consulted for a later, unrelated
        # conversation's outcome classification.
        self._last_turn_trace.pop(session_id, None)
        # Response Depth Policy sprint - same reasoning: a brand new
        # conversation must not inherit the previous conversation's
        # response-depth continuation score.
        self._response_depth_context.pop(session_id, None)
        # Persistent Adaptive Response Depth Preference sprint - a
        # best-effort FINAL merge into the cross-session baseline, done
        # here (before the pop immediately below discards the local
        # preference for good) regardless of the %3 `should_persist()`
        # threshold used by the per-turn PRIMARY trigger in
        # `_update_depth_preference()` - a conversation that accumulated
        # only 1 or 2 local feedback events (never crossing the threshold
        # mid-conversation) would otherwise lose that evidence entirely
        # once its local `DepthPreference` is popped. This is documented
        # as a SECONDARY path, not the primary one - the PRIMARY trigger
        # is still `should_persist()`, checked every turn from
        # `_handle_utterance()`. Conversation_ended Lifecycle Routing
        # sprint: `conversation_ended` IS now routed to this module in
        # both production (`luno/bootstrap/modules.py`) and this console
        # (`self.runtime.add_route("conversation_ended", "planner")`
        # above `__init__`'s route-wiring block) - this path is reachable
        # through the real Event Bus, not merely via direct test calls,
        # as of that sprint. See
        # docs/change_impact/conversation_ended_lifecycle_routing.md for
        # the full before/after trace. Only merges if this conversation
        # ever produced real feedback
        # (`feedback_count > 0`) - an untouched, still-neutral seeded
        # entry (see the seeding block above) must never itself be
        # written back as if it were evidence.
        try:
            ended_preference = self._depth_preference.get(session_id)
            if ended_preference is not None and ended_preference.feedback_count > 0:
                with self._persistent_depth_preference_lock:
                    self._persistent_depth_preference = merge_conversation_into_persistent(
                        self._persistent_depth_preference, ended_preference.bias,
                    )
                    DepthPreferenceStore.save(self._persistent_depth_preference)
            # Adaptive Response Depth Learning sprint - same reasoning: a
            # brand new conversation must not inherit the previous
            # conversation's adaptive depth-preference bias either.
            self._depth_preference.pop(session_id, None)
        finally:
            # Conversation_end Race Safety sprint - the race-sensitive
            # section (`_wait_for_turn_to_settle()` through the merge +
            # pop immediately above) is now complete, so a conversation
            # reusing this exact `session_id` (Phase 5 Case F - an
            # immediate new conversation) is no longer refused by
            # `on_event()`. Scoped to end HERE, not at the very bottom of
            # this method, so a brand-new conversation is never blocked
            # for the duration of the (slower, unrelated, possibly
            # network-bound) session-summary archiving below. `discard()`
            # (not `remove()`) so this is always safe even if
            # `_wait_for_turn_to_settle()` itself no-op'd (falsy
            # `session_id`) or this runs twice for any reason.
            if session_id:
                with self._active_turn_lock:
                    self._ending_conversations.discard(session_id)
        self._last_response_policy.pop(session_id, None)
        self.browser_permissions.clear(session_id)
        # Emotion Engine sprint - same "a brand new conversation must not
        # inherit state from one that ended minutes/hours ago" reasoning
        # as the two pops above (session-boundary decay, section 11/12
        # of that sprint's brief).
        self.emotion_tracker.reset()

        if self.session_summary_client is None:
            return
        try:
            summary = memory.summarize_and_archive_session(self.session_summary_client, model=self.session_summary_model)
        except Exception as ex:
            log(f"conversation_ended (session={session_id}, reason={reason}) - session summary failed: {ex}", "planner_bridge")
            return
        if summary:
            log(f"conversation_ended (session={session_id}, reason={reason}) - session summary saved: {summary!r}", "planner_bridge")

    def _run_utterance_turn_safely(self, event: Event) -> None:
        """Dashboard Turn-State Recovery fix - the single guaranteed
        terminal-lifecycle boundary for a conversational turn.

        `_handle_utterance()` below is dispatched onto a freshly-spawned
        `luno-planner-turn` daemon thread (see `on_event()` above) with no
        outer supervisor. That method already wraps most of its own
        individually risky steps in their own `try/except` ("a bug here
        must never break a turn" - memory retrieval, tool execution,
        adaptive-depth feedback, ...), but large stretches between those
        blocks are NOT individually guarded - `self.planner.create_plan()`
        and the final `self._event_bus.publish(NeedLLMResponse(...))` call
        among them. `SessionManagerModule` already transitioned the
        session to THINKING before this thread even starts
        (`_forward_to_conversation()` in `luno/wake_session/manager.py`,
        triggered by the `speech_recognized` that led here), and THINKING
        has no timeout anywhere in this codebase (see that file's own
        docstring - "a permanent post-reply deadlock"). Its only existing
        recovery path, `SessionManagerModule._handle_llm_failure()`, is
        keyed exclusively on `llm_error`/`llm_cancelled` - both published
        ONLY from inside the OpenRouter adapter's own already-well-guarded
        `_run_request()`. So if any exception escapes `_handle_utterance()`
        BEFORE it ever reaches `NeedLLMResponse` (proven live: a
        `ConnectionAbortedError`-style failure injected into
        `self.planner.create_plan()`), neither event ever fires, THINKING
        never clears, and the Dashboard's own busy-guard in
        `send_chat_message()` (`luno/dashboard/controls.py`) permanently
        rejects every subsequent ordinary command with "Luno is busy
        right now (state=thinking)".

        The fix is additive and reuses EXISTING plumbing rather than
        inventing a new event type, route, or state machine: on any
        escaped exception, this wrapper publishes the SAME `llm_error`
        event a real OpenRouter failure already publishes
        (`luno/adapters/openrouter.py::_publish_error()`). `llm_error` is
        already routed to `session_manager` (clears THINKING, matches the
        existing "llm failure - returning control to user" recovery) and
        to `barge_in` (`BargeInModule` already treats `llm_error` as "this
        turn is over, clear my own busy flag too" - see
        `luno/barge_in/manager.py`'s own `elif t in ("llm_error",
        "llm_cancelled")` branch) - zero new routing, zero new consumers,
        zero new architecture. Both existing handlers are already
        idempotent (`_handle_llm_failure()` only acts `if self.session.
        state == THINKING`), so this can never double-fire incorrectly
        even in the (today, impossible - the publish is the function's
        own last line) case of a future exception after `NeedLLMResponse`
        already published.

        A turn that completes normally is entirely unaffected: this
        wrapper's own `try` simply returns once `_handle_utterance()`
        does, exactly as calling it directly always did."""
        request_id = event.get("request_id") or event.event_id
        conversation_id = event.get("conversation_id")
        try:
            self._handle_utterance(event)
        except Exception as ex:
            log(
                f"request_id={request_id} - _handle_utterance raised an unhandled "
                f"exception ({type(ex).__name__}: {ex}) before the turn reached "
                "NeedLLMResponse - publishing llm_error so the session/Dashboard "
                "recover instead of staying stuck at Thinking forever.",
                "planner_bridge",
            )
            if self._event_bus is not None:
                self._event_bus.publish(Event(type="llm_error", data={
                    "request_id": request_id,
                    "conversation_id": conversation_id,
                    "model": None,
                    "error": str(ex),
                    "error_type": type(ex).__name__,
                    "retryable": False,
                    "source": "planner_bridge_unhandled_exception",
                }))

    def _handle_utterance(self, event: Event) -> None:
        text = event.get("text", "")
        request_id = event.get("request_id") or event.event_id
        conversation_id = event.get("conversation_id")

        # Sprint 57 (Contextual Home Assistant References) - stamp
        # THIS thread (this method runs on a fresh `luno-planner-turn`
        # thread per utterance, see `on_event`) with which conversation
        # it's processing, so `_tool_bridge_handler` - a single shared-
        # instance method with no `conversation_id` parameter of its
        # own - can later correlate a failed tool call back to the
        # right conversation's device memory via `_invalidate_device_
        # context_on_failure`. See `self._tool_bridge_local`'s own
        # docstring in `__init__` for why this is `threading.local()`
        # and not a bare instance attribute.
        self._tool_bridge_local.conversation_id = conversation_id

        # Voice Output Mode sprint (Phase 4-5) - read THIS conversation's
        # CURRENT sticky mode first (before any command below can change
        # it), so THIS turn's own `response_depth_assigned` publish (near
        # the bottom of this method) still reflects the mode this
        # conversation was ALREADY in - a just-uttered mode-switch
        # command only ever applies starting the NEXT turn (brief's own
        # explicit "berlaku untuk turn berikutnya" requirement), never
        # retroactively to the reply about to be generated for this one.
        voice_output_mode_for_this_turn = self.get_voice_output_mode(conversation_id)

        # Explicit voice-mode command detection - a small, fixed,
        # bilingual phrase list (`luno.voice_output_mode.
        # match_voice_output_mode_command()`), deliberately NOT a new
        # classifier/intent model (Phase 5's own "jangan membuat
        # classifier besar baru" + "prioritaskan explicit command").
        # `None` (the overwhelming common case - an ordinary utterance)
        # is a complete no-op here; every existing turn's behavior is
        # totally unaffected by this check even existing at all.
        voice_mode_command = match_voice_output_mode_command(text)
        if voice_mode_command is not None:
            self.set_voice_output_mode(conversation_id, voice_mode_command)

        # Sprint 5 - Smart Memory Injection: called straight from here,
        # synchronously - no nested background thread. `_handle_utterance`
        # itself already runs off the main event-pump thread (`on_event`
        # spawns a fresh `luno-planner-turn` thread per utterance, see
        # above), so calling `retrieve_memories()` inline here already
        # cannot block event delivery to any other module; nesting a
        # SECOND thread inside that one bought nothing but an extra
        # thread-per-turn. That extra thread was pure overhead, and it
        # turned out to be an active liability: this project's own
        # long-running test suites accumulate many lingering
        # daemon threads (heartbeat/tool-manager/event-bus workers,
        # ~20+ of them) across earlier scenarios, so measurably adding
        # yet one more live thread per turn increased GIL contention
        # enough to intermittently tip a timing-sensitive pre-existing
        # test over its budget. Real Vision Memory reads are cheap
        # (single-digit ms once cached, ~56ms worst case cold - see
        # `_ttl_cached` above) so paying that cost inline, once, is
        # strictly better than the threading overhead it replaced.
        try:
            relevant_memories_early = self.memory_retriever.retrieve_memories(text)
            # Memory & Voice Observability Dashboard sprint - Phase 1's
            # own "retrieval called?" question, answered honestly from
            # this SAME try/except's outcome (not a new check) - `True`
            # only when `retrieve_memories()` actually ran to completion
            # this turn.
            _retrieval_called = True
        except Exception as ex:
            log(f"request_id={request_id} - memory retrieval raised (skipped): {ex}", "planner_bridge")
            relevant_memories_early = []
            _retrieval_called = False

        # Memory Lifecycle & Maintenance sprint (Step 4) - usage tracking
        # only, never mutates WHAT a memory says, only bumps
        # retrieval_count/last_retrieved_at (and, conservatively, capped
        # importance reinforcement) for manual-memory entries that
        # actually survived relevance gating AND the retrieval budget
        # above - never for something that merely exists in the store.
        # Own try/except, same as every other note-producing call site in
        # this method - a usage-tracking bug must never break a turn.
        try:
            memory.record_memory_usage(relevant_memories_early)
        except Exception as ex:
            log(f"request_id={request_id} - memory usage tracking failed (skipped): {ex}", "planner_bridge")

        # Memory Retrieval & Decision Quality sprint - THIS turn's
        # deterministic query-intent classification
        # (`luno.memory.classify_query_intent()` - plain keyword/regex
        # matching, no LLM/embeddings/second tokenizer) and, for a
        # `continuation_of_topic`-classified turn, the PREVIOUS turn's
        # bounded topic-terms snapshot for this same conversation. Both
        # computed/read here, alongside the other early per-turn reads
        # above, and threaded into `memory_context.assemble_context()`
        # below purely as bounded, low-priority ranking tiebreakers -
        # never a second retrieval pass, never a relevance override (see
        # that function's own docstring). Own try/except, same "a bug
        # here must never break a turn" convention as every other
        # note-producing early read in this method - on failure, both
        # fall back to `None`, i.e. `assemble_context()` behaves exactly
        # as it did before this sprint for this turn.
        try:
            query_intent = memory.classify_query_intent(text)
        except Exception as ex:
            log(f"request_id={request_id} - query-intent classification raised (skipped): {ex}", "planner_bridge")
            query_intent = None
        _topic_key = conversation_id or self._ENV_CONFIRMATION_KEY
        previous_topic_terms = self._last_topic_terms.get(_topic_key) if query_intent == "continuation_of_topic" else None

        # Memory Continuity & Short Follow-up Reference Resolution sprint
        # (Sprint 4, Phase 2-4) - a SEPARATE, additive classifier from
        # `query_intent` above (see `luno.memory.classify_reference_type()`'s
        # own module comment for why: `query_intent`'s
        # `continuation_of_topic` value never fires for these phrases).
        # `reference_type`/`is_short_followup` are read again below, right
        # before `assemble_context()`, to decide whether to expand THIS
        # turn's retrieval query and/or offer an active-topic candidate -
        # both computed here, alongside every other early per-turn read in
        # this method, same "compute once, own try/except, never break the
        # turn" convention.
        try:
            reference_type = memory.classify_reference_type(text)
            is_short_followup = reference_type in memory.NEEDS_TOPIC_CONTEXT_TYPES
        except Exception as ex:
            log(f"request_id={request_id} - reference-type classification raised (skipped): {ex}", "planner_bridge")
            reference_type = "unknown"
            is_short_followup = False
        active_topic_snapshot = self._active_topic.get(_topic_key)

        # Sprint 50 (Runtime Observability, Test Logging & Real-World
        # Data Capture) - OBSERVABILITY ONLY: publishes the classification
        # this method already computed above onto the existing Event Bus,
        # exactly the same `self._event_bus.publish(Event(...))` pattern
        # used everywhere else in this class. Never the raw utterance text
        # (`text` itself is deliberately NOT included in `data` - only the
        # bounded classification labels, matching `MemoryTurnTrace`'s own
        # long-standing "never raw conversation text" privacy boundary).
        # A publish failure must never break a turn - own try/except, same
        # "a bug in telemetry can't break production" convention as every
        # other note-producing call site in this method.
        try:
            self._event_bus.publish(Event(type="memory_reference_classified", data={
                "request_id": request_id,
                "conversation_id": conversation_id,
                "reference_type": reference_type,
                "is_short_followup": is_short_followup,
                "query_intent": query_intent,
            }))
        except Exception:
            pass  # telemetry must never be able to break a turn

        # Emotion Engine sprint - estimate USER emotion from this turn's
        # own text (cheap regex/keyword matching, no I/O, no LLM call -
        # see luno/emotion_engine.py). Computed here, alongside the other
        # early per-turn reads above, so it's available for the bounded
        # prompt note appended near the end of this method (right before
        # the language/character-reminder block - see that append site's
        # own comment for why THIS ordering). Never allowed to affect
        # anything else this turn if it fails.
        try:
            self.emotion_tracker.observe(text)
        except Exception as ex:
            log(f"request_id={request_id} - emotion estimation raised (skipped): {ex}", "planner_bridge")

        # Response Depth Policy sprint - computed ONCE per turn, here,
        # alongside the other early per-turn reads above (memory
        # retrieval, emotion estimation) - same "compute once, own
        # try/except, never break the turn" convention. Deterministic,
        # no LLM/API call (see luno/response_policy.py's own module
        # docstring). `previous_score` is this SAME conversation's last
        # resolved score, kept in a small bounded, in-memory,
        # never-persisted dict (mirrors `_session_feedback_target`'s
        # exact convention) - never read from/written to any persistent
        # store, never a new memory system. The resulting instruction is
        # appended to `notes` below (right after the persona block) once
        # that list exists; this method's other logic (memory retrieval,
        # planning, tool execution) never reads `response_policy` at
        # all - it exists solely to reach the prompt.
        # Adaptive Response Depth Learning sprint - THIS conversation's
        # current bounded preference bias (if any), read here so it can be
        # passed into `compute_response_policy()` below alongside
        # `previous_score`. Deliberately read BEFORE this turn's own
        # feedback text is classified (`_update_depth_preference()`,
        # called later in this method, near `_update_session_feedback_target()`
        # - see that call site's own comment for why) - a feedback message
        # ("kepanjangan, singkat aja") nudges the preference used for the
        # NEXT turn's depth decision, never retroactively changes the
        # depth of the reply currently being generated. `None` (no
        # modifier at all) when this conversation has no prior feedback,
        # or no `conversation_id` was given - identical fallback shape to
        # `previous_score` immediately below.
        # Persistent Adaptive Response Depth Preference sprint - a
        # conversation that has never produced real depth feedback (no
        # entry in `_depth_preference` yet) now falls back to the frozen
        # `_depth_preference_startup_bias` snapshot (see `__init__`)
        # instead of `None`/neutral - this is the ONLY behavioral
        # difference from Sprint 2. Deliberately a READ-ONLY fallback,
        # never an insertion into `_depth_preference` itself - inserting
        # here would (a) make every conversation appear as if it had real
        # feedback, breaking `_depth_preference`'s own "only real
        # feedback ever creates an entry" invariant (see
        # `tests/test_adaptive_response_depth.py::test_e2e_4_content_correction_does_not_change_depth_preference`,
        # a pre-existing Sprint 2 regression test - unmodified by this
        # sprint, must keep passing), and (b) risk unbounded dict growth
        # from conversations that never produce feedback and are never
        # popped via `_on_conversation_ended()`'s pop - `conversation_ended`
        # IS routed to this module now (Conversation_ended Lifecycle
        # Routing sprint), but a process kill mid-conversation or any
        # other path that skips the normal end-of-conversation event
        # would still leave an entry un-popped, so `_depth_preference_max`
        # (the cap enforced right after this insertion) remains the real
        # safety net regardless. When no persisted file exists yet
        # (fresh install), the startup snapshot
        # is the dataclass default (`bias=0`), and `compute_response_policy()`
        # treats `adaptive_modifier=0` identically to `None` (`if
        # adaptive_modifier:` below is falsy for both) - so behavior is
        # BYTE-IDENTICAL to Sprint 2 whenever nothing has ever been
        # persisted, exactly satisfying "preserve exact existing behavior
        # when no stored preference exists".
        if conversation_id and conversation_id in self._depth_preference:
            adaptive_modifier = self._depth_preference[conversation_id].bias
        elif conversation_id:
            adaptive_modifier = self._depth_preference_startup_bias
        else:
            adaptive_modifier = None
        try:
            response_policy = compute_response_policy(
                text, previous_score=self._response_depth_context.get(conversation_id) if conversation_id else None,
                adaptive_modifier=adaptive_modifier,
            )
            if conversation_id:
                self._response_depth_context[conversation_id] = response_policy.score
                while len(self._response_depth_context) > self._response_depth_context_max:
                    oldest = next(iter(self._response_depth_context))
                    self._response_depth_context.pop(oldest, None)
                self._last_response_policy[conversation_id] = response_policy.to_dict()
                while len(self._last_response_policy) > self._response_depth_context_max:
                    oldest_policy = next(iter(self._last_response_policy))
                    self._last_response_policy.pop(oldest_policy, None)
            log(
                f"request_id={request_id} - response depth policy: depth={response_policy.depth} "
                f"score={response_policy.score} explicit={response_policy.explicit} reasons={response_policy.reasons}",
                "planner_bridge",
            )
        except Exception as ex:
            log(f"request_id={request_id} - response depth policy failed (defaulted to normal): {ex}", "planner_bridge")
            response_policy = ResponsePolicy(depth="normal", score=32, reasons=["policy_error_defaulted"], explicit=False)

        # Voice Output Mode sprint (Phase 5) - "Setting command sendiri
        # jangan ikut dibacakan sebagai response panjang." Reuses the
        # EXISTING `ResponsePolicy`/`explicit_short_instruction` shape
        # (see `compute_response_policy()`'s own handling of
        # `_EXPLICIT_SHORT_PHRASES` a few lines above) rather than adding
        # a second mechanism - a turn where the user just switched voice
        # mode always speaks its own (typically short) confirmation at
        # SHORT depth, regardless of what the ordinary classifier would
        # have picked. The mode SWITCH itself still only applies starting
        # next turn (see `voice_output_mode_for_this_turn` above) - this
        # only ever affects how THIS turn's own reply is spoken.
        if voice_mode_command is not None:
            response_policy = ResponsePolicy(
                depth="short", score=10, reasons=["voice_mode_command"], explicit=True,
            )

        # Chat/Voice Dual Output sprint - the depth this turn already
        # resolved above is published once, correlated by request_id, so
        # `BehaviorTreeModule._speak()` (a DIFFERENT module - no direct
        # call allowed, see luno/core's own "no module calls another
        # module's methods directly" rule) can build this turn's
        # `DualResponse` using the SAME depth, never a second, independent
        # classification. Mirrors the EXISTING `speaking_mode_assigned`
        # event (published a few lines below, right before
        # `NeedLLMResponse`) - same "auxiliary per-turn metadata,
        # correlated by request_id, consumed by a sibling module" shape,
        # nothing structurally new introduced here.
        self._event_bus.publish(Event(type="response_depth_assigned", data={
            "request_id": request_id, "depth": response_policy.depth,
            # Sprint 3 (Production-Safe LLM -> TTS Streaming Activation) -
            # found via Phase 0 audit: this event previously carried ONLY
            # `depth` as a plain string, so every consumer downstream
            # (`BehaviorTreeModule._speak()`'s own `build_dual_response()`
            # call, and now `StreamingSpeechCoordinator`'s) could never
            # know whether the user had EXPLICITLY asked for full detail
            # (`ResponsePolicy.explicit`) - `_resolve_explicit()` in
            # `response_output.py` reads `getattr(response_policy,
            # "explicit", False)`, which silently returns `False` for a
            # bare string, defeating `build_dual_response()`'s own
            # documented "explicit DETAILED skips compression entirely"
            # rule for every REAL turn (confirmed empirically: a real
            # "jelaskan detail ..." utterance through the actual event
            # path still lost content to budget-based compression, even
            # though this exact behavior is unit-tested and passes when
            # `build_dual_response()` is called directly with a full
            # `ResponsePolicy` object). Pre-existing gap, unrelated to
            # streaming - affected the non-streaming path identically -
            # fixed here since Phase 8 of this sprint requires explicit
            # SHORT/DETAILED instructions to remain authoritative for
            # BOTH paths, and both read this SAME event.
            "explicit": response_policy.explicit,
            # Voice Output Mode sprint - carries the mode THIS conversation
            # was already in BEFORE this turn's own command (if any) was
            # applied (see `voice_output_mode_for_this_turn` above) - the
            # SAME event/correlation mechanism `depth`/`explicit` already
            # use, extended with one more field, exactly how `explicit`
            # itself was added on top of a previously depth-only event
            # (Sprint 3, see this event's own comment above). Consumed by
            # `BehaviorTreeModule._generate_reply()`'s `_on_depth` closure
            # and `StreamingSpeechCoordinator._on_depth_assigned()`,
            # mirroring `depth`/`explicit` exactly.
            "voice_output_mode": voice_output_mode_for_this_turn,
        }))

        # AI-assisted device intent fallback (opt-in - see
        # `_classify_device_intent()`'s own docstring): only even
        # considered when IntentParser's fast regex parser found NOTHING
        # at all (every step came back "unknown") - a real match (even a
        # typo-tolerant one, e.g. "trun") never pays this extra LLM
        # round-trip. `effective_text` (what actually gets planned) can
        # differ from `text` (what the user actually said / what the
        # final spoken reply responds to) ONLY in that case.
        # Explicit long-term memory commands ("remember that...", "forget
        # that...") are checked BEFORE the device-intent classifier below -
        # they're meta conversation commands with zero IntentParser
        # vocabulary, so letting the classifier also take a swing at them
        # would just be a wasted (though harmless) extra LLM round trip.
        explicit_memory_note = self._handle_explicit_memory_command(text, request_id)
        if explicit_memory_note is None:
            explicit_memory_note = self._handle_manual_summarize_command(text, request_id)

        # Browser permission confirm-first release ("Luno: I need to
        # click Submit - go ahead?" -> "iya") - checked before
        # environmental intent below for the same reason that one is
        # checked before the device-intent classifier: a pending yes/no
        # reply must be resolved here, not accidentally re-parsed as a
        # fresh command. See `_handle_browser_confirmation()`'s own
        # docstring.
        if explicit_memory_note is None:
            browser_confirm_note = self._handle_browser_confirmation(text, request_id, conversation_id)
            if browser_confirm_note is not None:
                explicit_memory_note = browser_confirm_note

        # Environmental intent inference ("hawanya panas nih" -> propose
        # the AC) - checked next, same "meta, zero IntentParser
        # vocabulary, before planning" tier as the two commands above,
        # and BEFORE the device-intent classifier below (a pending
        # confirmation's yes/no reply - e.g. "iya" - would otherwise
        # itself get run through IntentParser/the device-intent
        # classifier and correctly find nothing, wasting a round trip
        # and, worse, never actually resolving the pending question).
        # See `_handle_environmental_intent()`'s own docstring for the
        # full two-turn state machine; unlike `explicit_memory_note`
        # above, this can ALSO override `effective_text` directly
        # (`env_command_override`) when the user just confirmed a
        # pending action, which is why it needs its own variable rather
        # than folding into `explicit_memory_note`.
        env_command_override: Optional[str] = None
        if explicit_memory_note is None:
            env_command_override, env_note = self._handle_environmental_intent(text, request_id, conversation_id)
            if env_note is not None:
                explicit_memory_note = env_note

        # Efficient LLM Classifier sprint - routing-confirmation reply
        # ("Sepertinya kamu maksudnya soal ... - mau aku lanjutkan?
        # (ya/tidak)" -> "iya"/"tidak") - same "pending yes/no must be
        # resolved here, before being re-parsed as a fresh command" tier
        # as browser_confirm_note/environmental intent above, checked
        # LAST among the three (only when NEITHER of those already
        # claimed this turn) so this newer, opt-in-by-default-off
        # feature never pre-empts an existing, already-relied-on
        # confirmation flow in the rare case both happened to be
        # pending at once. See `ConfirmationHandler.resolve_reply()`'s
        # own docstring for the full one-shot/cross-conversation-
        # isolation/expiry guarantees.
        routing_confirm_override: Optional[str] = None
        routing_confirm_forced_intent: Optional[RoutingIntent] = None
        if explicit_memory_note is None and env_command_override is None:
            routing_outcome = self.confirmation_handler.resolve_reply(conversation_id, text)
            if routing_outcome is not None:
                if routing_outcome.pending.intent == "browser_fallback":
                    # AppNotFound fallback offer (see the real_tasks loop
                    # further down where this pending entry is created) -
                    # a DIRECT action, not a routing re-classification: on
                    # confirm, open the browser fallback right here via
                    # `desktop_control.open_url` (same mechanism/honesty
                    # level `_handle_browser_research_intent`/
                    # `_handle_image_search_intent` already use - no
                    # Planner/Tool Manager step exists for "open a URL",
                    # same established precedent). `original_text` holds
                    # the FAILED APP SLUG (not a URL) so the exact same
                    # deterministic `guess_fallback_search_url()` call
                    # reproduces the identical URL here as when it was
                    # first offered - nothing stored is itself executable,
                    # per the "no I/O in ConfirmationHandler" contract.
                    # `routing_confirm_override`/`forced_intent` stay
                    # unset (this is not a re-classify), but
                    # `forced_intent=GENERAL_CHAT` is still required below
                    # so decide() doesn't ambiguous-classify the bare
                    # "iya" reply itself into a SECOND, unrelated pending
                    # confirmation (same bug class already fixed for the
                    # cancel branch below).
                    routing_confirm_forced_intent = RoutingIntent.GENERAL_CHAT
                    if routing_outcome.action == "confirmed":
                        from luno.desktop_control import guess_fallback_search_url, open_url
                        fallback_url, fallback_label = guess_fallback_search_url(routing_outcome.pending.original_text)
                        ok, message = open_url(fallback_url)
                        if ok:
                            explicit_memory_note = (
                                f'The user just confirmed the browser fallback offer. You opened it: {fallback_label} - {message} '
                                f'Tell them naturally that it\'s open now, do not add any other claim.'
                            )
                        else:
                            explicit_memory_note = (
                                f'The user just confirmed the browser fallback offer, but opening it failed: {message}. '
                                f'Tell them honestly that it did not open - do not claim it worked.'
                            )
                        log(
                            f"request_id={request_id} - browser fallback confirmation CONFIRMED (original "
                            f"request_id={routing_outcome.pending.request_id}) -> opened {fallback_url!r} ok={ok}",
                            "planner_bridge",
                        )
                    else:
                        explicit_memory_note = (
                            f'The user just declined the browser fallback offer. '
                            f'Acknowledge briefly and naturally (something like "{self.confirmation_handler.cancelled_ack()}") '
                            f'- do not open anything.'
                        )
                        log(
                            f"request_id={request_id} - browser fallback confirmation CANCELLED (original "
                            f"request_id={routing_outcome.pending.request_id})",
                            "planner_bridge",
                        )
                elif routing_outcome.pending.intent == "proactive_habit":
                    # Learned-habit proposal offer (see luno/proactive/
                    # habit_memory.py + manager.py::_maybe_ask_habit_proposal) -
                    # ANOTHER direct action, same shape as browser_fallback
                    # just above: no routing re-classification, no LLM
                    # call to phrase anything. `ProactiveModule` owns the
                    # actual `HabitMemory` (this module deliberately never
                    # holds a direct reference to it - see that package's
                    # own "communicates only via Planner + Event Bus"
                    # convention) - resolving here just PUBLISHES the
                    # outcome; `ProactiveModule.on_event()` applies it.
                    routing_confirm_forced_intent = RoutingIntent.GENERAL_CHAT
                    import json as _json
                    try:
                        payload = _json.loads(routing_outcome.pending.original_text)
                        habit_time_bucket = payload.get("time_bucket")
                        habit_items = payload.get("items") or []
                    except Exception:
                        habit_time_bucket, habit_items = None, []
                    outcome_str = "confirmed" if routing_outcome.action == "confirmed" else "declined"
                    if habit_time_bucket and habit_items and self._event_bus is not None:
                        self._event_bus.publish(Event(type="proactive_habit_resolved", data={
                            "outcome": outcome_str, "time_bucket": habit_time_bucket, "items": habit_items,
                        }))
                    if routing_outcome.action == "confirmed":
                        explicit_memory_note = (
                            'The user just confirmed the learned-habit automation offer. Acknowledge briefly and '
                            'naturally that it will happen automatically from now on - do NOT claim anything was '
                            'done just now, this only takes effect on future arrivals.'
                        )
                    else:
                        explicit_memory_note = (
                            f'The user just declined the learned-habit automation offer. '
                            f'Acknowledge briefly and naturally (something like "{self.confirmation_handler.cancelled_ack()}") '
                            f'- it will stay manual, nothing changes.'
                        )
                    log(
                        f"request_id={request_id} - proactive habit confirmation {outcome_str.upper()} "
                        f"(original request_id={routing_outcome.pending.request_id}) items={habit_items} "
                        f"time_bucket={habit_time_bucket!r}",
                        "planner_bridge",
                    )
                elif routing_outcome.action == "confirmed":
                    routing_confirm_override = routing_outcome.pending.original_text
                    try:
                        routing_confirm_forced_intent = RoutingIntent(routing_outcome.pending.intent)
                    except ValueError:
                        routing_confirm_forced_intent = None  # defensive - never crash a turn over a stored value
                    log(
                        f"request_id={request_id} - routing confirmation CONFIRMED (original "
                        f"request_id={routing_outcome.pending.request_id}) -> re-processing "
                        f"{routing_confirm_override!r} as intent={routing_outcome.pending.intent}",
                        "planner_bridge",
                    )
                else:
                    explicit_memory_note = (
                        f'The user just declined a pending clarification about "{routing_outcome.pending.original_text}". '
                        f'Acknowledge briefly and naturally (something like "{self.confirmation_handler.cancelled_ack()}") '
                        f'- do not do anything.'
                    )
                    # The bare "tidak"/"batal" reply itself must NOT be
                    # handed to `decision_engine.decide()` as a brand new
                    # utterance to classify - it would (correctly, by its
                    # own rules) find no deterministic match, hit the
                    # SAME ambiguous-gate, and mint a SECOND, unrelated
                    # pending confirmation about the word "tidak" itself.
                    # Forcing GENERAL_CHAT here is the honest
                    # characterization of what this turn actually was (a
                    # plain acknowledgment reply, already fully handled
                    # above) - see the `decision_engine.decide(...,
                    # forced_intent=...)` call site below.
                    routing_confirm_forced_intent = RoutingIntent.GENERAL_CHAT
                    log(
                        f"request_id={request_id} - routing confirmation CANCELLED (original "
                        f"request_id={routing_outcome.pending.request_id})",
                        "planner_bridge",
                    )

        # Memory Learning & Feedback Loop sprint - conversational memory
        # feedback ("iya benar" / "itu salah" / "yang tadi salah, sekarang
        # X"), checked ONLY after every pending-confirmation mechanism
        # above (browser permission, environmental intent, routing
        # classifier) has already had its chance and found NOTHING pending
        # for this turn - `explicit_memory_note`/`env_command_override`/
        # `routing_confirm_override`/`routing_confirm_forced_intent` all
        # still `None` here means none of those flows claimed this turn.
        # This ordering is deliberate and load-bearing: a real pending
        # "iya"-shaped confirmation for one of those existing flows must
        # never be intercepted by this newer, additive feedback check
        # instead (see `_handle_memory_feedback_command()`'s own docstring
        # for the full reasoning).
        if (
            explicit_memory_note is None
            and env_command_override is None
            and routing_confirm_override is None
            and routing_confirm_forced_intent is None
        ):
            try:
                feedback_note = self._handle_memory_feedback_command(text, request_id, conversation_id)
            except Exception as ex:
                log(f"request_id={request_id} - memory feedback handling raised (skipped): {ex}", "planner_bridge")
                feedback_note = None
            if feedback_note is not None:
                explicit_memory_note = feedback_note

        # Recomputes THIS conversation's feedback target for the NEXT turn
        # from THIS turn's own `relevant_memories_early` - must run AFTER
        # the feedback check immediately above (which needs the PREVIOUS
        # turn's target), never before. Own try/except, same "a bug here
        # must never break a turn" convention as every other note-producing
        # call site in this method.
        try:
            self._update_session_feedback_target(conversation_id, relevant_memories_early, query_text=text)
        except Exception as ex:
            log(f"request_id={request_id} - session feedback target update failed (skipped): {ex}", "planner_bridge")

        # Memory Retrieval & Decision Quality sprint (Phase 2) - replaces
        # THIS conversation's stored topic-terms snapshot with THIS turn's
        # own bounded token set, for the NEXT turn's continuity bonus.
        # Deliberately runs here (after this turn's own `previous_topic_terms`
        # was already read, above, and after `assemble_context()` further
        # below has not yet run for THIS turn - order relative to
        # `assemble_context()` doesn't matter either way, since that call
        # only reads `previous_topic_terms`, a local variable captured
        # BEFORE this update). Own try/except, same convention as every
        # other note-producing call site in this method.
        try:
            self._last_topic_terms[_topic_key] = memory_context.extract_topic_terms(text)
            while len(self._last_topic_terms) > self._last_topic_terms_max:
                oldest = next(iter(self._last_topic_terms))
                self._last_topic_terms.pop(oldest, None)
        except Exception as ex:
            log(f"request_id={request_id} - topic continuity update failed (skipped): {ex}", "planner_bridge")

        # Adaptive Response Depth Learning sprint - classifies THIS turn's
        # own `text` for depth feedback ("kepanjangan"/"kurang jelas"/
        # "pas" about the PREVIOUS reply) and folds it into this
        # conversation's bounded preference. Deliberately runs here -
        # AFTER `response_policy` (and this turn's OWN depth) was already
        # computed and published above, so a feedback message only ever
        # influences the NEXT turn's depth decision, never retroactively
        # changes the reply already being generated this turn (matches
        # the sprint's own worked examples: feedback about the PREVIOUS
        # reply changes what happens on the turn AFTER that). Own
        # try/except, same "a bug here must never break a turn" convention
        # as every other note-producing call site in this method - a
        # failure here silently leaves the preference exactly as it was
        # (fail-safe, never fail-open into "always short"/"always
        # detailed").
        try:
            self._update_depth_preference(conversation_id, text)
        except Exception as ex:
            log(f"request_id={request_id} - adaptive depth preference update failed (skipped): {ex}", "planner_bridge")
        finally:
            # Conversation_end Race Safety sprint - THIS turn's
            # feedback-relevant processing has now settled (success or
            # failure - either way, `_depth_preference` will not change
            # again for this turn), so a `_on_conversation_ended()` call
            # for this same conversation, possibly already waiting in
            # `_wait_for_turn_to_settle()`, can stop waiting on it. Runs
            # unconditionally (even if `_update_depth_preference()`
            # somehow raised past its own try/except above) so a bug
            # here can never leave the counter stuck and the runtime
            # waiting the full timeout for nothing.
            self._mark_turn_settled(conversation_id)

        effective_text = text
        ha_group_refusal_note: Optional[str] = None
        if env_command_override is not None:
            effective_text = env_command_override
            # Remember-only: `effective_text` is already fixed to the
            # confirmed environmental action (e.g. "turn on AC Kamar"),
            # but a LATER "sekarang matikan" should still be able to
            # reuse that same device - see `_apply_device_context()`'s
            # own docstring. Its return value is deliberately discarded
            # here (nothing to override - we already have a target).
            self._apply_device_context(effective_text, conversation_id)
        elif routing_confirm_override is not None:
            # Re-process the ORIGINAL (pre-confirmation) text through the
            # exact same deterministic `IntentParser`/Planner/Tool
            # Manager/Verification path any normal command already goes
            # through below - see `_handle_utterance`'s own docstring
            # note near `decision_engine.decide(..., forced_intent=...)`
            # further down for why `routing_confirm_forced_intent` is
            # passed there too (skips re-classification for this ONE
            # call only, never stored anywhere).
            effective_text = routing_confirm_override
        elif explicit_memory_note is None:
            # Sprint 58 (HA Multi-Entity & Group Commands) - checked BEFORE
            # Sprint 57's own single-target contextual fill immediately
            # below: a group/multi-target command is fully self-contained
            # in `text` (an explicit target list, or the "semua lampu"
            # keyword) and never depends on conversation memory, unlike a
            # contextual single-target reference ("Matikan." reusing the
            # last device talked about). `_apply_ha_group_resolution()`
            # returns `text` byte-for-byte unchanged whenever it isn't a
            # detected group shape at all (the overwhelming common case,
            # and every existing Sprint 52/56/57 single-target test) - only
            # then does that unchanged text flow into the pre-existing
            # `_apply_device_context()` call exactly as before this sprint
            # existed. See `_apply_ha_group_resolution()`'s own docstring
            # for why a REFUSED group turn's `effective_text` is always the
            # empty string (the mechanism that guarantees zero HA API
            # calls for any target in a refused group).
            text_for_context, ha_group_refusal_note = self._apply_ha_group_resolution(text, conversation_id)
            if ha_group_refusal_note is not None:
                effective_text = text_for_context
            else:
                # Short-term device-context resolution ("sekarang matikan"
                # after "aktifkan lampu kamar" -> understood as "matikan
                # lampu kamar") - checked BEFORE the AI-assisted device-
                # intent fallback below, on the SAME `text` that fallback
                # would otherwise see. If a remembered device applies,
                # `effective_text` already resolves to a real home_assistant
                # command below, so `parser_found_nothing` comes back False
                # and the (more expensive, LLM-backed) fallback is never
                # even reached - see `_apply_device_context()`'s own
                # docstring for the full mechanism.
                effective_text = self._apply_device_context(text_for_context, conversation_id)
                try:
                    parsed_steps = IntentParser.parse(effective_text)
                    parser_found_nothing = bool(parsed_steps) and all(step.tool == "unknown" for step in parsed_steps)
                except Exception:
                    parser_found_nothing = False
                if parser_found_nothing:
                    corrected = self._classify_device_intent(text)
                    if corrected:
                        effective_text = corrected
                        log(f"request_id={request_id} - device intent classifier: {text!r} -> {effective_text!r}", "planner_bridge")

        # BUG FIX (reported): "nyalakan rgb strip dan matikan fish light"
        # only ever ran the FIRST action - the second was silently never
        # even attempted, yet the reply still sounded like a success.
        # Root cause: `IntentParser` marks every parsed clause as
        # SEQUENTIAL/depends_on_previous (see parser.py's own docstring -
        # a deliberate, conservative choice about ORDERING, not a claim
        # that clause 2 needs clause 1 to have SUCCEEDED). `PlanOptions`
        # defaults to `continue_on_failure=False` (the right default for
        # the Planner package in general - a real dependency chain SHOULD
        # halt if an earlier, necessary step failed), which here caused
        # `PlanRunner._apply_failure_policy` to mark every remaining
        # WAITING task SKIPPED the moment the first clause failed/didn't
        # verify - "turn off the fish light" never even reached
        # `ToolRequested`. A live utterance's clauses (split on ","/"and"/
        # "dan"/"then") are independent user intents chained only for
        # display ORDER, not success-dependency, so this call site (and
        # only this one - PlanOptions' own default is untouched for any
        # other caller) opts into `continue_on_failure=True`: every
        # parsed command is now actually attempted regardless of whether
        # an earlier one in the same sentence succeeded.
        plan = self.planner.create_plan(effective_text, options=PlanOptions(continue_on_failure=True))
        self.last_plan_id = plan.id
        # Debug logging (interrupt routing / request_id correlation audit):
        # request_id and plan_id are DELIBERATELY different namespaces -
        # request_id (format "turn-...") identifies one conversational
        # turn end-to-end (NeedLLMResponse -> LLMStarted/Finished ->
        # AssistantResponse -> SpeakRequest -> SpeechStarted/Finished ->
        # LLMCancelled/SpeechCancelled); plan_id (format "plan_...")
        # identifies this Planner's own internal plan and is used ONLY
        # for Planner introspection (`get_status`, `pause`, `resume`) and
        # the PlannerCreated/PlannerFinished events below. request_id is
        # always threaded through unchanged from `user_utterance` - never
        # replaced by plan_id anywhere below.
        log(f"request_id={request_id} plan_id={plan.id} text={text!r} - plan created", "planner_bridge")
        from luno.core.events import PlannerCreated
        self._event_bus.publish(PlannerCreated(data={
            "request_id": request_id, "conversation_id": conversation_id,
            "plan_id": plan.id, "task_count": len(plan.tasks),
            "validation_errors": plan.validation_errors,
        }))

        # Unknown-tool fix: "unknown" is IntentParser's own sentinel for
        # "not a device command at all" (plain conversation, a question,
        # a vision question already handled separately by
        # `_handle_vision_intent()` above, ...) - it was never registered
        # against a real Tool Manager handler (see `KNOWN_TOOLS`'s own
        # comment), so executing a plan whose ONLY task is "unknown" did
        # nothing but round-trip through `ToolRequested`/`ToolFailed` for
        # a "No handler registered for tool 'unknown'" failure on every
        # single plain-conversation turn - harmless (filtered out of
        # `real_tasks`/the LLM notes below either way) but pure noise.
        # Skipping `execute()` entirely whenever there's no REAL task
        # avoids that round-trip completely for the common case (a whole
        # utterance that isn't a command at all).
        #
        # Residual edge case, accepted rather than engineered around: a
        # MIXED utterance ("matikan lampu dan bagaimana cuaca hari ini")
        # still has at least one real task, so `execute()` DOES run, and
        # the unknown clause specifically will still hit "not registered"
        # internally. `continue_on_failure=True` above (same fix as the
        # "nyalakan rgb strip dan matikan fish light" bug) already
        # guarantees that doesn't block/skip the real task next to it,
        # and it's still excluded from the LLM notes below - so the only
        # remaining cost is one harmless internal `tool_failed` event for
        # a genuinely rare phrasing, not a functional problem.
        real_task_count = sum(1 for t in plan.tasks if t.tool_call.tool != "unknown")

        task_summaries: List[str] = []
        if plan.tasks and not plan.validation_errors and real_task_count > 0:
            try:
                self.planner.execute(plan)
            except Exception as ex:
                self._event_bus.publish(Event(type="planner_failed", data={
                    "request_id": request_id, "plan_id": plan.id, "error": str(ex),
                }))
            deadline = time.time() + max(5.0, self.tool_timeout_s * max(1, len(plan.tasks)))
            while time.time() < deadline:
                status = self.planner.get_status(plan.id)
                if status.plan_status.value in ("completed", "failed", "cancelled"):
                    break
                time.sleep(0.05)
            for task in plan.tasks:
                label = task.label or f"{task.tool_call.tool}.{task.tool_call.action}"
                task_summaries.append(f"{label}: {task.status.value}")

        from luno.core.events import PlannerFinished
        self._event_bus.publish(PlannerFinished(data={
            "request_id": request_id, "conversation_id": conversation_id,
            "plan_id": plan.id, "tasks": task_summaries,
        }))

        notes: List[str] = []
        # Persona (config/persona.json via luno/persona.py) - BUG: this was
        # built for the legacy luno/main.py single-file assistant only
        # (see that module's own docstring: "Dipanggil dari main.py's
        # build_system_prompt()") and was never wired into THIS bridge -
        # the one `luno/bootstrap/modules.py` actually loads for the
        # Sprint 6+ production runtime (`python main.py` at the project
        # root). Every real LLM call went out with zero persona/character
        # instructions at all, so the model had nothing to go on but its
        # own default identity - explains a plain "Hello, I'm Gemini..."
        # self-introduction once a real (non-mock) LLM started actually
        # reading the system prompt. Always included, unconditionally -
        # unlike the task/memory notes below, the persona applies to every
        # turn, tool-using or not.
        try:
            persona_block = build_persona_prompt()
        except Exception as ex:
            log(f"request_id={request_id} - persona prompt build failed (skipped): {ex}", "planner_bridge")
            persona_block = ""
        if persona_block:
            notes.append(persona_block)

        # Response Depth Policy sprint - one small instruction layer
        # (never three separate giant prompts, never overriding persona/
        # personality above) telling the LLM how long/detailed THIS
        # turn's reply should be. `response_policy` was already computed
        # once, early in this method - never recomputed here.
        notes.append(build_depth_instruction(response_policy))

        # Relationship Engine Foundation sprint - compact, banded,
        # deterministic relationship-state note (see luno/
        # relationship_engine.py's own module docstring for the full
        # design). Placed here deliberately: right after persona/identity
        # and BEFORE memory/vision/verified-facts/session-summary notes -
        # per that sprint's own "the relationship context should occupy a
        # clearly defined layer between identity/personality and
        # conversation/tool/memory context" guidance, cross-checked
        # against this method's actual existing note order (persona is
        # the only thing that already sits this early). `plan.tasks`
        # already reflects real execution results by this point (planned
        # + executed above, before this notes-building section begins),
        # so `had_successful_tool_call` can be read here safely without
        # waiting for `real_tasks`/`completed_real_tasks` to be computed
        # further down for their own (differently-scoped) purposes.
        # `memory.detect_remember_command()` is an existing, public,
        # side-effect-free function - calling it again here to detect
        # "the user just explicitly asked to be remembered" costs
        # nothing and never duplicates or mutates Memory itself (the
        # Relationship Engine only ever READS this boolean, it never
        # calls `memory.add_memory()`). `build_prompt_block()` itself
        # returns "" for a still-new relationship or a turn that changed
        # nothing meaningful - most turns, especially technical ones,
        # add nothing here at all, same "only when actually relevant"
        # shape as `memory_block`/the Emotion Engine's own note below.
        try:
            _had_successful_tool_call = any(
                t.tool_call.tool != "unknown" and t.status.value == "completed" for t in plan.tasks
            )
            _remember_command_used = bool(memory.detect_remember_command(text))
            # Shared Experience & Episodic Memory sprint - detect + persist
            # (see luno/episodic_memory.py's own docstring for the full
            # detect -> validate -> deduplicate -> persist pipeline). Wrapped
            # in its OWN try/except, nested inside the relationship block's
            # existing one, so a failure here degrades to "no new episodic
            # signal this turn" without also skipping the relationship
            # update below (same "one failure never cascades" discipline as
            # every other note in this method).
            try:
                _new_experience, _episodic_entry = episodic_memory.observe_turn(
                    text,
                    had_successful_tool_call=_had_successful_tool_call,
                    explicit_memory_shared=_remember_command_used,
                )
            except Exception as ex:
                log(f"request_id={request_id} - episodic memory detection failed (skipped): {ex}", "planner_bridge")
                _new_experience = False
            # Episodic Memory -> Relationship Engine, one-way (section 14 of
            # that sprint's brief): a genuinely NEW, grounded shared
            # experience is OR'd into the SAME `explicit_memory_shared`
            # signal the Relationship Engine already accepted before this
            # sprint - it never gains a new parameter, and a duplicate/no
            # candidate this turn changes nothing about its existing
            # behavior (detect_remember_command's own boolean still works
            # exactly as before).
            _explicit_memory_shared = _remember_command_used or _new_experience
            self.relationship_state = RelationshipEngine.observe_turn(
                self.relationship_state,
                text,
                had_successful_tool_call=_had_successful_tool_call,
                explicit_memory_shared=_explicit_memory_shared,
                emotion_state=self.emotion_tracker.current(),
            )
            RelationshipStore.save(self.relationship_state)
            relationship_block = RelationshipContextBuilder.build_prompt_block(self.relationship_state)
        except Exception as ex:
            log(f"request_id={request_id} - relationship engine update failed (skipped): {ex}", "planner_bridge")
            relationship_block = ""
        if relationship_block:
            notes.append(relationship_block)

        # Explicit long-term memory (config/long_term_memory.json via
        # luno/memory.py) - two independent pieces:
        #   1) explicit_memory_note - set above ONLY when this exact
        #      turn WAS a remember/forget/clear command, describing what
        #      was actually just done so the LLM confirms honestly.
        #   2) build_memory_prompt(query_text=text) - Memory Prompt
        #      Intelligence sprint: this used to be an unconditional full
        #      dump of every saved fact, every turn, regardless of what
        #      the user was asking - the "legacy prompt path" that the
        #      newer importance/lifecycle/conflict intelligence (already
        #      controlling `memory_block` below via the "manual_memory"
        #      MemoryRetriever source) didn't yet reach. Passing `text`
        #      here makes THIS path obey the same rules: relevance-gated
        #      first (an irrelevant, even importance=4, memory is never
        #      injected just because it exists), lifecycle-aware (archived
        #      entries excluded, not deleted), conflict-aware (an
        #      unresolved ambiguous conflict is surfaced as an explicit
        #      hedge, never silently picked), and budget-bounded via the
        #      same MemoryRetrievalConfig the retrieval pipeline already
        #      uses. Most turns now add nothing here at all - same "only
        #      when actually relevant" shape as memory_block. This does
        #      NOT replace memory_block/MemoryRetriever - see
        #      `luno/memory.py`'s own `build_memory_prompt()` docstring.
        if explicit_memory_note:
            notes.append(explicit_memory_note)
        # Sprint 58 (HA Multi-Entity & Group Commands) - same "note, not a
        # tool call" mechanism as `explicit_memory_note` just above: a
        # refused group/multi-target command still needs the LLM to
        # honestly explain WHY nothing was touched, even though
        # `effective_text` (the empty string - see `_apply_ha_group_
        # resolution()`'s own docstring) produced zero real Planner tasks
        # for this turn.
        if ha_group_refusal_note:
            notes.append(ha_group_refusal_note)
        # Memory Context Assembly & Retrieval Unification sprint: this used
        # to also build `explicit_memory_block` here via `memory.
        # build_memory_prompt(query_text=text)` - a SECOND, independent
        # Manual-Memory relevance pass, running alongside `memory_block`
        # (built further down from the SAME turn's `relevant_memories_early`,
        # which already includes a `"manual_memory"` MemoryRetriever pass).
        # That duplication is now unified into the single
        # `memory_context.assemble_context(...)` call below (see this
        # method's own note near `memory_block` for the full explanation) -
        # `build_memory_prompt(query_text=...)` itself is UNCHANGED and
        # still fully supported for any other/future caller (Step 19
        # backward compatibility), this is only removing the SECOND call
        # site that duplicated it.

        # Vision intent ("ada apa di kamera") - classified + (if matched)
        # actually queried against the real camera+Gemini pipeline in
        # `_handle_vision_intent()` above; see that method's own docstring
        # for why this is a pre-fetch-and-inject note rather than live
        # function-calling. Runs on the ORIGINAL `text`, not
        # `effective_text` - a vision question is never a device command,
        # so it must never be affected by the short-term device-context
        # substitution `_apply_device_context()` may have applied above.
        try:
            vision_note = self._handle_vision_intent(text, request_id)
        except Exception as ex:
            log(f"request_id={request_id} - vision intent handling raised (skipped): {ex}", "planner_bridge")
            vision_note = None
        if vision_note:
            notes.append(vision_note)

        # Screen intent ("screenshot terus liat kenapa error") - same
        # pre-fetch-and-inject tier as vision_note above, independent
        # feature/master-switch (see `_handle_screen_intent()`'s own
        # docstring). Also runs on the ORIGINAL `text` for the same
        # reason vision_note does - a screen-diagnosis request is never
        # a device command either.
        try:
            screen_note = self._handle_screen_intent(text, request_id)
        except Exception as ex:
            log(f"request_id={request_id} - screen intent handling raised (skipped): {ex}", "planner_bridge")
            screen_note = None
        if screen_note:
            notes.append(screen_note)

        # Browser: monitoring / computer-use / research intents - same
        # "pre-fetch and inject" tier as vision above (this project has
        # no live function-calling loop, see `_handle_utterance`'s own
        # comment near vision_note). Checked independently (each has a
        # narrow, largely non-overlapping co-occurrence rule - see
        # `luno/browser/intent.py`) on the ORIGINAL `text`, never
        # `effective_text`, same reasoning as vision: none of these are
        # device commands, so short-term device-context substitution
        # must never affect them.
        try:
            image_search_note = self._handle_image_search_intent(text, request_id)
        except Exception as ex:
            log(f"request_id={request_id} - image search intent handling raised (skipped): {ex}", "planner_bridge")
            image_search_note = None
        if image_search_note:
            notes.append(image_search_note)

        try:
            monitoring_note = self._handle_monitoring_intent(text, request_id)
        except Exception as ex:
            log(f"request_id={request_id} - monitoring intent handling raised (skipped): {ex}", "planner_bridge")
            monitoring_note = None
        if monitoring_note:
            notes.append(monitoring_note)

        try:
            computer_use_note = self._handle_computer_use_intent(text, request_id, conversation_id)
        except Exception as ex:
            log(f"request_id={request_id} - computer-use intent handling raised (skipped): {ex}", "planner_bridge")
            computer_use_note = None
        if computer_use_note:
            notes.append(computer_use_note)

        # Skip the (heavier) text-research workflow when image search
        # already matched this same utterance - "carikan gambar kucing"
        # satisfies BOTH classifiers' keyword rules ("carikan" alone is
        # enough for `classify_research_intent`), but opening a visible
        # image-search browser window already fully answers an image
        # request; also running a redundant text-research round trip
        # would be wasted work and a confusing double response.
        research_note = None
        if not image_search_note:
            try:
                research_note = self._handle_browser_research_intent(text, request_id)
            except Exception as ex:
                log(f"request_id={request_id} - browser research intent handling raised (skipped): {ex}", "planner_bridge")
                research_note = None
        if research_note:
            notes.append(research_note)

        # Session summaries (config/session_summaries.json) - same
        # unconditional-when-non-empty treatment as the long-term memory
        # block above, so the LLM can naturally answer "what did we talk
        # about last time" without a separate special-cased recall path.
        try:
            session_summary_block = memory.build_session_summary_prompt()
        except Exception as ex:
            log(f"request_id={request_id} - session summary prompt build failed (skipped): {ex}", "planner_bridge")
            session_summary_block = ""
        if session_summary_block:
            notes.append(session_summary_block)

        # Reliability Sprint (Never Assume Success) - tell the LLM the
        # VERIFIED FACTS for every real tool task, not just a label plus a
        # blanket "confirm this succeeded" instruction.
        #
        # `task.result` (set only on TaskStatus.COMPLETED - see
        # `luno/planner/executor.py::TaskExecutor._safe_run`) is exactly
        # `_tool_bridge_handler()`'s return value, which is exactly
        # `ToolFinished.data` = `ToolResult.to_dict()` (see
        # `PlannerBridgeModule._tool_bridge_handler` above and
        # `ToolManagerModule.on_event` in this same file) - so
        # `task.result["message"]` is already the sprint's own honest,
        # VERIFIED phrasing ("I've turned on Bedroom Light." /
        # "I tried to turn on Bedroom Light, but it didn't respond.").
        # `task.error` (set only on TaskStatus.FAILED) is
        # `_tool_bridge_handler`'s raised exception's message, which is
        # `box["failed"].get("error") or box["failed"].get("message")` -
        # same underlying `ToolResult.message`/`error_type` text, just
        # reached via the raise-on-failure path instead of a return value
        # (this is also what makes the Planner's own TaskStatus already
        # correctly FAILED rather than COMPLETED whenever
        # `ToolResult.success` is False - `_tool_bridge_handler` raises
        # instead of returning in exactly that case, so `TaskExecutor`
        # never has to guess).
        #
        # BUG (fixed here): only `completed` tasks ever got a note before
        # this fix, built from the task's LABEL, telling the LLM to
        # "confirm this succeeded" - with ZERO regard for what the
        # verified `ToolResult.message` actually said, and FAILED tasks
        # got no note at all, leaving the LLM with no information to
        # contradict its own default assumption that a request it was
        # just asked to fulfil went fine. Both are now reported using the
        # tool's own verified message text.
        # TODO(World Model): this project has no dedicated World Model
        # module yet (only luno/proactive/context_evaluator.py reads a
        # small ad-hoc snapshot). Once one exists, the correct hook is
        # right here: for each `completed_lines` entry, update the World
        # Model with `data["entity_id"] = data["actual_state"]` (from
        # `task.result["data"]`) - and deliberately do NOT update it for
        # anything in `failed_lines` below, so the World Model never
        # drifts from the real, verified device state.
        real_tasks = [t for t in plan.tasks if t.tool_call.tool != "unknown"]
        for note in build_verified_action_notes(real_tasks, text):
            notes.append(note)

        # AppNotFound browser-fallback offer (reported gap: "buka channel
        # Mr beast di youtube" used to just fail with "not registered,
        # apps I know are: steam/chrome/...", leaving it entirely up to
        # the LLM's own free-form judgement whether to offer a browser
        # fallback - inconsistent (sometimes offered, sometimes not,
        # never actually acted on without another full round-trip).
        # Instead of guessing, detect this SPECIFIC verified failure
        # (error_type="AppNotFound" - see `RealWindowsHandler.execute()`)
        # and go through the exact same `ConfirmationHandler` the routing
        # classifier uses above - reused generically per its own
        # docstring ("works for HA, browser, and future tools"), NOT a
        # new state machine. Only for AppNotFound - a bad path or launch
        # exception isn't fixable by opening a browser, so those are left
        # as a plain failure (already reported via `failed_lines` above).
        # Only the FIRST such failure this turn gets an offer - one
        # pending confirmation per conversation is `ConfirmationHandler`'s
        # own existing model (a second ambiguous turn would supersede it
        # anyway).
        app_not_found_task = next(
            (
                t for t in real_tasks
                if t.status.value == "failed"
                and t.tool_call.tool == "windows"
                and t.tool_call.action in ("open_app", "launch_app")
                and isinstance(t.result, dict)
                and t.result.get("error_type") == "AppNotFound"
            ),
            None,
        )
        if app_not_found_task is not None:
            from luno.desktop_control import guess_fallback_search_url
            failed_target = app_not_found_task.tool_call.target or ""
            _fallback_url, fallback_label = guess_fallback_search_url(failed_target)
            pending = self.confirmation_handler.request_confirmation(
                request_id=request_id, conversation_id=conversation_id, text=failed_target,
                intent="browser_fallback", confidence=1.0,
            )
            notes.append(
                f'The app the user asked to open ("{failed_target}") is not registered - that already failed, '
                f'see above. Ask the user, naturally and briefly, whether they want you to try {fallback_label} '
                f'in the browser instead - do NOT say you already opened it, you are only ASKING. It will only '
                f'actually open if they say yes on their next reply.'
            )
            log(
                f"request_id={request_id} - AppNotFound({failed_target!r}) -> offering browser fallback "
                f"({fallback_label}), pending request_id={pending.request_id}",
                "planner_bridge",
            )

        # Memory Guard Sprint - the ONLY call site that turns a verified
        # ToolResult into a durable fact. Reuses `task.result` (same data
        # `build_verified_action_notes` above already reads) - no new
        # query to Home Assistant, no LLM call. Only COMPLETED tasks are
        # offered; `VerifiedFactStore.record()` itself is the actual
        # gate (`should_store_verified_result`) so this stays correct
        # even if that changes. Failed tasks are deliberately never
        # passed in at all - see Bagian 1/3 of the sprint spec.
        for task in real_tasks:
            if task.status.value == "completed":
                self.memory_guard.record(task.result, tool_name=task.tool_call.tool, request_id=request_id)
                # World Model Sprint Bagian 4 - same gate as above
                # (`update_from_tool_result` re-checks `success is True`
                # itself), same `task.result`, no extra work: a verified
                # success updates the Single Source of Truth, a failure
                # never does.
                self.world_model.update_from_tool_result(task.result)

        # Sprint 5 - Smart Memory Injection, unified by the Memory Context
        # Assembly & Retrieval Unification sprint: `relevant_memories_early`
        # (already computed synchronously near the top of this method, and
        # already used for usage-tracking above - no second retrieval pass)
        # is now handed to `memory_context.assemble_context()` instead of
        # rendered directly via `build_memory_prompt_block()`. That single
        # call reuses `relevant_memories_early` as its base candidate pool
        # (vision/planner-state/episodic/manual-memory, all already
        # relevance-gated, deduped, ranked, and budget-limited exactly as
        # before by `MemoryRetriever` itself - nothing about THAT machinery
        # changed), and additively layers in:
        #   - the ambiguous-conflict-group joint presentation
        #     `make_manual_memory_source()` itself doesn't do (previously
        #     only available through the now-removed `explicit_memory_block`
        #     path above - see that removal's own comment),
        #   - a new, previously-nonexistent read-only Verified Facts adapter
        #     (`self.memory_guard` was write-only in production before this
        #     sprint - see docs/change_impact/memory_context_assembly.md
        #     section 3.2),
        # then cross-source-deduplicates, re-ranks (relevance always first,
        # importance/priority only break ties - Step 7/14), and re-bounds
        # the WHOLE combined set by the same `MemoryRetrievalConfig` budget
        # every memory prompt path in this project already reads. Read-only
        # (see luno/memory_context.py's own module docstring) and grouped
        # into labeled sections (`[Verified Facts]`/`[Relevant Memories]`/
        # `[Relevant Experiences]`/`[Historical Context]`) - only sections
        # with selected content appear, same "add nothing when nothing is
        # relevant" shape every other note in this method already has.
        # Relationship context is deliberately NOT routed through here -
        # `relationship_block` above already handles it via its own,
        # already-correct, already-tested path (Step 15: "keep minimal",
        # never duplicate an already-working integration).
        # Memory Continuity & Short Follow-up Reference Resolution sprint
        # (Sprint 4, Phase 4) - only ever engages when THIS turn's own
        # text is a short/elliptical follow-up (`is_short_followup`,
        # computed above) AND a non-stale active-topic snapshot exists for
        # this conversation. A normal, richly-worded turn is completely
        # unaffected: `retrieval_query_override` stays `None` and
        # `relevant_memories_for_context` stays the SAME object as
        # `relevant_memories_early` - i.e. `assemble_context()` behaves
        # byte-for-byte as it did before this sprint. The synthetic
        # candidate is deliberately built into a NEW list here, never
        # written back into `relevant_memories_early` itself, so every
        # OTHER consumer of that list this turn (usage tracking above,
        # `_update_session_feedback_target`, the routing Decision Engine,
        # turn-trace telemetry, response selection - all either already
        # ran or read the list independently elsewhere in this method)
        # never sees this synthetic, non-retrieved candidate - exactly-once
        # retrieval (`self.memory_retriever.retrieve_memories()` is still
        # called exactly once per turn, at the top of this method) is
        # unaffected; nothing here issues a second retrieval call.
        retrieval_query_override = None
        relevant_memories_for_context = relevant_memories_early
        active_topic_candidate = None

        # Conversation Reference Resolution sprint (Sprint 38, Phase 3-4,
        # 8) - resolved FIRST, ahead of even the topic-history branch
        # below: Phase 8's own required pipeline order is "resolve the
        # reference target, THEN retrieve" - an ordinal reference ("yang
        # kedua gimana?") names a SPECIFIC item ("MAX9814"), which is a
        # strictly more precise target than either the topic-history or
        # single-slot bag-of-terms branches below can produce (neither
        # has any concept of list position). `resolve_ordinal_targets()`
        # never fabricates (Phase 9) - it returns `((), "none")` whenever
        # `text` names no ordinal at all (the overwhelming common case,
        # in which this block is a complete no-op) OR there is no usable
        # list to resolve against, leaving the branches below as the
        # correct, unmodified fallback for every other shape.
        ordinal_targets: tuple = ()
        try:
            ordinal_targets, _ordinal_confidence = memory_context.resolve_ordinal_targets(
                text, active_topic_snapshot, self._topic_history.get(_topic_key),
            )
        except Exception as ex:
            log(f"request_id={request_id} - ordinal reference resolution raised (skipped): {ex}", "planner_bridge")
            ordinal_targets = ()

        if ordinal_targets:
            try:
                retrieval_query_override = memory_context.build_expanded_retrieval_text_for_targets(text, ordinal_targets)
                # Also fold in the PARENT topic's own terms (e.g. "esp32",
                # "mikrofon") alongside the specific resolved item(s) -
                # Phase 5's own "preserve the parent topic, don't lose it"
                # requirement applies here too: "yang kedua gimana?" should
                # retrieve MAX9814-specific content WITHOUT losing the
                # fact this is still an ESP32/microphone conversation.
                if active_topic_snapshot is not None and not active_topic_snapshot.is_stale:
                    retrieval_query_override = memory_context.build_expanded_retrieval_text(
                        retrieval_query_override, active_topic_snapshot,
                    )
                ordinal_candidate = memory_context.ordinal_targets_to_relevant_memory(ordinal_targets, turn_id=request_id)
                if ordinal_candidate is not None:
                    relevant_memories_for_context = relevant_memories_early + [ordinal_candidate]
            except Exception as ex:
                log(f"request_id={request_id} - ordinal reference retrieval expansion raised (skipped): {ex}", "planner_bridge")
                retrieval_query_override = None
                relevant_memories_for_context = relevant_memories_early
                ordinal_targets = ()

        # Memory Topic Retention & Recall Reliability sprint (Phase 6) -
        # computed FIRST, ahead of Sprint 4's own single-slot branch below
        # (that branch's body is left completely unmodified - only WHEN it
        # fires changes here). Deliberately runs UNCONDITIONALLY (not
        # gated on `is_short_followup`) - see `memory_context.
        # select_topic_candidates()`'s own docstring for exactly why: a
        # turn classified `comparison`/`is_short_followup=True` (e.g.
        # "Yang tadi soal mic gimana?") can still carry its own real
        # residual word ("mic"), and trusting `is_short_followup` alone
        # would reproduce this sprint's own root-cause bug one layer
        # deeper (falling back to "whichever topic is most recent",
        # regardless of what the words actually name). This branch
        # instead matches by TOKEN OVERLAP against this conversation's
        # bounded topic HISTORY (`_topic_history`, plural - separate from
        # the single-slot `_active_topic` below) and naturally returns
        # nothing for a truly signal-less fragment ("terus?"), leaving the
        # branch below as the sole (correct, unmodified) handler for that
        # shape. Overlap-based matching is also what keeps Phase 5's
        # multi-topic safety guarantee: asking about "pompa" only
        # overlaps an aquascape entry, never an unrelated ESP32 entry, no
        # matter which was pushed more recently.
        topic_history_candidates: list = []
        # Sprint 50 (Runtime Observability) - two OBSERVABILITY-ONLY
        # local variables, read (never written to) by the telemetry
        # publish after this branch chain below. `_topic_decision`
        # defaults to "NO_CANDIDATE" and is only ever overwritten INSIDE
        # a branch's own already-existing success path (a pure additive
        # assignment statement, zero control-flow change) - so it always
        # honestly reflects which branch actually produced a candidate,
        # including the case where a branch's own try/except fired and
        # fell back to nothing. `_topic_relevance_check_result` mirrors
        # `is_active_topic_relevant_to_query()`'s own return value ONLY
        # when that function is actually evaluated (see the walrus
        # assignment inside the `elif` below) - `None` otherwise, never
        # a guessed default.
        _topic_decision: str = "NO_CANDIDATE"
        _topic_relevance_check_result: Optional[bool] = None
        try:
            topic_history = self._topic_history.get(_topic_key)
            topic_history_candidates = memory_context.select_topic_candidates(
                topic_history, text, is_short_followup,
            )
        except Exception as ex:
            log(f"request_id={request_id} - topic-history candidate selection raised (skipped): {ex}", "planner_bridge")
            topic_history_candidates = []

        if ordinal_targets:
            # Sprint 38's own ordinal resolution above already produced a
            # strictly more precise target than either branch below can -
            # deliberately a no-op here, never running BOTH an ordinal
            # resolution and a bag-of-terms topic match for the same turn
            # (same "skip the coarser branch once a precise one already
            # fired" discipline the topic-history/active-topic branches
            # below already established between themselves).
            _topic_decision = "ORDINAL_RESOLVED"
        elif topic_history_candidates:
            # A precise, content-matched topic was found - use ONLY this,
            # skipping the coarser recency-only branch below entirely.
            # Live reproduction (Phase 5's own 3-topic scenario: ESP32 ->
            # Aquascape -> "Yang tadi soal mic gimana?") proved running
            # BOTH branches together re-introduces this sprint's own root
            # contamination one layer up: the recency-only branch below
            # would unconditionally re-offer whichever topic was merely
            # MOST RECENT (Aquascape) alongside the correctly-matched one
            # (ESP32/mic) - technically not "wrong" (it was the literal
            # previous topic) but exactly the kind of unrelated-topic
            # contamination Phase 5 explicitly measures against. Skipping
            # the branch below here does NOT touch its own code/behavior
            # for the case it still owns (see the `elif` below).
            try:
                retrieval_query_override = memory_context.build_expanded_retrieval_text_from_history(
                    text, topic_history_candidates,
                )
                history_relevant_memories = memory_context.topic_history_to_relevant_memories(
                    topic_history_candidates, turn_id=request_id,
                )
                if history_relevant_memories:
                    relevant_memories_for_context = relevant_memories_early + history_relevant_memories
                _topic_decision = "MERGE_TOPIC_HISTORY"
            except Exception as ex:
                log(f"request_id={request_id} - topic-history retrieval expansion raised (skipped): {ex}", "planner_bridge")
                retrieval_query_override = None
                relevant_memories_for_context = relevant_memories_early
                topic_history_candidates = []
        elif (
            is_short_followup and active_topic_snapshot is not None and not active_topic_snapshot.is_stale
            and (
                reference_type not in ("comparison", "attribute_reference")
                # Sprint 50 (Runtime Observability) - a walrus assignment
                # ADDED around this pre-existing call, nothing else about
                # it changed: same function, same two arguments, same
                # short-circuit laziness (Python's own `or` still skips
                # this operand entirely when the left side is already
                # True, so `_topic_relevance_check_result` simply keeps
                # its `None` default for those turns - never a fabricated
                # value). Exists purely so the telemetry publish after
                # this branch chain can report what this guard actually
                # decided, without a second call or any behavior change.
                or (_topic_relevance_check_result := memory_context.is_active_topic_relevant_to_query(active_topic_snapshot, text, topic_history))
            )
        ):
            # Sprint 4's own branch, body UNMODIFIED - the correct,
            # already-tested fallback for a genuinely signal-less
            # elliptical fragment (the branch above found nothing to
            # match by content, so "whichever topic is most recent" is
            # exactly the right default here, same as before this sprint).
            #
            # Sprint 43 (Semantic Context Bridging, Phase 3/5) added ONE
            # new condition to the `elif` above:
            # `is_active_topic_relevant_to_query()`, gated to
            # `reference_type == "comparison"` ONLY. Live reproduction
            # (Phase 1, Scenarios D/E/G) found this branch firing even
            # when the CURRENT turn carried real, unmatched content that
            # plainly pointed elsewhere ("Aku baru beli headset baru buat
            # gaming." -> unrelated "Kalau upgrade PC-ku gimana ya?" wrongly
            # injected the headset topic) - a genuine, reproduced false
            # positive, not a hypothetical - and every one of the proven
            # false-positive/ambiguity cases classified as `"comparison"`
            # (`classify_reference_type()`'s "Kalau ... gimana?" pattern).
            # Writing the Phase 6 regression suite then caught a real
            # over-broadening: gating ALL `is_short_followup` types (not
            # just `"comparison"`) regressed seven pre-existing,
            # already-tested `test_memory_continuity.py` E2E cases whose
            # follow-ups ("other option?", "yang lain?", "kalau tanpa
            # itu?") are genuinely signal-less STRUCTURAL references
            # (`alternative_request`/`negation_of_current_option`/
            # `direct_reference`/etc.) with no topical words of their own
            # to evaluate relevance against in the first place - for those
            # types, unconditional recency was always correct and remains
            # so, untouched. Only `"comparison"`-classified turns (which
            # DO carry their own residual content, e.g. "upgrade"/"PC"/
            # "budget") are checked against the new guard; every other
            # `NEEDS_TOPIC_CONTEXT_TYPES` member falls through to the
            # unmodified pre-Sprint-43 unconditional path.
            #
            # Sprint 44 (Entity & Concept Continuity, Phase 7) - added
            # `"attribute_reference"` to the gated set alongside
            # `"comparison"`. Live cross-topic adversarial reproduction
            # (Phase 7's own 3-topic scenario: ESP32/INMP441, aquascape/
            # pompa, GPU/RTX3060 all live in the same bounded history,
            # then "Yang wireless?") found `attribute_reference` turns -
            # unlike the seven Sprint-43-era types this gate deliberately
            # excludes - DO carry their own real residual content (the
            # `_ATTRIBUTE_REFERENCE_CANDIDATE_RE` match itself, e.g.
            # "wireless" in "yang wireless?"), so they are not "genuinely
            # signal-less structural references" either - the same
            # relevance question Sprint 43 asked of `comparison` turns
            # applies here too. Confirmed via full regression (see Sprint
            # 44 change-impact doc) that this does NOT reproduce Sprint
            # 43's own seven-test regression, since those were tied to
            # `alternative_request`/`negation_of_current_option`/
            # `direct_reference` (still excluded, unchanged) rather than
            # `attribute_reference`.
            try:
                retrieval_query_override = memory_context.build_expanded_retrieval_text(text, active_topic_snapshot)
                active_topic_candidate = memory_context.active_topic_to_relevant_memory(active_topic_snapshot, turn_id=request_id)
                if active_topic_candidate is not None:
                    relevant_memories_for_context = relevant_memories_early + [active_topic_candidate]
                _topic_decision = "MERGE_ACTIVE_TOPIC"
            except Exception as ex:
                log(f"request_id={request_id} - active-topic retrieval expansion raised (skipped): {ex}", "planner_bridge")
                retrieval_query_override = None
                relevant_memories_for_context = relevant_memories_early
                active_topic_candidate = None
        else:
            # Temporal Memory & Timeline Awareness sprint (Sprint 41,
            # Phase 6) - LAST-RESORT fallback, deliberately positioned
            # after all three branches above and NOT gated on
            # `is_short_followup`. Phase 2's own root-cause finding: the
            # topic-history branch above is pure lexical overlap and
            # correctly returns nothing when a temporal QUERY uses
            # different wording than the ORIGINAL statement did
            # ("Sebelumnya aku pakai apa?" shares no token with "Aku
            # pakai RTX 3060 Ti."; "Sekarang aku pakai board apa?" shares
            # no token with "Sudah aku pindah ke ESP32-S3." and is also a
            # rich/non-followup turn, so the single-slot branch above
            # never even attempts it either) - both are genuine candidate-
            # ELIGIBILITY gaps, not classification or ranking bugs. This
            # branch closes that gap ONLY when the turn's own wording
            # unambiguously asks a current/historical/planned-state
            # question (see `memory_context.
            # select_temporal_fallback_candidate()`'s own docstring) and
            # only searches the bounded topic-history list already built
            # above - no new store, no new ranking system. It fires last
            # so a genuine lexical match (topic_history_candidates) or an
            # ordinal match always takes priority; this only activates
            # when neither of those found anything at all.
            try:
                topic_history = self._topic_history.get(_topic_key)
                temporal_fallback_entry = memory_context.select_temporal_fallback_candidate(topic_history, text)
                if temporal_fallback_entry is not None:
                    retrieval_query_override = memory_context.build_expanded_retrieval_text(text, temporal_fallback_entry)
                    active_topic_candidate = memory_context.active_topic_to_relevant_memory(temporal_fallback_entry, turn_id=request_id)
                    if active_topic_candidate is not None:
                        relevant_memories_for_context = relevant_memories_early + [active_topic_candidate]
                    _topic_decision = "MERGE_TEMPORAL_FALLBACK"
            except Exception as ex:
                log(f"request_id={request_id} - temporal fallback candidate retrieval raised (skipped): {ex}", "planner_bridge")
                retrieval_query_override = None
                relevant_memories_for_context = relevant_memories_early
                active_topic_candidate = None

        # Sprint 4, Phase 11 - bounded, non-persistent debug telemetry
        # (never logs raw conversation/reply text, only classification
        # results and bounded token sets - same "no raw conversation dump"
        # constraint as the state itself). Extended (Memory Topic
        # Retention sprint) with the topic-history candidate count.
        log(f"request_id={request_id} [MemoryContinuity] reference_type={reference_type} "
            f"is_short_followup={is_short_followup} "
            f"active_topic_terms={sorted(active_topic_snapshot.terms) if active_topic_snapshot else []} "
            f"topic_age={active_topic_snapshot.turns_since_active if active_topic_snapshot else None} "
            f"candidate_injected={active_topic_candidate is not None} "
            f"topic_history_candidates={len(topic_history_candidates)} "
            f"ordinal_targets={list(ordinal_targets)} "
            f"topic_decision={_topic_decision} "
            f"ambiguity_check_result={_topic_relevance_check_result}", "planner_bridge")

        if ordinal_targets:
            log(f"request_id={request_id} [ConversationReference] resolved ordinal target(s)="
                f"{list(ordinal_targets)} source=list_items", "planner_bridge")

        # Sprint 50 (Runtime Observability) - publishes this turn's
        # already-computed topic decision onto the Event Bus (same
        # OBSERVABILITY-ONLY discipline as the `memory_reference_classified`
        # publish above: bounded labels/counts only, never raw text, own
        # try/except, never able to break a turn). `ambiguity_refusal`
        # is a plain derived read (`_topic_relevance_check_result is
        # False`), not a second decision - mirrors `MemoryTurnTrace.
        # is_ambiguity_refusal` exactly so the live event stream and the
        # turn-trace inspector always agree.
        try:
            self._event_bus.publish(Event(type="memory_topic_decision", data={
                "request_id": request_id,
                "conversation_id": conversation_id,
                "topic_decision": _topic_decision,
                "active_topic_terms": sorted(active_topic_snapshot.terms) if active_topic_snapshot else [],
                "topic_age": active_topic_snapshot.turns_since_active if active_topic_snapshot else None,
                "topic_history_candidate_count": len(topic_history_candidates),
                "ordinal_target_count": len(ordinal_targets),
                "ambiguity_check_result": _topic_relevance_check_result,
                "ambiguity_refusal": _topic_relevance_check_result is False,
            }))
        except Exception:
            pass  # telemetry must never be able to break a turn

        # Memory & Voice Observability Dashboard sprint (Phase 2) - a
        # plain `dict` handed to `assemble_context(funnel=...)` as a
        # WRITE-ONLY observability tap (see that parameter's own
        # docstring) - never read by this method, never influences
        # anything below. `"query"` and `"topic_candidates"` are filled
        # here because they're caller-side counts `assemble_context()`
        # itself has no way to know (this turn's own existence is the
        # "query" count; `topic_history_candidates` was already computed
        # above, before `assemble_context()` is even called).
        _funnel: Dict[str, int] = {"query": 1, "topic_candidates": len(topic_history_candidates)}
        try:
            assembled_context = memory_context.assemble_context(
                text,
                memory_retriever=self.memory_retriever,
                get_manual_memories=memory.list_memories,
                verified_fact_store=self.memory_guard,
                relationship_state=None,
                precomputed_relevant_memories=relevant_memories_for_context,
                intent=query_intent,
                previous_topic_terms=previous_topic_terms,
                retrieval_query_override=retrieval_query_override,
                funnel=_funnel,
            )
            memory_context_block = assembled_context.render()
            # Memory Outcome Telemetry & Closed-Loop Learning sprint
            # (Step 3/4/5) - builds this turn's `MemoryTurnTrace` from
            # data already computed above (`relevant_memories_early` +
            # the real `assembled_context` this call just returned - no
            # second retrieval, no second ranking pass) via
            # `build_turn_trace()`, which ALSO resolves any selected
            # conflict-group joint note back to its real member ids (see
            # that function's own docstring) - the fix for a real gap the
            # PRIOR sprint's simpler set-comprehension had (it would have
            # recorded evidence against the non-existent synthetic
            # `"conflict:<group>"` id instead of the real members).
            # Best-effort/non-fatal, same as every other note in this
            # method: a failure here must never break the turn.
            try:
                _turn_trace = build_turn_trace(
                    request_id, relevant_memories_early, assembled_context, query_text=text,
                    retrieval_called=_retrieval_called,
                    query_intent=query_intent or "",
                    reference_type=reference_type,
                    is_short_followup=is_short_followup,
                    active_topic_snapshot=active_topic_snapshot,
                    topic_history=topic_history,
                    topic_history_candidates=topic_history_candidates,
                    funnel=_funnel,
                    topic_decision=_topic_decision,
                    ambiguity_check_result=_topic_relevance_check_result,
                )
                if _turn_trace.candidate_memory_ids:
                    memory.record_context_selection(_turn_trace.candidate_memory_ids, _turn_trace.selected_memory_ids)
                # Bounded, session-scoped, REPLACED (never appended) each
                # turn - see `self._last_turn_trace`'s own docstring for
                # why this can never grow into an unbounded log.
                _trace_key = conversation_id or self._ENV_CONFIRMATION_KEY
                self._last_turn_trace[_trace_key] = _turn_trace
                while len(self._last_turn_trace) > self._last_turn_trace_max:
                    oldest = next(iter(self._last_turn_trace))
                    self._last_turn_trace.pop(oldest, None)
                # Memory & Voice Observability Dashboard sprint - see
                # `self._turn_trace_history`'s own docstring above for why
                # this is a SEPARATE, additive append (cross-conversation
                # browsing) rather than a change to the dict write just
                # above (`_last_turn_trace`'s own one-per-conversation
                # contract is unchanged). `deque(maxlen=...)` silently
                # evicts the oldest entry once full - no manual bound
                # check needed here, unlike the dict above.
                self._turn_trace_history.append((_trace_key, _turn_trace))
                # Sprint 50 (Runtime Observability) - the THIRD and last
                # new per-turn publish, straight from the `_turn_trace`/
                # `_funnel` this method already built above - never a
                # second selection pass. Bounded counts only (never memory
                # ids' underlying text, never the prompt).
                try:
                    self._event_bus.publish(Event(type="memory_selection_summary", data={
                        "request_id": request_id,
                        "conversation_id": conversation_id,
                        "funnel": dict(_funnel),
                        "candidate_memory_count": len(_turn_trace.candidate_memory_ids),
                        "selected_memory_count": len(_turn_trace.selected_memory_ids),
                    }))
                except Exception:
                    pass  # telemetry must never be able to break a turn
            except Exception as ex:
                log(f"request_id={request_id} - memory context selection tracking failed (skipped): {ex}", "planner_bridge")
        except Exception as ex:
            log(f"request_id={request_id} - memory context assembly failed (skipped): {ex}", "planner_bridge")
            memory_context_block = ""
        if memory_context_block:
            notes.append(memory_context_block)

        # Emotion Engine sprint - bounded, uncertainty-hedged emotional-
        # context note (see luno/emotion_engine.py's own docstring for
        # the full design). Placed here deliberately: AFTER persona/
        # memory/verified-facts/session-summary/AppNotFound notes (this
        # is a soft behavioral lean, never allowed to compete with or
        # precede the honest, factual notes above it) and BEFORE the
        # language/character-reminder block right below (which stays the
        # true FINAL instruction, unchanged) - matching the ordering the
        # Emotion Engine sprint brief itself suggests, cross-checked
        # against this method's ACTUAL existing note order rather than
        # assumed. `build_emotional_context_prompt()` itself returns ""
        # (adds nothing) whenever the estimate isn't confident enough or
        # resolves to a no-op response policy - most turns add nothing
        # here at all, same "only when actually relevant" shape as
        # `memory_block` just above.
        try:
            _emotion_state = self.emotion_tracker.current()
            _emotion_policy = derive_response_policy(_emotion_state)
            emotional_context_block = build_emotional_context_prompt(_emotion_state, _emotion_policy)
        except Exception as ex:
            log(f"request_id={request_id} - emotional context prompt build failed (skipped): {ex}", "planner_bridge")
            emotional_context_block = ""
        if emotional_context_block:
            notes.append(emotional_context_block)

        # Bug fix (language leakage into real LLM replies, e.g. "Lampu utama
        # sudah dimatikan. There, happy now?" - Indonesian device-status
        # sentence followed by an English persona catchphrase): `config.
        # LUNO_LANGUAGE` (env var, e.g. LUNO_LANGUAGE=english in .env) is
        # the project's own pre-existing "force one language regardless of
        # what the user typed" knob - already implemented and already
        # correctly wired into the legacy `luno/main.py` (see that file's
        # own `build_system_prompt()`), but - same bug class already noted
        # above for `persona_block` - NEVER wired into THIS bridge, the one
        # `luno/bootstrap/modules.py` actually loads for the real Sprint 6+
        # Runtime (`python main.py` at the project root). Without this, the
        # LLM had nothing telling it to override the language it naturally
        # drifts toward (mirroring the user's own message - Indonesian in,
        # Indonesian out for at least PART of the reply), while the persona
        # block above still supplied English catchphrases/example lines
        # verbatim - producing exactly this kind of mixed-language reply.
        # Deliberately appended LAST (not merged into the persona block
        # above it) and phrased as a hard override: a long persona block
        # full of English character text sitting right above this can still
        # pull a smaller/weaker LLM's own language "gravity" the wrong way
        # without an explicit, final, unambiguous instruction - mirrors
        # `luno/main.py`'s own comment on this exact ordering choice.
        # Bug fix (persona feels weak in ordinary chat): this note used to
        # end on ONLY a language instruction ("Keep it casual and brief.
        # No emojis.") with zero mention of character - since this is the
        # LAST thing in the whole system prompt, an LLM's own recency bias
        # tends to weight it more heavily than the persona block several
        # notes earlier, so replies drifted toward a generic "casual
        # assistant" tone instead of actually staying in character. Adding
        # a short character-reinforcement clause HERE (not just relying on
        # the persona block up top) fixes that without touching the
        # language-override logic itself or persona.json's content.
        _persona_name = (PERSONA.get("name") or "Luno").strip() or "Luno"
        _character_reminder = (
            f"Still stay fully in character as {_persona_name} while doing this - dry humor, "
            f"understated, quietly attentive, not a generic assistant tone."
        )
        if legacy_config.LUNO_LANGUAGE != "auto":
            notes.append(
                f"IMPORTANT, FINAL INSTRUCTION - language: your ENTIRE reply MUST be written in "
                f"{legacy_config.LUNO_LANGUAGE}, no matter what language the user's message is in, "
                f"and even though your character background/traits/example lines above may be "
                f"written in a different language - those are personality flavor only, NOT a "
                f"language instruction. Never mix languages within one reply, not even for a "
                f"single short phrase or a device-status confirmation. Translate the spirit/tone, "
                f"not the literal words. Keep it casual and brief. No emojis. {_character_reminder}"
            )
        else:
            notes.append(
                "IMPORTANT, FINAL INSTRUCTION - language: reply in the SAME language the user just "
                "used in their message (match them, don't default to English or drift into a "
                "different language mid-reply). This overrides the language of any character/"
                f"background text above. Keep it casual and brief. No emojis. {_character_reminder}"
            )

        # Intelligent AI Routing Engine sprint - decide intent/knowledge
        # source/provider BEFORE building the final system_note (so a
        # Tavily context note, if the Decision Engine triggers one, can
        # be appended into `notes` exactly like every other block above -
        # persona/memory/verified-action notes - never a special-cased
        # prompt path). `real_tasks`/`relevant_memories_early` were
        # already computed above for other reasons - nothing here issues
        # a new query to anything; this only ever READS what this turn
        # already fetched (see `luno/routing/decision_engine.py`'s own
        # docstring's "Safety rules").
        completed_real_tasks = [t for t in real_tasks if t.status.value == "completed"]
        # Efficient LLM Classifier sprint: on a CONFIRMED routing-
        # classification reply, re-decide() using the ORIGINAL utterance
        # (never the bare "iya"/"tidak" reply itself) and skip
        # re-classification entirely via `forced_intent` - a plain
        # function argument, scoped to this one call only (see
        # `DecisionEngine.decide()`'s own docstring for why this can
        # never become a persistent/global bypass). A normal turn (no
        # pending confirmation) passes neither and behaves exactly as
        # before this sprint.
        decision = self.decision_engine.decide(
            request_id=request_id,
            text=routing_confirm_override or text,
            conversation_id=conversation_id,
            relevant_memories=relevant_memories_early,
            world_model_entities=self.world_model.all_entities(),
            tool_state_hit=bool(completed_real_tasks),
            tool_state_detail=", ".join(f"{t.tool_call.tool}.{t.tool_call.action}" for t in completed_real_tasks) or None,
            needs_tools=bool(real_tasks),
            forced_intent=routing_confirm_forced_intent,
        )
        self._event_bus.publish(RoutingDecisionMade(data=decision.to_dict()))
        if decision.needs_confirmation:
            # Medium-confidence classifier result - request confirmation
            # instead of guessing. Deterministic/template prompt (see
            # `ConfirmationHandler.prompt_for()`'s own docstring - NO
            # extra LLM call to phrase this), instructed to the ALREADY-
            # HAPPENING conversational LLM call for this turn via the
            # SAME `notes`-injection convention every other pre-fetched
            # context block (vision/screen/environmental-intent) already
            # uses - see that block's own comment for the precedent this
            # mirrors exactly.
            pending = self.confirmation_handler.request_confirmation(
                request_id=request_id, conversation_id=conversation_id, text=text,
                intent=decision.primary_intent.value, confidence=decision.classifier_confidence or 0.0,
            )
            notes.append(
                f'Classification of the user\'s last message is uncertain ("{text}"). Ask them, naturally '
                f'and briefly, exactly this question (translate/adapt the phrasing/tone to match your own but '
                f'keep the meaning): "{self.confirmation_handler.prompt_for(pending)}" - do NOT claim you '
                f'already did anything, you are only ASKING.'
            )
        log(
            f"request_id={request_id} - routing decision: intent={decision.primary_intent.value} "
            f"complexity={decision.complexity.value} knowledge_source={decision.knowledge_source.value} "
            f"provider_alias={decision.provider_alias}->{decision.provider} needs_internet={decision.needs_internet} "
            f"affinity_applied={decision.affinity_applied}",
            "planner_bridge",
        )
        if decision.search_context:
            notes.append(decision.search_context)

        system_note = "\n\n".join(notes) if notes else None

        # Sprint 3 - classify BEFORE publishing NeedLLMResponse, so
        # BargeInModule already knows this request_id's SpeakingMode by
        # the time llm_started fires (see luno/barge_in/manager.py).
        # Rule-based only: matched against the user's own request text
        # plus whichever tool(s) the Planner actually ran for this turn
        # - never an LLM call.
        classify_text = f"{text} " + " ".join(t.tool_call.tool for t in plan.tasks)
        mode = classify_speaking_mode(text=classify_text, emergency_active=False, config=self._speaking_mode_config)
        self._event_bus.publish(Event(type="speaking_mode_assigned", data={"request_id": request_id, "mode": mode.value}))

        messages = [{"role": "user", "content": text}]
        log(f"request_id={request_id} plan_id={plan.id} - publishing NeedLLMResponse "
            f"(request_id NOT plan_id; mode={mode.value})", "planner_bridge")
        self._remember_pending_turn(request_id, text, conversation_id)
        llm_request_data = {
            "messages": messages,
            "system_prompt": system_note,
            "stream": True,
            "request_id": request_id,
            "conversation_id": conversation_id,
            "correlation_id": request_id,
        }
        # Optional per-request provider/model hint (see
        # `luno/adapters/llm_manager.py`'s `_handle_need_llm_response` -
        # absent/None leaves that adapter's own configured default/
        # fallback order completely untouched, e.g. when
        # ENABLE_AUTO_ROUTING=false or an unrecognized alias resolved to
        # `(provider, None)`).
        if decision.provider:
            llm_request_data["provider"] = decision.provider
        if decision.model:
            llm_request_data["model"] = decision.model
        # OpenAI-Primary/DeepSeek-Fallback sprint - reasoning_effort only
        # ever means anything to the `openai` provider client (see
        # `OpenAIProvider._extra_payload_fields()`); every other provider
        # ignores unknown metadata keys, so this is safe to always attach
        # when the Decision Engine actually chose a route.
        if decision.reasoning_effort:
            llm_request_data["metadata"] = {"reasoning_effort": decision.reasoning_effort}
        self._event_bus.publish(NeedLLMResponse(data=llm_request_data))


# ============================================================================
# Module: Behavior Tree bridge
# ============================================================================

class BehaviorTreeModule(Module):
    """Wraps `luno.behavior_tree.BehaviorTree` + `Scheduler` exactly the
    way `luno.core`'s OWN module docstring shows as the worked example for
    "wrapping an existing standalone package as a Module" - nothing here
    is new architecture, it's that example filled in for real. The
    `Handlers` implementations below are the ONLY code in this file that
    "speaks" to OpenRouter/Tool Manager conceptually, and every single one
    of them does so exclusively by publishing an Event and (when a
    synchronous return value is unavoidable, because that's `Handlers`'
    existing, unmodified contract) waiting on the correlated response."""

    name = "behavior_tree"
    dependencies: List[str] = ["planner", "vision_memory"]

    def __init__(self, llm_timeout_s: float = 45.0) -> None:
        self.bb = Blackboard()
        self.handlers = Handlers(
            speak=self._speak,
            generate_reply=self._generate_reply,
            execute_tool=self._execute_tool,
            set_avatar_emotion=lambda emotion: None,
        )
        self.tree = BehaviorTree(blackboard=self.bb, handlers=self.handlers)
        self.scheduler: Optional[BTScheduler] = None
        self._event_bus = None
        self.llm_timeout_s = llm_timeout_s
        self.conversation_id = generate_id("conv")
        # Sprint 3: the OLD suppression-flag trick (_suppress_speak_text /
        # _suppress_speak_until) only worked because Fish Audio's copy and
        # this module's copy of the reply were byte-identical raw text. It
        # is gone now that `_speak()` publishes a NORMALIZED `SpeakRequest`
        # instead of a second `AssistantResponse` - the console's adapter
        # wiring already keeps Fish Audio off `assistant_response`
        # entirely (see RuntimeDemoConsole.__init__), so there is no
        # double-speak left to suppress. `_last_turn_request_id` instead
        # lets `_speak()` tag its `SpeakRequest` with the SAME request_id
        # `_generate_reply()` used for this turn's `user_utterance` /
        # `assistant_response`, so anything correlating on request_id
        # (barge-in, wake_session, console dedup) sees one consistent id
        # per turn.
        self._last_turn_request_id: Optional[str] = None
        # Chat/Voice Dual Output sprint - the SAME per-turn depth
        # `PlannerBridgeModule` already resolved once (published as
        # `response_depth_assigned`, correlated by request_id - see that
        # module's own comment) is captured here by `_generate_reply()`
        # and consumed by `_speak()` moments later, exactly mirroring how
        # `_last_turn_request_id` itself already threads the turn's
        # request_id across those same two methods. Never recomputed -
        # this module never imports/calls `compute_response_policy()`.
        self._last_turn_depth: Optional[str] = None
        # Sprint 3 (Production-Safe LLM -> TTS Streaming Activation) -
        # sibling of `_last_turn_depth` above, threading `ResponsePolicy.
        # explicit` the SAME way - see `response_depth_assigned`'s own
        # publish site for why this was added (an explicit "jelaskan
        # semuanya secara detail" request was silently still being
        # compressed via the real event path without it).
        self._last_turn_explicit: bool = False
        # Voice Output Mode sprint - sibling of `_last_turn_depth`/
        # `_last_turn_explicit` above, threading `voice_output_mode` the
        # SAME way (via `response_depth_assigned`, captured by
        # `_generate_reply()`'s own `_on_depth` closure, consumed by
        # `_speak()` moments later, reset to `None` right after each read
        # exactly like its two siblings).
        self._last_turn_voice_output_mode: Optional[str] = None
        # Voice Output Mode sprint (Phase 6 - visibility) - UNLIKE
        # `_last_turn_voice_output_mode` above (reset to `None` the
        # instant `_speak()` reads it, purely a one-shot per-turn relay),
        # this copy is deliberately NOT reset - `status_snapshot()` below
        # exposes it so the console's status panel (or a test) can always
        # answer "what mode did Luno last actually speak in", not just
        # "mid-turn, nothing spoken yet". `None` until the first turn ever
        # speaks.
        self._last_spoken_voice_output_mode: Optional[str] = None
        self.speak_log: Deque[str] = deque(maxlen=50)
        # Bug fix (wake session / barge-in integration): there is a real
        # gap between the LLM finishing (BargeInModule's `thinking` flag
        # going False) and Fish Audio actually starting to play (the
        # reply still has to pass through the text normalizer and
        # `_speak()` below) - long enough, against a real TTS backend,
        # for a spoken interrupt to land in it. `BargeInModule` already
        # tolerates this window (see its own `_speech_pending_deadline`)
        # and still publishes `cancel_llm_request` for the turn even
        # though the stream itself already finished - which OpenRouterAdapter
        # always answers with `llm_cancelled` regardless (a no-op
        # internally, but the event still fires). This module listens
        # for that `llm_cancelled` purely to avoid actually SPEAKING a
        # reply that was already interrupted before `_speak()` ever ran -
        # bounded so it can never grow unboundedly across a long session.
        self._cancelled_request_ids: Deque[str] = deque(maxlen=64)
        # LLM Streaming -> Real-Time Speech Pipeline sprint - lazily built
        # in `bind_event_bus()` below, and ONLY when
        # `legacy_config.ENABLE_LLM_TTS_STREAMING` is on (default: off -
        # see that constant's own docstring in `luno/config.py`). When
        # `None` (the default/disabled case), every method below that
        # touches it is a guarded no-op, so `_generate_reply()`/`_speak()`
        # behave EXACTLY as they did before this sprint.
        self._streaming_coordinator: Optional[StreamingSpeechCoordinator] = None

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus
        if legacy_config.ENABLE_LLM_TTS_STREAMING and event_bus is not None:
            self._streaming_coordinator = StreamingSpeechCoordinator(
                event_bus,
                max_pending_chunks=legacy_config.LLM_TTS_STREAM_MAX_PENDING_CHUNKS,
                max_buffer_chars=legacy_config.VOICE_CHUNK_MAX_CHARS,
            )

    def start(self) -> None:
        self.scheduler = BTScheduler(self.tree, self.bb, tick_interval_s=0.15)
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler:
            self.scheduler.stop(wait=True)

    def health(self) -> ModuleHealthStatus:
        stalled = self.scheduler is not None and self.scheduler.last_tick_duration_s > 2.0
        return ModuleHealthStatus(healthy=not stalled, stalled=stalled)

    # -- events in --------------------------------------------------------

    def on_event(self, event: Event) -> None:
        # Sprint 2: the routing table no longer sends raw
        # SpeechRecognized here directly (SessionManagerModule gates it
        # first - see RuntimeDemoConsole's routes) - the live path is
        # CONVERSATION_SPEECH_EVENT, published only once a session is
        # genuinely awake. Both are still handled identically here so
        # this module keeps working unchanged if something ever routes
        # SpeechRecognized straight through again (e.g. sleep_enabled=False).
        if event.type in (SpeechRecognized.EVENT_TYPE, CONVERSATION_SPEECH_EVENT):
            text = event.get("text", "")
            with self.bb.lock:
                self.bb.conversation.pending_user_text = text
            self.bb.push_event(f"heard: {text!r}")
        elif event.type in ("smoke_detected", "fire_alarm"):
            self.bb.push_ha_event(HAEvent(entity_id=event.type, kind=event.type, severity=HAEventSeverity.EMERGENCY))
        elif event.type in ("battery_low", "internet_lost"):
            self.bb.push_ha_event(HAEvent(entity_id=event.type, kind=event.type, severity=HAEventSeverity.CRITICAL))
        elif event.type == "door_open":
            self.bb.push_ha_event(HAEvent(entity_id="front_door", kind="door_opened", severity=HAEventSeverity.NORMAL))
            with self.bb.lock:
                self.bb.room.door_closed = False
        elif event.type in ("motion", "person_detected"):
            with self.bb.lock:
                self.bb.user.present = True
                self.bb.user.last_seen_at = self.bb.now
            self.bb.push_visual_event_safe(event.type)
        elif event.type == "wake_word":
            with self.bb.lock:
                self.bb.wake_word_detected = True
                self.bb.user.present = True
        elif event.type == "planner_failed":
            self.bb.record_error(f"planner_failed: {event.get('error', '?')}")
        elif event.type == "tool_timeout":
            self.bb.record_error("tool_timeout")
        elif event.type in ("stop_requested", "cancel_requested"):
            self.bb.interrupt_requested = True
        elif event.type == "conversation_reset":
            with self.bb.lock:
                self.bb.conversation = type(self.bb.conversation)()
        elif event.type == "llm_cancelled":
            # Bug fix (wake session / barge-in integration): remember
            # this turn was cancelled so `_speak()` can drop its reply
            # if `_generate_reply()` had already returned it by the time
            # the cancellation arrived (the thinking->speaking gap - see
            # this module's own __init__ docstring note).
            rid = event.get("request_id")
            if rid:
                self._cancelled_request_ids.append(rid)
                # LLM Streaming -> Real-Time Speech Pipeline sprint - stop
                # the streaming coordinator from producing/publishing any
                # MORE SpeechChunks for this turn too (Phase 3: "cancel
                # pending text chunking"). Idempotent, no-op if this
                # request_id was never streamed or already finished/
                # cancelled. Does not itself touch Fish Audio playback -
                # that is already, separately, driven by `StopPlayback`/
                # `llm_cancelled` reaching `FishAudioAdapter` directly via
                # the existing adapter-event routing, unchanged.
                if self._streaming_coordinator is not None:
                    self._streaming_coordinator.cancel_turn(rid)

    # -- Handlers: publish-and-wait, never a direct call --------------------

    def _generate_reply(self, user_text: str, context: Dict[str, Any]) -> str:
        if self._event_bus is None:
            return "(no event bus bound - cannot reach OpenRouter)"
        request_id = generate_id("turn")
        assistant = wait_for_event(
            self._event_bus, "assistant_response",
            lambda e: e.get("request_id") == request_id, 0,  # subscribed below before publish
        ) if False else None  # placeholder to keep lints happy; real wait below

        done = threading.Event()
        box: Dict[str, Event] = {}

        def _on_ok(e: Event) -> None:
            if e.get("request_id") == request_id:
                box["ok"] = e
                done.set()

        def _on_err(e: Event) -> None:
            if e.get("request_id") == request_id:
                box["err"] = e
                done.set()

        # Sprint 3 (Production-Safe LLM -> TTS Streaming Activation),
        # Phase 10 gate - PRE-EXISTING gap found and fixed: this `done`
        # Event previously only woke on `assistant_response`/`llm_error`,
        # never on `llm_cancelled` - so a barge-in that landed WHILE the
        # LLM was still actively generating (not yet finished) left this
        # turn's `_generate_reply()` call, and therefore this
        # single-threaded module's own event processing, blocked until
        # `self.llm_timeout_s` (45s default) before the NEXT utterance
        # could even be forwarded to the planner. This affected BOTH the
        # streaming and non-streaming paths identically (the gap is here,
        # in the shared wait, not in the streaming coordinator). `on_event()`'s
        # own `llm_cancelled` handling (elsewhere in this class) already
        # appends this request_id to `_cancelled_request_ids` on the SAME
        # event, so `_speak()`'s existing suppression mechanism already
        # ensures whatever this method returns below is never actually
        # spoken - this fix only makes the WAIT itself unblock promptly.
        def _on_cancel(e: Event) -> None:
            if e.get("request_id") == request_id:
                box["cancelled"] = e
                done.set()

        # Chat/Voice Dual Output sprint - opportunistically captures the
        # SAME depth `PlannerBridgeModule` already resolved once for this
        # request_id (see that module's `response_depth_assigned` publish,
        # right after its own once-per-turn `compute_response_policy()`
        # call). Never gates `done.wait()` below - this turn still
        # completes/times out purely on `assistant_response`/`llm_error`,
        # exactly as before; a missing depth event (e.g. a caller that
        # invoked `_apply_device_context()` directly, bypassing the real
        # event bus) just leaves `box["depth"]` unset, and `_speak()`
        # below already defaults safely when that happens.
        def _on_depth(e: Event) -> None:
            if e.get("request_id") == request_id:
                box["depth"] = e.get("depth")
                # Sprint 3 - see `response_depth_assigned`'s own publish
                # site for why this is needed: without it, an explicit
                # "jelaskan semuanya secara detail" request still lost
                # content to budget-based compression in the REAL event
                # path (confirmed empirically), even though this exact
                # case is unit-tested and passes when `build_dual_response()`
                # is called directly with a full `ResponsePolicy` object.
                box["explicit"] = e.get("explicit", False)
                # Voice Output Mode sprint - sibling capture, same event,
                # same closure, same reasoning as `explicit` immediately
                # above.
                box["voice_output_mode"] = e.get("voice_output_mode")

        sub_ok = self._event_bus.subscribe("assistant_response", _on_ok)
        sub_err = self._event_bus.subscribe("llm_error", _on_err)
        sub_cancel = self._event_bus.subscribe("llm_cancelled", _on_cancel)
        sub_depth = self._event_bus.subscribe("response_depth_assigned", _on_depth)
        try:
            # LLM Streaming -> Real-Time Speech Pipeline sprint - must be
            # started BEFORE `user_utterance` is published (same ordering
            # requirement `sub_ok`/`sub_err`/`sub_depth` above already
            # follow), so no `llm_streaming`/`llm_chunk` for this
            # request_id can possibly arrive before the coordinator is
            # listening. No-op when streaming is disabled
            # (`self._streaming_coordinator is None`).
            if self._streaming_coordinator is not None:
                self._streaming_coordinator.start_turn(request_id, self.conversation_id)
            self._event_bus.publish(Event(type="user_utterance", data={
                "text": user_text, "request_id": request_id,
                "conversation_id": self.conversation_id, "context": context,
            }))
            got = done.wait(self.llm_timeout_s)
        finally:
            self._event_bus.unsubscribe(sub_ok)
            self._event_bus.unsubscribe(sub_err)
            self._event_bus.unsubscribe(sub_cancel)
            self._event_bus.unsubscribe(sub_depth)

        if not got:
            return "Sorry, that took too long - I gave up waiting for a reply."
        if "cancelled" in box:
            # Sprint 3 - see `_on_cancel()`'s own docstring above. The
            # returned text is never actually spoken (`_speak()`'s
            # existing `_cancelled_request_ids` suppression handles that,
            # exactly as it already does for the "cancelled after
            # `_generate_reply()` already returned" race) - still set the
            # SAME turn-correlation bookkeeping the other early-return
            # branches already do, so `_speak()` can find this exact
            # request_id in `_cancelled_request_ids` rather than falling
            # back to a fresh, non-matching id.
            self._last_turn_request_id = request_id
            self._last_turn_depth = box.get("depth")
            self._last_turn_explicit = bool(box.get("explicit", False))
            self._last_turn_voice_output_mode = box.get("voice_output_mode")
            # Voice Output Mode sprint (Phase 6) - set HERE, not inside
            # `_speak()`, because `_speak()` returns early (never reaches
            # its own `build_dual_response()` call) whenever this turn was
            # already fully spoken incrementally via
            # `StreamingSpeechCoordinator` (streaming defaults ON - see
            # `luno.config.ENABLE_LLM_TTS_STREAMING`) - `_generate_reply()`
            # itself is the one call site guaranteed to run for BOTH the
            # streaming and non-streaming path.
            self._last_spoken_voice_output_mode = resolve_voice_output_mode(box.get("voice_output_mode"))
            return "(cancelled by barge-in during generation)"
        if "err" in box:
            # LLM Streaming -> Real-Time Speech Pipeline sprint bug fix:
            # `_last_turn_request_id` used to only be set on the SUCCESS
            # path below, so `_speak()`'s apology call for THIS request_id
            # would fall back to a brand-new `generate_id("say")` instead -
            # harmless before streaming existed (it only affected the
            # apology SpeakRequest's own correlation id), but with
            # streaming it meant `_speak()` could never correlate back to
            # THIS request_id's `StreamingSpeechCoordinator` turn state
            # (already marked `failed` by `_on_error()` - see that
            # method's own docstring), so `forget_turn()` was never called
            # for it - a real state leak on the failure-after-partial-text
            # path. Setting it here (for BOTH streaming and non-streaming)
            # closes that leak and, as a side benefit, makes the apology's
            # own SpeakRequest correctly correlate to the turn that failed.
            self._last_turn_request_id = request_id
            self._last_turn_depth = box.get("depth")
            self._last_turn_explicit = bool(box.get("explicit", False))
            self._last_turn_voice_output_mode = box.get("voice_output_mode")
            # Voice Output Mode sprint (Phase 6) - set HERE, not inside
            # `_speak()`, because `_speak()` returns early (never reaches
            # its own `build_dual_response()` call) whenever this turn was
            # already fully spoken incrementally via
            # `StreamingSpeechCoordinator` (streaming defaults ON - see
            # `luno.config.ENABLE_LLM_TTS_STREAMING`) - `_generate_reply()`
            # itself is the one call site guaranteed to run for BOTH the
            # streaming and non-streaming path.
            self._last_spoken_voice_output_mode = resolve_voice_output_mode(box.get("voice_output_mode"))
            return f"Sorry, I ran into a problem: {box['err'].get('error', 'unknown error')}"
        reply = box["ok"].get("text", "")
        # `assistant_response` (published by OpenRouterAdapter above) is
        # the raw conversation-record copy - history/context/display all
        # want the original wording. It no longer reaches Fish Audio in
        # this console (see the custom `console_adapter_mapping` in
        # RuntimeDemoConsole.__init__). The Behavior Tree's own
        # `_speak_and_finish` is about to call `_speak()` with this same
        # reply text next; remember this turn's request_id (and its
        # already-resolved depth) so `_speak()` can tag its (separate,
        # normalized) `SpeakRequest` with it.
        self._last_turn_request_id = request_id
        self._last_turn_depth = box.get("depth")
        self._last_turn_explicit = bool(box.get("explicit", False))
        self._last_turn_voice_output_mode = box.get("voice_output_mode")
        self._last_spoken_voice_output_mode = resolve_voice_output_mode(box.get("voice_output_mode"))
        return reply

    def _speak(self, text: str) -> None:
        request_id = self._last_turn_request_id or generate_id("say")
        self._last_turn_request_id = None
        depth = self._last_turn_depth
        self._last_turn_depth = None
        explicit = self._last_turn_explicit
        self._last_turn_explicit = False
        voice_output_mode = self._last_turn_voice_output_mode
        self._last_turn_voice_output_mode = None
        # LLM Streaming -> Real-Time Speech Pipeline sprint - if this
        # turn's reply was ALREADY fully, successfully spoken incrementally
        # while the LLM was still generating (via `StreamingSpeechCoordinator`/
        # `SpeakStreamChunk`), do NOT publish a second, whole-response
        # `SpeakRequest` here - that would be two audio paths speaking the
        # SAME turn (Phase 9's own explicit "jangan membuat dua jalur
        # audio yang dapat berbicara bersamaan" rule). Chat's own
        # `AssistantResponse` (published earlier by the LLM adapter,
        # carrying the full accumulated text) is completely unaffected -
        # this only ever skips the VOICE side. When streaming is disabled,
        # was never started for this turn, failed, or was cancelled
        # partway, `is_turn_streamed_and_completed()` returns False and
        # this method proceeds exactly as it always has (byte-identical
        # to pre-streaming behavior).
        if self._streaming_coordinator is not None:
            # Closes a real race: `assistant_response`/`llm_error` (which
            # `_generate_reply()` waits on, right above this call) and
            # `llm_finished`/`llm_error` (which the coordinator's own
            # `_on_finished()`/`_on_error()` react to, to flush the final
            # buffered sentence) are two INDEPENDENT subscriptions of
            # events published back-to-back for the same completion - see
            # `StreamingSpeechCoordinator.wait_until_settled()`'s own
            # docstring. Bounded (default 2s) and a no-op in the normal
            # case where the turn already settled by the time we get here.
            self._streaming_coordinator.wait_until_settled(request_id)
            if self._streaming_coordinator.is_turn_streamed_and_completed(request_id):
                self._streaming_coordinator.forget_turn(request_id)
                self.speak_log.append(f"(already spoken via LLM streaming) {text}")
                return
            self._streaming_coordinator.forget_turn(request_id)
        # Bug fix (wake session / barge-in integration): if this exact
        # turn was already cancelled (barge-in caught an interrupt in the
        # thinking->speaking gap, AFTER `_generate_reply()` had already
        # returned this text but BEFORE we got here), do not speak it -
        # the user already heard "Okay."/"Sure." (or "Okay, cancelled.")
        # from BargeInModule's own ack. Still logged to `speak_log` for
        # /debug visibility, just never published as a SpeakRequest.
        if request_id in self._cancelled_request_ids:
            self.speak_log.append(f"(suppressed - turn {request_id} was cancelled) {text}")
            return
        self.speak_log.append(text)
        if self._event_bus is None:
            return
        # Chat/Voice Dual Output sprint - ONE reasoning response (`text`,
        # already decided by `_generate_reply()` above) is adapted into
        # separate Chat/Voice presentations here. Chat's own consumer
        # (`assistant_response`, published earlier by the LLM adapter)
        # already carries the untouched raw text - nothing about that
        # path changes. Only what gets SPOKEN changes: previously a
        # bare `normalize_for_speech(text)` call; now
        # `build_dual_response()` (which calls that SAME function
        # internally - see luno/response_output.py - plus, for a
        # DETAILED turn only, bounded priority-based sentence selection).
        # `depth`/`explicit` are the SAME values `PlannerBridgeModule`
        # resolved once this turn (see `_generate_reply()`'s
        # `response_depth_assigned` capture above) - this call never
        # re-classifies depth itself. Sprint 3: wrapped in a minimal
        # `ResponsePolicy` (rather than passing `depth` as a bare string)
        # so `build_dual_response()`'s own "explicit DETAILED skips
        # compression entirely" rule actually applies on the real event
        # path - see `response_depth_assigned`'s own publish site for why
        # this was previously silently lost.
        resolved_policy = ResponsePolicy(depth=depth or DEPTH_NORMAL, score=0, reasons=[], explicit=explicit)
        dual = build_dual_response(
            text, resolved_policy, request_id=request_id,
            max_chunk_chars=legacy_config.VOICE_CHUNK_MAX_CHARS,
            voice_output_mode=voice_output_mode,
        )
        # Voice Output Mode sprint (Phase 6) - sticky, non-resetting copy
        # for `status_snapshot()` - see `_last_spoken_voice_output_mode`'s
        # own docstring in `__init__`. Reads `dual.voice_output_mode`
        # (the RESOLVED mode `build_dual_response()` actually used - see
        # that field's own docstring) rather than the local
        # `voice_output_mode` variable, so an invalid/`None` input is
        # reflected here as whatever it actually resolved to, never a
        # raw/unresolved value.
        self._last_spoken_voice_output_mode = dual.voice_output_mode
        # TTS Chunking/Streaming sprint - `dual.voice_chunks` is the SAME
        # sentence selection `dual.voice_text` was joined from (see
        # luno/response_output.py's own docstring), just grouped into
        # playback-sized pieces so FishAudioAdapter can start speaking
        # chunk 1 without waiting for the whole reply to be synthesized.
        # Wrapped defensively: if chunk computation ever produced
        # something unusable (empty list for non-empty voice_text - should
        # never happen given `voice_chunks` is derived from the exact same
        # `selected` sentences as `voice_text`, but this is the sprint's
        # own required fallback-to-old-mechanism safety net), `chunks` is
        # simply omitted from the payload - `FishAudioAdapter._play()`
        # then derives a single chunk from `text` itself, i.e. the OLD,
        # pre-chunking behavior, unchanged.
        #
        # TTS Chunk Queue & Cancellation sprint - `dual.voice_chunks`/
        # `dual.voice_chunks_raw` (already-computed TEXT, unchanged from
        # the prior sprint) are wrapped into the correlation-aware
        # `SpeechChunk` contract here (`luno/speech_chunk.py`) - chunk_id/
        # request_id/conversation_id/sequence/total/is_final - so
        # `FishAudioAdapter` can log/correlate/validate per chunk. This
        # ONLY attaches identity on top of already-segmented text; no
        # second text-splitting pass happens here.
        payload = {
            "text": dual.voice_text, "raw_text": text, "request_id": request_id,
            "conversation_id": self.conversation_id, "depth": dual.depth,
            "voice_adapted": dual.voice_adapted,
        }
        if dual.voice_chunks:
            speech_chunks = build_speech_chunks(
                dual.voice_chunks, dual.voice_chunks_raw,
                request_id=request_id, conversation_id=self.conversation_id,
            )
            payload["chunks"] = [c.to_dict() for c in speech_chunks]
        self._event_bus.publish(Event(type="speak_request", data=payload))

    def _execute_tool(self, name: str, args: dict) -> Any:
        if self._event_bus is None:
            raise RuntimeError("not bound to an event bus")
        execution_id = generate_id("exec")
        self._event_bus.publish(Event(type="tool_requested", data={
            "execution_id": execution_id,
            "tool_call": {"tool": name, "action": args.get("action", "run"), "target": args.get("target"), "parameters": args},
        }))
        finished = wait_for_event(self._event_bus, "tool_finished", lambda e: e.get("execution_id") == execution_id, 15.0)
        return finished.data if finished else {"success": False, "message": "timed out"}

    # -- introspection for the console's status panel ------------------------

    def status_snapshot(self) -> Dict[str, Any]:
        state = self.bb.current_state
        return {
            "state": state,
            "behavior": self.bb.current_behavior,
            "listening": state == LunoState.LISTENING.value,
            "thinking": state == LunoState.THINKING.value,
            "talking": state == LunoState.TALKING.value,
            "idle": state == LunoState.IDLE.value,
            "tick_count": self.scheduler.tick_count if self.scheduler else 0,
            "last_tick_ms": (self.scheduler.last_tick_duration_s * 1000.0) if self.scheduler else 0.0,
            # Voice Output Mode sprint (Phase 6 - visibility) - the mode
            # the LAST spoken turn actually used (`None` if nothing has
            # been spoken yet this process). Minimal, log/status-only
            # observability - see `_last_spoken_voice_output_mode`'s own
            # docstring; no new dashboard page/UI was added for this.
            "last_voice_output_mode": self._last_spoken_voice_output_mode,
        }


# Blackboard doesn't have a generic "push a visual event by category name"
# helper (its `push_visual_event` takes a full VisualEvent) - small
# convenience added here, on the Blackboard INSTANCE only (monkeypatch of
# one bound method), so BehaviorTreeModule.on_event stays a one-liner per
# injected event type without needing to import VisualEvent at every call
# site. Does not touch behavior_tree/blackboard.py itself.
def _bb_push_visual_event_safe(self: Blackboard, category: str) -> None:
    from luno.behavior_tree.blackboard import VisualEvent
    self.push_visual_event(VisualEvent(category=category, description=f"{category.replace('_', ' ')} observed", importance=3))


Blackboard.push_visual_event_safe = _bb_push_visual_event_safe  # type: ignore[attr-defined]


# ============================================================================
# The console itself
# ============================================================================

BANNER = r"""
====================================================
L U N O
Developer Runtime Console
====================================================
"""

INJECTABLE_EVENTS = (
    "smoke_detected", "fire_alarm", "door_open", "person_detected",
    "motion", "wake_word", "planner_failed", "tool_timeout",
    "internet_lost", "battery_low",
)

HELP_TEXT = """
Type plain text to simulate speech (Whisper stand-in) - it is published as SpeechRecognized.

Commands:
  /help                 show this help
  /status               live module status snapshot
  /health                Runtime + module health report
  /events [N]            recent event history (default 20)
  /modules               registered modules + adapters + state
  /plans                 Planner inspection (current/completed/running/failed tasks)
  /tasks                 Tool Manager inspection (current tool, timing, retries)
  /memory                Vision Memory inspection (objects/locations/events/long-term)
  /memquery <text>        preview Sprint 5 memory retrieval for a given question (no LLM call)
  /context                exact LLMContext that would be sent to the LLM
  /history                conversation log (USER/LUNO/SYSTEM/EVENT lines)
  /config                 current OpenRouter/runtime configuration
  /debug on|off            toggle the live event firehose
  /clear                   clear the screen
  /restart                  restart every registered module
  /reload                   reload config/adapters/model without a process restart
  /event <name>              inject a hardware-style event (see below)
  /sleep                      force the conversation session to Sleeping (manual test)
  /wake                       force a wake sequence, as if the wake word was just heard
  /session                     inspect the current conversation session (state/timeout/config)
  /bargein                      inspect barge-in state (thinking/speaking/mode/confirmation)
  /emergency [clear]              inject smoke_detected (forces CRITICAL mode) / clear it again
  /quit                      graceful shutdown and exit

Injectable events (/event <name>):
  """ + ", ".join(INJECTABLE_EVENTS) + """

While a reply is streaming, type: stop | cancel | pause | resume
  (these are direct developer shortcuts - they publish CancelLLMRequest /
  manipulate the Planner's own task-pause directly, unchanged since Sprint 1)

Sprint 2 - wake word + session: while Sleeping, type a configured wake
word (default: "luno", "hey luno", "hi luno") to start a conversation -
everything else is ignored until then. Once awake, speak naturally for
as long as the configured session_timeout (default 15s) keeps getting
reset by your replies; after that long with no speech, Luno goes back
to sleep automatically.

Sprint 3 - full barge-in: type any natural interrupt phrase as plain
speech (not the bare shortcut words above) - "stop", "cancel", "pause",
"wait", "hold on", "enough", "that's enough", or Indonesian "batal",
"sudah", "diam dulu", "tunggu", "sebentar" - at ANY point while Luno is
thinking or speaking, with no wake word required. What happens depends
on the current SpeakingMode (see /bargein): FREE interrupts immediately
and replies "Okay."/"Sure."; SOFT stops speech only (the task keeps
running); CONFIRM asks "Do you want to cancel the operation?" and waits
up to 12s for yes/no ("resume"/"continue"/"lanjutkan" also works to
keep going); CRITICAL (emergency active) only ever pauses, never
cancels. Say "resume"/"continue"/"lanjutkan"/"lanjut" to un-pause.
"""


class RuntimeDemoConsole:
    """Owns the Runtime, every wrapper Module, the adapters, and all
    console-facing state (conversation log, streaming buffer, debug
    monitor). Kept separate from `main()`'s `input()` loop on purpose -
    every method here is callable/testable without a real terminal, which
    is what `test_runtime_demo.py` exercises."""

    def __init__(self, openrouter_client: Optional[Any] = None, fish_audio_client: Optional[Any] = None,
                 history_len: int = 500, session_config: Optional[WakeSessionConfig] = None,
                 barge_in_config: Optional[BargeInConfig] = None,
                 enable_observability_log: bool = False,
                 observability_log_dir: str = "logs") -> None:
        self.runtime = Runtime()
        self.event_bus = self.runtime.event_bus
        #: Sprint 50 (Runtime Observability) - OFF by default so every
        #: pre-existing test/script that builds a console (there are
        #: hundreds, across every sprint since 36) gets byte-for-byte
        #: identical behavior and zero new on-disk side effects unless it
        #: explicitly opts in. Sprint 50's own tests, and the replay
        #: engine (`luno/replay.py`), pass `enable_observability_log=True`
        #: with `observability_log_dir` pointed at a temp directory.
        self.enable_observability_log = enable_observability_log
        self.observability_log_dir = observability_log_dir
        self._event_log_writer: Optional[Any] = None

        self.vision_module = VisionMemoryModule()
        self.tool_manager_module = ToolManagerBridgeModule()
        self.barge_in_config = barge_in_config or BargeInConfig.from_env()
        self.planner_module = PlannerBridgeModule(speaking_mode_config=self.barge_in_config)
        self.behavior_tree_module = BehaviorTreeModule()
        self.session_manager = SessionManagerModule(config=session_config)
        self.barge_in_module = BargeInModule(config=self.barge_in_config)
        for m in (self.vision_module, self.tool_manager_module, self.planner_module,
                  self.behavior_tree_module, self.session_manager, self.barge_in_module):
            m.bind_event_bus(self.event_bus)

        self.runtime.register_module(self.vision_module)
        self.runtime.register_module(self.tool_manager_module)
        self.runtime.register_module(self.planner_module, dependencies=self.planner_module.dependencies)
        self.runtime.register_module(self.behavior_tree_module, dependencies=self.behavior_tree_module.dependencies)
        self.runtime.register_module(self.session_manager)
        self.runtime.register_module(self.barge_in_module)

        # Sprint 2: SpeechRecognized now lands on SessionManagerModule
        # FIRST, not Behavior Tree directly - while Sleeping, everything
        # that isn't a matching wake word is dropped right here, before
        # it can ever reach Behavior Tree/Planner/Tool Manager/OpenRouter.
        # Once genuinely awake, SessionManagerModule forwards the
        # utterance onward as CONVERSATION_SPEECH_EVENT, which IS routed
        # to behavior_tree (see BehaviorTreeModule.on_event below, which
        # handles that event type identically to a raw SpeechRecognized).
        #
        # Sprint 3: BargeInModule ALSO subscribes to the same raw
        # SpeechRecognized (ordinary fan-out, same mechanism "motion"
        # already uses to reach both behavior_tree and vision_memory) so
        # interrupt/resume commands work even mid-turn, when
        # SessionManagerModule itself would otherwise just drop stray
        # speech. SessionManagerModule now keys its SPEAKING transition
        # off "speak_request" (literally about to make sound), not
        # "assistant_response" (the raw conversation record) - see
        # wake_session/manager.py's own docstring for why.
        self.runtime.add_route(SpeechRecognized.EVENT_TYPE, "session_manager")
        self.runtime.add_route(SpeechRecognized.EVENT_TYPE, "barge_in")
        self.runtime.add_route("wake_word_detected", "session_manager")
        self.runtime.add_route("speak_request", "session_manager")
        # Production-Safe LLM -> TTS Streaming Activation sprint - a fully
        # streamed reply never publishes "speak_request" (see
        # BehaviorTreeModule._speak()'s own "do NOT publish a second...
        # SpeakRequest" rule), so without this route SessionManagerModule's
        # THINKING -> SPEAKING transition was never reached for a streamed
        # turn - a permanent post-reply deadlock (THINKING has no timeout).
        # See wake_session/manager.py's own docstring and
        # _handle_playback_started() for the full explanation.
        self.runtime.add_route("speech_playback_started", "session_manager")
        self.runtime.add_route("speech_playback_finished", "session_manager")
        self.runtime.add_route("speech_playback_cancelled", "session_manager")
        self.runtime.add_route("llm_error", "session_manager")
        self.runtime.add_route("llm_cancelled", "session_manager")
        self.runtime.add_route(CONVERSATION_SPEECH_EVENT, "behavior_tree")
        # Bug fix (wake session / barge-in integration): behavior_tree
        # ALSO needs to see "llm_cancelled" (plain fan-out, same event
        # type already routed to session_manager/barge_in above) purely
        # to suppress speaking a reply that BargeInModule interrupted in
        # the thinking->speaking gap - see BehaviorTreeModule._speak().
        self.runtime.add_route("llm_cancelled", "behavior_tree")

        for pattern in BARGE_IN_REQUIRED_ROUTES:
            if pattern != SpeechRecognized.EVENT_TYPE:  # already added above
                self.runtime.add_route(pattern, "barge_in")

        self.runtime.add_route("user_utterance", "planner")
        # conversation_ended lifecycle routing fix - PlannerBridgeModule.
        # on_event() has always handled event.type == "conversation_ended"
        # (dispatching to _on_conversation_ended() below) but no route ever
        # delivered it here - SessionManagerModule (luno/wake_session/
        # manager.py) publishes ConversationEnded on inactivity timeout /
        # manual sleep, and it previously reached nobody in this console.
        # Kept byte-for-byte in sync with luno/bootstrap/modules.py's own
        # copy of this line, per that file's own "byte-for-byte mirror"
        # convention (see its module docstring). See
        # docs/change_impact/conversation_ended_lifecycle_routing.md for
        # the full root-cause trace.
        self.runtime.add_route("conversation_ended", "planner")
        # Memory Continuity & Short Follow-up Reference Resolution sprint
        # (Sprint 4) - the SAME kind of missing-route gap as the
        # "conversation_ended lifecycle routing fix" immediately above,
        # found by a live probe through this exact console (not
        # assumption): `PlannerBridgeModule.on_event()` has always
        # handled `event.type == "assistant_response"` (dispatching to
        # `_on_assistant_response()` above - which pairs this turn's reply
        # with its user text for `memory.remember_turn()` AND, as of this
        # sprint, updates the per-conversation active-topic snapshot
        # `_active_topic` used for short-follow-up reference resolution)
        # but no route ever delivered "assistant_response" here in this
        # console. Without this route, `_on_assistant_response()` was dead
        # code via the real event-routed path: `session_log` and, now,
        # `_active_topic` both silently never received real turn content.
        # Kept byte-for-byte in sync with `luno/bootstrap/modules.py`'s
        # own copy of this line, per that file's own "byte-for-byte
        # mirror" convention. See
        # docs/change_impact/memory_continuity_reference_resolution.md for
        # the full root-cause trace.
        self.runtime.add_route("assistant_response", "planner")
        self.runtime.add_route("tool_requested", "tool_manager")
        for evt_type in INJECTABLE_EVENTS:
            self.runtime.add_route(evt_type, "behavior_tree")
        for evt_type in ("motion", "person_detected", "door_open"):
            self.runtime.add_route(evt_type, "vision_memory")

        # Sprint 3: fish_audio must NOT also be auto-wired to the raw
        # "assistant_response" conversation-record event in THIS
        # console's setup - only "speak_request" (see BehaviorTreeModule
        # ._speak() below) should ever trigger real playback here, or
        # Fish Audio would speak every reply twice (once raw off
        # OpenRouter's own AssistantResponse, once normalized off
        # SpeakRequest). This is a LOCAL customization of the mapping,
        # not a change to the package's own DEFAULT_ADAPTER_EVENT_MAPPING
        # - anything else still using that default (Task A's own tests,
        # the adapters package's own Quick Start example) is unaffected.
        console_adapter_mapping = dict(DEFAULT_ADAPTER_EVENT_MAPPING)
        console_adapter_mapping.pop("assistant_response", None)
        self.adapter_manager = AdapterManager(
            self.runtime.module_manager, self.runtime.coordinator, self.runtime.event_bus,
            event_mapping=EventMapping.from_dict(console_adapter_mapping),
        )
        self.openrouter_adapter = OpenRouterAdapter(
            client=openrouter_client or _default_openrouter_client(),
            default_model=os.getenv("OPENROUTER_MODEL", "anthropic/claude-3.5-sonnet"),
        )
        self.fish_audio_adapter = FishAudioAdapter(client=fish_audio_client or _default_fish_audio_client())
        self.adapter_manager.register(self.openrouter_adapter, AdapterConfig(name="openrouter"))
        self.adapter_manager.register(self.fish_audio_adapter, AdapterConfig(name="fish_audio"))

        self.history = EventHistory(self.event_bus, max_len=history_len)
        self.debug = DebugMonitor(self.event_bus)

        self.conversation_log: Deque[Tuple[str, str]] = deque(maxlen=500)
        self._streaming_request_id: Optional[str] = None
        self._streaming_buffer: List[str] = []
        self._logged_reply_ids: set = set()
        self._started = False

        self._wire_console_listeners()

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self.history.start()
        # Sprint 50 (Runtime Observability) - constructed lazily here
        # (not in `__init__`) so `enable_observability_log=False` (the
        # default) never even imports `event_log_writer` for the
        # overwhelming majority of existing callers; mirrors `self.history`'s
        # own start()/stop() lifecycle exactly.
        if self.enable_observability_log and self._event_log_writer is None:
            from luno.dashboard.event_log_writer import EventLogWriter
            self._event_log_writer = EventLogWriter(self.event_bus, log_dir=self.observability_log_dir)
        if self._event_log_writer is not None:
            self._event_log_writer.start()
        self.runtime.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self.debug.off()
        self.history.stop()
        if self._event_log_writer is not None:
            self._event_log_writer.stop()
        self.runtime.stop()
        self._started = False

    def mark_test(self, conversation_id: Optional[str] = None, note: str = "", scenario: str = "",
                   base_dir: str = "tests/real_world") -> Optional[Dict[str, Any]]:
        """Sprint 50 (Runtime Observability) - the SAME `/mark_test`
        real-world test-data capture `luno.bootstrap.console.
        ProductionConsole.mark_test()` exposes as a typed command,
        offered here as a plain method so a probe/replay script driving
        this console programmatically (this project's own established
        E2E test pattern since Sprint 46) can call it directly with no
        text-command layer in between."""
        from luno.test_capture import mark_test_case
        return mark_test_case(self, conversation_id=conversation_id, note=note, scenario=scenario, base_dir=base_dir)

    # -- console-facing event listeners (display only, never behavior) -------

    def _wire_console_listeners(self) -> None:
        self.event_bus.subscribe(SpeechRecognized.EVENT_TYPE, lambda e: self._log("USER", e.get("text", "")))
        self.event_bus.subscribe("assistant_response", lambda e: self._on_assistant_response(e))
        self.event_bus.subscribe("speak_request", lambda e: self._on_speak_request(e))
        self.event_bus.subscribe("llm_streaming", lambda e: self._on_stream_start(e))
        self.event_bus.subscribe("llm_chunk", lambda e: self._on_stream_chunk(e))
        self.event_bus.subscribe("llm_error", lambda e: self._log("SYSTEM", f"LLM error: {e.get('error')}"))
        self.event_bus.subscribe("llm_cancelled", lambda e: self._log("SYSTEM", f"LLM request cancelled ({e.get('request_id')})"))
        self.event_bus.subscribe("planner_created", lambda e: self._log(
            "SYSTEM", f"Planner created {e.get('task_count', 0)} task(s) for plan {e.get('plan_id')}"))
        self.event_bus.subscribe("planner_finished", lambda e: self._log(
            "SYSTEM", f"Planner finished plan {e.get('plan_id')}: {e.get('tasks')}"))
        self.event_bus.subscribe("tool_started", lambda e: self._log("SYSTEM", f"Tool '{e.get('tool')}' started"))
        self.event_bus.subscribe("tool_finished", lambda e: self._log("EVENT", "ToolFinished"))
        self.event_bus.subscribe("tool_failed", lambda e: self._log("EVENT", f"ToolFailed: {e.get('error')}"))
        self.event_bus.subscribe("speech_playback_finished", lambda e: self._log("EVENT", "SpeechFinished"))
        self.event_bus.subscribe("system_error", lambda e: self._log("SYSTEM", f"SystemError: {e.get('error')}"))

        # Sprint 2: wake word / conversation session lifecycle (purely
        # display - SessionManagerModule has already made every real
        # decision by the time these fire).
        self.event_bus.subscribe("wake_word_detected", lambda e: self._log(
            "SYSTEM", f"Wake word detected (phrase={e.get('matched_phrase')!r}, confidence={e.get('confidence')})"))
        self.event_bus.subscribe("wake_word_rejected", lambda e: self._log(
            "SYSTEM", f"Wake word rejected ({e.get('reason')}, confidence={e.get('confidence')})"))
        self.event_bus.subscribe("conversation_started", lambda e: self._log(
            "SYSTEM", f"Conversation started (session={e.get('session_id')})"))
        self.event_bus.subscribe("conversation_ended", lambda e: self._log(
            "SYSTEM", f"Conversation ended ({e.get('reason')})"))
        self.event_bus.subscribe("conversation_timeout", lambda e: self._log(
            "SYSTEM", f"Conversation timed out (session={e.get('session_id')})"))

    def _log(self, channel: str, text: str) -> None:
        self.conversation_log.append((channel, text))
        color = {"USER": Colors.CYAN, "LUNO": Colors.GREEN, "SYSTEM": Colors.YELLOW, "EVENT": Colors.GREY}.get(channel, Colors.WHITE)
        print(f"{c(channel, Colors.BOLD, color)} {text}")

    def _on_assistant_response(self, event: Event) -> None:
        text = event.get("text", "")
        request_id = event.get("request_id")
        # Sprint 3: a real conversational turn now produces BOTH a raw
        # `assistant_response` (this event - the conversation record) and
        # a normalized `speak_request` (published moments later by
        # BehaviorTreeModule._speak(), tagged with this SAME request_id -
        # see `_last_turn_request_id`). Mark it seen here so
        # `_on_speak_request` below skips re-logging it once it arrives.
        if request_id:
            self._logged_reply_ids.add(request_id)
        if self._streaming_request_id == request_id:
            # already displayed live via chunks - just close the line out
            if Colors.enabled:
                print()
            self._streaming_request_id = None
            self._streaming_buffer = []
        else:
            self._log("LUNO", text)
            return
        self._log("LUNO", "")  # log entry for /history even though it streamed live
        self.conversation_log[-1] = ("LUNO", text)

    def _on_speak_request(self, event: Event) -> None:
        """`speak_request` fires for every real turn (already logged via
        `_on_assistant_response` above, so skipped here by request_id) AND
        for turns that have NO `assistant_response` counterpart at all -
        wake acknowledgements (`wake_session`) and barge-in acks/prompts
        (`barge_in`, e.g. the FREE-mode "Okay."/"Sure." or the CONFIRM
        prompt) - those need to show up in console history too, since
        this is the only event that carries their text."""
        request_id = event.get("request_id")
        if request_id and request_id in self._logged_reply_ids:
            return
        if request_id:
            self._logged_reply_ids.add(request_id)
        self._log("LUNO", event.get("text", ""))

    def _on_stream_start(self, event: Event) -> None:
        self._streaming_request_id = event.get("request_id")
        self._streaming_buffer = []
        print(f"{c('LUNO', Colors.BOLD, Colors.GREEN)} ", end="", flush=True)

    def _on_stream_chunk(self, event: Event) -> None:
        if event.get("request_id") != self._streaming_request_id:
            return
        delta = event.get("delta", "")
        self._streaming_buffer.append(delta)
        print(delta, end="", flush=True)

    # -- commands ---------------------------------------------------------

    def handle_line(self, line: str) -> bool:
        """Returns False when the console should exit (`/quit`)."""
        line = line.strip()
        if not line:
            return True
        if line.startswith("/"):
            return self._handle_command(line)
        if line.lower() in ("stop", "cancel"):
            self._interrupt("cancel")
            return True
        if line.lower() == "pause":
            self._interrupt("pause")
            return True
        if line.lower() == "resume":
            self._interrupt("resume")
            return True
        self.simulate_speech(line)
        return True

    def simulate_speech(self, text: str, confidence: Optional[float] = None) -> None:
        """The Whisper stand-in. `confidence=None` (the default, used for
        anything typed at the keyboard) is treated by SessionManagerModule
        as "always trust it" - a developer typing text isn't a noisy
        acoustic detection. Pass an explicit `confidence` to exercise the
        False Wake Protection path (see test suite)."""
        self.event_bus.publish(SpeechRecognized(data={"text": text, "confidence": confidence}))

    def inject_event(self, name: str) -> bool:
        if name not in INJECTABLE_EVENTS:
            print(c(f"Unknown injectable event '{name}'. See /help.", Colors.RED))
            return False
        self.event_bus.publish(Event(type=name, data={"injected": True, "at": utcnow_str()}))
        self._log("EVENT", f"injected: {name}")
        return True

    def _interrupt(self, kind: str) -> None:
        if kind == "cancel":
            # Bug fix (request_id / plan_id correlation): this used to
            # fall back to `self.planner_module.last_plan_id` - a
            # Planner-internal plan id (format "plan_...") - whenever
            # nothing was actively streaming (`_streaming_request_id` is
            # only set between LLMStreaming and AssistantResponse/
            # LLMFinished for THIS console's own live-print bookkeeping,
            # so it's None once a reply has finished streaming and moved
            # into TTS synthesis/playback). Publishing
            # `cancel_llm_request(request_id=<plan_id>)` can never match
            # anything in `OpenRouterAdapter`'s own in-flight table (it
            # only ever knows about `request_id`s, never plan ids), so the
            # cancel silently targeted nothing. `BargeInModule.
            # current_request_id` is the correct source instead - it's
            # the SAME request_id `OpenRouterAdapter` echoes into
            # `LLMStarted`/`LLMFinished`/`AssistantResponse`, and (unlike
            # `_streaming_request_id`) it stays valid for the whole turn,
            # not just the streaming phase. Planner ids are never used as
            # a substitute for an LLM request_id anywhere in this file.
            request_id = self._streaming_request_id or self.barge_in_module.current_request_id
            self.event_bus.publish(CancelLLMRequest(data={"request_id": request_id}))
            self.event_bus.publish(Event(type="cancel_requested", data={}))
            self._log("SYSTEM", "cancel requested")
        elif kind == "pause":
            plan_id = self.planner_module.last_plan_id
            if plan_id:
                try:
                    self.planner_module.planner.pause(plan_id)
                except Exception:
                    pass
            self._log("SYSTEM", "pause requested")
        elif kind == "resume":
            plan_id = self.planner_module.last_plan_id
            if plan_id:
                try:
                    self.planner_module.planner.resume(plan_id)
                except Exception:
                    pass
            self._log("SYSTEM", "resume requested")

    def _handle_command(self, line: str) -> bool:
        parts = line.split()
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd == "/help":
            print(HELP_TEXT)
        elif cmd == "/status":
            self.print_status()
        elif cmd == "/health":
            self.print_health()
        elif cmd == "/events":
            limit = int(args[0]) if args and args[0].isdigit() else 20
            self.print_events(limit)
        elif cmd == "/modules":
            self.print_modules()
        elif cmd == "/plans":
            self.print_plans()
        elif cmd == "/tasks":
            self.print_tasks()
        elif cmd == "/memory":
            self.print_memory()
        elif cmd == "/memquery":
            query_text = line.split(" ", 1)[1] if " " in line else ""
            if not query_text.strip():
                print(c("usage: /memquery <question>", Colors.RED))
            else:
                self.print_memquery(query_text)
        elif cmd == "/context":
            self.print_context()
        elif cmd == "/history":
            self.print_history()
        elif cmd == "/config":
            self.print_config()
        elif cmd == "/debug":
            if args and args[0].lower() == "off":
                self.debug.off()
                print(c("debug mode OFF", Colors.YELLOW))
            else:
                self.debug.on()
                print(c("debug mode ON", Colors.YELLOW))
        elif cmd == "/clear":
            os.system("cls" if os.name == "nt" else "clear")
        elif cmd == "/restart":
            self.print_restart()
        elif cmd == "/reload":
            self.runtime.reload()
            self.adapter_manager.restart_all()
            self.planner_module.memory_retriever.reload_config()
            print(c("configuration reloaded", Colors.YELLOW))
            session = self.session_manager.status_snapshot()
            print(c("Wake words loaded from:", Colors.BOLD))
            print(f"  {session['config']['wake_words_source']}")
            print(c("Wake words:", Colors.BOLD))
            print(f"  {session['config']['wake_words']}")
            if session["config"].get("wake_words_conflict_warning"):
                print(c("Warning:", Colors.BOLD, Colors.RED))
                print(f"  {session['config']['wake_words_conflict_warning']}")
        elif cmd == "/event":
            if not args:
                print(c("usage: /event <name>", Colors.RED))
            else:
                self.inject_event(args[0])
        elif cmd == "/sleep":
            self.session_manager.force_sleep()
            print(c("session forced to Sleeping", Colors.YELLOW))
        elif cmd == "/wake":
            self.session_manager.force_wake()
            print(c("wake sequence forced", Colors.YELLOW))
        elif cmd == "/session":
            self.print_session()
        elif cmd == "/bargein":
            self.print_bargein()
        elif cmd == "/emergency":
            if args and args[0].lower() == "clear":
                self.barge_in_module.clear_emergency()
                print(c("emergency cleared", Colors.YELLOW))
            else:
                self.event_bus.publish(Event(type="smoke_detected", data={"injected": True}))
                print(c("smoke_detected injected - barge-in mode forced to CRITICAL", Colors.YELLOW))
        elif cmd == "/quit":
            return False
        else:
            print(c(f"Unknown command '{cmd}'. Type /help.", Colors.RED))
        return True

    # -- inspection renderers -------------------------------------------------

    def print_status(self) -> None:
        bt = self.behavior_tree_module.status_snapshot()
        session = self.session_manager.status_snapshot()
        stats = self.event_bus.stats()
        print(c("\n-- Live Module Status --------------------------------------", Colors.BOLD))
        print(f"  Conversation session : {c(session['state'], Colors.CYAN)}  "
              f"(remaining={session['seconds_remaining']})")
        print(f"  Behavior Tree state : {c(bt['state'], Colors.CYAN)}  (behavior={bt['behavior']})")
        print(f"  Listening={bt['listening']}  Thinking={bt['thinking']}  Talking={bt['talking']}  Idle={bt['idle']}")
        print(f"  Planner status       : plan={self.planner_module.last_plan_id}")
        print(f"  Current tool         : {self.tool_manager_module.last_tool}")
        print(f"  Current LLM model    : {self.openrouter_adapter.default_model}")
        print(f"  Vision Memory        : {len(self.vision_module.last_events)} recent event(s)")
        print(f"  Execution queue      : {self.event_bus.stats()['queue_size']} event(s) queued")
        print(f"  Health               : {'OK' if self.runtime.health().healthy else 'DEGRADED'}")
        print(f"  Event bus            : published={stats['published']} delivered={stats['delivered']} dropped={stats['dropped']}")
        print()

    def print_health(self) -> None:
        report = self.runtime.health()
        print(c("\n-- Health -----------------------------------------------------", Colors.BOLD))
        overall = "Healthy" if report.healthy else "Warning"
        print(f"  Overall: {c(overall, Colors.GREEN if report.healthy else Colors.YELLOW)}")
        for name, status in report.modules.items():
            state = "Healthy" if status.healthy and not status.stalled else ("Restarting" if status.stalled else "Warning")
            color = Colors.GREEN if state == "Healthy" else Colors.YELLOW
            print(f"    {name:<16} {c(state, color)}  {status.message}")
        if report.issues:
            print(c("  Issues:", Colors.RED))
            for issue in report.issues:
                print(f"    - {issue}")
        print()

    def print_events(self, limit: int) -> None:
        print(c(f"\n-- Recent Events (last {limit}) --------------------------------", Colors.BOLD))
        for r in self.history.recent(limit):
            lat = f" latency={r.latency_ms:.1f}ms" if r.latency_ms is not None else ""
            print(f"  [{r.at}] #{r.seq} {c(r.type, Colors.MAGENTA):<30} src={r.source or '-':<12}{lat}  {r.data_preview}")
        print()

    def print_modules(self) -> None:
        print(c("\n-- Modules & Adapters -------------------------------------------", Colors.BOLD))
        for name, record in self.runtime.module_manager.all_modules().items():
            print(f"  {name:<16} {record.state.value}")
        print()

    def print_plans(self) -> None:
        print(c("\n-- Planner Inspection ------------------------------------------", Colors.BOLD))
        plan_id = self.planner_module.last_plan_id
        if not plan_id:
            print("  (no plan created yet)")
            print()
            return
        try:
            plan = self.planner_module.planner.get_plan(plan_id)
            status = self.planner_module.planner.get_status(plan_id)
        except Exception as ex:
            print(f"  error: {ex}")
            print()
            return
        print(f"  Current Plan     : {plan.id} ({plan.status.value}) - \"{plan.source_request}\"")
        print(f"  Completed Tasks  : {status.completed_tasks}")
        print(f"  Running Tasks    : {status.current_tasks}")
        print(f"  Waiting Tasks    : {status.remaining_tasks}")
        print(f"  Failed Tasks     : {status.failed_tasks}")
        print(f"  Dependencies     : {[t.depends_on for t in plan.tasks]}")
        print(f"  Rollback         : rollback_on_failure={plan.rollback_on_failure}")
        print()

    def print_tasks(self) -> None:
        print(c("\n-- Tool Manager Inspection ---------------------------------------", Colors.BOLD))
        result = self.tool_manager_module.last_result
        print(f"  Current Tool     : {self.tool_manager_module.last_tool}")
        if result:
            print(f"  Execution time   : {result.get('execution_time_ms')}ms")
            print(f"  Status           : {result.get('status')}")
            print(f"  Retryable        : {result.get('retryable')}")
            print(f"  Result           : {result.get('message')} data={result.get('data')}")
        else:
            print("  (no tool executed yet)")
        print()

    def print_memory(self) -> None:
        print(c("\n-- Vision Memory Inspection --------------------------------------", Colors.BOLD))
        state = vm.get_world_state()
        events = vm.get_recent_events(limit=10)
        ltm = vm.get_long_term_memory()
        print(f"  Known Objects    : {list(state.objects.keys())}")
        print(f"  Known Locations  : {[o.location for o in state.objects.values() if getattr(o, 'location', None)]}")
        print(f"  Recent Events    : {[e.description for e in events]}")
        print(f"  Long-term Memory : {[m.statement for m in ltm]}")
        print(f"  Current Scene    : {state.room.to_dict() if hasattr(state.room, 'to_dict') else state.room}")
        print()

    def print_memquery(self, query_text: str) -> None:
        """Sprint 5 debug helper - shows exactly what
        `PlannerBridgeModule._handle_utterance()` would retrieve/inject for
        a given question, WITHOUT actually running a turn or calling the
        LLM. Never triggers live vision - see luno/memory_retrieval's own
        docstring for why that's true by construction, not just by
        convention here."""
        memories = self.planner_module.memory_retriever.retrieve_memories(query_text)
        print(c(f"\n-- Memory Retrieval preview for {query_text!r} ----------------------", Colors.BOLD))
        if not memories:
            print("  (no relevant memories found - nothing would be injected)")
        else:
            for mem in memories:
                stale_tag = " [STALE]" if mem.stale else ""
                print(f"  [{mem.source}] score={mem.score:.2f}{stale_tag}  {mem.text}")
            block = build_memory_prompt_block(memories)
            print(c("\n  Prompt block that would be injected:", Colors.BOLD))
            for block_line in block.splitlines():
                print(f"    {block_line}")
        print()

    def print_session(self) -> None:
        s = self.session_manager.status_snapshot()
        print(c("\n-- Conversation Session (wake word + session mgmt) ------------------", Colors.BOLD))
        print(f"  State            : {c(s['state'], Colors.CYAN)}  (was: {s['previous_state']})")
        print(f"  Time in state    : {s['time_in_state_s']}s")
        remaining = s["seconds_remaining"]
        print(f"  Timeout remaining: {f'{remaining:.1f}s' if remaining is not None else '(not running)'}")
        print(f"  Wake count       : {s['wake_count']}")
        print(f"  Session id       : {s['session_id']}")
        cfg = s["config"]
        print(f"  wake_words       : {cfg['wake_words']}")
        print(f"  wake_words source: {cfg['wake_words_source']}")
        if cfg.get("wake_words_conflict_warning"):
            print(c(f"  WARNING          : {cfg['wake_words_conflict_warning']}", Colors.RED))
        print(f"  session_timeout_s: {cfg['session_timeout_s']}")
        print(f"  wake_acknowledgement: {cfg['wake_acknowledgement']!r}")
        print(f"  wake_confidence  : {cfg['wake_confidence']}")
        print(f"  sleep_enabled    : {cfg['sleep_enabled']}")
        print()

    def print_bargein(self) -> None:
        s = self.barge_in_module.status_snapshot()
        print(c("\n-- Barge-In (interruptible conversation) ---------------------------", Colors.BOLD))
        print(f"  Thinking          : {s['thinking']}")
        print(f"  Speaking          : {s['speaking']}")
        print(f"  Current mode      : {c(s['current_mode'], Colors.CYAN)}")
        print(f"  Emergency active  : {s['emergency_active']}")
        print(f"  Current request_id: {s['current_request_id']}")
        print(f"  Awaiting confirm  : {s['awaiting_confirmation']}")
        print(f"  Last action       : {s['last_action']}")
        cfg = self.barge_in_config
        print(f"  interrupt_words   : {cfg.interrupt_words}")
        print(f"  resume_words      : {cfg.resume_words}")
        print()

    def print_context(self) -> None:
        self._register_context_providers()
        ctx = self.runtime.context_builder.build()
        print(c("\n-- Context that would be sent to the LLM -------------------------", Colors.BOLD))
        for key, value in ctx.to_dict().items():
            print(f"  {key:<20}: {value}")
        print()

    def print_history(self) -> None:
        print(c("\n-- Conversation Log -----------------------------------------------", Colors.BOLD))
        for channel, text in list(self.conversation_log)[-40:]:
            color = {"USER": Colors.CYAN, "LUNO": Colors.GREEN, "SYSTEM": Colors.YELLOW, "EVENT": Colors.GREY}.get(channel, Colors.WHITE)
            print(f"  {c(channel, Colors.BOLD, color):<10} {text}")
        print()

    def print_config(self) -> None:
        cfg = self.openrouter_adapter.config
        print(c("\n-- Configuration ---------------------------------------------------", Colors.BOLD))
        print(f"  OPENROUTER_MODEL       : {self.openrouter_adapter.default_model}")
        print(f"  OPENROUTER_BASE_URL    : {cfg.base_url}")
        print(f"  OPENROUTER_TIMEOUT     : {cfg.timeout_s}s")
        print(f"  OPENROUTER_MAX_RETRIES : {cfg.max_retries}")
        print(f"  stream_default         : {cfg.stream_default}")
        print(f"  client                 : {type(self.openrouter_adapter.client).__name__}")
        print()

    def print_restart(self) -> None:
        for name in list(self.runtime.module_manager.all_modules().keys()):
            try:
                self.runtime.module_manager.restart(name)
            except Exception as ex:
                print(c(f"  restart '{name}' failed: {ex}", Colors.RED))
        print(c("all modules restarted", Colors.YELLOW))

    def _register_context_providers(self) -> None:
        bb = self.behavior_tree_module.bb
        cb = self.runtime.context_builder
        cb.register_provider("conversation_memory", lambda: [
            {"role": "user" if ch == "USER" else "assistant", "content": t} for ch, t in list(self.conversation_log)[-10:]
        ])
        cb.register_provider("vision_memory", lambda: vm.get_world_state().to_dict())
        cb.register_provider("behavior_tree_state", lambda: self.behavior_tree_module.status_snapshot())
        cb.register_provider("planner_state", lambda: {"last_plan_id": self.planner_module.last_plan_id})
        cb.register_provider("tool_results", lambda: [self.tool_manager_module.last_result] if self.tool_manager_module.last_result else [])
        cb.register_provider("ha_snapshot", lambda: {"door_closed": bb.room.door_closed, "light_on": bb.room.light_on})
        cb.register_provider("long_term_memory", lambda: [m.statement for m in vm.get_long_term_memory()])
        cb.register_provider("current_emotion", lambda: bb.emotion)
        cb.register_provider("current_activity", lambda: bb.user.activity)
        cb.register_provider("conversation_session", lambda: self.session_manager.status_snapshot())


def _default_openrouter_client() -> Any:
    """Real client if `OPENROUTER_API_KEY` is set (matches
    `luno.adapters.openrouter`'s own default-construction rule), a
    friendlier canned mock otherwise - the demo must run with zero
    external dependencies out of the box."""
    if os.getenv("OPENROUTER_API_KEY"):
        return RequestsOpenRouterClient(OpenRouterConfig.from_env())
    return _DemoMockOpenRouterClient(chunk_delay_s=0.02)


def _default_fish_audio_client() -> Any:
    """Mirrors `_default_openrouter_client()`'s own rule exactly: the
    real GPT-SoVITS/F5-TTS-backed client only if explicitly opted into
    via `FISH_AUDIO_BACKEND=real` (or `=gptsovits`/`=f5tts`, same
    effect - the actual engine choice is `RealFishAudioConfig.from_env()`'s
    own `TTS_ENGINE`) - the demo must still run with zero external
    dependencies (no TTS server, no audio hardware) out of the box, so
    an unset/`mock` value keeps using `MockFishAudioClient` exactly as
    before this fix. This is the ONLY place in the whole Runtime Demo
    that knows a real client exists at all - Behavior Tree, Wake
    Session, Barge-In, Planner, Tool Manager never do, and never need
    to (see `luno/adapters/fish_audio_real.py`'s own docstring)."""
    backend = os.getenv("FISH_AUDIO_BACKEND", "mock").strip().lower()
    if backend in ("real", "gptsovits", "f5tts"):
        return RealFishAudioClient(RealFishAudioConfig.from_env())
    return MockFishAudioClient(playback_delay_s=0.05)


class _DemoMockOpenRouterClient(MockOpenRouterClient):
    """Same mock the OpenRouter adapter test suite uses, with a slightly
    friendlier canned-reply function for a nicer console demo - still
    zero network, zero API key, per the spec's testing requirement."""

    def _resolve_text(self, messages: List[Dict[str, str]]) -> str:
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "")
        if system:
            return "Done! " + system.split(".")[1].strip() if "." in system else "Done!"
        last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
        low = last_user.lower()
        if any(w in low for w in ("hello", "hi", "hey")):
            return "Hi there! How can I help?"
        if "how are you" in low:
            return "I'm doing well, thanks for asking!"
        return f"You said: {last_user}"


# ============================================================================
# Startup banner
# ============================================================================

def print_banner(console: "RuntimeDemoConsole") -> None:
    print(c(BANNER, Colors.BOLD, Colors.CYAN))
    print(c("Runtime Status", Colors.BOLD))
    checks = [
        ("Core", True),
        ("Event Bus", True),
        ("Scheduler", True),
        ("Behavior Tree", console.runtime.module_manager.get("behavior_tree") is not None),
        ("Planner", console.runtime.module_manager.get("planner") is not None),
        ("Tool Manager", console.runtime.module_manager.get("tool_manager") is not None),
        ("OpenRouter Adapter", console.runtime.module_manager.get("openrouter") is not None),
        ("Fish Audio Adapter", console.runtime.module_manager.get("fish_audio") is not None),
        ("Vision Memory", console.runtime.module_manager.get("vision_memory") is not None),
        ("Session Manager (wake word)", console.runtime.module_manager.get("session_manager") is not None),
    ]
    for label, ok in checks:
        mark = c("✓", Colors.GREEN) if ok else c("✗", Colors.RED)
        print(f"{mark} {label}")
    print()
    session = console.session_manager.status_snapshot()
    print(f"Conversation session: {session['state']} (sleep_enabled={session['config']['sleep_enabled']}, "
          f"wake_words={session['config']['wake_words']})")
    print()
    print(c("Wake words loaded from:", Colors.BOLD))
    print(f"  {session['config']['wake_words_source']}")
    print(c("Wake words:", Colors.BOLD))
    print(f"  {session['config']['wake_words']}")
    if session["config"].get("wake_words_conflict_warning"):
        print()
        print(c("Warning:", Colors.BOLD, Colors.RED))
        print(f"  {session['config']['wake_words_conflict_warning']}")
    print()
    print(c("Ready.", Colors.BOLD, Colors.GREEN))
    print("Type /help for commands.\n")


# ============================================================================
# main()
# ============================================================================

def main() -> int:
    console = RuntimeDemoConsole()
    console.start()
    print_banner(console)

    try:
        while True:
            try:
                line = input("> ")
            except EOFError:
                break
            try:
                if not console.handle_line(line):
                    break
            except Exception as ex:
                print(c(f"error handling input: {ex}", Colors.RED))
                traceback.print_exc()
    except KeyboardInterrupt:
        print("\n(interrupted)")
    finally:
        print(c("\nShutting down...", Colors.YELLOW))
        console.stop()
        print(c("Goodbye.", Colors.GREEN))
    return 0


if __name__ == "__main__":
    sys.exit(main())
