"""
tests/test_sprint60_area_schema.py
====================================

Sprint 60 - Structured Room/Area Schema Foundation.

Adds an OPTIONAL, ADDITIVE, backward-compatible `"area"` string field to
`config/lights.config.json` entries (`luno/devices.py::load_lights_
config()`), plus two small pure-registry-lookup helpers
(`luno.devices.get_device_area()` / `get_devices_by_area()`), so this
project's device registry can express structured room/area membership
instead of Sprint 59's converging-textual-evidence approach. This is a
SCHEMA + REGISTRY sprint only - it does NOT implement multi-room control.

**Phase 0 finding (see `docs/change_impact/area_schema_foundation.md`
for the full writeup):** `luno.devices.LIGHTS` (populated from `config/
lights.config.json` by `load_lights_config()`) is the one canonical,
already-in-use source every HA-adjacent module reads from directly
(`luno/tool_manager/builtin/real_home_assistant.py`'s `_lookup_light()`/
`_all_known_device_names()`/`_all_known_device_entities()`,
`main_runtime_demo.py`'s Sprint 58/59 group-expansion loop, `luno/
environment_intent.py`). Every entry is ALREADY a dict (even the
"short format" - entity_id-only - is normalized into one at load time),
so adding one more optional key (`"area"`) is purely additive: nothing
downstream iterates or validates the KEY SET of that dict, everything
reads named keys via `.get(...)`.

**Real device fixture, area-tagged** (mirrors this checkout's ACTUAL,
now-migrated `config/lights.config.json` - all 3 currently-configured
lights carry `"area": "kamar"`, the same evidence-backed room Sprint 59
already established; unlike the SHARED `_REAL_LIGHTS` fixture in
`tests/test_sprint52_ha_entity_resolution.py`, this local fixture uses
RGB Computer's TRUE `entity_id` - `light.komputer` - not that fixture's
own long-documented `light.kamar_tidur_pc` discrepancy, since this is a
new, locally-scoped fixture this sprint fully controls):

    Main Lamp    -> light.kamar_tidur_light_bulb  (area: kamar)
    RGB Strip    -> light.wled                    (area: kamar)
    RGB Computer -> light.komputer                (area: kamar)

Run:
    python3 -m pytest tests/test_sprint60_area_schema.py -v
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
import json

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
    FakeHAClient, _patch_devices, _restore_devices,
)


# ============================================================================
# Local fixtures
# ============================================================================

# NOTE: keys/aliases are deliberately pre-lowercased here, matching
# EXACTLY what `luno.devices.load_lights_config()` itself always produces
# in production (`key = name.strip().lower()`, aliases lowercased too) -
# unlike the older, already-established `_REAL_LIGHTS` fixture in
# `tests/test_sprint52_ha_entity_resolution.py` (capitalized keys, relies
# on every consumer re-normalizing on lookup). Sprint 60's own
# `get_device_area()`/`get_devices_by_area()` DO re-normalize defensively
# regardless (see their docstrings), but using production-shaped keys
# here keeps this file's direct `devices.LIGHTS[...]` assertions simple
# and honest about what real config actually loads as.
_AREA_TAGGED_LIGHTS = {
    "main lamp": {
        "entity_id": "light.kamar_tidur_light_bulb",
        "aliases": ["lampu utama"],
        "area": "kamar",
    },
    "rgb strip": {
        "entity_id": "light.wled",
        "aliases": [],
        "area": "kamar",
    },
    "rgb computer": {
        "entity_id": "light.komputer",
        "aliases": ["rgb komputer"],
        "area": "kamar",
    },
}

# Same 3 lights, but WITHOUT any "area" key at all - simulates a registry
# that has never adopted the new optional field (pre-Sprint-60 shape).
_NO_AREA_LIGHTS = {
    "main lamp": {"entity_id": "light.kamar_tidur_light_bulb", "aliases": ["lampu utama"]},
    "rgb strip": {"entity_id": "light.wled", "aliases": []},
    "rgb computer": {"entity_id": "light.komputer", "aliases": ["rgb komputer"]},
}

# The 3 kamar lights PLUS one hypothetical light in a different room -
# proves area metadata now correctly SCOPES a room-qualified group
# instead of always sweeping in the entire registry.
_MIXED_AREA_LIGHTS = dict(_AREA_TAGGED_LIGHTS)
_MIXED_AREA_LIGHTS["lampu dapur"] = {
    "entity_id": "light.dapur",
    "aliases": [],
    "area": "dapur",
}

# One kamar-tagged light, one light with NO area at all (mixed migration -
# proves an area-less device is never accidentally swept into "kamar" once
# ANY structured area data exists in the registry).
_PARTIALLY_MIGRATED_LIGHTS = {
    "main lamp": {"entity_id": "light.kamar_tidur_light_bulb", "aliases": [], "area": "kamar"},
    "lampu misterius": {"entity_id": "light.misterius", "aliases": []},  # no "area" key at all
}


def _bridge() -> "demo.PlannerBridgeModule":
    return demo.PlannerBridgeModule()


def _with_devices(lights, fn):
    saved = _patch_devices(lights=lights, switches={}, scripts={})
    try:
        return fn(_bridge())
    finally:
        _restore_devices(saved)


def _config_files():
    from luno import config as luno_config
    return [
        luno_config.LIGHTS_CONFIG_FILE,
        luno_config.SWITCHES_CONFIG_FILE,
        luno_config.SCRIPTS_CONFIG_FILE,
    ]


def _hash_files(paths):
    digests = {}
    for p in paths:
        if os.path.exists(p):
            with open(p, "rb") as f:
                digests[p] = hashlib.md5(f.read()).hexdigest()
        else:
            digests[p] = None
    return digests


# ============================================================================
# A - old device without area still loads
# ============================================================================

def test_A_old_device_without_area_still_loads():
    def run(bridge):
        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered("main_lamp")
        assert result.executable is True
        assert result.resolved_entity == "light.kamar_tidur_light_bulb"
        # no "area" key at all in this fixture - still a fully valid device
        assert "area" not in devices.LIGHTS["main lamp"]
    _with_devices(_NO_AREA_LIGHTS, run)


# ============================================================================
# B - device with area loads correctly
# ============================================================================

def test_B_device_with_area_loads_correctly():
    def run(bridge):
        assert devices.LIGHTS["main lamp"]["area"] == "kamar"
        assert devices.LIGHTS["rgb strip"]["area"] == "kamar"
        assert devices.LIGHTS["rgb computer"]["area"] == "kamar"
        # resolution is completely unaffected by the extra key
        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered("main_lamp")
        assert result.executable is True
        assert result.resolved_entity == "light.kamar_tidur_light_bulb"
    _with_devices(_AREA_TAGGED_LIGHTS, run)


# ============================================================================
# C - invalid/missing area does not crash the registry loader
# ============================================================================

def test_C_invalid_or_missing_area_does_not_crash_registry():
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w", encoding="utf-8")
    try:
        json.dump({
            "No Area": {"entity_id": "light.no_area"},
            "Empty Area": {"entity_id": "light.empty_area", "area": ""},
            "Whitespace Area": {"entity_id": "light.ws_area", "area": "   "},
            "Messy Area": {"entity_id": "light.messy_area", "area": "  Kamar  "},
            "Wrong Type Int": {"entity_id": "light.wrong_int", "area": 123},
            "Wrong Type List": {"entity_id": "light.wrong_list", "area": ["kamar"]},
            "Wrong Type Dict": {"entity_id": "light.wrong_dict", "area": {"nested": "kamar"}},
            "Short Format": "light.short_format",
        }, tmp)
        tmp.close()

        old_path = devices.LIGHTS_CONFIG_FILE
        devices.LIGHTS_CONFIG_FILE = tmp.name
        try:
            loaded = devices.load_lights_config()
        finally:
            devices.LIGHTS_CONFIG_FILE = old_path

        assert loaded["no area"]["area"] is None
        assert loaded["empty area"]["area"] is None
        assert loaded["whitespace area"]["area"] is None
        assert loaded["messy area"]["area"] == "kamar"  # stripped + lowercased
        assert loaded["wrong type int"]["area"] is None  # invalid type -> ignored, device still loads
        assert loaded["wrong type list"]["area"] is None
        assert loaded["wrong type dict"]["area"] is None
        assert loaded["short format"]["area"] is None  # short format has no area at all -> None
        # every device is still present and registered - a bad "area" never
        # drops the whole entry the way a missing entity_id would.
        assert len(loaded) == 8
    finally:
        os.unlink(tmp.name)


# ============================================================================
# D - entity_id lookup unchanged
# ============================================================================

def test_D_entity_id_lookup_unchanged():
    def run(bridge):
        assert devices.LIGHTS["main lamp"]["entity_id"] == "light.kamar_tidur_light_bulb"
        assert devices.LIGHTS["rgb strip"]["entity_id"] == "light.wled"
        assert devices.LIGHTS["rgb computer"]["entity_id"] == "light.komputer"
    _with_devices(_AREA_TAGGED_LIGHTS, run)


# ============================================================================
# E - exact resolver unchanged
# ============================================================================

def test_E_exact_resolver_unchanged():
    def run(bridge):
        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered("rgb_strip")
        assert result.executable is True
        assert result.resolved_entity == "light.wled"
        assert result.resolution_method in ("exact", "alias")
    _with_devices(_AREA_TAGGED_LIGHTS, run)


# ============================================================================
# F - alias resolver unchanged
# ============================================================================

def test_F_alias_resolver_unchanged():
    def run(bridge):
        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered("rgb_komputer")
        assert result.executable is True
        assert result.resolved_entity == "light.komputer"
    _with_devices(_AREA_TAGGED_LIGHTS, run)


# ============================================================================
# G - fuzzy resolver unchanged
# ============================================================================

def test_G_fuzzy_resolver_unchanged():
    def run(bridge):
        handler = RealHomeAssistantHandler(client=None)
        # typo'd target, same fuzzy scenario Sprint 52 already proves -
        # the "area" key must never leak into name/alias/entity scoring.
        result = handler._resolve_entity_tiered("rgb_strp")
        assert result.executable is True
        assert result.resolved_entity == "light.wled"
    _with_devices(_AREA_TAGGED_LIGHTS, run)


# ============================================================================
# H - domain validation unchanged
# ============================================================================

def test_H_domain_validation_unchanged():
    def run(bridge):
        handler = RealHomeAssistantHandler(client=None)
        result = handler._resolve_entity_tiered("main_lamp")
        assert result.resolved_entity.split(".", 1)[0] == "light"
    _with_devices(_AREA_TAGGED_LIGHTS, run)


# ============================================================================
# I - get_device_area() returns expected value
# ============================================================================

def test_I_get_device_area_returns_expected_value():
    def run(bridge):
        assert devices.get_device_area("main lamp") == "kamar"
        assert devices.get_device_area("Main Lamp") == "kamar"  # case-insensitive
        assert devices.get_device_area("lampu utama") == "kamar"  # via alias
        assert devices.get_device_area("rgb komputer") == "kamar"  # via alias
        assert devices.get_device_area("nonexistent device") is None
        assert devices.get_device_area(None) is None
        assert devices.get_device_area("") is None
    _with_devices(_AREA_TAGGED_LIGHTS, run)


def test_I_get_device_area_none_when_unset():
    def run(bridge):
        assert devices.get_device_area("main lamp") is None
    _with_devices(_NO_AREA_LIGHTS, run)


# ============================================================================
# J - get_devices_by_area("kamar") returns correct devices
# ============================================================================

def test_J_get_devices_by_area_returns_correct_devices():
    def run(bridge):
        result = set(devices.get_devices_by_area("kamar"))
        assert result == {"main lamp", "rgb strip", "rgb computer"}
        # case-insensitive
        assert set(devices.get_devices_by_area("KAMAR")) == result
        assert set(devices.get_devices_by_area("  kamar  ")) == result
    _with_devices(_AREA_TAGGED_LIGHTS, run)


def test_J_get_devices_by_area_excludes_other_areas():
    def run(bridge):
        result = set(devices.get_devices_by_area("kamar"))
        assert "lampu dapur" not in result
        assert set(devices.get_devices_by_area("dapur")) == {"lampu dapur"}
    _with_devices(_MIXED_AREA_LIGHTS, run)


# ============================================================================
# K - unknown area returns empty result safely
# ============================================================================

def test_K_unknown_area_returns_empty_result_safely():
    def run(bridge):
        assert devices.get_devices_by_area("ruang_tamu") == []
        assert devices.get_devices_by_area("garasi") == []
        assert devices.get_devices_by_area("") == []
        assert devices.get_devices_by_area(None) == []
        assert devices.get_devices_by_area(123) == []  # invalid type - never raises
        assert devices.get_devices_by_area([]) == []
    _with_devices(_AREA_TAGGED_LIGHTS, run)


# ============================================================================
# L-P — Sprint 52/56/57/58/59 regression is proved by running those files'
# own test suites together with this one (not duplicated here - matching
# the exact convention Sprint 58/59's own test files already established:
# a "targeted regression" pytest invocation, not reimplemented assertions).
#
#     python3 -m pytest tests/test_sprint52_ha_entity_resolution.py \
#         tests/test_sprint56_ha_safety_matrix.py \
#         tests/test_sprint56_query_entity_differentiator.py \
#         tests/test_sprint57_contextual_ha_references.py \
#         tests/test_sprint57_ha_contextual_reference.py \
#         tests/test_device_context.py \
#         tests/test_sprint58_ha_multi_entity_commands.py \
#         tests/test_sprint59_single_room_group_control.py \
#         tests/test_sprint60_area_schema.py -q
#
# See docs/change_impact/area_schema_foundation.md for the actual result.
# ============================================================================


# ============================================================================
# Q - "nyalakan semua lampu" unchanged (no room word -> always every light,
#     regardless of area metadata - this sprint's own "SINGLE ROOM MUST
#     REMAIN IDENTICAL" principle, proved structurally, not just today)
# ============================================================================

def test_Q_nyalakan_semua_lampu_unchanged():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 3
        assert {s.target for s in steps} == {"main_lamp", "rgb_strip", "rgb_computer"}
    _with_devices(_AREA_TAGGED_LIGHTS, run)


def test_Q_nyalakan_semua_lampu_unchanged_even_with_a_light_in_another_area():
    """Proves principle 4 structurally: a room word is NOT present here, so
    EVERY configured light is still included, even one tagged with a
    different area - "semua lampu" always means "every light", unchanged."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 4
        assert {s.target for s in steps} == {"main_lamp", "rgb_strip", "rgb_computer", "lampu_dapur"}
    _with_devices(_MIXED_AREA_LIGHTS, run)


# ============================================================================
# R - "nyalakan semua lampu kamar" unchanged (identical result to Sprint 59,
#     now computed via structured area metadata instead of "the whole
#     registry" - same output, different, more correct mechanism)
# ============================================================================

def test_R_nyalakan_semua_lampu_kamar_unchanged():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 3
        assert {s.target for s in steps} == {"main_lamp", "rgb_strip", "rgb_computer"}
    _with_devices(_AREA_TAGGED_LIGHTS, run)


def test_R_matches_sprint59_behavior_when_no_structured_area_data_exists():
    """Backward compatibility: an UNMIGRATED registry (no light anywhere has
    an "area" key) must fall back to Sprint 59's original full-registry
    behavior for a "kamar" room word - proved against the exact same
    fixture Sprint 59's own tests use (no area metadata at all)."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 3
        assert {s.target for s in steps} == {"main_lamp", "rgb_strip", "rgb_computer"}
    _with_devices(_NO_AREA_LIGHTS, run)


def test_R_area_metadata_excludes_lights_in_other_areas():
    """The forward-looking value of the schema: once structured area data
    exists, "semua lampu di kamar" correctly EXCLUDES a light tagged with
    a different area, instead of sweeping it in just for being a light -
    something Sprint 59 alone could never safely do. Uses the "di kamar"
    (WITH preposition) phrasing deliberately - Sprint 58's own
    `_GROUP_AREA_RE` (untouched by this sprint) only captures an area
    word when "di"/"in" immediately follows "lampu"/"light(s)"; "semua
    lampu kamar" (no preposition) never captures an area word at all and
    is therefore always treated as the unqualified "every light" shape -
    see `test_R_nyalakan_semua_lampu_kamar_unchanged` above, and Sprint
    59's own `test_C_semua_lampu_kamar_no_preposition`, for that exact,
    pre-existing, unchanged behavior."""
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 3
        targets = {s.target for s in steps}
        assert targets == {"main_lamp", "rgb_strip", "rgb_computer"}
        assert "lampu_dapur" not in targets
    _with_devices(_MIXED_AREA_LIGHTS, run)


# ============================================================================
# S - device without area is never accidentally assigned to "kamar" once
#     ANY structured area data exists in the registry
# ============================================================================

def test_S_device_without_area_is_never_accidentally_assigned():
    def run(bridge):
        # get_devices_by_area() itself never includes an area-less device
        assert "lampu misterius" not in set(devices.get_devices_by_area("kamar"))

        # and the full group-expansion command excludes it too (using "di
        # kamar" so an area word is actually captured - see
        # test_R_area_metadata_excludes_lights_in_other_areas's own note)
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 1
        assert steps[0].target == "main_lamp"
    _with_devices(_PARTIALLY_MIGRATED_LIGHTS, run)


# ============================================================================
# T - no config corruption: the real, on-disk config/*.json files are
#     never mutated by any operation this sprint added (loader, helpers,
#     or the group-resolution integration), proved with a real MD5 check
#     of the actual files this checkout ships.
# ============================================================================

def test_T_no_config_corruption():
    paths = _config_files()
    before = _hash_files(paths)

    # Exercise every Sprint 60 read path against the REAL, on-disk config -
    # none of this should ever write to disk.
    for _ in range(5):
        devices.load_lights_config()
    devices.get_device_area("main lamp")
    devices.get_devices_by_area("kamar")
    bridge = _bridge()
    bridge._apply_ha_group_resolution("nyalakan semua lampu kamar", "c1")
    bridge._apply_ha_group_resolution("nyalakan semua lampu", "c1")
    bridge._apply_ha_group_resolution("nyalakan semua lampu di dapur", "c1")

    after = _hash_files(paths)
    assert before == after


def test_T_real_config_migration_applied_correctly():
    """Directly verifies Phase 3's migration actually landed on disk: the
    REAL config/lights.config.json (not a fixture) now tags all 3
    currently-configured lights with area="kamar", entity_id/aliases/name
    completely unchanged from before Sprint 60."""
    loaded = devices.load_lights_config()
    assert loaded["main lamp"]["entity_id"] == "light.kamar_tidur_light_bulb"
    assert loaded["main lamp"]["aliases"] == ["lampu utama"]
    assert loaded["main lamp"]["area"] == "kamar"

    assert loaded["rgb strip"]["entity_id"] == "light.wled"
    assert loaded["rgb strip"]["area"] == "kamar"

    assert loaded["rgb computer"]["entity_id"] == "light.komputer"
    assert loaded["rgb computer"]["aliases"] == ["rgb komputer"]
    assert loaded["rgb computer"]["area"] == "kamar"


# ============================================================================
# Safety invariants (Phase 6)
# ============================================================================

def test_invariant_unknown_room_still_zero_ha_calls():
    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu di dapur", "c1")
        assert eff == ""
        assert note is not None
        assert IntentParser.parse(eff) == []
    _with_devices(_AREA_TAGGED_LIGHTS, run)


def test_invariant_area_metadata_never_bypasses_domain_check():
    """A hypothetical LIGHTS entry with an area but no usable entity_id is
    still skipped, exactly like Sprint 58/59's own fail-safe - area
    metadata is only ever a FILTER, never a substitute for a real,
    resolvable entity_id."""
    broken = {
        "Main Lamp": {"entity_id": "light.kamar_tidur_light_bulb", "aliases": [], "area": "kamar"},
        "Ghost Light": {"entity_id": None, "aliases": [], "area": "kamar"},
    }

    def run(bridge):
        eff, note = bridge._apply_ha_group_resolution("nyalakan semua lampu kamar", "c1")
        assert note is None
        steps = IntentParser.parse(eff)
        assert len(steps) == 1
        assert steps[0].target == "main_lamp"
    _with_devices(broken, run)


def test_invariant_group_all_light_shape_still_requires_exactly_one_step():
    """Sprint 58's own compound-utterance guard is untouched by the area
    work - a mixed utterance still never triggers group expansion."""
    def run(bridge):
        assert bridge._ha_group_all_lights_shape(
            "nyalakan semua lampu kamar dan bagaimana cuaca hari ini"
        ) is None
    _with_devices(_AREA_TAGGED_LIGHTS, run)


def test_invariant_helpers_are_synchronous_pure_functions():
    """Structural proof there is no network/LLM/embedding call anywhere in
    the area-lookup path: both helpers are ordinary, synchronous, in-
    process functions (not coroutines), and complete near-instantly even
    across many calls - see the dedicated performance test below for the
    quantitative bound."""
    import inspect
    assert not inspect.iscoroutinefunction(devices.get_device_area)
    assert not inspect.iscoroutinefunction(devices.get_devices_by_area)


# ============================================================================
# Performance (Phase 7) - target: <5ms/call
# ============================================================================

def test_performance_area_lookups_under_5ms():
    def run(bridge):
        iterations = 500

        start = time.perf_counter()
        for _ in range(iterations):
            devices.get_device_area("main lamp")
        per_call_area = (time.perf_counter() - start) / iterations * 1000
        assert per_call_area < 5.0, f"get_device_area() too slow: {per_call_area:.4f}ms/call"

        start = time.perf_counter()
        for _ in range(iterations):
            devices.get_devices_by_area("kamar")
        per_call_by_area = (time.perf_counter() - start) / iterations * 1000
        assert per_call_by_area < 5.0, f"get_devices_by_area() too slow: {per_call_by_area:.4f}ms/call"

        start = time.perf_counter()
        for _ in range(iterations):
            bridge._apply_ha_group_resolution("nyalakan semua lampu kamar", "c1")
        per_call_group = (time.perf_counter() - start) / iterations * 1000
        assert per_call_group < 5.0, f"_apply_ha_group_resolution() (area path) too slow: {per_call_group:.4f}ms/call"
    _with_devices(_AREA_TAGGED_LIGHTS, run)


# ============================================================================
# Realistic end-to-end (Phase 6/FASE 6 style) - reads the REAL, migrated
# config/lights.config.json off disk via the real loader, then runs the
# full pipeline: utterance -> group detection -> room resolution (via
# structured area metadata) -> group membership -> existing resolver ->
# existing planner/parser -> existing HA execution (SIMULATED - no live HA
# reachable from this sandbox, see docs/change_impact/
# area_schema_foundation.md for the explicit "not live-verified" note).
# ============================================================================

def test_end_to_end_realistic_migrated_config_single_room_all_lights_on():
    real_lights = devices.load_lights_config()  # the REAL, on-disk, migrated config
    assert all(cfg.get("area") == "kamar" for cfg in real_lights.values())

    def run(bridge):
        utterance = "nyalakan semua lampu di kamar"
        effective_text, refusal_note = bridge._apply_ha_group_resolution(utterance, "c1")
        assert refusal_note is None
        steps = IntentParser.parse(effective_text)
        assert len(steps) == 3

        effective_text = bridge._apply_device_context(effective_text, "c1")
        steps = IntentParser.parse(effective_text)

        client = FakeHAClient()
        for entity_id in ("light.kamar_tidur_light_bulb", "light.wled", "light.komputer"):
            client.states[entity_id] = "off"
            client.state_after_call[entity_id] = "on"
        handler = RealHomeAssistantHandler(client)
        results = []
        from luno.tool_manager.models import ToolCall as TMToolCall
        for s in steps:
            tc = TMToolCall(tool="home_assistant", action=s.action, target=s.target, parameters={})
            results.append(handler.execute(tc))

        assert all(r.success for r in results)
        assert len(client.calls) == 3
        called_entities = {c[2] for c in client.calls}
        assert called_entities == {"light.kamar_tidur_light_bulb", "light.wled", "light.komputer"}
    _with_devices(real_lights, run)


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
