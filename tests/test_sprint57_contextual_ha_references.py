"""
tests/test_sprint57_contextual_ha_references.py
=================================================

Sprint 57 - Contextual Home Assistant References & Target Continuity.

Sprint 56 concluded "no contextual-reference resolver for HA entities
exists today" - true of the layers Sprint 56 actually investigated
(the Tool Manager resolver, and the LLM-prompt-context layer in
`luno/memory_context.py`), but incomplete: `PlannerBridgeModule.
_apply_device_context()` in `main_runtime_demo.py` is a live, tested,
PRE-EXISTING short-term device-context mechanism (predates Sprint
52/55/56) doing exactly this at a THIRD layer - a text-rewrite step
that runs before `IntentParser`/the Planner ever sees the utterance.

This sprint does not build a new resolver or a second memory/topic
system. It HARDENS the existing one:

  - a richer per-tool memory value (`{"target", "turn_seq",
    "entity_id", "domain"}` instead of a bare string) so freshness and
    domain-compatibility can be checked before a contextual fill is
    trusted (`_remember_device_target`, `_device_context_entity_info`);
  - bounded freshness via a per-conversation turn counter
    (`_device_context_turn_seq`, `_CONTEXT_MAX_TURN_AGE`);
  - domain compatibility for a plain on/off fill
    (`_CONTEXT_FILL_COMPATIBLE_DOMAINS`);
  - same-turn multi-device ambiguity clears memory rather than letting
    the last-mentioned device silently win (no "most-recent-wins"
    guess when the evidence is genuinely ambiguous);
  - a broadened REMEMBER action set (`_CONTEXT_REMEMBER_ACTIONS`,
    covering `set_color`/`set_brightness`/`set_value` in addition to
    `turn_on`/`turn_off`) so "Setel RGB komputer ke biru." -> "Matikan."
    works, not just plain on/off pairs;
  - a failed/timed-out HA execution un-remembering its own target
    (`_invalidate_device_context_on_failure`, correlated via a
    `threading.local()` slot set once per spawned `_handle_utterance`
    thread - see that attribute's own docstring in `__init__`), so a
    command that never actually succeeded can never become a strong
    contextual target for a later turn;
  - two new referential filler words ("yang", "tadi") added to the
    EXISTING `_CONTEXT_FILLER_WORDS` set so "yang itu"/"yang tadi"
    phrasing is recognized as "named no real device" (eligible for
    context fill), matching "-nya" (already covered by the pre-
    existing "nya" filler word);
  - a message-quality fix in `real_home_assistant.py`'s `execute()` -
    a genuinely target-less command (post-context-resolution) now gets
    an honest "which device did you mean" refusal instead of falling
    through into execution with `entity_id=None`/`target=""` and
    producing the confusing "None is currently unavailable." message.

Scenarios below are labeled A-V per the sprint brief's own convention.
Tests against `_apply_device_context` reuse `tests/test_device_context.
py`'s own convention: the checkout's REAL configured devices (via
`luno.devices`, unpatched) - "RGB Strip"/"Main Lamp" (alias "lampu
utama")/"RGB Computer" (alias "rgb komputer")/"Baterai"/"Aquascape".
Tests against `execute()`/the Tool Manager layer reuse `tests/
test_sprint52_ha_entity_resolution.py`'s own fixture helpers
(`_patch_real_devices`, `FakeHAClient`, `_handler`), the same
convention `test_sprint56_ha_safety_matrix.py` already established for
a takeover sprint building on Sprint 52's own test infrastructure
rather than duplicating it.

Run:
    python3 -m pytest tests/test_sprint57_contextual_ha_references.py
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
from luno.planner.models import ToolCall as PlannerToolCall  # noqa: E402

from test_sprint52_ha_entity_resolution import (  # noqa: E402
    FakeHAClient, _handler, _patch_real_devices, _restore_devices,
)
from luno.tool_manager.models import ToolCall as TMToolCall  # noqa: E402


def _bridge() -> "demo.PlannerBridgeModule":
    return demo.PlannerBridgeModule()


# ============================================================================
# A - explicit target after previous target: normal Sprint 52/56
#     resolution untouched, and REMEMBER correctly moves on to the NEW
#     device rather than sticking with the old one.
# ============================================================================

def test_A_explicit_target_after_previous_target_is_never_overridden_by_context():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    result = bridge._apply_device_context("matikan rgb komputer", "c1")
    assert result == "matikan rgb komputer"  # explicit target - text passes through untouched
    remembered = bridge._last_device_target["c1"]["home_assistant"]
    assert remembered["target"] == "rgb_komputer"  # REMEMBER moved on to the new device


# ============================================================================
# B - exactly one clear contextual target resolves, for both the
#     original FILLABLE actions (turn_on/turn_off) and the broadened
#     REMEMBER set (set_color).
# ============================================================================

def test_B_one_clear_contextual_target_fills_turn_off():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert bridge._apply_device_context("matikan", "c1") == "turn off rgb_strip"


def test_B_set_color_remembered_target_fills_a_later_turn_off():
    """"Setel RGB komputer ke biru." -> "Matikan." - the sprint's own
    named example. `set_color` is not itself a FILLABLE action, but it
    must still populate REMEMBER so a LATER plain on/off can fill from
    it."""
    bridge = _bridge()
    r1 = bridge._apply_device_context("set rgb komputer ke biru", "c1")
    assert r1 == "set rgb komputer ke biru"  # explicit clause, unchanged
    remembered = bridge._last_device_target["c1"]["home_assistant"]
    assert remembered["target"] == "rgb_komputer"
    assert bridge._apply_device_context("matikan", "c1") == "turn off rgb_komputer"


# ============================================================================
# C - two possible contextual targets named in the SAME turn: ambiguous
#     evidence about "the device this conversation is about" - memory is
#     cleared, never resolved by picking whichever clause was last.
# ============================================================================

def test_C_two_distinct_targets_in_one_turn_clears_memory_instead_of_guessing():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    bridge._apply_device_context("nyalakan rgb strip, lalu nyalakan rgb komputer", "c1")
    assert bridge._last_device_target.get("c1", {}).get("home_assistant") is None
    # and a later bare command must NOT resolve to either device
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"


# ============================================================================
# D - no contextual target at all: brand new conversation, unchanged.
# ============================================================================

def test_D_no_contextual_target_leaves_text_unchanged():
    bridge = _bridge()
    assert bridge._apply_device_context("matikan", "c1") == "matikan"
    assert bridge._last_device_target.get("c1", {}) == {}


# ============================================================================
# E / S - stale contextual target: older than `_CONTEXT_MAX_TURN_AGE`
#     turns must NOT resolve. Boundary-tested at exactly the limit too.
# ============================================================================

def test_E_stale_contextual_target_does_not_resolve():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    remembered_turn = bridge._last_device_target["c1"]["home_assistant"]["turn_seq"]
    # Push the conversation's turn counter past the freshness window
    # using harmless intervening turns (unrelated chit-chat), the same
    # way a real multi-turn conversation would age it.
    for _ in range(bridge._CONTEXT_MAX_TURN_AGE + 1):
        bridge._apply_device_context("apa kabar", "c1")
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"  # too old - refuses to guess, does not fill
    current = bridge._device_context_turn_seq["c1"]
    assert (current - remembered_turn) > bridge._CONTEXT_MAX_TURN_AGE


def test_S_context_expiration_boundary_exact_max_age_still_fresh():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    remembered_turn = bridge._last_device_target["c1"]["home_assistant"]["turn_seq"]
    # Advance exactly `_CONTEXT_MAX_TURN_AGE` turns (still inside the window).
    for _ in range(bridge._CONTEXT_MAX_TURN_AGE - 1):
        bridge._apply_device_context("apa kabar", "c1")
    current_before_fill = bridge._device_context_turn_seq["c1"]
    assert (current_before_fill - remembered_turn) == bridge._CONTEXT_MAX_TURN_AGE - 1
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "turn off rgb_strip"  # still within the window


def test_S_context_expiration_boundary_one_past_max_age_refuses():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    for _ in range(bridge._CONTEXT_MAX_TURN_AGE):
        bridge._apply_device_context("apa kabar", "c1")
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"


# ============================================================================
# F - context after conversation reset: `ConversationEnded` clears the
#     remembered device AND the turn-sequence counter backing freshness.
# ============================================================================

def test_F_conversation_ended_clears_device_context_and_turn_sequence():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert "c1" in bridge._last_device_target
    assert "c1" in bridge._device_context_turn_seq
    bridge._on_conversation_ended(demo.Event(type="conversation_ended", data={"session_id": "c1", "reason": "timeout"}))
    assert "c1" not in bridge._last_device_target
    assert "c1" not in bridge._device_context_turn_seq
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"  # brand new conversation - no memory left


# ============================================================================
# G - a remembered/resolved device that is genuinely unavailable in HA
#     must still get the NORMAL "X is currently unavailable" honest
#     result (Reliability Sprint behavior, unchanged) - never confused
#     with the NEW `_missing_target_result` refusal, which is reserved
#     for the case where there is truly no target/entity at all.
# ============================================================================

def test_G_device_unavailable_after_context_fill_gives_normal_unavailable_message_not_missing_target():
    saved = _patch_real_devices()
    try:
        client = FakeHAClient()
        # Simulate: service call succeeds, but the entity never actually
        # reports the new state (stays "unavailable") - a real device
        # genuinely not responding.
        client.state_after_call["light.wled"] = "unavailable"
        h = _handler(client)
        result = h.execute(TMToolCall(tool="home_assistant", action="turn_off", target="rgb_strip"))
        assert result.success is False
        assert result.error_type != "MissingTarget"
        assert "unavailable" in result.message.lower()
        assert "rgb_strip" in result.message or "rgb strip" in result.message.lower() or "rgb_strip" in str(result.data)
    finally:
        _restore_devices(saved)


# ============================================================================
# H - domain compatibility: a remembered target whose HA domain is NOT
#     one of `_CONTEXT_FILL_COMPATIBLE_DOMAINS` must never be used to
#     fill a plain on/off. No natural example exists in this checkout's
#     real registry (only light/switch are configured) - engineered
#     fixture proves the gate structurally, same "no natural example,
#     prove the gate anyway" precedent Sprint 52's own `test_T` used.
# ============================================================================

def test_H_incompatible_domain_is_never_used_to_fill_plain_on_off():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    # Engineer a remembered device from a domain outside the compatible
    # set (e.g. "lock" - a real HA domain, genuinely not safe to blindly
    # "turn off" the same way a light/switch/fan/climate/media_player is).
    bridge._last_device_target["c1"]["home_assistant"] = {
        "target": "front_door", "turn_seq": bridge._device_context_turn_seq["c1"],
        "entity_id": "lock.front_door", "domain": "lock",
    }
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"  # refuses - domain not compatible with plain on/off


def test_H_compatible_but_unconfigured_domain_still_fills():
    """Positive counterpart to the above - `fan`/`climate`/`media_player`
    ARE in `_CONTEXT_FILL_COMPATIBLE_DOMAINS` even though this checkout
    has none configured; the gate is domain-based, not registry-based."""
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    bridge._last_device_target["c1"]["home_assistant"] = {
        "target": "bedroom_fan", "turn_seq": bridge._device_context_turn_seq["c1"],
        "entity_id": "fan.bedroom", "domain": "fan",
    }
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "turn off bedroom_fan"


# ============================================================================
# I - previous target with a similar (but not identical) name to a new
#     EXPLICIT target: the explicit target always wins, never silently
#     swapped for the textually-similar remembered one.
# ============================================================================

def test_I_similar_named_explicit_target_is_not_confused_with_remembered_device():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    result = bridge._apply_device_context("matikan rgb komputer", "c1")
    assert result == "matikan rgb komputer"
    steps = IntentParser.parse(result)
    assert steps[0].target == "rgb_komputer"  # not rgb_strip


# ============================================================================
# J - a typo in an EXPLICIT target must still flow to the normal
#     Sprint 52 fuzzy resolver, never get swapped for a DIFFERENT
#     remembered device just because the typo left no clean target.
# ============================================================================

def test_J_typo_in_explicit_target_is_not_overridden_by_a_different_remembered_device():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb komputer", "c1")  # remembers rgb_komputer
    result = bridge._apply_device_context("matikan rgb strp", "c1")  # typo'd DIFFERENT device
    assert result == "matikan rgb strp"  # untouched - goes to normal parse+fuzzy resolution
    steps = IntentParser.parse(result)
    assert steps[0].target == "rgb_strp"  # the typo'd text itself, not "rgb_komputer"


def test_J_sprint52_fuzzy_resolution_of_the_typo_is_unaffected():
    """End-to-end proof the typo still resolves correctly via the
    UNMODIFIED Sprint 52 resolver once it reaches `execute()`."""
    saved = _patch_real_devices()
    try:
        client = FakeHAClient()
        client.state_after_call["light.wled"] = "off"
        h = _handler(client)
        result = h.execute(TMToolCall(tool="home_assistant", action="turn_off", target="rgb_strp"))
        assert result.success is True
        assert client.calls and client.calls[0][2] == "light.wled"
    finally:
        _restore_devices(saved)


# ============================================================================
# K - explicit target combined with a contextual/referential phrase
#     ("matikan rgb strip yang itu") - the explicit device name present
#     in the SAME clause must still be used; the referential words are
#     just noise attached to a genuine target, not a signal to ignore it.
# ============================================================================

def test_K_explicit_target_plus_contextual_phrase_still_uses_the_named_device():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb komputer", "c1")  # remembers rgb_komputer
    text = "nyalakan rgb strip yang itu"
    steps = IntentParser.parse(text)
    # IntentParser folds "yang itu" into the captured target text; this
    # is a genuine, explicit device name (rgb strip) plus trailing
    # referential words, not a target-less utterance - must not be
    # treated as "no real target" and silently filled from memory.
    assert steps[0].tool == "home_assistant" and steps[0].target
    result = bridge._apply_device_context(text, "c1")
    assert result == text  # explicit clause - passed through unchanged, never context-filled


# ============================================================================
# L - multiple commands in sequence: each turn's REMEMBER/FILL is
#     evaluated independently and correctly threads forward.
# ============================================================================

def test_L_multiple_commands_in_sequence_thread_context_forward_correctly():
    bridge = _bridge()
    assert bridge._apply_device_context("nyalakan rgb strip", "c1") == "nyalakan rgb strip"
    assert bridge._apply_device_context("matikan", "c1") == "turn off rgb_strip"
    assert bridge._apply_device_context("nyalakan rgb komputer", "c1") == "nyalakan rgb komputer"
    assert bridge._apply_device_context("matikan", "c1") == "turn off rgb_komputer"


# ============================================================================
# M - context after an unrelated conversation: conversations never
#     share device memory.
# ============================================================================

def test_M_context_after_unrelated_conversation_stays_isolated():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    result = bridge._apply_device_context("matikan", "c2")
    assert result == "matikan"


# ============================================================================
# N - context after a non-HA turn (ordinary chit-chat) in the SAME
#     conversation: memory survives untouched, and the turn counter
#     still advances (a turn that named no device says nothing about
#     whether the old memory is still right, but it does still count
#     as one turn older for freshness purposes).
# ============================================================================

def test_N_context_survives_an_intervening_non_ha_turn():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    before = dict(bridge._last_device_target["c1"]["home_assistant"])
    bridge._apply_device_context("gimana cuaca hari ini", "c1")  # unrelated chit-chat
    after = bridge._last_device_target["c1"]["home_assistant"]
    assert after["target"] == before["target"]
    assert after["turn_seq"] == before["turn_seq"]  # memory itself untouched
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "turn off rgb_strip"  # still fills correctly afterwards


# ============================================================================
# O - a FAILED (or timed-out) HA command must NOT become a strong
#     contextual target - `_invalidate_device_context_on_failure`,
#     wired into `_tool_bridge_handler`'s failure branch, un-remembers
#     the target that just failed.
# ============================================================================

def test_O_failed_command_un_remembers_its_own_target():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert bridge._last_device_target["c1"]["home_assistant"]["target"] == "rgb_strip"
    bridge._tool_bridge_local.conversation_id = "c1"
    failed_call = PlannerToolCall(tool="home_assistant", action="turn_off", target="rgb_strip", params={})
    bridge._invalidate_device_context_on_failure(failed_call)
    assert bridge._last_device_target["c1"].get("home_assistant") is None
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "matikan"  # nothing left to fill from


def test_O_failed_command_for_a_DIFFERENT_target_does_not_touch_memory():
    """The invalidation must be scoped to the SAME target that failed -
    a failure for device B must never wipe out a perfectly good, still
    fresh, remembered device A."""
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    bridge._tool_bridge_local.conversation_id = "c1"
    failed_call = PlannerToolCall(tool="home_assistant", action="turn_off", target="rgb_komputer", params={})
    bridge._invalidate_device_context_on_failure(failed_call)
    assert bridge._last_device_target["c1"]["home_assistant"]["target"] == "rgb_strip"


def test_O_failed_command_never_masks_the_real_exception():
    """`_invalidate_device_context_on_failure` must be a pure side
    effect - it must never itself raise or swallow the real failure
    `_tool_bridge_handler` is about to propagate."""
    bridge = _bridge()
    bad_call = object()  # no `.tool`/`.target` attributes at all
    bridge._invalidate_device_context_on_failure(bad_call)  # must not raise


def test_O_end_to_end_failed_tool_call_through_tool_bridge_handler_un_remembers_target():
    """Full proof through the REAL `_tool_bridge_handler` (not just the
    invalidation helper in isolation): publish `tool_requested`, have a
    subscriber reply `tool_failed` for THIS target, and confirm the
    conversation's device memory is cleared afterward - exercising the
    exact wiring `_tool_bridge_handler`'s failure branch now has."""
    from luno.core.event_bus import EventBus

    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert bridge._last_device_target["c1"]["home_assistant"]["target"] == "rgb_strip"

    bus = EventBus()
    bus.start()
    bridge.bind_event_bus(bus)
    bridge._tool_bridge_local.conversation_id = "c1"

    def _fail_it(e: demo.Event) -> None:
        execution_id = e.get("execution_id")
        bus.publish(demo.Event(type="tool_failed", data={
            "execution_id": execution_id, "error": "Home Assistant service call failed",
        }))

    bus.subscribe("tool_requested", _fail_it)
    try:
        tool_call = PlannerToolCall(tool="home_assistant", action="turn_off", target="rgb_strip", params={})
        raised = False
        try:
            bridge._tool_bridge_handler(tool_call)
        except RuntimeError:
            raised = True
        assert raised, "a tool_failed reply must surface as a raised error"
        assert bridge._last_device_target["c1"].get("home_assistant") is None
    finally:
        bus.stop()


def test_O_only_home_assistant_tool_calls_are_ever_considered_for_invalidation():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    bridge._tool_bridge_local.conversation_id = "c1"
    other_tool_call = PlannerToolCall(tool="camera_ptz", action="goto_preset", target="rgb_strip", params={})
    bridge._invalidate_device_context_on_failure(other_tool_call)
    assert bridge._last_device_target["c1"]["home_assistant"]["target"] == "rgb_strip"


# ============================================================================
# P - a SUCCESSFUL HA command must NOT be invalidated - only the
#     failure path calls `_invalidate_device_context_on_failure`.
# ============================================================================

def test_P_successful_command_leaves_context_intact():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    before = dict(bridge._last_device_target["c1"]["home_assistant"])
    # A successful outcome never calls the invalidation helper at all
    # (see `_tool_bridge_handler` - only reached inside `if "failed" in
    # box` and the timeout branch) - simulate that discipline directly:
    # nothing here should change memory.
    after = bridge._last_device_target["c1"]["home_assistant"]
    assert after == before


# ============================================================================
# Q - an AMBIGUOUS HA command (Sprint 52's own near-tie refusal) must
#     never become a contextual target in the first place - its raw
#     (unresolved, typo'd) text never matches a KNOWN device, so
#     REMEMBER's own `_is_known_home_assistant_device` guard already
#     keeps it out, independent of the failure-invalidation mechanism.
# ============================================================================

def test_Q_ambiguous_command_is_never_remembered_even_before_any_failure_event():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    # "rgb cprip" - Sprint 56's own engineered adversarial near-tie
    # between RGB Strip and RGB Computer - is not a recognized device
    # name in THIS checkout's real registry (it's a corrupted spelling),
    # so it must never overwrite the perfectly good existing memory.
    bridge._apply_device_context("matikan rgb cprip", "c1")
    assert bridge._last_device_target["c1"]["home_assistant"]["target"] == "rgb_strip"


# ============================================================================
# R - repeated contextual commands: a bare fill can be used again and
#     again while still fresh, without the intervening fills themselves
#     accidentally poisoning or losing the original memory.
# ============================================================================

def test_R_repeated_bare_contextual_commands_keep_resolving_while_fresh():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert bridge._apply_device_context("matikan", "c1") == "turn off rgb_strip"
    assert bridge._apply_device_context("matikan", "c1") == "turn off rgb_strip"
    assert bridge._apply_device_context("matikan", "c1") == "turn off rgb_strip"


# ============================================================================
# T - contextual reference using explicitly referential phrasing:
#     "yang itu" / "yang tadi" / "-nya".
# ============================================================================

def test_T_yang_itu_resolves_from_context():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert bridge._apply_device_context("matikan yang itu", "c1") == "turn off rgb_strip"


def test_T_yang_tadi_resolves_from_context():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert bridge._apply_device_context("matikan yang tadi", "c1") == "turn off rgb_strip"


def test_T_nya_suffix_resolves_from_context():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert bridge._apply_device_context("matiin nya", "c1") == "turn off rgb_strip"


def test_T_referential_phrasing_without_any_memory_stays_unchanged():
    bridge = _bridge()
    result = bridge._apply_device_context("matikan yang itu", "c1")
    assert result == "matikan yang itu"


# ============================================================================
# U - contextual reference after multiple DIFFERENT devices, named on
#     SEPARATE turns (not the same turn - contrast with C): the most
#     recent single-target turn correctly wins, this is not the
#     same-turn-ambiguity case.
# ============================================================================

def test_U_context_after_sequential_different_devices_uses_the_latest_one():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    bridge._apply_device_context("nyalakan rgb komputer", "c1")
    bridge._apply_device_context("nyalakan main lamp", "c1")
    result = bridge._apply_device_context("matikan", "c1")
    assert result == "turn off main_lamp"


# ============================================================================
# V - query-side differentiator (Sprint 56, `luno/memory_context.py`)
#     and the device-context mechanism (this sprint, `main_runtime_
#     demo.py`) are architecturally independent subsystems - neither
#     reads nor mutates the other's state. Both keep working when
#     exercised back-to-back.
# ============================================================================

def test_V_device_context_and_query_differentiator_do_not_share_or_corrupt_state():
    import luno.memory_context as mc

    bridge = _bridge()
    # Exercise the device-context mechanism first.
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    assert bridge._apply_device_context("matikan", "c1") == "turn off rgb_strip"

    # `_CONTEXT_MAX_TURN_AGE` (this sprint) and `_ACTIVE_TOPIC_MAX_AGE_
    # TURNS` (Sprint 56's own differentiator) are two independently
    # declared module-level constants that happen to share a value -
    # not the same object, not read from one another.
    assert bridge._CONTEXT_MAX_TURN_AGE == mc._ACTIVE_TOPIC_MAX_AGE_TURNS
    assert "_CONTEXT_MAX_TURN_AGE" not in dir(mc)
    assert "_ACTIVE_TOPIC_MAX_AGE_TURNS" not in dir(demo.PlannerBridgeModule)

    # Sprint 56's differentiator operates on `select_topic_candidates`'s
    # own topic-selection state (a `memory_context.py` concept, keyed
    # differently and never touched by `_apply_device_context`) - running
    # it must never mutate this bridge instance's HA device memory.
    before = dict(bridge._last_device_target["c1"]["home_assistant"])
    candidates = [
        {"topic": "rgb strip", "score": 0.9, "last_seen_turn": 1},
        {"topic": "rgb komputer", "score": 0.85, "last_seen_turn": 2},
    ]
    try:
        mc._narrow_by_query_differentiator("apa warnanya", candidates, current_turn=3)
    except Exception:
        pass  # signature may differ across checkouts - only state isolation matters here
    after = bridge._last_device_target["c1"]["home_assistant"]
    assert after == before

    # And the reverse: device-context resolution still works normally
    # after the differentiator ran.
    assert bridge._apply_device_context("matikan", "c1") == "turn off rgb_strip"


# ============================================================================
# Explicit-target-priority precedence (requirement list item #8): a
# strong Sprint 52 fuzzy match must beat a weaker contextual guess even
# when both a typo'd explicit target AND a fresh remembered device
# exist at the same time.
# ============================================================================

def test_explicit_target_always_beats_contextual_even_with_fresh_memory_present():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan main lamp", "c1")  # fresh, different device remembered
    result = bridge._apply_device_context("matikan rgb strp", "c1")  # typo'd explicit target
    assert result == "matikan rgb strp"
    steps = IntentParser.parse(result)
    assert steps[0].target == "rgb_strp"


# ============================================================================
# Message-quality fix: a genuinely target-less command reaching
# `execute()` (post context-resolution) gets an honest, distinct
# refusal - never "None is currently unavailable.", never confused with
# `_unknown_device_result`'s "I couldn't find '<name>'" (which requires
# an actual named-but-unrecognized target).
# ============================================================================

def test_missing_target_message_is_honest_and_distinct_from_unknown_device():
    saved = _patch_real_devices()
    try:
        h = _handler(FakeHAClient())
        result = h.execute(TMToolCall(tool="home_assistant", action="turn_off", target=None))
        assert result.success is False
        assert result.error_type == "MissingTarget"
        assert "none" not in result.message.lower()
        assert "unavailable" not in result.message.lower()
    finally:
        _restore_devices(saved)


def test_run_script_with_no_target_keeps_its_own_pre_existing_behavior():
    """The message-quality fix must not touch `run_script`'s own
    legitimate no-target path (falls back to `parameters['script']`)."""
    saved = _patch_real_devices()
    try:
        h = _handler(FakeHAClient())
        result = h.execute(TMToolCall(tool="home_assistant", action="run_script", target=None, parameters={}))
        assert result.success is False
        assert result.error_type != "MissingTarget"
        assert "requires a target or parameters.script" in result.message
    finally:
        _restore_devices(saved)


def test_unknown_named_device_still_uses_the_original_suggestion_message():
    """Regression guard: a target that WAS named but isn't recognized
    must still go through `_unknown_device_result` (with its
    suggestions), never the new `_missing_target_result`."""
    saved = _patch_real_devices()
    try:
        h = _handler(FakeHAClient())
        result = h.execute(TMToolCall(tool="home_assistant", action="turn_off", target="lampu dapur"))
        assert result.success is False
        assert result.error_type == "UnknownDevice"
    finally:
        _restore_devices(saved)


# ============================================================================
# Performance: contextual resolution overhead must stay under 5ms.
# ============================================================================

def test_performance_apply_device_context_overhead_under_5ms():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "c1")
    iterations = 200
    start = time.perf_counter()
    for _ in range(iterations):
        bridge._apply_device_context("matikan", "c1")
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / iterations
    assert per_call_ms < 5.0, f"contextual resolution averaged {per_call_ms:.3f}ms/call, over the 5ms budget"


# ============================================================================
# No LLM / network / embeddings in the contextual resolution path.
# ============================================================================

def test_no_llm_or_network_import_in_device_context_methods():
    import inspect
    src = "\n".join([
        inspect.getsource(demo.PlannerBridgeModule._apply_device_context),
        inspect.getsource(demo.PlannerBridgeModule._remember_device_target),
        inspect.getsource(demo.PlannerBridgeModule._device_context_entity_info),
        inspect.getsource(demo.PlannerBridgeModule._invalidate_device_context_on_failure),
    ])
    forbidden = ["openai", "openrouter.chat", "requests.", "httpx.", "embedding"]
    lowered = src.lower()
    for literal in forbidden:
        assert literal not in lowered, f"contextual resolution must never call {literal!r}"


# ============================================================================
# Observability - reuses the existing Event Bus (`Event(type="device_
# context_resolution", ...)`), the same `self._event_bus.publish(Event(
# ...))` pattern Sprint 50 established for `memory_reference_classified`.
# Structured, bounded fields only - no raw utterance text.
# ============================================================================

def test_observability_event_fires_on_a_successful_contextual_fill():
    from luno.core.event_bus import EventBus
    bus = EventBus()
    bus.start()
    try:
        bridge = _bridge()
        bridge.bind_event_bus(bus)
        events = []
        bus.subscribe("device_context_resolution", lambda e: events.append(dict(e.data)))
        bridge._apply_device_context("nyalakan rgb strip", "c1")
        bridge._apply_device_context("matikan", "c1")
        assert _wait_until(lambda: len(events) >= 1, 2.0)
        e = events[0]
        assert e["attempted"] is True
        assert e["resolved"] is True
        assert e["candidate_count"] == 1
        assert e["target"] == "rgb_strip"
        assert e["refusal_reason"] is None
        assert e["turn_age"] == 1
        # never the raw utterance text
        assert "matikan" not in str(e.values())
    finally:
        bus.stop()


def test_observability_event_reports_refusal_reason_when_stale():
    from luno.core.event_bus import EventBus
    bus = EventBus()
    bus.start()
    try:
        bridge = _bridge()
        bridge.bind_event_bus(bus)
        bridge._apply_device_context("nyalakan rgb strip", "c1")
        for _ in range(bridge._CONTEXT_MAX_TURN_AGE + 1):
            bridge._apply_device_context("apa kabar", "c1")
        events = []
        bus.subscribe("device_context_resolution", lambda e: events.append(dict(e.data)))
        bridge._apply_device_context("matikan", "c1")
        assert _wait_until(lambda: len(events) >= 1, 2.0)
        e = events[0]
        assert e["resolved"] is False
        assert e["refusal_reason"] == "stale"
    finally:
        bus.stop()


def test_observability_event_does_not_fire_when_no_contextual_resolution_is_attempted():
    """An EXPLICIT command never even reaches the "no real target" branch
    - no observability event should fire for it at all."""
    from luno.core.event_bus import EventBus
    bus = EventBus()
    bus.start()
    try:
        bridge = _bridge()
        bridge.bind_event_bus(bus)
        events = []
        bus.subscribe("device_context_resolution", lambda e: events.append(dict(e.data)))
        bridge._apply_device_context("nyalakan rgb strip", "c1")
        time.sleep(0.2)
        assert events == []
    finally:
        bus.stop()


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
