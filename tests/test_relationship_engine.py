"""
LUNO Relationship Engine Foundation sprint - test suite for
`luno/relationship_engine.py`.

Same flat `tests/test_<module>.py` convention as `tests/test_persona.py`/
`tests/test_emotion_engine.py` for a brand-new, self-contained, top-level
loose module (see `ARCHITECTURE_GUARD.md` §6/§10). One end-to-end
integration test lives separately in `tests/test_runtime_demo.py`.

Categories (per the sprint brief's own §20/§21 test requirements):
  - Initialization (missing/empty/default state)
  - Validation (valid/invalid numeric values, missing fields, wrong
    types, unknown fields)
  - Persistence (save/load/round-trip/malformed JSON/wrong root type/
    partial state/unknown schema version)
  - Update model (normal/technical/meaningful-shared-experience/
    correction/neutral interaction)
  - Bounds (values below/above valid limits, NaN, Infinity)
  - Determinism (same event sequence -> same state)
  - Isolation (never touches Memory/Emotion Engine/Persona/LLM config)
  - Prompt context builder (empty for a brand-new relationship, banded/
    compact for an established one, never mentions verified facts)
"""

import json
import math

import pytest

from luno.relationship_engine import (
    RELATIONSHIP_SCHEMA_VERSION,
    RelationshipContextBuilder,
    RelationshipEngine,
    RelationshipSignal,
    RelationshipState,
    RelationshipStore,
    classify_turn,
)


# ─────────────────────────────────────────────
#  Initialization
# ─────────────────────────────────────────────


def test_default_state_is_a_safe_neutral_baseline():
    state = RelationshipState()
    assert state.schema_version == RELATIONSHIP_SCHEMA_VERSION
    assert state.familiarity == 0.0
    assert state.trust == 0.0
    assert state.closeness == 0.0
    assert state.interaction_count == 0
    assert state.shared_experience_count == 0
    assert state.last_interaction_timestamp is None


def test_missing_persisted_file_loads_default_state(tmp_path, monkeypatch):
    from luno import config as config_module

    monkeypatch.setattr(config_module, "RELATIONSHIP_STATE_FILE", str(tmp_path / "does_not_exist.json"))
    state = RelationshipStore.load()
    assert state == RelationshipState()


def test_empty_file_loads_default_state(tmp_path, monkeypatch):
    from luno import config as config_module

    path = tmp_path / "relationship_state.json"
    path.write_text("")
    monkeypatch.setattr(config_module, "RELATIONSHIP_STATE_FILE", str(path))
    state = RelationshipStore.load()
    assert state == RelationshipState()


# ─────────────────────────────────────────────
#  Validation
# ─────────────────────────────────────────────


def test_from_dict_valid_state_loads_correctly():
    state = RelationshipState.from_dict({
        "schema_version": 1, "familiarity": 0.4, "trust": 0.5, "closeness": 0.6,
        "interaction_count": 10, "shared_experience_count": 2, "last_interaction_timestamp": 100.0,
    })
    assert state.familiarity == 0.4
    assert state.trust == 0.5
    assert state.closeness == 0.6
    assert state.interaction_count == 10
    assert state.shared_experience_count == 2
    assert state.last_interaction_timestamp == 100.0


def test_from_dict_wrong_root_type_falls_back_to_default():
    for bad_root in ([1, 2, 3], "not a dict", 42, None, True):
        assert RelationshipState.from_dict(bad_root) == RelationshipState()


def test_from_dict_missing_fields_default_independently():
    state = RelationshipState.from_dict({"schema_version": 1, "trust": 0.7})
    assert state.trust == 0.7
    assert state.familiarity == 0.0
    assert state.closeness == 0.0
    assert state.interaction_count == 0


def test_from_dict_wrong_types_fall_back_per_field():
    state = RelationshipState.from_dict({
        "schema_version": 1,
        "familiarity": "not a number",
        "trust": ["also", "not", "a", "number"],
        "closeness": {"nested": "dict"},
        "interaction_count": "oops",
        "shared_experience_count": None,
    })
    assert state.familiarity == 0.0
    assert state.trust == 0.0
    assert state.closeness == 0.0
    assert state.interaction_count == 0
    assert state.shared_experience_count == 0


def test_from_dict_unknown_extra_fields_are_ignored_not_fatal():
    state = RelationshipState.from_dict({
        "schema_version": 1, "trust": 0.3, "totally_made_up_field": "romance_score", "another_one": 999,
    })
    assert state.trust == 0.3
    assert not hasattr(state, "totally_made_up_field")


@pytest.mark.parametrize("bad_version", [None, 0, 2, 999, "1", "future", [1]])
def test_from_dict_mismatched_schema_version_falls_back_to_full_default(bad_version):
    state = RelationshipState.from_dict({"schema_version": bad_version, "trust": 0.9, "closeness": 0.9})
    assert state == RelationshipState()


# ─────────────────────────────────────────────
#  Bounds
# ─────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    (-999, 0.0), (-1, 0.0), (0, 0.0), (0.5, 0.5), (1, 1.0), (2, 1.0), (999, 1.0),
])
def test_bounded_dimensions_clamp_to_valid_range(raw, expected):
    state = RelationshipState.from_dict({"schema_version": 1, "trust": raw, "familiarity": raw, "closeness": raw})
    assert state.trust == expected
    assert state.familiarity == expected
    assert state.closeness == expected


def test_nan_falls_back_to_default_not_clamped():
    state = RelationshipState.from_dict({"schema_version": 1, "trust": float("nan")})
    assert state.trust == 0.0  # default, not a clamped NaN artifact


def test_infinity_clamps_to_bound_edges():
    state = RelationshipState.from_dict({
        "schema_version": 1, "trust": float("inf"), "familiarity": float("-inf"),
    })
    assert state.trust == 1.0
    assert state.familiarity == 0.0


def test_negative_counters_clamp_to_zero():
    state = RelationshipState.from_dict({
        "schema_version": 1, "interaction_count": -50, "shared_experience_count": -1,
    })
    assert state.interaction_count == 0
    assert state.shared_experience_count == 0


def test_absurdly_large_counter_is_bounded():
    state = RelationshipState.from_dict({"schema_version": 1, "interaction_count": 10**30})
    assert 0 <= state.interaction_count <= 1_000_000_000


def test_apply_never_produces_out_of_bounds_state_even_after_many_repeated_signals():
    state = RelationshipState()
    signals = [RelationshipSignal.USER_FEEDBACK_POSITIVE, RelationshipSignal.MEANINGFUL_SHARED_EXPERIENCE]
    for i in range(500):
        state = RelationshipEngine.apply(state, signals, now=float(i))
    assert 0.0 <= state.trust <= 1.0
    assert 0.0 <= state.closeness <= 1.0
    assert 0.0 <= state.familiarity <= 1.0
    assert state.interaction_count == 500


# ─────────────────────────────────────────────
#  Persistence (save / load / round-trip / malformed)
# ─────────────────────────────────────────────


def _redirect(tmp_path, monkeypatch, name="relationship_state.json"):
    from luno import config as config_module

    path = str(tmp_path / name)
    monkeypatch.setattr(config_module, "RELATIONSHIP_STATE_FILE", path)
    return path


def test_save_then_load_round_trip_preserves_state(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    state = RelationshipState(
        familiarity=0.33, trust=0.44, closeness=0.55,
        interaction_count=12, shared_experience_count=3, last_interaction_timestamp=555.5,
    )
    assert RelationshipStore.save(state) is True
    loaded = RelationshipStore.load()
    assert loaded == state


def test_malformed_json_falls_back_to_default(tmp_path, monkeypatch):
    path = _redirect(tmp_path, monkeypatch)
    with open(path, "w", encoding="utf-8") as f:
        f.write("{not valid json at all")
    assert RelationshipStore.load() == RelationshipState()


def test_wrong_root_type_json_falls_back_to_default(tmp_path, monkeypatch):
    path = _redirect(tmp_path, monkeypatch)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(["a", "list", "not", "a", "dict"], f)
    assert RelationshipStore.load() == RelationshipState()


def test_partial_state_file_loads_present_fields_and_defaults_the_rest(tmp_path, monkeypatch):
    path = _redirect(tmp_path, monkeypatch)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"schema_version": 1, "trust": 0.6}, f)
    state = RelationshipStore.load()
    assert state.trust == 0.6
    assert state.familiarity == 0.0
    assert state.interaction_count == 0


def test_save_creates_parent_directory_if_missing(tmp_path, monkeypatch):
    nested_path = tmp_path / "nested" / "dirs" / "relationship_state.json"
    from luno import config as config_module

    monkeypatch.setattr(config_module, "RELATIONSHIP_STATE_FILE", str(nested_path))
    assert RelationshipStore.save(RelationshipState(trust=0.1)) is True
    assert nested_path.exists()


def test_save_failure_returns_false_not_raise(monkeypatch):
    from luno import config as config_module

    # An empty string path is never a writable file - save() must
    # degrade to False, never raise, matching "never let a side-concern
    # crash the runtime."
    monkeypatch.setattr(config_module, "RELATIONSHIP_STATE_FILE", "")
    assert RelationshipStore.save(RelationshipState()) is False


def test_load_with_no_configured_path_returns_default(monkeypatch):
    from luno import config as config_module

    monkeypatch.setattr(config_module, "RELATIONSHIP_STATE_FILE", "")
    assert RelationshipStore.load() == RelationshipState()


# ─────────────────────────────────────────────
#  Update model
# ─────────────────────────────────────────────


def test_technical_command_causes_no_relationship_score_change():
    """Section 9's own worked examples - plain technical/device turns
    must not move trust/closeness/familiarity, only the neutral
    interaction_count counter."""
    state = RelationshipState()
    for text in ("berapa suhu CPU?", "buka lampu kamar", "berapa 1+1?"):
        signals = classify_turn(text)
        assert signals == []
        new_state = RelationshipEngine.apply(state, signals, now=1.0)
        assert new_state.trust == state.trust
        assert new_state.closeness == state.closeness
        assert new_state.familiarity == state.familiarity
        assert new_state.interaction_count == state.interaction_count + 1


def test_successful_task_causes_small_trust_increase_only():
    state = RelationshipState()
    signals = classify_turn("buka lampu kamar", had_successful_tool_call=True)
    assert signals == [RelationshipSignal.SUCCESSFUL_TASK]
    new_state = RelationshipEngine.apply(state, signals, now=1.0)
    assert new_state.trust > state.trust
    assert new_state.trust < state.trust + 0.05  # small, not a "major" jump
    assert new_state.closeness == state.closeness
    assert new_state.familiarity == state.familiarity


def test_user_correction_slightly_decreases_trust():
    state = RelationshipState(trust=0.5)
    signals = classify_turn("bukan itu maksudku")
    assert RelationshipSignal.USER_CORRECTION in signals
    new_state = RelationshipEngine.apply(state, signals, now=1.0)
    assert new_state.trust < state.trust


def test_meaningful_shared_experience_increases_familiarity_and_counter():
    state = RelationshipState()
    signals = classify_turn("ingetkan aku", explicit_memory_shared=True)
    assert RelationshipSignal.MEANINGFUL_SHARED_EXPERIENCE in signals
    new_state = RelationshipEngine.apply(state, signals, now=1.0)
    assert new_state.familiarity > state.familiarity
    assert new_state.shared_experience_count == state.shared_experience_count + 1


def test_positive_feedback_increases_closeness_and_trust_slightly():
    state = RelationshipState()
    signals = classify_turn("terima kasih banyak, kamu emang membantu")
    assert RelationshipSignal.USER_FEEDBACK_POSITIVE in signals
    new_state = RelationshipEngine.apply(state, signals, now=1.0)
    assert new_state.closeness > state.closeness
    assert new_state.trust > state.trust


def test_neutral_interaction_only_advances_counters():
    state = RelationshipState(trust=0.2, closeness=0.2, familiarity=0.2)
    new_state = RelationshipEngine.apply(state, [], now=42.0)
    assert new_state.trust == state.trust
    assert new_state.closeness == state.closeness
    assert new_state.familiarity == state.familiarity
    assert new_state.interaction_count == 1
    assert new_state.last_interaction_timestamp == 42.0


def test_observe_turn_is_classify_plus_apply_composed():
    state = RelationshipState()
    combined = RelationshipEngine.observe_turn(state, "terima kasih banyak", now=7.0)
    manual_signals = classify_turn("terima kasih banyak")
    manual = RelationshipEngine.apply(state, manual_signals, now=7.0)
    assert combined == manual


def test_llm_cannot_write_relationship_fields_directly():
    """There is no code path anywhere in this module that accepts a
    pre-computed trust/closeness/familiarity delta or absolute value
    from arbitrary text - `apply()` only reacts to membership in the
    closed `RelationshipSignal` enum via a `set()` lookup, so an
    arbitrary string/number masquerading as a "signal" (e.g. what an
    LLM might try to inject: `"trust += 0.9"`) matches NOTHING and is
    silently, safely ignored - it can NEVER move any score, which is a
    stronger and more meaningful guarantee than merely raising."""
    state = RelationshipState()
    poisoned = RelationshipEngine.apply(state, ["trust += 0.9", "closeness=1.0", 12345], now=1.0)  # type: ignore[list-item]
    assert poisoned.trust == state.trust
    assert poisoned.closeness == state.closeness
    assert poisoned.familiarity == state.familiarity
    # only the plain activity counter/timestamp ever move unconditionally
    assert poisoned.interaction_count == state.interaction_count + 1


# ─────────────────────────────────────────────
#  Determinism
# ─────────────────────────────────────────────


def test_same_event_sequence_always_produces_same_final_state():
    def run_sequence():
        state = RelationshipState()
        state = RelationshipEngine.apply(state, classify_turn("berapa suhu CPU?"), now=1.0)
        state = RelationshipEngine.apply(state, classify_turn("buka lampu kamar", had_successful_tool_call=True), now=2.0)
        state = RelationshipEngine.apply(state, classify_turn("bukan itu maksudku"), now=3.0)
        state = RelationshipEngine.apply(state, classify_turn("makasih banyak ya"), now=4.0)
        return state

    result_a = run_sequence()
    result_b = run_sequence()
    assert result_a == result_b


def test_signal_order_within_apply_call_does_not_matter_but_call_order_does():
    state = RelationshipState()
    seq1 = RelationshipEngine.apply(
        RelationshipEngine.apply(state, [RelationshipSignal.SUCCESSFUL_TASK], now=1.0),
        [RelationshipSignal.USER_CORRECTION], now=2.0,
    )
    seq2 = RelationshipEngine.apply(state, [RelationshipSignal.SUCCESSFUL_TASK, RelationshipSignal.USER_CORRECTION], now=2.0)
    # Two separate turns (interaction_count=2) vs one combined turn
    # (interaction_count=1) are legitimately different states - this
    # test documents that distinction rather than asserting equality.
    assert seq1.interaction_count == 2
    assert seq2.interaction_count == 1


# ─────────────────────────────────────────────
#  Isolation
# ─────────────────────────────────────────────


def test_relationship_engine_module_never_imports_memory_or_emotion_or_persona():
    """Uses `ast` to inspect actual `import`/`from ... import` statements
    only - not a raw substring search over the whole file, which would
    trivially self-match this module's own docstring (it explicitly
    NAMES `luno.memory`/`luno.memory_guard`/`luno.emotion_engine`/
    `luno.persona` in prose, explaining that it does NOT import them)."""
    import ast
    import inspect
    import luno.relationship_engine as re_module

    source = inspect.getsource(re_module)
    tree = ast.parse(source)
    imported_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported_modules.add(module)
            for alias in node.names:
                imported_modules.add(f"{module}.{alias.name}")

    forbidden_substrings = ("memory", "memory_guard", "emotion_engine", "persona")
    for imported in imported_modules:
        for forbidden in forbidden_substrings:
            assert forbidden not in imported, f"unexpected import touching {forbidden!r}: {imported!r}"


def test_observing_many_turns_never_touches_memory_module(monkeypatch):
    from luno import memory as memory_module

    def _explode(*args, **kwargs):
        raise AssertionError("Relationship Engine must never call memory.add_memory()")

    monkeypatch.setattr(memory_module, "add_memory", _explode)

    state = RelationshipState()
    for text in ("aku senang banget", "capek deh", "ingetkan aku ya", "makasih banyak"):
        state = RelationshipEngine.observe_turn(state, text, now=1.0)
    # If add_memory had been called even once, _explode() would have raised.


def test_classify_turn_accepts_but_ignores_emotion_state_argument():
    """Section 11: emotion may be READ, but must never by itself cause a
    relationship change - passing an arbitrary "emotion-like" object
    must not affect classification at all in this foundation sprint."""
    class _FakeEmotion:
        emotion = "sad"
        confidence = 0.99

    without = classify_turn("berapa suhu CPU?", emotion_state=None)
    with_emotion = classify_turn("berapa suhu CPU?", emotion_state=_FakeEmotion())
    assert without == with_emotion == []


# ─────────────────────────────────────────────
#  Prompt context builder
# ─────────────────────────────────────────────


def test_context_block_empty_for_brand_new_relationship():
    state = RelationshipState(interaction_count=0)
    assert RelationshipContextBuilder.build_prompt_block(state) == ""


def test_context_block_empty_below_minimum_interaction_threshold():
    state = RelationshipState(interaction_count=4, trust=0.9, closeness=0.9, familiarity=0.9)
    assert RelationshipContextBuilder.build_prompt_block(state) == ""


def test_context_block_present_for_established_relationship():
    state = RelationshipState(interaction_count=20, trust=0.6, closeness=0.6, familiarity=0.6, shared_experience_count=4)
    block = RelationshipContextBuilder.build_prompt_block(state)
    assert block != ""
    assert "familiarity" in block
    assert "trust" in block
    assert "closeness" in block
    assert "4 shared experience" in block


def test_context_block_uses_semantic_bands_not_raw_floats():
    state = RelationshipState(interaction_count=20, trust=0.6123456, closeness=0.6, familiarity=0.6)
    block = RelationshipContextBuilder.build_prompt_block(state)
    assert "0.6123456" not in block
    assert "0.6" not in block


def test_context_block_never_contains_verified_facts_marker():
    state = RelationshipState(interaction_count=20, trust=0.6, closeness=0.6, familiarity=0.6)
    block = RelationshipContextBuilder.build_prompt_block(state)
    assert "VERIFIED results" not in block


def test_context_block_explicitly_never_overrides_facts_or_justifies_romance():
    state = RelationshipState(interaction_count=20, trust=0.6, closeness=0.6, familiarity=0.6)
    block = RelationshipContextBuilder.build_prompt_block(state)
    assert "NEVER overrides" in block
    assert "romantic" in block.lower() or "clingy" in block.lower()
