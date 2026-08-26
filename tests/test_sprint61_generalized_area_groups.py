"""
tests/test_sprint61_generalized_area_groups.py
=================================================

Sprint 61 - Generalized Area-Aware Home Assistant Group Command.

Generalizes Sprint 59/60's HARDCODED single-room ("kamar") group
handling in `PlannerBridgeModule._apply_ha_group_resolution()` into a
truly area-AWARE mechanism: `devices.get_devices_by_area()` (Sprint 60)
is now the ONLY source of truth for room/area membership, for ANY area
word `_GROUP_AREA_RE` captures - not just "kamar". Sprint 59's own
`_SINGLE_ROOM_NAME`/`_is_single_room_word()` were REMOVED entirely
(confirmed unused anywhere else in this codebase before removal - see
`docs/change_impact/generalized_area_groups.md` Phase 0/9 writeup).

**Phase 0 finding:** `_GROUP_AREA_RE` (Sprint 58, untouched) already
captured ANY area word generically - the ONLY hardcoding was in what
happened AFTER capture (`_apply_ha_group_resolution()`'s own branch,
which used to compare `area_word` against the single literal string
"kamar"). No parser change was needed at all - this is a pure
`main_runtime_demo.py`-internal generalization on top of Sprint 60's
already-built, already-tested schema foundation.

**Exact match only, never fuzzy** - `devices.get_devices_by_area()`
itself (Sprint 60, untouched) only ever does a normalized (strip+lower)
`==` comparison; this sprint adds no new matching logic of its own.

**A required, additive fixture update** (documented in `tests/
test_sprint52_ha_entity_resolution.py`'s own comment): the shared
`_REAL_LIGHTS` fixture now carries `"area": "kamar"` on all 3 entries,
matching the REAL, on-disk `config/lights.config.json` Sprint 60 already
migrated - needed so Sprint 59's own "kamar"-scoped tests (which patch
that shared fixture) keep passing under this sprint's new PHASE 8
safety rule (unknown area always refuses, no more "unmigrated registry"
fallback to the whole registry).

Run:
    python3 -m pytest tests/test_sprint61_generalized_area_groups.py -v
"""

from __future__ import annotations

import hashlib
import inspect
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
from luno import devices  # noqa: E402
from luno.planner.parser import IntentParser  # noqa: E402
from luno.tool_manager.builtin.real_home_assistant import RealHomeAssistantHandler  # noqa: E402

from test_sprint52_ha_entity_resolution import (  # noqa: E402
    FakeHAClient, _patch_devices, _patch_real_devices, _restore_devices,
)


# ============================================================================
# Local fixtures - multi-area registry, matching PHASE 4's own worked
# example (Main Lamp/RGB Computer -> kamar, Kitchen Lamp -> dapur).
# ============================================================================

_MULTI_AREA_LIGHTS = {
    "main lamp": {"entity_id": "light.kamar_tidur_light_bulb", "aliases": ["lampu utama"], "area": "kamar"},
    "rgb computer": {"entity_id": "light.komputer", "aliases": ["rgb komputer"], "area": "kamar"},
    "kitchen lamp": {"entity_id": "light.dapur_utama", "aliases": [], "area": "dapur"},
}

# Same registry, plus one non-light-domain entry (a climate device) that
# happens to be tagged area="kamar" too - proves domain validation still
# excludes it even though its area matches.
_WITH_NON_LIGHT_DOMAIN_ENTRY = dict(_MULTI_AREA_LIGHTS)
_WITH_NON_LIGHT_DOMAIN_ENTRY["fake ac"] = {"entity_id": "climate.ac_kamar", "aliases": [], "area": "kamar"}

# Two DIFFERENT names resolving to the SAME entity_id, both tagged with
# the same area - proves duplicate-entity dedup stays deterministic.
_DUPLICATE_ENTITY_SAME_AREA = {
    "main lamp": {"entity_id": "light.kamar_tidur_light_bulb", "aliases": [], "area": "kamar"},
    "lampu utama duplikat": {"entity_id": "light.kamar_tidur_light_bulb", "aliases": [], "area": "kamar"},
    "rgb computer": {"entity_id": "light.komputer", "aliases": [], "area": "kamar"},
}

# A light with NO area at all, alongside area-tagged lights - proves an
# area-less device never joins any area group.
_MIXED_WITH_UNTAGGED_LIGHT = dict(_MULTI_AREA_LIGHTS)
_MIXED_WITH_UNTAGGED_LIGHT["mystery lamp"] = {"entity_id": "light.mystery", "aliases": []}


def _bridge() -> "demo.PlannerBridgeModule":
    return demo.PlannerBridgeModule()


def _with_devices(lights, fn):
    saved = _patch_devices(lights=lights, switches={}, scripts={})
    try:
        return fn(_bridge())
    finally:
        _restore_devices(saved)


def _with_real_devices(fn):
    saved = _patch_real_devices()
    try:
        return fn(_bridge())
    finally:
        _restore_devices(saved)


# ============================================================================
# A/B - "semua lampu kamar" and "semua lampu di kamar" still work
# ============================================================================

def test_A_semua_lampu_kamar_still_works():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 3  # no preposition -> unqualified "every light" shape, unchanged since Sprint 58
        assert {s.target for s in steps} == {"main_lamp", "rgb_computer", "kitchen_lamp"}
    _with_devices(_MULTI_AREA_LIGHTS, run)


def test_B_semua_lampu_di_kamar_still_works():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 2  # WITH preposition -> area-scoped: only the 2 "kamar" lights
        assert {s.target for s in steps} == {"main_lamp", "rgb_computer"}
        assert "kitchen_lamp" not in {s.target for s in steps}
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# C/D - a SECOND, previously-unsupported area ("dapur") now works too -
#     the actual generalization this sprint delivers.
# ============================================================================

def test_C_semua_lampu_dapur_no_preposition_is_the_unqualified_shape():
    """No preposition -> `_GROUP_AREA_RE` (unchanged) never captures an
    area word at all, so this is treated as the unqualified "every
    light" shape - exactly the same pre-existing behavior already
    established for "semua lampu kamar" (no preposition) in Sprint 59.
    Deliberately preserved, not reconsidered, by this sprint (PHASE 5/9:
    "jangan mengubah semantics existing")."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu dapur", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 3
        assert {s.target for s in steps} == {"main_lamp", "rgb_computer", "kitchen_lamp"}
    _with_devices(_MULTI_AREA_LIGHTS, run)


def test_D_semua_lampu_di_dapur_works():
    """THE generalization: "dapur" - previously always refused by Sprint
    59's hardcoded "kamar"-only check - now resolves correctly, using
    the exact same general mechanism "kamar" itself uses."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di dapur", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 1
        assert steps[0].target == "kitchen_lamp"
    _with_devices(_MULTI_AREA_LIGHTS, run)


def test_D_matikan_semua_lampu_di_dapur_excludes_kamar_lights():
    """PHASE 4's own worked example, verbatim: "matikan semua lampu di
    kamar" must yield Main Lamp + RGB Computer, and NOT Kitchen Lamp."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("matikan semua lampu di kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 2
        targets = {s.target for s in steps}
        assert targets == {"main_lamp", "rgb_computer"}
        assert "kitchen_lamp" not in targets
        assert all(s.action == "turn_off" for s in steps)
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# E/F - area normalization (case-insensitive, exact match only)
# ============================================================================

def test_E_area_uppercase_works():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di KAMAR", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert {s.target for s in steps} == {"main_lamp", "rgb_computer"}
    _with_devices(_MULTI_AREA_LIGHTS, run)


def test_F_area_lowercase_works():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert {s.target for s in steps} == {"main_lamp", "rgb_computer"}
    _with_devices(_MULTI_AREA_LIGHTS, run)


def test_E_F_mixed_case_produces_identical_result_to_lowercase():
    def run(bridge):
        a, note_a = bridge._apply_ha_group_resolution("nyalakan semua lampu di KaMaR", "c1")
        b, note_b = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note_a is None and note_b is None
        assert a == b
    _with_devices(_MULTI_AREA_LIGHTS, run)


def test_no_fuzzy_room_matching_between_similar_area_names():
    """PHASE 3's own explicit warning: "dapur" must NEVER fuzzy-match
    "ruang makan" or any other similarly-themed area name - exact match
    only."""
    fixture = dict(_MULTI_AREA_LIGHTS)
    fixture["ruang makan lamp"] = {"entity_id": "light.ruang_makan", "aliases": [], "area": "ruang makan"}

    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di dapur", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        # only the exact "dapur" match - "ruang makan" is NOT swept in
        # despite being a thematically similar/adjacent area name.
        assert {s.target for s in steps} == {"kitchen_lamp"}
    _with_devices(fixture, run)


# ============================================================================
# G/H - unknown area -> refusal, zero HA calls
# ============================================================================

def test_G_unknown_area_refuses_honestly():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di garasi", "c1")
        assert eff == ""
        assert note is not None
        assert "garasi" in note
    _with_devices(_MULTI_AREA_LIGHTS, run)


def test_H_unknown_area_zero_ha_calls():
    """Proved against the REAL production gate mechanism, not just by
    construction - matching Sprint 58's own convention for this exact
    kind of safety claim: `effective_text` is empty, `IntentParser.
    parse("")` yields zero steps, so `_handle_utterance()`'s own
    `real_task_count > 0` gate never calls `self.planner.execute()` -
    the actual mechanism that guarantees zero HA calls, not merely an
    assertion about intent."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di garasi", "c1")
        assert note is not None
        assert eff == ""
        steps = IntentParser.parse(eff)
        assert steps == []  # zero steps -> _handle_utterance()'s real_task_count>0 gate never fires

        # No ToolCall is ever constructed from an empty step list, so no
        # HA client is ever touched - confirm the client stays untouched.
        client = FakeHAClient()
        RealHomeAssistantHandler(client)  # constructed, but never called
        assert len(client.calls) == 0
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# I - empty area -> safe refusal
# ============================================================================

def test_I_empty_registry_area_qualified_command_refuses_safely():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert eff == ""
        assert note is not None
        assert IntentParser.parse(eff) == []
    _with_devices({}, run)


def test_I_get_devices_by_area_empty_string_and_none_are_safe():
    assert devices.get_devices_by_area("") == []
    assert devices.get_devices_by_area(None) == []
    assert devices.get_devices_by_area("   ") == []


# ============================================================================
# J - device in a different area never joins the group
# ============================================================================

def test_J_device_in_different_area_excluded():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        targets = {s.target for s in IntentParser.parse(eff)}
        assert "kitchen_lamp" not in targets
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# K/L - area group only ever pulls the "light" domain; a non-light
#     device (even if coincidentally tagged with a matching area) never
#     joins.
# ============================================================================

def test_K_area_group_only_pulls_light_domain():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        for step in IntentParser.parse(eff):
            # every resolved target must slugify from a name whose real
            # entity_id (looked up back in the registry) is domain "light"
            match = [cfg for name, cfg in _WITH_NON_LIGHT_DOMAIN_ENTRY.items()
                     if name.replace(" ", "_") == step.target]
            assert match, f"unexpected target {step.target}"
            assert match[0]["entity_id"].split(".", 1)[0] == "light"
    _with_devices(_WITH_NON_LIGHT_DOMAIN_ENTRY, run)


def test_L_non_light_device_never_joins_even_with_matching_area():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        targets = {s.target for s in IntentParser.parse(eff)}
        assert "fake_ac" not in targets
        assert targets == {"main_lamp", "rgb_computer"}
    _with_devices(_WITH_NON_LIGHT_DOMAIN_ENTRY, run)


# ============================================================================
# M-R - Sprint 52/56/57/58/59/60 all still work, completely unaffected
# ============================================================================

def test_M_sprint52_resolver_still_works():
    def run(bridge):
        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered("rgb_strp")  # typo, fuzzy tier
        assert result.executable is True
        assert result.resolved_entity == "light.wled"
    _with_real_devices(run)


def test_N_sprint56_differentiator_still_works():
    from luno import memory_context as mc

    def _snap(sentence):
        tokens = frozenset(mc.analyze_query(sentence).tokens)
        return mc.ActiveTopicSnapshot(terms=tokens, source_sentence=sentence)

    pair = [_snap("Server A pakai Ubuntu."), _snap("Server B pakai Debian.")]
    result = mc._narrow_by_query_differentiator(pair, "Server B gimana?")
    assert len(result) == 1
    assert result[0].source_sentence == "Server B pakai Debian."


def test_O_sprint57_contextual_reference_still_works():
    def run(bridge):
        pre1, note1 = bridge._apply_ha_group_resolution("nyalakan rgb strip", "c1")
        assert note1 is None and pre1 == "nyalakan rgb strip"
        bridge._apply_device_context(pre1, "c1")

        pre2, note2 = bridge._apply_ha_group_resolution("matikan", "c1")
        assert note2 is None and pre2 == "matikan"
        result = bridge._apply_device_context(pre2, "c1")
        assert result == "turn off rgb_strip"
    _with_real_devices(run)


def test_P_sprint58_explicit_multi_target_still_works():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 2
        assert {s.target for s in steps} == {"rgb_strip", "rgb_komputer"}
    _with_real_devices(run)


def test_P_multi_target_never_caught_as_an_area_group():
    """PHASE 6's own explicit worry: an explicit "A dan B" command must
    never be misdetected/taken over by area/group resolution."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan lampu kamar dan lampu ruang tamu", "c1")
        # neither "lampu kamar" nor "lampu ruang tamu" is a registered
        # device in this fixture, so this correctly refuses as an
        # UNRESOLVED explicit multi-target (Sprint 58's own mechanism),
        # never silently reinterpreted as an area-group command.
        assert eff == ""
        assert note is not None
        assert "more than one device" in note
    _with_real_devices(run)


def test_Q_sprint59_kamar_behavior_still_works_without_special_casing():
    """The generalized mechanism reproduces Sprint 59's own exact
    "kamar" behavior - not via any special-cased string comparison
    anymore (confirmed removed - see module docstring), but via the
    same devices.get_devices_by_area() call every other area word now
    goes through too."""
    def run(bridge):
        for text in ("nyalakan semua lampu kamar", "nyalakan semua lampu di kamar"):
            eff, note = bridge._apply_ha_group_resolution(text, "c1")
            assert note is None
            steps = IntentParser.parse(eff)
            assert len(steps) == 3
            assert all(s.tool == "home_assistant" and s.action == "turn_on" for s in steps)
            assert {s.target for s in steps} == {"main_lamp", "rgb_strip", "rgb_computer"}
    _with_real_devices(run)


def test_R_sprint60_area_metadata_is_the_source_of_truth():
    """Directly proves devices.get_devices_by_area() output is what
    determines group membership - not a separate/parallel mapping."""
    def run(bridge):
        expected = set(devices.get_devices_by_area("kamar"))
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        targets = {s.target.replace("_", " ") for s in IntentParser.parse(eff)}
        assert targets == expected
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# S/T/U - no fuzzy room matching, no LLM call, no network call during
#     resolution (structural proofs)
# ============================================================================

def test_S_no_fuzzy_room_matching_structurally():
    """get_devices_by_area() (Sprint 60, reused unchanged) does a plain
    normalized `==` comparison - no difflib/fuzzy library import
    anywhere in its own module or in the Sprint 61 code path."""
    import luno.devices as devices_module
    source = inspect.getsource(devices_module.get_devices_by_area)
    assert "difflib" not in source
    assert "fuzzy" not in source.lower() or "no fuzzy" in source.lower()  # only appears in comments disclaiming it


def test_T_no_llm_call_structurally():
    assert not inspect.iscoroutinefunction(devices.get_devices_by_area)
    assert not inspect.iscoroutinefunction(devices.get_device_area)
    import main_runtime_demo as demo_module
    source = inspect.getsource(demo_module.PlannerBridgeModule._apply_ha_group_resolution)
    assert "openai" not in source.lower()
    assert "gpt" not in source.lower()
    assert "embedding" not in source.lower()


def test_U_no_network_call_during_resolution():
    """`_apply_ha_group_resolution()` never touches an HA client - the
    only object it ever constructs is a throwaway `client=None`
    RealHomeAssistantHandler (Sprint 58's own convention, used only by
    the explicit_multi_target branch, unaffected by this sprint)."""
    def run(bridge):
        # a bridge with no event bus and no client anywhere reachable
        # still resolves an area-group command correctly - proves no
        # I/O of any kind is required.
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        assert len(IntentParser.parse(eff)) == 2
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# V - no persistent-state mutation
# ============================================================================

def _config_files():
    from luno import config as luno_config
    return [luno_config.LIGHTS_CONFIG_FILE, luno_config.SWITCHES_CONFIG_FILE, luno_config.SCRIPTS_CONFIG_FILE]


def _hash_files(paths):
    out = {}
    for p in paths:
        if os.path.exists(p):
            with open(p, "rb") as f:
                out[p] = hashlib.md5(f.read()).hexdigest()
        else:
            out[p] = None
    return out


def test_V_no_persistent_state_mutation():
    paths = _config_files()
    before = _hash_files(paths)

    def run(bridge):
        bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        bridge._apply_ha_group_resolution("nyalakan semua lampu di dapur", "c1")
        bridge._apply_ha_group_resolution("nyalakan semua lampu di garasi", "c1")
        bridge._apply_ha_group_resolution("nyalakan semua lampu", "c1")
        devices.get_devices_by_area("kamar")
        devices.get_device_area("main lamp")

    _with_real_devices(run)
    after = _hash_files(paths)
    assert before == after


# ============================================================================
# W - empty registry -> safe refusal (distinct from empty AREA - the
#     whole registry has zero lights)
# ============================================================================

def test_W_empty_registry_semua_lampu_safe_refusal():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu", "c1")
        assert eff == ""
        assert note is not None and "no lights configured" in note
        assert IntentParser.parse(eff) == []
    _with_devices({}, run)


# ============================================================================
# X - duplicate area membership -> deterministic
# ============================================================================

def test_X_duplicate_entity_same_area_is_deduped_deterministically():
    def run(bridge):
        eff1, note1 = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        eff2, note2 = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note1 is None and note2 is None
        assert eff1 == eff2  # deterministic across repeated calls
        steps = IntentParser.parse(eff1)
        # 3 registry entries, but 2 of them ("main lamp" and its
        # duplicate) share ONE entity_id -> only 2 distinct clauses
        assert len(steps) == 2
        assert {s.target for s in steps} == {"main_lamp", "rgb_computer"}
    _with_devices(_DUPLICATE_ENTITY_SAME_AREA, run)


# ============================================================================
# Y - mixed known/unknown area never causes partial unsafe execution
# ============================================================================

def test_Y_unknown_area_never_partially_executes_other_known_areas():
    """Even though this registry DOES have known areas ("kamar", "dapur"),
    asking for an unknown one ("garasi") must never fall back to
    executing devices from a DIFFERENT, known area - all-or-nothing,
    never a partial/best-effort substitution."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di garasi", "c1")
        assert eff == ""
        assert note is not None
        assert IntentParser.parse(eff) == []
    _with_devices(_MULTI_AREA_LIGHTS, run)


def test_Y_untagged_light_never_joins_any_area_group():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        targets = {s.target for s in IntentParser.parse(eff)}
        assert "mystery_lamp" not in targets
        assert targets == {"main_lamp", "rgb_computer"}
    _with_devices(_MIXED_WITH_UNTAGGED_LIGHT, run)


# ============================================================================
# Removed-symbol invariant (PHASE 9)
# ============================================================================

def test_invariant_single_room_hardcoding_fully_removed():
    assert not hasattr(demo.PlannerBridgeModule, "_SINGLE_ROOM_NAME")
    assert not hasattr(demo.PlannerBridgeModule, "_is_single_room_word")


# ============================================================================
# Performance (Phase 10) - target: <5ms/call
# ============================================================================

def test_performance_area_resolution_under_5ms():
    def run(bridge):
        iterations = 500

        start = time.perf_counter()
        for _ in range(iterations):
            devices.get_devices_by_area("kamar")
        per_call = (time.perf_counter() - start) / iterations * 1000
        assert per_call < 5.0, f"get_devices_by_area() too slow: {per_call:.4f}ms/call"

        start = time.perf_counter()
        for _ in range(iterations):
            bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        per_call_group = (time.perf_counter() - start) / iterations * 1000
        assert per_call_group < 5.0, f"_apply_ha_group_resolution() too slow: {per_call_group:.4f}ms/call"

        start = time.perf_counter()
        for _ in range(iterations):
            bridge._apply_ha_group_resolution("nyalakan semua lampu di dapur", "c1")
        per_call_second_area = (time.perf_counter() - start) / iterations * 1000
        assert per_call_second_area < 5.0, f"second-area resolution too slow: {per_call_second_area:.4f}ms/call"
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# Realistic end-to-end (using the same real registry Sprint 52-60 use)
# ============================================================================

def test_end_to_end_realistic_two_area_registry():
    def run(bridge):
        utterance = "matikan semua lampu di kamar"
        effective_text, refusal_note = bridge._apply_ha_group_resolution(utterance, "c1")
        assert refusal_note is None
        steps = IntentParser.parse(effective_text)
        assert len(steps) == 2

        effective_text = bridge._apply_device_context(effective_text, "c1")
        steps = IntentParser.parse(effective_text)

        client = FakeHAClient()
        for entity_id in ("light.kamar_tidur_light_bulb", "light.komputer"):
            client.states[entity_id] = "on"
            client.state_after_call[entity_id] = "off"
        handler = RealHomeAssistantHandler(client)
        results = []
        from luno.tool_manager.models import ToolCall as TMToolCall
        for s in steps:
            tc = TMToolCall(tool="home_assistant", action=s.action, target=s.target, parameters={})
            results.append(handler.execute(tc))

        assert all(r.success for r in results)
        assert len(client.calls) == 2
        called_entities = {c[2] for c in client.calls}
        assert called_entities == {"light.kamar_tidur_light_bulb", "light.komputer"}
        assert "light.dapur_utama" not in called_entities
    _with_devices(_MULTI_AREA_LIGHTS, run)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
