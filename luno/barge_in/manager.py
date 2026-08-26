"""
manager.py
==========

`BargeInModule` - the Event Bus adapter around `classify_speaking_mode`
and the interrupt/resume/confirmation matchers. Exactly the same "thin
Module wrapper around pure, standalone logic" pattern as
`wake_session.manager.SessionManagerModule` - and, like that module,
this one is fully decoupled from it: BargeInModule subscribes to the
SAME raw `speech_recognized` events SessionManagerModule already sees
(a plain Event Bus fan-out, the same mechanism that already lets
`"motion"` reach both `behavior_tree` and `vision_memory`), and decides
independently whether the text is an interrupt/resume command worth
acting on. Neither package imports the other.

Nothing here calls OpenRouter, Fish Audio, Planner, Behavior Tree, or
Tool Manager directly - every decision is a published Event:
`CancelLLMRequest` (OpenRouter), `PausePlayback`/`ResumePlayback`/
`StopPlayback` (Fish Audio), `SpeakRequest` (a short, hardcoded - never
LLM-generated - acknowledgement, exactly like `SessionManagerModule`'s
own wake acknowledgement; deliberately NOT `AssistantResponse`, which is
Sprint 3's separate "conversation record" event - see that event's own
docstring in `luno/adapters/events.py`).

Correlation: `llm_started`/`speech_playback_started` and everything
downstream all carry the SAME `request_id` for one conversational turn
(confirmed end-to-end: `OpenRouterAdapter` echoes the `NeedLLMResponse`
request_id into both `LLMStarted`/`LLMFinished`/`AssistantResponse`;
`FishAudioAdapter` reads `request_id` straight off the `AssistantResponse`
it's playing) - so tracking one `current_request_id` is enough to know
what "the thing currently in flight" even means.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, Optional

from ..core.events import Event
from ..core.models import ModuleHealthStatus
from ..core.module_manager import Module
from ..core.utils import generate_id, log
from .classifier import classify_speaking_mode
from .matcher import match_confirmation, match_interrupt_word, match_resume_word
from .models import BargeInConfig, SpeakingMode

#: event types this module needs routed to it - see module docstring.
REQUIRED_ROUTES = (
    "speech_recognized",
    "speaking_mode_assigned",
    "llm_started",
    "llm_finished",
    "llm_error",
    "llm_cancelled",
    "speech_playback_started",
    "speech_playback_finished",
    "speech_playback_cancelled",
    "smoke_detected",
    "fire_alarm",
)


def _wait_for(event_bus: Any, pattern: str, match_fn, timeout_s: float) -> bool:
    done = threading.Event()
    sub_id = event_bus.subscribe(pattern, lambda e: done.set() if match_fn(e) else None)
    try:
        return done.wait(timeout_s)
    finally:
        event_bus.unsubscribe(sub_id)


class BargeInModule(Module):
    name = "barge_in"
    dependencies: list = []

    def __init__(
        self, config: Optional[BargeInConfig] = None, confirm_timeout_s: float = 12.0,
        speech_pending_grace_s: float = 8.0,
    ) -> None:
        self.config = config or BargeInConfig.from_env()
        self._event_bus: Any = None
        self._lock = threading.RLock()

        self.thinking = False
        self.speaking = False
        self.emergency_active = False
        self.current_request_id: Optional[str] = None
        self.current_mode: SpeakingMode = SpeakingMode.FREE
        self._modes_by_request: Dict[str, SpeakingMode] = {}

        #: Bug fix (wake session / barge-in integration): `llm_finished`
        #: and `speech_playback_started` are two SEPARATE events, each
        #: its own round trip through text normalization + Fish Audio's
        #: playback executor - there is a real (occasionally seconds-long
        #: against a real TTS backend, not just a software artifact) gap
        #: between "the reply is decided" and "audio actually starts"
        #: during which `thinking` is already False and `speaking` is
        #: still False. An interrupt spoken in that exact window used to
        #: be silently dropped (`_handle_interrupt`'s "nothing in flight"
        #: guard saw both flags False). `_speech_pending_deadline` keeps
        #: this window "busy" for a bounded grace period after
        #: `llm_finished`, cleared the moment either `speech_playback_started`
        #: (playback genuinely began - `speaking` now covers it) or
        #: `llm_error`/`llm_cancelled` (nothing is coming) fires, so it
        #: can never get stuck true forever.
        self._speech_pending_deadline: Optional[float] = None
        self.speech_pending_grace_s = speech_pending_grace_s

        self.awaiting_confirmation = False
        self.confirm_request_id: Optional[str] = None
        self._confirm_deadline: Optional[float] = None
        self._confirm_timeout_s = confirm_timeout_s

        self._stop_event = threading.Event()
        self._watch_thread: Optional[threading.Thread] = None

        #: bookkeeping purely for tests/inspection - last decision made
        #: and why, never read by any behavior itself.
        self.last_action: Optional[Dict[str, Any]] = None

    # -- Module interface -------------------------------------------------

    def bind_event_bus(self, event_bus: Any) -> None:
        self._event_bus = event_bus

    def start(self) -> None:
        self._stop_event.clear()
        self._watch_thread = threading.Thread(target=self._confirm_watch_loop, daemon=True, name="luno-bargein-confirm-watch")
        self._watch_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=2.0)
            self._watch_thread = None

    def health(self) -> ModuleHealthStatus:
        alive = self._watch_thread is not None and self._watch_thread.is_alive()
        return ModuleHealthStatus(healthy=alive, message=f"thinking={self.thinking} speaking={self.speaking}")

    def reload(self) -> None:
        with self._lock:
            self.config = BargeInConfig.from_env()
        log("config reloaded", self.name)

    # -- event bus entry point ----------------------------------------------

    def on_event(self, event: Event) -> None:
        try:
            self._dispatch(event)
        except Exception as ex:
            log(f"on_event raised for '{event.type}': {ex}", self.name)

    def _dispatch(self, event: Event) -> None:
        t = event.type
        if t == "speaking_mode_assigned":
            rid = event.get("request_id")
            mode = event.get("mode")
            if rid and mode:
                with self._lock:
                    self._modes_by_request[rid] = SpeakingMode(mode)
        elif t == "llm_started":
            rid = event.get("request_id")
            with self._lock:
                self.thinking = True
                self.current_request_id = rid
                self.current_mode = self._modes_by_request.get(rid, SpeakingMode.FREE)
        elif t == "llm_finished":
            with self._lock:
                self.thinking = False
                # a reply was just decided and is on its way to Fish
                # Audio (text normalization -> SpeakRequest -> playback
                # executor pickup) - stay "busy" for a bounded grace
                # window so an interrupt spoken in that gap isn't dropped.
                self._speech_pending_deadline = time.time() + self.speech_pending_grace_s
        elif t in ("llm_error", "llm_cancelled"):
            with self._lock:
                self.thinking = False
                self._speech_pending_deadline = None  # nothing is coming
        elif t == "speech_playback_started":
            with self._lock:
                self.speaking = True
                self._speech_pending_deadline = None  # `speaking` now covers it
                if not self.thinking:
                    # a reply that skipped NeedLLMResponse entirely (a
                    # wake/barge-in acknowledgement) - nothing pre-assigned
                    # its mode, FREE is the correct, safe default.
                    rid = event.get("request_id")
                    self.current_request_id = rid
                    self.current_mode = self._modes_by_request.get(rid, SpeakingMode.FREE)
        elif t in ("speech_playback_finished", "speech_playback_cancelled"):
            with self._lock:
                self.speaking = False
                self._speech_pending_deadline = None
        elif t in ("smoke_detected", "fire_alarm"):
            with self._lock:
                self.emergency_active = True
        elif t == "speech_recognized":
            self._handle_speech(event.get("text", ""))

    # -- speech recognized while something may be in flight ------------------

    def _handle_speech(self, text: str) -> None:
        if not text:
            return
        if self.awaiting_confirmation:
            answer = match_confirmation(text, self.config.confirm_yes_words, self.config.confirm_no_words)
            if answer is True or match_interrupt_word(text, self.config.interrupt_words):
                threading.Thread(target=self._confirm_cancel, daemon=True, name="luno-bargein-confirm").start()
            elif answer is False or match_resume_word(text, self.config.resume_words):
                threading.Thread(target=self._decline_cancel, daemon=True, name="luno-bargein-decline").start()
            # else: not a recognizable answer yet - keep waiting
            return

        if match_resume_word(text, self.config.resume_words):
            threading.Thread(target=self._handle_resume, daemon=True, name="luno-bargein-resume").start()
            return

        if match_interrupt_word(text, self.config.interrupt_words):
            threading.Thread(target=self._handle_interrupt, daemon=True, name="luno-bargein-interrupt").start()
            return
        # anything else: not barge-in's concern - SessionManagerModule
        # (Sprint 2) independently decides what to do with ordinary speech.

    # -- busy-state helpers (bug fix: close the thinking->speaking gap) -------

    def _is_speech_pending(self) -> bool:
        with self._lock:
            deadline = self._speech_pending_deadline
        return deadline is not None and time.time() < deadline

    def _is_busy(self) -> bool:
        with self._lock:
            thinking, speaking = self.thinking, self.speaking
        return thinking or speaking or self._is_speech_pending()

    # -- the actual interruption decision -------------------------------------

    def _handle_interrupt(self) -> None:
        if not self._is_busy():
            log(f"InterruptDetected=True but nothing in flight (thinking={self.thinking} speaking={self.speaking} "
                f"speech_pending={self._is_speech_pending()}) request_id={self.current_request_id} - no-op", self.name)
            return  # nothing in flight - genuinely nothing to interrupt

        mode = SpeakingMode.CRITICAL if self.emergency_active else self.current_mode
        if mode == SpeakingMode.FREE:
            self._do_free_interrupt()
        elif mode == SpeakingMode.SOFT:
            self._do_soft_interrupt()
        elif mode == SpeakingMode.CONFIRM:
            self._do_confirm_interrupt()
        elif mode == SpeakingMode.CRITICAL:
            self._do_critical_interrupt()

    def _do_free_interrupt(self) -> None:
        rid = self.current_request_id
        if self.speaking:
            self._publish(Event(type="stop_playback", data={"request_id": rid}))
            _wait_for(self._event_bus, "speech_playback_cancelled", lambda e: e.get("request_id") == rid, 1.0)
        if self.thinking or self._is_speech_pending():
            # Either a stream is still genuinely in flight, OR the reply
            # already finished and is about to be spoken any moment now
            # (see `_speech_pending_deadline`) - either way, publishing
            # `cancel_llm_request` is safe (a no-op inside OpenRouterAdapter
            # if the request already finished) and, critically,
            # `llm_cancelled` still gets published for this request_id,
            # which is what `BehaviorTreeModule._speak()` checks to avoid
            # speaking a reply that was already interrupted before it
            # ever reached Fish Audio.
            self._publish(Event(type="cancel_llm_request", data={"request_id": rid}))
        ack = random.choice(self.config.free_acknowledgements)
        self._publish(Event(type="speak_request", data={"text": ack, "request_id": generate_id("bargein_ack")}))
        self._record("free", rid)

    def _do_soft_interrupt(self) -> None:
        rid = self.current_request_id
        if self.speaking:
            self._publish(Event(type="stop_playback", data={"request_id": rid}))
        # Task/plan already dispatched independently - deliberately left
        # untouched; thinking (if any) is also left untouched, matching
        # "Interrupt speech only. Do NOT stop the underlying task."
        self._record("soft", rid)

    def _do_confirm_interrupt(self) -> None:
        rid = self.current_request_id
        if self.speaking:
            self._publish(Event(type="pause_playback", data={"request_id": rid}))
        with self._lock:
            self.awaiting_confirmation = True
            self.confirm_request_id = rid
            self._confirm_deadline = time.time() + self._confirm_timeout_s
        self._publish(Event(type="speak_request", data={"text": self.config.confirm_prompt, "request_id": generate_id("bargein_confirm")}))
        self._record("confirm_requested", rid)

    def _do_critical_interrupt(self) -> None:
        rid = self.current_request_id
        if self.speaking:
            self._publish(Event(type="pause_playback", data={"request_id": rid}))
        # Never touches LLM/planner in CRITICAL mode - "emergency
        # monitoring continues" is entirely the concern of whatever
        # published the emergency event in the first place, untouched here.
        self._record("critical_pause", rid)

    # -- resume ---------------------------------------------------------------

    def _handle_resume(self) -> None:
        rid = self.current_request_id
        self._publish(Event(type="resume_playback", data={"request_id": rid}))
        self._record("resume", rid)

    # -- confirmation outcome ---------------------------------------------------

    def _confirm_cancel(self) -> None:
        with self._lock:
            if not self.awaiting_confirmation:
                return
            self.awaiting_confirmation = False
            rid = self.confirm_request_id
            self.confirm_request_id = None
            self._confirm_deadline = None
        if self.thinking or self._is_speech_pending():
            self._publish(Event(type="cancel_llm_request", data={"request_id": rid}))
        self._publish(Event(type="stop_playback", data={"request_id": rid}))
        self._publish(Event(type="speak_request", data={"text": "Okay, cancelled.", "request_id": generate_id("bargein_ack")}))
        self._record("confirmed_cancel", rid)

    def _decline_cancel(self) -> None:
        with self._lock:
            if not self.awaiting_confirmation:
                return
            self.awaiting_confirmation = False
            rid = self.confirm_request_id
            self.confirm_request_id = None
            self._confirm_deadline = None
        self._publish(Event(type="resume_playback", data={"request_id": rid}))
        self._record("declined_cancel", rid)

    # -- confirmation timeout watcher ------------------------------------------

    def _confirm_watch_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                deadline = self._confirm_deadline
            if deadline is not None and time.time() >= deadline:
                # No answer within the window - the SAFE default is to
                # decline the cancellation (resume), never to silently
                # cancel something that was never actually confirmed.
                self._decline_cancel()
            self._stop_event.wait(0.2)

    # -- inspection -------------------------------------------------------------

    def status_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "thinking": self.thinking,
                "speaking": self.speaking,
                "speech_pending": self._is_speech_pending(),
                "busy": self.thinking or self.speaking or self._is_speech_pending(),
                "emergency_active": self.emergency_active,
                "current_request_id": self.current_request_id,
                "current_mode": self.current_mode.value,
                "awaiting_confirmation": self.awaiting_confirmation,
                "last_action": self.last_action,
            }

    def clear_emergency(self) -> None:
        """Testing/manual convenience - emergencies don't auto-clear
        themselves in this sprint's scope."""
        with self._lock:
            self.emergency_active = False

    # -- helpers ------------------------------------------------------------

    def _record(self, action: str, request_id: Optional[str]) -> None:
        self.last_action = {"action": action, "request_id": request_id, "at": time.time()}
        log(f"InterruptDetected=True action={action} request_id={request_id}", self.name)
        self._publish(Event(type="barge_in_action", data={"action": action, "request_id": request_id}))

    def _publish(self, event: Event) -> None:
        if self._event_bus is not None:
            self._event_bus.publish(event)
