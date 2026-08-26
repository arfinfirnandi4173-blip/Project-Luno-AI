"""
actions.py
==========

One function per Behavior described in the spec, plus the `Handlers`
dependency-injection surface that keeps this whole package testable and
importable WITHOUT a running Luno (no audio hardware, no OpenRouter key,
no Home Assistant, no camera) - exactly the same "standalone, swap real
I/O in later" shape `luno/vision_memory/` was built with.

`Handlers` is the ONE seam between the Behavior Tree and the rest of
Luno. Every field is an optional callable; a `None` handler makes the
corresponding action degrade to a harmless no-op instead of crashing
(same pattern as `vision.py`'s "camera unavailable -> return [], not an
error"). Real wiring (not part of this task - see the package docstring
in `__init__.py`) would construct a `Handlers` whose `generate_reply` IS
main.py's `Luno_Brain`, whose `speak` IS main.py's `speak()`, etc.

Design decisions worth being upfront about (same "honest limitation"
spirit as the rest of this codebase):

- **Tool execution** (priority 3) only guards tools dispatched OUTSIDE a
  normal conversation turn. Tools the LLM decides to call mid-conversation
  are already handled inside whatever `generate_reply` does (real wiring:
  `Luno_Brain`'s existing tool-calling loop) - duplicating that here would
  fork the tool-calling logic into two places. See `conditions.
  tool_execution_pending`'s docstring for the full reasoning.

- **Home Assistant Behavior** (continuous monitoring) has no dedicated
  action of its own. Non-critical HA state (lights/doors/temperature/
  presence) is expected to be written straight onto `Blackboard.room` by
  whatever perceives it (real wiring: a callback passed to `scheduler.
  Scheduler(perceive=...)`), the same way "Watching Behavior runs
  continuously" happens via `Blackboard.visual_events` regardless of
  which STATE is currently active. Only EMERGENCY/CRITICAL severity HA
  events get their own priority-tier actions below, because only those
  need to actively interrupt something.

- **Interruption is cooperative, not preemptive.** Python cannot safely
  force-kill a running thread. `bb.interrupt_requested` is set briefly
  when a higher-priority node takes over from a busy one - a
  long-running handler MAY check it (none of the example handlers here
  do, since they're meant to be short), but nothing in this package can
  guarantee an in-flight `generate_reply`/`speak` call actually stops
  early. What IS guaranteed: the moment that call returns, its
  `on_success`/`on_error` callback still runs and updates the Blackboard,
  it just may be updating state nobody cares about anymore.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from . import conditions
from .blackboard import Blackboard, HAEventSeverity
from .cooldowns import CooldownManager
from .planner import Planner
from .state_machine import LunoState, StateMachine


@dataclass
class Handlers:
    """Every field optional - see module docstring. Types are documented
    in comments rather than strict `Callable[[...], ...]` annotations for
    readability; `Any` is used liberally on purpose since the whole point
    is these get swapped for real Luno functions later."""

    # Speech / conversation
    speak: Optional[Callable[[str], None]] = None                              # (text) -> None
    listen_and_transcribe: Optional[Callable[[], Optional[str]]] = None        # () -> transcript or None
    generate_reply: Optional[Callable[[str, dict], str]] = None                # (user_text, context) -> reply

    # Vision
    capture_vision: Optional[Callable[[str], dict]] = None                     # (question) -> {"description"|"error"}
    query_vision_memory_location: Optional[Callable[[str], Optional[str]]] = None  # (label) -> location or None

    # Home Assistant / tools
    ha_call_service: Optional[Callable[..., Any]] = None                       # (domain, service, entity_id, data) -> Any
    execute_tool: Optional[Callable[[str, dict], Any]] = None                  # (name, args) -> result

    # Avatar / animation
    face_user: Optional[Callable[[], None]] = None
    blink: Optional[Callable[[], None]] = None
    look_around: Optional[Callable[[], None]] = None
    shift_gaze: Optional[Callable[[], None]] = None
    set_avatar_emotion: Optional[Callable[[str], None]] = None                 # (emotion_name) -> None

    # Misc
    background_maintenance: Optional[Callable[[], None]] = None


@dataclass
class RunContext:
    """Bundles everything an action needs besides the Blackboard itself -
    passed as one object so action function signatures stay `(bb, ctx)`
    regardless of how many services get added later."""
    handlers: Handlers
    cooldowns: CooldownManager
    state_machine: StateMachine
    executor: Any  # a concurrent.futures.Executor - see scheduler.py


@dataclass
class ActionResult:
    """What a node did this tick - purely informational (logging/tests),
    NOT used for control flow (state transitions happen inside the action
    itself via `ctx.state_machine`)."""
    node: str
    note: str = ""
    spoke: bool = False


def _dispatch(bb: Blackboard, ctx: RunContext, busy_state: LunoState,
              fn: Callable[[], Any], on_success: Callable[[Blackboard, RunContext, Any], None],
              on_error: Optional[Callable[[Blackboard, RunContext, Exception], None]] = None,
              node_name: str = "") -> None:
    """Run `fn` on `ctx.executor` (a background thread) so the tick loop
    itself NEVER blocks (per the spec's Performance Requirements). Holds
    `busy_state` on the state machine while `fn` is in flight; whichever
    of `on_success`/`on_error` applies runs on the SAME background thread
    once `fn` finishes (not on the tick thread) - both are expected to
    grab `bb.lock` for any multi-field update, per blackboard.py's
    concurrency contract."""
    ctx.state_machine.transition_to(busy_state, reason=node_name)

    def _run() -> None:
        try:
            result = fn()
        except Exception as ex:
            bb.record_error(f"{node_name}: {ex}")
            if on_error is not None:
                try:
                    on_error(bb, ctx, ex)
                except Exception as inner_ex:
                    bb.record_error(f"{node_name} on_error handler failed: {inner_ex}")
            else:
                ctx.state_machine.transition_to(LunoState.ERROR_RECOVERY, reason=str(ex))
            return
        try:
            on_success(bb, ctx, result)
        except Exception as ex:
            bb.record_error(f"{node_name} on_success handler failed: {ex}")
            ctx.state_machine.transition_to(LunoState.ERROR_RECOVERY, reason=str(ex))

    ctx.executor.submit(_run)


# ---------------------------------------------------------------------------
# Priority 0 - Emergency
# ---------------------------------------------------------------------------

def run_emergency(bb: Blackboard, ctx: RunContext) -> ActionResult:
    """Speaks SYNCHRONOUSLY (the one deliberate exception to "always
    dispatch async" in this file) - an emergency message must not sit
    behind whatever else is in the executor's queue. Messages here are
    short by construction, so the tick-blocking cost is bounded."""
    events = bb.unhandled_ha_events(HAEventSeverity.EMERGENCY)
    system_emergency = not bb.system.healthy

    message: Optional[str] = None
    if events:
        event = events[0]
        message = f"Emergency: {event.kind.replace('_', ' ')} detected on {event.entity_id}."
        bb.mark_ha_events_handled(events)
    elif system_emergency:
        message = "I've hit repeated internal errors and need a moment to recover."

    bb.interrupt_requested = True
    if message and ctx.handlers.speak:
        ctx.handlers.speak(message)
    bb.interrupt_requested = False

    if system_emergency:
        bb.clear_errors()  # acknowledged - don't re-fire forever

    ctx.state_machine.transition_to(LunoState.WAITING, reason="emergency acknowledged")
    return ActionResult(node="emergency", note=message or "", spoke=bool(message))


# ---------------------------------------------------------------------------
# Priority 1 - Critical Home Assistant events
# ---------------------------------------------------------------------------

def run_critical_ha(bb: Blackboard, ctx: RunContext) -> ActionResult:
    events = bb.unhandled_ha_events(HAEventSeverity.CRITICAL)
    if not events:
        return ActionResult(node="critical_ha", note="no critical events")
    event = events[0]
    message = f"Heads up - {event.kind.replace('_', ' ')} on {event.entity_id}."
    bb.mark_ha_events_handled(events)
    if ctx.handlers.speak:
        ctx.handlers.speak(message)
    ctx.state_machine.transition_to(LunoState.WAITING, reason="critical HA event handled")
    return ActionResult(node="critical_ha", note=message, spoke=True)


# ---------------------------------------------------------------------------
# Priority 2 - Direct user speech (Listening Behavior)
# ---------------------------------------------------------------------------

def run_listening(bb: Blackboard, ctx: RunContext) -> ActionResult:
    if ctx.state_machine.state == LunoState.LISTENING:
        return ActionResult(node="direct_user_speech", note="already listening")
    if bb.conversation.pending_user_text is not None:
        # A transcript is already waiting to be picked up by the
        # conversation node - don't start listening again on top of it.
        return ActionResult(node="direct_user_speech", note="transcript already pending, not re-listening")

    if ctx.handlers.face_user:
        ctx.handlers.face_user()
    bb.wake_word_detected = False

    if ctx.handlers.listen_and_transcribe is None:
        ctx.state_machine.transition_to(LunoState.WAITING, reason="no STT handler configured")
        return ActionResult(node="direct_user_speech", note="no listen_and_transcribe handler configured")

    def _on_success(bb: Blackboard, ctx: RunContext, transcript: Optional[str]) -> None:
        with bb.lock:
            bb.user.speaking = False
            if transcript:
                bb.conversation.pending_user_text = transcript
        ctx.state_machine.transition_to(LunoState.WAITING, reason="transcript ready")

    _dispatch(bb, ctx, LunoState.LISTENING, ctx.handlers.listen_and_transcribe, _on_success, node_name="listening")
    return ActionResult(node="direct_user_speech", note="listening dispatched")


# ---------------------------------------------------------------------------
# Priority 3 - Tool execution (guard only - see module docstring)
# ---------------------------------------------------------------------------

def run_tool_execution(bb: Blackboard, ctx: RunContext) -> ActionResult:
    return ActionResult(node="tool_execution", note=f"tool '{bb.tool.name}' in progress")


def dispatch_tool(bb: Blackboard, ctx: RunContext, name: str, args: dict) -> None:
    """Helper for anything OUTSIDE a conversation turn that wants to run a
    tool directly (e.g. a future Proactive action auto-turning off a
    forgotten light). Not itself a priority-tier action - callers invoke
    this, which sets `bb.tool.running` so `conditions.tool_execution_
    pending` correctly blocks lower-priority nodes until it finishes."""
    if ctx.handlers.execute_tool is None:
        bb.record_error(f"dispatch_tool('{name}'): no execute_tool handler configured")
        return

    with bb.lock:
        bb.tool.running = True
        bb.tool.name = name
        bb.tool.started_at = bb.now

    def _on_success(bb: Blackboard, ctx: RunContext, result: Any) -> None:
        with bb.lock:
            bb.tool.running = False
            bb.tool.last_result = result
        ctx.state_machine.transition_to(LunoState.WAITING, reason="tool finished")

    def _on_error(bb: Blackboard, ctx: RunContext, ex: Exception) -> None:
        with bb.lock:
            bb.tool.running = False
            bb.tool.last_result = None

    _dispatch(bb, ctx, LunoState.EXECUTING_TOOL, lambda: ctx.handlers.execute_tool(name, args),
              _on_success, on_error=_on_error, node_name=f"tool:{name}")


# ---------------------------------------------------------------------------
# Priority 4 - Conversation continuation
# ---------------------------------------------------------------------------

def _speak_and_finish(bb: Blackboard, ctx: RunContext, reply: str) -> None:
    def _do_speak() -> None:
        if ctx.handlers.speak and reply:
            ctx.handlers.speak(reply)

    def _on_done(bb: Blackboard, ctx: RunContext, _result: Any) -> None:
        with bb.lock:
            bb.conversation.ongoing = False
        ctx.state_machine.transition_to(LunoState.IDLE, reason="turn complete")

    _dispatch(bb, ctx, LunoState.TALKING, _do_speak, _on_done, node_name="talking")


def run_conversation(bb: Blackboard, ctx: RunContext) -> ActionResult:
    if bb.conversation.pending_user_text is not None and not bb.conversation.thinking:
        user_text = bb.conversation.pending_user_text
        with bb.lock:
            bb.conversation.pending_user_text = None
            bb.conversation.thinking = True
            bb.conversation.ongoing = True
            bb.conversation.last_user_text = user_text

        def _on_success(bb: Blackboard, ctx: RunContext, reply: str) -> None:
            with bb.lock:
                bb.conversation.thinking = False
                bb.conversation.last_reply = reply
                bb.conversation.turn_count += 1
                bb.conversation.last_turn_at = bb.now
            _speak_and_finish(bb, ctx, reply)

        def _on_error(bb: Blackboard, ctx: RunContext, ex: Exception) -> None:
            with bb.lock:
                bb.conversation.thinking = False
                bb.conversation.ongoing = False

        def _generate() -> str:
            return Planner.handle_user_text(bb, ctx.handlers, user_text)

        _dispatch(bb, ctx, LunoState.THINKING, _generate, _on_success, on_error=_on_error, node_name="conversation")
        return ActionResult(node="conversation_continuation", note=f"thinking about: {user_text[:60]}")

    if ctx.state_machine.state in (LunoState.THINKING, LunoState.TALKING):
        return ActionResult(node="conversation_continuation", note="turn in progress")

    # `conversation.ongoing` True but nothing pending/thinking/talking - a
    # stray state (e.g. left over after an error) - close it out instead
    # of permanently blocking priority 4 for no reason.
    with bb.lock:
        bb.conversation.ongoing = False
    ctx.state_machine.transition_to(LunoState.IDLE, reason="conversation state cleanup")
    return ActionResult(node="conversation_continuation", note="stray conversation state cleaned up")


# ---------------------------------------------------------------------------
# Priority 5 - Visual events (Watching Behavior - silent)
# ---------------------------------------------------------------------------

def run_watching(bb: Blackboard, ctx: RunContext) -> ActionResult:
    events = bb.unhandled_visual_events()
    if not events:
        return ActionResult(node="visual_events", note="no new visual events")
    for event in events:
        bb.push_event(f"(watching) {event.description}")
    bb.mark_visual_events_handled(events)
    # Per spec: "Do not speak unless another behavior requests it." A
    # lower-priority Proactive node may independently decide to mention
    # one of these same events next tick via conditions.find_proactive_
    # candidate() - this node itself never calls handlers.speak.
    ctx.state_machine.transition_to(LunoState.WATCHING, reason="observed visual event(s)")
    return ActionResult(node="visual_events", note=f"logged {len(events)} visual event(s)")


# ---------------------------------------------------------------------------
# Proactive Behavior
# ---------------------------------------------------------------------------

def run_proactive(bb: Blackboard, ctx: RunContext) -> ActionResult:
    candidate = conditions.proactive_eligible(bb, ctx.cooldowns)
    if candidate is None:
        return ActionResult(node="proactive", note="no eligible candidate")
    if ctx.handlers.speak is None:
        return ActionResult(node="proactive", note="no speak handler configured")

    ctx.cooldowns.mark_fired(candidate.key)
    message = candidate.message_hint
    bb.push_event(f"(proactive) {message}")

    def _do_speak() -> None:
        ctx.handlers.speak(message)

    def _on_done(bb: Blackboard, ctx: RunContext, _result: Any) -> None:
        ctx.state_machine.transition_to(LunoState.IDLE, reason="proactive remark done")

    _dispatch(bb, ctx, LunoState.TALKING, _do_speak, _on_done, node_name="proactive")
    return ActionResult(node="proactive", note=message, spoke=True)


# ---------------------------------------------------------------------------
# Idle Behavior (generic fallback)
# ---------------------------------------------------------------------------

def run_idle(bb: Blackboard, ctx: RunContext) -> ActionResult:
    ctx.state_machine.transition_to(LunoState.IDLE, reason="nothing higher priority")
    did = []
    if ctx.handlers.blink and ctx.cooldowns.is_ready("idle:blink", 4.0):
        ctx.handlers.blink()
        ctx.cooldowns.mark_fired("idle:blink")
        did.append("blink")
    if ctx.handlers.shift_gaze and ctx.cooldowns.is_ready("idle:shift_gaze", 6.0):
        ctx.handlers.shift_gaze()
        ctx.cooldowns.mark_fired("idle:shift_gaze")
        did.append("shift_gaze")
    if ctx.handlers.look_around and ctx.cooldowns.is_ready("idle:look_around", 20.0):
        ctx.handlers.look_around()
        ctx.cooldowns.mark_fired("idle:look_around")
        did.append("look_around")
    return ActionResult(node="idle", note=", ".join(did) or "quiet idle tick")


# ---------------------------------------------------------------------------
# Error recovery (quiet cleanup - only runs when nothing more urgent is happening)
# ---------------------------------------------------------------------------

def run_error_recovery(bb: Blackboard, ctx: RunContext) -> ActionResult:
    count = bb.system.consecutive_errors
    bb.clear_errors()
    return ActionResult(node="error_recovery", note=f"cleared {count} logged error(s)")


# ---------------------------------------------------------------------------
# Sleep
# ---------------------------------------------------------------------------

def run_sleep(bb: Blackboard, ctx: RunContext) -> ActionResult:
    if ctx.state_machine.state != LunoState.SLEEPING:
        ctx.state_machine.transition_to(LunoState.SLEEPING, reason="user absent, late hour")
        if ctx.handlers.speak and ctx.cooldowns.is_ready("sleep:announce", 3600.0):
            ctx.handlers.speak("I'll rest for a bit - just say my name if you need me.")
            ctx.cooldowns.mark_fired("sleep:announce")
    return ActionResult(node="sleep", note="sleeping")


# ---------------------------------------------------------------------------
# Background maintenance
# ---------------------------------------------------------------------------

def run_background_maintenance(bb: Blackboard, ctx: RunContext) -> ActionResult:
    ctx.cooldowns.mark_fired("background_maintenance")
    if ctx.handlers.background_maintenance:
        ctx.handlers.background_maintenance()
    return ActionResult(node="background_maintenance", note="maintenance hook ran")
