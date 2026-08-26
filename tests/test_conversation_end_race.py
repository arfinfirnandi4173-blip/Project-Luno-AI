"""
test_conversation_end_race.py
================================

Conversation_end Race Safety sprint - closes the race documented as a
Known Limitation by the Conversation_ended Lifecycle Routing sprint:
`_handle_utterance()` runs on its own background "luno-planner-turn"
thread and performs a few synchronous, non-zero-latency reads (memory
retrieval, etc.) BEFORE it reaches `_update_depth_preference()`. If
`conversation_ended` for the SAME conversation is delivered (on a
different thread - the Event Bus pump/dispatcher) while that gap is
still open, the final merge in `_on_conversation_ended()` could read
`_depth_preference` before this turn ever wrote to it - silently losing
that turn's depth-feedback contribution to the persisted baseline.

The fix: `PlannerBridgeModule` gained a small, purpose-built
`threading.Condition` (`_active_turn_lock`/`_active_turn_cv`) guarding
two plain, bounded, never-persisted structures -
`_active_turn_counts: Dict[str, int]` (how many turns are currently
in flight per conversation_id) and `_ending_conversations: set` (which
conversation_ids are currently inside `_on_conversation_ended()`).
`on_event()` now refuses a new "user_utterance" for a conversation that
is already ending; `_on_conversation_ended()` now calls
`_wait_for_turn_to_settle(session_id)` - bounded by
`self.turn_settle_timeout_s` (default 2.0s, configurable per-instance,
same shape as this class's own pre-existing `tool_timeout_s`) - BEFORE
reading `_depth_preference` for the final merge.

Section 1 below reproduces the OLD, racy behavior deterministically (by
configuring `turn_settle_timeout_s=0`, which collapses
`_wait_for_turn_to_settle()` to "check once, don't actually wait" - a
faithful stand-in for "no synchronization at all", i.e. this project's
literal pre-sprint behavior) and shows it loses the late turn's
contribution. Section 2 proves the SAME scenario, with the SAME
production code path, at the real default timeout, no longer loses it.
Both use a real, controlled `threading.Event` to make the timing
deterministic - never an arbitrary `sleep()`-based guess.

Persistent-state safety: every test here runs under `tests/conftest.py`'s
autouse `isolate_persistent_state` fixture - no test can touch Vinn's
real `config/response_depth_preference.json` or any other real store.
"""

from __future__ import annotations

import importlib.util
import json
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
        "main_runtime_demo_conversation_end_race", os.path.join(_ROOT, "main_runtime_demo.py")
    )
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_conversation_end_race"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.01) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _new_console(demo, turn_settle_timeout_s: float = 2.0):
    from luno.adapters import MockOpenRouterClient
    console = demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0))
    console.planner_module.turn_settle_timeout_s = turn_settle_timeout_s
    return console


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


def _publish_user_utterance(console, demo, text, request_id, conversation_id):
    console.event_bus.publish(
        demo.Event(type="user_utterance", data={"text": text, "request_id": request_id, "conversation_id": conversation_id})
    )


def _publish_conversation_ended(console, demo, session_id, reason="manual_sleep"):
    console.event_bus.publish(demo.Event(type="conversation_ended", data={"session_id": session_id, "reason": reason}))


def _publish_conversation_ended_tracked(console, demo, session_id, reason="manual_sleep"):
    """Publishes `conversation_ended` and returns `(done, restore)`:
    `done` is a `threading.Event` that becomes set exactly when
    `_on_conversation_ended()` RETURNS for this specific call, and
    `restore()` puts the original, unwrapped handler back (must be
    called exactly once, after the test is finished waiting on `done`).

    Use this - instead of polling `_ending_conversations` membership -
    whenever a test needs a deterministic "has this call finished yet?"
    signal. Polling for "not in the set" is NOT reliable on fast paths:
    when there is no in-flight turn to wait for (or a hung turn just got
    force-cleared by a short timeout), the whole
    add -> process -> discard sequence can complete in well under a
    millisecond - faster than any practical polling interval can
    reliably observe. Polling for "IN the set" (has it STARTED) remains
    safe to use on its own when the test is deliberately holding a turn
    open (via `_install_blocking_memory_retrieval`) and has not yet
    released it - there is no equivalent fast-path race in that
    direction, since the test itself controls when the block ends."""
    done = threading.Event()
    original = console.planner_module._on_conversation_ended

    def _wrapped(event):
        try:
            original(event)
        finally:
            done.set()

    console.planner_module._on_conversation_ended = _wrapped

    def _restore():
        console.planner_module._on_conversation_ended = original

    console.event_bus.publish(demo.Event(type="conversation_ended", data={"session_id": session_id, "reason": reason}))
    return done, _restore


def _publish_conversation_ended_and_wait(console, demo, session_id, reason="manual_sleep", timeout_s=5.0):
    """Convenience wrapper around `_publish_conversation_ended_tracked()`
    for tests that just want to publish and block until it's fully done,
    with no mid-flight inspection in between."""
    done, restore = _publish_conversation_ended_tracked(console, demo, session_id, reason=reason)
    try:
        assert _wait_until(done.is_set, timeout_s), f"_on_conversation_ended never completed for session_id={session_id!r}"
    finally:
        restore()


def _install_blocking_memory_retrieval(console, only_for_text=None):
    """Makes the NEXT call to `memory_retriever.retrieve_memories()`
    (the first synchronous thing `_handle_utterance()` does) block until
    the test releases it - a deterministic stand-in for "this turn is
    still processing" that does not rely on real timing/sleep guesses.

    If `only_for_text` is given, ONLY a call whose `text` matches it
    blocks - every other call passes straight through to `original()`
    immediately. Tests that need to prove an unrelated conversation is
    NOT affected by conversation A's wait (isolation / no-global-lock
    checks) must pass this, otherwise a second conversation's turn would
    also stall on the same shared `release_event` - a test-harness
    artifact, not evidence of a real global lock in production code.

    Returns (entered_event, release_event)."""
    entered_event = threading.Event()
    release_event = threading.Event()
    original = console.planner_module.memory_retriever.retrieve_memories

    def _blocking_retrieve(text):
        if only_for_text is not None and text != only_for_text:
            return original(text)
        entered_event.set()
        release_event.wait(timeout=10.0)  # safety net only - tests always set() this themselves
        return original(text)

    console.planner_module.memory_retriever.retrieve_memories = _blocking_retrieve
    return entered_event, release_event


BORDERLINE_NORMAL_QUERY = "cara pasang relay ke ESP32?"  # base score 38, NORMAL


# ============================================================================
# Section 1 - deterministic reproduction of the OLD (pre-fix) race
# ============================================================================


def test_race_reproduction_zero_wait_loses_the_late_turns_feedback():
    """`turn_settle_timeout_s=0` collapses `_wait_for_turn_to_settle()`
    to "don't actually wait" - a faithful stand-in for this project's
    literal pre-sprint behavior (no synchronization existed at all).
    This test proves that configuration DOES lose a late turn's
    feedback - the concrete evidence the race was real."""
    demo = _load_demo()
    console = _new_console(demo, turn_settle_timeout_s=0)
    console.start()
    try:
        conv_id = "race-old-1"
        entered, release = _install_blocking_memory_retrieval(console)

        _publish_user_utterance(console, demo, "kepanjangan, singkat aja", "race-old-1a", conv_id)
        assert _wait_until(entered.is_set), "turn never reached the blocking point"
        # at this instant, the turn is blocked BEFORE _update_depth_preference()
        # has run - conv_id has no entry in _depth_preference yet.
        assert conv_id not in console.planner_module._depth_preference

        # with timeout=0, _on_conversation_ended() does not wait - it
        # proceeds (and finishes) almost immediately (sub-millisecond),
        # so we use the deterministic completion-event helper rather
        # than polling _ending_conversations membership.
        _publish_conversation_ended_and_wait(console, demo, conv_id)
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE), (
            "conversation_ended's merge ran before the turn recorded any feedback - nothing to persist yet, as expected"
        )

        # now let the delayed turn finish - it will (uselessly) create a
        # fresh, orphaned _depth_preference entry for a conversation that
        # has ALREADY been cleaned up.
        release.set()
        assert _wait_until(lambda: conv_id in console.planner_module._depth_preference)

        # the defining symptom of the race: real feedback was observed,
        # but it was never persisted, because the merge already ran
        # before this moment and will never run again for this conversation.
        time.sleep(0.2)
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE), (
            "RACE NOT REPRODUCED - expected the late turn's feedback to be lost "
            "(never persisted) under turn_settle_timeout_s=0, but a file appeared"
        )
    finally:
        console.stop()


# ============================================================================
# Section 2 - the fix: the same scenario, the same code path, real timeout
# ============================================================================


def test_B_case_new_ordering_waits_and_captures_the_late_feedback():
    """The exact same race window as the reproduction above, but with the
    REAL default `turn_settle_timeout_s` (2.0s) - `_on_conversation_ended()`
    must now block until the late turn settles, and the final merge must
    see (and persist) its feedback."""
    demo = _load_demo()
    console = _new_console(demo)  # real default timeout
    console.start()
    try:
        conv_id = "race-new-1"
        entered, release = _install_blocking_memory_retrieval(console)

        _publish_user_utterance(console, demo, "kepanjangan, singkat aja", "race-new-1a", conv_id)
        assert _wait_until(entered.is_set)
        assert conv_id not in console.planner_module._depth_preference

        done, restore = _publish_conversation_ended_tracked(console, demo, conv_id)
        # give _on_conversation_ended() a moment to actually start waiting
        assert _wait_until(lambda: conv_id in console.planner_module._ending_conversations)
        # it must NOT have finished yet - it should be blocked in
        # _wait_for_turn_to_settle(), not racing ahead.
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)

        release.set()
        try:
            assert _wait_until(done.is_set, timeout_s=5.0), (
                "the late turn's feedback was still lost even with the real timeout - fix did not work"
            )
        finally:
            restore()
        assert os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
        on_disk = DepthPreferenceStore.load()
        assert on_disk.bias < 0  # "kepanjangan" leans SHORT
        # and the conversation ended cleanly - no leftover ending-mark.
        assert conv_id not in console.planner_module._ending_conversations
    finally:
        console.stop()


def test_case_A_turn_finishes_before_conversation_ended_still_works():
    """Baseline (no race at all) - ordinary completion before end must
    behave exactly as it did before this sprint: the wait is a no-op
    (nothing in flight), and cleanup proceeds normally. (Note: the
    PRE-EXISTING, unmodified `_on_conversation_ended()` merge-gating
    condition - `feedback_count > 0` - does not distinguish "already
    flushed by the primary %3 trigger" from "still has unflushed
    evidence", so ending a conversation right after its local bias
    already crossed that threshold performs one additional blend of the
    same local bias into the already-updated baseline. This is a
    pre-existing characteristic of the merge-gating logic this sprint
    did not touch or need to touch - not a regression introduced here -
    see docs/change_impact/conversation_end_race_safety.md's "Known
    limitations".)"""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "race-caseA"
        for i in range(3):
            _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", f"race-caseA-{i}", conv_id)
        assert _wait_until(lambda: os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE))
        local_bias = console.planner_module._depth_preference[conv_id].bias
        before = DepthPreferenceStore.load()
        expected_after = merge_conversation_into_persistent(before, local_bias)

        _publish_conversation_ended(console, demo, conv_id)
        assert _wait_until(lambda: conv_id not in console.planner_module._depth_preference)
        after = DepthPreferenceStore.load()
        assert after == expected_after
        assert -25 <= after.bias <= 25
    finally:
        console.stop()


def test_case_C_worker_hang_times_out_without_deadlock_or_corruption():
    """A turn that never settles (simulating a genuinely hung/crashed
    worker) must not block the runtime forever - `_on_conversation_ended()`
    must return within `turn_settle_timeout_s`, log the timeout, and
    leave no stuck state behind."""
    demo = _load_demo()
    console = _new_console(demo, turn_settle_timeout_s=0.3)
    console.start()
    try:
        conv_id = "race-hang"
        entered, release = _install_blocking_memory_retrieval(console)
        # deliberately never release() - simulates a permanently hung worker

        _publish_user_utterance(console, demo, "kepanjangan, singkat aja", "race-hang-a", conv_id)
        assert _wait_until(entered.is_set)

        started = time.monotonic()
        _publish_conversation_ended_and_wait(console, demo, conv_id, timeout_s=3.0)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0, f"took {elapsed:.2f}s - timeout was not bounded to ~turn_settle_timeout_s"

        # the force-cleared count must not be left in a broken/negative state.
        assert console.planner_module._active_turn_counts.get(conv_id, 0) == 0

        # persisted state must still be perfectly valid JSON (or absent) -
        # a timeout must never corrupt anything.
        if os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE):
            with open(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE, "r", encoding="utf-8") as fh:
                json.load(fh)  # raises if corrupted
    finally:
        release.set()  # let the hung thread go so it doesn't leak past this test
        console.stop()


def test_case_D_duplicate_conversation_ended_after_hang_timeout_is_idempotent():
    """A second `conversation_ended` for the same session_id, arriving
    after the first one already force-cleared a hung turn's count, must
    not block, error, or double-merge."""
    demo = _load_demo()
    console = _new_console(demo, turn_settle_timeout_s=0.2)
    console.start()
    try:
        conv_id = "race-dup-hang"
        entered, release = _install_blocking_memory_retrieval(console)
        _publish_user_utterance(console, demo, "kepanjangan, singkat aja", "race-dup-hang-a", conv_id)
        assert _wait_until(entered.is_set)

        _publish_conversation_ended_and_wait(console, demo, conv_id, timeout_s=2.0)

        started = time.monotonic()
        _publish_conversation_ended_and_wait(console, demo, conv_id, timeout_s=2.0)  # duplicate
        assert time.monotonic() - started < 1.0, "duplicate event re-waited instead of returning immediately"
    finally:
        release.set()
        console.stop()


def test_case_E_concurrent_conversation_isolation_during_a_wait():
    """Conversation A ending (and actively WAITING on a late turn) must
    not block or affect conversation B in any way."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_a, conv_b = "race-caseE-a", "race-caseE-b"
        entered, release = _install_blocking_memory_retrieval(console, only_for_text="kepanjangan, singkat aja")
        _publish_user_utterance(console, demo, "kepanjangan, singkat aja", "race-caseE-a1", conv_a)
        assert _wait_until(entered.is_set)

        done, restore = _publish_conversation_ended_tracked(console, demo, conv_a)
        assert _wait_until(lambda: conv_a in console.planner_module._ending_conversations)
        # A is now actively waiting - B must be completely unaffected.
        prompt_b = _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "race-caseE-b1", conv_b)
        assert "Response depth: NORMAL" in prompt_b, prompt_b
        assert conv_b not in console.planner_module._ending_conversations

        release.set()
        try:
            assert _wait_until(done.is_set, timeout_s=5.0), "conversation A's end never completed"
        finally:
            restore()
    finally:
        console.stop()


def test_case_F_immediate_new_conversation_with_same_id_is_not_blocked_forever():
    """Once `_on_conversation_ended()` has passed its race-sensitive
    section (merge + pop), a brand-new conversation reusing the SAME
    session_id must be accepted immediately, with correct fresh/persisted
    state - never permanently refused."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "race-caseF"
        _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "race-caseF-1", conv_id)
        _publish_conversation_ended_and_wait(console, demo, conv_id)

        # immediately reuse the same id - must not be refused.
        prompt = _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "race-caseF-2", conv_id)
        assert "Response depth: NORMAL" in prompt, prompt
    finally:
        console.stop()


def test_unknown_conversation_id_wait_is_a_no_op():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        started = time.monotonic()
        _publish_conversation_ended_and_wait(console, demo, "race-never-existed")
        assert time.monotonic() - started < 1.0
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
    finally:
        console.stop()


def test_empty_conversation_id_wait_is_a_no_op():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        console.event_bus.publish(demo.Event(type="conversation_ended", data={"session_id": "", "reason": "test"}))
        console.event_bus.publish(demo.Event(type="conversation_ended", data={}))
        time.sleep(0.3)  # must not raise / must not hang
        prompt = _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "race-empty-1", "race-empty-conv")
        assert "Response depth: NORMAL" in prompt
    finally:
        console.stop()


# ============================================================================
# Section 3 - adaptive depth E2E through the race window, both directions
# ============================================================================


def test_short_preference_survives_the_race_and_seeds_the_next_process():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "race-short-e2e"
        entered, release = _install_blocking_memory_retrieval(console)
        _publish_user_utterance(console, demo, "kepanjangan, singkat aja", "race-short-e2e-a", conv_id)
        assert _wait_until(entered.is_set)
        # at this instant conv_id has no _depth_preference entry yet -
        # that's the whole point of the race window this sprint closes.

        _publish_conversation_ended(console, demo, conv_id)
        assert _wait_until(lambda: conv_id in console.planner_module._ending_conversations)
        release.set()
        assert _wait_until(lambda: os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE), timeout_s=5.0)
        on_disk = DepthPreferenceStore.load()
        assert on_disk.bias < 0
    finally:
        console.stop()

    demo2 = _load_demo()
    console2 = _new_console(demo2)
    console2.start()
    try:
        startup_bias = console2.planner_module._depth_preference_startup_bias
        assert startup_bias < 0
        conv2 = "race-short-e2e-2"
        _run_turn_and_capture(console2, demo2, BORDERLINE_NORMAL_QUERY, "race-short-e2e-2a", conv2)
        policy2 = console2.planner_module._last_response_policy[conv2]
        assert any(r.startswith("adaptive_depth_preference:-") for r in policy2["reasons"]), policy2
        assert policy2["score"] == 38 + startup_bias
    finally:
        console2.stop()


def test_detailed_preference_survives_the_race_and_seeds_the_next_process():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "race-detailed-e2e"
        entered, release = _install_blocking_memory_retrieval(console)
        _publish_user_utterance(console, demo, "terlalu singkat, jelaskan lebih detail", "race-detailed-e2e-a", conv_id)
        assert _wait_until(entered.is_set)

        _publish_conversation_ended(console, demo, conv_id)
        assert _wait_until(lambda: conv_id in console.planner_module._ending_conversations)
        release.set()
        assert _wait_until(lambda: os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE), timeout_s=5.0)
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
        conv2 = "race-detailed-e2e-2"
        _run_turn_and_capture(console2, demo2, BORDERLINE_NORMAL_QUERY, "race-detailed-e2e-2a", conv2)
        policy2 = console2.planner_module._last_response_policy[conv2]
        assert any(r.startswith("adaptive_depth_preference:+") for r in policy2["reasons"]), policy2
        assert policy2["score"] == 38 + startup_bias
    finally:
        console2.stop()


def test_explicit_short_overrides_persisted_detailed_after_race_window():
    DepthPreferenceStore.save(PersistedDepthPreference(schema_version=1, bias=25, sample_count=20))
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        prompt = _run_turn_and_capture(
            console, demo, "jawab singkat aja ya, apa itu resistor?", "race-explicit-short", "race-explicit-short-conv",
        )
        assert "Response depth: SHORT" in prompt, prompt
    finally:
        console.stop()


def test_explicit_detailed_overrides_persisted_short_after_race_window():
    DepthPreferenceStore.save(PersistedDepthPreference(schema_version=1, bias=-25, sample_count=20))
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        prompt = _run_turn_and_capture(
            console, demo, "jelaskan secara detail dong, apa itu resistor?", "race-explicit-detailed", "race-explicit-detailed-conv",
        )
        assert "Response depth: DETAILED" in prompt, prompt
    finally:
        console.stop()


# ============================================================================
# Section 4 - persisted-file validity / privacy
# ============================================================================


def test_persisted_file_remains_valid_json_after_race_scenarios():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "race-validity"
        entered, release = _install_blocking_memory_retrieval(console)
        _publish_user_utterance(console, demo, "kepanjangan, singkat aja", "race-validity-a", conv_id)
        assert _wait_until(entered.is_set)
        _publish_conversation_ended(console, demo, conv_id)
        assert _wait_until(lambda: conv_id in console.planner_module._ending_conversations)
        release.set()
        assert _wait_until(lambda: os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE), timeout_s=5.0)
        with open(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        assert set(raw.keys()) == {"schema_version", "bias", "sample_count"}
    finally:
        console.stop()


def test_no_raw_conversation_data_persisted_by_the_synchronization_mechanism():
    """The new `_active_turn_counts`/`_ending_conversations` structures
    hold only conversation_id strings and ints - never text - and are
    never written to disk. This test asserts the on-disk file (once
    written) still contains no trace of the feedback text, same
    guarantee the Persistent Adaptive Response Depth Preference sprint
    already established, reconfirmed here under race conditions."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "race-privacy"
        entered, release = _install_blocking_memory_retrieval(console)
        _publish_user_utterance(console, demo, "kepanjangan, singkat aja", "race-privacy-a", conv_id)
        assert _wait_until(entered.is_set)
        _publish_conversation_ended(console, demo, conv_id)
        assert _wait_until(lambda: conv_id in console.planner_module._ending_conversations)
        release.set()
        assert _wait_until(lambda: os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE), timeout_s=5.0)
        with open(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE, "r", encoding="utf-8") as fh:
            serialized = fh.read()
        assert "kepanjangan" not in serialized
        assert "singkat" not in serialized
        assert conv_id not in serialized
    finally:
        console.stop()


def test_no_cross_conversation_state_leak_across_many_interleaved_races():
    """Stress-shaped isolation check - several conversations racing their
    endings concurrently must never mix up each other's local adaptive
    preference."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        convs = [f"race-stress-{i}" for i in range(4)]
        for i, conv in enumerate(convs):
            feedback = "kepanjangan, singkat aja" if i % 2 == 0 else "terlalu singkat, jelaskan lebih detail"
            _run_turn_and_capture(console, demo, feedback, f"{conv}-a", conv)

        for conv in convs:
            assert conv in console.planner_module._depth_preference

        threads = [threading.Thread(target=_publish_conversation_ended, args=(console, demo, c)) for c in convs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        for conv in convs:
            assert _wait_until(lambda c=conv: c not in console.planner_module._depth_preference)
            assert conv not in console.planner_module._ending_conversations
    finally:
        console.stop()


def test_cleanup_occurs_exactly_once_per_conversation_ended_event():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "race-once"
        _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", "race-once-a", conv_id)

        calls = []
        original = console.planner_module._on_conversation_ended

        def _counting(event):
            calls.append(event)
            return original(event)

        console.planner_module._on_conversation_ended = _counting
        try:
            _publish_conversation_ended(console, demo, conv_id)
            assert _wait_until(lambda: len(calls) >= 1)
            time.sleep(0.2)
            assert len(calls) == 1
        finally:
            console.planner_module._on_conversation_ended = original
    finally:
        console.stop()


def test_no_global_lock_regression_unrelated_conversations_proceed_during_a_wait():
    """The new lock/condition must never behave like a runtime-wide lock -
    while conversation A is actively blocked inside
    `_wait_for_turn_to_settle()`, entirely unrelated turns for OTHER
    conversations must complete promptly (not queue up behind A's wait)."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_a = "race-nolock-a"
        entered, release = _install_blocking_memory_retrieval(console, only_for_text="kepanjangan, singkat aja")
        _publish_user_utterance(console, demo, "kepanjangan, singkat aja", "race-nolock-a1", conv_a)
        assert _wait_until(entered.is_set)
        done, restore = _publish_conversation_ended_tracked(console, demo, conv_a)
        assert _wait_until(lambda: conv_a in console.planner_module._ending_conversations)

        started = time.monotonic()
        for i in range(5):
            _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, f"race-nolock-b{i}", f"race-nolock-b{i}-conv")
        elapsed = time.monotonic() - started
        assert elapsed < 3.0, f"unrelated turns took {elapsed:.2f}s while conversation A was waiting - looks like a global lock"

        release.set()
        try:
            assert _wait_until(done.is_set, timeout_s=5.0), "conversation A's end never completed"
        finally:
            restore()
    finally:
        console.stop()


# ============================================================================
# Section 5 - EventBus route safety (unchanged - reconfirmed after this sprint)
# ============================================================================


def test_route_table_still_contains_conversation_ended_to_planner_exactly_once():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        routes = console.runtime.coordinator.routes()
        targets = routes.get("conversation_ended", [])
        assert targets.count("planner") == 1
    finally:
        console.stop()


def test_repeated_bind_event_bus_still_does_not_duplicate_the_route():
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
