"""
test_temporal_memory_timeline_awareness.py
============================================

SPRINT 41 - TEMPORAL MEMORY & TIMELINE AWARENESS.

Covers: `luno.memory.classify_temporal_status()`, `is_current_state_query()`,
`is_planned_query()`, `is_historical_statement()`, the interrogative-gating
fix to `is_correction_signal()`; `luno.memory_context`'s extended
`ActiveTopicSnapshot.status` value set (active/superseded/planned/completed/
cancelled), `_STATUS_CONFIDENCE`, the `_STATUS_LABELS` rendering inside
`active_topic_to_relevant_memory()`, the temporal-aware dispatch in
`update_topic_history()` (planned/completed/cancelled retagging), the
compound-clause split (`_split_temporal_clauses()`/
`_build_compound_clause_entries()`), and `select_temporal_fallback_candidate()`
plus its call-site wiring in `main_runtime_demo.py`.

Root cause (Phase 2, reproduced live via RuntimeDemoConsole before any code
changed): the existing `_active_topic`/`_topic_history` layer had a
two-value temporal axis (active/superseded, Sprint 40) but no notion of
PLANNED or COMPLETED, `is_correction_signal()`'s bare "sekarang" alternative
false-fired on ordinary CURRENT-state QUESTIONS (not just declarative
corrections), and `select_topic_candidates()`'s pure lexical-overlap
eligibility check had no fallback for a temporal query whose own wording
differs from the original statement's wording ("Sebelumnya aku pakai apa?"
shares no token with "Aku pakai RTX 3060 Ti."). A single compound sentence
naming multiple distinct temporal facts about the same subject also
collapsed into one blended topic-history entry with a single whole-turn
status, losing the individual facts.

Per this sprint's own Critical Rule ("do not invent a complicated state
machine unless evidence requires it... the objective is not to maximize
code changes"), every mechanism here is additive and evidence-backed:
- reuses the EXISTING `status` field (extended value set), not a new field.
- reuses `classify_temporal_status()`'s own marker lists for compound-
  clause classification (`_classify_clause_temporal_role()`), no new
  vocabulary.
- `select_temporal_fallback_candidate()` and the compound-clause split are
  strictly LAST-RESORT / narrowly-gated additions that fall through
  unchanged for the overwhelming majority of ordinary single-fact turns.
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


def _load_demo():
    demo_spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_temporal", os.path.join(_ROOT, "main_runtime_demo.py"),
    )
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules["main_runtime_demo_temporal"] = demo
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


def _lines_starting(sp: str, *prefixes: str) -> list:
    out = []
    for line in sp.splitlines():
        s = line.strip()
        if any(s.startswith(p) for p in prefixes):
            out.append(s)
    return out


# ============================================================================
# Section 1 - classify_temporal_status() (Phase 4 classifier)
# ============================================================================

def test_01_classify_temporal_status_none_for_ordinary_current_statement():
    assert memory.classify_temporal_status("Sekarang aku pakai RTX 4070.") == "none"


def test_02_classify_temporal_status_planned_time_marker():
    assert memory.classify_temporal_status("Minggu depan aku mau ganti ke RTX 5070.") == "planned"


def test_03_classify_temporal_status_planned_intent_marker():
    assert memory.classify_temporal_status("Aku berencana upgrade GPU.") == "planned"


def test_04_classify_temporal_status_planned_verb_combo():
    assert memory.classify_temporal_status("Aku mau ganti GPU-nya.") == "planned"


def test_05_classify_temporal_status_bare_mau_is_not_planned():
    """Sprint 39's own established caution: a bare 'mau' without a
    change-shaped verb must NOT trigger PLANNED (too common a phrasing
    for anything else, e.g. 'aku mau tanya')."""
    assert memory.classify_temporal_status("Aku mau tanya soal GPU.") == "none"


def test_06_classify_temporal_status_completed():
    assert memory.classify_temporal_status("Sudah aku pindah ke ESP32-S3.") == "completed"


def test_07_classify_temporal_status_cancelled():
    assert memory.classify_temporal_status("Jadi beli RTX 5070 batal.") == "cancelled"


def test_08_classify_temporal_status_precedence_cancelled_over_completed():
    assert memory.classify_temporal_status("Sudah dibatalkan rencananya.") == "cancelled"


def test_09_classify_temporal_status_empty_text():
    assert memory.classify_temporal_status("") == "none"
    assert memory.classify_temporal_status(None) == "none"


def test_10_classify_temporal_status_domain_generic():
    domains = [
        "Minggu depan aku mau ganti mic ke condenser.",
        "Minggu depan aku mau ganti filter aquascape ke canister.",
        "Minggu depan aku mau ganti router ke mesh system.",
    ]
    for text in domains:
        assert memory.classify_temporal_status(text) == "planned", text


# ============================================================================
# Section 2 - is_correction_signal() interrogative-gating fix (root cause)
# ============================================================================

def test_11_correction_signal_bare_sekarang_still_fires_on_statement():
    assert memory.is_correction_signal("Sekarang aku pakai RTX 4070.") is True


def test_12_correction_signal_bare_sekarang_does_not_fire_on_question():
    """Root-cause regression test - live reproduction found
    'Sekarang aku pakai GPU apa?' wrongly triggered supersession tagging
    merely because it shared a token with an unrelated planned entry."""
    assert memory.is_correction_signal("Sekarang aku pakai GPU apa?") is False


def test_13_correction_signal_explicit_phrases_unaffected_by_question_shape():
    """Every OTHER correction phrase remains an unconditional signal
    regardless of question shape - only the bare-'sekarang' alternative
    is gated."""
    assert memory.is_correction_signal("Power supply-nya ganti menjadi 5V 5A, kan?") is True
    assert memory.is_correction_signal("Dulu pakai GTX 1070, sekarang RTX 3060 Ti, betul?") is True


def test_14_correction_signal_empty_text():
    assert memory.is_correction_signal("") is False
    assert memory.is_correction_signal(None) is False


def test_15_is_interrogative_detects_question_mark_and_words():
    assert memory._is_interrogative("sekarang aku pakai gpu apa?") is True
    assert memory._is_interrogative("gimana caranya?") is True
    assert memory._is_interrogative("sekarang aku pakai rtx 4070.") is False


# ============================================================================
# Section 3 - is_current_state_query() / is_planned_query() / is_historical_statement()
# ============================================================================

def test_16_is_current_state_query_true_for_sekarang_question():
    assert memory.is_current_state_query("Sekarang aku pakai GPU apa?") is True


def test_17_is_current_state_query_false_for_sekarang_statement():
    assert memory.is_current_state_query("Sekarang aku pakai RTX 4070.") is False


def test_18_is_current_state_query_false_without_marker():
    assert memory.is_current_state_query("GPU apa yang bagus?") is False


def test_19_is_planned_query_true_for_yang_mau():
    assert memory.is_planned_query("GPU yang mau aku beli apa?") is True


def test_20_is_planned_query_true_for_rencana():
    assert memory.is_planned_query("Rencana upgrade ke apa?") is True


def test_21_is_planned_query_false_for_statement():
    assert memory.is_planned_query("Aku berencana upgrade GPU.") is False


def test_22_is_historical_statement_true_for_dulu():
    assert memory.is_historical_statement("Aku dulu pakai GTX 1070.") is True


def test_23_is_historical_statement_false_for_current():
    assert memory.is_historical_statement("Aku sekarang pakai RTX 3060 Ti.") is False


def test_24_is_historical_statement_empty_text():
    assert memory.is_historical_statement("") is False
    assert memory.is_historical_statement(None) is False


# ============================================================================
# Section 4 - ActiveTopicSnapshot.status extended value set + confidence
# ============================================================================

def test_25_status_confidence_planned_between_active_and_superseded():
    rm_planned = _fake_relevant_memory(status="planned")
    rm_active = _fake_relevant_memory(status="active")
    rm_superseded = _fake_relevant_memory(status="superseded")
    c_planned = memory_context._confidence_for_relevant_memory(rm_planned)
    c_active = memory_context._confidence_for_relevant_memory(rm_active)
    c_superseded = memory_context._confidence_for_relevant_memory(rm_superseded)
    assert c_superseded < c_planned < c_active


def test_26_status_confidence_completed_equals_active():
    rm_completed = _fake_relevant_memory(status="completed")
    rm_active = _fake_relevant_memory(status="active")
    assert memory_context._confidence_for_relevant_memory(rm_completed) == memory_context._confidence_for_relevant_memory(rm_active)


def test_27_status_confidence_cancelled_equals_superseded():
    rm_cancelled = _fake_relevant_memory(status="cancelled")
    rm_superseded = _fake_relevant_memory(status="superseded")
    assert memory_context._confidence_for_relevant_memory(rm_cancelled) == memory_context._confidence_for_relevant_memory(rm_superseded)


def _fake_relevant_memory(status):
    from luno.memory_retrieval.models import RelevantMemory
    return RelevantMemory(source="active_conversation", text="x", score=1.0, raw={"status": status})


def test_28_historical_flag_true_for_superseded_and_cancelled_only():
    for status in ("superseded", "cancelled"):
        rm = _fake_relevant_memory(status=status)
        item = memory_context.relevant_memory_to_context_item(rm)
        assert item.historical is True, status
    for status in ("active", "completed", "planned"):
        rm = _fake_relevant_memory(status=status)
        item = memory_context.relevant_memory_to_context_item(rm)
        assert item.historical is False, status


def test_29_active_topic_to_relevant_memory_labels_all_five_statuses():
    expectations = {
        "active": "Active conversation topic:",
        "superseded": "Previously stated",
        "planned": "Planned (not yet current)",
        "completed": "Completed (previously planned)",
        "cancelled": "Cancelled (no longer an active plan)",
    }
    for status, expected_prefix in expectations.items():
        snap = memory_context.ActiveTopicSnapshot(terms=frozenset({"x"}), status=status, source_sentence="X saya.")
        rm = memory_context.active_topic_to_relevant_memory(snap, turn_id="t1")
        assert rm is not None
        assert expected_prefix in rm.text, status


# ============================================================================
# Section 5 - update_topic_history() temporal dispatch (Phase 5)
# ============================================================================

def test_30_planned_turn_pushes_planned_never_retags_front():
    history = [memory_context.update_active_topic(None, "Aku sekarang pakai RTX 3060 Ti.", "Oke.", is_followup=False)]
    updated = memory_context.update_topic_history(
        history, "Minggu depan aku mau ganti ke RTX 5070.", "Oke.", is_followup=False,
    )
    assert updated[0].status == "planned"
    assert updated[1].status == "active"  # front NOT retagged by a planned turn


def test_31_completed_turn_retags_matching_planned_front():
    history = memory_context.update_topic_history(
        None, "Minggu depan aku mau pindah ke ESP32-S3.", "Oke.", is_followup=False,
    )
    assert history[0].status == "planned"
    updated = memory_context.update_topic_history(
        history, "Sudah aku pindah ke ESP32-S3.", "Oke.", is_followup=False,
    )
    assert updated[0].status == "active"
    assert updated[1].status == "completed"


def test_32_completed_turn_without_overlap_does_not_retag():
    history = memory_context.update_topic_history(
        None, "Minggu depan aku mau pindah ke ESP32-S3.", "Oke.", is_followup=False,
    )
    updated = memory_context.update_topic_history(
        history, "Sudah makan siang tadi.", "Oke.", is_followup=False,
    )
    assert updated[1].status == "planned"  # unrelated completion must not retag


def test_33_cancelled_turn_retags_matching_planned_front():
    history = memory_context.update_topic_history(
        None, "Minggu depan aku mau beli RTX 5070.", "Oke.", is_followup=False,
    )
    updated = memory_context.update_topic_history(
        history, "Jadi beli RTX 5070 batal.", "Oke.", is_followup=False,
    )
    assert updated[1].status == "cancelled"


def test_34_cancelled_never_deletes_the_entry():
    history = memory_context.update_topic_history(
        None, "Minggu depan aku mau beli RTX 5070.", "Oke.", is_followup=False,
    )
    updated = memory_context.update_topic_history(
        history, "Jadi beli RTX 5070 batal.", "Oke.", is_followup=False,
    )
    assert len(updated) == 2  # cancelled entry still present, not removed


def test_35_ordinary_correction_supersession_unaffected():
    """Sprint 40's own supersession-tagging behavior must be byte-for-
    byte unchanged for the ordinary (non-temporal) correction case."""
    history = [memory_context.update_active_topic(None, "ESP32 pakai INMP441.", "Oke, dicatat.", is_followup=False)]
    updated = memory_context.update_topic_history(
        history, "Sekarang saya ganti ke ESP32-S3.", "Oke, dicatat.", is_followup=False,
    )
    assert updated[1].status == "superseded"


def test_36_current_state_question_does_not_wrongly_supersede():
    """Root-cause regression - a CURRENT-state QUESTION sharing a token
    with a planned entry must never retag it 'superseded'."""
    history = memory_context.update_topic_history(
        None, "Sekarang aku pakai RTX 3060 Ti.", "Oke.", is_followup=False,
    )
    history = memory_context.update_topic_history(
        history, "Minggu depan aku mau ganti ke RTX 5070.", "Oke.", is_followup=False,
    )
    updated = memory_context.update_topic_history(
        history, "Sekarang aku pakai GPU apa?", "Kamu pakai RTX 3060 Ti.", is_followup=False,
    )
    statuses = {e.status for e in updated}
    assert "superseded" not in statuses


# ============================================================================
# Section 6 - compound-clause split (Phase 4/7, Scenario F)
# ============================================================================

def test_37_split_temporal_clauses_single_sentence_returns_empty():
    assert memory_context._split_temporal_clauses("Aku pakai RTX 3060 Ti.") == []


def test_38_split_temporal_clauses_multi_sentence():
    clauses = memory_context._split_temporal_clauses(
        "Aku dulu pakai GTX 1070. Sekarang pakai RTX 3060 Ti. Bulan depan rencana upgrade ke RTX 5070.",
    )
    assert len(clauses) == 3


def test_39_classify_clause_temporal_role():
    assert memory_context._classify_clause_temporal_role("Aku dulu pakai GTX 1070.") == "superseded"
    assert memory_context._classify_clause_temporal_role("Sekarang pakai RTX 3060 Ti.") == "active"
    assert memory_context._classify_clause_temporal_role("Bulan depan rencana upgrade ke RTX 5070.") == "planned"


def test_40_build_compound_clause_entries_none_for_single_fact_turn():
    """Common case: a single-sentence turn must never trigger the split
    path - `None` means 'fall through unchanged'."""
    assert memory_context._build_compound_clause_entries("Aku pakai RTX 3060 Ti.") is None


def test_41_build_compound_clause_entries_none_when_all_clauses_share_role():
    """Two current-fact sentences back to back - no temporal ambiguity,
    must not trigger the split path either."""
    result = memory_context._build_compound_clause_entries(
        "Aku pakai RTX 3060 Ti. Enak banget performanya.",
    )
    assert result is None


def test_42_build_compound_clause_entries_splits_three_distinct_roles():
    result = memory_context._build_compound_clause_entries(
        "Aku dulu pakai GTX 1070. Sekarang pakai RTX 3060 Ti. Bulan depan rencana upgrade ke RTX 5070.",
    )
    assert result is not None
    roles = {status for _terms, status, _source in result}
    assert roles == {"superseded", "active", "planned"}


def test_43_update_topic_history_pushes_one_entry_per_clause():
    updated = memory_context.update_topic_history(
        None,
        "Aku dulu pakai GTX 1070. Sekarang pakai RTX 3060 Ti. Bulan depan rencana upgrade ke RTX 5070.",
        "Oke, dicatat.",
        is_followup=False,
    )
    statuses = sorted(e.status for e in updated)
    assert statuses == sorted(["superseded", "active", "planned"])


def test_44_update_topic_history_single_fact_turn_pushes_exactly_one_entry():
    updated = memory_context.update_topic_history(
        None, "Aku pakai RTX 3060 Ti.", "Oke.", is_followup=False,
    )
    assert len(updated) == 1


# ============================================================================
# Section 7 - select_temporal_fallback_candidate() (Phase 6 retrieval)
# ============================================================================

def test_45_temporal_fallback_none_for_empty_history():
    assert memory_context.select_temporal_fallback_candidate([], "Sebelumnya aku pakai apa?") is None
    assert memory_context.select_temporal_fallback_candidate(None, "Sebelumnya aku pakai apa?") is None


def test_46_temporal_fallback_none_for_non_temporal_query():
    history = memory_context.update_topic_history(None, "Aku pakai RTX 3060 Ti.", "Oke.", is_followup=False)
    assert memory_context.select_temporal_fallback_candidate(history, "Terus?") is None


def test_47_temporal_fallback_finds_historical_entry():
    history = memory_context.update_topic_history(None, "Aku pakai RTX 3060 Ti.", "Oke.", is_followup=False)
    history = memory_context.update_topic_history(
        history, "Sekarang aku ganti ke RTX 4070.", "Oke.", is_followup=False,
    )
    result = memory_context.select_temporal_fallback_candidate(history, "Sebelumnya aku pakai apa?")
    assert result is not None
    assert result.status == "superseded"


def test_48_temporal_fallback_finds_current_entry_including_completed():
    history = memory_context.update_topic_history(
        None, "Minggu depan aku mau pindah ke ESP32-S3.", "Oke.", is_followup=False,
    )
    history = memory_context.update_topic_history(
        history, "Sudah aku pindah ke ESP32-S3.", "Oke.", is_followup=False,
    )
    result = memory_context.select_temporal_fallback_candidate(history, "Sekarang aku pakai board apa?")
    assert result is not None
    assert result.status in ("active", "completed")


def test_49_temporal_fallback_finds_planned_entry():
    history = memory_context.update_topic_history(
        None, "Minggu depan aku mau ganti ke RTX 5070.", "Oke.", is_followup=False,
    )
    result = memory_context.select_temporal_fallback_candidate(history, "Rencana upgrade ke apa?")
    assert result is not None
    assert result.status == "planned"


def test_50_temporal_fallback_none_when_no_matching_status():
    history = memory_context.update_topic_history(None, "Aku pakai RTX 3060 Ti.", "Oke.", is_followup=False)
    assert memory_context.select_temporal_fallback_candidate(history, "Rencana upgrade ke apa?") is None


# ============================================================================
# Section 8 - E2E Scenarios A-F via RuntimeDemoConsole (Phase 1/11)
# ============================================================================

def test_51_e2e_scenario_A_current():
    demo = _load_demo()
    replies = {
        "Aku sekarang pakai RTX 3060 Ti.": "Oke, RTX 3060 Ti dicatat.",
        "Sekarang aku pakai GPU apa?": "Kamu sekarang pakai RTX 3060 Ti.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku sekarang pakai RTX 3060 Ti.", "sA-1")
        sp = _run_turn_capture_prompt(console, demo, "Sekarang aku pakai GPU apa?", "sA-2")
        active = _lines_starting(sp, "- Active conversation topic")
        assert active and "3060" in active[0]
    finally:
        console.stop()


def test_52_e2e_scenario_B_replacement_current_and_historical():
    demo = _load_demo()
    replies = {
        "Aku pakai RTX 3060 Ti.": "Oke, dicatat.",
        "Sekarang aku ganti ke RTX 4070.": "Oke, jadi sekarang RTX 4070.",
        "Sekarang aku pakai GPU apa?": "Kamu sekarang pakai RTX 4070.",
        "Sebelumnya aku pakai apa?": "Sebelumnya kamu pakai RTX 3060 Ti.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku pakai RTX 3060 Ti.", "sB-1")
        _run_turn(console, demo, "Sekarang aku ganti ke RTX 4070.", "sB-2")
        sp3 = _run_turn_capture_prompt(console, demo, "Sekarang aku pakai GPU apa?", "sB-3")
        active = _lines_starting(sp3, "- Active conversation topic")
        assert active and "4070" in active[0]
        sp4 = _run_turn_capture_prompt(console, demo, "Sebelumnya aku pakai apa?", "sB-4")
        hist = _lines_starting(sp4, "- Previously stated")
        assert hist and "3060" in hist[0], f"expected historical RTX 3060 Ti candidate, prompt had: {sp4}"
    finally:
        console.stop()


def test_53_e2e_scenario_C_planned_does_not_overwrite_current():
    demo = _load_demo()
    replies = {
        "Sekarang aku pakai RTX 3060 Ti.": "Oke, dicatat.",
        "Minggu depan aku mau ganti ke RTX 5070.": "Oke, dicatat rencananya.",
        "Sekarang aku pakai GPU apa?": "Kamu sekarang pakai RTX 3060 Ti.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Sekarang aku pakai RTX 3060 Ti.", "sC-1")
        _run_turn(console, demo, "Minggu depan aku mau ganti ke RTX 5070.", "sC-2")
        sp = _run_turn_capture_prompt(console, demo, "Sekarang aku pakai GPU apa?", "sC-3")
        active = _lines_starting(sp, "- Active conversation topic")
        assert active and "3060" in active[0], f"planned entry must not overwrite current, prompt had: {sp}"
    finally:
        console.stop()


def test_54_e2e_scenario_D_completed_plan_becomes_current():
    demo = _load_demo()
    replies = {
        "Minggu depan aku mau pindah ke ESP32-S3.": "Oke, dicatat rencananya.",
        "Sudah aku pindah ke ESP32-S3.": "Oke, sudah pindah ya.",
        "Sekarang aku pakai board apa?": "Kamu sekarang pakai ESP32-S3.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Minggu depan aku mau pindah ke ESP32-S3.", "sD-1")
        _run_turn(console, demo, "Sudah aku pindah ke ESP32-S3.", "sD-2")
        sp = _run_turn_capture_prompt(console, demo, "Sekarang aku pakai board apa?", "sD-3")
        active = _lines_starting(sp, "- Active conversation topic", "- Completed")
        assert active and "esp32" in active[0].lower(), f"prompt had: {sp}"
    finally:
        console.stop()


def test_55_e2e_scenario_E_cancelled_plan_marked_distinctly():
    demo = _load_demo()
    replies = {
        "Minggu depan aku mau beli RTX 5070.": "Oke, dicatat rencananya.",
        "Jadi beli RTX 5070 batal.": "Oke, dibatalkan ya.",
        "GPU yang mau aku beli apa?": "Kamu tidak punya rencana pembelian GPU saat ini.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Minggu depan aku mau beli RTX 5070.", "sE-1")
        _run_turn(console, demo, "Jadi beli RTX 5070 batal.", "sE-2")
        sp = _run_turn_capture_prompt(console, demo, "GPU yang mau aku beli apa?", "sE-3")
        cancelled = _lines_starting(sp, "- Cancelled")
        assert cancelled and "5070" in cancelled[0], f"expected a distinctly-labeled cancelled plan, prompt had: {sp}"
    finally:
        console.stop()


def test_56_e2e_scenario_F_three_temporal_states_distinguishable():
    demo = _load_demo()
    replies = {
        "Aku dulu pakai GTX 1070. Sekarang pakai RTX 3060 Ti. Bulan depan rencana upgrade ke RTX 5070.": "Oke, semua dicatat.",
        "Aku dulu pakai apa?": "Dulu kamu pakai GTX 1070.",
        "Sekarang pakai apa?": "Sekarang kamu pakai RTX 3060 Ti.",
        "Rencana upgrade ke apa?": "Rencana upgrade ke RTX 5070.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(
            console, demo,
            "Aku dulu pakai GTX 1070. Sekarang pakai RTX 3060 Ti. Bulan depan rencana upgrade ke RTX 5070.",
            "sF-1",
        )
        sp2 = _run_turn_capture_prompt(console, demo, "Aku dulu pakai apa?", "sF-2")
        hist = _lines_starting(sp2, "- Previously stated")
        assert hist and "1070" in hist[0], f"prompt had: {sp2}"

        sp3 = _run_turn_capture_prompt(console, demo, "Sekarang pakai apa?", "sF-3")
        active = _lines_starting(sp3, "- Active conversation topic")
        assert active and "3060" in active[0], f"prompt had: {sp3}"

        sp4 = _run_turn_capture_prompt(console, demo, "Rencana upgrade ke apa?", "sF-4")
        planned = _lines_starting(sp4, "- Planned")
        assert planned and "5070" in planned[0], f"prompt had: {sp4}"
    finally:
        console.stop()


# ============================================================================
# Section 9 - Ambiguity safety (Phase 8) - must never fabricate temporal context
# ============================================================================

def test_57_ambiguous_fragment_no_history_injects_nothing():
    """Mirrors Sprint 40's own precedent exactly (fresh console, single
    turn, empty history) - each fragment gets its OWN console so a
    fragment's own reply-merged topic-history push can never trivially
    self-match its own just-created entry within the same run."""
    demo = _load_demo()
    for frag in ("Yang mana?", "Kenapa?", "Terus?", "Masih ada?", "Apa?", "Gimana?", "Yang tadi?"):
        console = _new_console(demo, canned_text="Maksudnya yang mana ya?")
        console.start()
        try:
            sp = _run_turn_capture_prompt(console, demo, frag, f"amb-{frag}")
            assert "Active conversation topic" not in sp, f"{frag!r} fabricated context: {sp}"
            assert "[Historical Context]" not in sp, f"{frag!r} fabricated historical context: {sp}"
        finally:
            console.stop()


def test_58_temporal_word_alone_does_not_force_retrieval_without_evidence():
    """'sekarang'/'nanti' etc. appearing in an UNRELATED sentence must not
    cause an unrelated prior memory to be injected."""
    demo = _load_demo()
    replies = {
        "Aku pakai RTX 3060 Ti.": "Oke, dicatat.",
        "Besok aku kerja.": "Oke, semoga lancar kerjanya.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku pakai RTX 3060 Ti.", "tw-1")
        sp = _run_turn_capture_prompt(console, demo, "Besok aku kerja.", "tw-2")
        active = _lines_starting(sp, "- Active conversation topic")
        # "Besok aku kerja." shares no real vocabulary with the GPU topic -
        # if anything is injected, it must not be the unrelated GPU fact.
        if active:
            assert "rtx" not in active[0].lower()
    finally:
        console.stop()


# ============================================================================
# Section 10 - MANDATORY multi-topic / domain-generalization test (Phase 7/9)
# ============================================================================

_DOMAINS = {
    "PC/GPU": dict(
        historical="Aku dulu pakai GPU GTX 1070.",
        current="Sekarang aku pakai GPU RTX 3060 Ti.",
        planned="Minggu depan aku mau ganti GPU ke RTX 5070.",
        current_q="Sekarang aku pakai GPU apa?",
        historical_q="Dulu aku pakai GPU apa?",
        planned_q="Rencana ganti GPU ke apa?",
        current_kw="3060", historical_kw="1070", planned_kw="5070",
    ),
    "IoT/microcontroller": dict(
        historical="Aku dulu pakai board ESP8266.",
        current="Sekarang aku pakai board ESP32.",
        planned="Minggu depan aku mau ganti ke board ESP32-S3.",
        current_q="Sekarang aku pakai board apa?",
        historical_q="Dulu aku pakai board apa?",
        planned_q="Rencana ganti board ke apa?",
        current_kw="esp32", historical_kw="esp8266", planned_kw="esp32-s3",
    ),
    "Audio": dict(
        historical="Aku dulu pakai headphone Sony WH-1000XM4.",
        current="Sekarang aku pakai headphone Sennheiser HD 660S.",
        planned="Minggu depan aku mau ganti ke headphone Audeze LCD-2.",
        current_q="Sekarang aku pakai headphone apa?",
        historical_q="Dulu aku pakai headphone apa?",
        planned_q="Rencana ganti headphone ke apa?",
        current_kw="660s", historical_kw="xm4", planned_kw="lcd-2",
    ),
    "Aquascape": dict(
        historical="Aku dulu pakai filter hang-on-back di aquascape.",
        current="Sekarang aku pakai filter canister di aquascape.",
        planned="Minggu depan aku mau ganti ke filter sump.",
        current_q="Sekarang aku pakai filter apa?",
        historical_q="Dulu aku pakai filter apa?",
        planned_q="Rencana ganti filter ke apa?",
        current_kw="canister", historical_kw="hang-on-back", planned_kw="sump",
    ),
    "Software/network": dict(
        historical="Aku dulu pakai router TP-Link Archer.",
        current="Sekarang aku pakai router Ubiquiti UniFi.",
        planned="Minggu depan aku mau ganti ke router pfSense.",
        current_q="Sekarang aku pakai router apa?",
        historical_q="Dulu aku pakai router apa?",
        planned_q="Rencana ganti router ke apa?",
        current_kw="unifi", historical_kw="archer", planned_kw="pfsense",
    ),
}

import pytest  # noqa: E402


@pytest.mark.parametrize("domain_key,spec", list(_DOMAINS.items()))
def test_59_domain_generalization_current_historical_planned_distinguishable(domain_key, spec):
    demo = _load_demo()
    replies = {
        spec["historical"]: "Oke, dicatat.",
        spec["current"]: "Oke, dicatat.",
        spec["planned"]: "Oke, dicatat rencananya.",
        spec["current_q"]: "Sekarang kamu pakai yang baru.",
        spec["historical_q"]: "Dulu kamu pakai yang lama.",
        spec["planned_q"]: "Rencana upgrade ke yang baru.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, spec["historical"], f"{domain_key}-1")
        _run_turn(console, demo, spec["current"], f"{domain_key}-2")
        _run_turn(console, demo, spec["planned"], f"{domain_key}-3")

        sp_cur = _run_turn_capture_prompt(console, demo, spec["current_q"], f"{domain_key}-cur")
        active = _lines_starting(sp_cur, "- Active conversation topic")
        assert active and spec["current_kw"] in active[0].lower(), f"[{domain_key}] current: {sp_cur}"

        sp_hist = _run_turn_capture_prompt(console, demo, spec["historical_q"], f"{domain_key}-hist")
        hist = _lines_starting(sp_hist, "- Previously stated")
        assert hist and spec["historical_kw"] in hist[0].lower(), f"[{domain_key}] historical: {sp_hist}"

        sp_plan = _run_turn_capture_prompt(console, demo, spec["planned_q"], f"{domain_key}-plan")
        planned = _lines_starting(sp_plan, "- Planned")
        assert planned and spec["planned_kw"] in planned[0].lower(), f"[{domain_key}] planned: {sp_plan}"
    finally:
        console.stop()


@pytest.mark.parametrize("domain_key,spec", list(_DOMAINS.items()))
def test_60_domain_generalization_cross_topic_no_contamination(domain_key, spec):
    """A temporal query about ONE domain must never surface another,
    unrelated domain's memory - reproduces Phase 7's 5-domain
    contamination check using this file's own domain matrix."""
    other_key = next(k for k in _DOMAINS if k != domain_key)
    other_spec = _DOMAINS[other_key]
    demo = _load_demo()
    replies = {
        spec["current"]: "Oke, dicatat.",
        other_spec["current"]: "Oke, dicatat.",
        spec["current_q"]: "Sekarang kamu pakai yang baru.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, spec["current"], f"{domain_key}-x-1")
        _run_turn(console, demo, other_spec["current"], f"{domain_key}-x-2")
        sp = _run_turn_capture_prompt(console, demo, spec["current_q"], f"{domain_key}-x-3")
        active = _lines_starting(sp, "- Active conversation topic")
        assert active, f"[{domain_key}] expected a match, prompt had: {sp}"
        assert other_spec["current_kw"] not in active[0].lower(), (
            f"[{domain_key}] cross-topic contamination: {other_key}'s value leaked into {domain_key}'s query"
        )
    finally:
        console.stop()


def test_61_domains_used_in_this_file_cover_five_distinct_unrelated_areas():
    required = {"PC/GPU", "IoT/microcontroller", "Audio", "Aquascape", "Software/network"}
    assert required.issubset(set(_DOMAINS.keys()))


# ============================================================================
# Section 11 - STRUCTURAL NO-HARDCODING PROOF (Phase 9, brief's mandatory check)
# ============================================================================

_FORBIDDEN_ENTITY_TOKENS = (
    "esp8266", "esp32", "inmp441", "wled", "aquascape",
    "gtx", "rtx", "nvidia", "amd", "gpu_model",
)


def _strip_comments_and_docstrings(source: str) -> str:
    import ast
    import io
    import tokenize

    out_tokens = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except tokenize.TokenizeError:
        tokens = []
    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            continue
        out_tokens.append(tok)
    try:
        no_comments = tokenize.untokenize(out_tokens)
    except Exception:
        no_comments = source

    try:
        tree = ast.parse(no_comments)
    except SyntaxError:
        return no_comments
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        kept = []
        for stmt in body:
            if isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Constant) \
                    and isinstance(getattr(stmt.value, "value", None), str):
                continue
            kept.append(stmt)
        node.body = kept
    try:
        return ast.unparse(tree)
    except Exception:
        return no_comments


def test_62_temporal_code_has_no_hardcoded_entity_branches():
    """Structural proof - the actual EXECUTABLE code of every Sprint 41
    function must contain NO conditional branch keyed on any of the
    brief's own example entity names."""
    functions = [
        memory.classify_temporal_status,
        memory.is_current_state_query,
        memory.is_planned_query,
        memory.is_historical_statement,
        memory.is_correction_signal,
        memory._is_interrogative,
        memory_context.update_topic_history,
        memory_context._split_temporal_clauses,
        memory_context._classify_clause_temporal_role,
        memory_context._build_compound_clause_entries,
        memory_context.select_temporal_fallback_candidate,
        memory_context.active_topic_to_relevant_memory,
        memory_context._confidence_for_relevant_memory,
        memory_context.relevant_memory_to_context_item,
    ]
    for fn in functions:
        code_only = _strip_comments_and_docstrings(inspect.getsource(fn)).lower()
        for token in _FORBIDDEN_ENTITY_TOKENS:
            assert token not in code_only, (
                f"found hardcoded entity token {token!r} in {fn.__qualname__}()'s "
                "EXECUTABLE code (not just a comment/docstring example)"
            )


def test_63_planned_time_markers_are_generic_wording_not_entities():
    for token in _FORBIDDEN_ENTITY_TOKENS:
        assert token not in memory._PLANNED_TIME_MARKERS
        assert token not in memory._PLANNED_INTENT_MARKERS
        assert token not in memory._COMPLETED_MARKERS
        assert token not in memory._CANCELLED_MARKERS


# ============================================================================
# Section 12 - Performance (Phase 10, <5ms deterministic overhead target)
# ============================================================================

def test_64_classify_temporal_status_performance():
    text = "Minggu depan aku mau ganti ke RTX 5070, soalnya yang sekarang mulai lemot."
    n = 2000
    start = time.perf_counter()
    for _ in range(n):
        memory.classify_temporal_status(text)
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / n
    assert per_call_ms < 1.0, f"classify_temporal_status() too slow: {per_call_ms:.4f}ms/call"


def test_65_update_topic_history_temporal_dispatch_performance():
    history = memory_context.update_topic_history(
        None, "Minggu depan aku mau pindah ke ESP32-S3.", "Oke.", is_followup=False,
    )
    n = 500
    start = time.perf_counter()
    for _ in range(n):
        memory_context.update_topic_history(
            history, "Sudah aku pindah ke ESP32-S3.", "Oke.", is_followup=False,
        )
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / n
    assert per_call_ms < 5.0, f"update_topic_history() temporal dispatch too slow: {per_call_ms:.4f}ms/call"


def test_66_compound_clause_split_performance():
    text = "Aku dulu pakai GTX 1070. Sekarang pakai RTX 3060 Ti. Bulan depan rencana upgrade ke RTX 5070."
    n = 1000
    start = time.perf_counter()
    for _ in range(n):
        memory_context._build_compound_clause_entries(text)
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / n
    assert per_call_ms < 5.0, f"compound clause split too slow: {per_call_ms:.4f}ms/call"


def test_67_temporal_fallback_candidate_performance():
    history = memory_context.update_topic_history(
        None, "Aku pakai RTX 3060 Ti.", "Oke.", is_followup=False,
    )
    history = memory_context.update_topic_history(
        history, "Sekarang aku ganti ke RTX 4070.", "Oke.", is_followup=False,
    )
    n = 2000
    start = time.perf_counter()
    for _ in range(n):
        memory_context.select_temporal_fallback_candidate(history, "Sebelumnya aku pakai apa?")
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / n
    assert per_call_ms < 1.0, f"select_temporal_fallback_candidate() too slow: {per_call_ms:.4f}ms/call"
