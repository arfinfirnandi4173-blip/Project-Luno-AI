"""
tests/test_replay_engine.py
==============================

Sprint 50 (Runtime Observability, Test Logging & Real-World Data
Capture) - Phase 15's own dedicated test suite for `luno.replay`
(Phase 9/10): PASS/FAIL/REVIEW verdicts, the expected-vs-actual diff
format, and Phase 11's own "only `approved` cases ever get swept into
automated regression" gate.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.replay import ReplayResult, format_diff, replay_all, replay_case  # noqa: E402
from luno import test_capture  # noqa: E402


@pytest.fixture
def tmp_base_dir():
    d = tempfile.mkdtemp(prefix="luno_sprint50_replay_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


_ESP32_CASE = {
    "id": "real_t01",
    "conversation": ["ESP32 pakai INMP441.", "Kalau koneksinya?", "Yang wireless?"],
    "expected": {
        "reference_type": "attribute_reference",
        "decision": "MERGE_ACTIVE_TOPIC",
        "required_terms": ["esp32", "inmp441"],
    },
}

_AQUASCAPE_CASE = {
    "id": "real_t02",
    "conversation": ["Aquascape A pakai pompa kecil.", "Aquascape B pakai pompa besar.", "Pompanya gimana?"],
    "expected": {"topic_decision": "NO_CANDIDATE", "ambiguity_refusal": True},
}

_UNANNOTATED_CASE = {
    "id": "real_t03",
    "conversation": ["Halo."],
    "expected": None,
}


# ─────────────────────────────────────────────
#  1 - replay_case verdicts
# ─────────────────────────────────────────────

def test_01_replay_pass_when_actual_matches_expected():
    r = replay_case(_ESP32_CASE)
    assert isinstance(r, ReplayResult)
    assert r.result == "PASS"
    assert r.primary_difference == ""
    assert r.actual["reference_type"] == "attribute_reference"
    assert r.actual["topic_decision"] == "MERGE_ACTIVE_TOPIC"


def test_02_replay_pass_for_ambiguity_refusal_expectation():
    """Proves replay can also PASS on an expected REFUSAL, not just an
    expected merge - the Sprint 49 Aquascape A/B gate, replayed and
    confirmed via this sprint's own tooling."""
    r = replay_case(_AQUASCAPE_CASE)
    assert r.result == "PASS"
    assert r.actual["ambiguity_refusal"] is True


def test_03_replay_fail_reports_primary_difference():
    wrong_case = {
        "id": "real_t04",
        "conversation": ["Aquascape A pakai pompa kecil.", "Aquascape B pakai pompa besar.", "Pompanya gimana?"],
        "expected": {"topic_decision": "MERGE_ACTIVE_TOPIC"},  # deliberately wrong
    }
    r = replay_case(wrong_case)
    assert r.result == "FAIL"
    assert "topic_decision" in r.primary_difference
    assert "MERGE_ACTIVE_TOPIC" in r.primary_difference


def test_04_replay_fail_required_terms_reports_missing_subset():
    wrong_case = {
        "id": "real_t05",
        "conversation": ["ESP32 pakai INMP441.", "Kalau koneksinya?", "Yang wireless?"],
        "expected": {"required_terms": ["esp32", "a_term_that_will_never_appear"]},
    }
    r = replay_case(wrong_case)
    assert r.result == "FAIL"
    assert "a_term_that_will_never_appear" in r.primary_difference


def test_05_replay_review_when_case_unannotated():
    r = replay_case(_UNANNOTATED_CASE)
    assert r.result == "REVIEW"
    assert r.expected is None
    assert "no annotated expected" in r.primary_difference.lower()


def test_06_replay_never_calls_a_real_llm():
    """Replay must be fully deterministic - proven by running the SAME
    case twice and getting byte-identical `actual` output both times
    (a real LLM call would not be guaranteed deterministic)."""
    r1 = replay_case(_ESP32_CASE)
    r2 = replay_case(_ESP32_CASE)
    assert r1.actual == r2.actual
    assert r1.result == r2.result == "PASS"


# ─────────────────────────────────────────────
#  2 - format_diff
# ─────────────────────────────────────────────

def test_10_format_diff_contains_required_sections():
    r = replay_case(_ESP32_CASE)
    text = format_diff(r)
    assert "CASE: real_t01" in text
    assert "EXPECTED:" in text
    assert "ACTUAL:" in text
    assert "RESULT:" in text
    assert "PASS" in text


def test_11_format_diff_shows_primary_and_secondary_on_fail():
    wrong_case = {
        "id": "real_t06",
        "conversation": ["Aquascape A pakai pompa kecil.", "Aquascape B pakai pompa besar.", "Pompanya gimana?"],
        "expected": {"topic_decision": "MERGE_ACTIVE_TOPIC", "reference_type": "unknown"},
    }
    r = replay_case(wrong_case)
    text = format_diff(r)
    assert "PRIMARY DIFFERENCE:" in text
    if r.secondary_difference:
        assert "SECONDARY DIFFERENCE:" in text


# ─────────────────────────────────────────────
#  3 - replay_all + Phase 11's own approved-only gate
# ─────────────────────────────────────────────

def test_20_replay_all_empty_corpus_is_a_no_op_not_a_failure(tmp_base_dir):
    results = replay_all(base_dir=tmp_base_dir)
    assert results == []


def test_21_replay_all_only_reads_approved_by_default(tmp_base_dir):
    """A `candidate`-status case sitting in the tree must NOT be picked
    up by the default sweep - Phase 11's own core guarantee, verified
    from the replay side (the capture side's own equivalent guarantee is
    `test_real_world_capture.py::test_25`)."""
    import json
    candidate_dir = os.path.join(tmp_base_dir, "candidate")
    os.makedirs(candidate_dir, exist_ok=True)
    with open(os.path.join(candidate_dir, "real_000001.json"), "w", encoding="utf-8") as fh:
        json.dump({**_ESP32_CASE, "id": "real_000001", "status": "candidate"}, fh)

    assert replay_all(base_dir=tmp_base_dir) == []  # candidate-only tree - untouched by default sweep
    assert replay_all(status="candidate", base_dir=tmp_base_dir) != []  # explicit override still works


def test_22_replay_all_sweeps_approved_cases_and_reports_verdicts(tmp_base_dir):
    for case, case_id in ((_ESP32_CASE, "real_000001"), (_AQUASCAPE_CASE, "real_000002")):
        approved_dir = os.path.join(tmp_base_dir, "approved")
        os.makedirs(approved_dir, exist_ok=True)
        import json
        with open(os.path.join(approved_dir, f"{case_id}.json"), "w", encoding="utf-8") as fh:
            json.dump({**case, "id": case_id, "status": "approved"}, fh)

    results = replay_all(base_dir=tmp_base_dir)
    assert len(results) == 2
    assert all(r.result == "PASS" for r in results)


def test_23_e2e_capture_then_approve_then_replay_full_loop(tmp_base_dir):
    """The complete Phase 18 "closed loop" this sprint exists to enable:
    REAL CONVERSATION -> LOGGED -> MARKED AS TEST CASE -> APPROVED ->
    REPLAYED - exercised end-to-end through the real production console,
    not synthetic case dicts."""
    import importlib.util
    import threading
    import time

    demo_spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_replay_loop", os.path.join(_ROOT, "main_runtime_demo.py")
    )
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules["main_runtime_demo_replay_loop"] = demo
    demo_spec.loader.exec_module(demo)
    from luno.adapters import MockOpenRouterClient

    console = demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text="Oke.", chunk_delay_s=0.0))
    console.start()

    def _run(text, request_id, conv_id):
        console.conversation_log.append(("USER", text))
        need_llm = threading.Event()
        sub = console.event_bus.subscribe("need_llm_response", lambda e: need_llm.set() if e.get("request_id") == request_id else None)
        try:
            console.event_bus.publish(demo.Event(type="user_utterance", data={"text": text, "request_id": request_id, "conversation_id": conv_id}))
            deadline = time.time() + 5.0
            while time.time() < deadline and request_id in console.planner_module._pending_turns:
                time.sleep(0.02)
        finally:
            console.event_bus.unsubscribe(sub)

    try:
        _run("ESP32 pakai INMP441.", "loop-1", "loop-conv")
        _run("Kalau koneksinya?", "loop-2", "loop-conv")
        _run("Yang wireless?", "loop-3", "loop-conv")
        case = console.mark_test(conversation_id="loop-conv", scenario="entity_continuity", base_dir=tmp_base_dir)
    finally:
        console.stop()

    assert case is not None
    ok = test_capture.set_case_status(
        case["id"], "approved", base_dir=tmp_base_dir,
        annotated_expected={"reference_type": "attribute_reference", "decision": "MERGE_ACTIVE_TOPIC"},
    )
    assert ok is True

    results = replay_all(base_dir=tmp_base_dir)
    assert len(results) == 1
    assert results[0].result == "PASS"
