"""
tests/test_real_world_capture.py
===================================

Sprint 50 (Runtime Observability, Test Logging & Real-World Data
Capture) - Phase 15's own dedicated test suite for `luno.test_capture`
(Phase 7/8/11): `mark_test_case()`, id allocation, status-lifecycle
gating (`candidate -> reviewed -> approved -> rejected`), and the E2E
`/mark_test` mechanism through both consoles.
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
from typing import Callable

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.core import Event  # noqa: E402
from luno import test_capture  # noqa: E402


def _load_demo():
    demo_spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_capture", os.path.join(_ROOT, "main_runtime_demo.py")
    )
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules["main_runtime_demo_capture"] = demo
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


def _run_turn(console, demo, text, request_id, conversation_id=None, log_as_user=True):
    """Same convention as the other Sprint 50 test files, PLUS appends
    to `console.conversation_log` itself (mirroring what the real
    `speech_recognized -> _wire_console_listeners()` path already does
    for an interactive session - this harness bypasses that path by
    publishing `user_utterance` directly, same as every Sprint 44-49
    probe script, so the USER line has to be added explicitly here)."""
    if log_as_user:
        console.conversation_log.append(("USER", text))
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


@pytest.fixture
def tmp_base_dir():
    d = tempfile.mkdtemp(prefix="luno_sprint50_rw_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────────────────────────
#  1 - mark_test_case E2E
# ─────────────────────────────────────────────

def test_01_e2e_mark_test_case_captures_conversation_and_actual(tmp_base_dir):
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "turn01-1", conversation_id="conv01")
        _run_turn(console, demo, "Kalau koneksinya?", "turn01-2", conversation_id="conv01")
        _run_turn(console, demo, "Yang wireless?", "turn01-3", conversation_id="conv01")
        case = console.mark_test(conversation_id="conv01", note="wireless attribute reference",
                                  scenario="entity_continuity", base_dir=tmp_base_dir)
    finally:
        console.stop()

    assert case is not None
    assert case["status"] == "candidate"
    assert case["conversation"] == ["ESP32 pakai INMP441.", "Kalau koneksinya?", "Yang wireless?"]
    assert case["actual"]["reference_type"] == "attribute_reference"
    assert case["actual"]["topic_decision"] == "MERGE_ACTIVE_TOPIC"
    assert case["expected"] is None
    path = os.path.join(tmp_base_dir, "candidate", f"{case['id']}.json")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["id"] == case["id"]


def test_02_mark_test_case_none_when_nothing_to_capture(tmp_base_dir):
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        case = console.mark_test(conversation_id="never-happened", base_dir=tmp_base_dir)
    finally:
        console.stop()
    assert case is None


def test_03_mark_test_case_publishes_test_case_captured_event(tmp_base_dir):
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    captured = []
    sub = console.event_bus.subscribe("test_case_captured", lambda e: captured.append(e.data))
    try:
        _run_turn(console, demo, "Halo, apa kabar?", "turn03-1", conversation_id="conv03")
        console.mark_test(conversation_id="conv03", base_dir=tmp_base_dir)
        # EventBus dispatch is asynchronous (a background dispatcher
        # thread, same as every other event in this project - see
        # `console.event_bus.stats()['queue_size']`) - `publish()` itself
        # does not block until subscribers have run.
        assert _wait_until(lambda: len(captured) >= 1, 3.0)
    finally:
        console.event_bus.unsubscribe(sub)
        console.stop()
    assert len(captured) == 1
    assert captured[0]["status"] == "candidate"
    assert captured[0]["conversation_turn_count"] == 1


def test_04_conversation_id_none_falls_back_to_most_recent_turn(tmp_base_dir):
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        _run_turn(console, demo, "Halo pertama.", "turn04-1", conversation_id="conv04a")
        _run_turn(console, demo, "Halo kedua.", "turn04-2", conversation_id="conv04b")
        case = console.mark_test(base_dir=tmp_base_dir)  # no conversation_id
    finally:
        console.stop()
    assert case is not None
    assert case["conversation_id"] == "conv04b"  # the MOST RECENT turn overall


# ─────────────────────────────────────────────
#  2 - privacy / bounding on captured text
# ─────────────────────────────────────────────

def test_10_captured_conversation_line_is_bounded(tmp_base_dir):
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    huge_text = "kata " * 300  # far beyond _MAX_LINE_CHARS
    try:
        _run_turn(console, demo, huge_text, "turn10-1", conversation_id="conv10")
        case = console.mark_test(conversation_id="conv10", base_dir=tmp_base_dir)
    finally:
        console.stop()
    assert case is not None
    assert len(case["conversation"][0]) <= test_capture._MAX_LINE_CHARS + len("...[truncated]")


def test_11_conversation_capture_bounded_to_max_turns(tmp_base_dir):
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        for i in range(test_capture._MAX_CONVERSATION_TURNS + 5):
            _run_turn(console, demo, f"pesan nomor {i}", f"turn11-{i}", conversation_id="conv11")
        case = console.mark_test(conversation_id="conv11", base_dir=tmp_base_dir)
    finally:
        console.stop()
    assert case is not None
    assert len(case["conversation"]) == test_capture._MAX_CONVERSATION_TURNS
    # Most recent turns kept, oldest dropped.
    assert case["conversation"][-1].endswith(str(test_capture._MAX_CONVERSATION_TURNS + 4))


# ─────────────────────────────────────────────
#  3 - id allocation / listing / status lifecycle
# ─────────────────────────────────────────────

def test_20_sequential_case_ids(tmp_base_dir):
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        _run_turn(console, demo, "Satu.", "turn20-1", conversation_id="conv20a")
        c1 = console.mark_test(conversation_id="conv20a", base_dir=tmp_base_dir)
        _run_turn(console, demo, "Dua.", "turn20-2", conversation_id="conv20b")
        c2 = console.mark_test(conversation_id="conv20b", base_dir=tmp_base_dir)
    finally:
        console.stop()
    assert c1["id"] == "real_000001"
    assert c2["id"] == "real_000002"


def test_21_list_cases_and_load_case_roundtrip(tmp_base_dir):
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        _run_turn(console, demo, "Halo.", "turn21-1", conversation_id="conv21")
        case = console.mark_test(conversation_id="conv21", base_dir=tmp_base_dir)
    finally:
        console.stop()
    ids = test_capture.list_cases(status="candidate", base_dir=tmp_base_dir)
    assert case["id"] in ids
    loaded = test_capture.load_case(case["id"], base_dir=tmp_base_dir)
    assert loaded["id"] == case["id"]
    assert loaded["conversation"] == case["conversation"]


def test_22_set_case_status_valid_transition_moves_file(tmp_base_dir):
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        _run_turn(console, demo, "Halo.", "turn22-1", conversation_id="conv22")
        case = console.mark_test(conversation_id="conv22", base_dir=tmp_base_dir)
    finally:
        console.stop()

    ok = test_capture.set_case_status(case["id"], "approved", base_dir=tmp_base_dir,
                                       annotated_expected={"reference_type": "unknown"})
    assert ok is True
    assert not os.path.isfile(os.path.join(tmp_base_dir, "candidate", f"{case['id']}.json"))
    approved_path = os.path.join(tmp_base_dir, "approved", f"{case['id']}.json")
    assert os.path.isfile(approved_path)
    with open(approved_path, encoding="utf-8") as fh:
        moved = json.load(fh)
    assert moved["status"] == "approved"
    assert moved["expected"] == {"reference_type": "unknown"}


def test_23_set_case_status_rejects_invalid_status(tmp_base_dir):
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        _run_turn(console, demo, "Halo.", "turn23-1", conversation_id="conv23")
        case = console.mark_test(conversation_id="conv23", base_dir=tmp_base_dir)
    finally:
        console.stop()
    ok = test_capture.set_case_status(case["id"], "totally_made_up_status", base_dir=tmp_base_dir)
    assert ok is False
    # Original file untouched.
    assert os.path.isfile(os.path.join(tmp_base_dir, "candidate", f"{case['id']}.json"))


def test_24_set_case_status_unknown_case_id_returns_false(tmp_base_dir):
    assert test_capture.set_case_status("real_999999", "approved", base_dir=tmp_base_dir) is False


def test_25_a_candidate_case_never_auto_promotes(tmp_base_dir):
    """Phase 11's own core guarantee: marking a case creates it as
    `"candidate"` and NOTHING in this module ever moves it further on
    its own - only an explicit `set_case_status()` call does."""
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        _run_turn(console, demo, "Halo.", "turn25-1", conversation_id="conv25")
        case = console.mark_test(conversation_id="conv25", base_dir=tmp_base_dir)
    finally:
        console.stop()
    assert case["status"] == "candidate"
    assert test_capture.list_cases(status="approved", base_dir=tmp_base_dir) == []


# ─────────────────────────────────────────────
#  4 - ProductionConsole's own /mark_test command (thin relay)
# ─────────────────────────────────────────────

def test_30_production_console_mark_test_command_is_a_thin_relay(monkeypatch):
    """Doesn't spin up a full `ProductionConsole` (heavy - needs a real
    bootstrap Runtime/AdapterManager/module set); instead proves the
    THIN RELAY contract directly: `handle_line("/mark_test note")` must
    call `mark_test_case()` with the typed note, nothing more."""
    import luno.bootstrap.console as console_mod

    calls = []

    def _fake_mark_test_case(console, note="", **kwargs):
        calls.append(note)
        return {"id": "real_000001", "conversation": ["x"]}

    monkeypatch.setattr("luno.test_capture.mark_test_case", _fake_mark_test_case)

    class _FakeConsole:
        mark_test = console_mod.ProductionConsole.mark_test

    _FakeConsole().mark_test(note="my annotation")
    assert calls == ["my annotation"]
