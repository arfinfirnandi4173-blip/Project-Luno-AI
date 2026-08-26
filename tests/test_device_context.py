"""
test_device_context.py
=========================

`PlannerBridgeModule._apply_device_context()` (main_runtime_demo.py) -
short-term device-context memory: "aktifkan lampu kamar" (turn on the
bedroom light) followed, on a LATER turn, by "sekarang matikan" (now
turn it off) with NO device named at all - understood as "matikan
lampu kamar" (turn off the SAME device), not a validation failure
demanding the user repeat the device name every time.

Tested against the project's real `config/lights.config.json`/
`switches.config.json` (via `luno.devices`) - "RGB Strip"/"Main Lamp"/
"Fish Light"/"Baterai" are real configured devices in this checkout;
"lampu dapur"/"lagi"/"aja" deliberately are not, used here to test the
"unregistered-but-real-looking name must fail honestly, never get
silently swapped" and "pure filler must not count as an explicit
target" distinctions respectively.

Run:
    python3 -m pytest tests/test_device_context.py
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main_runtime_demo as demo  # noqa: E402


def _bridge() -> "demo.PlannerBridgeModule":
    return demo.PlannerBridgeModule()


# ============================================================================
# core "remember, then fill" behavior
# ============================================================================

def test_explicit_device_is_remembered_unchanged():
    bridge = _bridge()
    result = bridge._apply_device_context("nyalakan rgb strip", "conv-1")
    assert result == "nyalakan rgb strip"  # unchanged - nothing to fill
    remembered = bridge._last_device_target["conv-1"]["home_assistant"]
    assert remembered["target"] == "rgb_strip"


def test_missing_target_is_filled_from_memory():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "conv-1")
    result = bridge._apply_device_context("sekarang matikan", "conv-1")
    assert result == "turn off rgb_strip"


def test_filled_command_reparses_to_a_real_home_assistant_call():
    from luno.planner.parser import IntentParser
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "conv-1")
    filled = bridge._apply_device_context("sekarang matikan", "conv-1")
    steps = IntentParser.parse(filled)
    assert len(steps) == 1
    assert (steps[0].tool, steps[0].action, steps[0].target) == ("home_assistant", "turn_off", "rgb_strip")


def test_turn_on_after_turn_off_also_uses_remembered_device():
    from luno import devices
    switch_name = next(iter(devices.SWITCHES))  # any real configured switch
    bridge = _bridge()
    bridge._apply_device_context(f"matikan {switch_name}", "conv-1")
    result = bridge._apply_device_context("nyalain lagi", "conv-1")
    from luno.planner.parser import _slugify
    assert result == f"turn on {_slugify(switch_name)}"


def test_no_memory_yet_leaves_text_unchanged():
    bridge = _bridge()
    result = bridge._apply_device_context("sekarang matikan", "conv-1")
    assert result == "sekarang matikan"


# ============================================================================
# filler-word handling (the real edge case caught while building this)
# ============================================================================

def test_filler_word_target_is_treated_as_no_target():
    """"nyalakan lagi"/"matiin aja" slugify to a NON-EMPTY target
    ("lagi"/"aja") via the parser's own "capture everything after the
    verb" shape - these must still count as "no real device named",
    not as an (unresolvable) explicit target."""
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "conv-1")
    assert bridge._apply_device_context("nyalakan lagi", "conv-1") == "turn on rgb_strip"
    assert bridge._apply_device_context("matiin aja", "conv-1") == "turn off rgb_strip"


def test_filler_word_alone_never_poisons_memory():
    """A filler-only utterance must never itself become the "remembered
    device" for a LATER turn."""
    bridge = _bridge()
    bridge._apply_device_context("nyalakan lagi", "conv-1")  # no memory yet, stays unchanged
    assert bridge._last_device_target.get("conv-1", {}) == {}


def test_unregistered_looking_device_name_is_never_silently_swapped():
    """CRITICAL safety guard: "lampu dapur" ("kitchen light") is not a
    configured device in this checkout, but it LOOKS like a genuine
    device name, not filler - it must be left alone (so the real
    handler fails honestly with "device not found"), never silently
    replaced with a different remembered device. Silently acting on the
    wrong device would be strictly worse than an honest failure."""
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "conv-1")
    result = bridge._apply_device_context("nyalakan lampu dapur", "conv-1")
    assert result == "nyalakan lampu dapur"  # unchanged, NOT "turn on rgb_strip"


def test_unregistered_device_name_does_not_overwrite_memory():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "conv-1")
    bridge._apply_device_context("nyalakan lampu dapur", "conv-1")
    remembered = bridge._last_device_target["conv-1"]["home_assistant"]
    assert remembered["target"] == "rgb_strip"


# ============================================================================
# isolation / scoping
# ============================================================================

def test_separate_conversations_have_separate_device_memory():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "conv-1")
    result = bridge._apply_device_context("sekarang matikan", "conv-2")
    assert result == "sekarang matikan"  # conv-2 has no memory of its own


def test_conversation_id_none_uses_sentinel_key_consistently():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", None)
    result = bridge._apply_device_context("sekarang matikan", None)
    assert result == "turn off rgb_strip"


def test_multi_clause_utterance_is_never_rewritten():
    """Deliberately conservative - only a SINGLE-clause utterance with a
    missing/filler target gets context-filled."""
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "conv-1")
    text = "matikan, lalu nyalakan lagi"
    result = bridge._apply_device_context(text, "conv-1")
    assert result == text


def test_camera_ptz_target_never_used_as_home_assistant_context():
    """Each tool gets its own memory slot - a camera preset name must
    never leak into a light command's context."""
    bridge = _bridge()
    bridge._apply_device_context("arahkan kamera ke pintu", "conv-1")
    result = bridge._apply_device_context("sekarang matikan", "conv-1")
    assert result == "sekarang matikan"  # no home_assistant memory recorded


def test_conversation_ended_resets_device_context():
    bridge = _bridge()
    bridge._apply_device_context("nyalakan rgb strip", "conv-1")
    assert "conv-1" in bridge._last_device_target
    bridge._on_conversation_ended(demo.Event(type="conversation_ended", data={"session_id": "conv-1", "reason": "timeout"}))
    assert "conv-1" not in bridge._last_device_target
    result = bridge._apply_device_context("sekarang matikan", "conv-1")
    assert result == "sekarang matikan"  # brand new conversation - no memory left


def test_conversation_ended_also_clears_pending_env_confirmation():
    bridge = _bridge()
    bridge._handle_environmental_intent("hawanya agak panas nih", "r1", "conv-1")
    assert "conv-1" in bridge._pending_env_confirmations
    bridge._on_conversation_ended(demo.Event(type="conversation_ended", data={"session_id": "conv-1", "reason": "timeout"}))
    assert "conv-1" not in bridge._pending_env_confirmations


# ============================================================================
# end-to-end through the real event bus (mirrors test_runtime_demo.py's
# own full-turn style)
# ============================================================================

# ============================================================================
# camera_ptz follow-up context ("sekarang arahkan ke komputer" right
# after "arahkan kamera ke tengah" - bug report: no "kamera" word means
# `_classify_camera_ptz`'s hard camera-word gate never even considers
# the clause, so it fell all the way through to "unknown" and the LLM
# freely improvised ("help me operate the PC") instead of moving the
# camera.
# ============================================================================

def test_camera_follow_up_without_camera_word_is_filled():
    bridge = _bridge()
    bridge._apply_device_context("arahkan kamera ke tengah", "conv-1")
    result = bridge._apply_device_context("sekarang arahkan ke komputer", "conv-1")
    assert result == "point the camera at komputer"


def test_camera_follow_up_reparses_to_a_real_camera_ptz_call():
    from luno.planner.parser import IntentParser
    bridge = _bridge()
    bridge._apply_device_context("arahkan kamera ke tengah", "conv-1")
    filled = bridge._apply_device_context("sekarang arahkan ke komputer", "conv-1")
    steps = IntentParser.parse(filled)
    assert len(steps) == 1
    assert (steps[0].tool, steps[0].action, steps[0].target) == ("camera_ptz", "goto_preset", "komputer")


def test_camera_follow_up_direction_word_still_resolves_to_pan_not_preset():
    bridge = _bridge()
    bridge._apply_device_context("arahkan kamera ke tengah", "conv-1")
    filled = bridge._apply_device_context("geser ke kiri", "conv-1")
    from luno.planner.parser import IntentParser
    steps = IntentParser.parse(filled)
    assert (steps[0].tool, steps[0].action) == ("camera_ptz", "pan_left")


def test_camera_follow_up_not_filled_without_prior_camera_command():
    """No camera_ptz memory yet this conversation - "arahkan ke komputer"
    stays "unknown", never guessed at."""
    bridge = _bridge()
    result = bridge._apply_device_context("arahkan ke komputer", "conv-1")
    assert result == "arahkan ke komputer"


def test_camera_follow_up_not_filled_when_camera_word_already_present():
    """A clause that already names "kamera" explicitly doesn't need (and
    must not go through) the follow-up fill path - it already parses to
    camera_ptz on its own."""
    bridge = _bridge()
    bridge._apply_device_context("arahkan kamera ke tengah", "conv-1")
    result = bridge._apply_device_context("arahkan kamera ke pintu", "conv-1")
    assert result == "arahkan kamera ke pintu"


def test_camera_follow_up_does_not_leak_into_home_assistant_memory():
    bridge = _bridge()
    bridge._apply_device_context("arahkan kamera ke tengah", "conv-1")
    result = bridge._apply_device_context("sekarang matikan", "conv-1")
    assert result == "sekarang matikan"  # no home_assistant device remembered


def test_end_to_end_context_fill_through_full_console():
    import time
    from luno.adapters import MockOpenRouterClient

    client = MockOpenRouterClient(canned_text="Oke!", chunk_delay_s=0.0)
    console = demo.RuntimeDemoConsole(openrouter_client=client)
    console.start()
    try:
        tool_events = []
        console.event_bus.subscribe("tool_finished", lambda e: tool_events.append(e))

        def run(text, rid):
            tool_events.clear()
            console.event_bus.publish(demo.Event(type="user_utterance", data={
                "text": text, "request_id": rid, "conversation_id": "conv-e2e",
            }))
            deadline = time.time() + 5
            while time.time() < deadline and not tool_events:
                time.sleep(0.02)
            return list(tool_events)

        first = run("nyalakan rgb strip", "r1")
        assert len(first) == 1 and first[0].data["success"] is True

        second = run("sekarang matikan", "r2")
        assert len(second) == 1
        assert second[0].data["tool"] == "home_assistant"
        assert second[0].data["action"] == "turn_off"
        assert second[0].data["success"] is True
    finally:
        console.stop()
