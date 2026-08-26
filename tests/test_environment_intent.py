"""
test_environment_intent.py
=============================

Two things:

  1. `luno/environment_intent.py` in isolation - cue classification
     (hot/cold/dark/sleepy, ID+EN, including the false-positive guards),
     confirmation-reply classification (including the "ya"/"ok" bare-
     word fix - a real bug caught while building this: those words are
     common Indonesian sentence-final softeners, not reliable "yes"
     signals, unless they're the user's ENTIRE reply), and trigger
     config loading (missing file / malformed entries / list vs single
     device - fails closed like every other `luno.devices` loader).

  2. `PlannerBridgeModule._handle_environmental_intent()` (main_runtime_
     demo.py) - the two-turn "ask, then act" state machine: a cue
     proposes an action, the user's VERY NEXT utterance in the same
     conversation confirms/declines/is unrelated, and only a
     confirmation ever produces a real command for the caller to plan/
     execute.

Run:
    python3 -m pytest tests/test_environment_intent.py
"""

from __future__ import annotations

import json
import os
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

from luno.environment_intent import (  # noqa: E402
    EnvTrigger,
    build_confirmation_command,
    classify_confirmation_reply,
    classify_environmental_cue,
    load_environment_triggers,
)

import main_runtime_demo as demo  # noqa: E402


# ============================================================================
# classify_environmental_cue
# ============================================================================

@pytest.mark.parametrize("text,expected", [
    ("hawanya agak panas nih", "hot"),
    ("gerah banget di sini", "hot"),
    ("aduh kepanasan aku", "hot"),
    ("so hot in here", "hot"),
    ("dingin banget deh", "cold"),
    ("aku kedinginan", "cold"),
    ("freezing in here", "cold"),
    ("gelap nih ruangannya", "dark"),
    ("duh gelap banget", "dark"),
    ("can't see anything", "dark"),
    ("ngantuk banget aku", "sleepy"),
    ("mau tidur ah", "sleepy"),
    ("going to bed now", "sleepy"),
])
def test_classify_environmental_cue_positive_cases(text, expected):
    assert classify_environmental_cue(text) == expected


@pytest.mark.parametrize("text", [
    "apa kabar",
    "harga minyak lagi panas-panasnya",  # figurative, no personal marker
    "kenapa gelap terus dari kemarin ruangan itu",  # no marker word
    "nyalain lampu kamar",
    "",
])
def test_classify_environmental_cue_negative_cases(text):
    assert classify_environmental_cue(text) is None


def test_classify_environmental_cue_handles_none_gracefully():
    assert classify_environmental_cue(None) is None


# ============================================================================
# classify_confirmation_reply
# ============================================================================

@pytest.mark.parametrize("text", ["iya", "boleh dong", "ya udah deh", "yes please", "gas aja", "ya", "ok"])
def test_classify_confirmation_reply_affirmative(text):
    assert classify_confirmation_reply(text) is True


@pytest.mark.parametrize("text", ["nggak usah", "gak ah", "jangan deh", "no thanks", "tidak usah"])
def test_classify_confirmation_reply_negative(text):
    assert classify_confirmation_reply(text) is False


@pytest.mark.parametrize("text", ["nyalain lampu kamar", "hmm apa ya", "gitu ya?", "apa kabar", ""])
def test_classify_confirmation_reply_neither(text):
    """Regression guard for a real bug caught while building this: "ya"
    alone is a common Indonesian sentence-final softener ("hmm apa ya",
    "gitu ya?") - it must NOT count as affirmative unless it's the
    user's entire reply."""
    assert classify_confirmation_reply(text) is None


# ============================================================================
# load_environment_triggers / build_confirmation_command
# ============================================================================

def test_load_environment_triggers_missing_file_returns_empty(tmp_path):
    assert load_environment_triggers(str(tmp_path / "nope.json")) == {}


def test_load_environment_triggers_malformed_json_fails_closed(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load_environment_triggers(str(path)) == {}


def test_load_environment_triggers_skips_unknown_cue(tmp_path):
    path = tmp_path / "triggers.json"
    path.write_text(json.dumps({"not_a_real_cue": {"action": "turn_on", "device": "x"}}), encoding="utf-8")
    assert load_environment_triggers(str(path)) == {}


def test_load_environment_triggers_skips_bad_action(tmp_path):
    path = tmp_path / "triggers.json"
    path.write_text(json.dumps({"hot": {"action": "explode", "device": "AC"}}), encoding="utf-8")
    assert load_environment_triggers(str(path)) == {}


def test_load_environment_triggers_skips_missing_device(tmp_path):
    path = tmp_path / "triggers.json"
    path.write_text(json.dumps({"hot": {"action": "turn_on"}}), encoding="utf-8")
    assert load_environment_triggers(str(path)) == {}


def test_load_environment_triggers_single_device_string(tmp_path):
    path = tmp_path / "triggers.json"
    path.write_text(json.dumps({"dark": {"action": "turn_on", "device": "Main Lamp"}}), encoding="utf-8")
    triggers = load_environment_triggers(str(path))
    assert triggers["dark"].devices == ("Main Lamp",)


def test_load_environment_triggers_device_list(tmp_path):
    path = tmp_path / "triggers.json"
    path.write_text(json.dumps({"sleepy": {"action": "turn_off", "device": ["A", "B"]}}), encoding="utf-8")
    triggers = load_environment_triggers(str(path))
    assert triggers["sleepy"].devices == ("A", "B")


def test_load_environment_triggers_default_ask_question_generated(tmp_path):
    path = tmp_path / "triggers.json"
    path.write_text(json.dumps({"hot": {"action": "turn_on", "device": "AC"}}), encoding="utf-8")
    triggers = load_environment_triggers(str(path))
    assert "AC" in triggers["hot"].ask


def test_build_confirmation_command_single_device():
    trigger = EnvTrigger(action="turn_on", devices=("AC Kamar",), ask="?")
    assert build_confirmation_command(trigger) == "turn on AC Kamar"


def test_build_confirmation_command_multiple_devices():
    trigger = EnvTrigger(action="turn_off", devices=("Main Lamp", "RGB Strip"), ask="?")
    assert build_confirmation_command(trigger) == "turn off Main Lamp, turn off RGB Strip"


def test_build_confirmation_command_run_script():
    trigger = EnvTrigger(action="run_script", devices=("night mode",), ask="?")
    assert build_confirmation_command(trigger) == "run night mode"


# ============================================================================
# PlannerBridgeModule._handle_environmental_intent - the confirm-first
# state machine, tested against a REAL PlannerBridgeModule (no event bus
# needed - this method touches nothing but its own pending-confirmation
# dict and the pure functions above).
# ============================================================================

def _bridge() -> "demo.PlannerBridgeModule":
    return demo.PlannerBridgeModule()


def test_new_cue_returns_note_and_records_pending():
    bridge = _bridge()
    override, note = bridge._handle_environmental_intent("hawanya agak panas nih", "r1", "conv-1")
    assert override is None
    assert note is not None and "AC" in note
    assert "conv-1" in bridge._pending_env_confirmations
    assert bridge._pending_env_confirmations["conv-1"]["cue"] == "hot"


def test_confirming_pending_returns_canonical_command_and_clears_pending():
    bridge = _bridge()
    bridge._handle_environmental_intent("hawanya agak panas nih", "r1", "conv-1")
    override, note = bridge._handle_environmental_intent("iya boleh", "r2", "conv-1")
    assert override == "turn on AC Kamar"
    assert note is None
    assert "conv-1" not in bridge._pending_env_confirmations


def test_declining_pending_returns_note_and_clears_pending():
    bridge = _bridge()
    bridge._handle_environmental_intent("hawanya agak panas nih", "r1", "conv-1")
    override, note = bridge._handle_environmental_intent("nggak usah", "r2", "conv-1")
    assert override is None
    assert note is not None and "declin" in note.lower()
    assert "conv-1" not in bridge._pending_env_confirmations


def test_unrelated_reply_clears_pending_without_acting():
    bridge = _bridge()
    bridge._handle_environmental_intent("hawanya agak panas nih", "r1", "conv-1")
    override, note = bridge._handle_environmental_intent("gimana kabar kamu", "r2", "conv-1")
    assert override is None and note is None
    assert "conv-1" not in bridge._pending_env_confirmations


def test_confirmation_is_one_shot_second_affirmative_does_nothing():
    bridge = _bridge()
    bridge._handle_environmental_intent("hawanya agak panas nih", "r1", "conv-1")
    bridge._handle_environmental_intent("iya", "r2", "conv-1")
    # the pending entry was already consumed - a second "iya" with
    # nothing pending must not do anything (and "iya" alone doesn't
    # independently classify as any cue).
    override, note = bridge._handle_environmental_intent("iya", "r3", "conv-1")
    assert override is None and note is None


def test_expired_pending_is_treated_as_absent():
    bridge = _bridge()
    bridge._handle_environmental_intent("hawanya agak panas nih", "r1", "conv-1")
    bridge._pending_env_confirmations["conv-1"]["expires_at"] = time.time() - 1.0
    override, note = bridge._handle_environmental_intent("iya", "r2", "conv-1")
    # expired -> "iya" alone doesn't independently classify as any cue
    assert override is None and note is None
    assert "conv-1" not in bridge._pending_env_confirmations


def test_separate_conversations_do_not_share_pending_state():
    bridge = _bridge()
    bridge._handle_environmental_intent("hawanya agak panas nih", "r1", "conv-1")
    override, note = bridge._handle_environmental_intent("iya", "r2", "conv-2")
    # conv-2 never had a pending confirmation of its own
    assert override is None and note is None
    assert "conv-1" in bridge._pending_env_confirmations


def test_conversation_id_none_uses_sentinel_key_consistently():
    bridge = _bridge()
    bridge._handle_environmental_intent("gelap nih ruangannya", "r1", None)
    assert bridge._ENV_CONFIRMATION_KEY in bridge._pending_env_confirmations
    override, note = bridge._handle_environmental_intent("iya", "r2", None)
    assert override == "turn on Main Lamp"


def test_unconfigured_cue_stays_silent():
    bridge = _bridge()
    demo.ENV_TRIGGERS.pop("hot", None)
    try:
        override, note = bridge._handle_environmental_intent("hawanya agak panas nih", "r1", "conv-1")
        assert override is None and note is None
        assert "conv-1" not in bridge._pending_env_confirmations
    finally:
        from luno.environment_intent import reload_environment_triggers
        reload_environment_triggers()


def test_no_cue_no_pending_is_a_pure_noop():
    bridge = _bridge()
    override, note = bridge._handle_environmental_intent("apa kabar", "r1", "conv-1")
    assert override is None and note is None
    assert bridge._pending_env_confirmations == {}


def test_multi_device_sleepy_trigger_builds_multi_clause_command():
    bridge = _bridge()
    bridge._handle_environmental_intent("ngantuk banget aku", "r1", "conv-1")
    override, note = bridge._handle_environmental_intent("iya", "r2", "conv-1")
    assert override == "turn off Main Lamp, turn off RGB Strip, turn off RGB Computer"


def test_override_reparses_cleanly_through_intent_parser():
    """The whole point of `build_confirmation_command` reusing the
    canonical "turn on/off <device>" phrasing - it must actually
    re-parse into real, independent home_assistant ToolCalls."""
    from luno.planner.parser import IntentParser
    bridge = _bridge()
    bridge._handle_environmental_intent("ngantuk banget aku", "r1", "conv-1")
    override, _ = bridge._handle_environmental_intent("iya", "r2", "conv-1")
    steps = IntentParser.parse(override)
    assert [(s.tool, s.action, s.depends_on_previous) for s in steps] == [
        ("home_assistant", "turn_off", False),
        ("home_assistant", "turn_off", False),
        ("home_assistant", "turn_off", False),
    ]
