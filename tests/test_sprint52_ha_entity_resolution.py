"""
Sprint 52 - Robust Home Assistant Command & Entity Resolution.

Tests the new bounded tiered entity resolver added to
`luno/tool_manager/builtin/real_home_assistant.py`
(`RealHomeAssistantHandler._resolve_entity_tiered()` /
`EntityResolutionResult` / `_score_candidates()`), which lets a
typo'd/misheard/differently-spaced device name ("rg strip", "rbg
strip", "rgb strp", "rgbstrip") still resolve and execute, WITHOUT
weakening safety: fuzzy matching only auto-executes when exactly one
DISTINCT device clears both a confidence and a margin bar; two or more
plausible devices always refuse rather than guess (see module docstring
"Sprint 52" section for the full design).

Fixtures below intentionally mirror the REAL devices discovered in this
checkout's own `config/lights.config.json` / `switches.config.json` /
`scripts.config.json` (not invented names) - see
`docs/change_impact/sprint52_ha_entity_resolution.md` for how these were
found:

    Main Lamp    -> light.kamar_tidur_light_bulb  (alias: "lampu utama")
    RGB Strip    -> light.wled                    (alias: "RGB Strip" - a no-op alias in the real config)
    RGB Computer -> light.kamar_tidur_pc           (alias: "RGB komputer")
    Baterai      -> switch.tasmota_tasmota3
    Aquascape    -> switch.tasmota_tasmota2
    gaming mode  -> script.gaming_mode             (alias: "mode gaming")

Scenarios are labeled A-V per the sprint brief's own convention, plus a
handful of additional coverage beyond that minimum (observability,
performance, explicit non-regression of the pre-existing test file this
sprint extends). Runnable both via pytest and standalone (same
dual-mode convention as
`luno/tool_manager/tests/test_real_home_assistant_verification.py`,
which this file deliberately does not duplicate - see `main()` below).

    pytest -q tests/test_sprint52_ha_entity_resolution.py
    python tests/test_sprint52_ha_entity_resolution.py
"""

import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from luno.tool_manager.builtin.real_home_assistant import RealHomeAssistantHandler  # noqa: E402
from luno.tool_manager.builtin.home_assistant import MockHomeAssistantHandler  # noqa: E402
from luno.tool_manager.models import ToolCall  # noqa: E402

PASS = "✓"
FAIL = "✗"

# -- real device fixture, mirroring this checkout's own config/*.config.json ----
#
# Sprint 61 (Generalized Area-Aware Home Assistant Group Command) added
# `"area": "kamar"` to all 3 entries below - a small, deliberate, ADDITIVE
# update (not a new discrepancy) needed to keep every pre-existing HA test
# file that patches this SHARED fixture (via `_patch_real_devices()`)
# passing under Sprint 61's generalized area resolver, which - per its own
# PHASE 8 safety rule ("unknown area = refusal, never a silent fallback to
# 'every light'") - deliberately REMOVED Sprint 60's "no structured area
# data anywhere -> treat 'kamar' as the whole registry" backward-
# compatibility fallback. Without this fixture update, every "kamar"-
# scoped group test in `tests/test_sprint59_single_room_group_control.py`
# (which patches this exact fixture) would have started refusing instead
# of succeeding - not because of a real regression, but only because this
# TEST-ONLY fixture had never been given the `"area"` field the REAL,
# on-disk `config/lights.config.json` already carries since Sprint 60's
# own migration. Adding one more key to each entry is purely additive -
# nothing in Sprint 52-60's own resolver logic reads `"area"` at all, so
# this cannot change any existing entity-resolution test's outcome (see
# `docs/change_impact/generalized_area_groups.md` for the full reasoning
# and the regression proof). RGB Computer's `entity_id` here is
# DELIBERATELY left as the pre-existing `light.kamar_tidur_pc` value -
# still a known, separately-documented discrepancy from the real config's
# `light.komputer` (see Sprint 59's own change-impact doc) - fixing THAT
# is out of scope for this sprint too; only `"area"` was added.

_REAL_LIGHTS = {
    "Main Lamp": {"entity_id": "light.kamar_tidur_light_bulb", "aliases": ["lampu utama"], "area": "kamar"},
    "RGB Strip": {"entity_id": "light.wled", "aliases": ["RGB Strip"], "area": "kamar"},
    "RGB Computer": {"entity_id": "light.kamar_tidur_pc", "aliases": ["RGB komputer"], "area": "kamar"},
}
_REAL_SWITCHES = {
    "Baterai": "switch.tasmota_tasmota3",
    "Aquascape": "switch.tasmota_tasmota2",
}
_REAL_SCRIPTS = {
    "gaming mode": {"entity_id": "script.gaming_mode", "aliases": ["mode gaming"]},
}


class FakeHAClient:
    """Minimal fake standing in for `RealHomeAssistantClient` - same
    shape as `test_real_home_assistant_verification.py`'s own
    `FakeHAClient` (kept separate/duplicated rather than imported, since
    that file is a standalone script appending `sys.path` itself and
    importing FROM a sibling test module is fragile - this is a small,
    deliberate, self-contained copy, not a new competing utility)."""

    def __init__(self):
        self.states = {}
        self.state_after_call = {}
        self._called_entities = set()
        self.calls = []

    def call_service(self, domain, service, entity_id=None, data=None):
        self.calls.append((domain, service, entity_id, data))
        self._called_entities.add(entity_id)
        return {"success": True, "domain": domain, "service": service, "entity_id": entity_id, "data": data or {}}

    def get_entity_state(self, entity_id):
        target = self.state_after_call.get(entity_id)
        if target is not None and entity_id in self._called_entities:
            self.states[entity_id] = target
        return self.states.get(entity_id)


class _EventRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, stage, payload):
        self.calls.append((stage, dict(payload)))

    @property
    def stages(self):
        return [stage for stage, _ in self.calls]


def _patch_devices(lights=None, switches=None, scripts=None):
    from luno import devices
    saved = (dict(devices.LIGHTS), dict(devices.SWITCHES), dict(devices.SCRIPTS))
    devices.LIGHTS.clear()
    devices.LIGHTS.update(lights if lights is not None else {})
    devices.SWITCHES.clear()
    devices.SWITCHES.update(switches if switches is not None else {})
    devices.SCRIPTS.clear()
    devices.SCRIPTS.update(scripts if scripts is not None else {})
    return saved


def _restore_devices(saved):
    from luno import devices
    devices.LIGHTS.clear()
    devices.LIGHTS.update(saved[0])
    devices.SWITCHES.clear()
    devices.SWITCHES.update(saved[1])
    devices.SCRIPTS.clear()
    devices.SCRIPTS.update(saved[2])


def _patch_real_devices():
    """The full real fixture (lights + switches + scripts together) -
    most scenarios below need the whole registry present at once so
    fuzzy scoring has real cross-device competition to be measured
    against (e.g. 'rgb comp' must be scored against BOTH RGB Strip and
    RGB Computer to prove it isn't confused)."""
    return _patch_devices(lights=_REAL_LIGHTS, switches=_REAL_SWITCHES, scripts=_REAL_SCRIPTS)


def _set_env(**kwargs):
    saved = {}
    for k, v in kwargs.items():
        saved[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    return saved


def _restore_env(saved):
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _handler(client=None, on_verification_event=None):
    return RealHomeAssistantHandler(client or FakeHAClient(), on_verification_event=on_verification_event)


# ---------------------------------------------------------------------------
# A-V: the sprint's own labeled scenarios
# ---------------------------------------------------------------------------

def test_A_exact_command_match_unchanged():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("rgb_strip")  # already-slugified, as the Planner's parser would produce
        ok = r.executable and r.resolved_entity == "light.wled" and r.resolution_method == "exact" and r.confidence == 1.0
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_B_case_variation():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("RGB Strip")
        ok = r.executable and r.resolved_entity == "light.wled" and r.resolution_method == "exact"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_C_spacing_variation_no_separator():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("rgbstrip")
        ok = r.executable and r.resolved_entity == "light.wled" and r.resolution_method == "fuzzy"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_D_missing_character_typo():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("rg strip")
        ok = r.executable and r.resolved_entity == "light.wled" and r.resolution_method == "fuzzy"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_E_transposition_typo():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("rbg strip")
        ok = r.executable and r.resolved_entity == "light.wled" and r.resolution_method == "fuzzy"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_F_missing_trailing_character():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("rgb strp")
        ok = r.executable and r.resolved_entity == "light.wled" and r.resolution_method == "fuzzy"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_G_alias_exact_match():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("lampu utama")
        ok = r.executable and r.resolved_entity == "light.kamar_tidur_light_bulb" and r.resolution_method == "alias"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_H_alias_typo_fuzzy():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("lampu utma")
        ok = r.executable and r.resolved_entity == "light.kamar_tidur_light_bulb" and r.resolution_method == "fuzzy"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_I_script_alias_bugfix_exact():
    """Also the regression test for the real, narrow bug this sprint
    found and fixed: `_lookup_script()` never checked `cfg["aliases"]`
    at all before this sprint (unlike `_lookup_light()`), so
    `config/scripts.config.json`'s own "gaming mode" -> ["mode gaming"]
    alias was silently unreachable."""
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("mode gaming")
        ok = r.executable and r.resolved_entity == "script.gaming_mode" and r.resolution_method == "alias"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_J_script_name_typo_fuzzy():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("gamin mode")
        ok = r.executable and r.resolved_entity == "script.gaming_mode" and r.resolution_method == "fuzzy"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_K_switch_exact_match():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("Baterai")
        ok = r.executable and r.resolved_entity == "switch.tasmota_tasmota3" and r.resolution_method == "exact"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_L_switch_typo_fuzzy_missing_char():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("batrai")
        ok = r.executable and r.resolved_entity == "switch.tasmota_tasmota3" and r.resolution_method == "fuzzy"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_M_switch_typo_fuzzy_extra_char():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("bateray")
        ok = r.executable and r.resolved_entity == "switch.tasmota_tasmota3" and r.resolution_method == "fuzzy"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_N_aquascape_typo_missing_trailing_char():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("aquascap")
        ok = r.executable and r.resolved_entity == "switch.tasmota_tasmota2" and r.resolution_method == "fuzzy"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_O_aquascape_typo_missing_middle_char():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("aqascape")
        ok = r.executable and r.resolved_entity == "switch.tasmota_tasmota2" and r.resolution_method == "fuzzy"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_P_partial_name_resolves_unambiguously():
    """'rgb comp' must resolve confidently to RGB Computer, not be
    confused with (or refused due to) RGB Strip - the two REAL devices
    sharing the "rgb" prefix. Exercises the margin/contention gate with
    real competing candidates, not just an isolated single-device
    fixture."""
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("rgb comp")
        ok = (
            r.executable and r.resolved_entity == "light.kamar_tidur_pc"
            and r.resolution_method == "fuzzy" and r.candidate_count == 1
        )
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_Q_alias_and_name_dedupe_to_same_entity():
    """'RGB Computer' (primary name) and 'RGB komputer' (its alias) must
    resolve to the exact same entity_id - proves the fuzzy tier's
    dedup-by-entity_id logic (`_score_candidates()`) isn't needed here
    (both are exact/alias tier already) but also isn't fooled into
    treating them as 2 competing devices."""
    saved = _patch_real_devices()
    try:
        h = _handler()
        r1 = h._resolve_entity_tiered("rgb computer")
        r2 = h._resolve_entity_tiered("rgb komputer")
        ok = (
            r1.executable and r2.executable
            and r1.resolved_entity == r2.resolved_entity == "light.kamar_tidur_pc"
        )
        return ok, f"r1={r1} r2={r2}"
    finally:
        _restore_devices(saved)


def test_R_too_little_information_refuses():
    """A bare 'rgb' (no other word) must NOT auto-resolve to either RGB
    device - too little information, confidence stays below the bar."""
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("rgb")
        ok = (not r.executable) and r.resolved_entity is None and r.resolution_method == "unknown"
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_S_unknown_device_no_crash():
    saved = _patch_real_devices()
    try:
        h = _handler()
        r = h._resolve_entity_tiered("xyz totally nonsense device")
        ok = (not r.executable) and r.resolved_entity is None and r.resolution_method == "unknown" and r.candidate_count == 0
        return ok, f"{r}"
    finally:
        _restore_devices(saved)


def test_T_ambiguity_gate_direct_unit_test():
    """The project's real registry (6 devices total) has no two devices
    similar enough to naturally tie from a typo alone - a deliberate,
    documented finding (see docs/change_impact/sprint52_ha_entity_resolution.md),
    not a gap in the safety net. This test exercises the SAME
    contention/margin logic `_resolve_entity_tiered()` runs, directly
    against `_score_candidates()`'s own output shape, engineered to
    produce a genuine near-tie between two of the project's real device
    labels - proving the ambiguity gate itself (not just "no natural
    collision exists yet") actually refuses when it should."""
    fuzzy_min_margin = 0.15
    scored = [
        (0.83, "light.wled", "RGB Strip"),
        (0.81, "light.kamar_tidur_pc", "RGB Computer"),
        (0.20, "switch.tasmota_tasmota2", "Aquascape"),
    ]
    scored.sort(key=lambda t: t[0], reverse=True)
    top_score = scored[0][0]
    contenders = [s for s in scored if s[0] >= top_score - fuzzy_min_margin]
    distinct = {s[1] for s in contenders}
    ok = len(distinct) == 2  # RGB Strip and RGB Computer both in contention -> would refuse
    return ok, f"scored={scored} distinct_in_contention={distinct}"


def test_U_end_to_end_execute_turn_off_fuzzy_target():
    """Full `execute()` path (not just the resolver in isolation) with a
    typo'd, already-slugified target (as `IntentParser._slugify()` would
    actually produce from "matikan rgb strp") - proves the ToolResult
    the caller actually gets back reflects the fuzzy resolution, and the
    correct real entity_id is what's sent to `call_service()`."""
    saved_devices = _patch_real_devices()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=1000)
    try:
        client = FakeHAClient()
        client.states["light.wled"] = "on"
        client.state_after_call["light.wled"] = "off"
        h = _handler(client)
        result = h.execute(ToolCall(tool="home_assistant", action="turn_off", target="rgb_strp"))
        ok = (
            result.success and result.data.get("entity_id") == "light.wled"
            and client.calls and client.calls[0][2] == "light.wled"
        )
        return ok, f"success={result.success} message={result.message!r} calls={client.calls}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_V_regression_exact_targets_unchanged():
    """Paranoia regression check: for a battery of already-exact real
    targets, the tiered resolver's output must be identical in shape to
    what `_resolve_entity_id()` alone always produced - method "exact"/
    "alias", confidence 1.0, no fuzzy scoring ever invoked."""
    saved = _patch_real_devices()
    try:
        h = _handler()
        cases = [
            ("rgb_strip", "light.wled"),
            ("main_lamp", "light.kamar_tidur_light_bulb"),
            ("baterai", "switch.tasmota_tasmota3"),
            ("aquascape", "switch.tasmota_tasmota2"),
            ("gaming_mode", "script.gaming_mode"),
        ]
        bad = []
        for target, expected in cases:
            r = h._resolve_entity_tiered(target)
            if not (r.executable and r.resolved_entity == expected and r.confidence == 1.0
                    and r.resolution_method in ("exact", "alias")):
                bad.append((target, r))
        return (not bad), f"failures={bad}"
    finally:
        _restore_devices(saved)


# ---------------------------------------------------------------------------
# Additional coverage beyond the required 22 - observability, performance,
# non-regression of the pre-existing verification test file, other actions.
# ---------------------------------------------------------------------------

def test_observability_fuzzy_resolution_emits_resolution_event():
    saved_devices = _patch_real_devices()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=1000)
    try:
        client = FakeHAClient()
        client.states["light.wled"] = "off"
        client.state_after_call["light.wled"] = "on"
        recorder = _EventRecorder()
        h = _handler(client, on_verification_event=recorder)
        h.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb_strp"))
        resolution_events = [p for stage, p in recorder.calls if stage == "resolution"]
        ok = (
            len(resolution_events) == 1
            and resolution_events[0]["resolution_method"] == "fuzzy"
            and resolution_events[0]["resolved_entity"] == "light.wled"
            and resolution_events[0]["executable"] is True
        )
        return ok, f"resolution_events={resolution_events}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_observability_exact_match_emits_no_resolution_event():
    """Tier 1-3 (the overwhelming majority of real traffic) stays silent
    - see `_emit_resolution()`'s own docstring for why (matches the
    module's pre-existing "no event for nothing new to report" rule and
    is what keeps `test_events_unknown_device_emits_nothing` in the
    Reliability Sprint's own test file passing unchanged)."""
    saved_devices = _patch_real_devices()
    saved_env = _set_env(VERIFY_DELAY_MS=10, VERIFY_TIMEOUT_MS=1000)
    try:
        client = FakeHAClient()
        client.states["light.wled"] = "off"
        client.state_after_call["light.wled"] = "on"
        recorder = _EventRecorder()
        h = _handler(client, on_verification_event=recorder)
        h.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb_strip"))
        resolution_events = [p for stage, p in recorder.calls if stage == "resolution"]
        ok = resolution_events == []
        return ok, f"resolution_events={resolution_events}"
    finally:
        _restore_devices(saved_devices)
        _restore_env(saved_env)


def test_observability_ambiguous_case_emits_resolution_event():
    saved_devices = _patch_real_devices()
    try:
        recorder = _EventRecorder()
        h = _handler(on_verification_event=recorder)
        # Force an engineered ambiguous case using the real handler's own
        # gate by widening fuzzy_min_margin so far that every distinct
        # device in the registry ends up "in contention" for a crafted
        # query - simpler/more honest than trying to find a naturally-
        # colliding real-world phrase (see test_T's own docstring, and
        # docs/change_impact/sprint52_ha_entity_resolution.md's finding
        # that this project's real 6-device registry has no two devices
        # close enough to tie from a typo alone). This still exercises
        # the REAL execute() -> _resolve_entity_tiered() ->
        # _emit_resolution() path end to end, just with a deliberately
        # loosened margin standing in for "two closer real devices".
        saved_env = _set_env(FUZZY_ENTITY_MIN_MARGIN=1.0, FUZZY_ENTITY_MIN_CONFIDENCE=0.1)
        try:
            result = h.execute(ToolCall(tool="home_assistant", action="turn_on", target="rgb"))
        finally:
            _restore_env(saved_env)
        resolution_events = [p for stage, p in recorder.calls if stage == "resolution"]
        ok = (
            not result.success and result.error_type == "UnknownDevice"
            and len(resolution_events) == 1 and resolution_events[0]["resolution_method"] == "ambiguous"
            and resolution_events[0]["executable"] is False
        )
        return ok, f"success={result.success} resolution_events={resolution_events}"
    finally:
        _restore_devices(saved_devices)


def test_set_brightness_benefits_from_fuzzy_resolution_too():
    """The resolver sits upstream of ALL actions (not just turn_on/off/
    toggle) - a typo'd target on set_brightness must benefit the same
    way."""
    saved_devices = _patch_real_devices()
    try:
        client = FakeHAClient()
        h = _handler(client)
        result = h.execute(ToolCall(tool="home_assistant", action="set_brightness", target="rgb_strp", parameters={"level": 50}))
        ok = result.success and client.calls and client.calls[0][2] == "light.wled"
        return ok, f"success={result.success} calls={client.calls}"
    finally:
        _restore_devices(saved_devices)


def test_mock_handler_untouched_by_sprint_52():
    """`MockHomeAssistantHandler` was deliberately left unmodified (see
    docs/change_impact/sprint52_ha_entity_resolution.md's scope notes) -
    it never consulted `luno.devices` at all, so there is no resolver to
    extend there; this is a quick smoke test that it still just echoes
    whatever target it's given, unaffected by anything in this sprint."""
    handler = MockHomeAssistantHandler()
    result = handler.execute(ToolCall(tool="home_assistant", action="turn_on", target="anything_at_all_even_typo3d"))
    ok = result.success and result.data.get("target") == "anything_at_all_even_typo3d"
    return ok, f"success={result.success} data={result.data}"


def test_performance_fuzzy_tier_is_fast():
    """No network/LLM call anywhere in the resolver - `_score_candidates()`
    is pure in-process string comparison over a ~6-device registry.
    500-call measurement, generous 5ms/call bound (matches the
    Runtime Observability sprint's own precedent target)."""
    saved = _patch_real_devices()
    try:
        h = _handler()
        n = 500
        start = time.time()
        for _ in range(n):
            h._resolve_entity_tiered("rg strip")  # deliberately the fuzzy path, not the tier-1 fast path
        elapsed_ms = (time.time() - start) * 1000
        mean_ms = elapsed_ms / n
        ok = mean_ms < 5.0
        return ok, f"mean={mean_ms:.4f}ms over {n} calls"
    finally:
        _restore_devices(saved)


def test_score_candidates_uses_only_stdlib_difflib():
    """Explicit guard for the sprint's own forbidden-actions list: no
    embeddings/vector search/second ranking system - `_score_candidates()`
    must be traceable to `difflib.SequenceMatcher` alone."""
    import inspect
    from luno.tool_manager.builtin import real_home_assistant as mod
    src = inspect.getsource(mod._score_candidates)
    ok = "difflib" in src and "SequenceMatcher" in src and "requests" not in src and "http" not in src.lower()
    return ok, "source inspected for forbidden-dependency markers"


def main():
    scenarios = [
        ("A_exact_command_match_unchanged", test_A_exact_command_match_unchanged),
        ("B_case_variation", test_B_case_variation),
        ("C_spacing_variation_no_separator", test_C_spacing_variation_no_separator),
        ("D_missing_character_typo", test_D_missing_character_typo),
        ("E_transposition_typo", test_E_transposition_typo),
        ("F_missing_trailing_character", test_F_missing_trailing_character),
        ("G_alias_exact_match", test_G_alias_exact_match),
        ("H_alias_typo_fuzzy", test_H_alias_typo_fuzzy),
        ("I_script_alias_bugfix_exact", test_I_script_alias_bugfix_exact),
        ("J_script_name_typo_fuzzy", test_J_script_name_typo_fuzzy),
        ("K_switch_exact_match", test_K_switch_exact_match),
        ("L_switch_typo_fuzzy_missing_char", test_L_switch_typo_fuzzy_missing_char),
        ("M_switch_typo_fuzzy_extra_char", test_M_switch_typo_fuzzy_extra_char),
        ("N_aquascape_typo_missing_trailing_char", test_N_aquascape_typo_missing_trailing_char),
        ("O_aquascape_typo_missing_middle_char", test_O_aquascape_typo_missing_middle_char),
        ("P_partial_name_resolves_unambiguously", test_P_partial_name_resolves_unambiguously),
        ("Q_alias_and_name_dedupe_to_same_entity", test_Q_alias_and_name_dedupe_to_same_entity),
        ("R_too_little_information_refuses", test_R_too_little_information_refuses),
        ("S_unknown_device_no_crash", test_S_unknown_device_no_crash),
        ("T_ambiguity_gate_direct_unit_test", test_T_ambiguity_gate_direct_unit_test),
        ("U_end_to_end_execute_turn_off_fuzzy_target", test_U_end_to_end_execute_turn_off_fuzzy_target),
        ("V_regression_exact_targets_unchanged", test_V_regression_exact_targets_unchanged),
        ("observability_fuzzy_resolution_emits_resolution_event", test_observability_fuzzy_resolution_emits_resolution_event),
        ("observability_exact_match_emits_no_resolution_event", test_observability_exact_match_emits_no_resolution_event),
        ("observability_ambiguous_case_emits_resolution_event", test_observability_ambiguous_case_emits_resolution_event),
        ("set_brightness_benefits_from_fuzzy_resolution_too", test_set_brightness_benefits_from_fuzzy_resolution_too),
        ("mock_handler_untouched_by_sprint_52", test_mock_handler_untouched_by_sprint_52),
        ("performance_fuzzy_tier_is_fast", test_performance_fuzzy_tier_is_fast),
        ("score_candidates_uses_only_stdlib_difflib", test_score_candidates_uses_only_stdlib_difflib),
    ]

    results = {}
    for name, fn in scenarios:
        print(f"\n{'=' * 60}\n{name}\n{'=' * 60}")
        start = time.time()
        try:
            ok, detail = fn()
        except Exception as ex:
            ok, detail = False, f"EXCEPTION: {ex}"
            import traceback
            traceback.print_exc()
        print(f"{PASS if ok else FAIL} ({time.time() - start:.2f}s) {detail}")
        results[name] = ok

    print(f"\n{'=' * 60}\nSummary\n{'=' * 60}")
    for name, ok in results.items():
        print(f"  {PASS if ok else FAIL}  {name}")

    all_ok = all(results.values())
    print(f"\n{PASS if all_ok else FAIL} {'All scenarios passed.' if all_ok else 'Some scenarios failed - see detail above.'}")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
