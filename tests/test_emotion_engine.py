"""
LUNO Emotion Engine sprint - test suite for `luno/emotion_engine.py`.

Mirrors this repository's existing test-hygiene conventions (see
`ARCHITECTURE_GUARD.md` §6/§10): a brand-new, self-contained, top-level
loose module (`luno/persona.py` -> `tests/test_persona.py`,
`luno/memory.py` -> `tests/test_memory_regression.py`) gets its own flat
`tests/test_<module>.py` file rather than a new package/subdirectory
layout. One end-to-end integration test lives separately in
`tests/test_runtime_demo.py`, alongside the equivalent persona
integration test, per that same convention.

Categories (per the sprint brief's own §22 test requirements):
  - User emotion estimation (all named categories, ambiguous/mixed,
    low-confidence, unknown/no-signal)
  - State tracking (replacement by new evidence, time decay, current-
    context precedence, session-boundary reset)
  - Response policy (per-emotion deltas, low-confidence gate, the
    technical_depth invariant)
  - Memory separation (never calls memory.add_memory, malformed/odd
    input never crashes anything)
  - LLM prompt integration (bounded block, uncertainty hedging, never
    mentions the VERIFIED-facts marker)
"""

import time

import pytest

from luno import config
from luno.emotion_engine import (
    EmotionEstimator,
    EmotionStateTracker,
    ResponsePolicy,
    UserEmotion,
    UserEmotionState,
    build_emotional_context_prompt,
    derive_response_policy,
)


# ─────────────────────────────────────────────
#  User emotion estimation
# ─────────────────────────────────────────────


def test_neutral_device_command_has_no_confident_emotion():
    """A plain smart-home command carries no emotional signal at all -
    must never be mis-read as some emotion just because it's a sentence."""
    state = EmotionEstimator.estimate_from_text("turn on the lights")
    assert state.emotion == UserEmotion.UNKNOWN
    assert state.confidence == 0.0
    assert not state.is_confident()


def test_positive_emotion_happy():
    state = EmotionEstimator.estimate_from_text("aku senang banget hari ini, makasih banyak!")
    assert state.emotion == UserEmotion.HAPPY
    assert state.is_confident()
    assert state.valence > 0


def test_negative_emotion_sad():
    state = EmotionEstimator.estimate_from_text("aku sedih banget, pengen nangis rasanya")
    assert state.emotion == UserEmotion.SAD
    assert state.is_confident()
    assert state.valence < 0


def test_frustration_detected():
    state = EmotionEstimator.estimate_from_text("ugh, masih error lagi, kesel banget aku")
    assert state.emotion == UserEmotion.FRUSTRATED
    assert state.is_confident()


def test_tiredness_detected():
    state = EmotionEstimator.estimate_from_text("capek banget hari ini")
    assert state.emotion == UserEmotion.TIRED
    assert state.is_confident()
    assert state.energy < 0.5


def test_excitement_detected():
    state = EmotionEstimator.estimate_from_text("YESSS akhirnya berhasil!!!")
    assert state.emotion == UserEmotion.EXCITED
    assert state.is_confident()
    assert state.energy > 0.5
    assert state.valence > 0


def test_technical_why_question_stays_below_confidence_threshold():
    """Emotion Engine sprint's own worked example (section 10): a
    troubleshooting "kenapa ... terus?" question must NOT read as a
    confident emotional state - "kenapa" and a bare trailing "?" are
    deliberately weak signals for exactly this reason."""
    state = EmotionEstimator.estimate_from_text("Kenapa Docker container-ku restart terus?")
    assert not state.is_confident()


def test_explicit_curiosity_is_detected_confidently():
    """Contrast case for the test above - EXPLICIT curiosity wording
    (not just a bare "why"/"?") should cross the confidence bar."""
    state = EmotionEstimator.estimate_from_text("aku penasaran banget kenapa ini bisa kejadian")
    assert state.emotion == UserEmotion.CURIOUS
    assert state.is_confident()


def test_ambiguous_mixed_signal_is_discounted():
    """Section 6: 'must gracefully handle ... mixed emotion' - text that
    reads as two different emotions at once should end up LESS confident
    than either single, clean emotion would on its own."""
    clean = EmotionEstimator.estimate_from_text("aku senang banget hari ini")
    mixed = EmotionEstimator.estimate_from_text("aku senang tapi juga sedih banget, campur aduk")
    assert mixed.confidence < clean.confidence


def test_low_confidence_bare_question_mark_is_unknown():
    """A single very weak marker (bare trailing '?') alone must not be
    enough to produce a confident read."""
    state = EmotionEstimator.estimate_from_text("gitu ya?")
    assert state.emotion == UserEmotion.UNKNOWN


def test_unknown_for_text_with_zero_keyword_matches():
    state = EmotionEstimator.estimate_from_text("tolong nyalain lampu ruang tamu")
    assert state.emotion == UserEmotion.UNKNOWN
    assert state.confidence == 0.0


@pytest.mark.parametrize("bad_input", [None, "", "   ", 12345, ["not", "a", "string"]])
def test_estimator_never_raises_on_malformed_input(bad_input):
    """Section 20: Emotion Engine must be non-critical - malformed input
    (including non-string types a caller should never pass, but might by
    accident) must degrade to the safe UNKNOWN state, never raise."""
    try:
        state = EmotionEstimator.estimate_from_text(bad_input)  # type: ignore[arg-type]
    except Exception as ex:  # pragma: no cover - the whole point is this never happens
        pytest.fail(f"estimate_from_text raised on {bad_input!r}: {ex}")
    assert state.emotion == UserEmotion.UNKNOWN
    assert state.confidence == 0.0


def test_state_is_immutable():
    state = EmotionEstimator.estimate_from_text("aku senang banget")
    with pytest.raises(Exception):
        state.emotion = UserEmotion.SAD  # type: ignore[misc]


def test_to_dict_is_a_plain_json_safe_structure():
    state = EmotionEstimator.estimate_from_text("aku senang banget hari ini")
    d = state.to_dict()
    assert d["emotion"] == "happy"
    assert isinstance(d["confidence"], float)
    assert isinstance(d["timestamp"], float)


# ─────────────────────────────────────────────
#  State tracking (decay / replacement / session boundaries)
# ─────────────────────────────────────────────


def test_tracker_starts_unknown():
    tracker = EmotionStateTracker()
    assert tracker.current().emotion == UserEmotion.UNKNOWN


def test_tracker_observe_updates_current_state():
    tracker = EmotionStateTracker()
    tracker.observe("capek banget hari ini")
    assert tracker.current().emotion == UserEmotion.TIRED


def test_fresh_evidence_replaces_old_state_current_context_takes_precedence():
    """Section 12: 'current context takes precedence over old context' -
    a fresh, different emotion this turn must immediately replace what
    was tracked before, not blend/average with it."""
    tracker = EmotionStateTracker()
    tracker.observe("aku sedih banget")
    assert tracker.current().emotion == UserEmotion.SAD
    tracker.observe("YESSS akhirnya berhasil!!!")
    assert tracker.current().emotion == UserEmotion.EXCITED


def test_no_fresh_signal_keeps_previous_state_within_decay_window():
    tracker = EmotionStateTracker(decay_seconds=3600.0)
    tracker.observe("capek banget hari ini")
    assert tracker.current().emotion == UserEmotion.TIRED
    # A neutral follow-up with no emotional signal of its own shouldn't
    # erase the still-recent tiredness read.
    tracker.observe("tolong nyalain lampu")
    assert tracker.current().emotion == UserEmotion.TIRED


def test_stale_emotion_decays_to_unknown_after_the_configured_window():
    """Section 11: 'emotion should not persist forever' - a very short
    decay window makes a previously-set state expire on the next read."""
    tracker = EmotionStateTracker(decay_seconds=0.05)
    tracker.observe("capek banget hari ini")
    assert tracker.current().emotion == UserEmotion.TIRED
    time.sleep(0.12)
    assert tracker.current().emotion == UserEmotion.UNKNOWN


def test_reset_clears_tracked_state_session_boundary():
    tracker = EmotionStateTracker()
    tracker.observe("aku sedih banget")
    assert tracker.current().emotion == UserEmotion.SAD
    tracker.reset()
    assert tracker.current().emotion == UserEmotion.UNKNOWN


def test_tracker_observe_never_raises_on_malformed_input():
    tracker = EmotionStateTracker()
    for bad in (None, "", 42, object()):
        try:
            tracker.observe(bad)  # type: ignore[arg-type]
        except Exception as ex:  # pragma: no cover
            pytest.fail(f"observe() raised on {bad!r}: {ex}")


# ─────────────────────────────────────────────
#  Response policy
# ─────────────────────────────────────────────


def test_confident_tired_state_produces_a_non_noop_policy():
    state = EmotionEstimator.estimate_from_text("capek banget hari ini")
    policy = derive_response_policy(state)
    assert not policy.is_noop()
    assert policy.warmth >= 1
    assert policy.energy <= -1


def test_low_confidence_state_always_produces_noop_policy():
    """Section 7: 'low-confidence emotion should have little or no effect
    on Luno behavior' - enforced structurally, not left to prompt wording."""
    weak_state = UserEmotionState(emotion=UserEmotion.SAD, intensity=0.3, confidence=0.1)
    policy = derive_response_policy(weak_state)
    assert policy.is_noop()


def test_unknown_state_always_produces_noop_policy():
    policy = derive_response_policy(UserEmotionState())
    assert policy.is_noop()


def test_response_policy_never_reduces_technical_depth():
    """Core safety property (section 8/10): emotion can color warmth/
    humor/tone, but must NEVER be allowed to touch technical_depth for
    ANY emotion category - iterates the whole table so a future
    contributor adding a new row can't silently violate this."""
    for emotion in UserEmotion:
        confident_state = UserEmotionState(emotion=emotion, intensity=0.8, confidence=0.9)
        policy = derive_response_policy(confident_state)
        assert policy.technical_depth == 0, f"{emotion} leaked into technical_depth"


def test_excited_policy_leans_energetic_and_warm():
    state = EmotionEstimator.estimate_from_text("YESSS akhirnya berhasil!!!")
    policy = derive_response_policy(state)
    assert policy.energy >= 1
    assert policy.humor >= 1


def test_response_policy_is_immutable():
    policy = ResponsePolicy(warmth=1)
    with pytest.raises(Exception):
        policy.warmth = 2  # type: ignore[misc]


# ─────────────────────────────────────────────
#  Memory separation
# ─────────────────────────────────────────────


def test_emotion_engine_module_never_imports_memory_subsystem():
    """Section 13: temporary emotional state must never be automatically
    persisted through the memory subsystem. The strongest guarantee of
    that is structural: this module doesn't even import
    `luno.memory`/`luno.memory_guard` at all, so there is no code path
    inside it that COULD call `add_memory()`."""
    import luno.emotion_engine as ee_module
    import inspect

    source = inspect.getsource(ee_module)
    assert "import memory" not in source
    assert "memory_guard" not in source
    assert "add_memory" not in source


def test_observing_many_emotional_turns_never_touches_long_term_memory(monkeypatch, tmp_path):
    """Behavioral (not just structural) confirmation of the same
    guarantee - patches `luno.memory.add_memory` to explode if ever
    called, then runs a stream of emotionally-loaded turns through the
    tracker and confirms it was never invoked."""
    from luno import memory as memory_module

    def _explode(*args, **kwargs):
        raise AssertionError("Emotion Engine must never call memory.add_memory()")

    monkeypatch.setattr(memory_module, "add_memory", _explode)

    tracker = EmotionStateTracker()
    turns = [
        "aku sedih banget",
        "capek banget hari ini",
        "YESSS akhirnya berhasil!!!",
        "ugh kesel banget masih error",
        "aku penasaran banget kenapa ini kejadian",
    ]
    for text in turns:
        tracker.observe(text)
        derive_response_policy(tracker.current())
        build_emotional_context_prompt(tracker.current(), derive_response_policy(tracker.current()))
    # If add_memory had been called even once, the monkeypatched
    # _explode() above would already have raised inside the loop.


# ─────────────────────────────────────────────
#  LLM prompt integration
# ─────────────────────────────────────────────


def test_prompt_block_present_when_confident_and_policy_is_non_noop():
    state = EmotionEstimator.estimate_from_text("capek banget hari ini")
    policy = derive_response_policy(state)
    block = build_emotional_context_prompt(state, policy)
    assert block != ""
    assert "Inferred emotional context" in block
    assert "tired" in block


def test_prompt_block_hedges_uncertainty_and_never_states_emotion_as_fact():
    state = EmotionEstimator.estimate_from_text("aku sedih banget")
    policy = derive_response_policy(state)
    block = build_emotional_context_prompt(state, policy)
    assert "uncertain" in block.lower()
    assert "Do not state this as fact" in block


def test_prompt_block_empty_for_low_confidence_state():
    weak_state = UserEmotionState(emotion=UserEmotion.ANGRY, intensity=0.2, confidence=0.1)
    policy = derive_response_policy(weak_state)
    block = build_emotional_context_prompt(weak_state, policy)
    assert block == ""


def test_prompt_block_empty_for_unknown_state():
    block = build_emotional_context_prompt(UserEmotionState(), ResponsePolicy())
    assert block == ""


def test_prompt_block_never_contains_the_verified_facts_marker():
    """Same guarantee `tests/test_persona.py::
    test_persona_prompt_never_contains_the_verified_facts_marker` proves
    for the persona block - the emotional-context note must never be
    able to masquerade as (or interfere with) the honest, verified tool-
    result grounding block built by `build_verified_action_notes()`."""
    state = EmotionEstimator.estimate_from_text("capek banget hari ini")
    policy = derive_response_policy(state)
    block = build_emotional_context_prompt(state, policy)
    assert "VERIFIED results" not in block


def test_prompt_block_explicitly_never_overrides_technical_or_safety():
    state = EmotionEstimator.estimate_from_text("capek banget hari ini")
    policy = derive_response_policy(state)
    block = build_emotional_context_prompt(state, policy)
    assert "NEVER overrides" in block
    assert "technical" in block.lower()


def test_default_low_confidence_threshold_matches_config():
    assert 0.0 <= config.EMOTION_LOW_CONFIDENCE_THRESHOLD <= 1.0
    assert config.EMOTION_DECAY_SECONDS > 0
