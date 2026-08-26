"""
test_persistent_adaptive_response_depth.py
==============================================

Persistent Adaptive Response Depth Preference sprint - tests for the new
`luno/response_depth_preference.py` module (`PersistedDepthPreference`,
`DepthPreferenceStore`, `should_persist()`, `merge_conversation_into_persistent()`)
AND for its wiring into the real `PlannerBridgeModule`/`RuntimeDemoConsole`
production pipeline in `main_runtime_demo.py`.

This sprint is a CROSS-SESSION EXTENSION of the conversation-scoped,
in-memory-only adaptive preference `tests/test_adaptive_response_depth.py`
already covers in full (detector, pure accumulator, priority order,
conversation-boundary reset, cross-conversation isolation within one
process). None of that is re-tested here except where this sprint
specifically changes the observable behavior (seeding a brand-new
conversation from a previously-persisted baseline). Everything else -
detection, explicit-instruction priority, oscillation bounds, decay - is
still owned entirely by `luno/response_policy.py` and is UNCHANGED.

Sections:
  1. `PersistedDepthPreference` - schema, clamping, round-trip (A-J).
  2. `DepthPreferenceStore` - load/save via `luno.persistence` (K-N).
  3. `should_persist()` / `merge_conversation_into_persistent()` - the
     threshold + conservative-blend learning policy (O-T).
  4. End-to-end integration through the real `RuntimeDemoConsole`
     pipeline - process-restart learning, in-process isolation,
     explicit-instruction priority, conversation-end best-effort merge,
     on-disk schema/privacy audit, concurrency smoke test.

Persistent-state safety: every test in this file runs under
`tests/conftest.py`'s autouse `isolate_persistent_state` fixture, which
already redirects `config.RESPONSE_DEPTH_PREFERENCE_FILE` to a fresh
`tmp_path` - no test here can ever touch Vinn's real
`config/response_depth_preference.json`.

Run:
    python3 -m pytest -q tests/test_persistent_adaptive_response_depth.py
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
from luno.response_policy import (  # noqa: E402
    DEPTH_BIAS_MAX,
    DEPTH_BIAS_MIN,
)
from luno.response_depth_preference import (  # noqa: E402
    MAX_SAMPLE_COUNT,
    PERSIST_BLEND_WEIGHT,
    PERSIST_MIN_SAMPLES,
    SCHEMA_VERSION,
    DepthPreferenceStore,
    PersistedDepthPreference,
    merge_conversation_into_persistent,
    should_persist,
)

# ============================================================================
# Section 1 - PersistedDepthPreference: schema, clamping, round-trip
# ============================================================================


def test_A_default_is_neutral():
    pref = PersistedDepthPreference()
    assert pref.schema_version == SCHEMA_VERSION
    assert pref.bias == 0
    assert pref.sample_count == 0


def test_B_to_dict_has_exactly_three_keys():
    """Privacy/trust boundary - no raw text, no transcript, no query
    history, no timestamps. Exactly the suggested schema, nothing more."""
    pref = PersistedDepthPreference(schema_version=1, bias=-7, sample_count=3)
    d = pref.to_dict()
    assert set(d.keys()) == {"schema_version", "bias", "sample_count"}
    assert d == {"schema_version": 1, "bias": -7, "sample_count": 3}


def test_C_from_dict_round_trip():
    original = PersistedDepthPreference(schema_version=1, bias=12, sample_count=9)
    restored = PersistedDepthPreference.from_dict(original.to_dict())
    assert restored == original


def test_D_from_dict_non_dict_input_is_default():
    for bad in (None, "not a dict", 42, [1, 2, 3], 3.14):
        assert PersistedDepthPreference.from_dict(bad) == PersistedDepthPreference()


def test_E_from_dict_missing_or_mismatched_schema_version_is_default():
    assert PersistedDepthPreference.from_dict({"bias": 20, "sample_count": 5}) == PersistedDepthPreference()
    assert PersistedDepthPreference.from_dict(
        {"schema_version": 2, "bias": 20, "sample_count": 5}
    ) == PersistedDepthPreference()
    assert PersistedDepthPreference.from_dict(
        {"schema_version": 999, "bias": 20, "sample_count": 5}
    ) == PersistedDepthPreference()


def test_F_bias_clamped_to_upper_bound():
    pref = PersistedDepthPreference.from_dict({"schema_version": 1, "bias": 9999, "sample_count": 0})
    assert pref.bias == DEPTH_BIAS_MAX


def test_G_bias_clamped_to_lower_bound():
    pref = PersistedDepthPreference.from_dict({"schema_version": 1, "bias": -9999, "sample_count": 0})
    assert pref.bias == DEPTH_BIAS_MIN


def test_H_bias_invalid_or_nan_falls_back_to_zero():
    for bad in (float("nan"), float("inf"), float("-inf"), "not a number", None, [1, 2], {}):
        pref = PersistedDepthPreference.from_dict({"schema_version": 1, "bias": bad, "sample_count": 0})
        assert pref.bias == 0


def test_I_sample_count_clamped_negative_and_over_ceiling():
    low = PersistedDepthPreference.from_dict({"schema_version": 1, "bias": 0, "sample_count": -50})
    assert low.sample_count == 0
    high = PersistedDepthPreference.from_dict(
        {"schema_version": 1, "bias": 0, "sample_count": MAX_SAMPLE_COUNT + 1000}
    )
    assert high.sample_count == MAX_SAMPLE_COUNT


def test_J_bounds_are_the_single_shared_contract_with_response_policy():
    """`luno.response_depth_preference` must reuse `luno.response_policy`'s
    own bias bounds - not redefine a second, independently-chosen range
    that could silently drift out of sync."""
    assert DEPTH_BIAS_MIN == -25
    assert DEPTH_BIAS_MAX == 25


# ============================================================================
# Section 2 - DepthPreferenceStore: load/save via luno.persistence
# ============================================================================


def test_K_load_missing_file_returns_neutral_default():
    assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
    loaded = DepthPreferenceStore.load()
    assert loaded == PersistedDepthPreference()


def test_L_save_then_load_round_trips():
    original = PersistedDepthPreference(schema_version=1, bias=-15, sample_count=4)
    assert DepthPreferenceStore.save(original) is True
    assert os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
    loaded = DepthPreferenceStore.load()
    assert loaded == original


def test_M_save_returns_false_for_falsy_path(monkeypatch):
    monkeypatch.setattr(luno_config, "RESPONSE_DEPTH_PREFERENCE_FILE", "", raising=False)
    assert DepthPreferenceStore.save(PersistedDepthPreference(bias=5)) is False


def test_N_load_corrupted_schema_on_disk_falls_back_safely(tmp_path):
    """A hand-edited / corrupted file with a mismatched schema_version
    must never crash the loader - fail-safe to neutral default, same
    convention `RelationshipState` already established."""
    path = luno_config.RESPONSE_DEPTH_PREFERENCE_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": 77, "bias": -25, "sample_count": 100}, fh)
    loaded = DepthPreferenceStore.load()
    assert loaded == PersistedDepthPreference()


# ============================================================================
# Section 3 - should_persist() / merge_conversation_into_persistent()
# ============================================================================


def test_O_should_persist_thresholds():
    assert PERSIST_MIN_SAMPLES == 3
    assert should_persist(0) is False
    assert should_persist(1) is False
    assert should_persist(2) is False
    assert should_persist(3) is True
    assert should_persist(4) is False
    assert should_persist(5) is False
    assert should_persist(6) is True
    assert should_persist(9) is True


def test_P_merge_is_pure_never_mutates_input():
    persisted = PersistedDepthPreference(schema_version=1, bias=0, sample_count=0)
    snapshot = PersistedDepthPreference(schema_version=1, bias=0, sample_count=0)
    merge_conversation_into_persistent(persisted, local_bias=-20)
    assert persisted == snapshot  # unchanged


def test_Q_merge_is_a_conservative_blend_not_an_overwrite():
    """A single merge from a neutral baseline must NOT jump straight to
    the conversation's local bias - only `PERSIST_BLEND_WEIGHT` of the
    way there. Directly encodes 'the user must never become permanently
    stuck ... because of a handful of comments'."""
    persisted = PersistedDepthPreference(bias=0, sample_count=0)
    merged = merge_conversation_into_persistent(persisted, local_bias=DEPTH_BIAS_MAX)
    expected = round(0 * (1 - PERSIST_BLEND_WEIGHT) + DEPTH_BIAS_MAX * PERSIST_BLEND_WEIGHT)
    assert merged.bias == expected
    assert merged.bias != DEPTH_BIAS_MAX
    assert 0 < merged.bias < DEPTH_BIAS_MAX


def test_R_merge_bounded_even_with_extreme_local_bias():
    persisted = PersistedDepthPreference(bias=DEPTH_BIAS_MAX, sample_count=50)
    merged = merge_conversation_into_persistent(persisted, local_bias=999999)
    assert DEPTH_BIAS_MIN <= merged.bias <= DEPTH_BIAS_MAX


def test_S_merge_increments_sample_count_and_caps_it():
    persisted = PersistedDepthPreference(bias=0, sample_count=0)
    merged = merge_conversation_into_persistent(persisted, local_bias=-10)
    assert merged.sample_count == 1
    at_ceiling = PersistedDepthPreference(bias=0, sample_count=MAX_SAMPLE_COUNT)
    merged2 = merge_conversation_into_persistent(at_ceiling, local_bias=-10)
    assert merged2.sample_count == MAX_SAMPLE_COUNT


def test_T_repeated_opposing_merges_pull_back_toward_neutral_not_flip_to_extreme():
    """Scenario J shape: consistent negative evidence, then consistent
    positive evidence - the baseline drifts back gradually, never
    snaps straight to the opposite extreme in one step."""
    pref = PersistedDepthPreference(bias=0, sample_count=0)
    for _ in range(5):
        pref = merge_conversation_into_persistent(pref, local_bias=DEPTH_BIAS_MIN)
    assert pref.bias < 0
    biased_negative = pref.bias
    pref = merge_conversation_into_persistent(pref, local_bias=DEPTH_BIAS_MAX)
    # one opposing merge moves it back toward zero, but does not overshoot
    # all the way to the positive extreme.
    assert pref.bias > biased_negative
    assert pref.bias < DEPTH_BIAS_MAX


def test_U_save_goes_through_atomic_write_and_creates_a_pre_write_backup():
    """Phase 2 requirement: no duplicated backup/atomic-write logic -
    `DepthPreferenceStore.save()` must actually go THROUGH
    `luno.persistence.atomic_write_json()`'s real backup-before-write
    path, not just structurally call it. First save (no primary file
    yet) creates no backup (nothing to back up); a SECOND save (primary
    now exists) must copy the prior contents into `backups/` before
    replacing it - the same observable contract every other
    `luno.persistence`-backed store already gets for free."""
    from luno import persistence as luno_persistence

    path = luno_config.RESPONSE_DEPTH_PREFERENCE_FILE
    first = PersistedDepthPreference(schema_version=1, bias=-5, sample_count=1)
    assert DepthPreferenceStore.save(first) is True
    assert luno_persistence.list_backups(path) == []  # nothing to back up yet

    second = PersistedDepthPreference(schema_version=1, bias=10, sample_count=2)
    assert DepthPreferenceStore.save(second) is True
    backups = luno_persistence.list_backups(path)
    assert len(backups) == 1
    backup_dir = luno_persistence.backup_dir_for(path)
    with open(os.path.join(backup_dir, backups[0]), "r", encoding="utf-8") as fh:
        backed_up = json.load(fh)
    # the backup holds the FIRST save's contents (the state that existed
    # right before the second, overwriting write) - proves this is a
    # real pre-write backup, not a coincidental empty directory.
    assert backed_up == first.to_dict()
    # and the primary now holds the second save's contents.
    assert DepthPreferenceStore.load() == second


def test_V_isolation_fixture_redirects_away_from_the_real_config_directory():
    """Phase 2 / Phase 10 requirement: every test in this file must be
    incapable of touching Vinn's real `config/response_depth_preference.json`
    - `tests/conftest.py`'s autouse `isolate_persistent_state` fixture is
    what guarantees this (see that fixture's own `_WRITABLE_STATE_ATTRS`
    entry for `RESPONSE_DEPTH_PREFERENCE_FILE`), and this test makes the
    guarantee an explicit, checked assertion rather than an implicit
    assumption every other test in this file silently relies on."""
    path = luno_config.RESPONSE_DEPTH_PREFERENCE_FILE
    assert path, "RESPONSE_DEPTH_PREFERENCE_FILE must be set while isolated"
    normalized = os.path.normpath(os.path.abspath(path))
    real_config_dir = os.path.normpath(os.path.join(_ROOT, "config"))
    assert not normalized.startswith(real_config_dir + os.sep), (
        f"isolate_persistent_state did not redirect RESPONSE_DEPTH_PREFERENCE_FILE - "
        f"still pointing at the real config/ directory: {path!r}"
    )


# ============================================================================
# Section 4 - E2E through the real production bridge
# ============================================================================


def _load_demo():
    spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_persistent_adaptive_depth", os.path.join(_ROOT, "main_runtime_demo.py")
    )
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_persistent_adaptive_depth"] = demo
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


BORDERLINE_NORMAL_QUERY = "cara pasang relay ke ESP32?"  # base score 38, NORMAL


def test_e2e_1_fresh_process_no_persisted_file_is_byte_identical_to_sprint_2():
    """Regression - with no `response_depth_preference.json` on disk, a
    brand-new conversation's very first turn must behave exactly like
    before this sprint existed (adaptive_modifier effectively None/0)."""
    assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        prompt = _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "pd-1a", "pd-e2e-1")
        assert "Response depth: NORMAL" in prompt, prompt
        assert console.planner_module._depth_preference_startup_bias == 0
    finally:
        console.stop()


def test_e2e_2_threshold_gated_persistence_writes_a_blended_baseline():
    """Three 'kepanjangan' feedback events within ONE conversation cross
    `PERSIST_MIN_SAMPLES` and persist a conservatively-blended baseline -
    never a raw overwrite, never before the threshold."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "pd-e2e-2"
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
        for i in range(2):
            _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", f"pd-2a-{i}", conv_id)
        # below threshold - nothing persisted yet
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)

        _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", "pd-2a-2", conv_id)
        # 3rd local feedback event crosses PERSIST_MIN_SAMPLES
        assert os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)

        local_pref = console.planner_module._depth_preference[conv_id]
        assert local_pref.feedback_count == 3
        on_disk = DepthPreferenceStore.load()
        expected = merge_conversation_into_persistent(PersistedDepthPreference(), local_pref.bias)
        assert on_disk == expected
        assert on_disk.bias < 0  # leans SHORT
        assert on_disk.bias != local_pref.bias  # blended, not overwritten
    finally:
        console.stop()


def test_e2e_3_new_process_seeds_a_brand_new_conversation_from_persisted_baseline():
    """The core cross-session behavior this whole sprint exists for: a
    SECOND process (new PlannerBridgeModule instance, simulating a
    restart) picks up whatever was persisted and applies it to a
    conversation it has never seen before, from turn 1."""
    DepthPreferenceStore.save(PersistedDepthPreference(schema_version=1, bias=DEPTH_BIAS_MIN, sample_count=10))

    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        assert console.planner_module._depth_preference_startup_bias == DEPTH_BIAS_MIN
        conv_id = "pd-e2e-3-brand-new"
        assert conv_id not in console.planner_module._depth_preference
        prompt = _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "pd-3a", conv_id)
        assert "Response depth: SHORT" in prompt, prompt
    finally:
        console.stop()


def test_e2e_4_concurrent_conversations_in_same_process_do_not_leak_mid_run_learning():
    """Hard constraint - 'do not create a global mutable preference
    shared between simultaneous conversations'. Conversation A's
    same-process, mid-run threshold-triggered persistence must NOT
    retroactively influence conversation B, which was already relying on
    the FROZEN startup snapshot taken before A ever ran."""
    assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_a = "pd-e2e-4-a"
        conv_b = "pd-e2e-4-b"
        for i in range(3):
            _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", f"pd-4a-{i}", conv_a)
        # conv_a's feedback crossed the threshold and updated the LIVE
        # in-memory persisted preference (and the on-disk file) ...
        assert console.planner_module._persistent_depth_preference.bias < 0
        # ... but conv_b, started fresh in this same process AFTER that
        # merge, must still see the ORIGINAL startup snapshot (0), not
        # conv_a's freshly-merged value.
        assert conv_b not in console.planner_module._depth_preference
        prompt_b = _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "pd-4b", conv_b)
        assert "Response depth: NORMAL" in prompt_b, prompt_b
    finally:
        console.stop()


def test_e2e_5_explicit_instruction_always_overrides_persisted_preference():
    """Priority order preserved - explicit user instruction beats every
    other signal, including a strongly-biased persisted baseline."""
    DepthPreferenceStore.save(PersistedDepthPreference(schema_version=1, bias=DEPTH_BIAS_MAX, sample_count=20))
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        prompt = _run_turn_and_capture(
            console, demo, "jawab singkat aja ya, apa itu resistor?", "pd-5a", "pd-e2e-5",
        )
        assert "Response depth: SHORT" in prompt, prompt
    finally:
        console.stop()


def test_e2e_6_conversation_end_best_effort_merge_below_threshold():
    """`_on_conversation_ended()` performs a FINAL, best-effort merge
    even when local feedback never crossed the per-turn %3 threshold -
    otherwise a conversation with only 1-2 feedback events would lose
    that evidence entirely once popped. Documented as a secondary path
    since `conversation_ended` is not currently routed to this module in
    production (see docs/change_impact/persistent_adaptive_response_depth.md)."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "pd-e2e-6"
        _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", "pd-6a", conv_id)
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
        local_pref = console.planner_module._depth_preference[conv_id]
        assert local_pref.feedback_count == 1

        console.planner_module._on_conversation_ended(demo.Event(type="conversation_ended", data={"session_id": conv_id}))
        assert os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
        on_disk = DepthPreferenceStore.load()
        assert on_disk.bias < 0
        assert conv_id not in console.planner_module._depth_preference  # still popped as before
    finally:
        console.stop()


def test_e2e_7_conversation_end_no_feedback_never_persists():
    """A conversation that never produced real depth feedback must never
    write a spurious merge on conversation-end either."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "pd-e2e-7"
        _run_turn_and_capture(console, demo, BORDERLINE_NORMAL_QUERY, "pd-7a", conv_id)
        assert conv_id not in console.planner_module._depth_preference
        console.planner_module._on_conversation_ended(demo.Event(type="conversation_ended", data={"session_id": conv_id}))
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
    finally:
        console.stop()


def test_e2e_8_on_disk_schema_matches_spec_exactly():
    """Privacy/trust boundary audit against the REAL production write
    path (not just the unit-level `to_dict()` test) - no raw feedback
    text, no transcript, no query history, no timestamps."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "pd-e2e-8"
        for i in range(3):
            _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", f"pd-8a-{i}", conv_id)
        with open(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        assert set(raw.keys()) == {"schema_version", "bias", "sample_count"}
        assert raw["schema_version"] == SCHEMA_VERSION
        assert isinstance(raw["bias"], int)
        assert isinstance(raw["sample_count"], int)
        assert DEPTH_BIAS_MIN <= raw["bias"] <= DEPTH_BIAS_MAX
        # no raw text anywhere in the serialized file
        serialized = json.dumps(raw)
        assert "kepanjangan" not in serialized
        assert "singkat" not in serialized
    finally:
        console.stop()


def test_e2e_10_repeated_detailed_direction_feedback_also_persists_symmetrically():
    """Sprint brief scenario I - the SAME threshold-gated persistence
    path proven in `test_e2e_2` (short direction) must also work in the
    opposite (detailed) direction. `merge_conversation_into_persistent()`/
    `should_persist()` are direction-agnostic by construction, but this
    is the one test that proves it end-to-end through the real
    production bridge rather than only at the pure-function level
    (`test_Q`/`test_T` above)."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "pd-e2e-10"
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
        for i in range(2):
            _run_turn_and_capture(console, demo, "terlalu singkat, jelaskan lebih detail", f"pd-10a-{i}", conv_id)
        assert not os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)  # below threshold

        _run_turn_and_capture(console, demo, "terlalu singkat, jelaskan lebih detail", "pd-10a-2", conv_id)
        assert os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)  # 3rd event crosses it

        local_pref = console.planner_module._depth_preference[conv_id]
        assert local_pref.feedback_count == 3
        assert local_pref.bias > 0  # leans DETAILED locally
        on_disk = DepthPreferenceStore.load()
        expected = merge_conversation_into_persistent(PersistedDepthPreference(), local_pref.bias)
        assert on_disk == expected
        assert on_disk.bias > 0  # leans DETAILED persistently
        assert on_disk.bias != local_pref.bias  # blended, not overwritten
    finally:
        console.stop()


def test_e2e_11_explicit_detailed_instruction_overrides_a_persisted_short_preference():
    """Symmetric counterpart to `test_e2e_5` - a strongly SHORT-leaning
    persisted baseline must not stop an explicit 'jelaskan detail'
    request for THIS turn from resolving to DETAILED. Proves the
    explicit-instruction short-circuit in `compute_response_policy()`
    (checked before `adaptive_modifier` is ever applied) holds
    regardless of which direction the persisted preference leans."""
    DepthPreferenceStore.save(PersistedDepthPreference(schema_version=1, bias=DEPTH_BIAS_MIN, sample_count=20))
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        assert console.planner_module._depth_preference_startup_bias == DEPTH_BIAS_MIN
        prompt = _run_turn_and_capture(
            console, demo, "jelaskan secara detail dong, apa itu resistor?", "pd-11a", "pd-e2e-11",
        )
        assert "Response depth: DETAILED" in prompt, prompt
    finally:
        console.stop()


def test_e2e_9_concurrent_conversations_saving_simultaneously_do_not_corrupt_the_file():
    """Thread-safety smoke test for `_persistent_depth_preference_lock` -
    two conversations both crossing the persistence threshold at close
    to the same time must never race-corrupt the shared in-memory value
    or the on-disk file."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        errors = []

        def _drive(conv_id, prefix):
            try:
                for i in range(3):
                    _run_turn_and_capture(console, demo, "kepanjangan, singkat aja", f"{prefix}-{i}", conv_id)
            except Exception as exc:  # pragma: no cover - failure surfaced via `errors`
                errors.append(exc)

        threads = [
            threading.Thread(target=_drive, args=("pd-e2e-9-a", "pd-9a")),
            threading.Thread(target=_drive, args=("pd-e2e-9-b", "pd-9b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        assert not errors, errors
        assert os.path.exists(luno_config.RESPONSE_DEPTH_PREFERENCE_FILE)
        # file must still be valid, schema-conformant JSON - not truncated
        # or interleaved by the race.
        on_disk = DepthPreferenceStore.load()
        assert DEPTH_BIAS_MIN <= on_disk.bias <= DEPTH_BIAS_MAX
        assert 0 <= on_disk.sample_count <= MAX_SAMPLE_COUNT
    finally:
        console.stop()
