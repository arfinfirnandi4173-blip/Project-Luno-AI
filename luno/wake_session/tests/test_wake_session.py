"""
test_wake_session.py
=====================

Standalone unit tests for `luno.wake_session` - `matcher.py`,
`models.py`, `session.py`. No Event Bus, no threads beyond what
`ConversationSession` itself uses, no microphone. Mirrors the test-file
convention of every other package (`luno/planner/tests/`,
`luno/tool_manager/tests/`, etc).

Run:
    python3 -m luno.wake_session.tests.test_wake_session
"""

from __future__ import annotations

import time
import traceback
from typing import Callable, List, Tuple

from luno.wake_session.matcher import looks_like_interrupt_or_resume, match_wake_word, normalize
from luno.wake_session.models import ConversationState, WakeSessionConfig
from luno.wake_session.session import ConversationSession

SCENARIOS: List[Tuple[str, Callable[[], None]]] = []


def scenario(fn):
    SCENARIOS.append((fn.__name__, fn))
    return fn


# ============================================================================
# matcher.py
# ============================================================================

@scenario
def test_exact_wake_word_matches():
    m = match_wake_word("luno", ["luno", "hey luno"])
    assert m is not None and m.matched_phrase == "luno" and m.remainder == ""


@scenario
def test_wake_word_with_trailing_command_extracts_remainder():
    m = match_wake_word("Luno, open chrome", ["luno"])
    assert m is not None
    assert m.matched_phrase == "luno"
    assert m.remainder == "open chrome"


@scenario
def test_longest_phrase_preferred():
    m = match_wake_word("hey luno what time is it", ["luno", "hey luno"])
    assert m is not None
    assert m.matched_phrase == "hey luno"
    assert m.remainder == "what time is it"


@scenario
def test_false_positive_substring_rejected():
    assert match_wake_word("lunotics are fun", ["luno"]) is None
    assert match_wake_word("balunowhatever", ["luno"]) is None


@scenario
def test_unrelated_text_does_not_match():
    assert match_wake_word("what's the weather like", ["luno", "hey luno"]) is None


@scenario
def test_empty_text_or_words_never_matches():
    assert match_wake_word("", ["luno"]) is None
    assert match_wake_word("luno", []) is None
    assert match_wake_word("   ", ["luno"]) is None


@scenario
def test_normalize_is_case_and_punctuation_insensitive():
    assert normalize("LUNO!!") == normalize("luno")
    assert normalize("Hey,  Luno.") == normalize("hey luno")


# ============================================================================
# matcher.py / looks_like_interrupt_or_resume (bug fix)
# ============================================================================

@scenario
def test_looks_like_interrupt_or_resume_matches_interrupt_words():
    cfg = WakeSessionConfig()
    for phrase in ("stop", "please stop", "cancel", "hold on", "that's enough", "never mind"):
        assert looks_like_interrupt_or_resume(phrase, cfg.interrupt_words, cfg.resume_words), phrase


@scenario
def test_looks_like_interrupt_or_resume_matches_resume_words():
    cfg = WakeSessionConfig()
    assert looks_like_interrupt_or_resume("resume", cfg.interrupt_words, cfg.resume_words)
    assert looks_like_interrupt_or_resume("continue please", cfg.interrupt_words, cfg.resume_words)


@scenario
def test_looks_like_interrupt_or_resume_rejects_ordinary_speech():
    cfg = WakeSessionConfig()
    assert not looks_like_interrupt_or_resume("tell me about Unity", cfg.interrupt_words, cfg.resume_words)
    assert not looks_like_interrupt_or_resume("", cfg.interrupt_words, cfg.resume_words)


# ============================================================================
# models.py / WakeSessionConfig
# ============================================================================

@scenario
def test_config_defaults():
    cfg = WakeSessionConfig()
    assert cfg.wake_words == ["luno", "hey luno", "hi luno"]
    assert cfg.session_timeout_s == 15.0
    assert cfg.wake_acknowledgement == "Yes?"
    assert cfg.wake_confidence == 0.6
    assert cfg.sleep_enabled is True
    assert "stop" in cfg.interrupt_words and "cancel" in cfg.interrupt_words
    assert "resume" in cfg.resume_words and "continue" in cfg.resume_words


@scenario
def test_config_from_env_reads_barge_in_word_lists_as_fallback(monkeypatch=None):
    import os
    old = {k: os.environ.get(k) for k in (
        "WAKE_SESSION_INTERRUPT_WORDS", "WAKE_SESSION_RESUME_WORDS",
        "BARGE_IN_INTERRUPT_WORDS", "BARGE_IN_RESUME_WORDS",
    )}
    try:
        for k in old:
            os.environ.pop(k, None)
        os.environ["BARGE_IN_INTERRUPT_WORDS"] = "freeze, halt"
        os.environ["BARGE_IN_RESUME_WORDS"] = "go on"
        cfg = WakeSessionConfig.from_env()
        assert cfg.interrupt_words == ["freeze", "halt"]
        assert cfg.resume_words == ["go on"]

        # a wake-session-specific override takes priority over the shared one
        os.environ["WAKE_SESSION_INTERRUPT_WORDS"] = "only-mine"
        cfg2 = WakeSessionConfig.from_env()
        assert cfg2.interrupt_words == ["only-mine"]
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@scenario
def test_config_from_env(monkeypatch=None):
    import os
    old = {k: os.environ.get(k) for k in ("WAKE_WORDS", "SESSION_TIMEOUT", "WAKE_ACKNOWLEDGEMENT", "WAKE_CONFIDENCE", "SLEEP_ENABLED")}
    try:
        os.environ["WAKE_WORDS"] = "computer, hey computer"
        os.environ["SESSION_TIMEOUT"] = "20"
        os.environ["WAKE_ACKNOWLEDGEMENT"] = "I'm listening"
        os.environ["WAKE_CONFIDENCE"] = "0.8"
        os.environ["SLEEP_ENABLED"] = "false"
        cfg = WakeSessionConfig.from_env()
        assert cfg.wake_words == ["computer", "hey computer"]
        assert cfg.session_timeout_s == 20.0
        assert cfg.wake_acknowledgement == "I'm listening"
        assert cfg.wake_confidence == 0.8
        assert cfg.sleep_enabled is False
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ============================================================================
# session.py / ConversationSession state machine
# ============================================================================

@scenario
def test_starts_sleeping_when_enabled():
    s = ConversationSession(WakeSessionConfig(sleep_enabled=True))
    assert s.state == ConversationState.SLEEPING


@scenario
def test_starts_idle_when_disabled():
    s = ConversationSession(WakeSessionConfig(sleep_enabled=False))
    assert s.state == ConversationState.IDLE


@scenario
def test_full_transition_cycle():
    s = ConversationSession(WakeSessionConfig(session_timeout_s=10.0))
    assert s.transition_to(ConversationState.AWAKENING, "wake") is True
    assert s.transition_to(ConversationState.LISTENING, "ack done") is True
    assert s.transition_to(ConversationState.THINKING, "speech in") is True
    assert s.transition_to(ConversationState.SPEAKING, "reply ready") is True
    assert s.transition_to(ConversationState.WAITING_USER, "reply done") is True
    assert s.state == ConversationState.WAITING_USER


@scenario
def test_reentering_same_state_is_a_noop():
    s = ConversationSession()
    assert s.transition_to(ConversationState.SLEEPING, "still sleeping") is False
    assert len(s.history) == 0


@scenario
def test_timeout_only_runs_in_listening_and_waiting_user():
    s = ConversationSession(WakeSessionConfig(session_timeout_s=0.2))
    s.transition_to(ConversationState.AWAKENING, "x")
    assert s.seconds_remaining() is None
    assert not s.is_timed_out()

    s.transition_to(ConversationState.LISTENING, "x")
    assert s.seconds_remaining() is not None
    time.sleep(0.3)
    assert s.is_timed_out()


@scenario
def test_touch_resets_the_deadline():
    s = ConversationSession(WakeSessionConfig(session_timeout_s=0.3))
    s.transition_to(ConversationState.LISTENING, "x")
    time.sleep(0.2)
    s.touch()
    time.sleep(0.2)
    # 0.4s elapsed total, but touch() reset the clock at the 0.2s mark,
    # so only 0.2s has passed since - should NOT be timed out yet.
    assert not s.is_timed_out()
    time.sleep(0.2)
    assert s.is_timed_out()


@scenario
def test_speaking_state_never_extends_timeout_by_itself():
    """Spec: 'Speaking by Luno alone must not extend the session
    indefinitely.' SPEAKING has no timeout of its own (not in
    TIMEOUT_ACTIVE_STATES) - simulate a long SPEAKING stretch and confirm
    the deadline that was set entering WAITING_USER is exactly one fresh
    window, not compounded by how long SPEAKING took."""
    s = ConversationSession(WakeSessionConfig(session_timeout_s=0.3))
    s.transition_to(ConversationState.LISTENING, "x")
    s.transition_to(ConversationState.THINKING, "x")
    s.transition_to(ConversationState.SPEAKING, "x")
    time.sleep(0.5)  # SPEAKING drags on well past what the timeout would have been
    assert not s.is_timed_out()  # no timeout clock was running during SPEAKING
    s.transition_to(ConversationState.WAITING_USER, "x")
    remaining = s.seconds_remaining()
    assert remaining is not None and 0 < remaining <= 0.3 + 0.05


@scenario
def test_reconfigure_swaps_config_without_forcing_unrelated_transition():
    s = ConversationSession(WakeSessionConfig(sleep_enabled=True))
    s.transition_to(ConversationState.LISTENING, "awake")
    s.reconfigure(WakeSessionConfig(sleep_enabled=True, session_timeout_s=99.0))
    assert s.state == ConversationState.LISTENING  # untouched - not an enabled/disabled edge
    assert s.config.session_timeout_s == 99.0


@scenario
def test_reconfigure_edge_sleeping_to_disabled_moves_to_idle():
    s = ConversationSession(WakeSessionConfig(sleep_enabled=True))
    assert s.state == ConversationState.SLEEPING
    s.reconfigure(WakeSessionConfig(sleep_enabled=False))
    assert s.state == ConversationState.IDLE


@scenario
def test_reconfigure_edge_disabled_to_enabled_moves_to_sleeping():
    s = ConversationSession(WakeSessionConfig(sleep_enabled=False))
    assert s.state == ConversationState.IDLE
    s.reconfigure(WakeSessionConfig(sleep_enabled=True))
    assert s.state == ConversationState.SLEEPING


@scenario
def test_history_is_bounded_and_ordered():
    s = ConversationSession()
    states = [ConversationState.AWAKENING, ConversationState.LISTENING, ConversationState.THINKING,
              ConversationState.SPEAKING, ConversationState.WAITING_USER]
    for st in states:
        s.transition_to(st, "step")
    assert [t.to_state for t in s.history] == states
    assert s.previous_state == ConversationState.SPEAKING


def main() -> int:
    passed = 0
    failed = 0
    for name, fn in SCENARIOS:
        try:
            fn()
            print(f"  [PASS] {name}")
            passed += 1
        except AssertionError as ex:
            print(f"  [FAIL] {name}: {ex}")
            failed += 1
        except Exception as ex:  # pragma: no cover
            print(f"  [ERROR] {name}: {ex}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed}/{len(SCENARIOS)} scenarios passed.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
