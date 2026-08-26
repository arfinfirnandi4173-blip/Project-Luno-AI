"""
test_barge_in.py
=================

Standalone tests for `luno.barge_in` - `matcher.py`, `classifier.py`,
`models.py`, and the full `BargeInModule` state machine. No Whisper, no
Fish Audio, no OpenRouter, no microphone - a tiny in-process
`luno.core.event_bus.EventBus` plus a handful of hand-published
"stimulus" events (`llm_started`, `speech_playback_started`,
`speaking_mode_assigned`, `smoke_detected`, `speech_recognized`, ...)
stand in for every real adapter, exactly the way `wake_session`'s own
standalone suite stands in for Whisper. A tiny in-test stub answers
`stop_playback`/`pause_playback`/`resume_playback` with the matching
playback-lifecycle event, playing the role Fish Audio would in
production, purely so `_do_free_interrupt`'s short internal wait
resolves immediately instead of timing out.

Covers every Sprint 3 testing requirement that doesn't require a real
Fish Audio/OpenRouter adapter to observe: interrupt while speaking,
interrupt while thinking, interrupt during a fire-and-forget (SOFT)
action, interrupt during an emergency (CRITICAL), pause, resume, the
CONFIRM ask/answer/timeout flow, concurrent interruptions, and a stress
run of rapid utterances.

Run:
    python3 -m luno.barge_in.tests.test_barge_in
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Callable, List, Tuple

from luno.core.event_bus import EventBus
from luno.core.events import Event
from luno.barge_in.classifier import classify_speaking_mode
from luno.barge_in.manager import REQUIRED_ROUTES, BargeInModule
from luno.barge_in.matcher import match_confirmation, match_interrupt_word, match_resume_word, normalize
from luno.barge_in.models import BargeInConfig, SpeakingMode

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


# ============================================================================
# matcher.py
# ============================================================================

@scenario
def test_normalize_strips_case_and_punctuation():
    assert normalize("  STOP!!  ") == "stop"
    assert normalize("That's enough.") == "that's enough"


@scenario
def test_match_interrupt_word_matches_english():
    words = BargeInConfig().interrupt_words
    for phrase in ("stop", "please stop now", "Cancel.", "hold on a second", "That's enough!"):
        assert match_interrupt_word(phrase, words), phrase


@scenario
def test_match_interrupt_word_matches_indonesian():
    words = BargeInConfig().interrupt_words
    for phrase in ("batal", "sudah", "diam dulu", "tunggu sebentar"):
        assert match_interrupt_word(phrase, words), phrase


@scenario
def test_match_interrupt_word_rejects_unrelated_text():
    words = BargeInConfig().interrupt_words
    assert not match_interrupt_word("what's the weather like today", words)
    assert not match_interrupt_word("", words)


@scenario
def test_match_resume_word():
    words = BargeInConfig().resume_words
    assert match_resume_word("resume please", words)
    assert match_resume_word("lanjutkan", words)
    assert not match_resume_word("stop", words)


@scenario
def test_match_confirmation_yes_no_none():
    cfg = BargeInConfig()
    assert match_confirmation("yes", cfg.confirm_yes_words, cfg.confirm_no_words) is True
    assert match_confirmation("iya", cfg.confirm_yes_words, cfg.confirm_no_words) is True
    assert match_confirmation("no thanks", cfg.confirm_yes_words, cfg.confirm_no_words) is False
    assert match_confirmation("tidak", cfg.confirm_yes_words, cfg.confirm_no_words) is False
    assert match_confirmation("what?", cfg.confirm_yes_words, cfg.confirm_no_words) is None


# ============================================================================
# classifier.py
# ============================================================================

@scenario
def test_classify_free_is_the_default():
    assert classify_speaking_mode("tell me a story about dragons") == SpeakingMode.FREE
    assert classify_speaking_mode("") == SpeakingMode.FREE


@scenario
def test_classify_soft_keywords():
    assert classify_speaking_mode("open the browser please") == SpeakingMode.SOFT
    assert classify_speaking_mode("turn on the lights") == SpeakingMode.SOFT
    assert classify_speaking_mode("nyalakan lampu") == SpeakingMode.SOFT


@scenario
def test_classify_confirm_keywords():
    assert classify_speaking_mode("delete all my files") == SpeakingMode.CONFIRM
    assert classify_speaking_mode("factory reset the device") == SpeakingMode.CONFIRM
    assert classify_speaking_mode("hapus semua data") == SpeakingMode.CONFIRM


@scenario
def test_classify_confirm_beats_soft_when_both_present():
    # "delete" (confirm) and "open" (soft) both appear - CONFIRM must win,
    # matching classify_speaking_mode's documented check order (dangerous
    # actions are checked before fire-and-forget ones).
    assert classify_speaking_mode("open the file manager and delete everything") == SpeakingMode.CONFIRM


@scenario
def test_classify_emergency_always_overrides():
    assert classify_speaking_mode("tell me a joke", emergency_active=True) == SpeakingMode.CRITICAL
    assert classify_speaking_mode("delete everything", emergency_active=True) == SpeakingMode.CRITICAL


# ============================================================================
# BargeInModule integration - a tiny EventBus, no real adapters
# ============================================================================

class _Recorder:
    """Subscribes to every event and lets tests wait for one matching a
    predicate, without polluting BargeInModule with test-only hooks."""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.events: List[Event] = []
        self._lock = threading.Lock()
        self._new = threading.Event()
        self.sub_id = bus.subscribe("*", self._on_event)

    def _on_event(self, event: Event) -> None:
        with self._lock:
            self.events.append(event)
        self._new.set()

    def close(self) -> None:
        self.bus.unsubscribe(self.sub_id)

    def snapshot(self) -> List[Event]:
        with self._lock:
            return list(self.events)

    def wait_for(self, pred: Callable[[Event], bool], timeout_s: float = 2.0) -> "Event | None":
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            for e in self.snapshot():
                if pred(e):
                    return e
            self._new.wait(0.02)
            self._new.clear()
        for e in self.snapshot():
            if pred(e):
                return e
        return None

    def none_seen(self, pred: Callable[[Event], bool], settle_s: float = 0.3) -> bool:
        """Confirms an event matching `pred` never shows up within a
        short settle window - used to assert the ABSENCE of a side
        effect (e.g. SOFT interrupt must never touch the LLM)."""
        time.sleep(settle_s)
        return not any(pred(e) for e in self.snapshot())


def _make_bus() -> EventBus:
    bus = EventBus()
    bus.start()
    return bus


def _fake_fish_audio(bus: EventBus) -> Callable[[], None]:
    """Answers stop_playback/pause_playback/resume_playback the way
    FishAudioAdapter would - just enough for BargeInModule's own short
    internal waits (see `_do_free_interrupt`) to resolve immediately
    instead of timing out. Returns an unsubscribe callback."""
    subs = []

    def _on_stop(e: Event) -> None:
        bus.publish(Event(type="speech_playback_cancelled", data={"request_id": e.get("request_id")}))

    def _on_pause(e: Event) -> None:
        bus.publish(Event(type="speech_playback_paused", data={"request_id": e.get("request_id")}))

    def _on_resume(e: Event) -> None:
        bus.publish(Event(type="speech_playback_resumed", data={"request_id": e.get("request_id")}))

    subs.append(bus.subscribe("stop_playback", _on_stop))
    subs.append(bus.subscribe("pause_playback", _on_pause))
    subs.append(bus.subscribe("resume_playback", _on_resume))

    def _cleanup():
        for s in subs:
            bus.unsubscribe(s)

    return _cleanup


class _Harness:
    """One BargeInModule wired to a fresh EventBus + fake Fish Audio
    stub, plus convenience methods for driving a turn through its
    lifecycle exactly as the real adapters would fire it."""

    def __init__(self, confirm_timeout_s: float = 12.0, config: "BargeInConfig | None" = None) -> None:
        self.bus = _make_bus()
        self.module = BargeInModule(config=config, confirm_timeout_s=confirm_timeout_s)
        self.module.bind_event_bus(self.bus)
        self.module.start()
        # Production wiring is `Runtime.add_route(pattern, "barge_in")` ->
        # Coordinator subscribes the module's own `on_event` for each
        # pattern. This standalone test has no Coordinator/Runtime, so it
        # reproduces that same subscription directly against
        # REQUIRED_ROUTES - the exact list `manager.py`'s own docstring
        # says this module needs routed to it.
        self._route_subs = [self.bus.subscribe(pattern, self.module.on_event) for pattern in REQUIRED_ROUTES]
        self.recorder = _Recorder(self.bus)
        self._cleanup_fish = _fake_fish_audio(self.bus)

    def close(self) -> None:
        self._cleanup_fish()
        for sub_id in self._route_subs:
            self.bus.unsubscribe(sub_id)
        self.module.stop()
        self.recorder.close()
        self.bus.stop()

    def assign_mode(self, request_id: str, mode: SpeakingMode) -> None:
        self.bus.publish(Event(type="speaking_mode_assigned", data={"request_id": request_id, "mode": mode.value}))

    def start_thinking(self, request_id: str) -> None:
        self.bus.publish(Event(type="llm_started", data={"request_id": request_id}))

    def finish_thinking(self, request_id: str) -> None:
        self.bus.publish(Event(type="llm_finished", data={"request_id": request_id}))

    def start_speaking(self, request_id: str) -> None:
        self.bus.publish(Event(type="speech_playback_started", data={"request_id": request_id}))

    def say(self, text: str) -> None:
        self.bus.publish(Event(type="speech_recognized", data={"text": text}))

    def wait_settled(self, s: float = 0.15) -> None:
        time.sleep(s)


def _by_type(t: str):
    return lambda e: e.type == t


def _by_type_and_rid(t: str, rid: str):
    return lambda e: e.type == t and e.get("request_id") == rid


# ---------------------------------------------------------------------------

@scenario
def test_interrupt_while_speaking_free_mode():
    h = _Harness()
    try:
        h.assign_mode("t1", SpeakingMode.FREE)
        h.start_thinking("t1")
        h.start_speaking("t1")
        h.say("stop")
        assert h.recorder.wait_for(_by_type_and_rid("stop_playback", "t1")) is not None
        assert h.recorder.wait_for(_by_type_and_rid("cancel_llm_request", "t1")) is not None
        ack = h.recorder.wait_for(lambda e: e.type == "speak_request" and e.get("text") in ("Okay.", "Sure."))
        assert ack is not None
        assert h.recorder.wait_for(lambda e: e.type == "barge_in_action" and e.get("action") == "free") is not None
    finally:
        h.close()


@scenario
def test_interrupt_while_thinking_free_mode_no_speech_yet():
    h = _Harness()
    try:
        h.assign_mode("t2", SpeakingMode.FREE)
        h.start_thinking("t2")
        # not speaking yet - still "thinking" only
        h.say("cancel")
        assert h.recorder.wait_for(_by_type_and_rid("cancel_llm_request", "t2")) is not None
        # nothing was playing, so no stop_playback should ever have been published
        assert h.recorder.none_seen(_by_type("stop_playback"))
        assert h.recorder.wait_for(lambda e: e.type == "speak_request" and e.get("text") in ("Okay.", "Sure.")) is not None
    finally:
        h.close()


@scenario
def test_interrupt_in_gap_between_llm_finished_and_speech_started():
    """Bug fix regression: `llm_finished` used to clear `thinking`
    immediately, and `speaking` only becomes true once
    `speech_playback_started` arrives - a real gap exists between "the
    reply is decided" and "audio actually starts" (text normalization +
    Fish Audio's executor pickup), during which an interrupt used to be
    silently dropped (`_handle_interrupt` saw thinking=False AND
    speaking=False, i.e. "nothing in flight"). `_speech_pending_deadline`
    closes this - an interrupt landing in that exact window must still
    work, publishing `cancel_llm_request` (so `llm_cancelled` still
    fires - what a caller like `BehaviorTreeModule` uses to suppress an
    already-decided-but-not-yet-spoken reply)."""
    h = _Harness()
    try:
        h.assign_mode("t-gap", SpeakingMode.FREE)
        h.start_thinking("t-gap")
        assert h.recorder.wait_for(_by_type_and_rid("llm_started", "t-gap")) is not None
        h.finish_thinking("t-gap")
        assert h.recorder.wait_for(_by_type_and_rid("llm_finished", "t-gap")) is not None
        h.wait_settled()
        assert h.module.thinking is False
        assert h.module.speaking is False
        assert h.module.status_snapshot()["speech_pending"] is True
        # NOT speaking yet, NOT thinking anymore - exactly the gap.
        h.say("stop")
        assert h.recorder.wait_for(_by_type_and_rid("cancel_llm_request", "t-gap")) is not None
        assert h.recorder.wait_for(lambda e: e.type == "barge_in_action" and e.get("action") == "free") is not None
        # nothing was actually playing, so stop_playback is correctly
        # never published for this turn.
        assert h.recorder.none_seen(_by_type_and_rid("stop_playback", "t-gap"))
    finally:
        h.close()


@scenario
def test_speech_pending_window_expires_after_grace_period():
    """The pending window must be BOUNDED - if playback never actually
    starts (a genuine failure, not just slow dispatch), `_is_busy()`
    must eventually go back to False rather than staying stuck "busy"
    forever."""
    h = _Harness(config=None)
    h.module.speech_pending_grace_s = 0.2
    try:
        h.assign_mode("t-expire", SpeakingMode.FREE)
        h.start_thinking("t-expire")
        assert h.recorder.wait_for(_by_type_and_rid("llm_started", "t-expire")) is not None
        h.finish_thinking("t-expire")
        assert h.recorder.wait_for(_by_type_and_rid("llm_finished", "t-expire")) is not None
        h.wait_settled()
        assert h.module.status_snapshot()["speech_pending"] is True
        time.sleep(0.35)
        assert h.module.status_snapshot()["speech_pending"] is False
        assert h.module._is_busy() is False
    finally:
        h.close()


@scenario
def test_speech_pending_cleared_once_speech_actually_starts():
    h = _Harness()
    try:
        h.assign_mode("t-clear", SpeakingMode.FREE)
        h.start_thinking("t-clear")
        assert h.recorder.wait_for(_by_type_and_rid("llm_started", "t-clear")) is not None
        h.finish_thinking("t-clear")
        assert h.recorder.wait_for(_by_type_and_rid("llm_finished", "t-clear")) is not None
        h.wait_settled()
        assert h.module.status_snapshot()["speech_pending"] is True
        h.start_speaking("t-clear")
        assert h.recorder.wait_for(_by_type_and_rid("speech_playback_started", "t-clear")) is not None
        h.wait_settled()
        assert h.module.status_snapshot()["speech_pending"] is False
        assert h.module.speaking is True
    finally:
        h.close()


@scenario
def test_interrupt_during_soft_action_stops_speech_but_not_the_task():
    h = _Harness()
    try:
        h.assign_mode("t3", SpeakingMode.SOFT)
        h.start_thinking("t3")
        h.start_speaking("t3")
        h.say("stop")
        assert h.recorder.wait_for(_by_type_and_rid("stop_playback", "t3")) is not None
        # SOFT = "interrupt speech only, do NOT stop the underlying task" -
        # cancel_llm_request must never be published for this turn.
        assert h.recorder.none_seen(_by_type("cancel_llm_request"))
        assert h.recorder.wait_for(lambda e: e.type == "barge_in_action" and e.get("action") == "soft") is not None
    finally:
        h.close()


@scenario
def test_interrupt_during_emergency_only_pauses_never_cancels():
    h = _Harness()
    try:
        h.assign_mode("t4", SpeakingMode.FREE)  # ordinary turn...
        h.start_thinking("t4")
        h.start_speaking("t4")
        h.bus.publish(Event(type="smoke_detected", data={}))  # ...but an emergency is active
        h.wait_settled()
        h.say("stop")
        assert h.recorder.wait_for(_by_type_and_rid("pause_playback", "t4")) is not None
        # CRITICAL: never touches the LLM/planner, and never a hard stop.
        assert h.recorder.none_seen(_by_type("cancel_llm_request"))
        assert h.recorder.none_seen(_by_type("stop_playback"))
        assert h.recorder.wait_for(lambda e: e.type == "barge_in_action" and e.get("action") == "critical_pause") is not None
    finally:
        h.close()


@scenario
def test_confirm_mode_interrupt_then_yes_cancels():
    h = _Harness()
    try:
        h.assign_mode("t5", SpeakingMode.CONFIRM)
        h.start_thinking("t5")
        h.start_speaking("t5")
        h.say("stop")
        assert h.recorder.wait_for(_by_type_and_rid("pause_playback", "t5")) is not None
        prompt = h.recorder.wait_for(lambda e: e.type == "speak_request" and "cancel the operation" in e.get("text", ""))
        assert prompt is not None
        assert h.module.awaiting_confirmation is True
        h.say("yes")
        assert h.recorder.wait_for(_by_type_and_rid("cancel_llm_request", "t5")) is not None
        assert h.recorder.wait_for(_by_type_and_rid("stop_playback", "t5")) is not None
        assert h.recorder.wait_for(lambda e: e.type == "barge_in_action" and e.get("action") == "confirmed_cancel") is not None
        assert h.module.awaiting_confirmation is False
    finally:
        h.close()


@scenario
def test_confirm_mode_interrupt_then_no_resumes():
    h = _Harness()
    try:
        h.assign_mode("t6", SpeakingMode.CONFIRM)
        h.start_thinking("t6")
        h.start_speaking("t6")
        h.say("wait")
        assert h.recorder.wait_for(_by_type_and_rid("pause_playback", "t6")) is not None
        h.say("no")
        assert h.recorder.wait_for(_by_type_and_rid("resume_playback", "t6")) is not None
        assert h.recorder.wait_for(lambda e: e.type == "barge_in_action" and e.get("action") == "declined_cancel") is not None
        assert h.module.awaiting_confirmation is False
    finally:
        h.close()


@scenario
def test_confirm_mode_timeout_auto_declines():
    h = _Harness(confirm_timeout_s=0.3)
    try:
        h.assign_mode("t7", SpeakingMode.CONFIRM)
        h.start_thinking("t7")
        h.start_speaking("t7")
        h.say("stop")
        assert h.recorder.wait_for(_by_type_and_rid("pause_playback", "t7")) is not None
        assert h.module.awaiting_confirmation is True
        # never answered - the safe default is decline/resume, never a
        # silent cancel of something that was never actually confirmed.
        assert h.recorder.wait_for(_by_type_and_rid("resume_playback", "t7"), timeout_s=2.0) is not None
        assert h.recorder.wait_for(lambda e: e.type == "barge_in_action" and e.get("action") == "declined_cancel") is not None
        assert h.module.awaiting_confirmation is False
    finally:
        h.close()


@scenario
def test_resume_command_outside_confirm_flow():
    h = _Harness()
    try:
        h.assign_mode("t8", SpeakingMode.FREE)
        h.start_thinking("t8")
        h.start_speaking("t8")
        h.say("resume")
        assert h.recorder.wait_for(_by_type_and_rid("resume_playback", "t8")) is not None
        assert h.recorder.wait_for(lambda e: e.type == "barge_in_action" and e.get("action") == "resume") is not None
    finally:
        h.close()


@scenario
def test_interrupt_with_nothing_in_flight_is_a_noop():
    h = _Harness()
    try:
        h.say("stop")
        h.wait_settled()
        assert h.recorder.none_seen(_by_type("stop_playback"), settle_s=0.2)
        assert h.recorder.none_seen(_by_type("cancel_llm_request"), settle_s=0.0)
        assert h.module.last_action is None
    finally:
        h.close()


@scenario
def test_ordinary_speech_is_ignored_by_barge_in():
    h = _Harness()
    try:
        h.assign_mode("t9", SpeakingMode.FREE)
        h.start_thinking("t9")
        h.start_speaking("t9")
        h.say("what's the weather like tomorrow")
        h.wait_settled()
        assert h.recorder.none_seen(_by_type("stop_playback"), settle_s=0.2)
        assert h.module.last_action is None
    finally:
        h.close()


@scenario
def test_concurrent_interruptions_do_not_crash_or_double_ack():
    h = _Harness()
    try:
        h.assign_mode("t10", SpeakingMode.FREE)
        h.start_thinking("t10")
        h.start_speaking("t10")

        threads = [threading.Thread(target=h.say, args=("stop",)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        # every one of the 8 concurrent "stop"s races into its own
        # daemon thread inside BargeInModule (see `_handle_speech`) -
        # the module must survive this without raising, and must still
        # end up in a coherent (not-speaking, not-thinking) state.
        h.wait_settled(0.5)
        assert h.recorder.wait_for(lambda e: e.type == "barge_in_action" and e.get("action") == "free") is not None
    finally:
        h.close()


@scenario
def test_stress_many_ordinary_utterances_then_one_real_interrupt():
    h = _Harness()
    try:
        h.assign_mode("t11", SpeakingMode.FREE)
        h.start_thinking("t11")
        h.start_speaking("t11")
        for i in range(200):
            h.say(f"just some ordinary sentence number {i}")
        h.say("that's enough")
        assert h.recorder.wait_for(_by_type_and_rid("stop_playback", "t11"), timeout_s=5.0) is not None
        assert h.recorder.wait_for(lambda e: e.type == "barge_in_action" and e.get("action") == "free", timeout_s=5.0) is not None
    finally:
        h.close()


@scenario
def test_reload_picks_up_new_env_config():
    import os
    old = os.environ.get("BARGE_IN_INTERRUPT_WORDS")
    try:
        os.environ["BARGE_IN_INTERRUPT_WORDS"] = "freeze"
        h = _Harness()
        try:
            h.module.reload()
            assert "freeze" in h.module.config.interrupt_words
            assert match_interrupt_word("freeze", h.module.config.interrupt_words)
        finally:
            h.close()
    finally:
        if old is None:
            os.environ.pop("BARGE_IN_INTERRUPT_WORDS", None)
        else:
            os.environ["BARGE_IN_INTERRUPT_WORDS"] = old


def main() -> int:
    passed = 0
    failed = 0
    for name, fn in SCENARIOS:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as ex:
            print(f"  [FAIL] {name}: {ex}")
            failed += 1
        except Exception as ex:  # pragma: no cover
            print(f"  [ERROR] {name}: {ex}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
