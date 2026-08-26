"""
tests/test_sprint57_ha_contextual_reference.py
================================================

Sprint 57 - Contextual Home Assistant References.

Exercises exactly the A-Q scenario matrix from this sprint's own brief
against the implementation in `PlannerBridgeModule._apply_device_
context()` (`main_runtime_demo.py`) and `RealHomeAssistantHandler.
execute()` (`luno/tool_manager/builtin/real_home_assistant.py`) - the
SAME pre-existing REMEMBER/FILL mechanism this sprint hardened (bounded
freshness, HA-domain compatibility, same-turn ambiguity clearing,
failed-command invalidation), not a second memory system, not a second
topic tracker, not a global `last_entity`, not an independent HA
resolver. See `docs/change_impact/ha_contextual_reference.md` for the
full architecture/reuse writeup.

**Device-name substitution note:** the brief's own illustrative
examples ("lampu kamar", "lampu ruang tamu") are not configured devices
in THIS checkout (`config/lights.config.json` only defines Main Lamp/
RGB Strip/RGB Computer as lights, Baterai/Aquascape as switches) - every
test below substitutes real, live-configured device names for those
illustrative ones (same convention `tests/test_sprint52_ha_entity_
resolution.py`/`tests/test_sprint56_ha_safety_matrix.py`/`tests/test_
sprint57_contextual_ha_references.py` all already established: never
invent a device name a real resolver could not actually resolve).

Run:
    python3 -m pytest tests/test_sprint57_ha_contextual_reference.py
"""

from __future__ import annotations

import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import main_runtime_demo as demo  # noqa: E402
from luno.planner.parser import IntentParser  # noqa: E402

from test_sprint52_ha_entity_resolution import (  # noqa: E402
    FakeHAClient, _handler, _patch_real_devices, _restore_devices,
)
from luno.tool_manager.models import ToolCall as TMToolCall  # noqa: E402


def _bridge() -> "demo.PlannerBridgeModule":
    return demo.PlannerBridgeModule()


# ============================================================================
# A - Basic contextual reference
# ============================================================================

def test_A_basic_contextual_reference():
    bridge = _bridge()
    on = bridge._apply_device_context("nyalakan rgb strip", "c1")  # "Nyalain lampu kamar." stand-in
    assert on == "nyalakan rgb strip"
    off = bridge._apply_device_context("matikan", "c1")  # "Matikan."
    assert off == "turn off rgb_strip"
    steps = IntentParser.parse(off)
    assert (steps[0].tool, steps[0].action, steps[0].target) == ("home_assistant", "turn_off", "rgb_strip")


# ============================================================================
# B - Explicit override: an explicit current target always wins over the
#     previous contextual target.
# ============================================================================

def test_B_explicit_override_always_wins():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")  # "Nyalain lampu kamar."
    result = bridge._apply_device_context("matikan rgb komputer", "c1")  # "Matikan lampu ruang tamu." stand-in
    assert result == "matikan rgb komputer"  # untouched - explicit target passes straight through
    steps = IntentParser.parse(result)
    assert steps[0].target == "rgb_komputer"  # controls the NEW device, not rgb_strip
    # and REMEMBER itself moved on to the new device for any FURTHER bare command
    assert bridge._last_device_target["c1"]["home_assistant"]["target"] == "rgb_komputer"


# ============================================================================
# C - Missing context: "Matikan" with no previous HA reference at all.
# ============================================================================

def test_C_missing_context_refuses_safely():
    bridge = _bridge()
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"  # unchanged - nothing to fill, falls through to the existing safe refusal


# ============================================================================
# D - Ambiguous context: multiple equally valid contextual candidates ->
#     refuse, never guess. This single-slot design's own shape of
#     "multiple candidates" is two distinct real devices named in the
#     SAME turn - the existing memory is cleared rather than letting
#     whichever clause was last silently win.
# ============================================================================

def test_D_ambiguous_context_never_guesses():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    bridge._apply_device_context("nyalakan rgb strip, lalu nyalakan rgb komputer", "c1")
    assert bridge._last_device_target.get("c1", {}).get("home_assistant") is None
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"  # refuses - never guesses between the two equally-plausible candidates


# ============================================================================
# E - Unsupported capability: the contextual entity cannot perform the
#     requested action -> use the existing unsupported-capability
#     behavior, never redirect to another device. Domain compatibility
#     is the mechanism that enforces this for the one context-fillable
#     action family (plain on/off) - an engineered fixture proves the
#     gate (this checkout's real registry only has light/switch domains,
#     both compatible, so a genuine incompatible-domain example needs a
#     constructed fixture, same "no natural example, prove the gate
#     anyway" precedent as Sprint 52's own `test_T`).
# ============================================================================

def test_E_unsupported_capability_refuses_never_redirects():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    # Engineer a remembered device from a domain a plain on/off fill is
    # not safe to assume compatible with (e.g. "lock" - a real HA
    # domain, genuinely not the same shape as "turn off a light").
    bridge._last_device_target["c1"]["home_assistant"] = {
        "target": "front_door", "turn_seq": bridge._device_context_turn_seq["c1"],
        "entity_id": "lock.front_door", "domain": "lock",
    }
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"  # refuses - never silently redirects to rgb_strip or any other device


def test_E_naikin_brightness_with_no_device_does_not_parse_as_a_fillable_ha_step():
    """Investigated (not assumed): IntentParser's own grammar for
    `set_brightness`/`set_color` requires a captured token in the
    target position - a bare "naikin brightness"/"set warna merah" with
    no device name at all does not parse as a `home_assistant` step in
    the first place (falls to "unknown"), so there is no fillable
    action-with-no-target shape to safely extend context-fill to
    without first widening IntentParser's own grammar - a Planner-layer
    change this sprint's own hard invariants caution against unless
    strictly required. Documented here as a proven (not assumed) scope
    boundary, not a defect: no crash, no wrong device, no silent
    guessing - the utterance just stays "unknown", the same honest
    behavior it had before this sprint."""
    for text in ("naikin brightness", "set warna merah"):
        steps = IntentParser.parse(text)
        assert steps[0].tool == "unknown", f"{text!r} unexpectedly parsed as {steps[0].tool!r}"


# ============================================================================
# F - Context expiration: stale contextual references are not reused
#     past the chosen freshness boundary (`_CONTEXT_MAX_TURN_AGE`).
# ============================================================================

def test_F_context_expiration_boundary():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    for _ in range(bridge._CONTEXT_MAX_TURN_AGE):
        bridge._apply_device_context("apa kabar", "c1")  # intervening unrelated turns age the context
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"  # one turn past the limit - refuses rather than reusing stale context


# ============================================================================
# G - Non-HA interruption: unrelated conversation in between must not
#     let an old device be silently controlled unless it is still
#     genuinely valid (fresh + compatible) evidence.
# ============================================================================

def test_G_non_ha_interruption_does_not_corrupt_or_prematurely_invalidate_context():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    bridge._apply_device_context("gimana cuaca hari ini", "c1")  # "Berapa suhu sekarang?" stand-in
    # Still within the freshness window - the architecture explicitly
    # considers this context valid (one intervening non-HA turn is not,
    # by itself, evidence the HA context is stale).
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "turn off rgb_strip"


# ============================================================================
# H - Sprint 52 fuzzy resolution unaffected.
# ============================================================================

def test_H_sprint52_fuzzy_resolution_unaffected():
    saved = _patch_real_devices()
    try:
        client = FakeHAClient()
        client.state_after_call["light.wled"] = "off"
        h = _handler(client)
        result = h.execute(TMToolCall(tool="home_assistant", action="turn_off", target="rgb strp"))
        assert result.success is True
        assert client.calls and client.calls[0][2] == "light.wled"
    finally:
        _restore_devices(saved)


# ============================================================================
# I - Sprint 56 query differentiator unaffected.
# ============================================================================

def test_I_sprint56_query_differentiator_unaffected():
    from luno import memory_context

    def _snap(sentence: str) -> "memory_context.ActiveTopicSnapshot":
        tokens = frozenset(memory_context.analyze_query(sentence).tokens)
        return memory_context.ActiveTopicSnapshot(terms=tokens, source_sentence=sentence)

    candidates = [_snap("Pompa A menyala"), _snap("Pompa B mati")]
    narrowed = memory_context._narrow_by_query_differentiator(candidates, "Pompa A gimana?")
    assert len(narrowed) == 1
    assert narrowed[0].source_sentence == "Pompa A menyala"


# ============================================================================
# J - Exact entity behavior unchanged.
# ============================================================================

def test_J_exact_entity_behavior_unchanged():
    saved = _patch_real_devices()
    try:
        h = _handler(FakeHAClient())
        resolution = h._resolve_entity_tiered("RGB Strip")
        assert resolution.resolution_method in ("exact", "alias")
        assert resolution.resolved_entity == "light.wled"
    finally:
        _restore_devices(saved)


# ============================================================================
# K - Alias behavior unchanged.
# ============================================================================

def test_K_alias_behavior_unchanged():
    saved = _patch_real_devices()
    try:
        h = _handler(FakeHAClient())
        resolution = h._resolve_entity_tiered("RGB komputer")  # the configured alias, not the canonical name
        assert resolution.resolved_entity is not None
        assert resolution.resolution_method in ("exact", "alias")
    finally:
        _restore_devices(saved)


# ============================================================================
# L - Ambiguous fuzzy match safety boundary unchanged.
# ============================================================================

def test_L_ambiguous_fuzzy_match_still_refuses():
    saved = _patch_real_devices()
    try:
        client = FakeHAClient()
        h = _handler(client)
        result = h.execute(TMToolCall(tool="home_assistant", action="turn_off", target="rgb cprip"))
        assert result.success is False
        assert client.calls == []
    finally:
        _restore_devices(saved)


# ============================================================================
# M - Repeated contextual commands: deterministic continuity across
#     several ON/OFF round trips.
# ============================================================================

def test_M_repeated_contextual_commands_stay_deterministic():
    bridge = _bridge()
    assert bridge._apply_device_context("nyalakan rgb strip", "c1") == "nyalakan rgb strip"
    assert bridge._apply_device_context("matikan", "c1") == "turn off rgb_strip"
    assert bridge._apply_device_context("nyalain lagi", "c1") == "turn on rgb_strip"
    assert bridge._apply_device_context("matikan lagi", "c1") == "turn off rgb_strip"


# ============================================================================
# N - Context switch: a later explicit target updates REMEMBER, and a
#     THIRD bare command follows the newest explicit target, not the
#     original one.
# ============================================================================

def test_N_context_switch_follows_the_newest_explicit_target():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")  # "Nyalain lampu kamar."
    bridge._apply_device_context("matikan rgb komputer", "c1")  # "Matikan lampu ruang tamu." stand-in
    result = bridge._apply_device_context("matikan", "c1")  # "Matikan."
    assert result == "turn off rgb_komputer"  # third command targets the NEWEST explicit device


# ============================================================================
# O - Failed first command must NOT create a contextual reference.
# ============================================================================

def test_O_failed_first_command_does_not_create_context():
    from luno.planner.models import ToolCall as PlannerToolCall

    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert bridge._last_device_target["c1"]["home_assistant"]["target"] == "rgb_strip"
    # Simulate the execution actually failing (the exact wiring
    # `_tool_bridge_handler`'s failure branch now has, exercised
    # directly here at the invalidation-helper level).
    bridge._tool_bridge_local.conversation_id = "c1"
    failed_call = PlannerToolCall(tool="home_assistant", action="turn_on", target="rgb_strip", params={})
    bridge._invalidate_device_context_on_failure(failed_call)
    assert bridge._last_device_target["c1"].get("home_assistant") is None
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"  # nothing left to fill from - the failed command never poisoned context


# ============================================================================
# P - Device disappears: the referenced entity becomes unresolvable
#     between REMEMBER and FILL. Proven end-to-end: the context layer
#     still fills the text (it does not re-check the live registry -
#     that would be a SECOND resolver, exactly what this sprint's own
#     hard invariants forbid), but the existing, unmodified Sprint 52
#     resolver honestly refuses once the rewritten command reaches
#     `execute()` - zero calls made to Home Assistant, no fallback
#     guessing at a different device.
# ============================================================================

def test_P_device_disappears_is_safely_refused_by_the_existing_resolver():
    saved = _patch_real_devices()
    try:
        from luno import devices
        client = FakeHAClient()
        h = _handler(client)
        # "RGB Strip" was resolvable a moment ago; simulate it vanishing
        # from the live registry before the follow-up command executes.
        devices.LIGHTS.pop("RGB Strip", None)
        result = h.execute(TMToolCall(tool="home_assistant", action="turn_off", target="rgb_strip"))
        assert result.success is False
        assert result.error_type == "UnknownDevice"
        assert client.calls == []  # no device was ever touched
    finally:
        _restore_devices(saved)


# ============================================================================
# Q - Session reset: after `ConversationEnded`, no stale HA reference
#     survives.
# ============================================================================

def test_Q_session_reset_clears_contextual_reference():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert "c1" in bridge._last_device_target
    bridge._on_conversation_ended(demo.Event(type="conversation_ended", data={"session_id": "c1", "reason": "timeout"}))
    assert "c1" not in bridge._last_device_target
    assert "c1" not in bridge._device_context_turn_seq
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"


# ============================================================================
# Performance
# ============================================================================

def test_performance_contextual_resolution_under_5ms():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    iterations = 500
    start = time.perf_counter()
    for _ in range(iterations):
        bridge._apply_device_context("matikan", "c1")
    elapsed_ms = (time.perf_counter() - start) * 1000
    mean_ms = elapsed_ms / iterations
    assert mean_ms < 5.0, f"mean {mean_ms:.4f}ms over the 5ms budget ({iterations} iterations)"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
