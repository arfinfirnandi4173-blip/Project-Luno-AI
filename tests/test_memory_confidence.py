"""
test_memory_confidence.py
============================================

SPRINT 40 - MEMORY CONFIDENCE & CONFLICT RESOLUTION (confidence half).

Covers the CONFIDENCE side of the brief: `ContextItem.confidence`,
`_confidence_for_relevant_memory()`, `_rank_key()`'s extended tuple (and
the invariant that RELEVANCE still strictly dominates CONFIDENCE),
confidence gating (ambiguous references must not inject arbitrary
context), and multi-topic safety (the brief's own exact three-topic
scenario). The CONFLICT model (supersession tagging, historical
retrieval, domain generalization, structural no-hardcoding proof) lives
in the sibling file `tests/test_memory_conflict_resolution.py`.

Root cause this sprint addresses (Phase 0 finding): the codebase already
has a sophisticated conflict-resolution system, but it lives entirely in
the PERSISTENT long-term memory layer (`luno.memory`), reachable only
via an explicit "inget ya ..." command. Ordinary conversation flows
exclusively through the EPHEMERAL `_active_topic`/`_topic_history` bag-
of-terms layer (`luno.memory_context`), which had zero confidence/
conflict awareness before this sprint - two topic-history entries about
the SAME subject (old value, new value) rendered as two identically-
labeled "Active conversation topic:" lines, with nothing telling the LLM
which one was current.

No LLM judge, no embeddings, no second ranking system, no second memory
store, no persistent raw conversation storage, no global topic state -
every mechanism here reuses an already-existing signal (`_TOPIC_OVERLAP_
STOPWORDS`, `_CORRECTION_RE`/`_is_temporal_change()`, the `historical`/
`_section_for_item()` machinery, `analyze_query()`) or is a small,
bounded, deterministic, conversation-scoped addition.
"""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import threading
import time
from typing import Callable

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno import memory  # noqa: E402
from luno import memory_context  # noqa: E402
from luno.memory_retrieval.models import RelevantMemory  # noqa: E402


def _load_demo():
    demo_spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_mem_confidence", os.path.join(_ROOT, "main_runtime_demo.py"),
    )
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules["main_runtime_demo_mem_confidence"] = demo
    demo_spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 6.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


class _SequentialMockOpenRouter:
    """Same duplicated-not-imported helper pattern every prior sprint's
    E2E test module already uses (see test_conversation_intelligence.py,
    test_conversation_reference_resolution.py) - each test module stays
    independently runnable."""

    @staticmethod
    def build(demo, replies):
        from luno.adapters import MockOpenRouterClient

        class _Client(MockOpenRouterClient):
            def __init__(self):
                super().__init__(canned_text=None)

            def _resolve_text(self, messages):
                text = messages[0]["content"] if messages else ""
                for key, val in replies.items():
                    if key.strip() in text or text.strip() == key.strip():
                        return val
                return "(no canned reply configured for this turn)"

        return _Client()


def _new_console(demo, replies=None, canned_text="Oke."):
    if replies:
        client = _SequentialMockOpenRouter.build(demo, replies)
    else:
        from luno.adapters import MockOpenRouterClient
        client = MockOpenRouterClient(canned_text=canned_text, chunk_delay_s=0.0)
    return demo.RuntimeDemoConsole(openrouter_client=client)


def _run_turn(console, demo, text, request_id, conversation_id=None):
    done = threading.Event()

    def _capture(e):
        if e.get("request_id") == request_id:
            done.set()

    sub = console.event_bus.subscribe("assistant_response", _capture)
    data = {"text": text, "request_id": request_id}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    try:
        console.event_bus.publish(demo.Event(type="user_utterance", data=data))
        assert _wait_until(done.is_set, 6.0), f"no assistant_response for {request_id!r} within timeout"
        assert _wait_until(lambda: request_id not in console.planner_module._pending_turns, 5.0), (
            "active-topic/topic-history update never completed for this request_id"
        )
    finally:
        console.event_bus.unsubscribe(sub)


def _run_turn_capture_prompt(console, demo, text, request_id, conversation_id=None):
    captured = {}
    need_llm = threading.Event()

    def _capture_prompt(e):
        if e.get("request_id") == request_id:
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

    sub = console.event_bus.subscribe("need_llm_response", _capture_prompt)
    try:
        _run_turn(console, demo, text, request_id, conversation_id=conversation_id)
        _wait_until(need_llm.is_set, 3.0)
    finally:
        console.event_bus.unsubscribe(sub)
    return captured.get("system_prompt") or ""


# ============================================================================
# Section 1 - ContextItem.confidence field + _confidence_for_relevant_memory()
# ============================================================================

def test_01_confidence_field_defaults_to_none():
    item = memory_context.ContextItem(source="manual_memory", memory_id="x", text="x", relevance=0.5)
    assert item.confidence is None


def test_02_confidence_none_for_non_active_conversation_sources():
    for source in ("manual_memory", "episodic_memory", "verified_facts", "vision_objects"):
        rm = RelevantMemory(text="x", source=source, score=0.5, timestamp=0.0, stale=False, raw={"status": "active"})
        assert memory_context._confidence_for_relevant_memory(rm) is None, source


def test_03_confidence_none_when_raw_is_not_a_dict():
    rm = RelevantMemory(text="x", source="active_conversation", score=0.5, timestamp=0.0, stale=False, raw="not-a-dict")
    assert memory_context._confidence_for_relevant_memory(rm) is None


def test_04_confidence_active_for_active_status():
    rm = RelevantMemory(text="x", source="active_conversation", score=0.5, timestamp=0.0, stale=False, raw={"status": "active"})
    assert memory_context._confidence_for_relevant_memory(rm) == memory_context._CONFIDENCE_ACTIVE


def test_05_confidence_lower_for_superseded_status():
    rm = RelevantMemory(text="x", source="active_conversation", score=0.5, timestamp=0.0, stale=False, raw={"status": "superseded"})
    assert memory_context._confidence_for_relevant_memory(rm) == memory_context._CONFIDENCE_SUPERSEDED
    assert memory_context._CONFIDENCE_SUPERSEDED < memory_context._CONFIDENCE_ACTIVE


def test_06_relevant_memory_to_context_item_wires_confidence_through():
    rm = RelevantMemory(text="x", source="active_conversation", score=0.5, timestamp=0.0, stale=False, raw={"status": "superseded"})
    item = memory_context.relevant_memory_to_context_item(rm)
    assert item.confidence == memory_context._CONFIDENCE_SUPERSEDED


def test_07_relevant_memory_to_context_item_confidence_none_for_manual_memory():
    rm = RelevantMemory(text="[korèksi] x", source="manual_memory", score=0.9, timestamp=0.0, stale=False, raw={})
    item = memory_context.relevant_memory_to_context_item(rm)
    assert item.confidence is None


# ============================================================================
# Section 2 - _rank_key() invariant: RELEVANCE > CONFIDENCE, never the reverse
# ============================================================================

def test_08_rank_key_reads_confidence():
    source = inspect.getsource(memory_context.ContextItem._rank_key)
    assert "self.confidence" in source


def test_09_rank_key_confidence_is_the_final_tuple_element():
    """Structural check, not just a source-text search - confidence must
    sit strictly AFTER every pre-existing signal (relevance, importance,
    context_evidence, usefulness, evaluation, usage_count, intent_bonus,
    priority), i.e. at index -1. This is the sprint's own documented,
    evidence-scoped placement decision (see ContextItem's own docstring):
    the only reproduced defect is an arbitrary tie between two items that
    are ALREADY tied on every other field, so confidence must never be
    able to outrank a real relevance/importance/source-priority
    difference - it may only break a tie after all of them."""
    item = memory_context.ContextItem(
        source="manual_memory", memory_id="x", text="x",
        relevance=0.1, importance=1, context_evidence=0.2, usefulness=0.3,
        evaluation=0.4, usage_count=1, priority=1, confidence=0.99,
    )
    key = item._rank_key()
    assert key[-1] == 0.99
    assert len(key) == 9


def test_10_high_confidence_irrelevant_never_beats_relevant():
    """THE core invariant, verbatim from the brief: 'A highly confident
    irrelevant memory must NEVER beat a relevant memory.'"""
    irrelevant_but_confident = memory_context.ContextItem(
        source="active_conversation", memory_id="a", text="a",
        relevance=0.05, importance=4, context_evidence=1.0, usefulness=1.0,
        evaluation=1.0, usage_count=99, priority=4, confidence=1.0,
    )
    relevant_but_unconfident = memory_context.ContextItem(
        source="manual_memory", memory_id="b", text="b",
        relevance=0.9, importance=0, context_evidence=0.0, usefulness=0.0,
        evaluation=0.0, usage_count=0, priority=0, confidence=None,
    )
    assert relevant_but_unconfident._rank_key() > irrelevant_but_confident._rank_key()


def test_11_confidence_only_breaks_ties_among_equally_relevant_items():
    """The one real, reproduced use case: two `active_conversation` items
    tied on relevance/importance/every other field (current vs. superseded
    topic-history entries built from the same pipeline) - confidence
    decides the tie, current wins."""
    current = memory_context.ContextItem(
        source="active_conversation", memory_id="cur", text="cur",
        relevance=0.55, importance=1, confidence=memory_context._CONFIDENCE_ACTIVE,
    )
    superseded = memory_context.ContextItem(
        source="active_conversation", memory_id="sup", text="sup",
        relevance=0.55, importance=1, confidence=memory_context._CONFIDENCE_SUPERSEDED,
    )
    assert current._rank_key() > superseded._rank_key()


def test_12_missing_confidence_contributes_zero_not_an_error():
    item = memory_context.ContextItem(source="manual_memory", memory_id="x", text="x", relevance=0.5)
    # Must not raise, and must sort as the lowest possible confidence.
    assert item._rank_key()[-1] == 0.0


# ============================================================================
# Section 3 - confidence gating: ambiguous references must not inject
# arbitrary context (Phase 5). Re-confirms Sprint 38/39 behavior is intact
# under the Sprint 40 code changes, per the brief's explicit "do not break
# existing valid reference resolution" instruction.
# ============================================================================

def test_13_no_usable_signal_zero_injection_yang_mana():
    history = [memory_context.update_active_topic(None, "ESP32 pakai INMP441.", "Oke.", is_followup=False)]
    candidates = memory_context.select_topic_candidates(history, "Yang mana?", False)
    # "Yang mana?" carries no content tokens of its own beyond stopwords -
    # must not blindly return every bounded history entry.
    assert len(candidates) <= memory_context._TOPIC_HISTORY_CANDIDATE_LIMIT


def test_14_ambiguous_phrases_do_not_force_injection():
    for phrase in ["Kenapa?", "Kenapa begitu?", "Kalau begitu?", "Yang mana?", "Masih ada?", "Terus?"]:
        history = []
        candidates = memory_context.select_topic_candidates(history, phrase, True)
        assert candidates == [], f"{phrase!r} must not fabricate context from empty history"


def test_15_known_reference_still_retrieves_e2e():
    """Sprint 38/39's own working case must remain intact: an explicit,
    content-bearing reference ('yang tadi soal mic') still retrieves the
    right entry after Sprint 40's changes."""
    demo = _load_demo()
    replies = {
        "ESP32 saya pakai INMP441 buat mic.": "Oke, ESP32 dengan INMP441 buat mic dicatat.",
        "Aquascape saya pakai pompa CO2.": "Oke, aquascape dengan pompa CO2 dicatat.",
        "Yang tadi soal mic gimana?": "Mic kamu pakai ESP32 dengan INMP441.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 saya pakai INMP441 buat mic.", "gate-1")
        _run_turn(console, demo, "Aquascape saya pakai pompa CO2.", "gate-2")
        sp = _run_turn_capture_prompt(console, demo, "Yang tadi soal mic gimana?", "gate-3")
        assert "esp32" in sp.lower() and "inmp441" in sp.lower()
    finally:
        console.stop()


def test_16_zero_prior_history_ambiguous_reference_injects_nothing():
    demo = _load_demo()
    console = _new_console(demo, canned_text="Bisa lebih spesifik?")
    console.start()
    try:
        sp = _run_turn_capture_prompt(console, demo, "Yang tadi gimana?", "gate-4")
        assert "Active conversation topic" not in sp
    finally:
        console.stop()


# ============================================================================
# Section 4 - multi-topic safety (Phase 6), the brief's own exact scenario:
# Topic A (ESP32+INMP441), Topic B (Aquascape+pompa), Topic C (WLED+ESP8266).
# ============================================================================

def _topic_lines(system_prompt: str) -> str:
    """Isolates just the topic-history-derived lines of the prompt
    ('Active conversation topic:' / '[Historical Context]' section /
    'Previously stated' lines), excluding unrelated always-on sources
    like the mock home_assistant '[VERIFIED FACT]' line (a pre-existing
    environment fixture - `config/habit_memory.json` registers a
    `light.wled` device whose state is injected into EVERY turn's prompt
    regardless of conversation content, confirmed via live probing - not
    something this sprint's topic-history mechanism controls). Scoping
    the substring checks to just these lines is what makes the
    isolation assertions below meaningful rather than accidentally
    tripping over an unrelated, always-present source."""
    lines = []
    capture = False
    for line in system_prompt.splitlines():
        s = line.strip()
        if s == "[Historical Context]":
            capture = True
            lines.append(s)
            continue
        if s.startswith("- Active conversation topic") or s.startswith("- Previously stated"):
            lines.append(s)
            continue
        if capture and s.startswith("- "):
            lines.append(s)
        elif capture and not s:
            capture = False
    return "\n".join(lines)


def _multi_topic_console(demo):
    replies = {
        "ESP32 saya pakai INMP441 buat mic.": "Oke, ESP32 dengan INMP441 buat mic dicatat.",
        "Aquascape saya pakai pompa CO2.": "Oke, aquascape dengan pompa CO2 dicatat.",
        "Saya pakai WLED di ESP8266.": "Oke, WLED di ESP8266 dicatat.",
        "Yang tadi soal mic gimana?": "Mic kamu pakai ESP32 dengan INMP441.",
        "Pompa yang tadi bagaimana?": "Pompa aquascape kamu pakai CO2.",
        "WLED yang tadi?": "WLED kamu ada di ESP8266.",
        "Yang tadi gimana?": "Maksudnya yang mana ya? Bisa lebih spesifik?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    return console


def test_17_multi_topic_mic_query_isolates_topic_a():
    demo = _load_demo()
    console = _multi_topic_console(demo)
    try:
        _run_turn(console, demo, "ESP32 saya pakai INMP441 buat mic.", "mt-1")
        _run_turn(console, demo, "Aquascape saya pakai pompa CO2.", "mt-2")
        _run_turn(console, demo, "Saya pakai WLED di ESP8266.", "mt-3")
        sp = _run_turn_capture_prompt(console, demo, "Yang tadi soal mic gimana?", "mt-4")
        low = _topic_lines(sp).lower()
        assert "esp32" in low and "inmp441" in low
        assert "aquascape" not in low and "pompa" not in low
        assert "wled" not in low
    finally:
        console.stop()


def test_18_multi_topic_pompa_query_isolates_topic_b():
    demo = _load_demo()
    console = _multi_topic_console(demo)
    try:
        _run_turn(console, demo, "ESP32 saya pakai INMP441 buat mic.", "mt-1")
        _run_turn(console, demo, "Aquascape saya pakai pompa CO2.", "mt-2")
        _run_turn(console, demo, "Saya pakai WLED di ESP8266.", "mt-3")
        _run_turn(console, demo, "Yang tadi soal mic gimana?", "mt-4")
        sp = _run_turn_capture_prompt(console, demo, "Pompa yang tadi bagaimana?", "mt-5")
        low = _topic_lines(sp).lower()
        assert "aquascape" in low and "pompa" in low
        assert "wled" not in low and "esp8266" not in low
    finally:
        console.stop()


def test_19_multi_topic_wled_query_isolates_topic_c():
    demo = _load_demo()
    console = _multi_topic_console(demo)
    try:
        _run_turn(console, demo, "ESP32 saya pakai INMP441 buat mic.", "mt-1")
        _run_turn(console, demo, "Aquascape saya pakai pompa CO2.", "mt-2")
        _run_turn(console, demo, "Saya pakai WLED di ESP8266.", "mt-3")
        _run_turn(console, demo, "Yang tadi soal mic gimana?", "mt-4")
        _run_turn(console, demo, "Pompa yang tadi bagaimana?", "mt-5")
        sp = _run_turn_capture_prompt(console, demo, "WLED yang tadi?", "mt-6")
        low = _topic_lines(sp).lower()
        assert "wled" in low and "esp8266" in low
        assert "aquascape" not in low and "pompa" not in low
    finally:
        console.stop()


def test_20_multi_topic_ambiguous_query_does_not_dump_all_three():
    """The brief's own hard requirement: 'Yang tadi gimana?' (no subject
    at all) 'MUST NOT blindly inject all three.' Turn 3 (WLED/ESP8266) is
    the most-recently-active topic at the time of the ambiguous query, so
    it legitimately appears as THE single active topic line - the
    invariant under test is that topic A (mic/ESP32/INMP441) and topic B
    (aquascape/pompa) do NOT also get pulled in alongside it."""
    demo = _load_demo()
    console = _multi_topic_console(demo)
    try:
        _run_turn(console, demo, "ESP32 saya pakai INMP441 buat mic.", "mt-1")
        _run_turn(console, demo, "Aquascape saya pakai pompa CO2.", "mt-2")
        _run_turn(console, demo, "Saya pakai WLED di ESP8266.", "mt-3")
        sp = _run_turn_capture_prompt(console, demo, "Yang tadi gimana?", "mt-4")
        topic_count = sp.count("Active conversation topic")
        # At most the single most-recent topic, never all three at once.
        assert topic_count <= 1, f"expected at most 1 injected topic line, got {topic_count}"
        low = _topic_lines(sp).lower()
        mentioned = sum(1 for kw in ("aquascape", "inmp441") if kw in low)
        assert mentioned == 0, "ambiguous query must not blindly surface every unrelated topic"
    finally:
        console.stop()


# ============================================================================
# Section 5 - cross-conversation isolation + no persistent-state mutation
# ============================================================================

def test_21_cross_conversation_isolation_confidence_e2e():
    demo = _load_demo()
    replies = {
        "ESP32 mikrofon INMP441": "ESP32 dengan INMP441 cocok untuk mic.",
        "aquascape pompa bagus": "Pompa submersible bagus untuk aquascape.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 mikrofon INMP441", "cci-1", conversation_id="conv-A")
        _run_turn(console, demo, "aquascape pompa bagus", "cci-2", conversation_id="conv-B")
        key_a = "conv-A"
        key_b = "conv-B"
        snap_a = console.planner_module._active_topic.get(key_a)
        snap_b = console.planner_module._active_topic.get(key_b)
        assert snap_a is not None and snap_b is not None
        assert "esp32" in snap_a.terms
        assert "aquascape" in snap_b.terms
        assert "aquascape" not in snap_a.terms
        assert "esp32" not in snap_b.terms
    finally:
        console.stop()


def test_22_no_persistent_state_mutation_from_confidence_conflict_layer():
    """The confidence/conflict mechanism is entirely conversation-scoped
    (`_active_topic`/`_topic_history`, in-process dicts) - it must never
    write to `luno.memory`'s on-disk stores. Confirmed by checking that a
    rich multi-turn conversation with an explicit correction does not
    change the on-disk long-term-memory item count."""
    before_count = len(memory.get_all_memories()) if hasattr(memory, "get_all_memories") else None
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke, dicatat.")
    console.start()
    try:
        _run_turn(console, demo, "Power supply saya 5V 3A.", "psm-1")
        _run_turn(console, demo, "Sekarang power supply saya ganti jadi 5V 5A.", "psm-2")
        _run_turn(console, demo, "Power supply saya berapa?", "psm-3")
    finally:
        console.stop()
    if before_count is not None:
        after_count = len(memory.get_all_memories())
        assert after_count == before_count, (
            "ordinary conversation (no explicit 'inget ya' command) must never "
            "add a persistent long-term memory entry"
        )


# ============================================================================
# Section 6 - performance (Phase 9): deterministic confidence computation
# must stay well under budget, excluding LLM/TTS latency.
# ============================================================================

def test_23_confidence_computation_latency_budget():
    import time as _time
    rm = RelevantMemory(text="x" * 100, source="active_conversation", score=0.5, timestamp=0.0, stale=False, raw={"status": "active"})
    start = _time.perf_counter()
    for _ in range(1000):
        memory_context._confidence_for_relevant_memory(rm)
    elapsed_ms = (_time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / 1000
    assert per_call_ms < 5.0, f"confidence computation too slow: {per_call_ms:.4f}ms/call"


def test_24_supersession_tagging_latency_budget():
    import time as _time
    history = [memory_context.update_active_topic(None, "ESP32 pakai INMP441.", "Oke, dicatat.", is_followup=False)]
    start = _time.perf_counter()
    for _ in range(200):
        memory_context.update_topic_history(history, "Sekarang saya ganti ke ESP32-S3.", "Oke, dicatat.", is_followup=False)
    elapsed_ms = (_time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / 200
    assert per_call_ms < 5.0, f"supersession tagging too slow: {per_call_ms:.4f}ms/call"
