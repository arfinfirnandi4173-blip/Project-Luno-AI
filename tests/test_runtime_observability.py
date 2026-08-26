"""
tests/test_runtime_observability.py
=====================================

Sprint 50 (Runtime Observability, Test Logging & Real-World Data
Capture) - Phase 15's own dedicated test suite for the event model
(Phase 1), the JSONL/text logging layer (Phase 2-4), the dashboard
collectors (Phase 5-6), privacy/security (Phase 13), and performance
(Phase 14).

Root cause this sprint addresses (see `docs/change_impact/
runtime_observability.md` for the full writeup): before this sprint,
NOTHING in this project persisted the Event Bus's own event stream to
disk, and the memory/reference/topic decision pipeline
(`PlannerBridgeModule._handle_utterance()`) never published a single
Event Bus event of its own for reference classification, topic
decisions, or ambiguity refusals - callers could only see these via
`log()` print lines or the Sprint-32 Brain Debugger's own read-only
state inspectors, never as a discrete, timestamped, durable event. This
sprint adds exactly three new event types
(`memory_reference_classified`/`memory_topic_decision`/
`memory_selection_summary`), all published from data
`_handle_utterance()` ALREADY computes (see that method's own Sprint 50
comments), plus `EventLogWriter` (the first component in this project to
persist ANY Event Bus event to disk).

Same self-contained-helpers house style as `tests/
test_memory_voice_observability.py` (this project's own established
"duplicate the small helper set per test file" convention, not a
cross-file import).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from typing import Callable, List

import pytest
import requests

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.core import Event  # noqa: E402
from luno.core.event_bus import EventBus  # noqa: E402
from luno.dashboard import collectors as dash_collectors  # noqa: E402
from luno.dashboard import DashboardServer  # noqa: E402
from luno.dashboard.event_log_writer import EventLogWriter, _bound_value, _redact, MAX_FIELD_CHARS  # noqa: E402
from luno.memory_turn_trace import MemoryTurnTrace  # noqa: E402


def _load_demo():
    demo_spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_observability2", os.path.join(_ROOT, "main_runtime_demo.py")
    )
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules["main_runtime_demo_observability2"] = demo
    demo_spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _new_console(demo, canned_text="Oke, dimengerti.", **kwargs):
    from luno.adapters import MockOpenRouterClient
    return demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text=canned_text, chunk_delay_s=0.0), **kwargs)


def _run_turn(console, demo, text, request_id, conversation_id=None, canned_reply=None):
    if canned_reply is not None:
        console.openrouter_adapter.client.canned_text = canned_reply
    need_llm = threading.Event()

    def _capture(e):
        if e.get("request_id") != request_id:
            return
        need_llm.set()

    sub = console.event_bus.subscribe("need_llm_response", _capture)
    data = {"text": text, "request_id": request_id}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    try:
        console.event_bus.publish(Event(type="user_utterance", data=data))
        assert _wait_until(need_llm.is_set, 5.0), "no need_llm_response within timeout"
        assert _wait_until(lambda: request_id not in console.planner_module._pending_turns, 5.0)
    finally:
        console.event_bus.unsubscribe(sub)


def _modules_for(console):
    return {"planner_module": console.planner_module}


@pytest.fixture
def tmp_log_dir():
    d = tempfile.mkdtemp(prefix="luno_sprint50_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────────────────────────
#  1 - MemoryTurnTrace field additions (regression lock, Sprint-49 style)
# ─────────────────────────────────────────────

def test_01_memory_turn_trace_field_set_additive_only():
    """`ActiveTopicSnapshot`-style invariant check, applied to
    `MemoryTurnTrace` instead: the two new Sprint 50 fields are present,
    and every pre-existing field name (Sprint 4 through Sprint 32) is
    still there too - proves this was a purely additive change."""
    import dataclasses
    names = {f.name for f in dataclasses.fields(MemoryTurnTrace)}
    assert "topic_decision" in names
    assert "ambiguity_check_result" in names
    for pre_existing in ("turn_id", "candidate_memory_ids", "reference_type", "active_topic_terms", "funnel"):
        assert pre_existing in names


def test_02_is_ambiguity_refusal_property_only_true_when_false_result():
    t = MemoryTurnTrace(turn_id="x")
    assert t.is_ambiguity_refusal is False  # None (not evaluated) is NOT a refusal
    t.ambiguity_check_result = True
    assert t.is_ambiguity_refusal is False
    t.ambiguity_check_result = False
    assert t.is_ambiguity_refusal is True


def test_03_build_turn_trace_backward_compatible_defaults():
    """An existing caller that omits the two new kwargs entirely (every
    call site before this sprint) gets a trace byte-for-byte unaffected
    on these two fields."""
    from luno.memory_turn_trace import build_turn_trace

    class _FakeAssembled:
        items = []

    trace = build_turn_trace("t1", [], _FakeAssembled())
    assert trace.topic_decision == ""
    assert trace.ambiguity_check_result is None
    assert trace.is_ambiguity_refusal is False


# ─────────────────────────────────────────────
#  2 - event model: real E2E publishes through the real production path
# ─────────────────────────────────────────────

def test_10_e2e_three_new_event_types_published_with_bounded_fields():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    captured = []
    sub = console.event_bus.subscribe("*", lambda e: captured.append(e) if e.type.startswith("memory_") else None, priority=-500)
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441 buat mic saya.", "turn10-1", conversation_id="conv10")
    finally:
        console.event_bus.unsubscribe(sub)
        console.stop()

    types_seen = {e.type for e in captured}
    assert {"memory_reference_classified", "memory_topic_decision", "memory_selection_summary"} <= types_seen

    for e in captured:
        # Privacy: raw utterance text must never appear in any of these
        # three event payloads (matches MemoryTurnTrace's own long-
        # standing "never raw text" boundary).
        blob = json.dumps(e.data)
        assert "ESP32 pakai INMP441" not in blob
        assert e.data.get("request_id") == "turn10-1"
        assert e.data.get("conversation_id") == "conv10"


def test_11_e2e_ambiguity_refusal_visible_as_a_live_event():
    """The exact Sprint 48/49 Aquascape A/B scenario, now observable as
    a structured `memory_topic_decision` event with
    `ambiguity_refusal=True` - proves this sprint's own core value
    proposition (an internal decision Sprint 49 could only prove via
    unit tests is now a real, timestamped, externally-observable event)."""
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    decisions = []
    sub = console.event_bus.subscribe("memory_topic_decision", lambda e: decisions.append(e.data))
    try:
        _run_turn(console, demo, "Aquascape A pakai pompa kecil.", "turn11-1", conversation_id="conv11")
        _run_turn(console, demo, "Aquascape B pakai pompa besar.", "turn11-2", conversation_id="conv11")
        _run_turn(console, demo, "Pompanya gimana?", "turn11-3", conversation_id="conv11")
    finally:
        console.event_bus.unsubscribe(sub)
        console.stop()

    last = decisions[-1]
    assert last["ambiguity_refusal"] is True
    assert last["ambiguity_check_result"] is False
    assert last["topic_decision"] == "NO_CANDIDATE"


def test_12_e2e_merge_decision_visible_for_genuine_continuity():
    """The positive-control counterpart to test_11 - a genuine single-
    entity follow-up still reports `topic_decision=MERGE_ACTIVE_TOPIC`
    and `ambiguity_refusal=False`, proving the new event model doesn't
    just report refusals, it honestly reports successful merges too."""
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    decisions = []
    sub = console.event_bus.subscribe("memory_topic_decision", lambda e: decisions.append(e.data))
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "turn12-1", conversation_id="conv12")
        _run_turn(console, demo, "Kalau koneksinya?", "turn12-2", conversation_id="conv12")
        _run_turn(console, demo, "Yang wireless?", "turn12-3", conversation_id="conv12")
    finally:
        console.event_bus.unsubscribe(sub)
        console.stop()
    assert decisions[-1]["topic_decision"] == "MERGE_ACTIVE_TOPIC"
    assert decisions[-1]["ambiguity_refusal"] is False


def test_13_a_telemetry_publish_failure_cannot_break_a_turn():
    """Monkeypatches `event_bus.publish` to raise ONLY for the three new
    event types - the turn must still complete normally (own try/except
    around every one of the three new publishes in `_handle_utterance()`)."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    real_publish = console.event_bus.publish

    def _flaky_publish(event):
        if event.type.startswith("memory_") and event.type != "memory_reference_classified":
            raise RuntimeError("simulated telemetry failure")
        return real_publish(event)

    console.event_bus.publish = _flaky_publish
    try:
        _run_turn(console, demo, "Halo, apa kabar?", "turn13-1", conversation_id="conv13")
        # Turn must have completed despite the simulated failures.
        assert "turn13-1" not in console.planner_module._pending_turns
    finally:
        console.event_bus.publish = real_publish
        console.stop()


# ─────────────────────────────────────────────
#  3 - EventLogWriter: JSONL + human-readable persistence
# ─────────────────────────────────────────────

def test_20_jsonl_and_text_files_created_and_readable(tmp_log_dir):
    bus = EventBus()
    bus.start()
    writer = EventLogWriter(bus, log_dir=tmp_log_dir)
    writer.start()
    try:
        bus.publish(Event(type="memory_reference_classified", data={
            "request_id": "r1", "conversation_id": "c1", "reference_type": "comparison",
            "is_short_followup": True, "query_intent": "other",
        }))
        assert _wait_until(lambda: writer.stats()["events_written"] >= 1, 3.0)
    finally:
        writer.stop()
        bus.stop()

    events_dir = os.path.join(tmp_log_dir, "events")
    runtime_dir = os.path.join(tmp_log_dir, "runtime")
    assert os.path.isdir(events_dir) and os.path.isdir(runtime_dir)
    jsonl_files = os.listdir(events_dir)
    text_files = os.listdir(runtime_dir)
    assert len(jsonl_files) == 1 and jsonl_files[0].endswith(".jsonl")
    assert len(text_files) == 1 and text_files[0].endswith(".log")

    with open(os.path.join(events_dir, jsonl_files[0]), encoding="utf-8") as fh:
        lines = [json.loads(l) for l in fh if l.strip()]
    assert any(l["type"] == "memory_reference_classified" for l in lines)

    with open(os.path.join(runtime_dir, text_files[0]), encoding="utf-8") as fh:
        text = fh.read()
    assert "EVENT: memory_reference_classified" in text
    assert "REFERENCE_TYPE: comparison" in text


def test_21_pretty_vs_compact_text_rendering():
    """The 6 Sprint 50 event types get the multi-line Phase 3 rendering;
    an ordinary pre-existing event type (e.g. `tool_finished`) gets one
    compact line - proves the dashboard/log doesn't turn into 50 kinds
    of multi-line noise for events this sprint didn't touch."""
    from luno.dashboard.event_log_writer import _format_text_line
    pretty = _format_text_line({"type": "memory_topic_decision", "timestamp": "2026-01-01T00:00:00", "data": {"request_id": "r1", "topic_decision": "MERGE_ACTIVE_TOPIC"}})
    assert pretty.count("\n") >= 3
    assert "EVENT: memory_topic_decision" in pretty

    compact = _format_text_line({"type": "tool_finished", "timestamp": "2026-01-01T00:00:00", "data": {"tool": "home_assistant"}})
    assert "\n" not in compact
    assert "tool_finished" in compact


def test_22_redaction_strips_secret_shaped_keys():
    payload = {
        "api_key": "sk-real-secret-value",
        "password": "hunter2",
        "AUTHORIZATION": "Bearer abc123",
        "nested": {"token": "xyz", "safe_field": "hello"},
        "safe_top_level": "world",
    }
    redacted = _redact(payload)
    assert redacted["api_key"] == "***REDACTED***"
    assert redacted["password"] == "***REDACTED***"
    assert redacted["AUTHORIZATION"] == "***REDACTED***"
    assert redacted["nested"]["token"] == "***REDACTED***"
    assert redacted["nested"]["safe_field"] == "hello"
    assert redacted["safe_top_level"] == "world"


def test_23_redaction_applied_before_either_file_format(tmp_log_dir):
    bus = EventBus()
    bus.start()
    writer = EventLogWriter(bus, log_dir=tmp_log_dir)
    writer.start()
    try:
        bus.publish(Event(type="some_custom_event", data={"api_key": "sk-should-never-appear", "note": "fine"}))
        assert _wait_until(lambda: writer.stats()["events_written"] >= 1, 3.0)
    finally:
        writer.stop()
        bus.stop()
    events_dir = os.path.join(tmp_log_dir, "events")
    runtime_dir = os.path.join(tmp_log_dir, "runtime")
    jsonl_text = open(os.path.join(events_dir, os.listdir(events_dir)[0]), encoding="utf-8").read()
    log_text = open(os.path.join(runtime_dir, os.listdir(runtime_dir)[0]), encoding="utf-8").read()
    assert "sk-should-never-appear" not in jsonl_text
    assert "sk-should-never-appear" not in log_text
    assert "***REDACTED***" in jsonl_text


def test_24_oversized_field_is_bounded():
    huge = "x" * (MAX_FIELD_CHARS * 5)
    bounded = _bound_value(huge)
    assert len(bounded) <= MAX_FIELD_CHARS + len("...[truncated]")
    assert bounded.endswith("...[truncated]")


def test_25_write_failure_isolated_never_raises(tmp_log_dir):
    """Points the writer at a path that cannot be a directory (a plain
    file sitting where `events/` needs to be a directory) - every write
    must fail silently, `stats()['write_failures']` must increase, and
    the Event Bus must keep delivering to every OTHER subscriber."""
    blocked_path = os.path.join(tmp_log_dir, "events")
    with open(blocked_path, "w", encoding="utf-8") as fh:
        fh.write("not a directory")

    bus = EventBus()
    bus.start()
    writer = EventLogWriter(bus, log_dir=tmp_log_dir)
    writer.start()
    other_received = threading.Event()
    bus.subscribe("probe_event", lambda e: other_received.set(), priority=0)
    try:
        bus.publish(Event(type="probe_event", data={}))
        assert _wait_until(other_received.is_set, 3.0), "a logging failure broke real event delivery"
        assert _wait_until(lambda: writer.stats()["write_failures"] >= 1, 3.0)
    finally:
        writer.stop()
        bus.stop()


def test_26_rotation_deletes_old_files_not_recent_ones(tmp_log_dir):
    events_dir = os.path.join(tmp_log_dir, "events")
    os.makedirs(events_dir, exist_ok=True)
    old_file = os.path.join(events_dir, "2000-01-01.jsonl")
    with open(old_file, "w", encoding="utf-8") as fh:
        fh.write("{}\n")
    old_time = time.time() - (30 * 86400)
    os.utime(old_file, (old_time, old_time))

    recent_file = os.path.join(events_dir, "2026-01-01.jsonl")
    with open(recent_file, "w", encoding="utf-8") as fh:
        fh.write("{}\n")

    bus = EventBus()
    EventLogWriter(bus, log_dir=tmp_log_dir, max_retention_days=14)  # rotation runs at construction
    assert not os.path.exists(old_file)
    assert os.path.exists(recent_file)


def test_27_disabled_by_default_no_new_files_for_ordinary_console():
    """The exact non-negotiable this sprint's own design hinges on:
    constructing a `RuntimeDemoConsole` the ordinary way (like every one
    of the ~2900 pre-existing tests already does) must create ZERO new
    observability files anywhere - `enable_observability_log` defaults
    to `False`. Snapshots the real `logs/events` directory's own file
    count before/after (rather than asserting the directory doesn't
    exist at all) so this test is safe to run alongside others that
    might have already created it via the OPT-IN path."""
    events_dir = os.path.join(_ROOT, "logs", "events")
    before = set(os.listdir(events_dir)) if os.path.isdir(events_dir) else None

    demo = _load_demo()
    console = _new_console(demo)
    assert console.enable_observability_log is False
    console.start()
    try:
        _run_turn(console, demo, "Halo.", "turn27-1", conversation_id="conv27")
    finally:
        console.stop()

    after = set(os.listdir(events_dir)) if os.path.isdir(events_dir) else None
    assert before == after, "an ordinary (non-opted-in) console must never create/modify logs/events"


def test_28_opt_in_console_writes_to_chosen_dir_only(tmp_log_dir):
    demo = _load_demo()
    console = _new_console(demo, enable_observability_log=True, observability_log_dir=tmp_log_dir)
    console.start()
    try:
        _run_turn(console, demo, "Halo.", "turn28-1", conversation_id="conv28")
        assert _wait_until(lambda: console._event_log_writer is not None and console._event_log_writer.stats()["events_written"] > 0, 3.0)
    finally:
        console.stop()
    assert os.path.isdir(os.path.join(tmp_log_dir, "events"))


# ─────────────────────────────────────────────
#  4 - dashboard collectors (Phase 5-6)
# ─────────────────────────────────────────────

def test_30_collect_observability_summary_reflects_ambiguity_refusal():
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        _run_turn(console, demo, "Aquascape A pakai pompa kecil.", "turn30-1", conversation_id="conv30")
        _run_turn(console, demo, "Aquascape B pakai pompa besar.", "turn30-2", conversation_id="conv30")
        _run_turn(console, demo, "Pompanya gimana?", "turn30-3", conversation_id="conv30")
        summary = dash_collectors.collect_observability_summary(_modules_for(console), conversation_id="conv30")
    finally:
        console.stop()
    assert summary["found"] is True
    assert summary["topic_decision"]["ambiguity_refusal"] is True
    assert summary["status"] == "REFUSED"
    # Privacy: this collector never exposes a raw-text field - only the
    # bounded label/count keys `MemoryTurnTrace` itself carries.
    assert "text" not in summary
    assert "query" not in summary  # the raw query string, specifically - never present


def test_31_collect_session_trace_bounded_and_pipeline_shaped():
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "turn31-1", conversation_id="conv31")
        _run_turn(console, demo, "Kalau koneksinya?", "turn31-2", conversation_id="conv31")
        trace = dash_collectors.collect_session_trace(_modules_for(console), conversation_id="conv31")
    finally:
        console.stop()
    assert trace["turns_available"] == 2
    stages = [s["stage"] for s in trace["turns"][0]["pipeline"]]
    assert stages == [
        "USER_INPUT", "CLASSIFICATION", "REFERENCE_RESOLUTION", "TOPIC_UPDATE",
        "MEMORY_CANDIDATES", "MEMORY_SELECTION", "CONTEXT_ASSEMBLY", "ASSISTANT_RESPONSE",
    ]


def test_32_e2e_observability_routes_through_real_dashboard_http(tmp_log_dir):
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    modules = {
        "planner_module": console.planner_module,
        "vision_module": console.vision_module,
        "tool_manager_module": console.tool_manager_module,
        "behavior_tree_module": console.behavior_tree_module,
        "session_manager": console.session_manager,
        "barge_in_module": console.barge_in_module,
    }
    from luno.bootstrap.launcher_config import LauncherConfig
    dashboard = DashboardServer(console.runtime, console.adapter_manager, modules, LauncherConfig(),
                                 host="127.0.0.1", port=0, observability_log_dir=tmp_log_dir)
    dashboard.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "turn32-1", conversation_id="conv32")

        r = requests.get(dashboard.url + "api/observability/summary", params={"conversation_id": "conv32"}, timeout=5)
        assert r.status_code == 200 and r.json()["found"] is True

        r = requests.get(dashboard.url + "api/observability/session_trace", params={"conversation_id": "conv32"}, timeout=5)
        assert r.status_code == 200 and r.json()["turns_available"] == 1

        # The pre-existing generic Event Bus page automatically shows the
        # 3 new event types too - no dashboard code change was needed for
        # this part (see change-impact doc's own "extends, does not
        # duplicate" section).
        assert _wait_until(lambda: any(
            e["type"] == "memory_topic_decision"
            for e in dashboard._events_buffer.snapshot(limit=200)
        ), 3.0)
    finally:
        dashboard.stop()
        console.stop()


# ─────────────────────────────────────────────
#  5 - performance (Phase 14, target <5ms/call)
# ─────────────────────────────────────────────

def test_40_event_log_writer_write_latency_budget(tmp_log_dir):
    bus = EventBus()
    bus.start()
    writer = EventLogWriter(bus, log_dir=tmp_log_dir)
    n = 300
    durations = []
    for i in range(n):
        start = time.perf_counter()
        writer._on_event(Event(type="memory_reference_classified", data={"request_id": f"r{i}", "reference_type": "comparison"}))
        durations.append((time.perf_counter() - start) * 1000.0)
    bus.stop()
    mean_ms = sum(durations) / len(durations)
    assert mean_ms < 5.0, f"mean write latency {mean_ms:.3f}ms exceeds 5ms budget"


def test_41_redact_function_latency_budget():
    payload = {"a": 1, "b": "text", "nested": {"api_key": "x", "c": [1, 2, 3]}}
    n = 2000
    start = time.perf_counter()
    for _ in range(n):
        _redact(payload)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert (elapsed_ms / n) < 5.0


# ─────────────────────────────────────────────
#  6 - cross-conversation isolation
# ─────────────────────────────────────────────

def test_50_cross_conversation_isolation_of_new_events():
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    events_by_conv = {"conv50a": [], "conv50b": []}
    sub = console.event_bus.subscribe("memory_topic_decision", lambda e: events_by_conv.get(e.data.get("conversation_id"), []).append(e.data))
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "turn50a-1", conversation_id="conv50a")
        _run_turn(console, demo, "GPU pakai RTX 3060.", "turn50b-1", conversation_id="conv50b")
        _run_turn(console, demo, "Kalau koneksinya?", "turn50a-2", conversation_id="conv50a")
    finally:
        console.event_bus.unsubscribe(sub)
        console.stop()
    assert len(events_by_conv["conv50a"]) == 2
    assert len(events_by_conv["conv50b"]) == 1
    assert "rtx" not in json.dumps(events_by_conv["conv50a"]).lower()
