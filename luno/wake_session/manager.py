"""
manager.py
==========

`SessionManagerModule` - the Event Bus adapter around `ConversationSession`
and `matcher.match_wake_word()`. This is the ONLY file in this package
that knows the Event Bus exists; everything it decides is delegated to
the pure, standalone `ConversationSession`/`matcher` logic, exactly the
"thin wrapper Module around a real, protected-package-agnostic engine"
pattern already used for `PlannerBridgeModule`/`BehaviorTreeModule` in
`main_runtime_demo.py`.

Wiring (done by whoever owns the Runtime - see `main_runtime_demo.py`):

    speech_recognized   -> session_manager   (was previously routed
    wake_word_detected  -> session_manager    straight to behavior_tree -
    speak_request        -> session_manager    see that file's routing
    speech_playback_*   -> session_manager    table for the one-line
    llm_error             -> session_manager    change this required)
    llm_cancelled          -> session_manager

Sprint 3 note: this module keys its THINKING -> SPEAKING transition
(and its own wake-acknowledgement tracking) off `speak_request`, NOT
`assistant_response`. `AssistantResponse` is Sprint 3's "conversation
record" event (published once, raw/un-normalized, by `OpenRouterAdapter`
- used for history/context/display); `SpeakRequest` is "literally speak
this text now" (published by `BehaviorTreeModule._speak()` AFTER
running the reply through the text normalizer, and by this module's own
`_do_wake()` for the wake acknowledgement). Keying off `speak_request`
means this module's state accurately reflects when Luno is ACTUALLY
about to make sound, regardless of how many "here's what the LLM said"
record events came before it.

Production-Safe LLM -> TTS Streaming Activation sprint - bug fix (real
deadlock, found empirically, not theoretical): the note above was
written when `speak_request` was the ONLY way a reply ever got spoken.
Once a turn is fully spoken incrementally via `StreamingSpeechCoordinator`/
`SpeakStreamChunk` (see `main_runtime_demo.py::BehaviorTreeModule._speak()`'s
own "do NOT publish a second... SpeakRequest" rule), `speak_request`
NEVER fires for that turn at all - `_handle_speak_request()` below was
therefore never reached, `self.session.state` stayed at THINKING
forever (THINKING is not in `TIMEOUT_ACTIVE_STATES`, so nothing ever
times it out either), and `_handle_playback_done()`'s own `if
self.session.state == ConversationState.SPEAKING` guard silently no-op'd
on every subsequent `speech_playback_finished`/`speech_playback_cancelled`
- a PERMANENT session deadlock after the very first fully-streamed
reply: every later utterance fell into `_handle_speech_recognized()`'s
"AWAKENING/THINKING/SPEAKING - busy, not forwarded" branch, silently,
forever. This module now also subscribes to `speech_playback_started`
(published by `FishAudioAdapter` for BOTH the legacy and streaming
paths, unmodified) and makes the SAME THINKING/IDLE -> SPEAKING
transition there, guarded so it is a harmless no-op for the legacy path
(state is already SPEAKING by the time playback audibly starts, since
`speak_request` already made the transition earlier) - see
`_handle_playback_started()` below for the full explanation.

    session_manager --publishes "conversation_speech"--> behavior_tree
                     (only once a session is genuinely awake - this is
                      the front-door gate: Sleeping speech never reaches
                      Behavior Tree/Planner/Tool Manager/OpenRouter at all)

Nothing here calls Behavior Tree, Planner, Tool Manager, or any adapter
directly - every decision this module makes is expressed as a published
Event, per the project's one hard architecture rule.

Bug fix note (wake session / barge-in integration): wake-word gating
only ever applies while SLEEPING - once a session is open
(LISTENING/THINKING/SPEAKING/WAITING_USER, or IDLE in always-on mode),
NO state here blocks `BargeInModule` from seeing/acting on an interrupt
phrase, because `BargeInModule` subscribes to the SAME raw
`speech_recognized` independently (see `main_runtime_demo.py`'s routing
table - plain Event Bus fan-out, not a chain). The one thing THIS
module needed fixing was forwarding an obviously interrupt/resume-like
utterance ("stop", "resume", ...) onward as a brand-new conversational
request while LISTENING/WAITING_USER/IDLE - see
`_handle_speech_recognized`'s use of `looks_like_interrupt_or_resume()`.

Bug fix note (interrupt routing / request_id correlation): the
LISTENING/WAITING_USER/IDLE check above was NOT the only place that
forwarded text onward - `_handle_playback_done()` had a second,
unguarded path for a wake word spoken WITH a trailing remainder in one
utterance (e.g. "Luno stop": wake word "Luno" matched, remainder
"stop" queued and forwarded once the wake acknowledgement finishes
playing). That path skipped the interrupt check entirely, so "Luno
stop" woke the session normally but then sent "stop" to Planner/
OpenRouter as if it were a real request. Both forwarding paths now run
the exact same `looks_like_interrupt_or_resume()` check before treating
text as a new conversational turn - waking still always happens
(whether the utterance is "just" a wake word or a wake word plus an
interrupt remainder is irrelevant to whether the session wakes), only
the REMAINDER's fate changes.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

from ..core.events import ConversationEnded, ConversationStarted, Event, WakeWordDetected
from ..core.models import ModuleHealthStatus
from ..core.module_manager import Module
from ..core.utils import generate_id, log
from .matcher import looks_like_interrupt_or_resume, match_wake_word
from .models import ConversationState, WakeSessionConfig
from .session import ConversationSession

#: event types this module needs routed to it - see module docstring.
REQUIRED_ROUTES = (
    "speech_recognized",
    "wake_word_detected",
    "speak_request",
    "speech_playback_started",
    "speech_playback_finished",
    "speech_playback_cancelled",
    "llm_error",
    "llm_cancelled",
)

#: the event type this module forwards a genuinely-awake utterance as -
#: route this to "behavior_tree" alongside (or instead of) the raw
#: "speech_recognized" route.
CONVERSATION_SPEECH_EVENT = "conversation_speech"


class SessionManagerModule(Module):
    name = "session_manager"
    dependencies: list = []

    def __init__(self, config: Optional[WakeSessionConfig] = None, timeout_poll_s: float = 0.2) -> None:
        self.config = config or WakeSessionConfig.from_env()
        self.session = ConversationSession(self.config)
        self._event_bus: Any = None
        self._lock = threading.RLock()
        self._pending_ack_request_id: Optional[str] = None
        self._pending_remainder: Optional[str] = None
        self.session_id: Optional[str] = None
        self._timeout_poll_s = timeout_poll_s
        self._stop_event = threading.Event()
        self._watch_thread: Optional[threading.Thread] = None
        # Bug fix (wake word configuration loading): report WHERE the
        # active wake word list came from at construction time too, not
        # only on `/reload` - a fresh startup deserves the same audit
        # trail as a reload. Never silent about it either way.
        self._log_wake_words_config()

    # -- wake word configuration audit logging -------------------------------

    def _log_wake_words_config(self) -> None:
        log(f"Wake words loaded from: {self.config.wake_words_source}", self.name)
        log(f"Wake words: {self.config.wake_words}", self.name)
        if self.config.wake_words_conflict_warning:
            log(f"WARNING: {self.config.wake_words_conflict_warning}", self.name)

    # -- Module interface -------------------------------------------------

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def start(self) -> None:
        self._stop_event.clear()
        self._watch_thread = threading.Thread(
            target=self._timeout_watch_loop, daemon=True, name="luno-session-timeout-watch",
        )
        self._watch_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=2.0)
            self._watch_thread = None

    def health(self) -> ModuleHealthStatus:
        watch_alive = self._watch_thread is not None and self._watch_thread.is_alive()
        return ModuleHealthStatus(healthy=watch_alive, message=f"state={self.session.state.value}")

    def reload(self) -> None:
        """Called automatically by `Runtime.reload()` - rebuild config
        from the environment and hot-swap it into the running session,
        without restarting this module or dropping an in-flight
        conversation (see `ConversationSession.reconfigure`)."""
        with self._lock:
            new_config = WakeSessionConfig.from_env()
            self.config = new_config
            self.session.reconfigure(new_config)
        log(f"config reloaded (wake_words={self.config.wake_words}, "
            f"timeout={self.config.session_timeout_s}s, sleep_enabled={self.config.sleep_enabled})", self.name)
        self._log_wake_words_config()

    # -- event bus entry point ----------------------------------------------

    def on_event(self, event: Event) -> None:
        try:
            self._dispatch(event)
        except Exception as ex:  # never let a bad event crash the pump thread
            log(f"on_event raised for '{event.type}': {ex}", self.name)

    def _dispatch(self, event: Event) -> None:
        if event.type == "wake_word_detected":
            self._handle_wake_word_detected(event)
        elif event.type == "speech_recognized":
            self._handle_speech_recognized(event)
        elif event.type == "speak_request":
            self._handle_speak_request(event)
        elif event.type == "speech_playback_started":
            self._handle_playback_started(event)
        elif event.type in ("speech_playback_finished", "speech_playback_cancelled"):
            self._handle_playback_done(event)
        elif event.type in ("llm_error", "llm_cancelled"):
            self._handle_llm_failure(event)

    # -- inbound: acoustic-only wake engine (no text) ------------------------

    def _handle_wake_word_detected(self, event: Event) -> None:
        if self.session.state != ConversationState.SLEEPING:
            return
        confidence = event.get("confidence")
        if confidence is not None and confidence < self.config.wake_confidence:
            self._publish(Event(type="wake_word_rejected", data={
                "reason": "low_confidence", "confidence": confidence, "source_event": "wake_word_detected",
            }))
            return
        self._do_wake(matched_phrase=None, remainder="", confidence=confidence)

    # -- inbound: transcribed speech (text-based wake matching) --------------

    def _handle_speech_recognized(self, event: Event) -> None:
        text = event.get("text", "")
        confidence = event.get("confidence")

        if self.session.state == ConversationState.SLEEPING:
            match = match_wake_word(text, self.config.wake_words)
            if match is None:
                # Ordinary background chatter while dormant - per spec
                # "all other speech is ignored", silently, with no event.
                return
            if confidence is not None and confidence < self.config.wake_confidence:
                self._publish(Event(type="wake_word_rejected", data={
                    "reason": "low_confidence", "text": text, "matched_phrase": match.matched_phrase,
                    "confidence": confidence, "source_event": "speech_recognized",
                }))
                return
            self._do_wake(
                matched_phrase=match.matched_phrase, remainder=match.remainder, confidence=confidence,
                request_id=event.get("request_id"), conversation_id=event.get("conversation_id"),
            )
            return

        if self.session.state in (ConversationState.LISTENING, ConversationState.WAITING_USER, ConversationState.IDLE):
            # Bug fix (repeated wake word while already awake): a user who
            # can't easily SEE the current session state (no console/
            # dashboard open) naturally falls back to habit and says the
            # wake word again even though Luno is already listening. Before
            # this fix, that utterance had no special handling here -
            # it isn't an interrupt/resume phrase, so it fell straight
            # through to `_forward_to_conversation()` and got sent to the
            # LLM as a literal, context-free message (e.g. a bare "alexa"),
            # producing a confusing reply about nothing. A wake word said
            # while already awake is never itself information Luno needs -
            # it is, at most, "I'm still here" (bare wake word - silently
            # extend the session, no reply needed) or "I'm still here, AND
            # here's what I want" (wake word + remainder - forward just the
            # actual request, exactly as if it had been said without the
            # wake word at all). Either way this must never reach the LLM
            # as the word "alexa" itself.
            wake_match = match_wake_word(text, self.config.wake_words)
            if wake_match is not None:
                if not wake_match.remainder:
                    self.session.touch()
                    log(f"SpeechRecognized text={text!r} state={self.session.state.value} "
                        f"bare wake word while already awake - session extended, not forwarded", self.name)
                    return
                text = wake_match.remainder
                log(f"SpeechRecognized text={event.get('text', '')!r} state={self.session.state.value} "
                    f"wake word + remainder while already awake - forwarding remainder={text!r} only", self.name)

            # Bug fix (wake session / barge-in integration): "Interrupt
            # Priority" - check whether this utterance is plainly an
            # interrupt/resume phrase BEFORE treating it as a new
            # conversational request. `BargeInModule` independently
            # receives this SAME raw `speech_recognized` event via its
            # own Event Bus route and is the one actually responsible for
            # acting on it (or correctly no-op'ing if nothing is in
            # flight to interrupt) - forwarding it here too would send a
            # stray "stop"/"cancel"/"resume" to the LLM as if it were a
            # literal user message.
            if looks_like_interrupt_or_resume(text, self.config.interrupt_words, self.config.resume_words):
                log(f"SpeechRecognized text={text!r} state={self.session.state.value} "
                    f"InterruptDetected=True ForwardedToPlanner=False", self.name)
                return
            log(f"SpeechRecognized text={text!r} state={self.session.state.value} "
                f"InterruptDetected=False ForwardedToPlanner=True request_id={event.get('request_id')}", self.name)
            self._forward_to_conversation(text, event)
            return

        # AWAKENING/THINKING/SPEAKING: Luno is mid-turn already. This
        # sprint doesn't ask for a barge-in/interrupt policy here (that's
        # the existing stop/cancel console flow) - a stray utterance
        # while busy is simply not forwarded, same "busy states aren't
        # freely preemptable" spirit as behavior_tree's own state machine.

    # -- the wake sequence ----------------------------------------------------

    def _do_wake(
        self, matched_phrase: Optional[str], remainder: str, confidence: Optional[float] = None,
        request_id: Optional[str] = None, conversation_id: Optional[str] = None,
    ) -> None:
        with self._lock:
            self.session.transition_to(ConversationState.AWAKENING, reason=f"wake word matched ({matched_phrase!r})")
            self.session.wake_count += 1
            self.session_id = generate_id("session")
            ack_request_id = generate_id("wake_ack")
            self._pending_ack_request_id = ack_request_id
            self._pending_remainder = remainder or None

        self._publish(WakeWordDetected(data={
            "matched_phrase": matched_phrase, "confidence": confidence, "session_id": self.session_id,
        }))
        self._publish(ConversationStarted(data={
            "session_id": self.session_id, "matched_phrase": matched_phrase,
            "request_id": request_id, "conversation_id": conversation_id,
        }))
        self._publish(Event(type="speak_request", data={
            "text": self.config.wake_acknowledgement, "request_id": ack_request_id,
            "conversation_id": conversation_id, "session_id": self.session_id,
        }))

    # -- forwarding a genuinely-awake utterance onward -----------------------

    def _forward_to_conversation(self, text: str, event: Event) -> None:
        self.session.transition_to(ConversationState.THINKING, reason="user speech forwarded")
        self._publish(Event(type=CONVERSATION_SPEECH_EVENT, data={
            "text": text,
            "request_id": event.get("request_id"),
            "conversation_id": event.get("conversation_id"),
            "confidence": event.get("confidence"),
            "session_id": self.session_id,
        }))

    # -- outbound: Luno's own reply lifecycle --------------------------------

    def _handle_speak_request(self, event: Event) -> None:
        """A `speak_request` means Luno is about to actually make sound
        - whether that's a real conversational reply (already past
        THINKING) or this module's own wake acknowledgement (tracked
        purely via the playback lifecycle below, not here)."""
        request_id = event.get("request_id")
        with self._lock:
            is_our_ack = request_id is not None and request_id == self._pending_ack_request_id
        if is_our_ack:
            return  # tracked purely via playback lifecycle below
        if self.session.state == ConversationState.THINKING:
            self.session.transition_to(ConversationState.SPEAKING, reason="speak_request")
        elif self.session.state == ConversationState.IDLE:
            self.session.transition_to(ConversationState.SPEAKING, reason="speak_request (always-on mode)")

    def _handle_playback_started(self, event: Event) -> None:
        """Production-Safe LLM -> TTS Streaming Activation sprint - see
        this module's own docstring for the full deadlock this closes. A
        fully-streamed turn never publishes `speak_request` (voice bypasses
        it entirely - see `BehaviorTreeModule._speak()`), so
        `_handle_speak_request()` above is never reached for it, and
        without this handler `self.session.state` would stay at THINKING
        forever once such a turn starts playing - `_handle_playback_done()`
        below only acts `if self.session.state == ConversationState.SPEAKING`,
        so the matching outbound transition on completion would silently
        never happen either.

        `speech_playback_started` is published by `FishAudioAdapter` for
        BOTH the legacy `SpeakRequest` path and the streaming
        `SpeakStreamChunk` path (unmodified by this sprint), so reusing it
        here - rather than inventing a second, streaming-only "turn
        started speaking" event - keeps exactly one state machine, exactly
        one source of truth, matching every other "reuse, do not
        duplicate" rule this sprint operates under. It is a harmless no-op
        for the legacy path: `_handle_speak_request()` already made this
        SAME THINKING/IDLE -> SPEAKING transition when `speak_request`
        fired (synthesis has not even started yet at that point), so by
        the time playback audibly begins the state is already SPEAKING
        and the checks below simply don't match anything."""
        request_id = event.get("request_id")
        with self._lock:
            is_our_ack = request_id is not None and request_id == self._pending_ack_request_id
        if is_our_ack:
            return  # the wake acknowledgement has its own dedicated lifecycle, untouched by this
        if self.session.state == ConversationState.THINKING:
            self.session.transition_to(ConversationState.SPEAKING, reason="speech_playback_started (streamed reply)")
        elif self.session.state == ConversationState.IDLE:
            self.session.transition_to(ConversationState.SPEAKING, reason="speech_playback_started (streamed reply, always-on mode)")

    def _handle_playback_done(self, event: Event) -> None:
        request_id = event.get("request_id")
        with self._lock:
            is_our_ack = request_id is not None and request_id == self._pending_ack_request_id
            if is_our_ack:
                self._pending_ack_request_id = None
                remainder = self._pending_remainder
                self._pending_remainder = None
            else:
                remainder = None

        if is_our_ack:
            if self.session.state == ConversationState.AWAKENING:
                self.session.transition_to(ConversationState.LISTENING, reason="wake acknowledgement finished")
                self.session.touch()
                if remainder:
                    # Bug fix (interrupt routing / request_id correlation):
                    # a wake word spoken TOGETHER with an interrupt phrase
                    # in one utterance (e.g. "Luno stop") used to skip the
                    # interrupt check entirely - the remainder ("stop") was
                    # forwarded straight into `_forward_to_conversation()`
                    # as if it were ordinary conversation text, reaching
                    # Planner/OpenRouter and producing a literal "you said
                    # stop" reply. `_handle_speech_recognized()` already
                    # runs this SAME check before forwarding while
                    # LISTENING/WAITING_USER/IDLE (see above) - this is the
                    # one remaining forwarding path that didn't. Waking
                    # still always happens (the wake word itself is never
                    # gated on this), but an interrupt/resume remainder is
                    # correctly treated as "nothing to do" (BargeInModule
                    # already independently saw the original utterance via
                    # its own `speech_recognized` subscription and no-ops
                    # since nothing was in flight to interrupt while
                    # Sleeping) rather than a new conversational request.
                    if looks_like_interrupt_or_resume(remainder, self.config.interrupt_words, self.config.resume_words):
                        log(f"wake remainder={remainder!r} InterruptDetected=True ForwardedToPlanner=False "
                            f"(wake word itself already processed separately)", self.name)
                    else:
                        log(f"wake remainder={remainder!r} InterruptDetected=False ForwardedToPlanner=True", self.name)
                        self._forward_to_conversation(remainder, Event(type="speech_recognized", data={"text": remainder}))
            return

        if self.session.state == ConversationState.SPEAKING:
            if self.config.sleep_enabled:
                self.session.transition_to(ConversationState.WAITING_USER, reason="assistant reply finished")
                self.session.touch()
            else:
                self.session.transition_to(ConversationState.IDLE, reason="assistant reply finished (always-on mode)")
        elif self.session.state == ConversationState.THINKING:
            # Dashboard Turn-State Recovery fix, part 2 (post-Sprint-51):
            # a real, LIVE-reproducible second stuck-THINKING path this
            # module's own tests never covered - `FishAudioAdapter`
            # correctly publishes `speech_playback_cancelled` (or, less
            # commonly, `speech_playback_finished`) when TTS fails/
            # produces nothing on its VERY FIRST chunk (e.g. the Fish
            # Audio TTS server at its configured host:port is unreachable
            # - a real `ConnectionRefusedError`/`WinError 10053` shape,
            # not a hypothetical). In that case `speech_playback_started`
            # is NEVER published (see `_on_playback_start()` in
            # `luno/adapters/fish_audio.py` - it only fires once real
            # audio actually begins), so this module's own
            # `_handle_playback_started()`/`_handle_speak_request()` never
            # ran either, and `self.session.state` is still `THINKING`,
            # not `SPEAKING`, by the time this terminal event arrives.
            # Before this fix, the `if ... == SPEAKING` branch above
            # silently dropped it - the ONLY other THINKING recovery path,
            # `_handle_llm_failure()`, is keyed exclusively on
            # `llm_error`/`llm_cancelled` and a TTS-side failure publishes
            # neither (the LLM call itself already succeeded - `assistant_
            # response` was already published and the Chat panel already
            # shows the reply). This left THINKING stuck forever with NO
            # exception anywhere - a pure state-machine gap, not a bug
            # Sprint 51's exception-wrapper fix could have caught (that
            # fix only guards `PlannerBridgeModule._handle_utterance()`,
            # which had already finished successfully by this point).
            # Reuses the EXACT SAME transition this method already makes
            # for the ordinary SPEAKING->done case - no new state, no new
            # event, no new route; a turn that DOES reach SPEAKING first
            # is completely unaffected (that path still only ever matches
            # the branch above).
            if self.config.sleep_enabled:
                self.session.transition_to(ConversationState.WAITING_USER, reason="assistant reply finished (tts never started)")
                self.session.touch()
            else:
                self.session.transition_to(ConversationState.IDLE, reason="assistant reply finished (tts never started, always-on mode)")

    def _handle_llm_failure(self, event: Event) -> None:
        """Safety net: if the LLM errors/gets cancelled without ever
        producing an `AssistantResponse`, THINKING must not get stuck
        forever - hand control back to the user exactly like a normal
        finished turn would."""
        if self.session.state != ConversationState.THINKING:
            return
        if self.config.sleep_enabled:
            self.session.transition_to(ConversationState.WAITING_USER, reason="llm failure - returning control to user")
            self.session.touch()
        else:
            self.session.transition_to(ConversationState.IDLE, reason="llm failure (always-on mode)")

    # -- inactivity timeout watcher (own daemon thread, no protected-package
    #    scheduler needed - mirrors the tick-loop style already used by
    #    BTScheduler/HeartbeatMonitor elsewhere in this project) -------------

    def _timeout_watch_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.session.is_timed_out():
                    self._handle_timeout()
            except Exception as ex:  # pragma: no cover - defensive, must never kill the thread
                log(f"timeout watch loop raised: {ex}", self.name)
            self._stop_event.wait(self._timeout_poll_s)

    def _handle_timeout(self) -> None:
        # Race guard: another thread (e.g. a fresh speech_recognized just
        # arrived) may have already moved us on by the time we get here.
        if self.session.state not in (ConversationState.LISTENING, ConversationState.WAITING_USER):
            return
        session_id = self.session_id
        self._publish(Event(type="conversation_timeout", data={"session_id": session_id}))
        self._publish(ConversationEnded(data={"session_id": session_id, "reason": "timeout"}))
        with self._lock:
            self._pending_ack_request_id = None
            self._pending_remainder = None
        if self.config.sleep_enabled:
            self.session.transition_to(ConversationState.SLEEPING, reason="inactivity timeout")
        else:
            self.session.transition_to(ConversationState.IDLE, reason="inactivity timeout (always-on mode)")

    # -- manual overrides (console /sleep, /wake - direct method calls from
    #    the module's OWNER are the sanctioned pattern, same as
    #    `runtime.reload()`/`AdapterManager.restart_all()` elsewhere) --------

    def force_sleep(self, reason: str = "manual /sleep") -> None:
        with self._lock:
            self._pending_ack_request_id = None
            self._pending_remainder = None
        if self.session.state != ConversationState.SLEEPING:
            self._publish(ConversationEnded(data={"session_id": self.session_id, "reason": "manual_sleep"}))
        self.session.transition_to(ConversationState.SLEEPING, reason=reason)

    def force_wake(self, reason: str = "manual /wake") -> None:
        if self.session.state == ConversationState.SLEEPING:
            self._do_wake(matched_phrase="<manual>", remainder="", confidence=1.0)
        else:
            # Already awake - a manual /wake just reaffirms attention,
            # giving the user a fresh timeout window rather than doing
            # nothing.
            self.session.touch()

    # -- inspection (/session) -----------------------------------------------

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "state": self.session.state.value,
            "previous_state": self.session.previous_state.value if self.session.previous_state else None,
            "time_in_state_s": round(self.session.time_in_state, 2),
            "seconds_remaining": self.session.seconds_remaining(),
            "wake_count": self.session.wake_count,
            "session_id": self.session_id,
            "config": {
                "wake_words": self.config.wake_words,
                "wake_words_source": self.config.wake_words_source,
                "wake_words_conflict_warning": self.config.wake_words_conflict_warning,
                "session_timeout_s": self.config.session_timeout_s,
                "wake_acknowledgement": self.config.wake_acknowledgement,
                "wake_confidence": self.config.wake_confidence,
                "sleep_enabled": self.config.sleep_enabled,
            },
        }

    # -- helpers ------------------------------------------------------------

    def _publish(self, event: Event) -> None:
        if self._event_bus is not None:
            self._event_bus.publish(event)
