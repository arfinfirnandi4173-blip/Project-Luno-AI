"""
events.py
=========

Adapter-specific structured event types - the same `core.events.Event`
envelope, extended with the event types the spec calls for that don't
already exist in `luno.core.events`'s catalogue of 23. Everything here
is a thin `Event` subclass exactly like `core/events.py`'s pattern (a
fixed `type` default, nothing else) - `core` never needs to be modified
to support these; `Event(type="...")` construction was always enough,
these subclasses just make them importable, autocomplete-able types
instead of string literals scattered through adapter code.

Reused as-is from `luno.core.events` (NOT redefined here - importing
them from core keeps exactly one definition of each):
    WakeWordDetected, SpeechStarted, SpeechRecognized, SpeechFinished
    VisionUpdated, VisionChanged, ObjectAppeared, ObjectDisappeared
    EmotionChanged, BehaviorChanged
    ToolRequested, ToolStarted, ToolFinished, ToolFailed
    HomeAssistantEvent
    SystemError, SystemStarted, SystemStopping, Heartbeat

New in this file (the spec's remaining event names, grouped by adapter):
    Vision         PersonAppeared, PersonDisappeared
    Vision (Aug 2026 Gemini migration - debounced room-level presence)
                   CameraPersonEntered, CameraPersonLeft
    Vision (Sprint 8 - real tracked-object/human-state pipeline)
                   VisionFrameProcessed, ObjectDetected, ObjectUpdated, ObjectLost,
                   HumanEntered, HumanLeft, PoseChanged, SceneChanged,
                   CameraDisconnected, CameraReconnected
    OpenRouter     NeedLLMResponse, CancelLLMRequest, ReloadModel, ConversationReset,
                   LLMStarted, LLMStreaming, LLMChunk, LLMFinished, LLMCancelled,
                   LLMError, LLMFailed (legacy alias, see openrouter.py), AssistantResponse
    LLM Manager (Multi-LLM Provider System sprint - see llm_manager.py)
                   ProviderSwitched, ProviderFallbackActivated, ProviderHealthChanged
    Decision Engine (Intelligent AI Routing Engine sprint - see luno/routing/)
                   RoutingDecisionMade
    Fish Audio     SpeechPlaybackStarted, SpeechPlaybackFinished, SpeechPlaybackCancelled,
                   PausePlayback, ResumePlayback, StopPlayback (input - Sprint 3 barge-in),
                   SpeechPlaybackPaused, SpeechPlaybackResumed (output - Sprint 3 barge-in)
    Unity          AnimationRequest, ExpressionRequest, AnimationFinished, AvatarReady
    Home Assistant DeviceStateChanged, AutomationTriggered
    Home Assistant (Verified Smart Home Execution sprint - verification
                   lifecycle visibility on top of the existing Reliability
                   Sprint verify-loop in luno/tool_manager/builtin/
                   real_home_assistant.py - see that file's own docstring)
                   ActionVerificationStarted, ActionVerificationRetry,
                   ActionVerified, ActionVerificationFailed, ActionVerificationTimeout
    Home Assistant (Sprint 52 - Robust HA Command & Entity Resolution -
                   bounded fuzzy/ambiguity entity-resolution decisions,
                   same real_home_assistant.py, see that file's own
                   "Sprint 52" docstring section)
                   EntityResolutionDecision
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar


def _named(type_name: str):
    return field(default=type_name, init=False)


from ..core.events import Event  # noqa: E402 - see module docstring


# -- Vision -------------------------------------------------------------------

@dataclass
class PersonAppeared(Event):
    type: str = _named("person_appeared")
    EVENT_TYPE: ClassVar[str] = "person_appeared"


@dataclass
class PersonDisappeared(Event):
    type: str = _named("person_disappeared")
    EVENT_TYPE: ClassVar[str] = "person_disappeared"


@dataclass
class CameraPersonEntered(Event):
    """Debounced, room-level presence signal - fires ONCE when YOLO goes
    from "no person detected" to "person detected" (see
    `VisionAdapter`'s presence state machine). Distinct from
    `PersonAppeared` above: that one fires on every raw label-set
    transition with no hysteresis (can flicker on a single missed
    detection); this one only flips ABSENT->PRESENT immediately but
    PRESENT->ABSENT only after `CAMERA_PERSON_ABSENCE_TIMEOUT_S` of
    continuous non-detection, so a momentary miss never generates a
    spurious enter/leave pair. Carries no identity - YOLO only ever
    knows "a person", never who (see `PersonDisappeared`'s own "no
    biometric identification" precedent elsewhere in this codebase)."""
    type: str = _named("camera_person_entered")
    EVENT_TYPE: ClassVar[str] = "camera_person_entered"


@dataclass
class CameraPersonLeft(Event):
    """The debounced counterpart to `CameraPersonEntered` above - fires
    ONCE when the room has gone `CAMERA_PERSON_ABSENCE_TIMEOUT_S` seconds
    with no person detected, having previously been PRESENT."""
    type: str = _named("camera_person_left")
    EVENT_TYPE: ClassVar[str] = "camera_person_left"


@dataclass
class HumanPresenceConfirmed(Event):
    """P0.8.6 - a STRICTER, SEPARATE signal from `CameraPersonEntered`
    above, deliberately not a replacement for it (that one, and every
    rule/test that already listens to it, is completely unchanged).
    Fires ONCE when `VisionAdapter`'s NEW confirmation gate (see
    `_update_confirmed_presence()`'s own docstring) has seen
    `config.HUMAN_DETECTION_CONFIRM_CYCLES` CONSECUTIVE tracked cycles,
    each with at least one person detection at >= `config.HUMAN_
    DETECTION_CONFIDENCE` - i.e. sustained, high-confidence presence,
    never a single frame. This is the event the physical WLED-turning-on
    automation rule (`config/automation_rules.json`) is wired to as of
    P0.8.6 - `CameraPersonEntered`/`human_detected` remains the raw,
    immediate, log-only signal (still used by `camera_human_detected_log`
    etc. for observability), this is the one gate strong enough for a
    real physical action. Computed ONLY from the tracked-cycle loop
    (`on_vision_cycle()`) - the only one with real per-detection
    confidence; the presence-watch loop (`on_detections()`) cannot
    participate (it has no confidence data at all) and is unaffected."""
    type: str = _named("human_presence_confirmed")
    EVENT_TYPE: ClassVar[str] = "human_presence_confirmed"


@dataclass
class HumanPresenceUnconfirmed(Event):
    """The counterpart to `HumanPresenceConfirmed` above - fires ONCE
    when a previously-confirmed streak is broken by a single
    non-qualifying tracked cycle (see `_update_confirmed_presence()`'s
    own docstring for why this reset is intentionally stricter/faster
    than `CameraPersonLeft`'s multi-second absence timeout - this gate
    exists specifically to keep physical automation conservative, not to
    describe whether a person is still in the room)."""
    type: str = _named("human_presence_unconfirmed")
    EVENT_TYPE: ClassVar[str] = "human_presence_unconfirmed"


# -- Vision (Sprint 8 - real tracked-object/human-state pipeline) -------------
# Additive to PersonAppeared/PersonDisappeared above (NOT a replacement -
# those keep firing exactly as before, for anything already listening).
# These are the spec's own richer event set, published alongside them by
# the same VisionAdapter - see vision.py's own module docstring for which
# listener method feeds which event.

@dataclass
class VisionFrameProcessed(Event):
    """One tracked-detection cycle finished. `data`: `fps`, `latency_ms`,
    `object_count`, `human_count`, `backend` ("mock"/"real")."""
    type: str = _named("vision_frame_processed")
    EVENT_TYPE: ClassVar[str] = "vision_frame_processed"


@dataclass
class ObjectDetected(Event):
    """A NEW tracked object appeared this cycle (first time this id has
    ever been seen). `data`: the `TrackedDetection.to_dict()` shape (id,
    label, confidence, bbox, first_seen, last_seen, tracking_age_s)."""
    type: str = _named("object_detected")
    EVENT_TYPE: ClassVar[str] = "object_detected"


@dataclass
class ObjectUpdated(Event):
    """An already-tracked object's label/confidence/bbox changed
    meaningfully this cycle (see vision.py's diffing rule for what counts
    as "meaningfully" - never fires just because the same object was
    seen again with a near-identical box). Same data shape as
    `ObjectDetected`."""
    type: str = _named("object_updated")
    EVENT_TYPE: ClassVar[str] = "object_updated"


@dataclass
class ObjectLost(Event):
    """A previously-tracked object hasn't been seen for longer than
    `TRACKING_TIMEOUT` and its track was dropped. `data["id"]`,
    `data["label"]`."""
    type: str = _named("object_lost")
    EVENT_TYPE: ClassVar[str] = "object_lost"


@dataclass
class HumanEntered(Event):
    """A new person tracking id appeared. `data`: `HumanState.to_dict()`
    shape (tracking_id, posture, facing, hand_raised, presence) - never
    a name/identity, see vision_human_state.py's own docstring."""
    type: str = _named("human_entered")
    EVENT_TYPE: ClassVar[str] = "human_entered"


@dataclass
class HumanLeft(Event):
    """A previously-tracked person hasn't been seen for longer than
    `TRACKING_TIMEOUT`. `data["tracking_id"]`."""
    type: str = _named("human_left")
    EVENT_TYPE: ClassVar[str] = "human_left"


@dataclass
class PoseChanged(Event):
    """An already-tracked person's ESTIMATED posture/facing/hand_raised
    changed since the last cycle. Same data shape as `HumanEntered`."""
    type: str = _named("pose_changed")
    EVENT_TYPE: ClassVar[str] = "pose_changed"


@dataclass
class SceneChanged(Event):
    """Fired once per cycle where ANY of ObjectDetected/ObjectUpdated/
    ObjectLost/HumanEntered/HumanLeft/PoseChanged also fired - a single
    coarse "something in view changed" signal for consumers (e.g. the
    Dashboard) that don't need the per-entity detail. `data["changes"]`
    - list of the specific event type names that fired this cycle."""
    type: str = _named("scene_changed")
    EVENT_TYPE: ClassVar[str] = "scene_changed"


@dataclass
class CameraDisconnected(Event):
    """The camera stopped producing frames (see `luno.vision.camera_
    status()`). `data["source"]`, `data["error"]`. Runtime keeps running -
    this is informational, not fatal (see VisionAdapter/RealVisionSource's
    own automatic-reconnect handling)."""
    type: str = _named("camera_disconnected")
    EVENT_TYPE: ClassVar[str] = "camera_disconnected"


@dataclass
class CameraReconnected(Event):
    """The camera started producing frames again after a
    `CameraDisconnected`. `data["source"]`."""
    type: str = _named("camera_reconnected")
    EVENT_TYPE: ClassVar[str] = "camera_reconnected"


# -- OpenRouter / LLM -----------------------------------------------------------

@dataclass
class NeedLLMResponse(Event):
    """Input. `data["model"]` (optional - falls back to the adapter's
    configured default, if any), `data["messages"]` (OpenAI-style list),
    optional `data["system_prompt"]`, `data["temperature"]`,
    `data["max_tokens"]`, `data["stream"]` (bool, default True),
    `data["metadata"]`, and the correlation triad `data["request_id"]` /
    `data["conversation_id"]` / `data["correlation_id"]` (all optional -
    missing ones are filled in, never overwritten, by the adapter)."""
    type: str = _named("need_llm_response")
    EVENT_TYPE: ClassVar[str] = "need_llm_response"


@dataclass
class CancelLLMRequest(Event):
    """Input. `data["request_id"]` - which in-flight `NeedLLMResponse`
    to cancel."""
    type: str = _named("cancel_llm_request")
    EVENT_TYPE: ClassVar[str] = "cancel_llm_request"


@dataclass
class ReloadModel(Event):
    """Input. Re-reads `OPENROUTER_*` environment variables (and, if
    `data["model"]` is given, overrides the configured default model)
    without restarting the adapter or Luno - see openrouter.py."""
    type: str = _named("reload_model")
    EVENT_TYPE: ClassVar[str] = "reload_model"


@dataclass
class ConversationReset(Event):
    """Input. `data["conversation_id"]` (optional - omit to cancel every
    in-flight request). The adapter holds no conversation history of its
    own (that belongs to Context Builder) - this only cancels in-flight
    requests tied to the conversation."""
    type: str = _named("conversation_reset")
    EVENT_TYPE: ClassVar[str] = "conversation_reset"


@dataclass
class LLMStarted(Event):
    type: str = _named("llm_started")
    EVENT_TYPE: ClassVar[str] = "llm_started"


@dataclass
class LLMStreaming(Event):
    """Output. Published once, right after `LLMStarted`, only for
    streaming requests - marks the point after which `LLMChunk` events
    start arriving."""
    type: str = _named("llm_streaming")
    EVENT_TYPE: ClassVar[str] = "llm_streaming"


@dataclass
class LLMChunk(Event):
    """Output. `data["delta"]` - the newly arrived token(s) only.
    `data["text_so_far"]` - full accumulated text up to and including
    this chunk (a convenience so subscribers don't have to concatenate
    themselves). `data["index"]` - 1-based chunk sequence number."""
    type: str = _named("llm_chunk")
    EVENT_TYPE: ClassVar[str] = "llm_chunk"


@dataclass
class LLMFinished(Event):
    type: str = _named("llm_finished")
    EVENT_TYPE: ClassVar[str] = "llm_finished"


@dataclass
class LLMCancelled(Event):
    type: str = _named("llm_cancelled")
    EVENT_TYPE: ClassVar[str] = "llm_cancelled"


@dataclass
class LLMError(Event):
    """Output. `data["error"]` - human-readable message. `data["error_type"]`
    - exception class name. `data["retryable"]` - whether this error class
    is ever retried (informational only; retries already happened, if
    any, before this was published)."""
    type: str = _named("llm_error")
    EVENT_TYPE: ClassVar[str] = "llm_error"


@dataclass
class LLMFailed(Event):
    """Legacy alias for `LLMError`, kept only so anything still importing
    the original mock adapter's event type keeps working. The real
    adapter (see openrouter.py) publishes `LLMError`, not this."""
    type: str = _named("llm_failed")
    EVENT_TYPE: ClassVar[str] = "llm_failed"


@dataclass
class AssistantResponse(Event):
    """`data["text"]` - the model's reply. `data["model"]` - which model
    actually answered. This is the CONVERSATION RECORD event - published
    once per turn by `OpenRouterAdapter` with the raw, un-normalized
    text (history/context/display all want the original wording). It is
    NOT necessarily what gets spoken verbatim - see `SpeakRequest`."""
    type: str = _named("assistant_response")
    EVENT_TYPE: ClassVar[str] = "assistant_response"


@dataclass
class SpeakRequest(Event):
    """Input (Sprint 3). `data["text"]` - literally what should be
    vocalized right now. Deliberately a SEPARATE event type from
    `AssistantResponse`: a caller that has already decided on final,
    TTS-ready text (e.g. after running it through a text normalizer, or
    a short hardcoded acknowledgement like a wake/barge-in response)
    publishes this instead, so `FishAudioAdapter` speaks EXACTLY this
    text without a second, differently-worded copy of the same turn
    also reaching it via `AssistantResponse`'s own (also valid, simpler-
    setups-only) playback trigger.

    TTS Chunking/Streaming sprint (additive) - `data["chunks"]`, optional
    `List[str]`. When present and non-empty, `FishAudioAdapter._play()`
    plays these strings SEQUENTIALLY (never overlapping) instead of
    treating `data["text"]` as one block, so playback of chunk 1 can
    start without waiting for the whole reply. When absent (every caller
    that predates this sprint, and any caller for whom chunking wasn't
    computed/failed), behavior is EXACTLY the pre-chunking behavior - the
    adapter derives a single one-item chunk list from `data["text"]`
    itself. `data["text"]` is still always the full voice string (used
    for logging/`on_playback_start` payload/non-chunk-aware consumers) -
    `data["chunks"]`, when present, is the SAME text split into
    playback-sized, ordered pieces (see luno/response_output.py's
    `build_dual_response().voice_chunks`), never a different rendering."""
    type: str = _named("speak_request")
    EVENT_TYPE: ClassVar[str] = "speak_request"


# -- LLM Streaming -> Real-Time Speech Pipeline sprint ---------------------------
# `SpeakStreamChunk` is the ONE new speech-side event this sprint adds - it
# does NOT replace `SpeakRequest` (still used for the non-streaming/whole-
# response path, byte-identical, unchanged). See
# `luno/incremental_speech.py`'s module docstring and
# `docs/change_impact/llm_streaming_speech_pipeline.md` for the full design.

@dataclass
class SpeakStreamChunk(Event):
    """Input. Incrementally appends ONE `SpeechChunk` (see
    `luno.speech_chunk.SpeechChunk.to_dict()`) to an in-progress spoken
    reply. `data["request_id"]`/`data["conversation_id"]` - same
    correlation identity `SpeakRequest`/`NeedLLMResponse` already use for
    this turn. `data["chunk"]` - one `SpeechChunk.to_dict()`-shaped dict.
    The FIRST `SpeakStreamChunk` seen for a given `request_id` opens a new
    streaming utterance in `FishAudioAdapter` (registers a
    `SpeechCancellationToken`, exactly like `SpeakRequest` does today - see
    that adapter's `handle_event()`); every subsequent one for the SAME
    `request_id` appends to that utterance's live playback queue, in the
    order published (ordering is the PUBLISHER's responsibility - this
    event carries no separate sequence-enforcement of its own beyond what
    `chunk["sequence"]` already records for observability/debugging).
    `chunk["is_final"] = True` marks the last chunk of this utterance -
    once that one has been dispatched to the client (or skipped, if its
    `text` is empty - see `FishAudioAdapter._play_stream()`), the
    utterance completes exactly like any other `SpeakRequest`/`AssistantResponse`
    turn (`SpeechPlaybackFinished`/`SpeechPlaybackCancelled`, chunk-level
    retry/skip, cancellation - ALL the same contract, same code paths as
    the non-streaming path use, just fed from a live queue instead of a
    precomputed list)."""
    type: str = _named("speak_stream_chunk")
    EVENT_TYPE: ClassVar[str] = "speak_stream_chunk"


@dataclass
class SpeechChunkPlaybackFinished(Event):
    """Output. Published once per chunk (played OR retried-then-skipped)
    by `FishAudioAdapter._play_stream()` ONLY (the non-streaming `_play()`
    path does not publish this - it has no backpressure producer to signal,
    since its whole chunk list is already known upfront). `data`:
    `request_id`, `chunk_id`, `sequence`. Purely a BACKPRESSURE signal for
    `luno.incremental_speech.StreamingSpeechCoordinator` (Phase 10 -
    "measure queue size / playback rate", "cancellation harus langsung
    membuang pending chunks") - it does NOT replace, duplicate, or
    substitute for `SpeechPlaybackStarted`/`SpeechPlaybackFinished`/
    `SpeechPlaybackCancelled`, which still fire exactly once per WHOLE
    utterance, unchanged."""
    type: str = _named("speech_chunk_playback_finished")
    EVENT_TYPE: ClassVar[str] = "speech_chunk_playback_finished"


# -- LLM Manager (Multi-LLM Provider System sprint) ----------------------------
# Published by `luno.adapters.llm_manager.LLMManagerAdapter` alongside the
# existing NeedLLMResponse/LLMStarted/.../AssistantResponse contract above
# (unchanged - Planner/Behavior Tree/Memory Retrieval never subscribe to
# these three, they're purely informational/Dashboard-facing).

@dataclass
class ProviderSwitched(Event):
    """The active provider changed (`ReloadModel` with a new
    `LLM_PROVIDER`/`data["provider"]`, or a manual switch). `data`:
    `from_provider`, `to_provider`, `reason` ("config_reload"/"manual")."""
    type: str = _named("provider_switched")
    EVENT_TYPE: ClassVar[str] = "provider_switched"


@dataclass
class ProviderFallbackActivated(Event):
    """The active provider failed and a request was retried against the
    next healthy provider in the priority list. `data`: `request_id`,
    `from_provider`, `to_provider`, `reason` (the classified error
    message), `attempt` (1-based position in the priority list)."""
    type: str = _named("provider_fallback_activated")
    EVENT_TYPE: ClassVar[str] = "provider_fallback_activated"


@dataclass
class ProviderHealthChanged(Event):
    """A provider's cached health state (from `LLMManagerAdapter`'s
    background poll loop) transitioned. `data`: `provider`, `from_state`,
    `to_state`, `message`."""
    type: str = _named("provider_health_changed")
    EVENT_TYPE: ClassVar[str] = "provider_health_changed"


# -- Decision Engine (Intelligent AI Routing Engine sprint) ---------------------
# Published by `main_runtime_demo.py::PlannerBridgeModule._handle_utterance()`
# right after `luno.routing.DecisionEngine.decide()` runs, BEFORE
# `NeedLLMResponse` - purely informational/Dashboard-facing/audit-trail,
# same role the three ProviderXxx events above play for the LLM Manager.
# Nothing in Planner/Behavior Tree/Tool Manager/the LLM Manager itself
# subscribes to this - it exists only so the Dashboard's "Decision Engine"
# panel and the log stream can show, per turn, what was decided and why
# (spec: "expose all of this transparently in logs and a Dashboard panel").

@dataclass
class RoutingDecisionMade(Event):
    """One `luno.routing.RoutingDecision`, flattened to a dict (see
    `RoutingDecision.to_dict()`) - `data` carries every field of that
    dataclass directly (`request_id`, `conversation_id`, `intents`,
    `primary_intent`, `complexity`, `complexity_score`, `knowledge_source`,
    `knowledge_hit`, `needs_internet`, `needs_tools`, `provider_alias`,
    `provider`, `model`, `affinity_applied`, `reasoning`, `search_queries`,
    `search_context`, `estimated_cost_tier`, `timestamp`)."""
    type: str = _named("routing_decision_made")
    EVENT_TYPE: ClassVar[str] = "routing_decision_made"


# -- Fish Audio -----------------------------------------------------------------

@dataclass
class SpeechPlaybackStarted(Event):
    type: str = _named("speech_playback_started")
    EVENT_TYPE: ClassVar[str] = "speech_playback_started"


@dataclass
class SpeechPlaybackFinished(Event):
    type: str = _named("speech_playback_finished")
    EVENT_TYPE: ClassVar[str] = "speech_playback_finished"


@dataclass
class SpeechPlaybackCancelled(Event):
    type: str = _named("speech_playback_cancelled")
    EVENT_TYPE: ClassVar[str] = "speech_playback_cancelled"


# -- Fish Audio: barge-in control (Sprint 3) -------------------------------------
# Input events - these three are the "pause/resume/stop" half of the spec's
# "Support: play, pause, resume, stop, cancel" (play = AssistantResponse,
# already above; "cancel" is StopPlayback under a different name - from the
# Fish Audio client's point of view a barge-in "cancel" and an explicit
# "stop" are the same primitive: halt now, discard, do not resume).

@dataclass
class PausePlayback(Event):
    """Input. `data["request_id"]` - optional, informational only (the
    adapter has exactly one playback in flight at a time)."""
    type: str = _named("pause_playback")
    EVENT_TYPE: ClassVar[str] = "pause_playback"


@dataclass
class ResumePlayback(Event):
    type: str = _named("resume_playback")
    EVENT_TYPE: ClassVar[str] = "resume_playback"


@dataclass
class StopPlayback(Event):
    """Input. Fully halts whatever is currently playing, no resume - the
    in-flight `play()` call notices and raises `PlaybackCancelled`, which
    publishes the existing `SpeechPlaybackCancelled` output event itself
    (no separate output event needed for this one)."""
    type: str = _named("stop_playback")
    EVENT_TYPE: ClassVar[str] = "stop_playback"


# Output events

@dataclass
class SpeechPlaybackPaused(Event):
    type: str = _named("speech_playback_paused")
    EVENT_TYPE: ClassVar[str] = "speech_playback_paused"


@dataclass
class SpeechPlaybackResumed(Event):
    type: str = _named("speech_playback_resumed")
    EVENT_TYPE: ClassVar[str] = "speech_playback_resumed"


# -- Unity ------------------------------------------------------------------------

@dataclass
class AnimationRequest(Event):
    type: str = _named("animation_request")
    EVENT_TYPE: ClassVar[str] = "animation_request"


@dataclass
class ExpressionRequest(Event):
    type: str = _named("expression_request")
    EVENT_TYPE: ClassVar[str] = "expression_request"


@dataclass
class AnimationFinished(Event):
    type: str = _named("animation_finished")
    EVENT_TYPE: ClassVar[str] = "animation_finished"


@dataclass
class AvatarReady(Event):
    type: str = _named("avatar_ready")
    EVENT_TYPE: ClassVar[str] = "avatar_ready"


# -- Home Assistant ---------------------------------------------------------------

@dataclass
class DeviceStateChanged(Event):
    type: str = _named("device_state_changed")
    EVENT_TYPE: ClassVar[str] = "device_state_changed"


@dataclass
class AutomationTriggered(Event):
    type: str = _named("automation_triggered")
    EVENT_TYPE: ClassVar[str] = "automation_triggered"


# -- Home Assistant: verified execution lifecycle (Verified Smart Home ---------
# Execution sprint) - published by `luno.tool_manager.builtin.
# real_home_assistant.RealHomeAssistantHandler` via an optional
# `on_verification_event(stage, payload)` hook, wired to the real Event
# Bus in exactly one place (`luno.bootstrap.adapters.
# _register_real_home_assistant_handler`) so the `tool_manager` package
# itself stays independent of the Event Bus (same "opt-in, one clear
# integration point" convention already used for `luno.routing`).
# Purely additive/informational - never changes ToolStarted/ToolFinished/
# tool_failed semantics, which keep firing exactly as before around the
# same `execute()` call. `payload` always includes at least `request_id`,
# `entity_id`, `service` (e.g. "homeassistant.turn_on"), `expected_state`;
# the terminal three (Verified/Failed/Timeout) additionally carry the
# full Execution Result Model already on `ToolResult.data`
# (`requested_action`, `actual_state`, `success`, `verification_attempts`,
# `elapsed_time_ms`, `failure_reason`, `message`).

@dataclass
class ActionVerificationStarted(Event):
    """The service call was accepted and the verify-loop (wait -> read ->
    compare) is beginning. `data`: request_id, entity_id, requested_action,
    service, expected_state, current_state (observed right before the
    call), max_attempts."""
    type: str = _named("action_verification_started")
    EVENT_TYPE: ClassVar[str] = "action_verification_started"


@dataclass
class ActionVerificationRetry(Event):
    """One verification read did not yet match the expected state and
    another attempt remains within the configured timeout. `data`:
    request_id, entity_id, service, expected_state, actual_state (this
    attempt's reading), attempt (1-based), max_attempts."""
    type: str = _named("action_verification_retry")
    EVENT_TYPE: ClassVar[str] = "action_verification_retry"


@dataclass
class ActionVerified(Event):
    """Verification succeeded - `actual_state == expected_state` (or the
    device was already in the requested state, `already_in_state=True`,
    zero attempts needed). Same data shape as `ToolResult.data` for this
    action, plus `service`/`message`."""
    type: str = _named("action_verified")
    EVENT_TYPE: ClassVar[str] = "action_verified"


@dataclass
class ActionVerificationFailed(Event):
    """Every configured retry was used up within the timeout budget and
    the device never reached the expected state (including "device
    reported unavailable") - distinct from `ActionVerificationTimeout`,
    which fires when the wall-clock budget ran out before retries could
    even be exhausted. Same data shape as `ActionVerified`, plus
    `failure_reason`."""
    type: str = _named("action_verification_failed")
    EVENT_TYPE: ClassVar[str] = "action_verification_failed"


@dataclass
class ActionVerificationTimeout(Event):
    """`VERIFY_TIMEOUT_MS` elapsed before the configured retries could
    all be attempted - HA (or the network to it) never answered in time,
    as opposed to `ActionVerificationFailed`'s "HA answered, state just
    never matched". Same data shape as `ActionVerificationFailed`."""
    type: str = _named("action_verification_timeout")
    EVENT_TYPE: ClassVar[str] = "action_verification_timeout"


@dataclass
class EntityResolutionDecision(Event):
    """Sprint 52 - Robust Home Assistant Command & Entity Resolution.
    Fired ONLY for the two outcomes that sprint actually introduces on
    top of `RealHomeAssistantHandler._resolve_entity_id()`'s pre-existing
    exact/alias/literal lookup - see that module's own "Sprint 52"
    docstring section and `_resolve_entity_tiered()`/`_emit_resolution()`
    for the full picture:

      - a target was auto-resolved to a real entity_id WITHOUT an exact
        name/alias/literal match (`resolution_method="fuzzy"`), or
      - a target was refused because two or more distinct devices were
        both plausible (`resolution_method="ambiguous"`, no entity ever
        chosen).

    Exact/alias/literal matches (the overwhelming majority of real
    traffic, unchanged from before this sprint) and fully unknown
    targets (also unchanged - `_unknown_device_result()`'s message is
    still the only signal) never publish this event, by design.

    `data`: raw_target, normalized_target, resolved_entity (`None` for
    "ambiguous"), resolution_method ("fuzzy"|"ambiguous"), confidence
    (0.0-1.0, the winning `difflib` ratio), candidate_count (distinct
    devices "in contention"), ambiguity (bool), executable (bool)."""
    type: str = _named("entity_resolution_decision")
    EVENT_TYPE: ClassVar[str] = "entity_resolution_decision"


ADAPTER_EVENT_TYPES = [
    PersonAppeared, PersonDisappeared,
    VisionFrameProcessed, ObjectDetected, ObjectUpdated, ObjectLost,
    HumanEntered, HumanLeft, PoseChanged, SceneChanged,
    CameraDisconnected, CameraReconnected,
    NeedLLMResponse, CancelLLMRequest, ReloadModel, ConversationReset,
    LLMStarted, LLMStreaming, LLMChunk, LLMFinished, LLMCancelled, LLMError, LLMFailed,
    AssistantResponse, SpeakRequest,
    ProviderSwitched, ProviderFallbackActivated, ProviderHealthChanged,
    RoutingDecisionMade,
    SpeechPlaybackStarted, SpeechPlaybackFinished, SpeechPlaybackCancelled,
    PausePlayback, ResumePlayback, StopPlayback, SpeechPlaybackPaused, SpeechPlaybackResumed,
    AnimationRequest, ExpressionRequest, AnimationFinished, AvatarReady,
    DeviceStateChanged, AutomationTriggered,
    ActionVerificationStarted, ActionVerificationRetry, ActionVerified,
    ActionVerificationFailed, ActionVerificationTimeout,
    EntityResolutionDecision,
]
