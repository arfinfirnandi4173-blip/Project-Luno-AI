"""
test_world_model.py
=====================

Automated tests for the World Model sprint ("the Single Source of
Truth" for current device state) - `luno/world_model.py`, plus a couple
of end-to-end checks through the real `RuntimeDemoConsole` pipeline
(same no-network/no-hardware conventions as `tests/test_runtime_demo.py`
and `tests/test_memory_guard.py`).

Covers the sprint's own Testing checklist: Startup (startup sync
berhasil, entity masuk World Model), ToolResult (success update,
failure tidak update), State Changed (event mengubah state), Conflict
(update terbaru mengganti lama), Snapshot (snapshot benar, restore
benar), Retrieval (get/exists/all_entities), Regression (seluruh test
lama tetap lolos).

No `unittest.mock` - fake dicts/objects instead, matching this
project's own convention.

Run:
    python3 tests/test_world_model.py
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from typing import Callable, List, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno.world_model import WorldModel  # noqa: E402
from luno.core.event_bus import EventBus  # noqa: E402
from luno.core.events import Event  # noqa: E402
from luno.tool_manager.result import ToolResult  # noqa: E402

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _ok_result(entity_id: str, actual_state: str, expected_state: str = None) -> dict:
    data = {"entity_id": entity_id, "actual_state": actual_state, "expected_state": expected_state or actual_state}
    return {"success": True, "tool": "home_assistant", "action": "turn_on", "message": "ok", "data": data}


def _failed_result(entity_id: str, expected_state="on", actual_state="off") -> dict:
    data = {"entity_id": entity_id, "expected_state": expected_state, "actual_state": actual_state,
            "failure_reason": "Device did not reach the expected state in time"}
    return {"success": False, "tool": "home_assistant", "action": "turn_on", "message": "nope",
            "error_type": "VerificationFailed", "data": data}


# ============================================================================
# Bagian 7 - core read/write API
# ============================================================================

@scenario
def test_get_and_exists_unknown_entity():
    wm = WorldModel()
    assert wm.get("light.bedroom") is None
    assert wm.exists("light.bedroom") is False


@scenario
def test_update_then_get_and_exists():
    wm = WorldModel()
    wm.update("light.bedroom", "on", source="test")
    assert wm.get("light.bedroom") == "on"
    assert wm.exists("light.bedroom") is True


@scenario
def test_all_entities_returns_plain_state_map():
    wm = WorldModel()
    wm.update("light.bedroom", "on")
    wm.update("switch.fan", "off")
    assert wm.all_entities() == {"light.bedroom": "on", "switch.fan": "off"}


@scenario
def test_remove():
    wm = WorldModel()
    wm.update("light.bedroom", "on")
    wm.remove("light.bedroom")
    assert wm.exists("light.bedroom") is False
    assert wm.get("light.bedroom") is None


@scenario
def test_lookup_is_o1_dict_backed():
    """Not a real complexity test (impossible to assert Big-O directly),
    but confirms the internal store is a plain dict, matching the
    sprint's own 'gunakan dictionary internal' / 'O(1) lookup, no
    dependency baru' requirement."""
    wm = WorldModel()
    assert isinstance(wm._states, dict)


# ============================================================================
# Bagian 4 - update from ToolResult
# ============================================================================

@scenario
def test_tool_result_success_updates_world_model():
    wm = WorldModel()
    updated = wm.update_from_tool_result(_ok_result("light.bedroom", "on"))
    assert updated is True
    assert wm.get("light.bedroom") == "on"


@scenario
def test_tool_result_failure_does_not_update_world_model():
    wm = WorldModel()
    updated = wm.update_from_tool_result(_failed_result("light.bedroom"))
    assert updated is False
    assert wm.exists("light.bedroom") is False


@scenario
def test_tool_result_failure_does_not_overwrite_existing_state():
    """A failed attempt on an entity the World Model already knows
    about must leave the existing (real, last-known-good) state alone."""
    wm = WorldModel()
    wm.update_from_tool_result(_ok_result("light.bedroom", "on"))
    wm.update_from_tool_result(_failed_result("light.bedroom", expected_state="off", actual_state="on"))
    assert wm.get("light.bedroom") == "on"


@scenario
def test_tool_result_accepts_real_toolresult_object():
    """Duck-typed: a real `ToolResult` (attribute access) works exactly
    like the dict shape."""
    wm = WorldModel()
    ok = ToolResult.ok("home_assistant", "turn_on", "I've turned on Bedroom Light.",
                        data={"entity_id": "light.bedroom", "actual_state": "on"})
    assert wm.update_from_tool_result(ok) is True
    assert wm.get("light.bedroom") == "on"


@scenario
def test_tool_result_never_parses_message_text():
    """Bagian 4: the fact must come from `.data`, never from parsing
    `.message` - a message that LIES about the state must be ignored in
    favor of (or absence of) `.data`."""
    wm = WorldModel()
    result = {"success": True, "message": "I've turned on Bedroom Light.",  # message says "on"...
              "data": {"entity_id": "light.bedroom", "actual_state": "off"}}  # ...but data says "off"
    wm.update_from_tool_result(result)
    assert wm.get("light.bedroom") == "off"  # trusts .data, not the message


# ============================================================================
# Bagian 3 - update from state_changed
# ============================================================================

@scenario
def test_state_changed_dict_updates_world_model():
    wm = WorldModel()
    updated = wm.update_from_state_changed({"entity_id": "light.bedroom", "old_state": "off", "new_state": "on"})
    assert updated is True
    assert wm.get("light.bedroom") == "on"


@scenario
def test_state_changed_real_event_updates_world_model():
    """Confirms `update_from_state_changed` can be registered directly
    as an EventBus subscriber with no shim - a real `Event` object,
    dict-like `.get()`, works identically to a plain dict."""
    wm = WorldModel()
    event = Event(type="device_state_changed", data={"entity_id": "light.bedroom", "old_state": "off", "new_state": "on"})
    wm.update_from_state_changed(event)
    assert wm.get("light.bedroom") == "on"


# ============================================================================
# Bagian 10 - Conflict
# ============================================================================

@scenario
def test_conflict_newest_update_wins_no_duplication():
    wm = WorldModel()
    wm.update("light.bedroom", "off", source="startup_sync")
    wm.update("light.bedroom", "on", source="state_changed")
    assert wm.get("light.bedroom") == "on"
    assert len(wm.all_entities()) == 1


# ============================================================================
# Bagian 1 - Snapshot/restore
# ============================================================================

@scenario
def test_snapshot_and_restore_roundtrip():
    wm = WorldModel()
    wm.update("light.bedroom", "on")
    wm.update("switch.fan", "off")
    snap = wm.snapshot()

    wm2 = WorldModel()
    wm2.restore(snap)
    assert wm2.all_entities() == {"light.bedroom": "on", "switch.fan": "off"}


@scenario
def test_snapshot_is_a_deep_copy():
    wm = WorldModel()
    wm.update("light.bedroom", "on")
    snap = wm.snapshot()
    snap["light.bedroom"]["state"] = "off"  # mutate the returned copy
    assert wm.get("light.bedroom") == "on"  # live state untouched


# ============================================================================
# Bagian 2 - startup sync
# ============================================================================

@scenario
def test_startup_sync_loads_entities():
    wm = WorldModel()
    count = wm.sync_from_states({"light.kamar": "off", "switch.tv": "on"})
    assert count == 2
    assert wm.get("light.kamar") == "off"
    assert wm.get("switch.tv") == "on"


@scenario
def test_startup_sync_source_is_tagged():
    wm = WorldModel()
    wm.sync_from_states({"light.kamar": "off"})
    snap = wm.snapshot()
    assert snap["light.kamar"]["source"] == "startup_sync"


@scenario
def test_real_ha_client_get_all_states_additive_method():
    """`RealHomeAssistantClient.get_all_states()` (new, additive) must
    return exactly the source's live state cache, with no existing
    method/signature touched."""
    from luno.adapters.real_home_assistant import RealHomeAssistantClient

    class FakeSource:
        loop = None
        _last_states = {"light.kamar": "off", "switch.tv": "on"}

    client = RealHomeAssistantClient(FakeSource())
    states = client.get_all_states()
    assert states == {"light.kamar": "off", "switch.tv": "on"}
    states["light.kamar"] = "on"  # mutate the returned copy
    assert client.get_all_states()["light.kamar"] == "off"  # source cache untouched


# ============================================================================
# Bagian 8 - optional world_model_updated event
# ============================================================================

@scenario
def test_publishes_world_model_updated_event_when_bound():
    bus = EventBus()
    bus.start()
    try:
        captured = []
        bus.subscribe("world_model_updated", lambda e: captured.append(e))
        wm = WorldModel(event_bus=bus)
        wm.update("light.bedroom", "on", source="test")
        assert _wait_until(lambda: len(captured) == 1)
        assert captured[0].get("entity_id") == "light.bedroom"
        assert captured[0].get("old_state") is None
        assert captured[0].get("new_state") == "on"
        assert captured[0].get("source") == "test"
    finally:
        bus.stop(wait=True)


@scenario
def test_no_event_published_for_a_no_op_update():
    bus = EventBus()
    bus.start()
    try:
        captured = []
        bus.subscribe("world_model_updated", lambda e: captured.append(e))
        wm = WorldModel(event_bus=bus)
        wm.update("light.bedroom", "on")
        wm.update("light.bedroom", "on")  # identical value - no real change
        time.sleep(0.2)
        assert len(captured) == 1
    finally:
        bus.stop(wait=True)


@scenario
def test_works_standalone_with_no_event_bus():
    """`event_bus=None` (the default) must work with zero Event Bus
    coupling - no import error, no crash."""
    wm = WorldModel()
    wm.update("light.bedroom", "on")
    assert wm.get("light.bedroom") == "on"


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


@scenario
def test_end_to_end_successful_tool_call_updates_world_model():
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
                data={"entity_id": "light.bedroom_light", "expected_state": "on", "actual_state": "on"},
            )

    console = demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0))
    console.tool_manager_module.manager.registry.register("home_assistant", VerifiedSuccessHandler())
    console.start()
    try:
        finished = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: finished.set())
        console.event_bus.publish(demo.Event(
            type="user_utterance", data={"text": "turn on the lights", "request_id": "wm-ok-1"},
        ))
        assert _wait_until(finished.is_set, 5.0)
        assert console.planner_module.world_model.get("light.bedroom_light") == "on"
    finally:
        console.stop()


@scenario
def test_end_to_end_failed_tool_call_does_not_update_world_model():
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
                data={"entity_id": "light.bedroom_light", "expected_state": "on", "actual_state": "off"},
            )

    console = demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0))
    console.tool_manager_module.manager.registry.register("home_assistant", AlwaysFailsHandler())
    console.start()
    try:
        finished = threading.Event()
        console.event_bus.subscribe("need_llm_response", lambda e: finished.set())
        console.event_bus.publish(demo.Event(
            type="user_utterance", data={"text": "turn on the bedroom light", "request_id": "wm-fail-1"},
        ))
        assert _wait_until(finished.is_set, 5.0)
        assert console.planner_module.world_model.exists("light.bedroom_light") is False
    finally:
        console.stop()


@scenario
def test_end_to_end_device_state_changed_event_updates_world_model():
    """Bagian 3: World Model updates straight from `device_state_changed`
    without needing a Planner turn at all."""
    demo = _load_demo()
    from luno.adapters import MockOpenRouterClient
    console = demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0))
    console.start()
    try:
        console.event_bus.publish(demo.Event(
            type="device_state_changed", data={"entity_id": "light.kitchen", "old_state": "off", "new_state": "on"},
        ))
        assert _wait_until(lambda: console.planner_module.world_model.get("light.kitchen") == "on")
    finally:
        console.stop()


@scenario
def test_can_skip_action_helper():
    """Bagian 5 - read-only helper, reflects World Model state without
    touching real Planner execution behavior."""
    demo = _load_demo()
    from luno.adapters import MockOpenRouterClient

    console = demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0))
    console.planner_module.world_model.update("light.bedroom", "on")

    class _FakeToolCall:
        action = "turn_on"
        target = "light.bedroom"

    assert console.planner_module.can_skip_action(_FakeToolCall()) is True

    class _FakeToolCallOff:
        action = "turn_off"
        target = "light.bedroom"

    assert console.planner_module.can_skip_action(_FakeToolCallOff()) is False

    class _FakeToolCallUnknown:
        action = "turn_on"
        target = "light.unknown_entity"

    assert console.planner_module.can_skip_action(_FakeToolCallUnknown()) is None


# ============================================================================
# Regression - public API untouched
# ============================================================================

@scenario
def test_tool_result_and_memory_guard_apis_untouched():
    """Bagian 'Backward Compatibility': `ToolResult`/`memory_guard` were
    not modified by this sprint - a smoke check that they still work
    identically."""
    from luno.memory_guard import should_store_verified_result
    ok = ToolResult.ok("home_assistant", "turn_on", "I've turned on Bedroom Light.",
                        data={"entity_id": "light.bedroom", "actual_state": "on"})
    assert ok.success is True
    assert should_store_verified_result(ok) is True


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
