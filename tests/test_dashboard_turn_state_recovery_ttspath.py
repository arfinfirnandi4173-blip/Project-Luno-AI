"""
tests/test_dashboard_turn_state_recovery_ttspath.py
=====================================================

Dashboard Turn-State Recovery fix, PART 2 - dedicated regression suite
for a second, SEPARATE stuck-`THINKING` path that the original
Dashboard Turn-State Recovery fix (see `tests/test_dashboard_turn_state_
recovery.py` and `docs/change_impact/dashboard_turn_state_recovery.md`)
did not cover, because it lives entirely on the TTS/voice-output side of
a turn, not the planner/LLM side.

ROOT CAUSE (see `docs/change_impact/dashboard_turn_state_recovery_ttspath.md`
for the full writeup): the original fix guarded
`PlannerBridgeModule._handle_utterance()` - the code path BEFORE
`NeedLLMResponse`/`assistant_response` is published. It explicitly,
correctly, did not touch `luno/adapters/fish_audio.py`. But
`SessionManagerModule` (`luno/wake_session/manager.py`) only ever leaves
`THINKING` via exactly two routes:

  1. `speech_playback_started`/`speak_request` -> `SPEAKING`, then
     `speech_playback_finished`/`speech_playback_cancelled` -> back to
     `WAITING_USER`/`IDLE` (`_handle_playback_done()`, gated
     `if self.session.state == ConversationState.SPEAKING`).
  2. `llm_error`/`llm_cancelled` -> `WAITING_USER`/`IDLE`
     (`_handle_llm_failure()`, gated
     `if self.session.state == ConversationState.THINKING`).

A turn whose LLM call ALREADY SUCCEEDED (so route 2 never fires - no
`llm_error`/`llm_cancelled` was ever published, `assistant_response`
already reached the Chat panel) but whose TTS synthesis then fails on
its VERY FIRST chunk - e.g. the Fish Audio TTS server is unreachable,
matching this project's own `[WinError 10053]`/`[WinError 10061]`-style
real-world Windows connection-failure precedent - falls into neither
route: `FishAudioAdapter` correctly publishes `speech_playback_cancelled`
(see `_play()`/`_play_stream()`'s own "every chunk failed" branch), but
`speech_playback_started` was NEVER published first (it only fires once
real audio begins - see `_on_playback_start()`), so `self.session.state`
is still `THINKING`, not `SPEAKING`, when that terminal event arrives -
and route 1's own guard silently drops it. `THINKING` is then stuck
forever, with NO exception anywhere in the process and the Chat panel
already showing a (correct) assistant reply - exactly the reported
symptom ("Hi, Vinn." gets a reply, every message after that is refused
with "Luno is busy right now (state=thinking)").

A SEPARATE, structural version of the same underlying bug CLASS also
existed one layer down: none of `FishAudioAdapter._play()`/
`_play_pipelined()`/`_play_stream()`/`_play_stream_pipelined()` had an
outer `except Exception` guaranteeing a terminal
`SpeechPlaybackFinished`/`SpeechPlaybackCancelled` publish no matter
what - each runs on `_playback_executor` (a `ThreadPoolExecutor`) via
`pool.submit(...)`, and nothing ever calls `.result()` on the returned
`Future` (see `handle_event()`'s own docstring - it must stay
non-blocking), so ANY exception escaping the per-chunk `except
Exception` guard (which only wraps `self.client.play(...)` itself, not
the surrounding loop/queue/token-check code) would silently kill that
worker thread with zero published event - the SAME "unsupervised daemon
thread, no outer try/except" bug class Sprint 51 already fixed for
`PlannerBridgeModule._handle_utterance()`, just never closed here.

THE FIX (two parts, both additive, both reuse existing plumbing, zero
new event type/route/state machine):

  1. `SessionManagerModule._handle_playback_done()` gained an
     `elif self.session.state == ConversationState.THINKING:` branch,
     making the EXACT SAME WAITING_USER/IDLE transition the existing
     `SPEAKING` branch already makes. A turn that DOES reach `SPEAKING`
     first is completely unaffected - that branch is unchanged and is
     still the one that fires for it.
  2. `FishAudioAdapter._play()`/`_play_pipelined()`/`_play_stream()`/
     `_play_stream_pipelined()` each gained an outer
     `except Exception as ex:` (before their existing `finally:`) that
     publishes `SpeechPlaybackCancelled(data={"request_id": ...,
     "error": f"unhandled: {ex}"})`. Every normal exit already publishes
     exactly one terminal event and returns immediately, so this branch
     can only be reached by a genuinely unanticipated exception, never
     double-publishes, and is a pure safety net - mirrors
     `PlannerBridgeModule._run_utterance_turn_safely()`'s own design
     from the original fix, applied to this module's own equivalent gap.

Same self-contained-helpers house style as `tests/
test_dashboard_turn_state_recovery.py` (this project's own established
"duplicate the small helper set per test file" convention).

IMPORTANT - PROVENANCE NOTE FOR THE NEXT AGENT: this file was written
and syntax-checked (`python3 -m py_compile`) in a sandboxed session that
had NO ability to actually execute the project's own Python environment
(no network to install the heavy ML/audio dependencies `main_runtime_
demo.py` imports at module level, no bridge to run the real Windows
`.venv`). Every test below follows this project's own established
patterns (`tests/test_dashboard_turn_state_recovery.py`'s own helpers,
reused verbatim where possible) and was reasoned through line-by-line
against the actual source, but **has not been run**. Run it - and the
existing `tests/test_dashboard_turn_state_recovery.py` suite alongside
it, to confirm no regression - before treating this fix as verified.
See `docs/change_impact/dashboard_turn_state_recovery_ttspath.md` §"Not
yet done" for the exact command.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from typing import Callable

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.core import Event  # noqa: E402
from luno.core.events import Event as CoreEvent  # noqa: E402
from luno.adapters.fish_audio import FishAudioAdapter, SpeechPlaybackCancelled, SpeechPlaybackFinished  # noqa: E402


def _load_demo():
    demo_spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_turn_state_recovery_ttspath", os.path.join(_ROOT, "main_runtime_demo.py")
    )
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules["main_runtime_demo_turn_state_recovery_ttspath"] = demo
    demo_spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _new_console(demo, canned_text="Oke, dimengerti.", fish_audio_client=None, **kwargs):
    from luno.adapters import MockFishAudioClient, MockOpenRouterClient
    kwargs["fish_audio_client"] = fish_audio_client or MockFishAudioClient(playback_delay_s=0.01)
    return demo.RuntimeDemoConsole(
        openrouter_client=MockOpenRouterClient(canned_text=canned_text, chunk_delay_s=0.0), **kwargs
    )


def _state(console) -> str:
    return console.session_manager.status_snapshot().get("state")


def _wake(console) -> None:
    console.session_manager.force_wake(reason="test")
    assert _wait_until(lambda: _state(console) in ("listening", "waiting_user", "idle"), 3.0), \
        f"session never left AWAKENING (state={_state(console)!r})"


def _speak(console, text: str, conversation_id=None) -> None:
    data = {"text": text, "confidence": None, "source": "test"}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    console.event_bus.publish(Event(type="speech_recognized", data=data))


# ─────────────────────────────────────────────
#  1 - THE reported bug: TTS fails before any chunk plays, LLM already
#      succeeded (assistant_response already delivered) - session must
#      NOT stay stuck at THINKING.
# ─────────────────────────────────────────────

def test_01_e2e_tts_fails_before_playback_started_recovers_from_thinking():
    """Direct live reproduction of the screenshot's own symptom: the
    Chat panel gets a correct reply ('Hi, Vinn.' style greeting works),
    but the TTS side fails to ever start (e.g. Fish Audio server
    unreachable) - `MockFishAudioClient(fail=True)` raises INSIDE
    `client.play()` before `on_playback_start()` is ever called,
    exactly matching that shape (see that class's own docstring: "fail=
    True simulates a genuine playback error instead"). Before this fix,
    `SessionManagerModule._handle_playback_done()` only handled this
    while `state == SPEAKING` - since `speech_playback_started` never
    fired, state was still `THINKING`, and the resulting
    `speech_playback_cancelled` event was silently dropped, exactly
    reproducing "Luno is busy right now (state=thinking)" on every
    subsequent command."""
    from luno.adapters import MockFishAudioClient
    demo = _load_demo()
    console = _new_console(demo, fish_audio_client=MockFishAudioClient(fail=True))
    console.start()
    try:
        _wake(console)
        _speak(console, "halo, apa kabar?")
        assert _wait_until(lambda: _state(console) == "thinking", 6.0), \
            f"turn never reached THINKING (state={_state(console)!r})"
        # The bug: without the fix, this never becomes true and the test
        # times out with state stuck at "thinking".
        assert _wait_until(lambda: _state(console) != "thinking", 6.0), (
            f"THINKING never cleared after a TTS-side failure that happened "
            f"before playback started (state={_state(console)!r}) - this is "
            f"the exact reported dashboard bug: the assistant reply already "
            f"reached the Chat panel via 'assistant_response', but the "
            f"session never returns to a state that accepts the next "
            f"'/api/chat/send' call."
        )
        assert _wait_until(lambda: _state(console) in ("waiting_user", "idle", "listening"), 4.0), \
            f"left THINKING but never settled into a non-busy state (state={_state(console)!r})"
    finally:
        console.stop()


def test_02_e2e_send_chat_message_accepts_a_new_command_after_tts_failure_recovery():
    """The actual, real-world-relevant assertion: not just that the
    internal `ConversationState` enum changes, but that the Dashboard's
    own `send_chat_message()` busy-guard (`luno/dashboard/controls.py`)
    - the exact function that produces "Luno is busy right now
    (state=thinking) - try again in a moment" - accepts the next message
    once recovery happens."""
    from luno.adapters import MockFishAudioClient
    from luno.dashboard import controls as dash_controls

    demo = _load_demo()
    console = _new_console(demo, fish_audio_client=MockFishAudioClient(fail=True))
    console.start()
    try:
        _wake(console)
        _speak(console, "halo, apa kabar?")
        assert _wait_until(lambda: _state(console) == "thinking", 6.0)
        assert _wait_until(lambda: _state(console) != "thinking", 6.0), \
            "precondition failed - see test_01 for the isolated version of this assertion"
        assert _wait_until(lambda: _state(console) in ("waiting_user", "idle", "listening"), 4.0)

        class _FakeRuntimeForControls:
            def __init__(self, event_bus):
                self.event_bus = event_bus

        modules = {"session_manager": console.session_manager, "barge_in_module": console.barge_in_module}
        result = dash_controls.send_chat_message(
            _FakeRuntimeForControls(console.event_bus), modules, "gimana kabarnya?"
        )
        assert result.get("ok") is True, f"dashboard chat still rejected a message after recovery: {result}"
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  2 - defense in depth: an unanticipated exception escaping the
#      streaming-playback worker thread must still publish exactly one
#      terminal event, never die silently.
# ─────────────────────────────────────────────

class _ExplodingCancelToken:
    """Duck-typed stand-in for `luno.speech_chunk.SpeechCancellationToken`
    whose `is_cancelled` check - the FIRST thing `_play_stream()`'s own
    loop does on every iteration, deliberately OUTSIDE the inner
    per-chunk `except Exception` that only wraps `client.play()` itself -
    raises instead of returning a bool. Models a genuinely unanticipated
    internal bug (not a TTS/network failure - those are already covered
    by test_01 above and were already correctly handled before this
    fix), proving the NEW outer `except Exception` in `_play_stream()`
    is what stands between this and a silently-dead worker thread."""

    def wait_while_paused(self) -> None:
        return None

    @property
    def is_cancelled(self) -> bool:
        raise RuntimeError("synthetic unanticipated internal failure (not a TTS/network error)")


def test_03_play_stream_unhandled_exception_still_publishes_terminal_event():
    """Unit-level (adapter only, no console/session) - calls
    `FishAudioAdapter._play_stream()` directly with the exploding token
    above, bypassing `handle_event()`'s normal dispatch (same technique
    this project's own `test_fish_audio_real.py`/`test_fish_audio_barge_
    in.py` already use for other `_play*` edge cases per `_play()`'s own
    docstring). Before this fix, this call would raise `RuntimeError`
    out of `_play_stream()` entirely - silently killing whatever thread
    ran it (in production, a `_playback_executor` worker whose `Future`
    result nobody ever collects) with NO event published at all. After
    the fix, exactly one `SpeechPlaybackCancelled` is published instead,
    carrying the original exception's message for observability."""
    published = []
    bus = type("FakeBus", (), {
        "publish": lambda self, event: published.append(event),
    })()

    adapter = FishAudioAdapter()
    adapter.bind(bus)
    # `_play_stream()` only touches `self._in_flight_lock`/`_chunk_control_lock`/
    # `_stream_queue_lock` (all created in `__init__`) plus `self.client` -
    # none of which require `start()`/`_playback_executor` for a direct call.
    adapter._play_stream(request_id="synthetic-req-1", conversation_id=None, token=_ExplodingCancelToken())

    assert len(published) == 1, (
        f"expected exactly one terminal event published, got {len(published)}: "
        f"{[e.type for e in published]}"
    )
    event = published[0]
    assert event.type == SpeechPlaybackCancelled.EVENT_TYPE, (
        f"expected a SpeechPlaybackCancelled for an unanticipated exception, got {event.type!r}"
    )
    assert event.get("request_id") == "synthetic-req-1"
    assert "unhandled" in str(event.get("error", "")).lower()
    # Bookkeeping must still be cleaned up (the `finally:` block) even
    # though the exception took the new `except` branch, not a normal
    # return.
    assert "synthetic-req-1" not in adapter._in_flight_request_ids
    assert "synthetic-req-1" not in adapter._chunk_control


# ─────────────────────────────────────────────
#  3 - a turn that reaches SPEAKING normally is completely unaffected
#      (regression guard for both parts of this fix).
# ─────────────────────────────────────────────

def test_04_e2e_normal_turn_still_returns_to_non_busy_state():
    """Baseline, unchanged from `tests/test_dashboard_turn_state_
    recovery.py::test_01` - included here too so this file is a complete,
    standalone regression unit for the TTS-path fix (both the new
    `SessionManagerModule` branch and the new `FishAudioAdapter` `except`
    clauses are pure ADDITIONS that must never fire, and never change
    behavior, for an ordinary successful turn)."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _wake(console)
        _speak(console, "halo, apa kabar?")
        assert _wait_until(lambda: _state(console) == "thinking", 6.0)
        assert _wait_until(lambda: _state(console) != "thinking", 6.0), \
            f"normal turn never left THINKING (state={_state(console)!r})"
        assert _wait_until(lambda: _state(console) in ("waiting_user", "idle", "listening"), 4.0), \
            f"turn left THINKING but never fully settled (state={_state(console)!r})"
    finally:
        console.stop()


def test_05_e2e_repeated_tts_failure_then_normal_cycle_stays_usable():
    """Mirrors `tests/test_dashboard_turn_state_recovery.py::test_05`'s
    own normal->failure->normal->failure->normal cycle, but for THIS
    fix's own failure mode (TTS-side, pre-SPEAKING) instead of the
    planner-side one that file already covers."""
    from luno.adapters import MockFishAudioClient

    demo = _load_demo()
    failing_client = MockFishAudioClient(fail=True)
    console = _new_console(demo, fish_audio_client=failing_client)
    console.start()
    try:
        _wake(console)
        for i in range(3):
            _speak(console, f"pesan ke-{i}", conversation_id=f"conv-{i}")
            assert _wait_until(lambda: _state(console) == "thinking", 6.0), \
                f"cycle {i}: turn never reached THINKING (state={_state(console)!r})"
            assert _wait_until(lambda: _state(console) != "thinking", 6.0), \
                f"cycle {i}: THINKING never cleared (state={_state(console)!r})"
            assert _wait_until(lambda: _state(console) in ("waiting_user", "idle", "listening"), 4.0), \
                f"cycle {i}: never settled into a non-busy state (state={_state(console)!r})"
    finally:
        console.stop()
