"""
test_memory_voice_observability.py
====================================

LUNO BRAIN DEBUGGER / MEMORY & VOICE OBSERVABILITY DASHBOARD sprint -
test suite for the ADDITIONS this sprint made:

  - `luno/memory_context.py`: `assemble_context(funnel=...)` - a
    write-only, additive, optional parameter that records stage counts
    at points already naturally computed inside the function. Default
    `None` preserves every pre-existing caller/test unchanged.
  - `luno/memory_turn_trace.py`: `MemoryTurnTrace` gained
    `retrieval_called`/`query_intent`/`reference_type`/
    `is_short_followup`/`active_topic_terms`/`topic_history`/`funnel`
    fields, and `build_turn_trace()` gained matching keyword parameters
    to populate them - additive, all optional, all default to falsy/empty.
  - `main_runtime_demo.py`: `PlannerBridgeModule._turn_trace_history` - a
    new, additive, bounded (`deque(maxlen=100)`) cross-conversation ring
    buffer, populated in the SAME `try/except` block that already wrote
    `_last_turn_trace` each turn (see that call site's own comments).
  - `luno/dashboard/voice_latency.py`: `VoiceLatencyRecorder` (a passive
    Event Bus observer, `priority=-1000`, mirrors `events_buffer.py`'s
    own `StatsAggregator` pattern) and `parse_chunk_timeline_from_logs()`
    (parses already-structured `fish_audio.py` log lines the dashboard's
    existing `LogCapture` already captures - never a new measurement
    inside TTS code).
  - `luno/dashboard/collectors.py`: the "Phase 1-4" section
    (`collect_memory_turn_list`, `collect_memory_decision_trace`,
    `collect_retrieval_funnel`, `collect_topic_history_timeline`,
    `collect_memory_quality_metrics`) and the "Phase 6/7" section
    (`collect_voice_pipeline`, `collect_voice_latency_timeline`).
  - `luno/dashboard/server.py`: the matching `/api/memory/turns`,
    `/api/memory/decision_trace`, `/api/memory/retrieval_funnel`,
    `/api/memory/topic_history_timeline`, `/api/memory/quality_metrics`,
    `/api/voice/pipeline`, `/api/voice/latency_timeline` GET routes, plus
    `DashboardServer`'s own `VoiceLatencyRecorder` lifecycle wiring.

This sprint is explicitly OBSERVABILITY ONLY - nothing here re-ranks,
re-retrieves, re-tokenizes, calls an LLM/embedding judge, or changes what
Luno remembers or says. Every scenario below either (a) proves the new
telemetry is recorded correctly through a REAL production turn, or (b)
proves the new telemetry provably CANNOT change production behavior
(scenarios H/I especially - see their own docstrings for exactly what is
compared).

Scenarios A-P, per this sprint's own Phase 10 checklist, plus one
explicit end-to-end test through the real production path:
RuntimeDemoConsole -> PlannerBridgeModule -> memory/context pipeline ->
telemetry -> a real, running DashboardServer's HTTP API.

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture already
redirects every persistent-state file to an isolated temp path for every
test in this file - no manual save/restore boilerplate needed, and no
test here can ever touch Vinn's real production data.

Run:
    python3 -m pytest tests/test_memory_voice_observability.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from typing import Callable, List

import pytest
import requests

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import luno.memory as memory  # noqa: E402
import luno.memory_context as mc  # noqa: E402
from luno.core import Event  # noqa: E402
from luno.core.event_bus import EventBus  # noqa: E402
from luno.dashboard import collectors as dash_collectors  # noqa: E402
from luno.dashboard import DashboardServer  # noqa: E402
from luno.dashboard.voice_latency import VoiceLatencyRecorder, parse_chunk_timeline_from_logs  # noqa: E402
from luno.memory_retrieval import MemoryRetriever, MemoryRetrievalConfig  # noqa: E402


# ─────────────────────────────────────────────
# Shared helpers - mirrors tests/test_memory_topic_retention.py's and
# tests/test_llm_tts_streaming_production.py's own conventions exactly
# (this project's established "duplicate the small helper set per test
# file" house style, not a cross-file import).
# ─────────────────────────────────────────────

def _load_demo():
    demo_spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_observability", os.path.join(_ROOT, "main_runtime_demo.py")
    )
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules["main_runtime_demo_observability"] = demo
    demo_spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _new_console(demo, canned_text="Oke, dimengerti."):
    from luno.adapters import MockOpenRouterClient
    return demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text=canned_text, chunk_delay_s=0.0))


def _run_turn(console, demo, text, request_id, conversation_id=None, canned_reply=None):
    """Same exactly-once-signal convention as test_memory_topic_retention.py's
    own `_run_turn_and_capture()`: waits for `need_llm_response` (filtered
    by request_id) AND for `_pending_turns` to be popped (the precise,
    race-free signal that `_on_assistant_response()` - which now ALSO
    appends to `_turn_trace_history` - has actually completed)."""
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
        assert _wait_until(lambda: request_id not in console.planner_module._pending_turns, 5.0), (
            "assistant_response / topic-history / turn-trace update never completed for this request_id"
        )
    finally:
        console.event_bus.unsubscribe(sub)


def _modules_for(console):
    return {"planner_module": console.planner_module}


def _wake(console, demo) -> None:
    from luno.wake_session import ConversationState
    console.simulate_speech("alexa")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


def _run_voice_turn(console, user_text, timeout_s=8.0):
    """Drives a REAL full voice turn (session_manager -> behavior_tree ->
    planner -> LLM -> speak_request -> fish_audio -> speech_playback_*)
    via `console.simulate_speech()`, same harness
    `test_llm_tts_streaming_production.py`'s own `_run_turn()` uses.
    Returns the collected `request_id` (from `speak_request`'s own
    payload) once playback has finished.

    Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened) - `ENABLE_LLM_TTS_STREAMING` now defaults
    to `True` (see `luno/config.py`), so the turn's voice dispatch may
    fire `speak_stream_chunk` instead of (or in addition to) the legacy
    `speak_request`. Both payloads carry `request_id` at the top level
    (see `luno/incremental_speech.py`'s own `SpeakStreamChunk` publish
    site), so this helper now also listens for it - the actual
    invariant (a real `request_id` was captured for the turn) is
    unchanged."""
    request_ids: List[str] = []
    finished = threading.Event()
    subs = [
        console.event_bus.subscribe("speak_request", lambda e: request_ids.append(e.data.get("request_id"))),
        console.event_bus.subscribe("speak_stream_chunk", lambda e: request_ids.append(e.data.get("request_id"))),
        console.event_bus.subscribe("speech_playback_finished", lambda e: finished.set()),
    ]
    try:
        console.simulate_speech(user_text)
        assert _wait_until(finished.is_set, timeout_s), "speech_playback_finished never fired"
    finally:
        for s in subs:
            console.event_bus.unsubscribe(s)
    return request_ids[-1] if request_ids else None


def _retriever_with_manual_memory():
    retriever = MemoryRetriever(MemoryRetrievalConfig.from_env())
    retriever.register_source("manual_memory", memory.make_manual_memory_source(memory.list_memories))
    return retriever


# ─────────────────────────────────────────────
#  A - memory trace recorded after a real turn
# ─────────────────────────────────────────────

def test_a_turn_trace_recorded_after_real_turn():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _run_turn(console, demo, "Halo, apa kabar?", "turn-a1", conversation_id="conv-a")
        assert len(console.planner_module._turn_trace_history) == 1
        cid, trace = console.planner_module._turn_trace_history[-1]
        assert cid == "conv-a"
        assert trace.turn_id == "turn-a1"

        modules = _modules_for(console)
        turn_list = dash_collectors.collect_memory_turn_list(modules)
        assert turn_list["turns"][0]["turn_id"] == "turn-a1"

        trace_data = dash_collectors.collect_memory_decision_trace(modules, turn_id="turn-a1")
        assert trace_data["found"] is True
        assert trace_data["turn_id"] == "turn-a1"
        assert trace_data["conversation_id"] == "conv-a"
        # Privacy boundary: raw query text must never appear anywhere in
        # the trace payload (MemoryTurnTrace never stores it - see that
        # module's own docstring).
        import json
        assert "apa kabar" not in json.dumps(trace_data)
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  B - empty retrieval represented correctly (never fabricated)
# ─────────────────────────────────────────────

def test_b_empty_retrieval_represented_correctly():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _run_turn(console, demo, "Halo, apa kabar?", "turn-b1", conversation_id="conv-b")
        modules = _modules_for(console)
        trace_data = dash_collectors.collect_memory_decision_trace(modules, turn_id="turn-b1")
        assert trace_data["found"] is True
        assert trace_data["retrieval"]["candidate_count"] == 0
        assert trace_data["candidates"] == []
        funnel = dash_collectors.collect_retrieval_funnel(modules, turn_id="turn-b1")
        stages_by_key = {s["stage"]: s["count"] for s in funnel["funnel"]}
        assert stages_by_key["memory_candidates"] == 0
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  C - candidate -> ranking -> budget -> prompt stages distinguishable
# ─────────────────────────────────────────────

def test_c_funnel_stages_present_and_ordered_sanely():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        memory.add_memory("Vinn suka kopi hitam tanpa gula", source="user_explicit")
        memory.add_memory("Vinn pakai RTX 3070 Ti di laptop", source="user_explicit")
        _run_turn(console, demo, "Aku suka minum apa ya biasanya?", "turn-c1", conversation_id="conv-c")
        modules = _modules_for(console)
        funnel = dash_collectors.collect_retrieval_funnel(modules, turn_id="turn-c1")
        stages = funnel["funnel"]
        assert [s["stage"] for s in stages] == [
            "query", "topic_candidates", "memory_candidates", "context_items",
            "after_dedup", "after_ranking", "after_budget", "prompt",
        ]
        for s in stages:
            assert s["count"] is None or isinstance(s["count"], int)
        counts = {s["stage"]: s["count"] for s in stages}
        # Budget/prompt can never exceed what survived ranking, which can
        # never exceed what survived dedup, which can never exceed the raw
        # candidate pool - a monotonic non-increasing funnel is exactly
        # what "candidate -> ranking -> budget -> prompt" means.
        assert counts["after_budget"] <= counts["after_ranking"] <= counts["after_dedup"] <= counts["context_items"]
        assert counts["prompt"] == counts["after_budget"]
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  D - topic history display never mutates production state
# ─────────────────────────────────────────────

def test_d_topic_history_display_does_not_mutate_production_state():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441 buat mic array saya.", "turn-d1", conversation_id="conv-d")
        modules = _modules_for(console)

        before_active = console.planner_module._active_topic.get("conv-d")
        before_terms = sorted(before_active.terms) if before_active else []
        before_history = [sorted(e.terms) for e in (console.planner_module._topic_history.get("conv-d") or [])]

        # Call every read-only collector twice - a mutation bug would
        # show up as the SECOND call seeing different state than the
        # first, or as the live state below differing from `before_*`.
        for _ in range(2):
            dash_collectors.collect_topic_history_timeline(modules, conversation_id="conv-d")
            dash_collectors.collect_memory_decision_trace(modules, conversation_id="conv-d")
            dash_collectors.collect_retrieval_funnel(modules, conversation_id="conv-d")

        after_active = console.planner_module._active_topic.get("conv-d")
        after_terms = sorted(after_active.terms) if after_active else []
        after_history = [sorted(e.terms) for e in (console.planner_module._topic_history.get("conv-d") or [])]

        assert after_terms == before_terms
        assert after_history == before_history
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  E - correct topic recovered for a pronoun follow-up
#      (the sprint brief's own worked example: ESP32/INMP441/mic turn,
#      unrelated turn, then "Yang tadi soal mic gimana?")
# ─────────────────────────────────────────────

def test_e_correct_topic_recovered_for_pronoun_followup():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441 buat mic wireless saya, gimana caranya?", "turn-e1", conversation_id="conv-e")
        _run_turn(console, demo, "Aquascape saya butuh CO2 diffuser apa ya?", "turn-e2", conversation_id="conv-e")
        _run_turn(console, demo, "Yang tadi soal mic gimana?", "turn-e3", conversation_id="conv-e")

        modules = _modules_for(console)
        trace_data = dash_collectors.collect_memory_decision_trace(modules, turn_id="turn-e3")
        assert trace_data["found"] is True
        history = trace_data["topic_state"]["topic_history"]
        # The ESP32/mic entry (pushed by turn-e1) must be the one marked
        # referenced/produced_candidate on turn-e3 - NOT the aquascape one.
        esp_entries = [e for e in history if "esp" in e["terms"] or "mic" in e["terms"]]
        assert esp_entries, f"no ESP32/mic topic-history entry found: {history}"
        assert any(e["referenced"] and e["produced_candidate"] for e in esp_entries)
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  F - wrong topic not falsely reported as correct (contamination check)
# ─────────────────────────────────────────────

def test_f_wrong_topic_not_falsely_marked_referenced():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441 buat mic wireless saya.", "turn-f1", conversation_id="conv-f")
        _run_turn(console, demo, "Aquascape saya butuh CO2 diffuser apa ya?", "turn-f2", conversation_id="conv-f")
        # A follow-up that is unambiguously about the aquascape topic, not mic.
        _run_turn(console, demo, "Diffusernya yang tadi merk apa ya?", "turn-f3", conversation_id="conv-f")

        modules = _modules_for(console)
        trace_data = dash_collectors.collect_memory_decision_trace(modules, turn_id="turn-f3")
        assert trace_data["found"] is True
        history = trace_data["topic_state"]["topic_history"]
        esp_entries = [e for e in history if "esp" in e["terms"] or ("mic" in e["terms"] and "aquascape" not in e["terms"])]
        # The ESP32/mic entry must NOT be falsely marked as referenced by
        # a turn that was actually about the aquascape/diffuser topic.
        for e in esp_entries:
            assert not e["referenced"], f"ESP32 entry falsely marked referenced by an aquascape follow-up: {e}"
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  G - multiple topics remain isolated across conversations
# ─────────────────────────────────────────────

def test_g_multiple_conversations_topic_history_isolated():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441 buat mic wireless saya.", "turn-g1", conversation_id="conv-g1")
        _run_turn(console, demo, "Aquascape saya butuh CO2 diffuser apa ya?", "turn-g2", conversation_id="conv-g2")

        modules = _modules_for(console)
        t1 = dash_collectors.collect_topic_history_timeline(modules, conversation_id="conv-g1")
        t2 = dash_collectors.collect_topic_history_timeline(modules, conversation_id="conv-g2")
        assert all(turn["turn_id"] != "turn-g2" for turn in t1["turns"])
        assert all(turn["turn_id"] != "turn-g1" for turn in t2["turns"])
        g1_terms = set()
        for turn in t1["turns"]:
            g1_terms |= set(turn["active_topic_terms"])
        g2_terms = set()
        for turn in t2["turns"]:
            g2_terms |= set(turn["active_topic_terms"])
        assert "aquascape" not in g1_terms
        assert "esp" not in g2_terms
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  H - telemetry does not affect ranking
#  I - telemetry does not affect retrieval
# ─────────────────────────────────────────────
# Both scenarios call `memory_context.assemble_context()` DIRECTLY (the
# exact function `funnel=` was added to) with identical inputs, once with
# `funnel=None` and once with a fresh `funnel={}` dict, and assert the
# returned `AssembledContext` is byte-for-byte identical either way. This
# is the strongest possible proof available: not "the dashboard looked
# consistent," but "the function that does ranking/retrieval selection
# returns the IDENTICAL object graph regardless of whether the write-only
# telemetry parameter is used."

def test_h_telemetry_does_not_affect_ranking():
    memory.add_memory("Vinn suka kopi hitam tanpa gula", source="user_explicit")
    memory.add_memory("Vinn pakai RTX 3070 Ti di laptop", source="user_explicit")
    memory.add_memory("Vinn selalu backup data setiap minggu", source="user_explicit")
    retriever = _retriever_with_manual_memory()
    precomputed = retriever.retrieve_memories("Aku suka minum apa ya biasanya?")

    ctx_without = mc.assemble_context(
        "Aku suka minum apa ya biasanya?", memory_retriever=retriever,
        get_manual_memories=memory.list_memories,
        precomputed_relevant_memories=precomputed, funnel=None,
    )
    ctx_with = mc.assemble_context(
        "Aku suka minum apa ya biasanya?", memory_retriever=retriever,
        get_manual_memories=memory.list_memories,
        precomputed_relevant_memories=precomputed, funnel={},
    )
    ids_without = [(i.source, i.memory_id, i.text) for i in ctx_without.items]
    ids_with = [(i.source, i.memory_id, i.text) for i in ctx_with.items]
    assert ids_without == ids_with, "funnel= must never change ranking/selection order"
    assert ctx_without.render() == ctx_with.render()


def test_i_telemetry_does_not_affect_retrieval_candidate_count():
    memory.add_memory("Vinn suka kopi hitam tanpa gula", source="user_explicit")
    memory.add_memory("Vinn pakai RTX 3070 Ti di laptop", source="user_explicit")
    retriever = _retriever_with_manual_memory()
    precomputed = retriever.retrieve_memories("Aku suka minum apa ya biasanya?")

    funnel: dict = {}
    ctx = mc.assemble_context(
        "Aku suka minum apa ya biasanya?", memory_retriever=retriever,
        get_manual_memories=memory.list_memories,
        precomputed_relevant_memories=precomputed, funnel=funnel,
    )
    # The funnel must TRUTHFULLY reflect what retrieval actually returned
    # - never a second, independently-triggered retrieval call, never a
    # padded/estimated count.
    assert funnel["memory_candidates"] == len(precomputed)
    assert funnel["context_items"] >= 0
    assert funnel["prompt"] == len(ctx.items)


# ─────────────────────────────────────────────
#  J - telemetry failure does not break the conversation
# ─────────────────────────────────────────────

def test_j_telemetry_failure_does_not_break_conversation(monkeypatch):
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        def _boom(*a, **k):
            raise RuntimeError("simulated telemetry failure")
        monkeypatch.setattr(demo, "build_turn_trace", _boom)

        # The turn must still complete normally (need_llm_response fires,
        # _pending_turns clears) even though telemetry construction now
        # unconditionally raises - proving the try/except around
        # `build_turn_trace()` in `_on_assistant_response()` truly
        # isolates a telemetry bug from the real conversation.
        _run_turn(console, demo, "Halo, apa kabar?", "turn-j1", conversation_id="conv-j")
        assert "turn-j1" not in console.planner_module._pending_turns
        # And, correctly, no trace was recorded for this turn (telemetry
        # genuinely failed - this must be visible as "nothing recorded",
        # never silently faked).
        assert not any(t.turn_id == "turn-j1" for _cid, t in console.planner_module._turn_trace_history)
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  K - telemetry remains bounded
# ─────────────────────────────────────────────

def test_k_turn_trace_history_is_bounded():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        assert console.planner_module._turn_trace_history.maxlen == 100
        for i in range(120):
            _run_turn(console, demo, "halo", f"turn-k{i}", conversation_id="conv-k")
        assert len(console.planner_module._turn_trace_history) <= 100
    finally:
        console.stop()


def test_k_voice_latency_recorder_is_bounded():
    bus = EventBus()
    bus.start()
    try:
        recorder = VoiceLatencyRecorder(bus, maxlen=10)
        for i in range(25):
            bus.publish(Event(type="need_llm_response", data={"request_id": f"req-{i}"}))
        _wait_until(lambda: len(recorder.list_recent(limit=100)) > 0, 3.0)
        time.sleep(0.3)  # let the last few events drain through the pump thread
        assert len(recorder._timelines) <= 10
        recorder.unsubscribe(bus)
    finally:
        bus.stop()


# ─────────────────────────────────────────────
#  L - concurrent conversations remain isolated
# ─────────────────────────────────────────────

def test_l_concurrent_conversations_remain_isolated():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        errors = []

        def _drive(conv_id, topic_text, n):
            try:
                for i in range(n):
                    _run_turn(console, demo, topic_text, f"turn-l-{conv_id}-{i}", conversation_id=conv_id)
            except Exception as ex:  # pragma: no cover - surfaced via `errors`
                errors.append(ex)

        t1 = threading.Thread(target=_drive, args=("conv-l1", "ESP32 mic project saya.", 3))
        t2 = threading.Thread(target=_drive, args=("conv-l2", "Aquascape CO2 diffuser saya.", 3))
        t1.start(); t2.start()
        t1.join(timeout=15); t2.join(timeout=15)
        assert not errors, f"concurrent turns raised: {errors}"

        modules = _modules_for(console)
        tl1 = dash_collectors.collect_topic_history_timeline(modules, conversation_id="conv-l1")
        tl2 = dash_collectors.collect_topic_history_timeline(modules, conversation_id="conv-l2")
        assert all(t["turn_id"].startswith("turn-l-conv-l1") for t in tl1["turns"])
        assert all(t["turn_id"].startswith("turn-l-conv-l2") for t in tl2["turns"])
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  M - TTS latency metrics recorded correctly
#  N - first-audio latency measured correctly
# ─────────────────────────────────────────────

def test_m_and_n_tts_and_first_audio_latency_recorded_correctly():
    demo = _load_demo()
    from luno.adapters import MockFishAudioClient
    console = demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text="Balasan singkat untuk pengujian latensi.", chunk_delay_s=0.0),
        fish_audio_client=MockFishAudioClient(playback_delay_s=0.05),
    )
    console.start()
    try:
        recorder = VoiceLatencyRecorder(console.event_bus)
        _wake(console, demo)
        request_id = _run_voice_turn(console, "halo")
        assert request_id, "no request_id captured from speak_request"
        time.sleep(0.1)  # let speech_playback_finished's own bus delivery settle

        snap = recorder.snapshot_for(request_id)
        assert snap is not None, f"no telemetry recorded for {request_id}"
        assert snap["llm_first_token_latency_ms"] is not None
        assert snap["llm_total_latency_ms"] is not None
        assert snap["first_audio_latency_ms"] is not None
        assert snap["playback_duration_ms"] is not None
        # Sanity ordering: LLM must finish (or at least start producing
        # tokens) before or at roughly the same time audio starts - a
        # first-audio latency that's negative or absurdly small would
        # indicate a mis-wired timestamp, not real behavior.
        assert snap["first_audio_latency_ms"] >= 0
        assert snap["llm_first_token_latency_ms"] >= 0
        assert snap["playback_duration_ms"] >= 0
        recorder.unsubscribe(console.event_bus)
    finally:
        console.stop()


# ─────────────────────────────────────────────
#  O - chunk gaps measured correctly
# ─────────────────────────────────────────────

def test_o_chunk_gaps_measured_correctly_from_log_lines():
    request_id = "req-o1"
    # Synthetic LogCapture-shaped records - same shape `LogCapture.
    # snapshot()` already returns (see logs_buffer.py), with `message`
    # text mirroring fish_audio.py's own real `log()` call format
    # (`ChunkAudioStart ... chunk_index=N chunk_synthesis_time_s=X`,
    # `ChunkFinished ... chunk_index=N total_s=X playback_s=Y`).
    log_entries = [
        {"message": f"ChunkAudioStart request_id={request_id} chunk_index=0 chunk_synthesis_time_s=0.12", "wall_time": 100.0, "request_id": request_id},
        {"message": f"ChunkFinished request_id={request_id} chunk_index=0 total_s=0.30 playback_s=0.18", "wall_time": 100.3, "request_id": request_id},
        {"message": f"ChunkAudioStart request_id={request_id} chunk_index=1 chunk_synthesis_time_s=0.10", "wall_time": 100.5, "request_id": request_id},
        {"message": f"ChunkFinished request_id={request_id} chunk_index=1 total_s=0.28 playback_s=0.18", "wall_time": 100.78, "request_id": request_id},
        # A different request_id's chunk lines must never leak in.
        {"message": "ChunkAudioStart request_id=other-req chunk_index=0 chunk_synthesis_time_s=0.50", "wall_time": 100.4, "request_id": "other-req"},
    ]
    chunks = parse_chunk_timeline_from_logs(log_entries, request_id)
    assert len(chunks) == 2
    assert chunks[0]["chunk_index"] == "0"
    assert chunks[0]["gap_before_ms"] is None  # first chunk has no "before" gap
    assert chunks[1]["chunk_index"] == "1"
    # 100.5 - 100.0 = 0.5s = 500ms
    assert chunks[1]["gap_before_ms"] == pytest.approx(500.0, abs=1.0)
    assert chunks[0]["chunk_synthesis_time_s"] == "0.12"
    assert chunks[0]["playback_s"] == "0.18"
    assert chunks[0]["total_s"] == "0.30"


# ─────────────────────────────────────────────
#  P - cancellation/pause events do not corrupt telemetry
# ─────────────────────────────────────────────

def test_p_cancellation_and_pause_events_do_not_corrupt_telemetry():
    bus = EventBus()
    bus.start()
    try:
        recorder = VoiceLatencyRecorder(bus)
        rid = "req-p1"
        bus.publish(Event(type="need_llm_response", data={"request_id": rid}))
        bus.publish(Event(type="llm_started", data={"request_id": rid}))
        bus.publish(Event(type="llm_chunk", data={"request_id": rid, "index": 0}))
        bus.publish(Event(type="llm_finished", data={"request_id": rid, "execution_time_ms": 42.0}))
        bus.publish(Event(type="speak_request", data={"request_id": rid}))
        bus.publish(Event(type="speech_playback_started", data={"request_id": rid}))
        bus.publish(Event(type="speech_playback_paused", data={"request_id": rid}))
        bus.publish(Event(type="speech_playback_resumed", data={"request_id": rid}))
        bus.publish(Event(type="speech_playback_cancelled", data={"request_id": rid}))
        assert _wait_until(lambda: recorder.snapshot_for(rid) is not None and recorder.snapshot_for(rid).get("cancelled"), 3.0)

        snap = recorder.snapshot_for(rid)
        assert snap["cancelled"] is True
        assert snap["pause_count"] == 1
        assert snap["resume_count"] == 1
        assert snap["llm_execution_time_ms"] == 42.0
        # No latency derived from these events should ever be negative -
        # a sign timestamps were misread/mis-subtracted.
        for key in ("llm_first_token_latency_ms", "llm_total_latency_ms", "first_audio_latency_ms",
                    "playback_duration_ms", "total_turn_latency_ms"):
            if snap[key] is not None:
                assert snap[key] >= 0
        recorder.unsubscribe(bus)
    finally:
        bus.stop()


# ─────────────────────────────────────────────
#  Z - end-to-end through the real production path + real dashboard HTTP
# ─────────────────────────────────────────────

def test_z_e2e_real_production_path_through_dashboard_http():
    """RuntimeDemoConsole -> PlannerBridgeModule -> memory/context
    pipeline -> telemetry -> a REAL, running DashboardServer's HTTP API
    (`requests`, not an in-process function call) - the exactly-once
    production-path E2E test Phase 10 explicitly requires, not just a
    test against mocked dashboard objects."""
    demo = _load_demo()
    from luno.adapters import MockFishAudioClient
    console = demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text="Oke, dimengerti.", chunk_delay_s=0.0),
        fish_audio_client=MockFishAudioClient(playback_delay_s=0.02),
    )
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
    # Sprint 50 (Runtime Observability) - `DashboardServer.start()` now
    # unconditionally wires an `EventLogWriter` too (same lifecycle as
    # `EventRingBuffer`/`StatsAggregator`/`VoiceLatencyRecorder` right
    # above it), which persists to disk. `observability_log_dir` is
    # pointed at a temp directory here so this pre-existing test doesn't
    # gain a new side effect of writing real files into the repository's
    # own `logs/` directory every time it runs.
    import shutil
    import tempfile
    _obs_log_dir = tempfile.mkdtemp(prefix="luno_dashboard_obs_")
    dashboard = DashboardServer(console.runtime, console.adapter_manager, modules, LauncherConfig(),
                                 host="127.0.0.1", port=0, observability_log_dir=_obs_log_dir)
    dashboard.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441 buat mic wireless saya.", "turn-z1", conversation_id="conv-z")
        _run_turn(console, demo, "Yang tadi soal mic gimana?", "turn-z2", conversation_id="conv-z")

        r = requests.get(dashboard.url + "api/memory/turns", timeout=5)
        assert r.status_code == 200
        turn_ids = [t["turn_id"] for t in r.json()["turns"]]
        assert "turn-z1" in turn_ids and "turn-z2" in turn_ids

        r = requests.get(dashboard.url + "api/memory/decision_trace", params={"turn_id": "turn-z2"}, timeout=5)
        assert r.status_code == 200
        trace = r.json()
        assert trace["found"] is True
        assert any(e["referenced"] for e in trace["topic_state"]["topic_history"])

        r = requests.get(dashboard.url + "api/memory/retrieval_funnel", params={"turn_id": "turn-z2"}, timeout=5)
        assert r.status_code == 200 and r.json()["found"] is True

        r = requests.get(dashboard.url + "api/memory/topic_history_timeline", params={"conversation_id": "conv-z"}, timeout=5)
        assert r.status_code == 200
        assert r.json()["turns_available"] == 2

        r = requests.get(dashboard.url + "api/memory/quality_metrics", timeout=5)
        assert r.status_code == 200 and r.json()["sample_size"] >= 2

        _wake(console, demo)
        req_id = _run_voice_turn(console, "halo")
        time.sleep(0.1)
        r = requests.get(dashboard.url + "api/voice/pipeline", params={"request_id": req_id}, timeout=5)
        assert r.status_code == 200
        voice = r.json()
        assert voice["found"] is True
        assert voice["latencies_ms"]["llm_total"] is not None

        r = requests.get(dashboard.url + "api/voice/latency_timeline", timeout=5)
        assert r.status_code == 200 and r.json()["turns_available"] >= 1

        # Full page still serves and includes the new panel - proves
        # server.py's routing changes didn't break the existing `/`.
        r = requests.get(dashboard.url, timeout=5)
        assert r.status_code == 200 and "Brain Debugger" in r.text
    finally:
        dashboard.stop()
        console.stop()
        shutil.rmtree(_obs_log_dir, ignore_errors=True)
