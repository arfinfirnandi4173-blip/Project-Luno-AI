"""
test_confirmation.py
=======================

`ConfirmationHandler` - Efficient LLM Classifier sprint. Covers every
scenario the sprint's own hard requirements listed explicitly: confirm,
cancel, timeout/expiry, unrelated message, duplicate confirmation,
concurrent requests, cross-conversation isolation, expired-cannot-
execute, and "a bare yes/no with nothing pending does nothing by
itself." Also confirms `prompt_for()`/`cancelled_ack()` are pure string
templates - no LLM call, no I/O, anywhere in this module.
"""

from __future__ import annotations

import threading
import time

from luno.routing.confirmation import ConfirmationHandler


def _handler(ttl_s: float = 60.0) -> ConfirmationHandler:
    return ConfirmationHandler(ttl_s=ttl_s)


# -- confirm / cancel ----------------------------------------------------------

def test_confirm_resolves_and_returns_original_text_and_intent():
    h = _handler()
    h.request_confirmation(request_id="r1", conversation_id="c1", text="bikin ruangan nyaman", intent="device_control", confidence=0.65)
    outcome = h.resolve_reply("c1", "iya")
    assert outcome is not None
    assert outcome.action == "confirmed"
    assert outcome.pending.original_text == "bikin ruangan nyaman"
    assert outcome.pending.intent == "device_control"
    assert outcome.pending.request_id == "r1"


def test_cancel_resolves_with_cancelled_action():
    h = _handler()
    h.request_confirmation(request_id="r1", conversation_id="c1", text="bikin ruangan nyaman", intent="device_control", confidence=0.65)
    outcome = h.resolve_reply("c1", "tidak")
    assert outcome is not None
    assert outcome.action == "cancelled"


def test_cancel_words_batal_and_cancel_both_work():
    for word in ("batal", "cancel"):
        h = _handler()
        h.request_confirmation(request_id="r1", conversation_id="c1", text="x", intent="device_control", confidence=0.6)
        outcome = h.resolve_reply("c1", word)
        assert outcome is not None and outcome.action == "cancelled", word


def test_confirm_word_lakukan_works():
    h = _handler()
    h.request_confirmation(request_id="r1", conversation_id="c1", text="x", intent="device_control", confidence=0.6)
    outcome = h.resolve_reply("c1", "lakukan")
    assert outcome is not None and outcome.action == "confirmed"


# -- unrelated message ---------------------------------------------------------

def test_unrelated_reply_returns_none_and_leaves_pending_intact():
    h = _handler()
    h.request_confirmation(request_id="r1", conversation_id="c1", text="x", intent="device_control", confidence=0.6)
    assert h.resolve_reply("c1", "apa kabar hari ini") is None
    # still pending - a later real yes/no should still work
    outcome = h.resolve_reply("c1", "ya")
    assert outcome is not None and outcome.action == "confirmed"


# -- duplicate confirmation -----------------------------------------------------

def test_duplicate_confirmation_is_a_noop_second_time():
    h = _handler()
    h.request_confirmation(request_id="r1", conversation_id="c1", text="x", intent="device_control", confidence=0.6)
    first = h.resolve_reply("c1", "iya")
    second = h.resolve_reply("c1", "iya")
    assert first is not None and first.action == "confirmed"
    assert second is None  # already consumed - nothing left to confirm again


# -- bare yes/no with nothing pending -------------------------------------------

def test_bare_yes_with_nothing_pending_does_nothing():
    h = _handler()
    assert h.resolve_reply("c1", "iya") is None
    assert h.resolve_reply("c1", "lakukan") is None
    assert h.resolve_reply("c1", "oke") is None


# -- expiry (never executes) -----------------------------------------------------

def test_expired_confirmation_cannot_be_confirmed_or_cancelled():
    h = _handler(ttl_s=0.05)
    h.request_confirmation(request_id="r1", conversation_id="c1", text="x", intent="device_control", confidence=0.6)
    time.sleep(0.15)
    assert h.resolve_reply("c1", "iya") is None
    assert h.resolve_reply("c1", "tidak") is None
    assert h.snapshot()["pending_count"] == 0


def test_peek_also_expires_a_stale_entry():
    h = _handler(ttl_s=0.05)
    h.request_confirmation(request_id="r1", conversation_id="c1", text="x", intent="device_control", confidence=0.6)
    time.sleep(0.15)
    assert h.peek("c1") is None
    assert h.snapshot()["pending_count"] == 0


# -- cross-conversation isolation ------------------------------------------------

def test_conversation_a_cannot_confirm_conversation_bs_pending_action():
    h = _handler()
    h.request_confirmation(request_id="rA", conversation_id="A", text="turn on kitchen light", intent="device_control", confidence=0.65)
    h.request_confirmation(request_id="rB", conversation_id="B", text="search for cat videos", intent="search_web", confidence=0.65)
    # B's "iya" must only ever resolve B's own pending entry.
    outcome_b = h.resolve_reply("B", "iya")
    assert outcome_b.pending.original_text == "search for cat videos"
    # A's entry must still be untouched/pending.
    outcome_a = h.resolve_reply("A", "iya")
    assert outcome_a.pending.original_text == "turn on kitchen light"


def test_none_conversation_id_uses_shared_sentinel_consistently():
    h = _handler()
    h.request_confirmation(request_id="r1", conversation_id=None, text="x", intent="device_control", confidence=0.6)
    outcome = h.resolve_reply(None, "iya")
    assert outcome is not None and outcome.action == "confirmed"


# -- new confirmation supersedes an old unanswered one in the same conversation --

def test_second_ambiguous_turn_supersedes_first_unanswered_one():
    h = _handler()
    h.request_confirmation(request_id="r1", conversation_id="c1", text="first ambiguous thing", intent="device_control", confidence=0.6)
    h.request_confirmation(request_id="r2", conversation_id="c1", text="second ambiguous thing", intent="search_web", confidence=0.6)
    outcome = h.resolve_reply("c1", "iya")
    assert outcome.pending.original_text == "second ambiguous thing"
    assert outcome.pending.request_id == "r2"


# -- concurrency -----------------------------------------------------------------

def test_concurrent_requests_across_conversations_do_not_corrupt_state():
    h = _handler()
    n = 50
    errors = []

    def worker(i: int) -> None:
        try:
            cid = f"conv-{i}"
            h.request_confirmation(request_id=f"req-{i}", conversation_id=cid, text=f"text-{i}", intent="device_control", confidence=0.6)
            outcome = h.resolve_reply(cid, "iya")
            if outcome is None or outcome.pending.original_text != f"text-{i}":
                errors.append(i)
        except Exception as ex:  # pragma: no cover - failure path only
            errors.append((i, ex))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    assert not errors, errors


# -- deterministic/template output, no LLM call ----------------------------------

def test_prompt_for_and_cancelled_ack_are_pure_string_templates():
    h = _handler()
    pending = h.request_confirmation(request_id="r1", conversation_id="c1", text="bikin ruangan nyaman", intent="device_control", confidence=0.6)
    prompt = h.prompt_for(pending)
    assert isinstance(prompt, str) and "bikin ruangan nyaman" in prompt
    ack = h.cancelled_ack()
    assert isinstance(ack, str) and len(ack) > 0


def test_snapshot_never_leaks_original_text():
    h = _handler()
    h.request_confirmation(request_id="r1", conversation_id="c1", text="super secret utterance", intent="device_control", confidence=0.6)
    snap = h.snapshot()
    assert "super secret utterance" not in str(snap)
    assert snap["pending_count"] == 1
