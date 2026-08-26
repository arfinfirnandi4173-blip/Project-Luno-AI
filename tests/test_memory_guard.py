"""
test_memory_guard.py
======================

Automated tests for the Memory Guard sprint ("Memory stores verified
facts, not generated language.") - `luno/memory_guard.py`, plus a couple
of end-to-end checks through the real `RuntimeDemoConsole` pipeline
(same no-network/no-hardware conventions as `tests/test_runtime_demo.py`).

Covers the sprint's own Testing checklist: Store (verified success
disimpan + metadata benar), Block (verification_failed / timeout /
offline / unavailable - semuanya tidak masuk factual memory), Conflict
(state lama diganti, tidak ada duplikasi), Retrieval (verified
diprioritaskan, conversation tidak mengalahkan verified), Regression
(memory lama tetap bekerja, API publik tidak berubah).

No `unittest.mock` - fake `ToolResult`-shaped dicts (and, for one test,
a real `luno.tool_manager.result.ToolResult`) instead, matching this
project's own convention.

Run:
    python3 tests/test_memory_guard.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import traceback
from typing import Callable, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.memory_guard import VerifiedFactStore, should_store_verified_result  # noqa: E402
from luno.tool_manager.result import ToolResult  # noqa: E402

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _tmp_store() -> VerifiedFactStore:
    """A store backed by a throwaway temp file, never the real
    `config/verified_facts.json` - keeps these tests from touching (or
    depending on) the project's actual data."""
    fd, path = tempfile.mkstemp(prefix="verified_facts_test_", suffix=".json")
    os.close(fd)
    os.remove(path)  # VerifiedFactStore._load() tolerates a missing file
    return VerifiedFactStore(path=path)


def _ok(entity_id: str, actual_state: str, expected_state: str = None, **extra_data) -> dict:
    data = {"entity_id": entity_id, "actual_state": actual_state,
             "expected_state": expected_state or actual_state, **extra_data}
    return {"success": True, "tool": "home_assistant", "action": "turn_on", "message": "ok", "data": data}


def _failed(entity_id: str, failure_reason: str, expected_state="on", actual_state="off") -> dict:
    data = {"entity_id": entity_id, "expected_state": expected_state, "actual_state": actual_state,
             "failure_reason": failure_reason}
    return {"success": False, "tool": "home_assistant", "action": "turn_on", "message": "nope",
            "error_type": "VerificationFailed", "data": data}


# ============================================================================
# Bagian 1 - the Guard itself
# ============================================================================

@scenario
def test_guard_allows_verified_success():
    assert should_store_verified_result(_ok("light.bedroom", "on")) is True


@scenario
def test_guard_blocks_missing_or_unreadable_input():
    assert should_store_verified_result(None) is False
    assert should_store_verified_result({}) is False
    assert should_store_verified_result("not a tool result") is False


@scenario
def test_guard_accepts_real_toolresult_object_not_just_dict():
    """Duck-typed: a real `ToolResult` (attribute access, not `.get()`)
    must work exactly the same as the dict shape."""
    ok = ToolResult.ok("home_assistant", "turn_on", "I've turned on Bedroom Light.",
                        data={"entity_id": "light.bedroom", "actual_state": "on", "expected_state": "on"})
    fail = ToolResult.fail("home_assistant", "turn_on", "didn't respond",
                            data={"entity_id": "light.bedroom", "actual_state": "off", "expected_state": "on"})
    assert should_store_verified_result(ok) is True
    assert should_store_verified_result(fail) is False


# ============================================================================
# Bagian 9 - Store
# ============================================================================

@scenario
def test_store_verified_success():
    store = _tmp_store()
    fact = store.record(_ok("light.bedroom", "on"), tool_name="home_assistant", request_id="r1")
    assert fact is not None
    assert store.get("light.bedroom")["value"] == "on"


@scenario
def test_store_metadata_is_correct():
    """Bagian 5: verified=true, source=tool_result, tool_name,
    entity_id, timestamp, request_id all present and correct."""
    store = _tmp_store()
    fact = store.record(_ok("light.bedroom", "on"), tool_name="home_assistant", request_id="req-42")
    assert fact["entity_id"] == "light.bedroom"
    assert fact["value"] == "on"
    assert fact["verified"] is True
    assert fact["source"] == "tool_result"
    assert fact["tool_name"] == "home_assistant"
    assert fact["request_id"] == "req-42"
    assert fact["timestamp"]  # non-empty, ISO-ish string


# ============================================================================
# Bagian 9 - Block
# ============================================================================

@scenario
def test_block_verification_failed():
    store = _tmp_store()
    fact = store.record(_failed("light.bedroom", "Device did not reach the expected state in time"))
    assert fact is None
    assert store.get("light.bedroom") is None


@scenario
def test_block_timeout():
    store = _tmp_store()
    result = {"success": False, "tool": "home_assistant", "action": "turn_on", "message": "timed out",
              "error_type": "Timeout", "data": {"entity_id": "light.bedroom", "failure_reason": "timeout"}}
    assert store.record(result) is None
    assert store.get("light.bedroom") is None


@scenario
def test_block_offline():
    store = _tmp_store()
    result = {"success": False, "tool": "home_assistant", "action": "turn_on",
              "message": "I can't reach Home Assistant right now. Please check if the server is online.",
              "error_type": "HomeAssistantError",
              "data": {"entity_id": "light.bedroom", "failure_reason": "Home Assistant offline"}}
    assert store.record(result) is None
    assert store.get("light.bedroom") is None


@scenario
def test_block_unavailable():
    store = _tmp_store()
    result = {"success": False, "tool": "home_assistant", "action": "turn_on",
              "message": "Bedroom Light is currently unavailable.", "error_type": "VerificationFailed",
              "data": {"entity_id": "light.bedroom", "actual_state": "unavailable", "failure_reason": "Device unavailable"}}
    assert store.record(result) is None
    assert store.get("light.bedroom") is None
    assert store.all_facts() == []


@scenario
def test_block_never_leaves_a_stale_or_partial_fact():
    """A blocked write must never create or corrupt an entry - not even
    a partial one - for that entity_id."""
    store = _tmp_store()
    store.record(_ok("light.bedroom", "on"))  # legit fact first
    store.record(_failed("light.bedroom", "verification_failed", actual_state="off"))  # later failed attempt
    fact = store.get("light.bedroom")
    assert fact is not None and fact["value"] == "on"  # untouched by the failed attempt


# ============================================================================
# Bagian 9 - Conflict
# ============================================================================

@scenario
def test_conflict_old_state_is_replaced():
    store = _tmp_store()
    store.record(_ok("light.bedroom", "off"))
    store.record(_ok("light.bedroom", "on"))
    assert store.get("light.bedroom")["value"] == "on"


@scenario
def test_conflict_no_duplicate_entries():
    store = _tmp_store()
    store.record(_ok("light.bedroom", "off"))
    store.record(_ok("light.bedroom", "on"))
    store.record(_ok("light.bedroom", "off"))
    matching = [f for f in store.all_facts() if f["entity_id"] == "light.bedroom"]
    assert len(matching) == 1


# ============================================================================
# Bagian 9 - Retrieval
# ============================================================================

@scenario
def test_retrieval_only_returns_verified_facts():
    """`all_facts()`/`get()` can only ever return `verified=True`
    entries - this store never contains anything else (Bagian 7:
    verified always 'wins' because there's nothing else in here to
    compete with it)."""
    store = _tmp_store()
    store.record(_ok("light.bedroom", "on"))
    store.record(_ok("switch.fan", "on"))
    assert all(f["verified"] is True for f in store.all_facts())


@scenario
def test_retrieval_conversation_history_is_a_separate_store():
    """Bagian 2/7: conversation history (`luno.memory.session_log`) and
    verified facts (`VerifiedFactStore`) are two independent stores -
    nothing in `luno.memory` can ever appear in `all_facts()`, so
    conversation text can never outrank/overwrite a verified fact."""
    from luno import memory
    store = _tmp_store()
    memory.remember_turn("turn on the bedroom light", "I tried to turn it on, but it didn't respond.")
    store.record(_ok("light.bedroom", "on"))
    facts = store.all_facts()
    assert len(facts) == 1
    assert all("didn't respond" not in str(f.get("value", "")) for f in facts)


# ============================================================================
# Regression - old memory + public API untouched
# ============================================================================

@scenario
def test_memory_module_public_api_untouched():
    """`luno/memory.py` itself was never modified by this sprint - a
    smoke check that its documented public functions still exist and
    still work exactly as before."""
    from luno import memory
    before = len(memory.get_history())
    memory.remember_turn("hello", "hi there")
    after = memory.get_history()
    assert len(after) >= before


@scenario
def test_tool_result_and_task_apis_untouched():
    """Bagian 'Backward Compatibility': `ToolResult`/Planner/Executor
    are untouched - constructing/using them the same way the
    Reliability + Never-Assume-Success sprints already did must still
    work identically."""
    ok = ToolResult.ok("home_assistant", "turn_on", "I've turned on Bedroom Light.",
                        data={"entity_id": "light.bedroom", "actual_state": "on"})
    assert ok.success is True
    assert ok.to_dict()["data"]["entity_id"] == "light.bedroom"


# ============================================================================
# End-to-end, through the real RuntimeDemoConsole pipeline
# ============================================================================

def _load_demo():
    import importlib.util
    spec = importlib.util.spec_from_file_location("main_runtime_demo", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


@scenario
def test_end_to_end_successful_tool_call_stores_a_verified_fact():
    """Uses a small custom handler with the Reliability Sprint's real
    `entity_id`/`actual_state` data shape - `MockHomeAssistantHandler`
    predates that shape (it only reports `target`/`on`), so it
    correctly produces no fact at all; that's a property of the mock
    handler's older data shape, not of the Memory Guard."""
    demo = _load_demo()
    from luno.adapters import MockOpenRouterClient
    from luno.tool_manager.handler import ToolHandler

    class VerifiedSuccessHandler(ToolHandler):
        name = "home_assistant"

        def supported_actions(self):
            return ["turn_on", "turn_off", "toggle", "run_script", "set_temperature"]

        def execute(self, tool_call, context=None):
            return ToolResult.ok(
                self.name, tool_call.action, "I've turned on Bedroom Light.",
                data={"entity_id": "light.bedroom_light", "expected_state": "on", "actual_state": "on",
                      "verification_attempts": 1},
            )

    console = demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0))
    console.planner_module.memory_guard = _tmp_store()  # isolate from the real data file
    console.tool_manager_module.manager.registry.register("home_assistant", VerifiedSuccessHandler())
    console.start()
    try:
        finished = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: finished.set())
        console.event_bus.publish(demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "mg-ok-1"},
        ))
        assert _wait_until(finished.is_set, 5.0)
        facts = console.planner_module.memory_guard.all_facts()
        assert len(facts) == 1
        assert facts[0]["verified"] is True
        assert facts[0]["source"] == "tool_result"
        assert facts[0]["entity_id"] == "light.bedroom_light"
        assert facts[0]["value"] == "on"
    finally:
        console.stop()


@scenario
def test_end_to_end_failed_tool_call_never_stores_a_fact():
    demo = _load_demo()
    from luno.adapters import MockOpenRouterClient
    from luno.tool_manager.handler import ToolHandler

    class AlwaysFailsHandler(ToolHandler):
        name = "home_assistant"

        def supported_actions(self):
            return ["turn_on", "turn_off", "toggle", "run_script", "set_temperature"]

        def execute(self, tool_call, context=None):
            return ToolResult.fail(
                self.name, tool_call.action, "I tried to turn on Bedroom Light, but it didn't respond.",
                error_type="VerificationFailed", retryable=True,
                data={"entity_id": "light.bedroom_light", "expected_state": "on", "actual_state": "off",
                      "failure_reason": "Device did not reach the expected state in time"},
            )

    console = demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0))
    console.planner_module.memory_guard = _tmp_store()
    console.tool_manager_module.manager.registry.register("home_assistant", AlwaysFailsHandler())
    console.start()
    try:
        finished = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: finished.set())
        console.event_bus.publish(demo.Event(
            type="user_utterance", data={"text": "turn on the bedroom light", "request_id": "mg-fail-1"},
        ))
        assert _wait_until(finished.is_set, 5.0)
        assert console.planner_module.memory_guard.all_facts() == []
    finally:
        console.stop()


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
