"""
test_conversation_ended_lifecycle_routing.py
================================================

Conversation_ended Lifecycle Routing sprint - proves that
`conversation_ended` events now reach `PlannerBridgeModule._on_conversation_ended()`
(`main_runtime_demo.py`) through the REAL, production Event Bus path -
`console.event_bus.publish(Event(type="conversation_ended", ...))` ->
`Coordinator._forward()` -> `PlannerBridgeModule.on_event()` -> that
method - not merely via a direct white-box call to
`_on_conversation_ended()` (that already-existing coverage lives in
`tests/test_persistent_adaptive_response_depth.py::test_e2e_6_...`/
`test_e2e_7_...` and is intentionally left untouched by this file).

Root cause (see `docs/change_impact/conversation_ended_lifecycle_routing.md`
for the full trace): `PlannerBridgeModule.on_event()` already handled
`event.type == "conversation_ended"` correctly - the ONLY thing missing
was a `Coordinator.add_route("conversation_ended", "planner")` call. No
handler logic, ordering, or persistence code needed to change. This
sprint added exactly one line in each of the two places a route table
for this module exists:
  - `main_runtime_demo.py` (`RuntimeDemoConsole.__init__` - what this
    test file's `_new_console()` below actually exercises)
  - `luno/bootstrap/modules.py` (`register_all_modules()` - real
    production `python main.py`; not directly exercised by pytest, but
    asserted structurally by `test_M2_production_bootstrap_route_table_also_has_the_fix`
    below via source inspection, since spinning up the full production
    bootstrap stack is out of scope for a unit/integration test file).

Persistent-state safety: every test here runs under `tests/conftest.py`'s
autouse `isolate_persistent_state` fixture - no test can touch Vinn's
real `config/response_depth_preference.json` or any other real store.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from typing import Callable

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno import config as luno_config  # noqa: E402
from luno.response_depth_preference import (  # noqa: E402
    DepthPreferenceStore,
    PersistedDepthPreference,
    merge_conversation_into_persistent,
)


def _load_demo():
    spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_conversation_ended_routing", os.path.join(_ROOT, "main_runtime_demo.py")
    )
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_conversation_ended_routing"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _new_console(demo):
    from luno.adapters import MockOpenRouterClient
    return demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0))


def _run_turn_and_capture(console, demo, text, request_id, conversation_id=None):
    captured = {}
    need_llm = threading.Event()

    def _capture(e):
        captured["system_prompt"] = e.get("system_prompt")
        need_llm.set()

    sub = console.event_bus.subscribe("need_llm_response", _capture)
    data = {"text": text, "request_id": request_id}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    try:
        console.event_bus.publish(demo.Event(type="user_utterance", data=data))
        assert _wait_until(need_llm.is_set, 5.0), "no need_llm_response within timeout"
    finally:
        console.event_bus.unsubscribe(sub)
    return captured.get("system_prompt") or ""


def _publish_conversation_ended(console, demo, session_id, reason="manual_sleep"):
    """The one thing this whole file exists to prove is possible: publish
    a REAL `conversation_ended` event onto the production Event Bus and
    let the Coordinator's route table deliver it - never a direct call
    to `_on_conversation_ended()`."""
    console.event_bus.publish(demo.Event(type="conversation_ended", data={"session_id": session_id, "reason": reason}))


BORDERLINE_NORMAL_QUERY = "cara pasang relay ke ESP32?"  # base score 38, NORMAL


# ============================================================================
# Section 1 - the route itself exists, exactly once, at the right layer
# ============================================================================


def test_route_table_contains_conversation_ended_to_planner_exactly_once():
    """Structural proof the fix is a single, non-duplicated Coordinator
    route - not a second Event Bus, not a duplicate subscription."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        routes = console.runtime.coordinator.routes()
        targets = routes.get("conversation_ended", [])
        assert targets.count("planner") == 1, f"expected exactly one 'planner' route for conversation_ended, got {targets}"
    finally:
        console.stop()


def test_M1_repeated_bind_event_bus_does_not_duplicate_the_conversation_ended_route():
    """Phase 8 scenario M - the routing fix lives in `Coordinator.add_route()`
    (called once, in `__init__`), NOT inside `Module.bind_event_bus()` -
    so re-invoking `bind_event_bus()` on the same module must not affect
    the route table at all."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        before = list(console.runtime.coordinator.routes().get("conversation_ended", []))
        console.planner_module.bind_event_bus(console.event_bus)
        console.planner_module.bind_event_bus(console.event_bus)
        after = list(console.runtime.coordinator.routes().get("conversation_ended", []))
        assert before == after
        assert after.count("planner") == 1
    finally:
        console.stop()


def test_M2_production_bootstrap_route_table_also_has_the_fix():
    """`luno/bootstrap/modules.py` (real production `python main.py`) and
    `main_runtime_demo.py` (this console) are documented as a
    byte-for-byte-mirrored route table (see `luno/bootstrap/modules.py`'s
    own module docstring). Spinning up the full production bootstrap
    stack (real/mocked adapters, dashboard, health checks) is out of
    scope for this file, so this is a direct source-level check that the
    same fix landed in both places, rather than only in the console this
    file's other tests exercise end-to-end."""
    bootstrap_path = os.path.join(_ROOT, "luno", "bootstrap", "modules.py")
    with open(bootstrap_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    assert 'runtime.add_route("conversation_ended", "planner")' in source


# ============================================================================
# Section 2 - real Event Bus reachability, exactly-once, duplicate safety
# ============================================================================


def test_A_conversation_ended_reaches_planner_bridge_through_the_real_event_bus():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "cel-a"
        _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", "cel-a-1", conv_id)
        assert conv_id in console.planner_module._depth_preference

        _publish_conversation_ended(console, demo, conv_id)
        # only the routed on_event() -> _on_conversation_ended() path pops
        # this entry - if it disappears, the real Event Bus route fired.
        assert _wait_until(lambda: conv_id not in console.planner_module._depth_preference), (
            "conversation_ended did not reach PlannerBridgeModule through the real Event Bus"
        )
    finally:
        console.stop()


def test_B_handler_executes_exactly_once_for_one_event():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "cel-b"
        _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "cel-b-1", conv_id)

        calls = []
        original = console.planner_module._on_conversation_ended

        def _counting_wrapper(event):
            calls.append(event)
            return original(event)

        console.planner_module._on_conversation_ended = _counting_wrapper
        try:
            _publish_conversation_ended(console, demo, conv_id)
            assert _wait_until(lambda: len(calls) >= 1)
            time.sleep(0.2)  # give any accidental double-delivery a chance to show up
            assert len(calls) == 1, f"expected exactly 1 invocation, got {len(calls)}"
        finally:
            console.planner_module._on_conversation_ended = original
    finally:
        console.stop()


def test_C_duplicate_conversation_ended_event_is_safe():
    """Two `conversation_ended` events for the SAME session_id (e.g. a
    genuinely duplicated publish) must not raise, and must not double-merge
    into the persistent baseline the second time (the local entry is
    already popped after the first)."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "cel-c"
        for i in range(3):
            _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", f"cel-c-{i}", conv_id)
        assert _wait_until(lambda: os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE))

        _publish_conversation_ended(console, demo, conv_id)
        assert _wait_until(lambda: conv_id not in console.planner_module._depth_preference)
        after_first = DepthPreferenceStore.load()

        # second, duplicate event for the same (already-cleaned-up) session_id
        _publish_conversation_ended(console, demo, conv_id)
        time.sleep(0.3)  # let it flow through the pump thread; nothing should change
        after_second = DepthPreferenceStore.load()
        assert after_second == after_first, "a duplicate conversation_ended event mutated the persisted baseline again"
    finally:
        console.stop()


def test_D_unknown_conversation_id_is_safe():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
        _publish_conversation_ended(console, demo, "cel-never-existed")
        time.sleep(0.3)
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
    finally:
        console.stop()


def test_D2_empty_conversation_id_is_safe():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        console.event_bus.publish(demo.Event(type="conversation_ended", data={"session_id": "", "reason": "test"}))
        console.event_bus.publish(demo.Event(type="conversation_ended", data={}))  # no session_id key at all
        time.sleep(0.3)  # must not raise / must not crash the pump thread
        # bus must still be alive and routing normally afterward
        prompt = _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "cel-d2-1", "cel-d2-conv")
        assert "Response depth: NORMAL" in prompt
    finally:
        console.stop()


# ============================================================================
# Section 3 - cleanup coverage (Phase 7 audit)
# ============================================================================


def test_E_conversation_local_state_is_fully_cleaned():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "cel-e"
        _run_turn_and_capture(console, demo, "kenapa ESP32 saya kepanjangan, singkat aja", "cel-e-1", conv_id)
        pb = console.planner_module
        assert conv_id in pb._depth_preference
        assert conv_id in pb._response_depth_context
        assert conv_id in pb._last_response_policy

        _publish_conversation_ended(console, demo, conv_id)
        assert _wait_until(lambda: conv_id not in pb._depth_preference)
        assert conv_id not in pb._response_depth_context
        assert conv_id not in pb._last_response_policy
        assert conv_id not in pb._last_device_target
        assert conv_id not in pb._session_feedback_target
        assert conv_id not in pb._pending_env_confirmations
        assert conv_id not in pb._last_turn_trace
    finally:
        console.stop()


# ============================================================================
# Section 4 - concurrent conversations (Phase 5)
# ============================================================================


def test_F_ending_one_conversation_does_not_touch_another_active_one():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_a, conv_b = "cel-f-a", "cel-f-b"
        _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", "cel-f-a1", conv_a)
        _run_turn_and_capture(console, demo, "terlalu singkat, jelaskan lebih detail", "cel-f-b1", conv_b)
        pb = console.planner_module
        assert conv_a in pb._depth_preference
        assert conv_b in pb._depth_preference
        b_bias_before = pb._depth_preference[conv_b].bias
        assert b_bias_before > 0  # leans DETAILED

        _publish_conversation_ended(console, demo, conv_a)
        assert _wait_until(lambda: conv_a not in pb._depth_preference)

        # conversation B must be completely untouched.
        assert conv_b in pb._depth_preference
        assert pb._depth_preference[conv_b].bias == b_bias_before

        # and B's next turn still applies exactly its own (unchanged)
        # adaptive modifier - not "must flip to a different depth bucket"
        # (a single feedback event's bounded nudge is deliberately too
        # small to do that by itself - see PERSIST_BLEND_WEIGHT/_DEPTH_
        # BIAS_STEP's own conservative-by-design rationale), but the
        # modifier must visibly be applied, in the DETAILED direction, and
        # by the exact same amount as B's own untouched local bias.
        _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "cel-f-b2", conv_b)
        policy_b = pb._last_response_policy[conv_b]
        assert any(r.startswith("adaptive_depth_preference:+") for r in policy_b["reasons"]), policy_b
        assert policy_b["score"] == 38 + b_bias_before  # base score for BORDERLINE_NORMAL_QUERY is 38
    finally:
        console.stop()


# ============================================================================
# Section 5 - adaptive-depth E2E through the real event path (Phase 6)
# ============================================================================


def test_G_H_short_direction_real_event_persists_and_seeds_next_process():
    """Conversation 1: repeated 'kepanjangan' feedback, ended through the
    REAL conversation_ended Event Bus path (not a direct call). Then a
    brand-new process (simulated restart) must load the persisted SHORT
    baseline and apply it from turn 1 of a brand-new conversation."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "cel-gh-1"
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
        # two events - below the %3 per-turn threshold, so nothing is
        # persisted yet by the PRIMARY trigger.
        for i in range(2):
            _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", f"cel-gh-1-{i}", conv_id)
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)

        local_bias_before_end = console.planner_module._depth_preference[conv_id].bias
        assert local_bias_before_end < 0

        # end THROUGH THE REAL EVENT BUS - this is what proves the routing
        # fix, not a direct _on_conversation_ended() call.
        _publish_conversation_ended(console, demo, conv_id)
        assert _wait_until(lambda: os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)), (
            "best-effort final merge on conversation_ended did not persist - "
            "the real Event Bus route is not reaching PlannerBridgeModule"
        )
        on_disk = DepthPreferenceStore.load()
        expected = merge_conversation_into_persistent(PersistedDepthPreference(), local_bias_before_end)
        assert on_disk == expected
        assert on_disk.bias < 0
    finally:
        console.stop()

    # --- Conversation 2: a brand-new process (new console = simulated restart) ---
    demo2 = _load_demo()
    console2 = _new_console(demo2)
    console2.start()
    try:
        startup_bias = console2.planner_module._depth_preference_startup_bias
        assert startup_bias < 0
        conv2_id = "cel-gh-2"
        assert conv2_id not in console2.planner_module._depth_preference
        _run_turn_and_capture(console2, demo2, BORDERLINE_NORMAL_QUERY, "cel-gh-2-1", conv2_id)
        # A single conversation's merge is DELIBERATELY conservative
        # (PERSIST_BLEND_WEIGHT=0.3) - it will not by itself flip a
        # NORMAL-scored query into the SHORT bucket. What matters here is
        # that the persisted baseline was actually LOADED and APPLIED to
        # this brand-new conversation's very first turn, in the correct
        # (SHORT-leaning, negative) direction, by exactly the persisted
        # amount - proving cross-session restoration end-to-end.
        policy2 = console2.planner_module._last_response_policy[conv2_id]
        assert any(r.startswith("adaptive_depth_preference:-") for r in policy2["reasons"]), policy2
        assert policy2["score"] == 38 + startup_bias  # base score for BORDERLINE_NORMAL_QUERY is 38
    finally:
        console2.stop()


def test_G_H_detailed_direction_real_event_persists_and_seeds_next_process():
    """Symmetric counterpart - 'terlalu singkat'/'jelaskan lebih detail'
    feedback, ended through the real event path, restored by a brand-new
    process."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "cel-gh-3"
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
        for i in range(2):
            _run_turn_and_capture(console, demo, "terlalu singkat, jelaskan lebih detail", f"cel-gh-3-{i}", conv_id)
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)

        local_bias_before_end = console.planner_module._depth_preference[conv_id].bias
        assert local_bias_before_end > 0

        _publish_conversation_ended(console, demo, conv_id)
        assert _wait_until(lambda: os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE))
        on_disk = DepthPreferenceStore.load()
        assert on_disk.bias > 0
    finally:
        console.stop()

    demo2 = _load_demo()
    console2 = _new_console(demo2)
    console2.start()
    try:
        startup_bias = console2.planner_module._depth_preference_startup_bias
        assert startup_bias > 0
        conv2_id = "cel-gh-4"
        _run_turn_and_capture(console2, demo2, BORDERLINE_NORMAL_QUERY, "cel-gh-4-1", conv2_id)
        policy2 = console2.planner_module._last_response_policy[conv2_id]
        assert any(r.startswith("adaptive_depth_preference:+") for r in policy2["reasons"]), policy2
        assert policy2["score"] == 38 + startup_bias
    finally:
        console2.stop()


def test_I_explicit_short_overrides_persisted_detailed_baseline_after_real_event_end():
    DepthPreferenceStore.save(PersistedDepthPreference(schema_version=1, bias=25, sample_count=20))
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        assert console.planner_module._depth_preference_startup_bias == 25
        prompt = _run_turn_and_capture(
            console, demo, "jawab singkat aja ya, apa itu resistor?", "cel-i-1", "cel-i-conv",
        )
        assert "Response depth: SHORT" in prompt, prompt
    finally:
        console.stop()


def test_J_explicit_detailed_overrides_persisted_short_baseline_after_real_event_end():
    DepthPreferenceStore.save(PersistedDepthPreference(schema_version=1, bias=-25, sample_count=20))
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        assert console.planner_module._depth_preference_startup_bias == -25
        prompt = _run_turn_and_capture(
            console, demo, "jelaskan secara detail dong, apa itu resistor?", "cel-j-1", "cel-j-conv",
        )
        assert "Response depth: DETAILED" in prompt, prompt
    finally:
        console.stop()


# ============================================================================
# Section 6 - persistent-state isolation sanity (Phase 10 / scenario L)
# ============================================================================


def test_L_isolation_fixture_redirects_away_from_the_real_config_directory():
    path = luno_config.RESPONSE_DEPTH_PREFERENCE_FILE
    normalized = os.path.normpath(os.path.abspath(path))
    real_config_dir = os.path.normpath(os.path.join(_ROOT, "config"))
    assert not normalized.startswith(real_config_dir + os.sep), (
        f"isolate_persistent_state did not redirect RESPONSE_DEPTH_PREFERENCE_FILE: {path!r}"
    )
