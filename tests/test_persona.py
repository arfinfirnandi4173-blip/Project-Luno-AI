"""
test_persona.py
=================

Personality Stabilization sprint - `luno/persona.py` (config/persona.json
-> a system-prompt personality block) previously had NO dedicated test
coverage at all, despite being a real, already-production-wired piece
(`PlannerBridgeModule._handle_utterance()` in `main_runtime_demo.py`
calls `build_persona_prompt()` unconditionally on every turn - see that
call site's own comment for the bug it fixed). This file closes that
gap.

Covers: safe loading (valid/missing/malformed/partial config), prompt
generation (every persona.json field actually reaches the prompt),
functional boundaries (persona can never impersonate the separate
tool-verification channel), and `build_persona_flavor_hint()`.

The full end-to-end "does a real turn's system_prompt actually contain
BOTH persona AND verified tool-result facts" integration check lives in
`tests/test_runtime_demo.py::test_llm_context_includes_persona_alongside_verified_facts_end_to_end`
(that file already has the `RuntimeDemoConsole` harness this would need)
- not duplicated here.

Run:
    python3 -m pytest tests/test_persona.py
"""

from __future__ import annotations

import json

import luno.persona as persona_module
from luno.persona import (
    PERSONA,
    _DEFAULT_PERSONA,
    build_persona_flavor_hint,
    build_persona_prompt,
    load_persona_config,
)


# ============================================================================
# Persona loading
# ============================================================================

def test_valid_persona_loads_from_real_config():
    """Sanity check against the REAL config/persona.json this project
    actually ships - not a synthetic fixture. Locks in the fields the
    rest of this project's prompt-ordering/character depends on."""
    cfg = load_persona_config()
    assert cfg["name"] == "Luno"
    assert cfg["personality"] == "composed"
    assert cfg["user_name"] == "Vinn"
    assert cfg["traits"]  # non-empty
    assert cfg["romantic_style"]
    assert "calm" in cfg["emotional_states"]


def test_missing_persona_file_falls_back_to_default_neutral(monkeypatch, tmp_path):
    """No crash, no exception surfaced - Luno must still run normally
    with a neutral default persona when persona.json simply isn't
    there yet (fresh install, wrong path, ...)."""
    monkeypatch.setattr(persona_module.config, "PERSONA_FILE", str(tmp_path / "does_not_exist.json"))
    cfg = load_persona_config()
    assert cfg == _DEFAULT_PERSONA
    assert cfg["personality"] == "neutral"
    assert cfg["traits"] == []


def test_malformed_json_falls_back_to_default_safely(monkeypatch, tmp_path):
    """Invalid JSON syntax (typo'd/corrupted file) must never crash
    Luno's startup - falls back to the same neutral default as a
    missing file."""
    bad_file = tmp_path / "persona.json"
    bad_file.write_text("{not valid json,,,", encoding="utf-8")
    monkeypatch.setattr(persona_module.config, "PERSONA_FILE", str(bad_file))
    cfg = load_persona_config()
    assert cfg == _DEFAULT_PERSONA


def test_malformed_json_wrong_top_level_type_falls_back_safely(monkeypatch, tmp_path):
    """Syntactically VALID json that isn't an object at all (e.g. a bare
    list) would raise inside the `{**_DEFAULT_PERSONA, **data}` merge -
    must still be caught and fall back, not propagate."""
    bad_file = tmp_path / "persona.json"
    bad_file.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(persona_module.config, "PERSONA_FILE", str(bad_file))
    cfg = load_persona_config()
    assert cfg == _DEFAULT_PERSONA


def test_partial_persona_merges_with_defaults(monkeypatch, tmp_path):
    """A persona.json that only sets a couple of fields must still
    produce a COMPLETE, usable persona dict - every field this project
    reads elsewhere (traits, speech_style, ...) stays present with its
    default value rather than being missing/KeyError-prone."""
    partial_file = tmp_path / "persona.json"
    partial_file.write_text(json.dumps({"name": "TestBot", "personality": "sassy"}), encoding="utf-8")
    monkeypatch.setattr(persona_module.config, "PERSONA_FILE", str(partial_file))
    cfg = load_persona_config()
    assert cfg["name"] == "TestBot"
    assert cfg["personality"] == "sassy"
    # Unspecified fields keep their defaults rather than vanishing
    assert cfg["traits"] == []
    assert cfg["speech_style"] == _DEFAULT_PERSONA["speech_style"]


def test_partial_speech_style_deep_merges_not_overwrites(monkeypatch, tmp_path):
    """`speech_style` is a nested dict - setting only ONE of its keys
    must not silently drop the other two (a naive `{**default, **data}`
    shallow merge would replace the whole sub-dict, losing
    `stammer_when_flustered`/`catchphrases`)."""
    partial_file = tmp_path / "persona.json"
    partial_file.write_text(json.dumps({"speech_style": {"japanese_flavor": "heavy"}}), encoding="utf-8")
    monkeypatch.setattr(persona_module.config, "PERSONA_FILE", str(partial_file))
    cfg = load_persona_config()
    assert cfg["speech_style"]["japanese_flavor"] == "heavy"
    assert cfg["speech_style"]["stammer_when_flustered"] is False  # still the default
    assert cfg["speech_style"]["catchphrases"] == []


def test_partial_emotional_states_deep_merges_adds_to_defaults(monkeypatch, tmp_path):
    """Same deep-merge guarantee as speech_style, for `emotional_states`
    (an open-ended dict, not a fixed schema)."""
    partial_file = tmp_path / "persona.json"
    partial_file.write_text(json.dumps({"emotional_states": {"sleepy": "Slower, softer replies."}}), encoding="utf-8")
    monkeypatch.setattr(persona_module.config, "PERSONA_FILE", str(partial_file))
    cfg = load_persona_config()
    assert cfg["emotional_states"]["sleepy"] == "Slower, softer replies."


# ============================================================================
# Prompt generation - every persona.json field must actually reach the
# assembled prompt `build_persona_prompt()` hands to the LLM.
# ============================================================================

_FULL_TEST_PERSONA = {
    **_DEFAULT_PERSONA,
    "name": "Testina",
    "full_name": "Test Intelligent Nurturing Assistant",
    "gender": "female",
    "role": "Test Companion",
    "personality": "composed",
    "user_name": "Tester",
    "traits": ["calm and observant", "never fabricates results"],
    "background": "A test fixture with a backstory.",
    "emotional_states": {"calm": "Speaks evenly.", "happy": "A bit more playful."},
    "humor_examples": ["The tests pass. Suspicious."],
    "smart_home_style": "Direct and verified, no pretending.",
    "technical_knowledge": ["Python", "pytest"],
    "caring_behaviors": ["reminds you to save your work"],
    "anger_triggers": ["skipped tests"],
    "romantic_style": "Light and respectful of boundaries.",
    "motto": "Verify, then speak.",
    "example_lines": ["Confirmed. It works."],
    "hobbies": ["watching CI pipelines"],
    "likes": ["green checkmarks"],
    "dislikes": ["flaky tests"],
    "speech_style": {"japanese_flavor": "none", "catchphrases": ["noted."], "stammer_when_flustered": True},
}


def test_prompt_contains_identity(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    prompt = build_persona_prompt()
    assert "Testina" in prompt
    assert "Test Intelligent Nurturing Assistant" in prompt
    assert "Test Companion" in prompt


def test_prompt_contains_ai_self_awareness(monkeypatch):
    """Section 5's hard requirement: never claims to be human. Only
    rendered when at least one identity field (full_name/role/gender/
    apparent_age) is set - `_FULL_TEST_PERSONA` sets all of them."""
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    prompt = build_persona_prompt()
    assert "you're an AI, never pretend human" in prompt


def test_prompt_contains_traits(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    prompt = build_persona_prompt()
    assert "calm and observant" in prompt
    assert "never fabricates results" in prompt


def test_prompt_contains_humor_examples(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    prompt = build_persona_prompt()
    assert "The tests pass. Suspicious." in prompt


def test_prompt_contains_emotional_states(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    prompt = build_persona_prompt()
    assert "Speaks evenly." in prompt
    assert "A bit more playful." in prompt


def test_prompt_contains_caring_behaviors(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    prompt = build_persona_prompt()
    assert "reminds you to save your work" in prompt


def test_prompt_contains_romantic_style(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    prompt = build_persona_prompt()
    assert "Light and respectful of boundaries." in prompt


def test_prompt_contains_technical_knowledge(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    prompt = build_persona_prompt()
    assert "Python" in prompt and "pytest" in prompt


def test_prompt_contains_natural_conversation_instruction(monkeypatch):
    """Personality Stabilization sprint addition - anti customer-service-
    bot-cadence guidance must be present regardless of which personality
    preset is active (it's not persona.json-specific)."""
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    prompt = build_persona_prompt()
    assert "customer-service cadence" in prompt


def test_prompt_natural_conversation_instruction_present_for_neutral_too(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", dict(_DEFAULT_PERSONA))
    prompt = build_persona_prompt()
    assert "customer-service cadence" in prompt


def test_empty_default_persona_still_produces_valid_neutral_prompt(monkeypatch):
    """The absolute floor case: brand-new install, persona.json never
    configured at all - must still produce SOME usable prompt, never
    crash, never an empty string."""
    monkeypatch.setattr(persona_module, "PERSONA", dict(_DEFAULT_PERSONA))
    prompt = build_persona_prompt()
    assert prompt
    assert "helpful AI assistant" in prompt


def test_prompt_never_raises_on_missing_or_wrong_typed_fields(monkeypatch):
    """Defensive regression guard: every `PERSONA.get(...)` call site in
    `build_persona_prompt()` must tolerate a missing key OR an explicit
    `None` value (both are realistic outcomes of a hand-edited
    persona.json) without raising."""
    sparse = {"name": "Sparse"}  # missing every other key entirely
    monkeypatch.setattr(persona_module, "PERSONA", sparse)
    prompt = build_persona_prompt()  # must not raise
    assert "Sparse" in prompt

    none_valued = {**_DEFAULT_PERSONA, "traits": None, "emotional_states": None, "humor_examples": None}
    monkeypatch.setattr(persona_module, "PERSONA", none_valued)
    build_persona_prompt()  # must not raise


# ============================================================================
# Functional boundaries - persona must never be able to impersonate the
# SEPARATE tool-verification channel (`build_verified_action_notes()` in
# main_runtime_demo.py), which is the project's actual "never fabricate
# success" enforcement mechanism.
# ============================================================================

def test_persona_prompt_never_contains_the_verified_facts_marker(monkeypatch):
    """"VERIFIED results" is the literal marker `build_verified_action_
    notes()` uses to introduce real, checked tool outcomes - it must
    never appear from the persona block alone, otherwise a weak model
    could conceivably confuse persona flavor text for a verified fact."""
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    prompt = build_persona_prompt()
    assert "VERIFIED results" not in prompt


def test_real_persona_prompt_never_contains_the_verified_facts_marker():
    """Same guard, against the REAL shipped config/persona.json (not a
    synthetic fixture) - this is the one that actually ships."""
    prompt = build_persona_prompt()
    assert "VERIFIED results" not in prompt


def test_real_persona_prompt_accuracy_outranks_personality_language_present():
    """`_PERSONALITY_DESCRIPTIONS["composed"]` (the persona.json shipped
    with this project uses "composed") must explicitly state accuracy/
    honesty outranks personality - locks in the sprint's core priority
    rule at the text level, not just by convention."""
    prompt = build_persona_prompt()
    assert "outrank personality" in prompt or "honest tool results always outrank" in prompt


# ============================================================================
# build_persona_flavor_hint() - short-form variant used by confirmation-
# style prompts elsewhere (e.g. main.py's generate_script_feedback).
# ============================================================================

def test_flavor_hint_empty_for_neutral_personality(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", dict(_DEFAULT_PERSONA))
    assert build_persona_flavor_hint() == ""


def test_flavor_hint_nonempty_for_non_neutral_personality(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    hint = build_persona_flavor_hint()
    assert hint
    assert "Testina" in hint
    assert "composed" in hint


def test_flavor_hint_includes_smart_home_style_when_set(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)
    hint = build_persona_flavor_hint()
    assert "Direct and verified, no pretending." in hint


def test_flavor_hint_includes_stammer_note_when_enabled(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", _FULL_TEST_PERSONA)  # stammer_when_flustered=True
    hint = build_persona_flavor_hint()
    assert "flustered elongation" in hint


def test_flavor_hint_never_raises_on_sparse_persona(monkeypatch):
    monkeypatch.setattr(persona_module, "PERSONA", {"name": "Sparse", "personality": "sassy"})
    build_persona_flavor_hint()  # must not raise
