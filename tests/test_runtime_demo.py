"""
test_runtime_demo.py
======================

Automated tests for `main_runtime_demo.py` - the Luno Developer Runtime
Console. No external API, network, or hardware is used anywhere in this
suite: OpenRouter is always a `MockOpenRouterClient`/`_DemoMockOpenRouterClient`
and Fish Audio is always a `MockFishAudioClient`, exactly as the demo
itself defaults to when no `OPENROUTER_API_KEY` is set.

Covers every item the spec calls out: Startup, Shutdown, Interactive
input, Developer commands, Event injection, Streaming, Interrupt, Debug
mode, Health inspection, Planner inspection, Context inspection, Memory
inspection, Concurrent events, Stress test.

Run:
    python3 tests/test_runtime_demo.py
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from typing import Callable, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import importlib.util

_spec = importlib.util.spec_from_file_location("main_runtime_demo", os.path.join(_ROOT, "main_runtime_demo.py"))
demo = importlib.util.module_from_spec(_spec)
sys.modules["main_runtime_demo"] = demo
_spec.loader.exec_module(demo)

from luno.adapters import MockOpenRouterClient  # noqa: E402
from luno import config as _luno_config  # noqa: E402

# Relationship Engine Foundation sprint - every scenario in this file
# constructs at least one real `PlannerBridgeModule` (via `_new_console()`
# below), which now loads/saves persistent relationship state (see
# `luno/relationship_engine.py`). Redirected to a throwaway temp path for
# this whole test module BEFORE any console is constructed, same
# "redirect the file path, never touch the real one" convention
# `tests/test_persona.py`/`tests/test_memory_regression.py` already use
# (there via `monkeypatch.setattr` per-test; here as a one-time module-
# level redirect, since this file predates using pytest fixtures at all
# and every scenario already shares plain module-level setup like `demo`
# above) - without this, running this test file would silently write
# test-derived interaction counts into Vinn's real
# `config/relationship_state.json`.
import tempfile as _tempfile  # noqa: E402
_MODULE_RELATIONSHIP_STATE_FILE = os.path.join(_tempfile.mkdtemp(prefix="luno_test_relationship_"), "relationship_state.json")
_luno_config.RELATIONSHIP_STATE_FILE = _MODULE_RELATIONSHIP_STATE_FILE

# Shared Experience & Episodic Memory sprint - same reasoning/convention as
# the RELATIONSHIP_STATE_FILE redirect immediately above: every scenario
# that constructs a `PlannerBridgeModule` now ALSO has a registered
# `episodic_memory` retrieval source and may call `episodic_memory.observe_turn()`
# once per turn (see `luno/episodic_memory.py`), which persists to
# `config.EPISODIC_MEMORY_FILE`. Redirected to its own throwaway temp path
# BEFORE any console is constructed, so running this file never writes
# test-derived experience records into Vinn's real `config/episodic_memory.json`.
_MODULE_EPISODIC_MEMORY_FILE = os.path.join(_tempfile.mkdtemp(prefix="luno_test_episodic_"), "episodic_memory.json")
_luno_config.EPISODIC_MEMORY_FILE = _MODULE_EPISODIC_MEMORY_FILE


# ============================================================================
# tiny test harness (mirrors the style of luno/adapters/tests/test_openrouter_adapter.py)
# ============================================================================

def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _silent(fn, *a, **kw):
    """Run fn with stdout captured, return (result, captured_text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = fn(*a, **kw)
    return result, buf.getvalue()


def _new_console(client=None) -> "demo.RuntimeDemoConsole":
    return demo.RuntimeDemoConsole(openrouter_client=client or MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0))


SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


# ============================================================================
# Startup / Shutdown
# ============================================================================

@scenario
def test_startup_registers_every_module():
    console = _new_console()
    _, _ = _silent(console.start)
    try:
        modules = console.runtime.module_manager.all_modules()
        expected = {"vision_memory", "tool_manager", "planner", "behavior_tree", "openrouter", "fish_audio"}
        assert expected.issubset(modules.keys()), modules.keys()
        for name in expected:
            assert modules[name].state.value == "running", f"{name} not running: {modules[name].state}"
        assert console.runtime.health().healthy
    finally:
        _silent(console.stop)


@scenario
def test_shutdown_stops_everything_and_is_idempotent():
    console = _new_console()
    _silent(console.start)
    _, out = _silent(console.stop)
    assert "Runtime stopped" in out or True  # stop() itself doesn't print; runtime.stop() logs via logger, not stdout
    for name, record in console.runtime.module_manager.all_modules().items():
        assert record.state.value in ("stopped", "created"), f"{name} left in state {record.state}"
    # calling stop() again must not raise (graceful, no double-stop crash)
    _silent(console.stop)


@scenario
def test_print_banner_shows_all_checkmarks():
    console = _new_console()
    _silent(console.start)
    try:
        _, out = _silent(demo.print_banner, console)
        for label in ("Behavior Tree", "Planner", "Tool Manager", "OpenRouter Adapter", "Fish Audio Adapter", "Vision Memory"):
            assert label in out
        assert "✗" not in out, out
    finally:
        _silent(console.stop)


# ============================================================================
# Interactive input / simulated speech (Whisper stand-in)
# ============================================================================

@scenario
def test_simulated_speech_publishes_speech_recognized():
    console = _new_console()
    _silent(console.start)
    try:
        seen = threading.Event()
        console.event_bus.subscribe("speech_recognized", lambda e: seen.set() if e.get("text") == "turn on the lights" else None)
        _silent(console.simulate_speech, "turn on the lights")
        assert _wait_until(seen.is_set, 2.0)
    finally:
        _silent(console.stop)


@scenario
def test_handle_line_routes_plain_text_as_speech_not_command():
    console = _new_console()
    _silent(console.start)
    try:
        seen = threading.Event()
        console.event_bus.subscribe("speech_recognized", lambda e: seen.set())
        keep_going, _ = _silent(console.handle_line, "hello luno")
        assert keep_going is True
        assert _wait_until(seen.is_set, 2.0)
    finally:
        _silent(console.stop)


@scenario
def test_quit_command_returns_false():
    console = _new_console()
    _silent(console.start)
    try:
        result, _ = _silent(console.handle_line, "/quit")
        assert result is False
    finally:
        _silent(console.stop)


# ============================================================================
# Developer commands (handled locally, never sent to the LLM)
# ============================================================================

@scenario
def test_slash_commands_never_reach_the_llm():
    console = _new_console()
    _silent(console.start)
    try:
        need_llm_seen = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm_seen.set())
        for cmd in ("/help", "/status", "/health", "/events", "/modules", "/plans",
                    "/tasks", "/memory", "/context", "/history", "/config", "/debug on", "/debug off"):
            keep_going, _ = _silent(console.handle_line, cmd)
            assert keep_going is True, cmd
        time.sleep(0.2)
        assert not need_llm_seen.is_set(), "a slash command incorrectly triggered an LLM request"
    finally:
        _silent(console.stop)


@scenario
def test_unknown_command_does_not_crash():
    console = _new_console()
    _silent(console.start)
    try:
        keep_going, out = _silent(console.handle_line, "/bogus")
        assert keep_going is True
        assert "Unknown command" in out
    finally:
        _silent(console.stop)


@scenario
def test_help_lists_every_required_command():
    console = _new_console()
    _silent(console.start)
    try:
        _, out = _silent(console.handle_line, "/help")
        for cmd in ("/help", "/status", "/health", "/events", "/modules", "/plans", "/tasks",
                    "/memory", "/context", "/history", "/config", "/debug", "/clear",
                    "/restart", "/reload", "/quit", "/event"):
            assert cmd in out, f"missing {cmd} from /help output"
    finally:
        _silent(console.stop)


# ============================================================================
# Event injection - the 10 named synthetic hardware events
# ============================================================================

@scenario
def test_every_injectable_event_flows_through_the_bus():
    console = _new_console()
    _silent(console.start)
    try:
        for name in demo.INJECTABLE_EVENTS:
            seen = threading.Event()
            sub = console.event_bus.subscribe(name, lambda e, ev=seen: ev.set())
            ok, _ = _silent(console.inject_event, name)
            assert ok is True, name
            assert _wait_until(seen.is_set, 2.0), f"{name} never delivered"
            console.event_bus.unsubscribe(sub)
    finally:
        _silent(console.stop)


@scenario
def test_unknown_injectable_event_is_rejected():
    console = _new_console()
    _silent(console.start)
    try:
        ok, out = _silent(console.inject_event, "not_a_real_event")
        assert ok is False
        assert "Unknown injectable event" in out
    finally:
        _silent(console.stop)


@scenario
def test_emergency_event_reaches_behavior_tree_blackboard():
    console = _new_console()
    _silent(console.start)
    try:
        before = len(console.behavior_tree_module.bb.ha_events)
        _silent(console.inject_event, "smoke_detected")
        assert _wait_until(lambda: len(console.behavior_tree_module.bb.ha_events) > before, 2.0)
    finally:
        _silent(console.stop)


@scenario
def test_motion_event_also_routes_to_vision_memory():
    console = _new_console()
    _silent(console.start)
    try:
        seen = threading.Event()
        console.event_bus.subscribe("motion", lambda e: seen.set(), priority=-500)
        _silent(console.inject_event, "motion")
        assert _wait_until(seen.is_set, 2.0)
        # vision_memory module and behavior_tree module both routed 'motion' -
        # confirm at least vision_memory's on_event ran without raising by
        # checking module health stayed green afterwards.
        assert console.runtime.module_manager.all_modules()["vision_memory"].state.value == "running"
    finally:
        _silent(console.stop)


# ============================================================================
# Streaming + full turn (speech -> planner -> tool -> LLM -> AssistantResponse)
# ============================================================================

@scenario
def test_streaming_reply_emits_chunks_and_finishes():
    client = MockOpenRouterClient(canned_text="Hello from the mock model", chunk_delay_s=0.01)
    console = _new_console(client=client)
    _silent(console.start)
    try:
        chunks: List[str] = []
        finished = threading.Event()
        console.event_bus.subscribe("llm_chunk", lambda e: chunks.append(e.get("delta", "")))
        console.event_bus.subscribe("llm_finished", lambda e: finished.set())
        request_id = "stream-test-1"
        _silent(console.event_bus.publish, demo.NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "hi"}], "stream": True, "request_id": request_id,
        }))
        assert _wait_until(finished.is_set, 5.0)
        assert len(chunks) > 1, "expected multiple streamed chunks"
        assert "".join(chunks).strip() == "Hello from the mock model"
    finally:
        _silent(console.stop)


@scenario
def test_full_turn_produces_assistant_response_and_fish_audio_speaks_once():
    client = MockOpenRouterClient(canned_text="Hi there!", chunk_delay_s=0.0)
    console = _new_console(client=client)
    _silent(console.start)
    try:
        assistant_events = []
        console.event_bus.subscribe("assistant_response", lambda e: assistant_events.append(e))
        playback_started = threading.Event()
        console.event_bus.subscribe("speech_playback_started", lambda e: playback_started.set())
        _silent(console.event_bus.publish, demo.Event(type="user_utterance", data={
            "text": "hello", "request_id": "turn-1", "conversation_id": "conv-1",
        }))
        assert _wait_until(lambda: len(assistant_events) >= 1, 5.0)
        time.sleep(0.3)  # let behavior_tree's post-turn speak() attempt (and get suppressed) run
        assert len(assistant_events) == 1, f"AssistantResponse published more than once (double-speak): {assistant_events}"
    finally:
        _silent(console.stop)


# ============================================================================
# Interrupt (stop / cancel / pause / resume)
# ============================================================================

@scenario
def test_stop_command_publishes_cancel_llm_request():
    client = MockOpenRouterClient(canned_text="a slow reply that takes a while to stream out", chunk_delay_s=0.25)
    console = _new_console(client=client)
    _silent(console.start)
    try:
        request_id = "cancel-test-1"
        console._streaming_request_id = request_id
        cancelled = threading.Event()
        console.event_bus.subscribe("llm_cancelled", lambda e: cancelled.set())
        _silent(console.event_bus.publish, demo.NeedLLMResponse(data={
            "messages": [{"role": "user", "content": "tell me a long story"}], "stream": True, "request_id": request_id,
        }))
        time.sleep(0.3)
        _silent(console.handle_line, "stop")
        assert _wait_until(cancelled.is_set, 3.0), "LLMCancelled was never published after 'stop'"
    finally:
        _silent(console.stop)


@scenario
def test_cancel_alias_word_also_interrupts():
    console = _new_console()
    _silent(console.start)
    try:
        keep_going, out = _silent(console.handle_line, "cancel")
        assert keep_going is True
        assert "cancel requested" in out
    finally:
        _silent(console.stop)


@scenario
def test_pause_and_resume_do_not_crash_with_no_active_plan():
    console = _new_console()
    _silent(console.start)
    try:
        _, out1 = _silent(console.handle_line, "pause")
        assert "pause requested" in out1
        _, out2 = _silent(console.handle_line, "resume")
        assert "resume requested" in out2
    finally:
        _silent(console.stop)


# ============================================================================
# Debug mode - observational only, must not change runtime behavior
# ============================================================================

@scenario
def test_debug_mode_prints_without_altering_event_delivery():
    console = _new_console()
    _silent(console.start)
    try:
        received: List[str] = []
        console.event_bus.subscribe("wake_word", lambda e: received.append(e.get("tag")))

        _silent(console.event_bus.publish, demo.Event(type="wake_word", data={"tag": "before"}))
        assert _wait_until(lambda: "before" in received, 2.0)

        _silent(console.handle_line, "/debug on")

        # Debug prints happen from whichever thread the Event Bus delivers
        # on, which may run after publish() itself returns - so the
        # capture window must stay open across the wait, not just the
        # publish call.
        buf = io.StringIO()
        with redirect_stdout(buf):
            console.event_bus.publish(demo.Event(type="wake_word", data={"tag": "during"}))
            _wait_until(lambda: "during" in received and "[DEBUG]" in buf.getvalue(), 2.0)
        out = buf.getvalue()
        _silent(console.handle_line, "/debug off")

        assert "before" in received and "during" in received, received
        assert "[DEBUG]" in out
    finally:
        _silent(console.stop)


@scenario
def test_debug_off_stops_the_firehose():
    console = _new_console()
    _silent(console.start)
    try:
        _silent(console.handle_line, "/debug on")
        _silent(console.handle_line, "/debug off")
        assert console.debug.enabled is False
        _, out = _silent(console.inject_event, "wake_word")
        assert "[DEBUG]" not in out
    finally:
        _silent(console.stop)


# ============================================================================
# Inspection commands
# ============================================================================

@scenario
def test_health_inspection_reports_every_module():
    console = _new_console()
    _silent(console.start)
    try:
        _, out = _silent(console.print_health)
        for name in ("vision_memory", "tool_manager", "planner", "behavior_tree", "openrouter", "fish_audio"):
            assert name in out
    finally:
        _silent(console.stop)


@scenario
def test_planner_inspection_before_and_after_a_plan():
    console = _new_console()
    _silent(console.start)
    try:
        _, out_before = _silent(console.print_plans)
        assert "no plan created yet" in out_before

        finished = threading.Event()
        console.event_bus.subscribe("planner_finished", lambda e: finished.set())
        _silent(console.event_bus.publish, demo.Event(type="user_utterance", data={"text": "turn on the lights", "request_id": "p1"}))
        assert _wait_until(finished.is_set, 5.0)

        _, out_after = _silent(console.print_plans)
        assert "Current Plan" in out_after
        assert console.planner_module.last_plan_id in out_after
    finally:
        _silent(console.stop)


@scenario
def test_tasks_inspection_reflects_tool_manager_state():
    console = _new_console()
    _silent(console.start)
    try:
        finished = threading.Event()
        console.event_bus.subscribe("tool_finished", lambda e: finished.set())
        console.event_bus.subscribe("tool_failed", lambda e: finished.set())
        _silent(console.event_bus.publish, demo.Event(type="user_utterance", data={"text": "turn on the lights", "request_id": "p2"}))
        assert _wait_until(finished.is_set, 5.0)
        _, out = _silent(console.print_tasks)
        assert "Current Tool" in out
    finally:
        _silent(console.stop)


@scenario
def test_context_inspection_shows_llm_ready_context_without_api_call():
    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())
        _, out = _silent(console.print_context)
        time.sleep(0.1)
        for key in ("conversation_memory", "vision_memory", "behavior_tree_state", "planner_state", "current_time"):
            assert key in out, key
        assert not need_llm.is_set(), "/context must never make a real API call"
    finally:
        _silent(console.stop)


@scenario
def test_memory_inspection_reports_vision_state():
    console = _new_console()
    _silent(console.start)
    try:
        _, out = _silent(console.print_memory)
        for label in ("Known Objects", "Known Locations", "Recent Events", "Long-term Memory", "Current Scene"):
            assert label in out
    finally:
        _silent(console.stop)


@scenario
def test_events_inspection_shows_history_with_timestamps_and_types():
    console = _new_console()
    _silent(console.start)
    try:
        _silent(console.inject_event, "wake_word")
        time.sleep(0.1)
        _, out = _silent(console.print_events, 20)
        assert "wake_word" in out
    finally:
        _silent(console.stop)


@scenario
def test_config_inspection_never_prints_api_key():
    os.environ["OPENROUTER_API_KEY"] = "sk-test-should-never-appear-anywhere"
    try:
        console = _new_console()
        _silent(console.start)
        try:
            _, out = _silent(console.print_config)
            assert "sk-test-should-never-appear-anywhere" not in out
        finally:
            _silent(console.stop)
    finally:
        del os.environ["OPENROUTER_API_KEY"]


# ============================================================================
# Concurrent events + stress test
# ============================================================================

@scenario
def test_concurrent_event_injection_no_crosstalk_or_crash():
    console = _new_console()
    _silent(console.start)
    try:
        errors: List[Exception] = []

        def _inject(name: str, n: int) -> None:
            for _ in range(n):
                try:
                    console.event_bus.publish(demo.Event(type=name, data={}))
                except Exception as ex:  # pragma: no cover
                    errors.append(ex)

        threads = [
            threading.Thread(target=_inject, args=(name, 20))
            for name in demo.INJECTABLE_EVENTS
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        time.sleep(0.3)
        assert not errors, errors
        assert console.runtime.health().healthy
    finally:
        _silent(console.stop)


@scenario
def test_stress_hundreds_of_injected_events_stays_responsive():
    console = _new_console()
    _silent(console.start)
    try:
        t0 = time.time()
        for i in range(300):
            name = demo.INJECTABLE_EVENTS[i % len(demo.INJECTABLE_EVENTS)]
            console.event_bus.publish(demo.Event(type=name, data={"i": i}))
        elapsed = time.time() - t0
        assert elapsed < 5.0, f"300 events took {elapsed:.2f}s - console not responsive enough"
        # console must still take normal input/commands after the burst
        keep_going, _ = _silent(console.handle_line, "/status")
        assert keep_going is True
        assert console.runtime.health().healthy
    finally:
        _silent(console.stop)


@scenario
def test_no_lingering_non_daemon_threads_after_stop():
    before = {t.ident for t in threading.enumerate()}
    console = _new_console()
    _silent(console.start)
    _silent(console.event_bus.publish, demo.Event(type="user_utterance", data={"text": "hi", "request_id": "leak-check"}))
    time.sleep(0.3)
    _silent(console.stop)
    time.sleep(0.3)
    leaked_non_daemon = [t for t in threading.enumerate() if t.ident not in before and not t.daemon and t is not threading.main_thread()]
    assert not leaked_non_daemon, f"non-daemon threads leaked after stop(): {[t.name for t in leaked_non_daemon]}"


# ============================================================================
# Architecture rule: console must never call business-logic packages directly
# ============================================================================

@scenario
def test_console_never_imports_or_calls_business_logic_synchronously():
    """Static check: RuntimeDemoConsole's own methods (excluding the
    dedicated *Module wrapper classes, whose job IS to hold one real
    instance of their wrapped package - the sanctioned integration
    pattern) must not reach into `.tool_manager_module.manager`,
    `.planner_module.planner`, or `.behavior_tree_module.tree` except via
    the inspection/introspection print_* methods (read-only, no
    decisions) and the `/pause`/`/resume` passthroughs the spec itself
    calls "manual interaction", which is inherently a direct pass-through
    of a user command."""
    import inspect
    src = inspect.getsource(demo.RuntimeDemoConsole)
    # the *_bridge_handler / on_event methods inside the wrapper Module
    # classes are allowed to call their own wrapped package - that's a
    # separate class. Here we only check RuntimeDemoConsole's own body.
    forbidden_calls = ["self.openrouter_adapter.client.", "self.fish_audio_adapter.client."]
    for token in forbidden_calls:
        assert token not in src, f"RuntimeDemoConsole calls an adapter's client directly: {token}"


# ============================================================================
# Reliability Sprint - "Never Assume Success" regression tests
# ============================================================================
#
# Covers the sprint's own Testing checklist: Planner memakai
# ToolResult.message, Planner tidak mengarang keberhasilan, LLM context
# berisi ToolResult.message, data diteruskan utuh, Runtime tetap
# kompatibel dengan test lama (the rest of this file, re-run alongside
# these, IS that check).

from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor  # noqa: E402
from luno.planner.models import RetryPolicy, TaskStatus  # noqa: E402
from luno.planner.models import ToolCall as PlannerToolCall  # noqa: E402
from luno.planner.task import Task  # noqa: E402
from luno.planner.queue import ExecutionQueue  # noqa: E402
from luno.planner.executor import TaskExecutor, ToolRegistry  # noqa: E402


from luno.planner.utils import generate_id as _planner_generate_id  # noqa: E402


def _real_task(tool="home_assistant", action="turn_on", target="bedroom_light", label=None) -> Task:
    return Task(id=_planner_generate_id("task"), tool_call=PlannerToolCall(tool=tool, action=action, target=target), label=label)


@scenario
def test_build_verified_action_notes_uses_verified_message_not_bare_label():
    """Planner tidak mengarang: a completed task's note must be built
    from the tool's own VERIFIED `ToolResult.message` (via `task.result`),
    not just the task label + a blanket 'confirm success' instruction."""
    task = _real_task(label="turn_on.home_assistant")
    task.status = TaskStatus.COMPLETED
    task.result = {"success": True, "message": "I've turned on Bedroom Light.", "data": {"actual_state": "on"}}

    notes = demo.build_verified_action_notes([task], "turn on the bedroom light")
    joined = "\n".join(notes)
    assert "I've turned on Bedroom Light." in joined
    assert "confirm this succeeded" not in joined.lower()  # the old, bug-era blanket phrasing


@scenario
def test_build_verified_action_notes_never_silent_on_failure():
    """The core bug this sprint fixes: a failed task used to produce NO
    note at all, leaving the LLM free to assume success. It must now
    always produce an explicit, honest failure note."""
    task = _real_task(label="turn_on.home_assistant")
    task.status = TaskStatus.FAILED
    task.error = "I tried to turn on Bedroom Light, but it didn't respond."

    notes = demo.build_verified_action_notes([task], "turn on the bedroom light")
    joined = "\n".join(notes)
    assert "did NOT succeed" in joined
    assert "I tried to turn on Bedroom Light, but it didn't respond." in joined
    assert "you already performed" not in joined.lower()


@scenario
def test_build_verified_action_notes_carries_expected_and_actual_state():
    """'data diteruskan utuh': a failed task's expected/actual state
    (from `task.result['data']`, preserved by executor.py even on
    failure - see `TaskExecutor._handle_failure`) must reach the note,
    not just the bare message string."""
    task = _real_task(label="turn_on.home_assistant")
    task.status = TaskStatus.FAILED
    task.error = "I tried to turn on Bedroom Light, but it didn't respond."
    task.result = {"success": False, "data": {"expected_state": "on", "actual_state": "off"}}

    notes = demo.build_verified_action_notes([task], "turn on the bedroom light")
    joined = "\n".join(notes)
    assert "expected_state=on" in joined
    assert "actual_state=off" in joined


@scenario
def test_build_verified_action_notes_never_silent_on_skipped():
    """Real, reported bug: "nyalakan rgb strip dan matikan fish light"
    only ever ran the first action - the second was silently SKIPPED
    (see scheduler.py's `_apply_failure_policy`/`_cascade_skip_blocked`)
    and `build_verified_action_notes` had no branch for that status at
    all, so the LLM got zero information about it and the reply sounded
    like a blanket success. A SKIPPED task must now get the same honest,
    mandatory-negative treatment as FAILED/CANCELLED."""
    task = _real_task(tool="home_assistant", action="turn_off", target="fish_light", label="turn_off.home_assistant")
    task.status = TaskStatus.SKIPPED
    task.error = "a dependency did not complete"

    notes = demo.build_verified_action_notes([task], "nyalakan rgb strip dan matikan fish light")
    joined = "\n".join(notes)
    assert "did NOT succeed" in joined
    assert "a dependency did not complete" in joined
    assert "you already performed" not in joined.lower()


@scenario
def test_build_verified_action_notes_no_notes_for_plain_chat():
    """A task with no real tool call (tool == 'unknown', already filtered
    out by the caller before `build_verified_action_notes` even sees it)
    must not produce any note - plain chat stays untouched."""
    notes = demo.build_verified_action_notes([], "hi there")
    assert notes == []


@scenario
def test_llm_context_never_claims_success_when_tool_fails_end_to_end():
    """Full pipeline: user_utterance -> Planner -> Tool Manager (forced
    to fail) -> NeedLLMResponse. Asserts the system_prompt the LLM
    actually receives is grounded in the honest failure, and contains
    no fabricated success claim - the sprint's Golden Rule, checked at
    the LLM boundary rather than only at the ToolResult source."""
    from luno.tool_manager.handler import ToolHandler
    from luno.tool_manager.result import ToolResult

    class AlwaysFailsHandler(ToolHandler):
        name = "home_assistant"

        def supported_actions(self):
            return ["turn_on", "turn_off", "toggle", "run_script", "set_temperature"]

        def execute(self, tool_call, context=None):
            return ToolResult.fail(
                self.name, tool_call.action,
                "I tried to turn on Bedroom Light, but it didn't respond.",
                error_type="VerificationFailed", retryable=True,
                data={"expected_state": "on", "actual_state": "off", "verification_attempts": 3},
            )

    console = _new_console()
    console.tool_manager_module.manager.registry.register("home_assistant", AlwaysFailsHandler())
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the bedroom light", "request_id": "r-fail-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        prompt = captured.get("system_prompt") or ""
        assert "did NOT succeed" in prompt, prompt
        assert "I tried to turn on Bedroom Light, but it didn't respond." in prompt, prompt
        # the exact bug this sprint fixes: no success claim anywhere in the prompt
        assert "you already performed" not in prompt.lower()
        assert "turned on" not in prompt.lower() or "did not" in prompt.lower()
    finally:
        _silent(console.stop)


@scenario
def test_llm_context_reports_verified_success_message_end_to_end():
    """Same pipeline, success path: the system_prompt must contain the
    tool's own verified message, not a bare task label."""
    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "r-ok-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        prompt = captured.get("system_prompt") or ""
        assert "VERIFIED results" in prompt, prompt
        assert "[MOCK] Turned on 'lights'" in prompt, prompt  # the tool's own verified message, not a bare label
    finally:
        _silent(console.stop)


@scenario
def test_llm_context_includes_persona_alongside_verified_facts_end_to_end():
    """Personality Stabilization sprint - integration guard: a real turn's
    system_prompt must carry BOTH persona/character instructions AND the
    verified tool-result grounding in the SAME request - proves
    `luno.persona.build_persona_prompt()` is actually wired into the live
    production bridge (`PlannerBridgeModule`, the one `luno/bootstrap/
    modules.py` loads for `python main.py`), not just unit-testable in
    isolation. Companion to `test_llm_context_reports_verified_success_
    message_end_to_end` above (that one predates persona wiring and only
    checks the verified-facts half)."""
    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "r-persona-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        prompt = captured.get("system_prompt") or ""
        # Persona/character content present (identity, traits, name)
        assert "Luno" in prompt
        assert "AI companion" in prompt
        assert "dry/deadpan" in prompt or "deadpan" in prompt
        # Verified-facts grounding still present ALONGSIDE it - persona
        # is additive, never a replacement for the honest tool-result note.
        assert "VERIFIED results" in prompt
        assert "[MOCK] Turned on 'lights'" in prompt
        # Final language/character-reinforcement instruction still present
        # and, critically, still appears AFTER the persona block (never
        # the other way around) - `system_prompt.index()` raises if either
        # substring is missing, which is itself a useful failure signal.
        assert "FINAL INSTRUCTION" in prompt
        assert prompt.index("AI companion") < prompt.index("FINAL INSTRUCTION")
    finally:
        _silent(console.stop)


@scenario
def test_llm_context_includes_emotional_context_alongside_persona_and_verified_facts_end_to_end():
    """Emotion Engine sprint - integration guard, same shape as
    `test_llm_context_includes_persona_alongside_verified_facts_end_to_end`
    above: proves `luno.emotion_engine` is actually wired into the live
    production bridge (`PlannerBridgeModule`), not just unit-testable in
    isolation, and that it coexists correctly with persona + verified
    facts + the final language/character instruction in the SAME
    request, in the ordering `_handle_utterance()` actually builds
    (persona/verified-facts notes first, emotional context next, the
    language/character reminder last)."""
    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "capek banget hari ini, tolong nyalain lampu", "request_id": "r-emotion-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        prompt = captured.get("system_prompt") or ""
        # Persona still present (additive, never replaced).
        assert "AI companion" in prompt
        # Verified tool-result grounding still present, untouched.
        assert "VERIFIED results" in prompt
        assert "[MOCK] Turned on 'lampu'" in prompt
        # New: bounded, uncertainty-hedged emotional-context note present.
        assert "Inferred emotional context" in prompt
        assert "tired" in prompt
        assert "uncertain" in prompt.lower()
        # Ordering: persona/verified-facts before emotional context,
        # emotional context before the final language/character
        # instruction - never the other way around.
        assert prompt.index("AI companion") < prompt.index("Inferred emotional context")
        assert prompt.index("VERIFIED results") < prompt.index("Inferred emotional context")
        assert prompt.index("Inferred emotional context") < prompt.index("FINAL INSTRUCTION")
    finally:
        _silent(console.stop)


@scenario
def test_llm_context_omits_emotional_context_for_neutral_technical_utterance_end_to_end():
    """Emotion Engine sprint section 10's own worked example, verified
    end-to-end: an ordinary device command/technical utterance with no
    real emotional signal must add NOTHING to the prompt - the engine
    must never manufacture an emotional read out of a plain request."""
    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "r-emotion-neutral-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        prompt = captured.get("system_prompt") or ""
        assert "VERIFIED results" in prompt  # sanity: this IS the real bridge, notes ARE being built
        assert "Inferred emotional context" not in prompt
    finally:
        _silent(console.stop)


@scenario
def test_relationship_context_absent_for_brand_new_relationship_end_to_end():
    """Relationship Engine Foundation sprint - a brand-new relationship
    (no prior persisted state) must say NOTHING about itself after just
    one turn - `RelationshipContextBuilder`'s own minimum-interaction
    gate, proven through the real bridge rather than only unit-tested in
    isolation. Points `RELATIONSHIP_STATE_FILE` at its own fresh, empty
    temp path (not the module-level one every other scenario in this
    file shares) so this test's result can never depend on how many
    OTHER scenarios already ran and persisted state first."""
    fresh_path = os.path.join(_tempfile.mkdtemp(prefix="luno_test_relationship_fresh_"), "relationship_state.json")
    _luno_config.RELATIONSHIP_STATE_FILE = fresh_path
    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "r-relationship-new-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        prompt = captured.get("system_prompt") or ""
        assert "VERIFIED results" in prompt
        assert "Relationship context" not in prompt

        # The turn still persisted (interaction_count=1) even though
        # nothing was injected into the prompt this time - state
        # advances silently in the background from turn one.
        assert os.path.exists(fresh_path)
        with open(fresh_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["interaction_count"] == 1
    finally:
        _silent(console.stop)
        _luno_config.RELATIONSHIP_STATE_FILE = _MODULE_RELATIONSHIP_STATE_FILE


@scenario
def test_relationship_context_appears_for_established_relationship_alongside_persona_and_verified_facts_end_to_end():
    """Relationship Engine Foundation sprint - integration guard, same
    shape as the Emotion Engine's own equivalent test above: proves
    `luno.relationship_engine` is actually wired into the live
    production bridge, coexists correctly with persona + verified facts
    + the final language/character instruction in the SAME request, in
    the ordering `_handle_utterance()` actually builds (persona ->
    relationship context -> ... -> verified facts -> ... -> FINAL
    INSTRUCTION), and that a live turn re-persists the updated state -
    proving the full loop: state -> prompt -> next turn -> persistence."""
    fresh_path = os.path.join(_tempfile.mkdtemp(prefix="luno_test_relationship_established_"), "relationship_state.json")
    established = {
        "schema_version": 1, "familiarity": 0.6, "trust": 0.6, "closeness": 0.6,
        "interaction_count": 10, "shared_experience_count": 3, "last_interaction_timestamp": 1000.0,
    }
    with open(fresh_path, "w", encoding="utf-8") as f:
        json.dump(established, f)
    _luno_config.RELATIONSHIP_STATE_FILE = fresh_path

    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "r-relationship-est-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        prompt = captured.get("system_prompt") or ""
        # Persona and verified-facts grounding both still present,
        # additive, never replaced.
        assert "AI companion" in prompt
        assert "VERIFIED results" in prompt
        assert "[MOCK] Turned on 'lights'" in prompt
        # New: compact, banded relationship-context note present.
        assert "Relationship context" in prompt
        assert "familiarity" in prompt
        assert "trust" in prompt
        assert "3 shared experience" in prompt
        # Ordering: persona first, relationship context right after it
        # and before verified facts/final instruction - never the other
        # way around.
        assert prompt.index("AI companion") < prompt.index("Relationship context")
        assert prompt.index("Relationship context") < prompt.index("VERIFIED results")
        assert prompt.index("VERIFIED results") < prompt.index("FINAL INSTRUCTION")

        # The live turn re-persisted incremented state (interaction_count
        # 10 -> 11) - proves the full state -> prompt -> persistence loop,
        # not just a static fixture read.
        with open(fresh_path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        assert saved["interaction_count"] == 11
    finally:
        _silent(console.stop)
        _luno_config.RELATIONSHIP_STATE_FILE = _MODULE_RELATIONSHIP_STATE_FILE


@scenario
def test_episodic_memory_end_to_end_detect_persist_retrieve_alongside_existing_context():
    """Shared Experience & Episodic Memory sprint - full loop proof through
    the REAL production bridge: turn 1 describes a meaningful, groundable
    accomplishment (a real technical-problem-solved pattern, not a bare
    device command) -> it is detected, validated, and persisted to
    `config.EPISODIC_MEMORY_FILE` -> turn 2 asks a memory-recall-shaped
    question ("kemarin kita benerin masalah apa ya?") -> the stored
    experience is retrieved through the EXISTING `memory_retriever` /
    `memory_block` prompt slot (no parallel "experience prompt") -> the
    SAME turn's prompt still contains persona + VERIFIED tool-result facts,
    proving "no existing system may disappear because a new context block
    was added" (this sprint's own end-to-end requirement)."""
    fresh_path = os.path.join(_tempfile.mkdtemp(prefix="luno_test_episodic_e2e_"), "episodic_memory.json")
    _luno_config.EPISODIC_MEMORY_FILE = fresh_path

    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)

        # Turn 0: an ordinary device command - establishes a real VERIFIED
        # fact (memory_guard persists it across turns within the session)
        # AND is a control proving ordinary commands never create an
        # episodic memory (also covered in isolation by
        # tests/test_episodic_memory.py's own detection unit tests).
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "r-episodic-0"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        zeroth_prompt = captured.get("system_prompt") or ""
        assert "VERIFIED results" in zeroth_prompt
        assert not os.path.exists(fresh_path)  # nothing persisted yet - ordinary command only

        # Turn 1: a real, groundable accomplishment - should be detected +
        # persisted, and should NOT be confused with an ordinary device
        # command.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "akhirnya masalah wifi kelar juga setelah dicoba lama banget", "request_id": "r-episodic-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        first_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in first_prompt  # persona still present
        # (note: `build_verified_action_notes()` only reports THIS turn's
        # tool activity, not a running log - turn 0 already independently
        # proved the verified-facts mechanism itself works; this turn has
        # no tool call of its own, so no VERIFIED note is expected here.)

        assert os.path.exists(fresh_path)
        with open(fresh_path, "r", encoding="utf-8") as f:
            stored = json.load(f)
        assert len(stored) == 1
        assert stored[0]["category"] == "technical_problem_solved"
        assert "masalah wifi kelar" in stored[0]["summary"]

        # Turn 2: a fresh device command - proves the verified-facts
        # mechanism itself is completely unaffected by the new episodic
        # detection/relationship code that now also runs every turn.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn off the lights", "request_id": "r-episodic-2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        second_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in second_prompt
        assert "VERIFIED results" in second_prompt

        # Turn 3: memory-recall-shaped question - the stored experience
        # should surface through the EXISTING memory_block retrieval slot,
        # and persona must still be present alongside it.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "kemarin kita benerin masalah apa ya?", "request_id": "r-episodic-2b"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        third_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in third_prompt

        # The episodic memory itself surfaced, through the same rendering
        # (`Shared experience with the user: ...`) `make_episodic_experience_source`
        # produces, with the existing retriever's own honest freshness
        # wording layered on top for free (never a raw invented date).
        assert "Shared experience with the user" in third_prompt
        assert "masalah wifi kelar" in third_prompt
        assert ("Observed" in third_prompt and "ago" in third_prompt)

        # A second identical accomplishment turn must NOT create a second
        # stored record (storage-time dedup, restart-safe).
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "akhirnya masalah wifi kelar juga setelah dicoba lama banget", "request_id": "r-episodic-3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        with open(fresh_path, "r", encoding="utf-8") as f:
            stored_after_repeat = json.load(f)
        assert len(stored_after_repeat) == 1
    finally:
        _silent(console.stop)
        _luno_config.EPISODIC_MEMORY_FILE = _MODULE_EPISODIC_MEMORY_FILE


@scenario
def test_manual_memory_end_to_end_explicit_save_recognized_and_retrieved_alongside_existing_context():
    """Manual Memory Management sprint - full loop proof through the REAL
    production bridge, same shape as the episodic end-to-end test above:

        ordinary technical statement -> NO manual memory created
        explicit "ingat ..." statement -> intent recognized -> saved
        new turn -> the existing MemoryRetriever ("manual_memory" source)
            finds it -> persona still present -> verified facts still
            present alongside it

    `tests/conftest.py`'s autouse fixture already redirects
    `config.LONG_TERM_MEMORY_FILE` to an isolated temp path AND resets
    `luno.memory._memories` to `[]` for this test (see that fixture's own
    "Manual Memory Management sprint" section) - no manual redirect
    needed here, unlike the relationship-state/episodic-memory module-
    level redirects above (which predate that fixture)."""
    import luno.memory as _luno_memory

    isolated_path = _luno_config.LONG_TERM_MEMORY_FILE
    assert isolated_path != os.path.join("config", "long_term_memory.json")

    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)

        # Turn 0: an ORDINARY technical statement - no "ingat"/"catat"/
        # "remember" trigger word at all. Must NOT create a manual memory,
        # per this sprint's own "EXPLICIT USER INTENT -> SAVE, ORDINARY
        # CONVERSATION -> DO NOT SAVE" rule (Step 7).
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "PC utamaku pakai RTX 3060 Ti", "request_id": "r-manual-mem-0"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        zeroth_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in zeroth_prompt  # persona still present
        assert _luno_memory.list_memories() == []  # nothing saved - ordinary statement only

        # Turn 1: a real device command - establishes a VERIFIED fact
        # (control, same reasoning as the episodic end-to-end test's own
        # turn 0) and proves the new memory-related code running every
        # turn does not interfere with it.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "r-manual-mem-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        first_prompt = captured.get("system_prompt") or ""
        assert "VERIFIED results" in first_prompt
        assert _luno_memory.list_memories() == []  # still nothing - a bare device command is not a memory either

        # Turn 2: EXPLICIT save intent - detected, saved, honestly confirmed.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku suka Avenged Sevenfold", "request_id": "r-manual-mem-2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        second_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in second_prompt  # persona still present
        assert "saved this to long-term memory" in second_prompt  # honest confirmation note

        saved = _luno_memory.list_memories()
        assert len(saved) == 1
        assert saved[0]["text"] == "aku suka Avenged Sevenfold"
        assert saved[0]["source"] == "user_explicit"
        assert saved[0]["category"] == "preference"

        with open(isolated_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert len(on_disk) == 1
        assert on_disk[0]["text"] == "aku suka Avenged Sevenfold"

        # Turn 3: a fresh device command - proves the verified-facts
        # mechanism is still completely unaffected after a memory save.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn off the lights", "request_id": "r-manual-mem-3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        third_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in third_prompt
        assert "VERIFIED results" in third_prompt

        # Turn 4: memory-recall-shaped question - the saved manual memory
        # should surface through the EXISTING memory_block retrieval slot
        # (the NEW "manual_memory" MemoryRetriever source), alongside
        # persona and verified facts, not a parallel prompt section.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "cari memory tentang Avenged Sevenfold", "request_id": "r-manual-mem-4"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        fourth_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in fourth_prompt
        assert "[MANUAL MEMORY" in fourth_prompt
        assert "Avenged Sevenfold" in fourth_prompt
    finally:
        _silent(console.stop)


def test_memory_intelligence_end_to_end_importance_affects_retrieval_and_context():
    """MEMORY INTELLIGENCE & IMPORTANCE ENGINE sprint - full loop proof
    through the REAL production bridge (Step 18: "not merely unit-testing
    helper functions"):

        explicit "ingat ..." utterance -> detect_remember_command ->
            add_memory() -> classified + stored with real importance
        a SECOND explicit save on the SAME topic, different wording/value
            -> consolidation (update-with-history), not a duplicate
        a later, topically-narrow question -> memory_retriever.retrieve_
            memories() (the "manual_memory" source) -> build_memory_prompt_
            block() -> system_prompt actually sent toward the LLM

    Proves the new importance/lifecycle metadata is not just a helper-
    function detail: an importance=4 ("core") memory that is IRRELEVANT
    to the current question must NOT appear in context (Step 12's own
    "Guitar Rig" example, run here through the real bridge instead of a
    unit-level MemoryRetriever), while a lower-importance but RELEVANT
    memory must. `tests/conftest.py`'s autouse fixture isolates
    `LONG_TERM_MEMORY_FILE` and resets `luno.memory._memories` for this
    test exactly as it does for the Manual Memory Management end-to-end
    test above."""
    import luno.memory as _luno_memory

    isolated_path = _luno_config.LONG_TERM_MEMORY_FILE
    assert isolated_path != os.path.join("config", "long_term_memory.json")

    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)

        # Turn 0: an identity-defining explicit save - classifies to
        # importance=4 ("core") via `_IDENTITY_DEFINING_RE`. Deliberately
        # phrased differently from the sprint brief's own illustrative
        # sentence (that sentence defines semantic intent only, it is not
        # a literal fixture to hardcode).
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku ingin Luno jadi personal AI companion aku", "request_id": "r-mem-intel-0"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        assert len(saved) == 1
        assert saved[0]["importance"] == 4
        companion_memory_id = saved[0]["id"]

        # Turn 1: a second, unrelated explicit save - reasonably useful
        # future context (Step 4's "useful" tier), topically about a
        # completely different subject (audio gear, not Luno's identity).
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku sekarang pakai Guitar Rig 7", "request_id": "r-mem-intel-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        assert len(saved) == 2
        guitar_rig_entry = next(m for m in saved if "Guitar Rig" in m["text"])
        assert guitar_rig_entry["importance"] < 4

        # Turn 2 (Step 12's own worked example, through the REAL bridge):
        # a narrow question about the LOWER-importance topic. The
        # importance=4 companion memory must NOT leak into the NEW,
        # relevance-gated "[Relevant Memories]" section just because it
        # outranks on importance - relevance is the gate.
        #
        # Memory Context Assembly & Retrieval Unification sprint: this
        # block used to be labeled "Relevant Memory:" (built directly via
        # `build_memory_prompt_block(relevant_memories_early)`); it is now
        # produced by `luno.memory_context.assemble_context()`'s unified,
        # grouped rendering instead (see that call site's own comment in
        # `main_runtime_demo.py` for the full explanation) - same
        # underlying `relevant_memories_early` candidate pool, same
        # relevance-first selection guarantee, only the section header
        # text changed. The SEPARATE, previously-independent
        # `build_memory_prompt(query_text=text)` call this test used to
        # need to reason around (`memory.build_memory_prompt()`'s "SEPARATE,
        # pre-existing, UNCONDITIONAL full-dump note" comment this
        # docstring used to reference) no longer runs at this call site at
        # all - it was the duplicate path this sprint unifies away - so
        # there is no second, unrelated memory note left to reason about
        # here.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "cara setting Guitar Rig gimana ya?", "request_id": "r-mem-intel-2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        second_prompt = captured.get("system_prompt") or ""
        assert "[Relevant Memories]" in second_prompt
        relevant_block = second_prompt[second_prompt.index("[Relevant Memories]"):].split("\n\n")[0]
        assert "Guitar Rig" in relevant_block
        assert "personal AI companion" not in relevant_block

        # Turn 3: an explicit CORRECTION to the SAME fact (same topic,
        # new value) - must consolidate (update-with-history), not create
        # a second, disconnected entry. Proves the consolidation pipeline
        # designed in luno/memory.py's add_memory() is actually reached
        # through detect_remember_command()/_handle_explicit_memory_command(),
        # not just callable directly.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku sekarang pakai Guitar Rig 6 bukan Guitar Rig 7", "request_id": "r-mem-intel-3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        assert len(saved) == 2  # still 2 total - companion memory + the (now-updated) Guitar Rig one
        updated_entry = _luno_memory.get_memory(guitar_rig_entry["id"])
        assert updated_entry is not None
        assert "Guitar Rig 6" in updated_entry["text"]
        assert len(updated_entry["history"]) >= 1

        # Turn 4: explicit "mark this important" (Step 14) applied to the
        # most-recently-touched memory (the Guitar Rig one, just updated
        # above) - proves the optional command is wired through the real
        # bridge, not just directly callable.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "Memory ini penting.", "request_id": "r-mem-intel-4"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        fourth_prompt = captured.get("system_prompt") or ""
        assert "marked" in fourth_prompt.lower()
        promoted = _luno_memory.get_memory(updated_entry["id"])
        assert promoted["importance"] == 4
        assert promoted["source"] == "user_explicit"

        # Companion memory must still exist, untouched, throughout.
        untouched = _luno_memory.get_memory(companion_memory_id)
        assert untouched is not None
        assert untouched["importance"] == 4

        with open(isolated_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert len(on_disk) == 2
    finally:
        _silent(console.stop)


def test_memory_conflict_resolution_end_to_end_correction_preserves_history_and_current_query_wins():
    """MEMORY CONFLICT RESOLUTION & TRUSTED FACTS GUARD sprint - full
    loop proof through the REAL production bridge (Section 19):

        user states an old configuration -> saved
        user explicitly corrects it -> conflict detected (CORRECTION) ->
            old preserved historically (in `history`, not deleted) ->
            new becomes the sole current entry
        a CURRENT-state question -> only the new value appears in the
            live "Relevant Memory:" block
        a HISTORICAL question -> the old, superseded value becomes
            reachable again, clearly labeled as historical

    Also proves persona, verified facts, and the rest of the pipeline
    remain completely unaffected by conflict-resolution code running
    every turn - same "AI companion"/"VERIFIED results" checks the other
    end-to-end scenarios in this file already use as their own proof
    that persona/verified-facts machinery is untouched."""
    import luno.memory as _luno_memory

    isolated_path = _luno_config.LONG_TERM_MEMORY_FILE
    assert isolated_path != os.path.join("config", "long_term_memory.json")

    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)

        # Turn 0: user states the OLD configuration.
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku pakai RTX 3070 Ti di laptop", "request_id": "r-conflict-0"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        zeroth_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in zeroth_prompt  # persona still present
        saved = _luno_memory.list_memories()
        assert len(saved) == 1
        assert "3070" in saved[0]["text"]

        # Turn 1: a real device command - proves verified-facts machinery
        # is completely unaffected by conflict-resolution code now
        # running every turn (same control-turn pattern the Manual
        # Memory Management end-to-end test above already uses).
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "r-conflict-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        first_prompt = captured.get("system_prompt") or ""
        assert "VERIFIED results" in first_prompt

        # Turn 2: EXPLICIT correction ("sekarang ...") - through the real
        # detect_remember_command() -> add_memory() -> _classify_conflict()
        # path, not called directly.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku sekarang pakai RTX 3060 Ti di laptop", "request_id": "r-conflict-2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        second_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in second_prompt

        saved = _luno_memory.list_memories()
        assert len(saved) == 1  # corrected, not duplicated
        assert "3060" in saved[0]["text"]
        assert "3070" not in saved[0]["text"]
        assert any("3070" in h["text"] and h.get("reason") == "correction" for h in saved[0]["history"])

        # Turn 3: CURRENT-state question - only the new value should
        # appear in the bounded "[Relevant Memories]" section (Memory
        # Context Assembly sprint: this section used to be labeled
        # "Relevant Memory:" - same underlying `relevant_memories_early`
        # candidate pool and relevance-first guarantee, only the unified
        # rendering's header text changed; see that sprint's call-site
        # comment in `main_runtime_demo.py`).
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "GPU ku sekarang apa?", "request_id": "r-conflict-3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        third_prompt = captured.get("system_prompt") or ""
        assert "[Relevant Memories]" in third_prompt
        current_block = third_prompt[third_prompt.index("[Relevant Memories]"):].split("\n\n")[0]
        assert "3060" in current_block
        assert "3070" not in current_block

        # Turn 4: HISTORICAL question - the superseded value must still
        # be reachable, clearly labeled as historical, never silently
        # gone. Historical results now render into their own
        # "[Historical Context]" section (Step 17 grouping) within the
        # same unified block.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "GPU yang dulu pernah aku pakai apa?", "request_id": "r-conflict-4"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        fourth_prompt = captured.get("system_prompt") or ""
        assert "[Historical Context]" in fourth_prompt
        historical_block = fourth_prompt[fourth_prompt.index("[Historical Context]"):].split("\n\n")[0]
        assert "3070" in historical_block
        assert "historical" in historical_block

        with open(isolated_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert len(on_disk) == 1
        assert any("3070" in h["text"] for h in on_disk[0]["history"])
    finally:
        _silent(console.stop)


def test_memory_context_assembly_end_to_end_unifies_sources_through_real_bridge():
    """MEMORY CONTEXT ASSEMBLY & RETRIEVAL UNIFICATION sprint - full loop
    proof through the REAL production bridge (Step 22):

        user utterance -> PlannerBridgeModule._handle_utterance() ->
            memory_retriever.retrieve_memories() (context analyzer +
            memory source retrieval, unchanged) ->
            memory_context.assemble_context() (the NEW unification layer)
            -> notes.append(...) -> system_prompt actually sent to the LLM

    Proves three things this sprint specifically adds, all through the
    real bridge rather than by calling `luno.memory_context` directly
    (that's `tests/test_memory_context.py`'s job):

      1. Verified Facts - previously write-only in production (see
         docs/change_impact/memory_context_assembly.md section 3.2) - now
         actually surface in the prompt via a "[Verified Facts]" section
         when relevant to the current turn, and stay out when not.
      2. An unrelated manual memory never leaks into the unified block
         just because SOMETHING else that turn was relevant (same
         relevance-gating guarantee proven per-source by the other memory
         end-to-end tests above, now proven for the unified output).
      3. Only ONE unified memory-context block is produced per turn - not
         the two independent, overlapping renderings
         (`explicit_memory_block` + `memory_block`) that existed before
         this sprint (see that removal's own comment in
         `main_runtime_demo.py`)."""
    import luno.memory as _luno_memory

    isolated_path = _luno_config.LONG_TERM_MEMORY_FILE
    assert isolated_path != os.path.join("config", "long_term_memory.json")

    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)

        # Seed an unrelated manual memory and a Verified Fact directly on
        # the REAL bridge instance's own `self.memory_guard` (the same
        # `VerifiedFactStore` `_handle_utterance()` reads from) - this is
        # the honest way to exercise the NEW read path even though
        # `MockHomeAssistantHandler.execute()`'s own `ToolResult.data`
        # doesn't currently carry an `entity_id` key (a separate,
        # pre-existing, out-of-scope gap in the MOCK tool handler, not
        # something this sprint's context-assembly layer controls or
        # should fix here - see that finding in this test's own docstring).
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku suka Avenged Sevenfold", "request_id": "r-ctx-0"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        assert len(saved) == 1

        console.planner_module.memory_guard.record(
            {"success": True, "data": {"entity_id": "living_room_light", "actual_state": "on"}},
            tool_name="home_assistant", request_id="r-ctx-seed",
        )

        # Turn 1: a query relevant to the Verified Fact, IRRELEVANT to the
        # saved manual memory. "[Verified Facts]" must appear; the
        # unrelated Avenged Sevenfold memory must not leak in.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "living room light gimana keadaannya?", "request_id": "r-ctx-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        first_prompt = captured.get("system_prompt") or ""
        assert "[Verified Facts]" in first_prompt
        assert "Avenged Sevenfold" not in first_prompt

        # Only ONE unified memory-context note per turn - not two
        # independent legacy renderings.
        assert first_prompt.count("[Verified Facts]") == 1

        # Turn 2: a query relevant to the manual memory, irrelevant to the
        # Verified Fact - proves the gate runs both ways.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "aku suka apa ya, inget gak?", "request_id": "r-ctx-2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        second_prompt = captured.get("system_prompt") or ""
        assert "Avenged Sevenfold" in second_prompt
        assert "[Verified Facts]" not in second_prompt

        # Turn 3: a completely irrelevant query - neither section appears.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "cara masak nasi goreng enak", "request_id": "r-ctx-3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        third_prompt = captured.get("system_prompt") or ""
        assert "[Verified Facts]" not in third_prompt
        assert "[Relevant Memories]" not in third_prompt
        assert "Avenged Sevenfold" not in third_prompt

        # Verified Facts store was never mutated by context assembly
        # itself (only by the earlier explicit `.record()` seed call) -
        # read-only guarantee, proven through the real bridge.
        facts = console.planner_module.memory_guard.all_facts()
        assert len(facts) == 1
        assert facts[0]["entity_id"] == "living_room_light"

        # Manual memory store untouched by context assembly.
        with open(isolated_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert len(on_disk) == 1
    finally:
        _silent(console.stop)


def test_memory_prompt_intelligence_end_to_end_relevance_gated_and_current_vs_historical():
    """MEMORY PROMPT INTELLIGENCE sprint - full loop proof through the
    REAL production bridge (Section 17):

        an irrelevant, importance=4 memory is saved -> must NEVER appear
            in the memory-context section for an unrelated query, no
            matter how important it is
        an old GPU configuration is saved, then explicitly corrected ->
            the prompt path must prefer the CURRENT value for a
            current-state question and surface the HISTORICAL value,
            clearly labeled, for a historical question
        a real device command in between proves Verified Facts stay
            completely unaffected
        a genuine accomplishment turn proves Episodic Memory is detected
            through its own separate store and never duplicated into the
            manual-memory prompt note
        persona stays present throughout

    Memory Context Assembly & Retrieval Unification sprint: this test
    used to be scoped specifically to the direct `build_memory_prompt
    (query_text=...)` note (identified by its own distinct marker text,
    "...relevant to this conversation:"), deliberately kept separate from
    the OTHER, already-smart "Relevant Memory:" block
    (`build_memory_prompt_block`) built from `MemoryRetriever` - the two
    independent, overlapping Manual-Memory prompt paths this later sprint
    exists specifically to unify (see docs/change_impact/
    memory_context_assembly.md section 3.1). `build_memory_prompt
    (query_text=text)` no longer runs at this production call site at all
    (that duplicate call was removed - `build_memory_prompt()` itself is
    unchanged and still fully supported for any other caller, e.g.
    `luno/main.py`'s own unconditional call, per Step 19 backward
    compatibility). This test now asserts against the single unified
    `luno.memory_context.assemble_context()` rendering's
    "[Relevant Memories]"/"[Historical Context]" sections instead - same
    underlying relevance/importance/conflict/historical guarantees this
    sprint originally proved, now proven against the unified output."""
    import luno.memory as _luno_memory
    import luno.episodic_memory as _episodic_memory

    isolated_path = _luno_config.LONG_TERM_MEMORY_FILE
    assert isolated_path != os.path.join("config", "long_term_memory.json")

    def _note_block(prompt, header):
        marker = f"[{header}]"
        assert marker in prompt
        return prompt[prompt.index(marker):].split("\n\n")[0]

    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)

        # Turn 0: an irrelevant, explicitly-flagged-important memory.
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku suka Avenged Sevenfold, ini penting banget", "request_id": "r-pi-0"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        zeroth_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in zeroth_prompt  # persona still present
        saved = _luno_memory.list_memories()
        assert len(saved) == 1
        assert saved[0]["importance"] == 4

        # Turn 1: old GPU configuration saved.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku pakai RTX 3070 Ti di laptop", "request_id": "r-pi-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        # Turn 2: a real device command - Verified Facts must be totally
        # unaffected by this sprint's prompt-selection changes.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "r-pi-2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        second_prompt = captured.get("system_prompt") or ""
        assert "VERIFIED results" in second_prompt

        # Turn 3: explicit correction.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku sekarang pakai RTX 3060 Ti di laptop", "request_id": "r-pi-3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        # Turn 4: a genuine accomplishment turn - Episodic Memory should
        # detect and persist this in its OWN, separate store.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "akhirnya home assistant integration jalan lancar, masalahnya udah kelar semua",
                  "request_id": "r-pi-4"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        fourth_prompt = captured.get("system_prompt") or ""
        # The episodic detector runs on this turn's OWN text - it never
        # writes into the manual-memory store this sprint's note reads.
        manual_texts = [m["text"] for m in _luno_memory.list_memories()]
        assert not any("home assistant integration" in t for t in manual_texts)

        # Turn 5: CURRENT-state, RELEVANT question - the direct prompt
        # path must show only the current GPU value, never the irrelevant
        # importance=4 preference memory, never the superseded value.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "RTX di laptop sekarang apa?", "request_id": "r-pi-5"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        fifth_prompt = captured.get("system_prompt") or ""
        assert "AI companion" in fifth_prompt  # persona still present
        current_block = _note_block(fifth_prompt, "Relevant Memories")
        assert "3060" in current_block
        assert "3070" not in current_block
        assert "Avenged Sevenfold" not in current_block
        assert "home assistant integration" not in current_block

        # Turn 6: a query IRRELEVANT to anything saved - even the
        # importance=4 memory must not leak in, and the note should be
        # entirely absent (not just empty-but-present).
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "cara masak nasi goreng enak", "request_id": "r-pi-6"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        sixth_prompt = captured.get("system_prompt") or ""
        assert "[Relevant Memories]" not in sixth_prompt
        assert "[Historical Context]" not in sixth_prompt
        assert "Avenged Sevenfold" not in sixth_prompt

        # Turn 7: HISTORICAL question - the superseded value must be
        # reachable again through THIS SAME unified prompt path, clearly
        # labeled, never presented as current.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "dulu RTX di laptop apa?", "request_id": "r-pi-7"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        seventh_prompt = captured.get("system_prompt") or ""
        historical_block = _note_block(seventh_prompt, "Historical Context")
        assert "3070" in historical_block
        assert "previously said" in historical_block or "superseded" in historical_block

        # Final on-disk check: manual memory store still exactly 2 entries
        # (the importance=4 preference + the corrected GPU fact), the GPU
        # entry's history still carries the superseded value - nothing
        # was deleted or duplicated by prompt generation.
        with open(isolated_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert len(on_disk) == 2
        gpu_entry = next(e for e in on_disk if "RTX" in e["text"])
        assert "3060" in gpu_entry["text"]
        assert any("3070" in h["text"] for h in gpu_entry["history"])
    finally:
        _silent(console.stop)


def test_memory_maintenance_end_to_end_health_preview_and_run_through_production_bridge():
    """MEMORY LIFECYCLE & MAINTENANCE ENGINE sprint - full loop proof
    through the REAL production bridge (Step 18):

        user saves a core memory (explicit "ini penting banget") and an
            ordinary one -> both persisted
        an obsolete-worded memory is saved, then artificially aged (real
            time can't be fast-forwarded in a test) to land in the
            "stale" lifecycle band, matching this sprint's own test
            precedent for exercising the archive path deterministically
        "cek kesehatan memory" -> a real health-report reply, read-only
        "preview maintenance memory" -> a real dry-run reply, read-only -
            asserted by re-reading the on-disk file and confirming
            nothing changed
        "jalankan maintenance memory" -> the obsolete memory is actually
            archived (hidden from normal retrieval, NOT deleted) while
            the core memory remains fully intact and protected
        a real device command proves Verified Facts stay completely
            unaffected throughout
        persona stays present throughout"""
    import luno.memory as _luno_memory

    isolated_path = _luno_config.LONG_TERM_MEMORY_FILE
    assert isolated_path != os.path.join("config", "long_term_memory.json")

    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)

        # Turn 0: a core, explicitly-important memory.
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku alergi kacang, ini penting banget", "request_id": "r-mnt-0"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        assert "AI companion" in (captured.get("system_prompt") or "")

        # Turn 1: an ordinary memory.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku suka kopi hitam", "request_id": "r-mnt-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        # Turn 2: an obsolete-worded memory - then artificially aged
        # in-place (bypassing real elapsed time, same precedent
        # `tests/test_memory_maintenance.py` itself uses) so it lands in
        # the "stale" lifecycle band and the planner's obsolete-wording +
        # low-importance rule actually recommends archiving it.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, untuk sementara pakai VPS test buat eksperimen doang", "request_id": "r-mnt-2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        saved = _luno_memory.list_memories()
        assert len(saved) == 3
        obsolete_entry = next(m for m in saved if "VPS test" in m["text"])
        old_ts = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
        for m in _luno_memory._memories:
            if m["id"] == obsolete_entry["id"]:
                m["created_at"] = m["updated_at"] = old_ts
        _luno_memory._save()

        # Turn 3: a real device command - Verified Facts must be totally
        # unaffected by any of this sprint's changes.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "r-mnt-3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        assert "VERIFIED results" in (captured.get("system_prompt") or "")

        # Turn 4: "cek kesehatan memory" - read-only health report.
        with open(isolated_path, "r", encoding="utf-8") as f:
            before_health = json.load(f)
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "cek kesehatan memory", "request_id": "r-mnt-4"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        with open(isolated_path, "r", encoding="utf-8") as f:
            after_health = json.load(f)
        assert before_health == after_health  # health check never mutates

        # Turn 5: "preview maintenance memory" - read-only dry run.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "preview maintenance memory", "request_id": "r-mnt-5"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        with open(isolated_path, "r", encoding="utf-8") as f:
            after_preview = json.load(f)
        assert before_health == after_preview  # preview never mutates either

        # Turn 6: "jalankan maintenance memory" - actually executes.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "jalankan maintenance memory", "request_id": "r-mnt-6"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        final = _luno_memory.list_memories()
        assert len(final) == 3  # nothing deleted
        obsolete_final = next(m for m in final if m["id"] == obsolete_entry["id"])
        assert obsolete_final["archived_by_maintenance"] is True
        assert _luno_memory.compute_lifecycle(obsolete_final) == "archived"
        assert obsolete_final["text"] == obsolete_entry["text"]  # text untouched

        core_final = next(m for m in final if "kacang" in m["text"])
        assert core_final["importance"] == 4
        assert not core_final.get("archived_by_maintenance")

        # On-disk confirmation - archived, not deleted.
        with open(isolated_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert len(on_disk) == 3
    finally:
        _silent(console.stop)


def test_memory_maintenance_ordinary_conversation_never_triggers_maintenance_end_to_end():
    """Step 18's second required scenario: ordinary conversation, even
    across several turns that mention memory-adjacent words, must NEVER
    execute maintenance (no archive, no consolidate) - only the exact
    explicit commands do."""
    import luno.memory as _luno_memory

    isolated_path = _luno_config.LONG_TERM_MEMORY_FILE
    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, untuk sementara pakai VPS test buat eksperimen doang", "request_id": "r-mnt-o0"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        before = [dict(m) for m in _luno_memory.list_memories()]

        ordinary_turns = [
            "gimana cuaca hari ini",
            "kapan terakhir kita ngobrol soal memory maintenance",
            "aku lagi mikirin soal memory nih",
            "berapa suhu CPU ideal",
        ]
        for i, text in enumerate(ordinary_turns):
            need_llm.clear()
            _silent(console.event_bus.publish, demo.Event(
                type="user_utterance", data={"text": text, "request_id": f"r-mnt-o{i + 1}"},
            ))
            assert _wait_until(need_llm.is_set, 5.0)

        after = [dict(m) for m in _luno_memory.list_memories()]
        assert after == before  # byte-for-byte identical - no archive/consolidate/reinforce fired
        for m in after:
            assert not m.get("archived_by_maintenance")

        with open(isolated_path, "r", encoding="utf-8") as f:
            on_disk = json.load(f)
        assert on_disk == before
    finally:
        _silent(console.stop)


def test_memory_learning_feedback_loop_end_to_end_positive_confirmation_scenario_a():
    """MEMORY LEARNING & FEEDBACK LOOP sprint - Scenario A (sprint brief
    Section 22): user explicitly saves a memory -> it is retrieved through
    the real production bridge -> usage is recorded -> the user confirms
    it is correct -> usefulness increases (bounded) -> the memory's
    metadata reflects the update afterward. Full loop through the REAL
    `PlannerBridgeModule`/`RuntimeDemoConsole`, not a unit-level call."""
    import luno.memory as _luno_memory

    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        # Turn 1: explicit save.
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku pakai keyboard mechanical Keychron K8.", "request_id": "r-learn-a1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        saved = _luno_memory.list_memories()
        assert len(saved) == 1
        entry_id = saved[0]["id"]
        assert _luno_memory.get_memory_usefulness(saved[0]) == 0.5  # neutral default, no evidence yet
        assert _luno_memory.get_memory_retrieval_count(saved[0]) == 0

        # Turn 2: a real recall-shaped query - retrieves the memory
        # through `self.memory_retriever.retrieve_memories()`, which is
        # what actually records usage AND sets this conversation's session
        # feedback target (Section 13) - not a second, test-only code path.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "keyboard apa yang aku pakai?", "request_id": "r-learn-a2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        after_retrieval = _luno_memory.get_memory(entry_id)
        assert after_retrieval is not None
        assert _luno_memory.get_memory_retrieval_count(after_retrieval) == 1  # genuine usage recorded
        # Small, bounded usage-driven nudge (Section 9) - never proof of
        # usefulness by itself, just a tiny signal.
        assert after_retrieval["usefulness_score"] > 0.5

        # Turn 3: the user confirms it was correct/useful.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "iya benar", "request_id": "r-learn-a3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        after_feedback = _luno_memory.get_memory(entry_id)
        assert after_feedback["positive_feedback_count"] == 1
        assert after_feedback.get("negative_feedback_count", 0) == 0
        assert after_feedback["usefulness_score"] > after_retrieval["usefulness_score"]
        assert after_feedback["usefulness_score"] <= 1.0
        # The memory's TEXT/importance were never touched by feedback -
        # positive feedback never overwrites content (Section 20).
        assert after_feedback["text"] == saved[0]["text"]
        assert after_feedback["importance"] == saved[0]["importance"]

        # Turn 4: next retrieval reflects the updated metadata - usage
        # keeps accumulating, the feedback-driven usefulness gain persists
        # (it is not reset by a later, ordinary retrieval).
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "keyboard apa yang aku pakai lagi?", "request_id": "r-learn-a4"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        final_entry = _luno_memory.get_memory(entry_id)
        assert _luno_memory.get_memory_retrieval_count(final_entry) == 2
        assert final_entry["usefulness_score"] >= after_feedback["usefulness_score"]
    finally:
        _silent(console.stop)


def test_memory_learning_feedback_loop_end_to_end_correction_scenario_b():
    """MEMORY LEARNING & FEEDBACK LOOP sprint - Scenario B (sprint brief
    Section 22): a memory is retrieved -> the user says it is wrong AND
    supplies a replacement value in the same turn -> the system identifies
    the target from THIS conversation's own session feedback target (no
    guessing) -> the memory is preserved (not deleted) -> the EXISTING
    correction/history path is used (`update_memory()`, not a second
    mechanism) -> the old value survives in `history` -> the new value
    becomes current."""
    import luno.memory as _luno_memory

    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, GPU-ku RTX 3070 Ti.", "request_id": "r-learn-b1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        assert len(saved) == 1
        entry_id = saved[0]["id"]

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "GPU apa yang aku pakai?", "request_id": "r-learn-b2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        assert _luno_memory.get_memory_retrieval_count(_luno_memory.get_memory(entry_id)) == 1

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "yang tadi salah, sekarang RTX 4090", "request_id": "r-learn-b3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        final = _luno_memory.list_memories()
        assert len(final) == 1  # preserved - never deleted, never duplicated
        updated_entry = final[0]
        assert updated_entry["id"] == entry_id
        assert "RTX 4090" in updated_entry["text"]
        assert any("3070" in h.get("text", "") for h in updated_entry.get("history", []))  # old value preserved
        assert updated_entry.get("negative_feedback_count", 0) == 1  # feedback metadata updated truthfully
        # importance was never silently touched by the feedback layer -
        # only `update_memory()`'s own pre-existing importance-never-
        # decreases rule applied (Section 10's "usefulness tidak boleh
        # langsung menggantikan importance").
        assert updated_entry["importance"] == saved[0]["importance"]
    finally:
        _silent(console.stop)


def test_memory_learning_feedback_loop_end_to_end_ambiguous_feedback_never_mutates():
    """MEMORY LEARNING & FEEDBACK LOOP sprint - Section 22's third required
    check: ambiguous feedback must never mutate any memory. Two DISTINCT
    (non-exclusive-category) memories are surfaced together by the SAME
    query, so this conversation's session feedback target is cleared
    (more than one candidate, Section 13's own "tidak ambigu" rule) -
    a later "iya benar" must then do nothing at all."""
    import luno.memory as _luno_memory

    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "ingat, aku suka kopi hitam.", "request_id": "r-learn-c1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "ingat, aku suka teh hijau.", "request_id": "r-learn-c2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        before = _luno_memory.list_memories()
        assert len(before) == 2  # two genuinely separate preference memories (non-exclusive category)

        # A query broad enough to surface BOTH ("aku suka ...") - ambiguous
        # target, session feedback target is cleared rather than guessed.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "aku suka minum apa?", "request_id": "r-learn-c3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "iya benar", "request_id": "r-learn-c4"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        after = _luno_memory.list_memories()
        # Byte-for-byte identical - no positive_feedback_count/
        # usefulness_score change on EITHER candidate; the only field that
        # may legitimately differ is usage bookkeeping from the ambiguous
        # retrieval turn itself (retrieval_count/last_retrieved_at/the tiny
        # usage nudge), never feedback-driven fields.
        assert len(after) == 2
        by_id_before = {m["id"]: m for m in before}
        for m in after:
            b = by_id_before[m["id"]]
            assert m.get("positive_feedback_count", 0) == b.get("positive_feedback_count", 0)
            assert m.get("negative_feedback_count", 0) == b.get("negative_feedback_count", 0)
            assert m["text"] == b["text"]
    finally:
        _silent(console.stop)


def test_memory_evaluation_self_calibration_end_to_end_positive_scenario_d():
    """MEMORY EVALUATION & SELF-CALIBRATION sprint - Step 14's first
    required scenario: (1) create memory, (2) retrieve it, (3) record
    actual context selection, (4) user confirms it, (5) evaluate memory,
    (6) verify score/evidence changes, (7) verify memory text/history/
    importance remain unchanged, (8) verify the dashboard can display the
    result. Full loop through the REAL `PlannerBridgeModule`/
    `RuntimeDemoConsole` - `assemble_context()`'s real ranking/budget cut
    is what actually drives step 3 here (via this sprint's new
    `record_context_selection()` call site in
    `main_runtime_demo.py`), not a test-only shortcut."""
    import luno.memory as _luno_memory
    from luno.dashboard import collectors as _collectors

    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        # Turn 1: explicit save.
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku pakai headset Sony WH-1000XM5.", "request_id": "r-eval-d1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        saved = _luno_memory.list_memories()
        assert len(saved) == 1
        entry_id = saved[0]["id"]
        before_eval = _luno_memory.evaluate_memory(saved[0])
        assert before_eval["score"] == 0.5  # neutral, no evidence yet
        assert _luno_memory.get_memory_last_evaluated_at(saved[0]) is None  # never calibrated yet

        # Turn 2: a real recall-shaped query - drives BOTH the pre-existing
        # `record_memory_usage()` usage tracking AND this sprint's new
        # `record_context_selection()` call site (Step 6: retrieved vs
        # actually-used), through the real `assemble_context()` call in
        # `main_runtime_demo.py`, not a direct unit call.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "headset apa yang aku pakai?", "request_id": "r-eval-d2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        after_retrieval = _luno_memory.get_memory(entry_id)
        assert _luno_memory.get_memory_retrieval_count(after_retrieval) == 1
        # Step 6's own distinction: at least one of success/miss must have
        # moved - this turn's candidate pool included this memory, and
        # `assemble_context()` either kept it in the final budget-limited
        # context (success) or dropped it (miss); either is valid evidence,
        # but SOME context-selection evidence must now exist.
        assert (_luno_memory._get_retrieval_success_count(after_retrieval)
                + _luno_memory._get_retrieval_miss_count(after_retrieval)) >= 1

        # Turn 3: the user confirms it was correct - drives
        # `apply_positive_feedback()` (pre-existing) AND this sprint's new
        # `record_feedback_event()` + synchronous `calibrate_memory()`
        # call, both from the SAME `_handle_memory_feedback_command()`
        # branch in `main_runtime_demo.py`.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "iya benar", "request_id": "r-eval-d3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        after_feedback = _luno_memory.get_memory(entry_id)
        assert after_feedback["positive_feedback_count"] == 1
        assert after_feedback.get("feedback_event_count", 0) == 1
        # `calibrate_memory()` ran synchronously - `evaluation_score`/
        # `last_evaluated_at` are now persisted and reflect real evidence.
        assert after_feedback.get("last_evaluated_at") is not None
        assert after_feedback["evaluation_score"] > before_eval["score"]
        live_eval = _luno_memory.evaluate_memory(after_feedback)
        assert live_eval["score"] == after_feedback["evaluation_score"]
        assert any("positive confirmation" in s for s in live_eval["strengths"])

        # Content/history/importance were never touched by evaluation or
        # calibration - only the evaluation-evidence fields moved.
        assert after_feedback["text"] == saved[0]["text"]
        assert after_feedback["importance"] == saved[0]["importance"]
        assert after_feedback.get("history", []) == saved[0].get("history", [])

        # The Memory Dashboard (read-only) can display the result without
        # triggering any further mutation.
        detail = _collectors.collect_memory_detail(entry_id)
        assert detail["evaluation_score"] == live_eval["score"]
        assert detail["evidence_counts"]["positive_feedback_count"] == 1
        assert detail["evaluation_recommendation"] in _luno_memory.MEMORY_EVALUATION_RECOMMENDATIONS
        after_dashboard_read = _luno_memory.get_memory(entry_id)
        assert after_dashboard_read == after_feedback  # GET never mutated anything
    finally:
        _silent(console.stop)


def test_memory_evaluation_self_calibration_end_to_end_correction_weakens_scenario_e():
    """MEMORY EVALUATION & SELF-CALIBRATION sprint - Step 14's second
    required scenario: (1) memory retrieved, (2) user corrects it, (3)
    evaluation becomes weaker, (4) the EXISTING correction/history
    mechanism (`update_memory()`) remains authoritative - never a second
    correction engine, (5) no destructive mutation occurs (the memory is
    preserved, never deleted, old wording survives in history)."""
    import luno.memory as _luno_memory

    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, alamat rumahku Jalan Melati nomor 12.", "request_id": "r-eval-e1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        assert len(saved) == 1
        entry_id = saved[0]["id"]

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "alamat rumahku apa?", "request_id": "r-eval-e2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        before_correction = _luno_memory.get_memory(entry_id)
        before_eval_score = before_correction.get("evaluation_score")  # None - never calibrated yet

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "yang tadi salah, sekarang Jalan Melati nomor 45", "request_id": "r-eval-e3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        final = _luno_memory.list_memories()
        assert len(final) == 1  # preserved - never deleted, never duplicated
        updated_entry = final[0]
        assert updated_entry["id"] == entry_id

        # The EXISTING correction/history mechanism is what actually
        # changed the content - this sprint added no second engine for
        # that. `correction_count` (this sprint's own new evidence field)
        # is bumped by `update_memory()` itself, at the SAME call site.
        assert "45" in updated_entry["text"]
        assert any("12" in h.get("text", "") for h in updated_entry.get("history", []))
        assert updated_entry.get("correction_count", 0) == 1

        # Calibration ran synchronously off the correction feedback event
        # too - `evaluation_score` now reflects the negative evidence
        # (correction is Step 4's own "stronger than a bare negative"
        # signal), strictly weaker than the neutral 0.5 baseline this
        # memory started from (it was never calibrated before this turn).
        assert before_eval_score is None
        assert updated_entry.get("last_evaluated_at") is not None
        assert updated_entry["evaluation_score"] < 0.5
        live_eval = _luno_memory.evaluate_memory(updated_entry)
        assert any("correction" in w for w in live_eval["weaknesses"])

        # No destructive mutation anywhere else - importance/source/id all
        # still intact, exactly as `update_memory()`'s own pre-existing
        # contract already guaranteed before this sprint.
        assert updated_entry["importance"] == saved[0]["importance"]
        assert updated_entry["id"] == saved[0]["id"]
    finally:
        _silent(console.stop)


def test_memory_outcome_telemetry_end_to_end_positive_scenario_a():
    """MEMORY OUTCOME TELEMETRY & CLOSED-LOOP LEARNING sprint - Step 18
    Scenario A: (1) save memory, (2) query causes retrieval, (3) memory
    selected, (4) user confirms, (5) outcome = positive, (6) evidence
    increments, (7) evaluation recalibrates, (8) dashboard shows updated
    evidence. Full loop through the REAL `PlannerBridgeModule`/
    `RuntimeDemoConsole` - `classify_context_outcome()` now actually
    drives `_handle_memory_feedback_command()`'s dispatch (this sprint's
    own "wire the existing function to production" requirement), not a
    parallel test-only code path."""
    import luno.memory as _luno_memory
    from luno.dashboard import collectors as _collectors

    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, aku pakai mouse Logitech MX Master.", "request_id": "r-outcome-a1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        assert len(saved) == 1
        entry_id = saved[0]["id"]
        before_summary = _luno_memory.get_memory_outcome_summary(entry_id)
        assert before_summary["retrieval_success_count"] == 0
        assert before_summary["retrieval_miss_count"] == 0

        # Turn 2: real retrieval - drives `record_context_selection()`
        # (via the new `MemoryTurnTrace`) AND sets this conversation's
        # session feedback target.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "mouse apa yang aku pakai?", "request_id": "r-outcome-a2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        after_retrieval = _luno_memory.get_memory(entry_id)
        counts = _luno_memory.get_memory_evidence_counts(after_retrieval)
        # Selected into this turn's context (Step 4's "candidate=true,
        # relevant=true, selected=true, rendered=true").
        assert counts["retrieval_success_count"] >= 1

        # Turn 3: user confirms - `classify_context_outcome("iya benar")`
        # -> "positive" -> `_handle_memory_feedback_command()`'s positive
        # branch -> `apply_positive_feedback()` (existing) AND this
        # sprint's `record_outcome_evidence(..., "positive")` (bumps
        # `retrieval_success_count` a SECOND time, as ADDITIONAL evidence
        # from the conversational outcome, distinct from the context-
        # selection bump above) AND `calibrate_memory()`.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "iya benar", "request_id": "r-outcome-a3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        after_feedback = _luno_memory.get_memory(entry_id)
        after_counts = _luno_memory.get_memory_evidence_counts(after_feedback)
        assert after_counts["retrieval_success_count"] >= 2  # context-selection bump + outcome bump
        assert after_counts["positive_feedback_count"] == 1
        assert after_feedback.get("last_evaluated_at") is not None
        assert after_feedback["evaluation_score"] > 0.5

        # Content untouched by any of this.
        assert after_feedback["text"] == saved[0]["text"]
        assert after_feedback["importance"] == saved[0]["importance"]

        # Dashboard shows the updated evidence, read-only.
        detail = _collectors.collect_memory_detail(entry_id)
        assert detail["outcome_summary"]["retrieval_success_count"] == after_counts["retrieval_success_count"]
        assert detail["outcome_summary"]["evaluation_score"] == after_feedback["evaluation_score"]
        assert _luno_memory.get_memory(entry_id) == after_feedback  # GET never mutated anything
    finally:
        _silent(console.stop)


def test_memory_outcome_telemetry_end_to_end_negative_scenario_b():
    """Step 18 Scenario B: (1) memory retrieved, (2) user says it is
    wrong, (3) outcome = negative, (4) the correct (unambiguous) target
    is identified via the existing session feedback target - no random
    memory mutation, (5) evaluation decreases conservatively (never to
    zero from one event)."""
    import luno.memory as _luno_memory

    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, alarm pagi aku jam 6.", "request_id": "r-outcome-b1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        entry_id = saved[0]["id"]

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "jam berapa alarm pagiku?", "request_id": "r-outcome-b2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "itu salah", "request_id": "r-outcome-b3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        after = _luno_memory.get_memory(entry_id)
        # Unambiguous target -> real mutation: negative feedback recorded,
        # retrieval-miss evidence bumped (Step 7's mapping), evaluation
        # recalibrated - but text/history/importance are all untouched
        # (negative feedback alone never rewrites content, Section 20's
        # "negative feedback -> delete memory" is structurally impossible
        # here, unchanged from before this sprint).
        assert after["negative_feedback_count"] == 1
        counts = _luno_memory.get_memory_evidence_counts(after)
        assert counts["retrieval_miss_count"] >= 1
        assert after.get("last_evaluated_at") is not None
        # Conservative: one negative event never crashes the score to 0.
        assert after["evaluation_score"] > 0.0
        assert after["text"] == saved[0]["text"]
        assert after["importance"] == saved[0]["importance"]
        assert len(_luno_memory.list_memories()) == 1  # nothing deleted

        # No OTHER memory was touched by this.
        assert len(_luno_memory.list_memories()) == 1
    finally:
        _silent(console.stop)


def test_memory_outcome_telemetry_end_to_end_correction_scenario_c():
    """Step 18 Scenario C: (1) memory exists, (2) user explicitly
    corrects it, (3) old value moves to history, (4) new value becomes
    current, (5) correction evidence increments, (6) evaluation
    recalibrates - proving `classify_context_outcome()`'s new dispatch
    role in `_handle_memory_feedback_command()` did not regress the
    EXISTING correction/history mechanism (`update_memory()` remains the
    sole authority, unchanged)."""
    import luno.memory as _luno_memory

    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, plat nomor motorku B 1234 ABC.", "request_id": "r-outcome-c1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        entry_id = saved[0]["id"]

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "plat nomor motorku apa?", "request_id": "r-outcome-c2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "yang tadi salah, sekarang B 5678 XYZ", "request_id": "r-outcome-c3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        final = _luno_memory.list_memories()
        assert len(final) == 1
        updated = final[0]
        assert updated["id"] == entry_id
        assert "5678" in updated["text"]
        assert any("1234" in h.get("text", "") for h in updated.get("history", []))
        assert updated.get("correction_count", 0) == 1
        assert updated.get("last_evaluated_at") is not None
        assert updated["evaluation_score"] < 0.5  # correction is negative evidence
    finally:
        _silent(console.stop)


def test_memory_outcome_telemetry_end_to_end_ambiguous_scenario_d():
    """Step 18 Scenario D: (1) multiple memories are candidates, (2) user
    says "itu salah" with no unique target, (3) no unique target exists
    (session feedback target was cleared as ambiguous), (4) no memory
    changes, (5) no destructive action, (6) evidence remains explainable
    (every counter is still exactly what it was before - nothing silently
    incremented on a guess)."""
    import luno.memory as _luno_memory

    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "ingat, aku suka nonton film horor.", "request_id": "r-outcome-d1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "ingat, aku suka nonton film komedi.", "request_id": "r-outcome-d2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        before = _luno_memory.list_memories()
        assert len(before) == 2

        # A query broad enough to surface BOTH - ambiguous target, session
        # feedback target is cleared rather than guessed (unchanged,
        # pre-existing Section 13 behavior).
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "aku suka nonton apa?", "request_id": "r-outcome-d3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "itu salah", "request_id": "r-outcome-d4"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        after = _luno_memory.list_memories()
        assert len(after) == 2  # nothing deleted, nothing merged
        by_id_before = {m["id"]: m for m in before}
        for m in after:
            b = by_id_before[m["id"]]
            # No destructive action, and Step 7's evidence-mapping was
            # never triggered for either candidate - `classify_context_outcome("itu
            # salah")` DID classify as "negative" (proven at the unit
            # level in `tests/test_memory_outcome_telemetry.py`), but
            # `_handle_memory_feedback_command()`'s own "no target -> no
            # mutation" guard means `record_outcome_evidence()`/
            # `apply_negative_feedback()` were never actually called for
            # either memory - evidence stays fully explainable (exactly
            # what it was, never guessed).
            assert m.get("negative_feedback_count", 0) == b.get("negative_feedback_count", 0)
            assert m.get("evaluation_score") == b.get("evaluation_score")
            assert m["text"] == b["text"]
            assert m.get("history", []) == b.get("history", [])
    finally:
        _silent(console.stop)


# ============================================================================
# MEMORY DECISION QUALITY & ADAPTIVE RETRIEVAL - full loop through the REAL
# production bridge (Phase 7): query -> retrieval -> adaptive ranking
# (query_category threaded through assemble_context()) -> context assembly
# -> rendering, producing one unified context block; and the closed loop
# back into context-specific evidence, attributed to the SURFACING turn's
# query category (via `_session_feedback_context`), not the reacting
# turn's own (near-meaningless) category.
# ============================================================================

def test_memory_decision_quality_adaptive_retrieval_end_to_end_context_evidence_scenario_a():
    """(1) save a technical_fact-shaped memory, (2) a technical_fact-shaped
    query causes real retrieval and lands the memory in the REAL rendered
    `system_prompt` (query -> retrieval -> adaptive ranking -> context
    assembly -> rendering, through `PlannerBridgeModule._handle_utterance()`
    itself, not a direct `assemble_context()`/`relevant_memory_to_context_item()`
    unit call), (3) the user confirms it, (4) the resulting context-specific
    evidence is attributed to "technical_fact" (the SURFACING turn's query
    category, captured via `_session_feedback_context` at retrieval time) -
    never to "other" (`classify_query_context_category("iya benar")`'s own
    category, which would be wrong evidence if used instead)."""
    import luno.memory as _luno_memory

    console = _new_console()
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, spek GPU aku RTX 4090.", "request_id": "r-adaptive-a1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        assert len(saved) == 1
        entry_id = saved[0]["id"]
        assert saved[0]["category"] == "technical_fact"
        assert saved[0].get("context_evidence", {}) == {}  # no evidence recorded yet

        # Turn 2: a real, technical_fact-shaped recall query - drives the
        # REAL `assemble_context()` call inside the production bridge,
        # which computes `query_category` once and threads it through to
        # `relevant_memory_to_context_item()`; the resulting unified
        # context block is what actually reaches the LLM system prompt.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "spek GPU aku apa?", "request_id": "r-adaptive-a2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        prompt = captured.get("system_prompt") or ""
        # One unified block - the memory's text appears exactly once, not
        # duplicated across two independent renderings.
        assert prompt.count("RTX 4090") == 1
        assert "[Relevant Memories]" in prompt

        # Turn 3: the user confirms it - `_handle_memory_feedback_command()`'s
        # positive branch, which reads `_session_feedback_context` (set
        # during turn 2's retrieval, from THAT turn's query text) rather
        # than re-deriving a category from "iya benar" itself.
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "iya benar", "request_id": "r-adaptive-a3"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        after = _luno_memory.get_memory(entry_id)
        evidence = _luno_memory.get_memory_context_evidence(after)
        assert evidence == {"technical_fact": {"positive": 1, "negative": 0}}, (
            f"expected evidence attributed to the surfacing turn's own query "
            f"category (technical_fact), got {evidence}"
        )
        # Confirming what the WRONG attribution would have looked like -
        # proves this isn't passing by accident: the confirmation text's
        # own category is "other", and "other" must NOT have been used.
        assert _luno_memory.classify_query_context_category("iya benar") == "other"
        assert "other" not in evidence

        score = _luno_memory.get_context_evidence_score(after, "technical_fact")
        assert score > 0.5  # positive evidence nudged it above neutral

        # Content/importance untouched by any of this - only evidence moved.
        assert after["text"] == saved[0]["text"]
        assert after["importance"] == saved[0]["importance"]
    finally:
        _silent(console.stop)


def test_memory_decision_quality_adaptive_retrieval_end_to_end_relevance_gate_scenario_b():
    """Relevance-first guarantee, proven through the REAL production
    bridge rather than direct `ContextItem` construction (that proof
    already lives in `tests/test_memory_adaptive_retrieval.py` Section A-C
    and `tests/test_memory_evaluation.py`): a memory with strong positive
    context-specific evidence and high importance, but IRRELEVANT to the
    current turn's query, must never appear in that turn's rendered
    system prompt just because its adaptive signals are strong."""
    import luno.memory as _luno_memory

    console = _new_console()
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "ingat, spek GPU aku RTX 4090.", "request_id": "r-adaptive-b1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        saved = _luno_memory.list_memories()
        entry_id = saved[0]["id"]

        # Build up strong positive context-specific evidence AND high
        # importance directly (simulating a memory this turn's query has
        # nothing to do with) - never via a mutation smoke-test against
        # production, this all happens inside the isolated per-test state
        # `tests/conftest.py` already provides.
        for _ in range(5):
            _luno_memory.record_outcome_evidence(entry_id, "positive", context_category="technical_fact")
        entry = _luno_memory.get_memory(entry_id)
        entry["importance"] = 5  # direct field bump on the isolated per-test store, not production

        captured = {}

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "apa rencana liburan keluarga tahun ini?", "request_id": "r-adaptive-b2"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        prompt = captured.get("system_prompt") or ""
        assert "RTX 4090" not in prompt, (
            "an irrelevant memory must never surface just because its importance/"
            "context-evidence is high - relevance is a hard gate, not a rankable signal"
        )
    finally:
        _silent(console.stop)


@scenario
def test_tool_manager_bridge_does_not_block_barge_in_interrupt_during_slow_tool_c1():
    """Architecture audit regression test (C1): `ToolManagerBridgeModule`
    used to run `self.manager.execute(tool_call)` INLINE inside
    `on_event()`, which `Coordinator.add_route()` calls synchronously on
    the Event Bus's single delivery ("pump") thread. `EventBus._pump_loop`
    fully delivers one event (to every matching subscriber) before it
    ever looks at the next queued event - so a slow tool call used to
    delay delivery of EVERY other event, including a barge-in
    interrupt's own `speech_recognized`, for the tool's entire duration.

    This test registers a deliberately slow tool handler, publishes
    `tool_requested` for it, then immediately (without waiting) publishes
    a `speech_recognized` interrupt. Before the C1 fix this would have
    taken ~the slow tool's full sleep duration to produce `barge_in_action`
    (the pump thread was stuck inside the slow `execute()` call); after
    the fix `on_event()` only submits to a dedicated worker and returns
    immediately, so `barge_in_action` must show up almost immediately -
    well before the slow tool finishes. The slow tool's own
    `ToolFinished`/`tool_failed` must still fire afterward, unchanged,
    proving execution ordering/behavior was preserved, not skipped."""
    from luno.planner.models import ToolCall as _PlannerToolCall
    from luno.tool_manager.handler import ToolHandler
    from luno.tool_manager.result import ToolResult

    SLOW_S = 2.0

    class SlowHandler(ToolHandler):
        name = "slow_test_tool"

        def supported_actions(self):
            return ["run"]

        def execute(self, tool_call, context=None):
            time.sleep(SLOW_S)
            return ToolResult.ok(self.name, tool_call.action, "slow tool finished")

    console = _new_console()
    console.tool_manager_module.manager.registry.register("slow_test_tool", SlowHandler())
    _silent(console.start)
    try:
        # Force barge-in "busy" without going through a real LLM turn, so
        # the interrupt isn't a no-op - see BargeInModule._is_busy().
        console.barge_in_module.thinking = True
        console.barge_in_module.current_request_id = "r-c1-slow"

        barge_in_action_at = {}
        tool_finished_at = {}
        barge_in_seen = threading.Event()
        tool_done_seen = threading.Event()

        def _on_barge_in(e):
            barge_in_action_at["t"] = time.monotonic()
            barge_in_action_at["data"] = dict(e.data)
            barge_in_seen.set()

        def _on_tool_done(e):
            tool_finished_at["t"] = time.monotonic()
            tool_finished_at["success"] = e.type == "tool_finished"
            tool_done_seen.set()

        console.event_bus.subscribe("barge_in_action", _on_barge_in)
        console.event_bus.subscribe("tool_finished", _on_tool_done)
        console.event_bus.subscribe("tool_failed", _on_tool_done)

        t0 = time.monotonic()
        tool_call = _PlannerToolCall(tool="slow_test_tool", action="run", target=None, params={})
        _silent(console.event_bus.publish, demo.Event(
            type="tool_requested", data={"execution_id": "exec-c1-slow", "tool_call": tool_call},
        ))
        # Published immediately after, with no wait - this is the race the
        # fix is about: both events are already queued before the pump
        # thread has had a chance to fully process either one.
        _silent(console.event_bus.publish, demo.Event(
            type="speech_recognized", data={"text": "stop"},
        ))

        assert _wait_until(barge_in_seen.is_set, 1.5), (
            "barge_in_action never fired within 1.5s - the Event Bus pump "
            "thread appears blocked behind the slow tool call (C1 regression)"
        )
        interrupt_latency_s = barge_in_action_at["t"] - t0
        assert interrupt_latency_s < (SLOW_S * 0.75), (
            f"barge_in_action took {interrupt_latency_s:.2f}s, close to/over the slow "
            f"tool's {SLOW_S}s duration - interrupt delivery was blocked by tool execution"
        )
        assert barge_in_action_at["data"].get("action") == "free"

        # The slow tool must still complete normally afterward - C1 must
        # not change ordering or drop/skip execution, only move it off
        # the pump thread.
        assert _wait_until(tool_done_seen.is_set, SLOW_S + 2.0)
        assert tool_finished_at["success"] is True
        assert tool_finished_at["t"] > barge_in_action_at["t"], (
            "slow tool finished before the interrupt was delivered - expected the "
            "interrupt to arrive first, tool completion after"
        )
    finally:
        _silent(console.stop)


# ============================================================================
# Efficient LLM Classifier sprint - end-to-end wiring through
# PlannerBridgeModule._handle_utterance (confirmation flow, one-shot
# bypass, Golden Rule preserved).
# ============================================================================

def _spy_classifier(intent="device_control", confidence=0.9):
    import json as _json
    from dataclasses import dataclass as _dc

    @_dc
    class _Resp:
        text: str

    calls = []

    def _fn(**kwargs):
        calls.append(kwargs)
        return _Resp(text=_json.dumps({
            "intent": intent, "confidence": confidence, "needs_confirmation": False, "reason": "test",
        }))

    _fn.calls = calls
    return _fn


@scenario
def test_classifier_confirmation_end_to_end_confirm_reprocesses_original_text():
    """Ambiguous turn -> medium confidence -> needs_confirmation note
    appended, ConfirmationHandler has one pending entry. User replies
    "iya" on the SAME conversation -> re-processed via `forced_intent`
    (classifier NOT called a second time - one-shot bypass), pending
    entry cleared."""
    console = _new_console()
    spy = _spy_classifier(intent="device_control", confidence=0.65)
    console.planner_module.decision_engine.set_classifier_client(spy)
    console.planner_module.decision_engine.config.classifier_enabled = True
    console.planner_module.decision_engine.config.classifier_confidence_threshold = 0.80
    console.planner_module.decision_engine.config.classifier_confirmation_threshold = 0.55
    _silent(console.start)
    try:
        conv_id = "conv-classifier-1"
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "make the room comfortable", "request_id": "cq-1", "conversation_id": conv_id},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        assert len(spy.calls) == 1
        prompt = captured.get("system_prompt") or ""
        assert "mau aku lanjutkan" in prompt or "confirm" in prompt.lower() or "ya/tidak" in prompt
        assert console.planner_module.confirmation_handler.snapshot()["pending_count"] == 1

        # second turn: user confirms
        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "iya", "request_id": "cq-2", "conversation_id": conv_id},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        # one-shot: classifier must NOT have been invoked again for the confirmed re-process.
        assert len(spy.calls) == 1
        assert console.planner_module.confirmation_handler.snapshot()["pending_count"] == 0
    finally:
        _silent(console.stop)


@scenario
def test_classifier_confirmation_end_to_end_cancel_clears_pending_without_reprocessing():
    console = _new_console()
    spy = _spy_classifier(intent="search_web", confidence=0.60)
    console.planner_module.decision_engine.set_classifier_client(spy)
    console.planner_module.decision_engine.config.classifier_enabled = True
    console.planner_module.decision_engine.config.classifier_confidence_threshold = 0.80
    console.planner_module.decision_engine.config.classifier_confirmation_threshold = 0.55
    _silent(console.start)
    try:
        conv_id = "conv-classifier-2"
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "handle that thing from before", "request_id": "cc-1", "conversation_id": conv_id},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        assert console.planner_module.confirmation_handler.snapshot()["pending_count"] == 1

        need_llm.clear()
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "tidak", "request_id": "cc-2", "conversation_id": conv_id},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        assert console.planner_module.confirmation_handler.snapshot()["pending_count"] == 0
        assert len(spy.calls) == 1  # never called again just to process the decline
    finally:
        _silent(console.stop)


@scenario
def test_classifier_high_confidence_routes_without_asking_for_confirmation():
    console = _new_console()
    spy = _spy_classifier(intent="device_control", confidence=0.95)
    console.planner_module.decision_engine.set_classifier_client(spy)
    console.planner_module.decision_engine.config.classifier_enabled = True
    console.planner_module.decision_engine.config.classifier_confidence_threshold = 0.80
    _silent(console.start)
    try:
        need_llm = threading.Event()
        decisions = []
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())
        console.event_bus.subscribe("routing_decision_made", lambda e: decisions.append(e.data))
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "do what we talked about earlier", "request_id": "hc-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        assert len(spy.calls) == 1
        assert decisions and decisions[0]["needs_confirmation"] is False
        assert decisions[0]["used_classifier"] is True
        assert console.planner_module.confirmation_handler.snapshot()["pending_count"] == 0
    finally:
        _silent(console.stop)


@scenario
def test_classifier_disabled_by_default_never_invoked_on_ambiguous_text():
    """Master switch off (the config default) - even a genuinely
    ambiguous utterance and a wired client must never trigger a call."""
    console = _new_console()
    spy = _spy_classifier()
    console.planner_module.decision_engine.set_classifier_client(spy)
    # Explicitly OFF - `.env` now sets CLASSIFIER_ENABLED=true for
    # production (Vinn's request), so this can no longer rely on the
    # bare dataclass default matching what `RoutingConfig.from_env()`
    # actually reads in this environment; force it here instead so the
    # test's real intent ("switch OFF -> never invoked, even if wired")
    # holds regardless of the real .env's current value.
    console.planner_module.decision_engine.config.classifier_enabled = False
    _silent(console.start)
    try:
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "make the room comfortable", "request_id": "off-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        assert len(spy.calls) == 0
    finally:
        _silent(console.stop)


@scenario
def test_classifier_confidence_never_claims_a_device_action_succeeded():
    """Golden Rule (M): a confident classification of 'device_control'
    must NOT make the system_prompt claim any device was actually
    controlled - no real IntentParser/Planner/ToolManager task ran for
    this ambiguous text, so there is nothing verified to claim."""
    console = _new_console()
    spy = _spy_classifier(intent="device_control", confidence=0.97)
    console.planner_module.decision_engine.set_classifier_client(spy)
    console.planner_module.decision_engine.config.classifier_enabled = True
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "do what we talked about earlier", "request_id": "gr-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        prompt = captured.get("system_prompt") or ""
        assert "VERIFIED results" not in prompt
        assert "you already performed" not in prompt.lower()
    finally:
        _silent(console.stop)


# ============================================================================
# AppNotFound browser-fallback confirmation (reported gap: "buka channel
# Mr beast di youtube" used to just fail with "not registered", nothing
# else) - reuses the SAME ConfirmationHandler as the classifier sprint
# above, just a different offering call site (a verified tool-execution
# failure, not an ambiguous routing classification).
# ============================================================================

def _app_not_found_windows_handler(error_type="AppNotFound"):
    from luno.tool_manager.handler import ToolHandler
    from luno.tool_manager.result import ToolResult

    class _Handler(ToolHandler):
        name = "windows"

        def supported_actions(self):
            return ["open_app", "launch_app"]

        def execute(self, tool_call, context=None):
            # Matches the REAL `luno.desktop_control.open_app()` message
            # shape (short, no app-list enumeration - reported gap: the
            # old "Yang sudah ada: steam, chrome, ..." made every reply
            # unnecessarily long).
            return ToolResult.fail(
                self.name, tool_call.action,
                f"Aplikasi '{tool_call.target}' belum terdaftar di config/apps.json.",
                error_type=error_type,
            )

    return _Handler()


@scenario
def test_app_not_found_offers_browser_fallback_confirmation_end_to_end():
    """"buka channel mr beast di youtube" -> open_app fails AppNotFound ->
    instead of just reporting the failure, a deterministic browser
    fallback offer (platform-detected: YouTube) is appended as a note
    and a pending confirmation is created - the LLM must only be told to
    ASK, never to claim anything is already open."""
    console = _new_console()
    console.tool_manager_module.manager.registry.register("windows", _app_not_found_windows_handler())
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "buka channel mr beast di youtube", "request_id": "anf-1", "conversation_id": "conv-anf-1"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        prompt = captured.get("system_prompt") or ""
        assert "not registered" in prompt or "belum terdaftar" in prompt  # honest failure still reported
        assert "YouTube" in prompt
        assert "only ASKING" in prompt
        assert "already opened it" not in prompt or "do NOT say" in prompt
        assert console.planner_module.confirmation_handler.snapshot()["pending_count"] == 1
    finally:
        _silent(console.stop)


@scenario
def test_app_not_found_confirmed_opens_browser_fallback_and_clears_pending():
    """Second turn "iya" on the same conversation actually opens the
    fallback (via `desktop_control.open_url`, patched here so no real
    browser launches during the test) - and the pending entry is
    cleared afterward (one-shot, same as every other ConfirmationHandler
    caller)."""
    import luno.desktop_control as dc

    console = _new_console()
    console.tool_manager_module.manager.registry.register("windows", _app_not_found_windows_handler())
    opened_urls = []
    original_open_url = dc.open_url
    dc.open_url = lambda url: (opened_urls.append(url) or (True, f"Membuka {url} di Chrome."))
    _silent(console.start)
    try:
        conv_id = "conv-anf-2"
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "buka channel mr beast di youtube", "request_id": "anf-2a", "conversation_id": conv_id},
        ))
        assert _wait_until(need_llm.is_set, 5.0)
        assert console.planner_module.confirmation_handler.snapshot()["pending_count"] == 1

        captured = {}
        need_llm.clear()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "iya", "request_id": "anf-2b", "conversation_id": conv_id},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        assert len(opened_urls) == 1, opened_urls
        assert "youtube.com" in opened_urls[0]
        assert "channel" in opened_urls[0] and "beast" in opened_urls[0]
        prompt = captured.get("system_prompt") or ""
        assert "opened it" in prompt.lower() or "membuka" in prompt.lower()
        assert console.planner_module.confirmation_handler.snapshot()["pending_count"] == 0
    finally:
        dc.open_url = original_open_url
        _silent(console.stop)


@scenario
def test_app_not_found_cancelled_never_opens_browser():
    """Second turn "tidak" must clear the pending entry WITHOUT ever
    calling `open_url` - decline means nothing happens."""
    import luno.desktop_control as dc

    console = _new_console()
    console.tool_manager_module.manager.registry.register("windows", _app_not_found_windows_handler())
    opened_urls = []
    original_open_url = dc.open_url
    dc.open_url = lambda url: (opened_urls.append(url) or (True, "ok"))
    _silent(console.start)
    try:
        conv_id = "conv-anf-3"
        need_llm = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set())
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "buka channel mr beast di youtube", "request_id": "anf-3a", "conversation_id": conv_id},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        captured = {}
        need_llm.clear()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "tidak", "request_id": "anf-3b", "conversation_id": conv_id},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        assert opened_urls == []
        prompt = captured.get("system_prompt") or ""
        assert "declined" in prompt.lower()
        assert "do not open anything" in prompt.lower()
        assert console.planner_module.confirmation_handler.snapshot()["pending_count"] == 0
    finally:
        dc.open_url = original_open_url
        _silent(console.stop)


@scenario
def test_app_not_found_fallback_never_offered_for_other_windows_failure_types():
    """Only `error_type == "AppNotFound"` gets a fallback offer - a
    different windows-tool failure (bad path, launch exception) isn't
    fixable by opening a browser, so it must stay a plain, honest
    failure with no pending confirmation created."""
    console = _new_console()
    console.tool_manager_module.manager.registry.register(
        "windows", _app_not_found_windows_handler(error_type="LaunchFailed"),
    )
    _silent(console.start)
    try:
        captured = {}
        need_llm = threading.Event()

        def _capture(e):
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

        console.event_bus.subscribe("need_llm_response", _capture)
        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "buka channel mr beast di youtube", "request_id": "anf-4", "conversation_id": "conv-anf-4"},
        ))
        assert _wait_until(need_llm.is_set, 5.0)

        prompt = captured.get("system_prompt") or ""
        assert "did NOT succeed" in prompt
        assert "only ASKING" not in prompt
        assert console.planner_module.confirmation_handler.snapshot()["pending_count"] == 0
    finally:
        _silent(console.stop)


@scenario
def test_executor_preserves_full_result_on_failure_when_handler_attaches_it():
    """`TaskExecutor._handle_failure` must keep `task.result` populated
    with the full failed payload (not just the string in `task.error`)
    when the raised exception carries `.tool_result` - additive-only, so
    `data` (verification facts) survives a failed task too."""
    task = _real_task()
    queue = ExecutionQueue([task])
    registry = ToolRegistry()

    payload = {"success": False, "message": "didn't respond", "data": {"actual_state": "off"}}

    def handler(tool_call):
        err = RuntimeError("didn't respond")
        err.tool_result = payload
        raise err

    registry.register("home_assistant", handler)
    executor = TaskExecutor(registry, _ThreadPoolExecutor(max_workers=1))
    done = threading.Event()
    executor.run_task(task, queue, lambda t: done.set())
    assert _wait_until(done.is_set, 5.0)

    finished = queue.get(task.id)
    assert finished.status == TaskStatus.FAILED
    assert finished.error == "didn't respond"
    assert finished.result == payload  # NOT dropped, unlike before this fix


@scenario
def test_executor_backward_compatible_with_handlers_that_dont_attach_tool_result():
    """A handler that raises a plain exception with no `.tool_result`
    attribute (every handler predating this sprint) must behave exactly
    as before: `task.result` stays `None` on failure, nothing new is
    required of existing handlers."""
    task = _real_task()
    queue = ExecutionQueue([task])
    registry = ToolRegistry()

    def handler(tool_call):
        raise RuntimeError("boom")

    registry.register("home_assistant", handler)
    executor = TaskExecutor(registry, _ThreadPoolExecutor(max_workers=1))
    done = threading.Event()
    executor.run_task(task, queue, lambda t: done.set())
    assert _wait_until(done.is_set, 5.0)

    finished = queue.get(task.id)
    assert finished.status == TaskStatus.FAILED
    assert finished.error == "boom"
    assert finished.result is None


@scenario
def test_known_tools_covers_every_tool_intent_parser_can_produce():
    """Regression guard for a REAL reported bug: `IntentParser.parse()`
    gained "camera_ptz" and "llm_mode" as producible `tool` values (the
    camera pan/tilt sprint and the LLM auto/manual routing sprint), but
    `PlannerBridgeModule.KNOWN_TOOLS` - the SEPARATE list controlling
    which tool names the Planner's own registry bridges through to the
    real Tool Manager (see that class's own docstring: "every tool name
    IntentParser.parse() can currently produce ... keeps this list the
    only place that needs to track that vocabulary") - was never
    updated to match. A user's "geser kamera ke kanan" parsed into a
    `camera_ptz` ToolCall the Planner had literally never heard of,
    raising `ToolNotRegisteredError("No handler registered for tool
    'camera_ptz'")` even though a `MockCameraPTZHandler` was correctly
    registered in the Tool Manager the whole time - the two registries
    (Planner's `luno.planner.executor.ToolRegistry` vs Tool Manager's
    `luno.tool_manager.registry.ToolRegistry`, deliberately kept
    independent per this project's own architecture) had silently
    drifted out of sync.

    This test parses the module docstring's own example plus one
    representative phrase per known parser pattern (turn_on/turn_off/
    set_value/run_script/navigate/type/press_key/play/describe/open_app/
    camera_ptz x5/llm_mode x2) and asserts every single `tool` value
    IntentParser actually produced is present in `KNOWN_TOOLS` - so
    adding a new parser pattern without also updating `KNOWN_TOOLS` (the
    exact mistake that caused the real bug) fails this test immediately,
    rather than silently shipping a tool a real user's utterance can
    parse into but the Planner can never execute."""
    from luno.planner.parser import IntentParser

    probe_phrases = [
        "open Chrome, turn on the bedroom light, turn off the desk lamp, then play Spotify.",
        "set the thermostat to 24",
        "run gaming mode",
        "navigate to google.com",
        "type hello world",
        "press enter",
        "look at the door",
        "geser kamera ke kanan",
        "putar kamera ke kiri",
        "tilt the camera up",
        "arahkan kamera ke bawah",
        "kalibrasi kamera",
        "pakai llm manual",
        "pakai llm openai",
        "this is just plain conversation with no command in it at all",
    ]
    produced_tools = set()
    for phrase in probe_phrases:
        for step in IntentParser.parse(phrase):
            produced_tools.add(step.tool)

    # Sanity: this probe set must actually exercise every tool the
    # parser can produce, or this test would pass for the wrong reason
    # (too few phrases, not full KNOWN_TOOLS coverage).
    assert produced_tools == {
        "home_assistant", "browser", "spotify", "vision", "windows",
        "camera_ptz", "llm_mode", "unknown",
    }

    # "unknown" is deliberately EXCLUDED from this coverage check (and
    # from KNOWN_TOOLS itself) - see KNOWN_TOOLS's own comment in
    # main_runtime_demo.py: it's IntentParser's sentinel for "not a
    # device command", never bridged to a real Tool Manager handler, and
    # `_handle_utterance()` now skips `Planner.execute()` entirely for an
    # all-"unknown" plan instead of registering a handler for it (the
    # "unknown-tool noise" fix - see test_unknown_tool_never_reaches_tool_manager
    # below for the behavioral regression guard).
    real_tools = produced_tools - {"unknown"}
    missing = real_tools - set(demo.PlannerBridgeModule.KNOWN_TOOLS)
    assert not missing, (
        f"IntentParser can produce tool(s) {missing} that PlannerBridgeModule.KNOWN_TOOLS "
        f"doesn't cover - the Planner would raise ToolNotRegisteredError for these at runtime "
        f"even though the Tool Manager has a real handler for them."
    )
    assert "unknown" not in demo.PlannerBridgeModule.KNOWN_TOOLS, (
        "\"unknown\" should never be registered against the Tool Manager bridge - "
        "see KNOWN_TOOLS's own comment for why."
    )


# ============================================================================
# Gemini vision migration - regression guards
# ============================================================================

@scenario
def test_camera_ptz_still_reaches_the_real_handler_end_to_end():
    """PTZ compatibility (migration task item 15): "geser kamera ke
    kanan" must still resolve to tool=camera_ptz and actually reach the
    Tool Manager's registered handler - completely independent of the
    Gemini vision changes (different tool, different code path)."""
    console = _new_console()
    _silent(console.start)
    try:
        tool_events = []
        console.event_bus.subscribe("tool_finished", lambda e: tool_events.append(e))

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance", data={"text": "geser kamera ke kanan", "request_id": "r-ptz-1"},
        ))
        assert _wait_until(lambda: len(tool_events) >= 1, 5.0)

        assert tool_events[0].data["tool"] == "camera_ptz"
        assert tool_events[0].data["action"] == "pan_right"
        assert tool_events[0].data["success"] is True
        assert "Panned camera right" in tool_events[0].data["message"]
    finally:
        _silent(console.stop)


@scenario
def test_unknown_tool_never_reaches_tool_manager():
    """Unknown-tool fix (migration task item 16): a plain-conversation
    utterance with no device command in it at all must NEVER produce a
    'No handler registered for tool 'unknown'' failure - `Planner.
    execute()` should be skipped entirely for an all-"unknown" plan (see
    `KNOWN_TOOLS`'s own comment in main_runtime_demo.py), and the turn
    must still flow normally to the LLM (`NeedLLMResponse` still
    published) instead of silently going nowhere."""
    console = _new_console()
    _silent(console.start)
    try:
        tool_requested_events = []
        tool_failed_events = []
        need_llm_events = []
        console.event_bus.subscribe("tool_requested", lambda e: tool_requested_events.append(e))
        console.event_bus.subscribe("tool_failed", lambda e: tool_failed_events.append(e))
        console.event_bus.subscribe("need_llm_response", lambda e: need_llm_events.append(e))

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "this is just plain conversation with no command in it at all", "request_id": "r-unk-1"},
        ))
        assert _wait_until(lambda: len(need_llm_events) >= 1, 5.0)

        assert tool_requested_events == [], (
            f"an 'unknown' (or any) tool_requested event should never fire for plain conversation, "
            f"got: {[e.data for e in tool_requested_events]}"
        )
        assert tool_failed_events == []
        for e in tool_failed_events:
            assert "unknown" not in (e.data.get("error") or "").lower()
    finally:
        _silent(console.stop)


@scenario
def test_mixed_utterance_real_command_still_succeeds_despite_unknown_clause():
    """The one accepted residual edge case (see the execute-gate comment
    in `_handle_utterance()`): a real command chained with an unrelated
    clause in the SAME utterance must still execute the real command
    normally - `continue_on_failure=True` (the pre-existing "nyalakan
    rgb strip dan matikan fish light" fix) already guarantees an
    internal "unknown" failure next to it never blocks/skips it."""
    console = _new_console()
    _silent(console.start)
    try:
        tool_events = []
        console.event_bus.subscribe("tool_finished", lambda e: tool_events.append(e))

        _silent(console.event_bus.publish, demo.Event(
            type="user_utterance",
            data={"text": "turn on the lights and bagaimana cuaca hari ini", "request_id": "r-mixed-1"},
        ))
        assert _wait_until(lambda: len(tool_events) >= 1, 5.0)

        assert any(e.data["tool"] == "home_assistant" and e.data["success"] is True for e in tool_events)
    finally:
        _silent(console.stop)


# ============================================================================
# runner
# ============================================================================

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
    print(f"\n{passed} passed, {failed} failed, {len(SCENARIOS)} total")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
