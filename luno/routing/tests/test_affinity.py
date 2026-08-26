"""
test_affinity.py
===================

`ConversationAffinityTracker` - spec's "Conversation Affinity": stay on
the reasoning provider mid-session, release back to default once the
topic genuinely shifts to routine chat.
"""

from __future__ import annotations

from luno.routing.affinity import ConversationAffinityTracker
from luno.routing.config import RoutingConfig
from luno.routing.models import ComplexityLevel, Intent


def _cfg(**overrides):
    base = dict(default_provider_alias="deepseek", reasoning_provider_alias="gpt", enable_provider_affinity=True)
    base.update(overrides)
    return RoutingConfig(**base)


def test_reasoning_choice_marks_conversation_sticky():
    tracker = ConversationAffinityTracker(_cfg())
    alias, applied = tracker.apply("conv-1", "gpt", [Intent.CODING], ComplexityLevel.HIGH, [])
    assert alias == "gpt"
    assert applied is False  # genuinely chosen this turn, not "applied via stickiness"
    assert tracker.snapshot() == {"conv-1": "gpt"}


def test_subsequent_reasoning_turn_stays_sticky():
    tracker = ConversationAffinityTracker(_cfg())
    tracker.apply("conv-1", "gpt", [Intent.CODING], ComplexityLevel.HIGH, [])
    # this turn's OWN classification would have picked "deepseek" (routine
    # question), but it's still reasoning-flavored complexity - stays on gpt
    alias, applied = tracker.apply("conv-1", "deepseek", [Intent.GENERAL_QUESTION], ComplexityLevel.HIGH, [])
    assert alias == "gpt"
    assert applied is True


def test_casual_chat_releases_affinity():
    tracker = ConversationAffinityTracker(_cfg())
    tracker.apply("conv-1", "gpt", [Intent.CODING], ComplexityLevel.HIGH, [])
    alias, applied = tracker.apply("conv-1", "deepseek", [Intent.GENERAL_CHAT], ComplexityLevel.LOW, [])
    assert alias == "deepseek"
    assert applied is False
    assert tracker.snapshot() == {}


def test_next_turn_after_release_uses_default_again():
    tracker = ConversationAffinityTracker(_cfg())
    tracker.apply("conv-1", "gpt", [Intent.CODING], ComplexityLevel.HIGH, [])
    tracker.apply("conv-1", "deepseek", [Intent.GENERAL_CHAT], ComplexityLevel.LOW, [])
    alias, applied = tracker.apply("conv-1", "deepseek", [Intent.GENERAL_CHAT], ComplexityLevel.LOW, [])
    assert alias == "deepseek"
    assert applied is False


def test_disabled_affinity_never_overrides():
    tracker = ConversationAffinityTracker(_cfg(enable_provider_affinity=False))
    tracker.apply("conv-1", "gpt", [Intent.CODING], ComplexityLevel.HIGH, [])
    alias, applied = tracker.apply("conv-1", "deepseek", [Intent.GENERAL_QUESTION], ComplexityLevel.HIGH, [])
    assert alias == "deepseek"
    assert applied is False


def test_no_conversation_id_never_sticky():
    tracker = ConversationAffinityTracker(_cfg())
    tracker.apply(None, "gpt", [Intent.CODING], ComplexityLevel.HIGH, [])
    alias, applied = tracker.apply(None, "deepseek", [Intent.GENERAL_QUESTION], ComplexityLevel.HIGH, [])
    assert alias == "deepseek"
    assert applied is False


def test_reset_clears_conversation():
    tracker = ConversationAffinityTracker(_cfg())
    tracker.apply("conv-1", "gpt", [Intent.CODING], ComplexityLevel.HIGH, [])
    tracker.reset("conv-1")
    assert tracker.snapshot() == {}
    alias, applied = tracker.apply("conv-1", "deepseek", [Intent.GENERAL_QUESTION], ComplexityLevel.HIGH, [])
    assert applied is False


def test_reset_unknown_conversation_never_raises():
    tracker = ConversationAffinityTracker(_cfg())
    tracker.reset("never-existed")
    tracker.reset(None)


def test_concurrent_conversations_are_isolated():
    tracker = ConversationAffinityTracker(_cfg())
    tracker.apply("conv-a", "gpt", [Intent.CODING], ComplexityLevel.HIGH, [])
    # conv-b never went reasoning - must independently get "deepseek",
    # never inherit conv-a's stickiness.
    alias_b, applied_b = tracker.apply("conv-b", "deepseek", [Intent.GENERAL_CHAT], ComplexityLevel.LOW, [])
    assert alias_b == "deepseek"
    assert applied_b is False

    alias_a, applied_a = tracker.apply("conv-a", "deepseek", [Intent.GENERAL_QUESTION], ComplexityLevel.HIGH, [])
    assert alias_a == "gpt"
    assert applied_a is True


def test_concurrent_threads_do_not_corrupt_state():
    import threading

    tracker = ConversationAffinityTracker(_cfg())
    errors = []

    def worker(i):
        try:
            cid = f"conv-{i % 5}"
            for _ in range(50):
                tracker.apply(cid, "gpt", [Intent.CODING], ComplexityLevel.HIGH, [])
                tracker.apply(cid, "deepseek", [Intent.GENERAL_QUESTION], ComplexityLevel.HIGH, [])
        except Exception as ex:  # pragma: no cover
            errors.append(ex)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors
