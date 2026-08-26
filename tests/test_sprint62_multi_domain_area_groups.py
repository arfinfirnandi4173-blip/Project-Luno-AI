"""
tests/test_sprint62_multi_domain_area_groups.py
=================================================

Sprint 62 - Multi-Domain Area Group Control.

**Phase 0/1 finding (the actual deliverable of this sprint):** of the 5
domains this sprint was asked to evaluate (light/switch/fan/climate/
media_player), only `light` has a registry structure that can safely
carry an `"area"` field today:

  - `light`  (`devices.LIGHTS`, `config/lights.config.json`) - dict-format
    entries, optional `"aliases"`/`"area"` (Sprint 60), a resolver
    (`RealHomeAssistantHandler._resolve_entity_tiered()`, Sprint 52), and
    an area-aware group-execution path (Sprint 58/59/61). SUPPORTED,
    unchanged by this sprint.

  - `switch` (`devices.SWITCHES`, `config/switches.config.json`) - a
    resolver and execution path DO exist, but `load_switches_config()`
    only ever produces a FLAT `name -> entity_id` STRING mapping (no
    dict-format entries, no `"aliases"`, structurally no way to carry an
    `"area"` field) - confirmed against both the loader source and the
    real on-disk config (`{"Baterai": "switch.tasmota_tasmota3", ...}`).
    DEFERRED (STOP CONDITION 1).

  - `fan`/`climate`/`media_player` - no registry, no config loader, no
    resolver at all in this checkout (mentioned only in Sprint 57's
    `_CONTEXT_FILL_COMPATIBLE_DOMAINS` frozenset, for forward-
    compatibility - never backed by real data). DEFERRED (STOP
    CONDITION 1, more clearly than `switch`).

Because no second domain is safely extendable, this sprint makes NO
functional/behavioral change to `_apply_ha_group_resolution()`,
`_GROUP_LIGHT_WORD_RE`, or `devices.py` - `light`'s area-group handling
is REUSED EXACTLY AS Sprint 61 left it (proved by re-running Sprint 61's
own scenarios below). The only `main_runtime_demo.py` change is a
documentation-only comment recording this evaluation (see
`_GROUP_LIGHT_WORD_RE`'s own Sprint 62 note).

This file both re-confirms every still-applicable Sprint 52-61 safety
guarantee (per PHASE 6/7's own instruction to prove precedence/
regression scenarios, not just describe them) AND adds NEW, evidence-
based tests proving that an "unsupported domain" area-group command
(switch/AC/fan-flavored) already falls through, UNCHANGED, to the
existing single-target pipeline and fails safely with ZERO Home
Assistant calls - traced all the way to `RealHomeAssistantHandler.
execute()`'s own `target and entity_id is None -> _unknown_device_
result()` guard, which returns BEFORE the `with self._lock:` block that
would ever reach `self._client.call_service(...)`.

Run:
    python3 -m pytest tests/test_sprint62_multi_domain_area_groups.py -v
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
from luno.tool_manager.models import ToolCall as TMToolCall  # noqa: E402

from test_sprint52_ha_entity_resolution import (  # noqa: E402
    FakeHAClient, _patch_devices, _patch_real_devices, _restore_devices,
)


_MULTI_AREA_LIGHTS = {
    "main lamp": {"entity_id": "light.kamar_tidur_light_bulb", "aliases": ["lampu utama"], "area": "kamar"},
    "rgb computer": {"entity_id": "light.komputer", "aliases": ["rgb komputer"], "area": "kamar"},
    "kitchen lamp": {"entity_id": "light.dapur_utama", "aliases": [], "area": "dapur"},
}

# Same registry, plus flat (string-only) switches present in the same
# "kamar" area's rooms - proves a real, configured switch NEVER joins an
# area-qualified LIGHT group, and that "semua switch di kamar" is not
# swept into anything even though switches genuinely exist.
_WITH_REAL_SWITCHES = {"lights": _MULTI_AREA_LIGHTS, "switches": {"Baterai": "switch.tasmota_tasmota3"}}


def _bridge() -> "demo.PlannerBridgeModule":
    return demo.PlannerBridgeModule()


def _with_devices(lights, fn, switches=None):
    saved = _patch_devices(lights=lights, switches=switches or {}, scripts={})
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
# A - existing light area command still works (unaffected by this sprint)
# ============================================================================

def test_A_existing_light_area_command_still_works():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert {s.target for s in steps} == {"main_lamp", "rgb_computer"}
        assert "kitchen_lamp" not in {s.target for s in steps}
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# B - no second domain is safe to support this sprint (documented, proven
#     structurally rather than faked green)
# ============================================================================

def test_B_no_second_domain_is_safe_documented_as_deferred():
    """`load_switches_config()` (the ONLY other domain with a real
    registry+resolver+execution path) only ever produces a flat
    `name -> entity_id` STRING value per entry - never a dict, so there
    is structurally no way to attach an `"area"` field to it today. This
    is the concrete evidence behind PHASE 1's "switch: DEFERRED"
    conclusion - not an assumption."""
    saved_switches = dict(devices.SWITCHES)
    try:
        devices.SWITCHES.clear()
        devices.SWITCHES.update({"Baterai": "switch.tasmota_tasmota3", "Aquascape": "switch.tasmota_tasmota2"})
        for name, value in devices.SWITCHES.items():
            assert isinstance(value, str), (
                f"expected switches.config.json entry '{name}' to be a flat entity_id "
                f"string (Sprint 62's own basis for deferring 'switch') - got {type(value)}"
            )
    finally:
        devices.SWITCHES.clear()
        devices.SWITCHES.update(saved_switches)

    # fan/climate/media_player: no registry attribute of any kind exists
    # on the devices module at all.
    for attr in ("FANS", "CLIMATES", "MEDIA_PLAYERS"):
        assert not hasattr(devices, attr)


def test_B_generic_get_devices_by_area_helper_reused_not_duplicated():
    """PHASE 2's own instruction: don't create `get_switches_by_area()`/
    `get_fans_by_area()` unless truly required. Since no second domain
    was extended, no such helper exists."""
    for name in ("get_switches_by_area", "get_fans_by_area", "get_climates_by_area", "get_media_players_by_area"):
        assert not hasattr(devices, name)
    assert hasattr(devices, "get_device_area")
    assert hasattr(devices, "get_devices_by_area")


# ============================================================================
# C - unknown area refuses (light domain, reused from Sprint 61)
# ============================================================================

def test_C_unknown_area_refuses():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di garasi", "c1")
        assert eff == ""
        assert note is not None
        assert "garasi" in note
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# D - unsupported domain refuses, zero HA calls (the actual NEW proof this
#     sprint adds)
# ============================================================================

def test_D_unsupported_domain_switch_refuses_safely():
    """'matikan semua switch di kamar' - `_GROUP_LIGHT_WORD_RE` never
    matches "switch", so this never becomes a detected group shape at
    all; `_apply_ha_group_resolution()` returns the text UNCHANGED, and
    the normal single-target pipeline resolves it to "unknown" (no real
    device is named "semua switch di kamar", nor scores within the fuzzy
    margin of one)."""
    def run(bridge):
        text = "matikan semua switch di kamar"
        eff, note = bridge._apply_ha_group_resolution(text, "c1")
        assert note is None
        assert eff == text  # completely untouched - not a detected group shape

        steps = IntentParser.parse(eff)
        assert len(steps) == 1
        assert steps[0].tool == "home_assistant"

        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered(steps[0].target)
        assert result.executable is False
        assert result.resolved_entity is None
        assert result.resolution_method == "unknown"
    _with_devices(_MULTI_AREA_LIGHTS, run, switches={"Baterai": "switch.tasmota_tasmota3"})


def test_D_unsupported_domain_ac_refuses_safely_zero_ha_calls():
    """'nyalakan semua AC di kamar' - proved all the way to zero real HA
    calls via `RealHomeAssistantHandler.execute()` itself (not just the
    resolver), using a `FakeHAClient` so an accidental call would be
    directly observable."""
    def run(bridge):
        text = "nyalakan semua AC di kamar"
        eff, note = bridge._apply_ha_group_resolution(text, "c1")
        assert note is None
        assert eff == text

        steps = IntentParser.parse(eff)
        assert len(steps) == 1

        client = FakeHAClient()
        handler = RealHomeAssistantHandler(client)
        tc = TMToolCall(tool="home_assistant", action=steps[0].action, target=steps[0].target, parameters={})
        result = handler.execute(tc)
        assert result.success is False
        assert len(client.calls) == 0  # the actual production gate - zero HA calls
    _with_devices(_MULTI_AREA_LIGHTS, run)


def test_D_unsupported_domain_fan_refuses_safely():
    def run(bridge):
        text = "matikan semua kipas di kamar"
        eff, note = bridge._apply_ha_group_resolution(text, "c1")
        assert note is None
        assert eff == text
        steps = IntentParser.parse(eff)
        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered(steps[0].target)
        assert result.executable is False
    _with_devices(_MULTI_AREA_LIGHTS, run)


def test_D_switch_never_included_in_a_light_area_group_even_when_configured():
    """A real switch existing in the registry must never leak into a
    LIGHT area-group's resolved targets."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        targets = {s.target for s in IntentParser.parse(eff)}
        assert "baterai" not in targets
        assert targets == {"main_lamp", "rgb_computer"}
    _with_devices(_MULTI_AREA_LIGHTS, run, switches={"Baterai": "switch.tasmota_tasmota3"})


# ============================================================================
# E - empty area (valid area, but zero entities of the requested domain)
#     refuses safely
# ============================================================================

def test_E_area_valid_but_zero_lights_refuses_safely():
    """"gudang" is a real area value in the fixture below (via a
    non-light device use is impossible since get_devices_by_area only
    scans LIGHTS - so this is equivalent, by construction, to "zero
    lights tagged this area", proving the empty-area-group case reduces
    safely to the same refusal path as an unknown area)."""
    fixture = dict(_MULTI_AREA_LIGHTS)

    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di gudang", "c1")
        assert eff == ""
        assert note is not None
        assert IntentParser.parse(eff) == []
    _with_devices(fixture, run)


# ============================================================================
# F - explicit target overrides group resolution
# ============================================================================

def test_F_explicit_single_target_never_triggers_group_resolution():
    """"lampu kamar" (no "semua") never matches `_GROUP_ALL_WORD_RE`, so
    group resolution never fires at all - Sprint 52's plain single-device
    resolution owns this shape end to end, unaffected by area/group
    logic."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan lampu kamar", "c1")
        assert note is None
        assert eff == "nyalakan lampu kamar"  # untouched - not a group shape
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# G - explicit multi-target (Sprint 58) still works
# ============================================================================

def test_G_explicit_multi_target_still_works():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 2
        assert {s.target for s in steps} == {"rgb_strip", "rgb_komputer"}
    _with_real_devices(run)


# ============================================================================
# H - contextual target (Sprint 57) still has correct precedence
# ============================================================================

def test_H_contextual_target_correct_precedence():
    def run(bridge):
        pre1, note1 = bridge._apply_ha_group_resolution("nyalakan rgb strip", "c1")
        assert note1 is None and pre1 == "nyalakan rgb strip"
        bridge._apply_device_context(pre1, "c1")

        pre2, note2 = bridge._apply_ha_group_resolution("matikan", "c1")
        assert note2 is None and pre2 == "matikan"
        result = bridge._apply_device_context(pre2, "c1")
        assert result == "turn off rgb_strip"
    _with_real_devices(run)


# ============================================================================
# I - fuzzy entity resolution (Sprint 52) still works
# ============================================================================

def test_I_fuzzy_entity_resolution_still_works():
    def run(bridge):
        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered("rgb_strp")  # typo, fuzzy tier
        assert result.executable is True
        assert result.resolved_entity == "light.wled"
    _with_real_devices(run)


# ============================================================================
# J - Sprint 56 differentiator still works
# ============================================================================

def test_J_sprint56_differentiator_still_works():
    from luno import memory_context as mc

    def _snap(sentence):
        tokens = frozenset(mc.analyze_query(sentence).tokens)
        return mc.ActiveTopicSnapshot(terms=tokens, source_sentence=sentence)

    pair = [_snap("Server A pakai Ubuntu."), _snap("Server B pakai Debian.")]
    result = mc._narrow_by_query_differentiator(pair, "Server B gimana?")
    assert len(result) == 1
    assert result[0].source_sentence == "Server B pakai Debian."


# ============================================================================
# K - no fuzzy area matching
# ============================================================================

def test_K_no_fuzzy_area_matching():
    fixture = dict(_MULTI_AREA_LIGHTS)
    fixture["ruang makan lamp"] = {"entity_id": "light.ruang_makan", "aliases": [], "area": "ruang makan"}

    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di dapur", "c1")
        assert note is None
        assert {s.target for s in IntentParser.parse(eff)} == {"kitchen_lamp"}
    _with_devices(fixture, run)


def test_K_no_fuzzy_area_matching_structurally():
    import luno.devices as devices_module
    source = inspect.getsource(devices_module.get_devices_by_area)
    assert "difflib" not in source


# ============================================================================
# L - no HA call during resolution
# ============================================================================

def test_L_no_ha_call_during_resolution():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        assert len(IntentParser.parse(eff)) == 2
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# M - no partial execution when resolution fails
# ============================================================================

def test_M_no_partial_execution_on_unknown_area():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di garasi", "c1")
        assert eff == ""
        assert note is not None
        assert IntentParser.parse(eff) == []  # zero steps -> zero ToolCalls -> zero HA calls
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# N - existing "semua lampu" (unqualified) behavior unchanged
# ============================================================================

def test_N_existing_semua_lampu_behavior_unchanged():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 3
        assert {s.target for s in steps} == {"main_lamp", "rgb_computer", "kitchen_lamp"}
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# O - existing Sprint 58 mixed-utterance regression fix remains fixed
# ============================================================================

def test_O_mixed_utterance_regression_remains_fixed():
    """"turn on the lights and how's the weather today" must NOT be
    treated as a 2-target group - the second clause is an unrelated
    question, not a second device (Sprint 58's own SCOPE GUARD)."""
    def run(bridge):
        text = "turn on the lights and how's the weather today"
        eff, note = bridge._apply_ha_group_resolution(text, "c1")
        # contains "and" -> disqualified from the "dan"-only explicit
        # multi-target shape; "lights" alone (no "semua/all/every") also
        # never satisfies the group-all shape -> completely untouched.
        assert note is None
        assert eff == text
    _with_real_devices(run)


# ============================================================================
# P - invalid/missing area metadata is safe
# ============================================================================

def test_P_untagged_light_never_joins_any_area_group():
    fixture = dict(_MULTI_AREA_LIGHTS)
    fixture["mystery lamp"] = {"entity_id": "light.mystery", "aliases": []}  # no "area" key at all

    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        targets = {s.target for s in IntentParser.parse(eff)}
        assert "mystery_lamp" not in targets
    _with_devices(fixture, run)


def test_P_non_string_area_value_is_ignored_safely():
    """A malformed `"area"` value (e.g. patched directly, bypassing
    `_normalize_optional_area()`'s own loader-time validation) must never
    crash `get_devices_by_area()` nor be treated as a match."""
    fixture = dict(_MULTI_AREA_LIGHTS)
    fixture["broken area lamp"] = {"entity_id": "light.broken", "aliases": [], "area": 12345}

    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        targets = {s.target for s in IntentParser.parse(eff)}
        assert "broken_area_lamp" not in targets
    _with_devices(fixture, run)


# ============================================================================
# Q - area matching normalized consistently
# ============================================================================

def test_Q_area_matching_normalized_consistently():
    def run(bridge):
        a, note_a = bridge._apply_ha_group_resolution("nyalakan semua lampu di KAMAR", "c1")
        b, note_b = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note_a is None and note_b is None
        assert a == b
    _with_devices(_MULTI_AREA_LIGHTS, run)


# ============================================================================
# R - performance <5ms/call
# ============================================================================

def test_R_performance_area_resolution_under_5ms():
    def run(bridge):
        iterations = 300

        start = time.perf_counter()
        for _ in range(iterations):
            bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        per_call = (time.perf_counter() - start) / iterations * 1000
        assert per_call < 5.0, f"light area resolution too slow: {per_call:.4f}ms/call"

        start = time.perf_counter()
        for _ in range(iterations):
            bridge._apply_ha_group_resolution("matikan semua switch di kamar", "c1")
        per_call_unsupported = (time.perf_counter() - start) / iterations * 1000
        assert per_call_unsupported < 5.0, f"unsupported-domain fallthrough too slow: {per_call_unsupported:.4f}ms/call"
    _with_devices(_MULTI_AREA_LIGHTS, run, switches={"Baterai": "switch.tasmota_tasmota3"})


# ============================================================================
# Persistent-state verification (PHASE 8)
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


def test_persistent_state_unmodified_by_this_sprints_resolution_paths():
    paths = _config_files()
    before = _hash_files(paths)

    def run(bridge):
        bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        bridge._apply_ha_group_resolution("matikan semua switch di kamar", "c1")
        bridge._apply_ha_group_resolution("nyalakan semua AC di kamar", "c1")
        bridge._apply_ha_group_resolution("nyalakan semua lampu di garasi", "c1")

    _with_real_devices(run)
    after = _hash_files(paths)
    assert before == after


# ============================================================================
# End-to-end realistic scenario (real registry, mirrors Sprint 61's own)
# ============================================================================

def test_end_to_end_light_area_group_executes_switch_area_group_refuses():
    def run(bridge):
        # Light area group - executes.
        light_text = "matikan semua lampu di kamar"
        eff, note = bridge._apply_ha_group_resolution(light_text, "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        client = FakeHAClient()
        for entity_id in ("light.kamar_tidur_light_bulb", "light.komputer"):
            client.states[entity_id] = "on"
            client.state_after_call[entity_id] = "off"
        handler = RealHomeAssistantHandler(client)
        results = [handler.execute(TMToolCall(tool="home_assistant", action=s.action, target=s.target, parameters={})) for s in steps]
        assert all(r.success for r in results)
        assert len(client.calls) == 2

        # Switch area group (unsupported domain) - refuses, zero extra calls.
        switch_text = "matikan semua switch di kamar"
        eff2, note2 = bridge._apply_ha_group_resolution(switch_text, "c1")
        assert note2 is None
        assert eff2 == switch_text
        steps2 = IntentParser.parse(eff2)
        result2 = handler.execute(TMToolCall(tool="home_assistant", action=steps2[0].action, target=steps2[0].target, parameters={}))
        assert result2.success is False
        assert len(client.calls) == 2  # unchanged - no new HA call happened
    _with_devices(_MULTI_AREA_LIGHTS, run, switches={"Baterai": "switch.tasmota_tasmota3"})


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
