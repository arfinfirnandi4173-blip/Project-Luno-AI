"""
tests/test_sprint59_single_room_group_control.py
==================================================

Sprint 59 - Single-Room Home Assistant Group Control.

Extends Sprint 58's group-all ("semua lampu") detection in
`PlannerBridgeModule._apply_ha_group_resolution()` to ALSO recognize
this project's one identifiable room ("kamar") as naming the exact same
set - "semua lampu kamar" / "semua lampu di kamar" now resolve instead
of being refused as "area-scoped groups not supported yet" (Sprint 58's
own deferred scenario). Multi-room is explicitly OUT OF SCOPE - any area
word other than "kamar" still refuses exactly as Sprint 58 did.

**Phase 0 finding (see `docs/change_impact/ha_single_room_group_
control.md` for the full writeup):** zero STRUCTURED area/room/zone
field exists anywhere in this project's config (re-confirmed by grep,
same as Sprint 58's own finding). However, converging TEXTUAL evidence
shows the ENTIRE currently-configured light registry lives in exactly
one identifiable room:
  - Main Lamp's own `entity_id` is literally `light.kamar_tidur_light_
    bulb` ("kamar_tidur" = "bedroom").
  - `config/environment_triggers.json`'s own pre-existing "sleepy"
    trigger already groups ALL THREE configured lights as one unit.
  - No config file anywhere mentions a second room.
"kamar" is therefore recognized as naming the SAME set Sprint 58's own
"semua lampu" already computes - not a new membership mapping, just an
additional way of NAMING the same, already-tested set.

**Real device fixture** (same convention every prior sprint's test file
established - substitutes real configured names for the brief's
illustrative "lampu meja"/"lampu ruang" examples where a specific
non-kamar device name is needed):

    Main Lamp    -> light.kamar_tidur_light_bulb  (alias: "lampu utama")
    RGB Strip    -> light.wled
    RGB Computer -> light.komputer                (alias: "RGB komputer")

Run:
    python3 -m pytest tests/test_sprint59_single_room_group_control.py -v
"""

from __future__ import annotations

import hashlib
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
from luno.tool_manager.builtin.real_home_assistant import RealHomeAssistantHandler  # noqa: E402
from luno.tool_manager.models import ToolCall as TMToolCall  # noqa: E402

from test_sprint52_ha_entity_resolution import (  # noqa: E402
    FakeHAClient, _patch_devices, _patch_real_devices, _restore_devices,
)


def _bridge() -> "demo.PlannerBridgeModule":
    return demo.PlannerBridgeModule()


def _with_real_devices(fn):
    saved = _patch_real_devices()
    try:
        return fn(_bridge())
    finally:
        _restore_devices(saved)


# ============================================================================
# A - Single-room all lights ON
# ============================================================================

def test_A_single_room_all_lights_on():
    def run(bridge):
        for text in ("nyalakan semua lampu kamar", "nyalakan semua lampu di kamar"):
            eff, note = bridge._apply_ha_group_resolution(text, "c1")
            assert note is None
            steps = IntentParser.parse(eff)
            assert len(steps) == 3
            assert all(s.tool == "home_assistant" and s.action == "turn_on" for s in steps)
            assert {s.target for s in steps} == {"main_lamp", "rgb_strip", "rgb_computer"}
    _with_real_devices(run)


# ============================================================================
# B - Single-room all lights OFF
# ============================================================================

def test_B_single_room_all_lights_off():
    def run(bridge):
        for text in ("matikan semua lampu kamar", "matikan semua lampu di kamar"):
            eff, note = bridge._apply_ha_group_resolution(text, "c1")
            assert note is None
            steps = IntentParser.parse(eff)
            assert len(steps) == 3
            assert all(s.tool == "home_assistant" and s.action == "turn_off" for s in steps)
            assert {s.target for s in steps} == {"main_lamp", "rgb_strip", "rgb_computer"}
    _with_real_devices(run)


# ============================================================================
# C - "semua lampu kamar" (no preposition)
# ============================================================================

def test_C_semua_lampu_kamar_no_preposition():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu kamar", "c1")
        assert note is None
        assert eff == "turn on main_lamp, turn on rgb_strip, turn on rgb_computer"
    _with_real_devices(run)


# ============================================================================
# D - "semua lampu di kamar" (with preposition)
# ============================================================================

def test_D_semua_lampu_di_kamar_with_preposition():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        assert eff == "turn on main_lamp, turn on rgb_strip, turn on rgb_computer"
    _with_real_devices(run)


# ============================================================================
# E - room alias: two different phrasings of the same room reference
#     resolve to the exact same set (deterministic, not phrasing-sensitive)
# ============================================================================

def test_E_room_phrasing_alias_produces_identical_result():
    def run(bridge):
        a, note_a = bridge._apply_ha_group_resolution("matikan semua lampu kamar", "c1")
        b, note_b = bridge._apply_ha_group_resolution("matikan semua lampu di kamar", "c1")
        assert note_a is None and note_b is None
        assert a == b  # "kamar" and "di kamar" name the exact same room/set
    _with_real_devices(run)


# ============================================================================
# F - explicit single-device command stays single-device (Sprint 58's own
#     invariant, PLUS the new Sprint 59 precedence rule: "lampu kamar"
#     (no "semua") already resolves via the UNMODIFIED Sprint 52 fuzzy
#     resolver to a single specific device - "Explicit entity" precedence
#     (rule #1) wins over "Explicit room/group" (rule #3) with ZERO new
#     code, because the group layer never even detects "lampu kamar"
#     (no "semua") as a group shape at all.
# ============================================================================

def test_F_single_device_commands_stay_single_device():
    def run(bridge):
        # Sprint 58's own single-target invariant, unaffected.
        eff, note = bridge._apply_ha_group_resolution("matikan rgb strip", "c1")
        assert note is None and eff == "matikan rgb strip"

        # NEW Sprint 59 precedence proof: "lampu kamar" (no "semua") is
        # NOT detected as a group shape at all...
        assert bridge._ha_group_all_lights_shape("nyalakan lampu kamar") is None
        eff2, note2 = bridge._apply_ha_group_resolution("nyalakan lampu kamar", "c1")
        assert note2 is None and eff2 == "nyalakan lampu kamar"  # untouched

        # ...and resolves, via the completely UNMODIFIED Sprint 52
        # resolver, to exactly ONE device (Main Lamp) - not a group.
        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered("lampu_kamar")
        assert result.executable is True
        assert result.resolved_entity == "light.kamar_tidur_light_bulb"
        assert result.candidate_count == 1
    _with_real_devices(run)


def test_F_lampu_meja_style_phrase_never_becomes_a_group():
    """The brief's own explicit warning: "nyalakan lampu meja" must never
    become a group command - it has no "semua"/"all"/"every" word at all."""
    def run(bridge):
        assert bridge._ha_group_all_lights_shape("nyalakan lampu meja") is None
        eff, note = bridge._apply_ha_group_resolution("nyalakan lampu meja", "c1")
        assert note is None and eff == "nyalakan lampu meja"
    _with_real_devices(run)


# ============================================================================
# G - Sprint 58 explicit multi-target still works, completely unaffected
# ============================================================================

def test_G_sprint58_explicit_multi_target_unaffected():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 2
        assert {s.target for s in steps} == {"rgb_strip", "rgb_komputer"}
    _with_real_devices(run)


# ============================================================================
# H - Sprint 56 query-side differentiator still works, completely
#     unaffected (a wholly separate subsystem - memory_context.py - never
#     touched by this sprint)
# ============================================================================

def test_H_sprint56_differentiator_unaffected():
    from luno import memory_context as mc

    def _snap(sentence):
        tokens = frozenset(mc.analyze_query(sentence).tokens)
        return mc.ActiveTopicSnapshot(terms=tokens, source_sentence=sentence)

    # Same worked example `tests/test_sprint56_query_entity_differentiator.
    # py` itself uses - this sprint touches nothing in memory_context.py.
    pair = [_snap("Server A pakai Ubuntu."), _snap("Server B pakai Debian.")]
    result = mc._narrow_by_query_differentiator(pair, "Server B gimana?")
    assert len(result) == 1
    assert result[0].source_sentence == "Server B pakai Debian."


# ============================================================================
# I - Sprint 57 contextual reference still works, completely unaffected -
#     group detection runs FIRST in _handle_utterance() but returns None
#     for a plain contextual reference, so _apply_device_context() gets
#     the exact same untouched text it always did.
# ============================================================================

def test_I_sprint57_contextual_reference_unaffected():
    def run(bridge):
        # group layer must be a complete no-op for both turns
        pre1, note1 = bridge._apply_ha_group_resolution("nyalakan rgb strip", "c1")
        assert note1 is None and pre1 == "nyalakan rgb strip"
        bridge._apply_device_context(pre1, "c1")

        pre2, note2 = bridge._apply_ha_group_resolution("matikan", "c1")
        assert note2 is None and pre2 == "matikan"
        result = bridge._apply_device_context(pre2, "c1")
        assert result == "turn off rgb_strip"
    _with_real_devices(run)


def test_I_bare_contextual_reference_never_becomes_a_group():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("Matikan.", "c1")
        assert note is None and eff == "Matikan."
    _with_real_devices(run)


# ============================================================================
# J - unknown room -> refusal (multi-room explicitly out of scope)
# ============================================================================

def test_J_unknown_room_refuses_honestly():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di dapur", "c1")
        assert eff == ""
        assert note is not None
        assert "dapur" in note
        assert "kamar" in note  # explains which room IS supported
    _with_real_devices(run)


# ============================================================================
# K - empty group -> refusal (registry has zero eligible lights)
# ============================================================================

def test_K_empty_room_group_is_a_safe_no_op():
    # UPDATED (Sprint 61 - Generalized Area-Aware Home Assistant Group
    # Command): with a COMPLETELY EMPTY registry, "kamar" is no longer a
    # "known area" at all - area recognition is now derived entirely from
    # devices.get_devices_by_area() (Sprint 60's structured metadata),
    # which requires at least one light tagged with that area to return
    # anything. So this now correctly refuses as "unsupported area"
    # (honest: "no configured light is tagged with that area") rather
    # than the OLD Sprint 59 message ("no lights configured in this
    # system at all"). Both are SAFE - zero HA calls either way (proved
    # below, unchanged) - only the refusal WORDING changed, matching this
    # sprint's own PHASE 8 safety rule (unknown area always refuses,
    # never falls back). See test_K_empty_area_word_semua_lampu_still_
    # uses_the_original_empty_no_op_message below for the ORIGINAL
    # "empty_no_op" path, which remains fully reachable and unchanged for
    # the non-area-qualified "semua lampu" shape.
    saved = _patch_devices(lights={}, switches={}, scripts={})
    try:
        bridge = _bridge()
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert eff == ""
        assert IntentParser.parse(eff) == []
        assert note is not None  # zero HA calls guaranteed either way - see docs/change_impact/generalized_area_groups.md
    finally:
        _restore_devices(saved)


def test_K_empty_area_word_semua_lampu_still_uses_the_original_empty_no_op_message():
    """The ORIGINAL Sprint 58/59 "empty_no_op" path (no area word at all -
    "semua lampu" - against a completely empty registry) is UNCHANGED by
    Sprint 61: area_word is None, so allowed_names stays None
    unconditionally (principle: "SINGLE ROOM MUST REMAIN IDENTICAL"), and
    the pre-existing "no lights configured in this system at all" message
    still fires exactly as before."""
    saved = _patch_devices(lights={}, switches={}, scripts={})
    try:
        bridge = _bridge()
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu", "c1")
        assert eff == ""
        assert IntentParser.parse(eff) == []
        assert note is not None and "no lights configured" in note
    finally:
        _restore_devices(saved)


# ============================================================================
# L - wrong domain -> refusal (AC is not a light; never guessed into the
#     room-light group; the phrase isn't even detected as a group shape)
# ============================================================================

def test_L_wrong_domain_never_guessed_into_the_room_group():
    def run(bridge):
        assert bridge._ha_group_all_lights_shape("nyalakan semua ac kamar") is None
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua ac kamar", "c1")
        assert note is None and eff == "nyalakan semua ac kamar"  # untouched - not a group shape

        # The existing, unmodified single-entity resolver also refuses
        # honestly rather than guessing "AC" belongs to the light group.
        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered("semua_ac_kamar")
        assert result.executable is False
    _with_real_devices(run)


# ============================================================================
# M - ambiguous/multi-room mention -> refusal (mentioning a second room
#     alongside "kamar" must never resolve to just "kamar", and must
#     never silently execute)
# ============================================================================

def test_M_mentioning_a_second_room_refuses_safely():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar dan dapur", "c1")
        assert eff == ""
        assert note is not None
        assert IntentParser.parse(eff) == []
    _with_real_devices(run)


# ============================================================================
# N - one member unavailable at runtime -> safe, independent per-member
#     failure (same scope boundary Sprint 58 already established: the
#     all-or-nothing guarantee is about RESOLUTION, not runtime outcomes)
# ============================================================================

def test_N_one_unavailable_member_does_not_corrupt_the_others():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("matikan semua lampu di kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 3

        client = FakeHAClient()
        client.state_after_call["light.wled"] = "off"  # RGB Strip verifies successfully
        client.state_after_call["light.kamar_tidur_pc"] = "off"  # RGB Computer verifies successfully
        # light.kamar_tidur_light_bulb (Main Lamp) deliberately has no
        # post-call state -> reports "unavailable"
        handler = RealHomeAssistantHandler(client)
        results = {}
        for s in steps:
            tc = TMToolCall(tool="home_assistant", action=s.action, target=s.target, parameters={})
            results[s.target] = handler.execute(tc)

        assert results["main_lamp"].success is False
        assert results["rgb_strip"].success is True
        assert results["rgb_computer"].success is True
        assert len(client.calls) == 3  # every member was still independently attempted
    _with_real_devices(run)


# ============================================================================
# O - CRITICAL SAFETY INVARIANT: resolution failure -> zero HA calls,
#     proved against the real production gate mechanism (same proof shape
#     as Sprint 58's own test_Q).
# ============================================================================

def test_O_zero_ha_calls_when_room_resolution_fails():
    def run(bridge):
        effective_text, refusal_note = bridge._apply_ha_group_resolution(
            "nyalakan semua lampu di dapur", "c1",
        )
        assert refusal_note is not None
        assert effective_text == ""

        parsed_steps = IntentParser.parse(effective_text)
        assert parsed_steps == []
        real_task_count = sum(1 for s in parsed_steps if s.tool != "unknown")
        assert real_task_count == 0  # self.planner.execute() is never reached this turn

        client = FakeHAClient()
        handler = RealHomeAssistantHandler(client)
        for s in parsed_steps:
            tc = TMToolCall(tool="home_assistant", action=s.action, target=s.target, parameters={})
            handler.execute(tc)
        assert client.calls == []
    _with_real_devices(run)


# ============================================================================
# P - no persistent-state modification
# ============================================================================

def _hash_config_files():
    digest = {}
    config_dir = os.path.join(_ROOT, "config")
    for name in ("lights.config.json", "switches.config.json", "scripts.config.json"):
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            with open(path, "rb") as f:
                digest[name] = hashlib.md5(f.read()).hexdigest()
    return digest


def test_P_persistent_state_untouched():
    before = _hash_config_files()

    def run(bridge):
        bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        bridge._apply_ha_group_resolution("matikan semua lampu kamar", "c1")
        bridge._apply_ha_group_resolution("nyalakan semua lampu di dapur", "c1")
        bridge._apply_ha_group_resolution("nyalakan lampu kamar", "c1")

    _with_real_devices(run)
    after = _hash_config_files()
    assert before == after


def test_P_no_new_persistent_state_attribute_introduced():
    bridge = _bridge()
    before_keys = set(vars(bridge).keys())
    bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
    bridge._apply_ha_group_resolution("matikan semua lampu kamar", "c1")
    after_keys = set(vars(bridge).keys())
    assert before_keys == after_keys  # no new instance attribute (no second memory system)


# ============================================================================
# Q - performance (<5ms typical)
# ============================================================================

def test_Q_performance_under_5ms():
    def run(bridge):
        n = 100
        t0 = time.perf_counter()
        for _ in range(n):
            bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        t1 = time.perf_counter()
        avg_ms = (t1 - t0) / n * 1000
        assert avg_ms < 5.0, f"single-room group resolution too slow: {avg_ms:.3f}ms"

        t0 = time.perf_counter()
        for _ in range(n):
            bridge._apply_ha_group_resolution("nyalakan semua lampu di dapur", "c1")
        t1 = time.perf_counter()
        avg_ms = (t1 - t0) / n * 1000
        assert avg_ms < 5.0, f"unsupported-room refusal path too slow: {avg_ms:.3f}ms"
    _with_real_devices(run)


# ============================================================================
# Realistic end-to-end (Phase 6): utterance -> group detection -> room
# resolution -> group membership -> existing Sprint 52 resolver -> existing
# parser -> existing (simulated) HA execution -> result. SIMULATED HA only
# (FakeHAClient) - this sandbox has no network egress to a real Home
# Assistant instance; no live-HA claim is made anywhere in this file.
# ============================================================================

def test_end_to_end_realistic_single_room_all_lights_on():
    def run(bridge):
        utterance = "nyalakan semua lampu di kamar"

        # 1. group detection + room resolution + group membership (all one
        #    call - Sprint 59's own new layer)
        effective_text, refusal_note = bridge._apply_ha_group_resolution(utterance, "c1")
        assert refusal_note is None
        assert effective_text == "turn on main_lamp, turn on rgb_strip, turn on rgb_computer"

        # 2. existing, unmodified Sprint 57 contextual layer (no-op here -
        #    every clause already has an explicit target)
        effective_text = bridge._apply_device_context(effective_text, "c1")

        # 3. existing, unmodified IntentParser
        steps = IntentParser.parse(effective_text)
        assert len(steps) == 3

        # 4. existing, unmodified Sprint 52 resolver + Tool Manager
        #    execute() path, against a SIMULATED HA client
        client = FakeHAClient()
        for entity_id in ("light.kamar_tidur_light_bulb", "light.wled", "light.kamar_tidur_pc"):
            client.states[entity_id] = "off"
            client.state_after_call[entity_id] = "on"
        handler = RealHomeAssistantHandler(client)
        results = []
        for s in steps:
            tc = TMToolCall(tool="home_assistant", action=s.action, target=s.target, parameters={})
            results.append(handler.execute(tc))

        # 5. result - every member succeeded, exactly 3 real HA calls made
        assert all(r.success for r in results)
        assert len(client.calls) == 3
        called_entities = {c[2] for c in client.calls}
        assert called_entities == {"light.kamar_tidur_light_bulb", "light.wled", "light.kamar_tidur_pc"}
    _with_real_devices(run)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
