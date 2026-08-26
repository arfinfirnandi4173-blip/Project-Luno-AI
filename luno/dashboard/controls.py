"""
controls.py
============

Every button the spec's "Controls" page asks for. Each function here is
a THIN call-through to something that already exists and is already
public - `runtime.reload()`, `session_manager.force_sleep()`,
`adapter_manager.restart(name)`, `planner.clear()` - or a `publish()`
of an Event onto the SAME Event Bus a real spoken interrupt/wake word/
smoke alarm would produce. Nothing in this file contains new decision
logic; every function is a one-to-a-few-line dispatch, exactly mirroring
what `luno/bootstrap/console.py`'s `/reload`, `/restart`, `/sleep`,
`/wake`, `/emergency` commands (and `main_runtime_demo.py`'s own
`/emergency`, `/sleep`, `/wake`) already do for the terminal console -
this is the same set of actions, callable over HTTP instead of typed.

Mapping (control name -> what actually runs):

    reload_configuration   -> ProductionConsole.reload()'s exact sequence
                               (runtime.reload(), adapter_manager.
                               restart_all(), memory_retriever.
                               reload_config(), launcher_config.reload())
    restart_runtime         -> runtime.restart()
    restart_module(name)    -> runtime.module_manager.restart(name)
    restart_adapter(name)   -> adapter_manager.restart(name)
    sleep_session            -> session_manager.force_sleep() (same as /sleep)
    wake_session               -> session_manager.force_wake() (same as /wake)
    clear_planner_queue          -> planner_module.planner.clear()
    cancel_current_llm             -> publish(cancel_llm_request) for the
                                       CURRENT request_id read from
                                       `barge_in_module.status_snapshot()` -
                                       the exact event
                                       `barge_in/manager.py`'s own
                                       `_do_free_interrupt()`/
                                       `_do_soft_interrupt()` already
                                       publish internally
    stop_speech                      -> publish(speech_recognized) with the
                                         FIRST configured interrupt word
                                         (`barge_in_config.interrupt_words[0]`) -
                                         i.e. simulates a real spoken
                                         interrupt, goes through the exact
                                         same classifier/routing a real one
                                         would
    resume_speech                      -> publish(speech_recognized) with the
                                           first configured resume word
    browser_mic_utterance(text)          -> publish(speech_recognized) with
                                             browser-transcribed text (Chat
                                             panel's "Wake Listen" toggle) -
                                             NO force_wake, unlike
                                             send_chat_message - lets
                                             SessionManagerModule's own
                                             wake-word matcher decide, same
                                             as a real microphone would
    approve_goal(goal_id)                  -> ProactiveModule.approve_goal() -
                                               Goals panel manual confirmation
    reject_goal(goal_id)                     -> ProactiveModule.reject_goal()
    emergency_stop                       -> publish(Event(type="smoke_detected"))
                                             (same as main_runtime_demo.py's
                                             own `/emergency` command) -
                                             forces CRITICAL barge-in mode
    emergency_clear                        -> barge_in_module.clear_emergency()
    enable_debug / disable_debug             -> dashboard-LOCAL flag only
                                                 (never touches Runtime) -
                                                 see `server.py`'s
                                                 `_debug_enabled`

Every function returns a small `{"ok": bool, "message": str}`-shaped
dict - `server.py` serializes it straight to JSON; nothing here talks
HTTP.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, Optional

from luno.barge_in.matcher import match_confirmation, match_interrupt_word, match_resume_word
from luno.core.events import Event

if TYPE_CHECKING:
    from luno.adapters.manager import AdapterManager
    from luno.core.runtime import Runtime
    from luno.bootstrap.launcher_config import LauncherConfig


def _ok(message: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": True, "message": message, **extra}


def _fail(message: str) -> Dict[str, Any]:
    return {"ok": False, "message": message}


def reload_configuration(runtime: "Runtime", adapter_manager: "AdapterManager", modules: Dict[str, Any], launcher_config: "LauncherConfig") -> Dict[str, Any]:
    """Byte-for-byte the same sequence `ProductionConsole.reload()`
    (the `/reload` command) already runs."""
    try:
        runtime.reload()
        try:
            adapter_manager.restart_all()
        except Exception as ex:
            return _ok(f"reloaded (adapter restart raised: {ex})")
        try:
            modules["planner_module"].memory_retriever.reload_config()
        except Exception:
            pass
        new_config = launcher_config.reload()
        launcher_config.__dict__.update(new_config.__dict__)
        return _ok("configuration reloaded")
    except Exception as ex:
        return _fail(f"reload failed: {ex}")


def restart_runtime(runtime: "Runtime") -> Dict[str, Any]:
    try:
        runtime.restart()
        return _ok("runtime restarted")
    except Exception as ex:
        return _fail(f"restart failed: {ex}")


def restart_module(runtime: "Runtime", name: str) -> Dict[str, Any]:
    try:
        runtime.module_manager.restart(name)
        return _ok(f"module '{name}' restarted")
    except Exception as ex:
        return _fail(f"restart '{name}' failed: {ex}")


def restart_adapter(adapter_manager: "AdapterManager", name: str) -> Dict[str, Any]:
    try:
        adapter_manager.restart(name)
        return _ok(f"adapter '{name}' restarted")
    except Exception as ex:
        return _fail(f"restart '{name}' failed: {ex}")


def switch_llm_provider(adapter_manager: "AdapterManager", provider: str) -> Dict[str, Any]:
    """LLM panel's "switch provider" control - spec's Runtime Switching
    example (`OpenRouter -> /reload -> Provider Gemini -> Next request
    uses Gemini`). Calls `LLMManagerAdapter.switch_provider()` directly
    (registered under the module id `"openrouter"` - see
    `luno/adapters/llm_manager.py`'s own docstring) rather than
    publishing a synthetic `ReloadModel` event, so this returns a real
    success/failure instead of firing into the Event Bus and hoping."""
    registry = getattr(adapter_manager, "registry", None)
    adapter = registry.get("openrouter") if registry is not None else None
    if adapter is None or not hasattr(adapter, "switch_provider"):
        return _fail("LLM Manager adapter not registered")
    try:
        ok = adapter.switch_provider(provider)
        if not ok:
            return _fail(f"unknown provider '{provider}'")

        # Bug fix: `switch_provider()` succeeds for ANY valid provider
        # name even if it has no API key / never initialized - the
        # adapter then silently skips it in `_priority_order()` and
        # every request actually lands on whichever OTHER provider IS
        # configured, with zero indication why. Report that honestly
        # here instead of a bare "switched successfully".
        configured = bool(getattr(adapter, "provider_configured", lambda _n: True)(provider))
        if not configured:
            return _ok(
                f"'{provider}' is now the preferred provider, but it has NO API key configured yet - "
                f"requests will keep silently using whichever provider IS configured until you add "
                f"a real API key for '{provider}' to .env and reload configuration.",
                warning=True,
            )
        return _ok(f"switched active LLM provider to '{provider}' - in-flight requests finish normally, new ones use it")
    except Exception as ex:
        return _fail(f"switch to '{provider}' failed: {ex}")


def sleep_session(modules: Dict[str, Any]) -> Dict[str, Any]:
    session_manager = modules.get("session_manager")
    if session_manager is None:
        return _fail("session_manager not registered")
    session_manager.force_sleep(reason="dashboard control")
    return _ok("session forced to Sleeping")


def wake_session(modules: Dict[str, Any]) -> Dict[str, Any]:
    session_manager = modules.get("session_manager")
    if session_manager is None:
        return _fail("session_manager not registered")
    session_manager.force_wake(reason="dashboard control")
    return _ok("wake sequence forced")


def clear_planner_queue(modules: Dict[str, Any]) -> Dict[str, Any]:
    planner_module = modules.get("planner_module")
    if planner_module is None:
        return _fail("planner not registered")
    try:
        cleared = planner_module.planner.clear()
        return _ok(f"cleared {cleared} plan(s)", cleared=cleared)
    except Exception as ex:
        return _fail(f"clear failed: {ex}")


def cancel_current_llm(runtime: "Runtime", modules: Dict[str, Any]) -> Dict[str, Any]:
    barge_in_module = modules.get("barge_in_module")
    request_id = None
    if barge_in_module is not None:
        request_id = barge_in_module.status_snapshot().get("current_request_id")
    runtime.event_bus.publish(Event(type="cancel_llm_request", data={"request_id": request_id, "source": "dashboard"}))
    return _ok(f"cancel requested for request_id={request_id}")


def stop_speech(runtime: "Runtime", modules: Dict[str, Any]) -> Dict[str, Any]:
    barge_in_config = modules.get("barge_in_config")
    word = barge_in_config.interrupt_words[0] if barge_in_config and barge_in_config.interrupt_words else "stop"
    runtime.event_bus.publish(Event(type="speech_recognized", data={"text": word, "confidence": None, "source": "dashboard"}))
    return _ok(f"published simulated interrupt utterance ({word!r})")


def resume_speech(runtime: "Runtime", modules: Dict[str, Any]) -> Dict[str, Any]:
    barge_in_config = modules.get("barge_in_config")
    word = barge_in_config.resume_words[0] if barge_in_config and barge_in_config.resume_words else "resume"
    runtime.event_bus.publish(Event(type="speech_recognized", data={"text": word, "confidence": None, "source": "dashboard"}))
    return _ok(f"published simulated resume utterance ({word!r})")


def approve_goal(modules: Dict[str, Any], goal_id: str) -> Dict[str, Any]:
    """Backs the Goals panel's manual-approval button for a goal the
    Policy Engine marked AWAITING_CONFIRMATION (medium confidence). A
    thin call-through to `ProactiveModule.approve_goal()` - same
    "everything here just dispatches to something that already exists"
    rule as every other function in this file."""
    proactive_module = modules.get("proactive_module")
    if proactive_module is None:
        return _fail("proactive module not registered")
    if not goal_id:
        return _fail("goal_id required")
    return proactive_module.approve_goal(goal_id)


def reject_goal(modules: Dict[str, Any], goal_id: str) -> Dict[str, Any]:
    proactive_module = modules.get("proactive_module")
    if proactive_module is None:
        return _fail("proactive module not registered")
    if not goal_id:
        return _fail("goal_id required")
    return proactive_module.reject_goal(goal_id)


def browser_mic_utterance(runtime: "Runtime", text: str, confidence: Optional[float] = None) -> Dict[str, Any]:
    """Backs the Chat panel's "Wake Listen" toggle - continuous browser
    Web Speech API transcription standing in for a real always-on
    microphone (see `RealWhisperSource`/`legacy_main.py`'s own
    `sr.Microphone()` loop, which needs a physical mic wired to the
    machine RUNNING `main.py`, not the browser tab viewing the
    dashboard).

    Deliberately NOT the same shape as `send_chat_message()`: a chat
    message is an explicit "I am talking to Luno right now" action, so
    it force-wakes on purpose. A wake-listen utterance is the opposite -
    ambient, continuous, "maybe nothing, maybe the wake word, maybe just
    someone talking nearby" - exactly what a real microphone produces
    non-stop. So this publishes the SAME raw `speech_recognized` event
    a real mic utterance (or `ProductionConsole`'s own "plain text
    simulates speech" path - see `bootstrap/console.py::handle_line()`)
    would, with NO force_wake and no session-state gating here at all -
    `SessionManagerModule`'s own `match_wake_word()` is the ONLY thing
    that decides whether this utterance wakes Luno up, extends an
    active session, or is silently dropped while dormant. Mirrors
    `stop_speech()`/`resume_speech()` above exactly (same one-line
    publish shape), just with browser-supplied text instead of a
    configured interrupt/resume word."""
    if not text or not text.strip():
        return _fail("empty utterance")
    runtime.event_bus.publish(Event(type="speech_recognized", data={"text": text.strip(), "confidence": confidence, "source": "browser_mic"}))
    return _ok("utterance published")


def emergency_stop(runtime: "Runtime") -> Dict[str, Any]:
    runtime.event_bus.publish(Event(type="smoke_detected", data={"injected": True, "source": "dashboard"}))
    return _ok("smoke_detected injected - barge-in mode forced to CRITICAL")


def emergency_clear(modules: Dict[str, Any]) -> Dict[str, Any]:
    barge_in_module = modules.get("barge_in_module")
    if barge_in_module is None:
        return _fail("barge_in not registered")
    barge_in_module.clear_emergency()
    return _ok("emergency cleared")


#: How long `send_chat_message()` waits for the session to actually
#: leave AWAKENING (i.e. for the "Yes?" wake acknowledgement to finish
#: playing - see `wake_session/manager.py::_handle_playback_done()`)
#: before giving up and reporting a timeout, when it had to auto-wake
#: a sleeping session first.
_WAKE_SETTLE_TIMEOUT_S = 5.0
_WAKE_SETTLE_POLL_S = 0.05

#: States `SessionManagerModule._handle_speech_recognized()` will
#: actually forward an utterance from (see that method's own branching -
#: LISTENING/WAITING_USER/IDLE). Reproduced here as a constant (not
#: imported - `wake_session` has no public constant for this, and this
#: package's own "read the same state, never import private branching
#: logic across a package boundary" rule already applies everywhere
#: else in this codebase) purely so `send_chat_message()` can decide
#: whether to publish immediately or wait for a just-triggered wake to
#: settle first.
_FORWARDABLE_STATES = ("listening", "waiting_user", "idle")
#: AWAKENING is a brief, self-resolving transitional state (the "Yes?"
#: wake acknowledgement is playing - typically well under a second on
#: the mock backend, a few seconds on a real TTS backend) - a message
#: arriving while it's in flight should WAIT for it to settle, same as
#: a freshly-woken SLEEPING session does, not be rejected. Only
#: THINKING/SPEAKING (Luno is actively working on or speaking a
#: DIFFERENT reply already) are genuinely "busy, try again" states -
#: bug fix: AWAKENING used to be lumped in with these and rejected
#: outright, which meant any chat message sent within that transitional
#: window (including, ironically, the message that itself triggered the
#: wake, if a second one raced it) surfaced a confusing
#: "Luno is busy (state=awakening)" error instead of just... waiting the
#: usual fraction of a second and going through.
_BUSY_STATES = ("thinking", "speaking")
_WAITABLE_STATES = ("sleeping", "awakening")


def _is_barge_in_relevant(text: str, modules: Dict[str, Any]) -> bool:
    """Bug fix: `send_chat_message()`'s busy-guard below used to reject
    ANY text outright while `state in _BUSY_STATES` - including an
    interrupt word ("stop"/"cancel") typed into the Chat panel while
    Luno is mid-reply, which is EXACTLY the situation barge-in exists to
    handle. A real microphone utterance never goes through this guard at
    all (`RealWhisperSource` publishes `speech_recognized` unconditionally,
    same as `ProductionConsole.handle_line()`'s typed-text path) - this
    function lets the Chat panel match that behavior for barge-in-
    relevant text specifically, without opening the busy-guard up
    entirely (an ordinary chat message sent while Luno is mid-reply
    should still wait its turn, same as before).

    Reads `barge_in_module.config` directly (never its own
    `BargeInConfig.from_env()` call) so a `/reload`-ed word list is
    always respected, and checks `awaiting_confirmation` the same way
    `BargeInModule._handle_speech()` itself does - a CONFIRM answer
    ("yes"/"no") is only barge-in-relevant while a confirmation is
    actually pending, otherwise an ordinary "yes" typed in chat would
    wrongly bypass the busy-guard too."""
    barge_in_module = modules.get("barge_in_module")
    if barge_in_module is None:
        return False
    config = barge_in_module.config
    if match_interrupt_word(text, config.interrupt_words):
        return True
    if match_resume_word(text, config.resume_words):
        return True
    if barge_in_module.awaiting_confirmation:
        if match_confirmation(text, config.confirm_yes_words, config.confirm_no_words) is not None:
            return True
    return False


def memory_archive(memory_id: str) -> Dict[str, Any]:
    """Memory Dashboard's Archive button - thin call-through to the
    EXISTING `memory.archive_memory_by_id()` (already id-targeted,
    already refuses on a protected entry - "defense in depth" is
    inherited for free, not reimplemented here). Archive is reversible
    (Unarchive below) and never deletes anything - Phase 7's own
    "Archive bukan delete.\""""
    from luno import memory

    if not memory_id:
        return _fail("memory id required")
    status, entry = memory.archive_memory_by_id(memory_id)
    if status == "archived":
        return _ok(f"memory {memory_id} archived", entry=entry)
    if status == "protected":
        return _fail(f"memory {memory_id} is protected (importance=4 or unresolved conflict) - cannot be auto-archived")
    return _fail(f"memory {memory_id} not found")


def memory_unarchive(memory_id: str) -> Dict[str, Any]:
    """Memory Dashboard's Unarchive button - thin call-through to the
    new id-targeted `memory.unarchive_memory_by_id()` (see
    docs/change_impact/memory_dashboard.md's "Gap found" section for why
    this small addition was needed)."""
    from luno import memory

    if not memory_id:
        return _fail("memory id required")
    entry = memory.unarchive_memory_by_id(memory_id)
    if entry is None:
        return _fail(f"memory {memory_id} not found or was not archived")
    return _ok(f"memory {memory_id} unarchived", entry=entry)


def memory_delete(memory_id: str, confirm: Any) -> Dict[str, Any]:
    """Memory Dashboard's Delete button - Phase 7/13's explicit
    "confirmation required, and the SERVER decides validity, never just
    a `confirmed=true` flag trusted blindly." `confirm` must be the
    literal boolean `True` (a strict identity check, not merely
    truthy) - a missing field, a stray `"false"` string, or any other
    value is treated as "not confirmed" and refused, before
    `memory.delete_memory_by_id()` (the EXISTING, already id-targeted
    deletion function) is ever called. Delete is permanent - unlike
    Archive, there is no undo (Phase 7's own "Delete this memory
    permanently?\" confirmation copy)."""
    from luno import memory

    if not memory_id:
        return _fail("memory id required")
    if confirm is not True:
        return _fail("confirmation required - this action permanently deletes the memory")
    text = memory.delete_memory_by_id(memory_id)
    if text is None:
        return _fail(f"memory {memory_id} not found")
    return _ok(f"memory {memory_id} permanently deleted", deleted_text=text)


def memory_update(memory_id: str, text: str) -> Dict[str, Any]:
    """Memory Dashboard's text-edit control - thin call-through to the
    EXISTING `memory.update_memory()`, which already preserves
    history/conflict semantics on its own (Phase 7's own "Update harus
    mempertahankan history/conflict semantics" is inherited for free,
    not reimplemented here). `reason="dashboard_edit"` makes a
    dashboard-originated edit distinguishable from a conversational one
    in the entry's own `history[]`, for free."""
    from luno import memory

    if not memory_id:
        return _fail("memory id required")
    if not text or not text.strip():
        return _fail("text required")
    updated = memory.update_memory(memory_id, text, reason="dashboard_edit")
    if updated is None:
        return _fail(f"memory {memory_id} not found")
    return _ok(f"memory {memory_id} updated", entry=updated)


def memory_mark_important(memory_id: str) -> Dict[str, Any]:
    """Memory Dashboard's "Mark important" button - thin call-through to
    the new id-targeted `memory.mark_memory_important_by_id()`.
    Deliberately the ONLY importance-editing control this sprint exposes
    (-> 4, "core") - no production surface anywhere in this codebase
    lets a user set importance to an arbitrary specific level, so the
    dashboard does not invent one either (see change-impact doc)."""
    from luno import memory

    if not memory_id:
        return _fail("memory id required")
    entry = memory.mark_memory_important_by_id(memory_id)
    if entry is None:
        return _fail(f"memory {memory_id} not found")
    return _ok(f"memory {memory_id} marked as important (core)", entry=entry)


def memory_feedback_positive(memory_id: str) -> Dict[str, Any]:
    """Memory Dashboard's "Mark useful" button (Memory Learning &
    Feedback Loop sprint) - thin call-through to the EXISTING
    `memory.apply_positive_feedback()`. Unlike the conversational feedback
    path (`main_runtime_demo.py`'s session-scoped target, needed because a
    spoken/typed "iya benar" carries no id of its own), the dashboard
    already has an explicit `memory_id` from the row the user clicked -
    no target-resolution ambiguity is possible here, so this is a direct,
    unconditional call, same shape as `memory_mark_important()` above."""
    from luno import memory

    if not memory_id:
        return _fail("memory id required")
    entry = memory.apply_positive_feedback(memory_id, reason="dashboard_feedback")
    if entry is None:
        return _fail(f"memory {memory_id} not found")
    # Memory Evaluation & Self-Calibration sprint - same synchronous
    # record-then-recalibrate pattern `main_runtime_demo.py`'s
    # conversational/explicit feedback handlers already use, so a
    # dashboard-originated feedback event is evidence to `evaluate_memory()`
    # exactly as promptly as a conversational one. Memory Outcome
    # Telemetry sprint (Step 7) - same evidence-mapping bump those
    # handlers now also apply.
    memory.record_outcome_evidence(memory_id, "positive")
    memory.record_feedback_event(memory_id)
    entry = memory.calibrate_memory(memory_id) or entry
    return _ok(f"memory {memory_id} marked useful (usefulness={entry.get('usefulness_score')})", entry=entry)


def memory_feedback_negative(memory_id: str) -> Dict[str, Any]:
    """Memory Dashboard's "Mark not useful" button - thin call-through to
    the EXISTING `memory.apply_negative_feedback()`. Never deletes or
    rewrites the memory - same honesty as every other control here."""
    from luno import memory

    if not memory_id:
        return _fail("memory id required")
    entry = memory.apply_negative_feedback(memory_id, reason="dashboard_feedback")
    if entry is None:
        return _fail(f"memory {memory_id} not found")
    memory.record_outcome_evidence(memory_id, "negative")
    memory.record_feedback_event(memory_id)
    entry = memory.calibrate_memory(memory_id) or entry
    return _ok(f"memory {memory_id} marked not useful (usefulness={entry.get('usefulness_score')})", entry=entry)


def memory_recalibrate(memory_id: str) -> Dict[str, Any]:
    """Memory Dashboard's "Recalibrate" button (Memory Evaluation &
    Self-Calibration sprint, Step 11) - thin call-through to the EXISTING
    `memory.calibrate_memory()`. Lets a user (or the E2E test suite)
    explicitly refresh a memory's persisted `evaluation_score`/
    `last_evaluated_at` from its CURRENT accumulated evidence on demand,
    without needing to trigger a feedback event first - same "explicit
    action, explicit endpoint" shape as `memory_apply_maintenance()`
    below. Writes ONLY `evaluation_score`/`last_evaluated_at` -
    `calibrate_memory()`'s own docstring is the enforcement point for
    that guarantee, not this thin wrapper."""
    from luno import memory

    if not memory_id:
        return _fail("memory id required")
    entry = memory.calibrate_memory(memory_id)
    if entry is None:
        return _fail(f"memory {memory_id} not found")
    return _ok(f"memory {memory_id} recalibrated (evaluation={entry.get('evaluation_score')})", entry=entry)


def memory_apply_maintenance(confirm: Any) -> Dict[str, Any]:
    """Memory Dashboard's "Apply Maintenance" button - Phase 9's
    Preview -> Apply flow, second half. Requires the same strict
    `confirm is True` check `memory_delete()` uses (Phase 13). ALWAYS
    recomputes a FRESH `analyze_memory_maintenance()` plan right here,
    server-side, rather than trusting any plan the client might echo
    back (which could be stale - built from an earlier snapshot the
    store has since changed under - or, in principle, tampered with) -
    this satisfies both Phase 9's "Preview first, don't apply directly"
    AND Phase 13's "the server decides whether the operation is valid,
    never the client" in one design choice. Delegates the actual
    mutation entirely to the EXISTING `apply_maintenance_plan()` - every
    protection/threshold rule it already enforces (never deletes,
    refuses on protected entries, consolidate only above the confidence
    threshold) applies unchanged."""
    from luno import memory

    if confirm is not True:
        return _fail("confirmation required - this action archives/reinforces/consolidates memories per the maintenance plan")
    plan = memory.analyze_memory_maintenance()
    results = memory.apply_maintenance_plan(plan)
    applied = sum(1 for r in results if r.get("status") == "applied")
    return _ok(f"maintenance applied - {applied} of {len(results)} plan item(s) changed something", results=results)


def send_chat_message(runtime: "Runtime", modules: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Backs the Chat panel's "send" action. A chat message is a
    deliberate, explicit interaction (the user typed/spoke directly
    into a dedicated chat box) - unlike ambient microphone audio, it
    doesn't need wake-word filtering to avoid false triggers, but it
    STILL goes through the real `SessionManagerModule` state machine
    rather than bypassing it (no direct `user_utterance`/
    `conversation_speech` publish here - see module docstring's mapping
    table): if the session is asleep, this calls the SAME
    `force_wake()` the "Wake Session" control already uses; either way
    (freshly woken, or already mid-wake from a near-simultaneous
    trigger), it waits for the resulting wake acknowledgement to finish
    (so the message lands once `SessionManagerModule` is actually ready
    to forward it, exactly the timing a real "say the wake word, hear
    'Yes?', then speak" exchange already has), THEN publishes the
    message as a normal `speech_recognized` event - the exact same
    event a real microphone utterance produces, so every existing rule
    (interrupt-priority checking, Barge-In's own independent fan-out,
    session timeout handling, ...) applies unchanged."""
    if not text or not text.strip():
        return _fail("empty message")

    session_manager = modules.get("session_manager")
    if session_manager is None:
        return _fail("session_manager not registered")

    try:
        state = session_manager.status_snapshot().get("state")
    except Exception as ex:
        return _fail(f"could not read session state: {ex}")

    if state in _BUSY_STATES:
        if _is_barge_in_relevant(text, modules):
            runtime.event_bus.publish(Event(type="speech_recognized", data={
                "text": text.strip(), "confidence": None, "source": "dashboard_chat",
            }))
            return _ok("interrupt sent")
        return _fail(f"Luno is busy right now (state={state}) - try again in a moment")

    if state in _WAITABLE_STATES:
        if state == "sleeping":
            session_manager.force_wake(reason="dashboard chat")
        settled = False
        deadline = time.time() + _WAKE_SETTLE_TIMEOUT_S
        while time.time() < deadline:
            current = session_manager.status_snapshot().get("state")
            if current in _FORWARDABLE_STATES:
                settled = True
                break
            if current in _BUSY_STATES:
                if _is_barge_in_relevant(text, modules):
                    runtime.event_bus.publish(Event(type="speech_recognized", data={
                        "text": text.strip(), "confidence": None, "source": "dashboard_chat",
                    }))
                    return _ok("interrupt sent")
                # something else (a real wake word, a different control)
                # moved this turn along while we were waiting - no longer
                # our place to forward a second utterance on top of it.
                return _fail(f"Luno is busy right now (state={current}) - try again in a moment")
            time.sleep(_WAKE_SETTLE_POLL_S)
        if not settled:
            return _fail("timed out waiting for the session to wake up")

    runtime.event_bus.publish(Event(type="speech_recognized", data={"text": text.strip(), "confidence": None, "source": "dashboard_chat"}))
    return _ok("message sent")
