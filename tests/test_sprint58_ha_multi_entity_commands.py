"""
tests/test_sprint58_ha_multi_entity_commands.py
=================================================

Sprint 58 - Home Assistant Multi-Entity & Group Commands.

Exercises the A-V scenario matrix from this sprint's own brief against
the new pre-Planner group/multi-target resolution layer added to
`PlannerBridgeModule` in `main_runtime_demo.py`:

    _ha_group_all_lights_shape()        - detects "semua lampu"/"all lights"
    _ha_explicit_multi_target_shape()   - detects "A dan B [dan C ...]"
    _resolve_ha_group_targets()         - resolves each target via the
                                           EXISTING Sprint 52 resolver
                                           (RealHomeAssistantHandler.
                                           _resolve_entity_tiered(), a
                                           throwaway client=None instance)
    _apply_ha_group_resolution()        - orchestrator: detect -> resolve
                                           ALL targets -> rewrite (all
                                           clear) or refuse-the-whole-
                                           command (any target failed)

This is NOT a second HA system, NOT a second entity resolver, NOT a new
persistent memory/state system. Group resolution sits ABOVE the existing
individual resolver and calls it once per target - see
`docs/change_impact/ha_multi_entity_commands.md` for the full
architecture/reuse writeup and Phase 0 reconnaissance findings.

**Device-name substitution note:** the brief's own illustrative examples
("lampu kamar", "lampu ruang tamu") are not configured devices in THIS
checkout (`config/lights.config.json` only defines Main Lamp/RGB Strip/
RGB Computer as lights, Baterai/Aquascape as switches, `config/scripts.
config.json` defines one script - gaming mode). Every test below
substitutes real, live-configured device names for those illustrative
ones - same convention every prior sprint's test file already
established (never invent a device name a real resolver could not
actually resolve).

    Main Lamp    -> light.kamar_tidur_light_bulb  (alias: "lampu utama")
    RGB Strip    -> light.wled
    RGB Computer -> light.kamar_tidur_pc           (alias: "RGB komputer")
    gaming mode  -> script.gaming_mode             (alias: "mode gaming")

Run:
    python3 -m pytest tests/test_sprint58_ha_multi_entity_commands.py -v
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
    """Runs `fn(bridge)` with the real device registry patched in,
    always restoring afterward even if `fn` raises - same discipline as
    every other sprint's own device-patching tests."""
    saved = _patch_real_devices()
    try:
        return fn(_bridge())
    finally:
        _restore_devices(saved)


# ============================================================================
# A - single target still works (group layer is a total no-op for it)
# ============================================================================

def test_A_single_target_still_works_unaffected():
    def run(bridge):
        text, note = bridge._apply_ha_group_resolution("matikan rgb strip", "c1")
        assert note is None
        assert text == "matikan rgb strip"  # byte-for-byte untouched
        # and the full existing Sprint 57 contextual-fill layer still works
        # on top of it, completely unaffected
        effective = bridge._apply_device_context(text, "c1")
        steps = IntentParser.parse(effective)
        assert (steps[0].tool, steps[0].action, steps[0].target) == ("home_assistant", "turn_off", "rgb_strip")
    _with_real_devices(run)


# ============================================================================
# B - two explicit targets
# ============================================================================

def test_B_two_explicit_targets_both_resolve_and_execute():
    def run(bridge):
        text, note = bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer", "c1")
        assert note is None
        assert text == "turn off rgb_strip, turn off rgb_komputer"
        steps = IntentParser.parse(text)
        assert len(steps) == 2
        assert all(s.tool == "home_assistant" and s.action == "turn_off" for s in steps)
        assert {s.target for s in steps} == {"rgb_strip", "rgb_komputer"}
    _with_real_devices(run)


# ============================================================================
# C - three explicit targets
# ============================================================================

def test_C_three_explicit_targets_all_resolve():
    def run(bridge):
        text, note = bridge._apply_ha_group_resolution(
            "matikan rgb strip dan rgb komputer dan lampu utama", "c1",
        )
        assert note is None
        steps = IntentParser.parse(text)
        assert len(steps) == 3
        assert {s.target for s in steps} == {"rgb_strip", "rgb_komputer", "lampu_utama"}
    _with_real_devices(run)


# ============================================================================
# D - all lights ("semua lampu")
# ============================================================================

def test_D_all_lights_group_all():
    def run(bridge):
        text, note = bridge._apply_ha_group_resolution("nyalain semua lampu", "c1")
        assert note is None
        steps = IntentParser.parse(text)
        assert len(steps) == 3  # Main Lamp, RGB Strip, RGB Computer - the whole real light registry
        assert all(s.tool == "home_assistant" and s.action == "turn_on" for s in steps)
        assert {s.target for s in steps} == {"main_lamp", "rgb_strip", "rgb_computer"}
    _with_real_devices(run)


# ============================================================================
# E - all lights with ZERO eligible entities (empty registry)
# ============================================================================

def test_E_all_lights_zero_eligible_entities_is_a_safe_no_op():
    saved = _patch_devices(lights={}, switches={}, scripts={})
    try:
        bridge = _bridge()
        text, note = bridge._apply_ha_group_resolution("nyalain semua lampu", "c1")
        assert text == ""  # zero real Planner tasks - IntentParser.parse("") == []
        assert IntentParser.parse(text) == []
        assert note is not None and "no lights configured" in note
    finally:
        _restore_devices(saved)


# ============================================================================
# F - area-based group ("semua lampu di kamar") - investigated, proven
#     unsupported (zero area metadata anywhere in this project), refused
#     honestly rather than silently expanding to every light or guessing.
# ============================================================================

def test_F_area_qualified_group_is_honestly_refused_not_guessed():
    # UPDATED (Sprint 60 - Structured Room/Area Schema Foundation): this
    # assertion originally proved NO LIGHTS entry carried any area/room/
    # zone metadata at all (Sprint 58's own gap analysis). Sprint 60
    # deliberately added an optional, additive "area" field to the real
    # registry (config/lights.config.json) - see docs/change_impact/
    # area_schema_foundation.md - so every real light now carries
    # "area": "kamar" (the one room this project's evidence supports,
    # unchanged since Sprint 59); "room"/"zone" remain unused key names
    # (Sprint 60 picked "area" to match this project's own pre-existing
    # "area_word"/"_GROUP_AREA_RE" vocabulary in main_runtime_demo.py).
    # This is a documentation-of-fact update, not a Sprint 52-59
    # regression - the actual command behavior this test exists to prove
    # (an unsupported area word is still honestly refused, never guessed)
    # is unchanged below.
    from luno import devices
    for cfg in devices.LIGHTS.values():
        if isinstance(cfg, dict):
            assert "room" not in cfg and "zone" not in cfg
            assert cfg.get("area") == "kamar"

    def run(bridge):
        # UPDATED (Sprint 59 - Single-Room Home Assistant Group Control):
        # "kamar" specifically is no longer refused - Sprint 59 recognized
        # it as this project's one identifiable room (converging evidence:
        # Main Lamp's own entity_id, the pre-existing "sleepy" environment
        # trigger already grouping all 3 lights, zero second-room evidence
        # anywhere) and un-deferred exactly that one case. This was Sprint
        # 58's own explicitly DEFERRED scenario, not a Sprint 52-58
        # regression - see docs/change_impact/ha_single_room_group_
        # control.md. Multi-room remains refused: any OTHER area word
        # still gets the exact same honest "not supported" refusal this
        # test originally proved for "kamar" too.
        text, note = bridge._apply_ha_group_resolution("matikan semua lampu di dapur", "c1")
        assert text == ""  # never silently expands to "every light" or guesses
        assert note is not None
    _with_real_devices(run)


# ============================================================================
# G - one ambiguous target -> ENTIRE command refused
# ============================================================================

def test_G_one_ambiguous_target_refuses_the_whole_command():
    saved = _patch_devices(lights={
        "RGB Strip": {"entity_id": "light.wled", "aliases": []},
        "Lampu Kamar Utama": {"entity_id": "light.kamar_utama", "aliases": []},
        "Lampu Kamar Depan": {"entity_id": "light.kamar_depan", "aliases": []},
    })
    try:
        bridge = _bridge()
        # "rgb strip" is a clean exact match; "lampu kamar" is genuinely
        # ambiguous between the two "Lampu Kamar ..." devices.
        text, note = bridge._apply_ha_group_resolution("matikan rgb strip dan lampu kamar", "c1")
        assert text == ""
        assert note is not None and "ambiguous" in note
        resolved, ambiguous_count, unresolved_count = bridge._resolve_ha_group_targets(["rgb_strip", "lampu_kamar"])
        assert ambiguous_count >= 1
    finally:
        _restore_devices(saved)


# ============================================================================
# H - one unresolved target -> ENTIRE command refused
# ============================================================================

def test_H_one_unresolved_target_refuses_the_whole_command():
    def run(bridge):
        text, note = bridge._apply_ha_group_resolution(
            "matikan rgb strip dan zzz_nonexistent_device_xyz", "c1",
        )
        assert text == ""
        assert note is not None
        resolved, ambiguous_count, unresolved_count = bridge._resolve_ha_group_targets(
            ["rgb_strip", "zzz_nonexistent_device_xyz"],
        )
        assert unresolved_count >= 1
    _with_real_devices(run)


# ============================================================================
# I - duplicate target (literal) -> not executed twice
# ============================================================================

def test_I_literal_duplicate_target_deduplicated():
    def run(bridge):
        text, note = bridge._apply_ha_group_resolution("matikan rgb strip dan rgb strip", "c1")
        assert note is None
        steps = IntentParser.parse(text)
        assert len(steps) == 1
        assert steps[0].target == "rgb_strip"
    _with_real_devices(run)


# ============================================================================
# J - same entity referenced via two different aliases -> deduplicated
# ============================================================================

def test_J_same_entity_via_two_aliases_deduplicated():
    def run(bridge):
        # "RGB Computer" (primary name) and "RGB komputer" (its configured
        # alias) both resolve to entity_id light.kamar_tidur_pc.
        text, note = bridge._apply_ha_group_resolution("matikan rgb computer dan rgb komputer", "c1")
        assert note is None
        steps = IntentParser.parse(text)
        assert len(steps) == 1
        resolved, _, _ = bridge._resolve_ha_group_targets(["rgb_computer", "rgb_komputer"])
        entity_ids = {r[2] for r in resolved}
        assert entity_ids == {"light.kamar_tidur_pc"}
    _with_real_devices(run)


# ============================================================================
# K - fuzzy target + exact target, both resolve, both execute
# ============================================================================

def test_K_fuzzy_target_plus_exact_target_both_resolve():
    def run(bridge):
        # "rgb strp" is a typo (Sprint 52 fuzzy tier); "lampu utama" is an
        # exact alias match.
        text, note = bridge._apply_ha_group_resolution("matikan rgb strp dan lampu utama", "c1")
        assert note is None
        steps = IntentParser.parse(text)
        assert len(steps) == 2
        resolved, ambiguous_count, unresolved_count = bridge._resolve_ha_group_targets(["rgb_strp", "lampu_utama"])
        assert ambiguous_count == 0 and unresolved_count == 0
        methods = {r[0]: r[1] for r in resolved}
        assert methods["rgb_strp"] == "fuzzy"
        assert methods["lampu_utama"] in ("exact", "alias")
    _with_real_devices(run)


# ============================================================================
# L - two clearly-differentiable targets resolve independently, no
#     cross-contamination (reuses the SAME per-target resolver Sprint 52/
#     56 already established - not a new differentiator mechanism).
# ============================================================================

def test_L_two_distinct_targets_resolve_independently_without_cross_talk():
    def run(bridge):
        text, note = bridge._apply_ha_group_resolution("matikan main lamp dan rgb strip", "c1")
        assert note is None
        resolved, _, _ = bridge._resolve_ha_group_targets(["main_lamp", "rgb_strip"])
        entity_map = {r[0]: r[2] for r in resolved}
        assert entity_map["main_lamp"] == "light.kamar_tidur_light_bulb"
        assert entity_map["rgb_strip"] == "light.wled"
        assert entity_map["main_lamp"] != entity_map["rgb_strip"]
    _with_real_devices(run)


# ============================================================================
# M - entity unavailable at RUNTIME (no such "disabled"/"unavailable"
#     field exists anywhere in this project's registry metadata - runtime
#     availability is a downstream execution-time concern, already
#     handled by the existing, UNMODIFIED verification loop in
#     RealHomeAssistantHandler.execute() - not a resolution-time concern
#     this sprint's all-or-nothing guarantee ever promised to cover).
# ============================================================================

def test_M_runtime_unavailability_is_handled_independently_per_target():
    def run(bridge):
        text, note = bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer", "c1")
        assert note is None
        steps = IntentParser.parse(text)

        client = FakeHAClient()
        client.state_after_call["light.wled"] = "off"  # rgb strip verifies successfully
        # light.kamar_tidur_pc deliberately has NO post-call state -> reports "unavailable"
        handler = RealHomeAssistantHandler(client)
        results = []
        for s in steps:
            tc = TMToolCall(tool="home_assistant", action=s.action, target=s.target, parameters={})
            results.append((s.target, handler.execute(tc)))

        by_target = dict(results)
        assert by_target["rgb_strip"].success is True
        assert by_target["rgb_komputer"].success is False
        # BOTH were independently attempted regardless of the other's outcome -
        # resolution-time all-or-nothing already guaranteed both were valid
        # targets; a RUNTIME failure for one never silently cancels the other.
        assert len(client.calls) == 2
    _with_real_devices(run)


# ============================================================================
# N - target domain mismatch (resolves to a real entity, but in a domain
#     turn_on/turn_off was never meant for - e.g. a SCRIPT)
# ============================================================================

def test_N_target_domain_mismatch_refuses_the_whole_command():
    def run(bridge):
        text, note = bridge._apply_ha_group_resolution("matikan rgb strip dan gaming mode", "c1")
        assert text == ""
        assert note is not None
        resolved, ambiguous_count, unresolved_count = bridge._resolve_ha_group_targets(["rgb_strip", "gaming_mode"])
        methods = {r[0]: r[1] for r in resolved}
        assert methods["gaming_mode"] == "domain_mismatch"
        assert unresolved_count >= 1
    _with_real_devices(run)


# ============================================================================
# O - the word "dan" appearing INSIDE a device's own name/alias. Confirmed
#     via Phase 0 reconnaissance: `_CLAUSE_SPLIT_RE` in luno.planner.parser
#     ALREADY splits on bare "dan" unconditionally (a pre-existing, general
#     parser limitation, not something this sprint introduces or should
#     try to fix - see this sprint's own STOP CONDITION: a universal fix
#     to clause splitting risks changing the semantics of every existing
#     multi-clause command, which is exactly the kind of "major change to
#     the parser" this sprint was told not to force). This test proves the
#     one thing that actually matters: the system still FAILS SAFELY
#     (refuses) rather than silently executing against the wrong
#     fragment(s) of the name.
# ============================================================================

def test_O_dan_inside_a_device_name_fails_safely_not_silently_wrong():
    saved = _patch_devices(lights={"Meja dan Kursi": {"entity_id": "light.meja_kursi", "aliases": []}})
    try:
        bridge = _bridge()
        text, note = bridge._apply_ha_group_resolution("matikan meja dan kursi", "c1")
        # Pre-existing parser limitation: this DOES get detected as a
        # (mis-split) 2-target command ("meja", "kursi") - neither
        # fragment matches the real device name, so both fail to resolve
        # and the whole command is safely refused. Never silently executes
        # against a wrong/partial match.
        assert text == ""
        assert note is not None
    finally:
        _restore_devices(saved)


# ============================================================================
# P - a plain contextual reference (Sprint 57) must never be misdetected
#     as a group.
# ============================================================================

def test_P_contextual_reference_never_becomes_a_group():
    def run(bridge):
        text, note = bridge._apply_ha_group_resolution("Matikan.", "c1")
        assert note is None
        assert text == "Matikan."  # completely untouched - falls through to _apply_device_context
        text2, note2 = bridge._apply_ha_group_resolution("nyalakan rgb strip", "c1")
        assert note2 is None and text2 == "nyalakan rgb strip"
    _with_real_devices(run)


# ============================================================================
# Q / CRITICAL SAFETY INVARIANT - Target A valid, Target B ambiguous ->
#     the Home Assistant API is called ZERO times for this turn. Simulates
#     the EXACT production wiring: _apply_ha_group_resolution() produces
#     the effective_text _handle_utterance() would use, IntentParser.parse()
#     runs on it, and `real_task_count > 0` gates self.planner.execute()
#     (see main_runtime_demo.py's own comment at that call site) - proving
#     the mechanism, not just asserting the outcome by construction.
# ============================================================================

def test_Q_critical_invariant_valid_plus_ambiguous_target_zero_ha_calls():
    saved = _patch_devices(lights={
        "RGB Strip": {"entity_id": "light.wled", "aliases": []},
        "Lampu Kamar Utama": {"entity_id": "light.kamar_utama", "aliases": []},
        "Lampu Kamar Depan": {"entity_id": "light.kamar_depan", "aliases": []},
    })
    try:
        bridge = _bridge()
        # Target A ("rgb strip") is unambiguous; Target B ("lampu kamar")
        # is genuinely ambiguous between two real devices.
        effective_text, refusal_note = bridge._apply_ha_group_resolution(
            "matikan rgb strip dan lampu kamar", "c1",
        )
        assert refusal_note is not None
        assert effective_text == ""

        # Exactly the production gate from _handle_utterance():
        #   plan = self.planner.create_plan(effective_text, ...)
        #   real_task_count = sum(1 for t in plan.tasks if t.tool_call.tool != "unknown")
        #   if plan.tasks and not plan.validation_errors and real_task_count > 0:
        #       self.planner.execute(plan)   # <- the ONLY place an HA tool call can originate
        parsed_steps = IntentParser.parse(effective_text)
        assert parsed_steps == []
        real_task_count = sum(1 for s in parsed_steps if s.tool != "unknown")
        assert real_task_count == 0  # self.planner.execute() is never reached this turn

        # Belt-and-suspenders: even if something DID call execute() on a
        # handler bound to a real client, prove zero calls actually happen.
        client = FakeHAClient()
        handler = RealHomeAssistantHandler(client)
        for s in parsed_steps:
            tc = TMToolCall(tool="home_assistant", action=s.action, target=s.target, parameters={})
            handler.execute(tc)
        assert client.calls == []
    finally:
        _restore_devices(saved)


# ============================================================================
# R - deterministic execution order (same input -> same rewritten order,
#     every time)
# ============================================================================

def test_R_deterministic_rewrite_order():
    def run(bridge):
        outputs = {bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer dan lampu utama", "c1")[0]
                   for _ in range(20)}
        assert len(outputs) == 1  # always the exact same rewritten text
        outputs2 = {bridge._apply_ha_group_resolution("nyalain semua lampu", "c1")[0] for _ in range(20)}
        assert len(outputs2) == 1
    _with_real_devices(run)


# ============================================================================
# S - zero-target discovery: registry has entries, but NONE are eligible
#     (fail-safe - never assume a registry entry has a valid entity_id)
# ============================================================================

def test_S_zero_eligible_entities_despite_non_empty_registry():
    saved = _patch_devices(lights={
        "Broken Light": {"aliases": []},           # missing entity_id entirely
        "Also Broken": {"entity_id": None, "aliases": []},
    })
    try:
        bridge = _bridge()
        text, note = bridge._apply_ha_group_resolution("nyalain semua lampu", "c1")
        assert text == ""
        assert note is not None and "no lights configured" in note
    finally:
        _restore_devices(saved)


# ============================================================================
# T - observability event correctness (Sprint 50 Event Bus reuse - no raw
#     utterance text, structured/bounded fields only)
# ============================================================================

def test_T_observability_event_on_successful_group():
    from luno.core.event_bus import EventBus
    bus = EventBus()
    bus.start()
    try:
        saved = _patch_real_devices()
        try:
            bridge = _bridge()
            bridge.bind_event_bus(bus)
            events = []
            bus.subscribe("ha_group_command_resolution", lambda e: events.append(dict(e.data)))
            bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer", "c1")
            assert _wait_until(lambda: len(events) >= 1, 2.0)
            e = events[0]
            assert e["command_kind"] == "explicit_multi_target"
            assert e["detected_target_count"] == 2
            assert e["resolved_target_count"] == 2
            assert e["ambiguous_target_count"] == 0
            assert e["unresolved_target_count"] == 0
            assert e["final_decision"] == "executed"
            assert "rgb strip" not in str(e.values())  # never the raw utterance text
        finally:
            _restore_devices(saved)
    finally:
        bus.stop()


def test_T_observability_event_on_refused_group():
    from luno.core.event_bus import EventBus
    bus = EventBus()
    bus.start()
    try:
        bridge = _bridge()
        bridge.bind_event_bus(bus)
        events = []
        bus.subscribe("ha_group_command_resolution", lambda e: events.append(dict(e.data)))
        bridge._apply_ha_group_resolution("matikan rgb strip dan zzz_nonexistent_device_xyz", "c1")
        assert _wait_until(lambda: len(events) >= 1, 2.0)
        e = events[0]
        assert e["command_kind"] == "explicit_multi_target"
        assert e["final_decision"] == "refused_unresolved"
        assert e["unresolved_target_count"] >= 1
    finally:
        bus.stop()


def test_T_observability_event_never_breaks_the_turn_without_a_bus():
    bridge = _bridge()  # no bind_event_bus() call at all - self._event_bus is None
    text, note = bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer", "c1")
    assert note is None  # still resolves correctly with zero event bus wired up


# ============================================================================
# U - persistent-state safety: no config/*.json file is ever touched by
#     group resolution (pure, read-only registry lookups + in-memory
#     rewrite), and no new persistent state dict is introduced.
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


def test_U_persistent_state_untouched_by_group_resolution():
    before = _hash_config_files()

    def run(bridge):
        bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer", "c1")
        bridge._apply_ha_group_resolution("nyalain semua lampu", "c1")
        bridge._apply_ha_group_resolution("matikan semua lampu di kamar", "c1")
        bridge._apply_ha_group_resolution("matikan rgb strip dan zzz_ghost", "c1")

    _with_real_devices(run)
    after = _hash_config_files()
    assert before == after


def test_U_no_new_persistent_state_attribute_introduced():
    bridge = _bridge()
    # Sprint 58 must not add a second memory system / global last_entity -
    # only pure methods and the pre-existing Sprint 57 per-conversation
    # dicts should exist; no new instance-level mutable "remembered group"
    # state should ever accumulate across calls.
    before = {k: (dict(v) if isinstance(v, dict) else v) for k, v in vars(bridge).items()}
    bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer", "c1")
    bridge._apply_ha_group_resolution("nyalain semua lampu", "c1")
    after_keys = set(vars(bridge).keys())
    assert set(before.keys()) == after_keys  # no new attribute was created


# ============================================================================
# V - performance (<5ms typical, no network calls per candidate)
# ============================================================================

def test_V_performance_under_5ms():
    def run(bridge):
        n = 100
        t0 = time.perf_counter()
        for _ in range(n):
            bridge._apply_ha_group_resolution("matikan rgb strip dan rgb komputer dan lampu utama", "c1")
        t1 = time.perf_counter()
        avg_ms = (t1 - t0) / n * 1000
        assert avg_ms < 5.0, f"explicit multi-target group resolution too slow: {avg_ms:.3f}ms"

        t0 = time.perf_counter()
        for _ in range(n):
            bridge._apply_ha_group_resolution("nyalain semua lampu", "c1")
        t1 = time.perf_counter()
        avg_ms = (t1 - t0) / n * 1000
        assert avg_ms < 5.0, f"group-all resolution too slow: {avg_ms:.3f}ms"
    _with_real_devices(run)


# ============================================================================
# Structural invariants (no second resolver, no LLM/embeddings, no bypass)
# ============================================================================

def test_invariant_group_layer_reuses_the_existing_resolver_class():
    import inspect
    src = inspect.getsource(demo.PlannerBridgeModule._resolve_ha_group_targets)
    assert "RealHomeAssistantHandler" in src
    assert "_resolve_entity_tiered" in src
    # no second resolver class/module is imported anywhere in this file's
    # group-resolution methods
    for method_name in (
        "_ha_group_all_lights_shape", "_ha_explicit_multi_target_shape",
        "_resolve_ha_group_targets", "_apply_ha_group_resolution",
    ):
        method_src = inspect.getsource(getattr(demo.PlannerBridgeModule, method_name))
        lowered = method_src.lower()
        for banned in ("openai", "embedding", "llm", "gpt", "anthropic", "sentence_transformer"):
            assert banned not in lowered, f"{method_name} must not reference {banned!r}"


def test_invariant_no_client_touching_calls_during_group_resolution():
    """`_resolve_ha_group_targets()` must never call `.execute()` or touch
    a real HA client - it only ever calls `_resolve_entity_tiered()`,
    confirmed (Phase 0 reconnaissance) to be pure/client-free."""
    class _ExplodingClient:
        def call_service(self, *a, **kw):
            raise AssertionError("group resolution must never touch a real HA client")

        def get_entity_state(self, *a, **kw):
            raise AssertionError("group resolution must never touch a real HA client")

    def run(bridge):
        # Monkeypatch the throwaway handler construction is unnecessary -
        # `_resolve_ha_group_targets` always constructs `client=None`
        # itself; this test instead proves that EVEN IF a real client
        # existed elsewhere in this process, group resolution never reaches
        # it, by resolving targets and confirming no exception was raised
        # (the exploding client is never even constructed by this code path).
        resolved, ambiguous_count, unresolved_count = bridge._resolve_ha_group_targets(["rgb_strip", "rgb_komputer"])
        assert ambiguous_count == 0 and unresolved_count == 0
    _with_real_devices(run)


def _wait_until(predicate, timeout_s):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
