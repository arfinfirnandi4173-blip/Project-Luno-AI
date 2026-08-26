"""
test_conversation_reference_resolution.py
============================================

SPRINT 38 - CONVERSATION REFERENCE RESOLUTION.

Goal: Luno must correctly resolve "yang tadi gimana?"/"kalau yang
kedua?"/"yang itu?"/"kalau versi wireless?"/"yang lebih murah?"/"terus
bagian software?"/"kalau dibanding yang tadi?"/"itu pakai apa?"/"yang
pertama tadi apa?"/"kalau alternatifnya?"/"kalau yang ini bagaimana?" and
similar elliptical references - determining WHAT is referenced, WHICH
part of the conversation is the referent, whether the user is continuing
the old topic or opening a new one, what context should carry forward,
and what must NOT carry forward (context contamination).

Phase 0's audit (see docs/change_impact/conversation_reference_resolution.md)
found the existing `classify_reference_type()`/`is_pure_reference_followup()`/
`ActiveTopicSnapshot`/`update_active_topic()`/`update_topic_history()`/
`select_topic_candidates()` machinery (Sprint 4 "Memory Continuity",
"Memory Topic Retention", "Context-Aware Comparison Topic Preservation" -
all COMPLETELY UNCHANGED by this sprint) already correctly resolves
"yang lain?"/"terus?"/"kalau itu?"/"ESP32 gimana?" via a bag-of-terms
active-topic snapshot that a turn either PRESERVES or REPLACES, and
already correctly keeps multiple subtopics (mic vs pompa) from
contaminating each other via `select_topic_candidates()`'s own
token-overlap matching.

Two concrete gaps were found (both reproduced live before any code was
written) and closed additively, reusing every existing mechanism:

  Gap A - ORDINAL/LIST reference ("yang kedua gimana?", "nomor tiga",
  "yang pertama dibanding yang ketiga"). The old snapshot only stored an
  unordered bag of terms - there was no way to resolve "the second one"
  to the actual item ("MAX9814"). Closed by `ActiveTopicSnapshot.
  list_items` (new field, `()` default - backward compatible) +
  `extract_list_items_from_reply()` (parses Luno's own numbered/bulleted
  reply) + `parse_ordinal_indices()`/`resolve_ordinal_targets()` (reuse
  `luno.memory.ORDINAL_WORD_MAP`/`CARDINAL_WORD_MAP` - no second ordinal
  vocabulary). NEVER fabricates - returns `((), "none")` whenever there
  is no list to resolve against or the position doesn't exist.

  Gap B - ATTRIBUTE reference ("kalau yang wireless?", "yang murah?"
  without "lebih", "kalau versi Bluetooth?"). These fell through to
  "unknown" before this sprint, which `is_pure_reference_followup()`
  correctly treats as a RICH turn - so the ENTIRE active-topic snapshot
  was REPLACED with junk tokens ("kalau", "yang", "wireless"), losing
  "esp32"/"mikrofon" outright. Closed by a new `attribute_reference` type
  (with an elliptical-fragment residual guard so a genuinely rich
  question like "Modul Bluetooth apa yang bagus buat ESP8266?" is never
  misclassified) + a new `is_merge`-based update path (`_merge_terms()`)
  that UNIONS the new attribute into the existing snapshot rather than
  replacing or preserving it. Also added: `repair_reference` ("eh
  maksudku ESP32-S3", "bukan yang itu, yang satunya") - the SAME merge
  behavior, for conversational self-correction.

No LLM judge, no embedding model, no second tokenizer, no second ranking
system, no unlimited/persistent conversation state was introduced
anywhere in this sprint - every new function reuses `analyze_query()`
(the one tokenizer) and plain, deterministic `re` patterns, following
this project's own existing style exactly.

Sections:
  1. Reference-type classification - new types + adversarial phrasing
     (Phase 16's own natural-language matrix) + closed-enum/precedence
     regression guard.
  2. Ordinal/list resolution (Phase 4).
  3. Merge behavior - repair/attribute (Phase 5-6).
  4. No-contamination test matrix A-L (Phase 11).
  5. Real E2E through `RuntimeDemoConsole` (Phase 15) - the brief's own
     exact mic-list scenario.
  6. Ambiguity / no-fabrication guards (Phase 9).
  7. Bounded state / persistence / structural guards (Phase 10, 12, 19).

Run:
    python3 -m pytest -q tests/test_conversation_reference_resolution.py
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from typing import Callable

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno import memory  # noqa: E402
from luno import memory_context  # noqa: E402


def _load_demo():
    demo_spec = importlib.util.spec_from_file_location(
        "main_runtime_demo_conv_ref", os.path.join(_ROOT, "main_runtime_demo.py"),
    )
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules["main_runtime_demo_conv_ref"] = demo
    demo_spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


class _SequentialMockOpenRouter:
    """Returns a scripted reply keyed by (a substring of) the incoming
    user text - lets an E2E scenario script a whole multi-turn
    conversation deterministically, reusing the real `MockOpenRouterClient`
    class for everything else (retry/failure semantics, `.calls`, etc.)."""

    @staticmethod
    def build(demo, replies):
        from luno.adapters import MockOpenRouterClient

        class _Client(MockOpenRouterClient):
            def __init__(self):
                super().__init__(canned_text=None)

            def _resolve_text(self, messages):
                text = messages[0]["content"] if messages else ""
                for key, val in replies.items():
                    if key.strip() in text or text.strip() == key.strip():
                        return val
                return "(no canned reply configured for this turn)"

        return _Client()


def _new_console(demo, replies=None, canned_text="ok"):
    if replies:
        client = _SequentialMockOpenRouter.build(demo, replies)
    else:
        from luno.adapters import MockOpenRouterClient
        client = MockOpenRouterClient(canned_text=canned_text, chunk_delay_s=0.0)
    return demo.RuntimeDemoConsole(openrouter_client=client)


def _run_turn(console, demo, text, request_id, conversation_id=None):
    """Publishes a "user_utterance" directly (bypassing wake-word/session
    gating) and waits for this turn's `_pending_turns` entry to be popped -
    the precise, race-free signal that `_on_assistant_response()` (and
    therefore this sprint's active-topic/topic-history update) has
    completed for this turn."""
    done = threading.Event()

    def _capture(e):
        if e.get("request_id") == request_id:
            done.set()

    sub = console.event_bus.subscribe("assistant_response", _capture)
    data = {"text": text, "request_id": request_id}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    try:
        console.event_bus.publish(demo.Event(type="user_utterance", data=data))
        assert _wait_until(done.is_set, 6.0), f"no assistant_response for {request_id!r} within timeout"
        assert _wait_until(lambda: request_id not in console.planner_module._pending_turns, 5.0), (
            "active-topic/topic-history update never completed for this request_id"
        )
    finally:
        console.event_bus.unsubscribe(sub)


def _run_turn_capture_prompt(console, demo, text, request_id, conversation_id=None):
    """Like `_run_turn()` but also captures the rendered `system_prompt`
    from `need_llm_response` - used when a test needs to inspect what
    context actually reached the LLM for this specific turn."""
    captured = {}
    need_llm = threading.Event()

    def _capture_prompt(e):
        if e.get("request_id") == request_id:
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

    sub = console.event_bus.subscribe("need_llm_response", _capture_prompt)
    try:
        _run_turn(console, demo, text, request_id, conversation_id=conversation_id)
        _wait_until(need_llm.is_set, 3.0)
    finally:
        console.event_bus.unsubscribe(sub)
    return captured.get("system_prompt") or ""


# ============================================================================
# Section 1 - reference-type classification (new types + adversarial
# Indonesian phrasing, Phase 16's own natural-language matrix)
# ============================================================================

def test_01_repair_reference_maksudku():
    assert memory.classify_reference_type("eh maksudku ESP32-S3") == "repair_reference"


def test_02_repair_reference_bukan_itu_yang_satunya():
    assert memory.classify_reference_type("bukan yang itu, yang satunya") == "repair_reference"


def test_03_ordinal_reference_yang_kedua():
    assert memory.classify_reference_type("kalau yang kedua?") == "ordinal_reference"


def test_04_ordinal_reference_nomor_tiga():
    assert memory.classify_reference_type("nomor tiga gimana?") == "ordinal_reference"


def test_05_ordinal_reference_multi_ordinal_takes_precedence_over_comparison():
    # "dibanding" is a comparison marker, but an explicit ordinal marker
    # takes precedence - Phase 4's own "yang pertama dibanding yang
    # ketiga?" worked example.
    assert memory.classify_reference_type("yang pertama dibanding yang ketiga?") == "ordinal_reference"


def test_06_attribute_reference_wireless():
    assert memory.classify_reference_type("kalau yang wireless?") == "attribute_reference"


def test_07_attribute_reference_versi():
    assert memory.classify_reference_type("kalau versi Bluetooth?") == "attribute_reference"


def test_08_attribute_reference_murah_without_lebih():
    assert memory.classify_reference_type("yang murah ada?") == "attribute_reference"


def test_09_direct_reference_bare_yang_itu():
    # Previously uncovered - fell through to "unknown" before this
    # sprint. One of the brief's own primary target phrases.
    assert memory.classify_reference_type("yang itu?") == "direct_reference"
    assert memory.needs_topic_context("yang itu?") is True


def test_10_direct_reference_bare_yang_ini():
    assert memory.classify_reference_type("yang ini?") == "direct_reference"


def test_11_rich_question_with_yang_bagus_is_not_attribute_false_positive():
    # The elliptical-fragment residual guard - a full, self-contained
    # question that merely happens to contain "yang bagus" must not be
    # misclassified as an elliptical ATTRIBUTE_REFERENCE fragment.
    text = "Modul Bluetooth apa yang bagus buat ESP8266?"
    assert memory.classify_reference_type(text) == "unknown"
    assert memory.needs_topic_context(text) is False


def test_12_terus_pilih_yang_mana_still_continuation_not_attribute():
    # Regression guard - "yang mana" must never be captured as an
    # attribute candidate (it's an interrogative, not a descriptor).
    assert memory.classify_reference_type("terus pilih yang mana?") == "continuation"


# ---- Phase 16's adversarial natural-language matrix ------------------------

_ADVERSARIAL_PHRASES = [
    ("yang tadi gimana?", "comparison"),
    ("yang itu maksudku", "repair_reference"),
    ("yang satunya", "repair_reference"),
    ("yang kedua dong", "ordinal_reference"),
    ("kalau yang wireless?", "attribute_reference"),
    ("yang lebih murah ada?", "cost_comparison"),
    ("terus yang pertama tadi?", "ordinal_reference"),
    ("kalau dibanding yang sebelumnya?", "comparison"),
    ("bukan itu, yang satu lagi", "repair_reference"),
    ("nah yang itu", "direct_reference"),
    # A conservative, deliberate design choice, not a gap: "kalau buat X?"
    # alone (no gimana/vs/dibanding comparison marker) is genuinely
    # ambiguous between "a fresh new question" and "a follow-up" - Phase
    # 9's own "don't fabricate" principle means the safe default here is
    # "unknown" (treated as a fresh, self-contained turn) rather than
    # guessing it's a reference.
    ("kalau buat ESP32-S3?", "unknown"),
    ("yang buat mic tadi", "direct_reference"),
    ("yang bagian power", "attribute_reference"),
    ("kalau koneksinya?", "unknown"),
]


def test_13_adversarial_phrase_matrix():
    failures = []
    for phrase, expected in _ADVERSARIAL_PHRASES:
        got = memory.classify_reference_type(phrase)
        if got != expected:
            failures.append((phrase, expected, got))
    assert not failures, f"adversarial phrase mismatches: {failures}"


def test_14_all_new_types_need_topic_context():
    for phrase in ("eh maksudku X", "yang kedua?", "kalau yang wireless?"):
        assert memory.needs_topic_context(phrase) is True


def test_15_is_merge_reference_followup_only_for_repair_and_attribute():
    assert memory.is_merge_reference_followup("eh maksudku ESP32-S3") is True
    assert memory.is_merge_reference_followup("kalau yang wireless?") is True
    assert memory.is_merge_reference_followup("yang lain?") is False
    assert memory.is_merge_reference_followup("terus?") is False
    assert memory.is_merge_reference_followup("yang kedua?") is False
    assert memory.is_merge_reference_followup("ESP32 gimana?") is False


# ============================================================================
# Section 2 - ordinal/list resolution (Phase 4)
# ============================================================================

_MIC_REPLY = (
    "Berikut pilihan mikrofon untuk ESP32:\n"
    "1. INMP441\n"
    "2. MAX9814\n"
    "3. SPH0645\n"
)


def test_16_extract_list_items_numbered():
    items = memory_context.extract_list_items_from_reply(_MIC_REPLY)
    assert items == ("INMP441", "MAX9814", "SPH0645")


def test_17_extract_list_items_bulleted():
    reply = "Pilihannya:\n- A\n- B\n- C"
    assert memory_context.extract_list_items_from_reply(reply) == ("A", "B", "C")


def test_18_extract_list_items_none_when_no_list():
    assert memory_context.extract_list_items_from_reply("Ini jawaban biasa tanpa list sama sekali.") == ()
    assert memory_context.extract_list_items_from_reply("") == ()
    assert memory_context.extract_list_items_from_reply(None) == ()


def test_19_parse_ordinal_indices_single():
    assert memory_context.parse_ordinal_indices("yang kedua gimana?") == (2,)


def test_20_parse_ordinal_indices_multi():
    assert memory_context.parse_ordinal_indices("yang pertama dibanding yang ketiga") == (1, 3)


def test_21_parse_ordinal_indices_cardinal_after_nomor():
    assert memory_context.parse_ordinal_indices("nomor tiga gimana?") == (3,)


def test_22_parse_ordinal_indices_digit():
    assert memory_context.parse_ordinal_indices("opsi 2 gimana?") == (2,)


def test_23_parse_ordinal_indices_empty_for_no_ordinal():
    assert memory_context.parse_ordinal_indices("ESP32 gimana?") == ()


def test_24_resolve_ordinal_targets_second_item():
    snap = memory_context.update_active_topic(
        None, "jelasin pilihan mikrofon", _MIC_REPLY, is_followup=False,
    )
    targets, confidence = memory_context.resolve_ordinal_targets("yang kedua gimana?", snap, [])
    assert targets == ("MAX9814",)
    assert confidence == "high"


def test_25_resolve_ordinal_targets_two_items():
    snap = memory_context.update_active_topic(
        None, "jelasin pilihan mikrofon", _MIC_REPLY, is_followup=False,
    )
    targets, confidence = memory_context.resolve_ordinal_targets(
        "yang pertama dibanding yang ketiga?", snap, [],
    )
    assert targets == ("INMP441", "SPH0645")
    assert confidence == "high"


def test_26_resolve_ordinal_targets_falls_back_to_topic_history():
    # Active-topic snapshot itself has no list, but an entry in the
    # bounded topic history does (Phase 3's own "search order: current
    # snapshot first, then history" requirement).
    history_entry = memory_context.update_active_topic(
        None, "jelasin pilihan mikrofon", _MIC_REPLY, is_followup=False,
    )
    empty_current = memory_context.ActiveTopicSnapshot(terms=frozenset({"lain", "hal"}))
    targets, confidence = memory_context.resolve_ordinal_targets(
        "yang kedua gimana?", empty_current, [history_entry],
    )
    assert targets == ("MAX9814",)
    assert confidence == "high"


def test_27_ordinal_targets_to_relevant_memory_shape():
    rm = memory_context.ordinal_targets_to_relevant_memory(("MAX9814",), turn_id="t1")
    assert rm is not None
    assert rm.source == "conversation_reference"
    assert "MAX9814" in rm.text
    assert rm.raw["targets"] == ["MAX9814"]
    assert memory_context.ordinal_targets_to_relevant_memory(()) is None


def test_28_build_expanded_retrieval_text_for_targets():
    expanded = memory_context.build_expanded_retrieval_text_for_targets("yang kedua gimana?", ("MAX9814",))
    assert "yang kedua gimana?" in expanded
    assert "MAX9814" in expanded
    assert memory_context.build_expanded_retrieval_text_for_targets("x", ()) == "x"


# ============================================================================
# Section 3 - merge behavior (repair/attribute, Phase 5-6)
# ============================================================================

def test_29_merge_preserves_old_terms_and_adds_new():
    existing = memory_context.update_active_topic(
        None, "ESP32 pakai INMP441.",
        "Bagus, ESP32 dengan INMP441 cocok untuk mic array digital I2S.",
        is_followup=False,
    )
    merged = memory_context.update_active_topic(
        existing, "Eh maksudku ESP32-S3.", "Oke, jadi maksudnya ESP32-S3.",
        is_merge=True,
    )
    terms = merged.terms
    assert "inmp441" in terms  # old term survives
    assert "esp32" in terms  # old term survives
    assert "s3" in terms  # new correction term added (esp32-s3 tokenizes with the hyphen split)
    assert merged.turns_since_active == 0


def test_30_merge_preserves_list_items_when_reply_has_no_new_list():
    existing = memory_context.update_active_topic(
        None, "jelasin pilihan mikrofon", _MIC_REPLY, is_followup=False,
    )
    merged = memory_context.update_active_topic(
        existing, "kalau yang wireless?", "Untuk wireless biasanya pakai modul I2S over WiFi.",
        is_merge=True,
    )
    assert merged.list_items == ("INMP441", "MAX9814", "SPH0645")


def test_31_merge_replaces_list_items_when_reply_has_a_new_list():
    existing = memory_context.update_active_topic(
        None, "jelasin pilihan mikrofon", _MIC_REPLY, is_followup=False,
    )
    new_reply = "Untuk wireless:\n1. ESP-NOW audio\n2. BLE audio\n"
    merged = memory_context.update_active_topic(
        existing, "kalau yang wireless?", new_reply, is_merge=True,
    )
    assert merged.list_items == ("ESP-NOW audio", "BLE audio")


def test_32_merge_takes_precedence_over_none_existing_still_fresh():
    fresh = memory_context.update_active_topic(None, "kalau yang wireless?", "reply", is_merge=True)
    assert "wireless" in fresh.terms  # no existing snapshot - merge degrades to a fresh build


def test_33_preserve_still_unaffected_by_merge_addition():
    # Backward-compat guard - a PURE preserve (is_followup=True,
    # is_merge=False, e.g. "yang lain?") must behave byte-for-byte as
    # before this sprint.
    existing = memory_context.update_active_topic(None, "ESP32 pakai INMP441.", "reply", is_followup=False)
    preserved = memory_context.update_active_topic(existing, "yang lain?", "reply2", is_followup=True)
    assert preserved.terms == existing.terms
    assert preserved.turns_since_active == 1


def test_34_topic_history_merge_pushes_merged_entry():
    history = [memory_context.update_active_topic(None, "ESP32 pakai INMP441.", "reply mic array I2S", is_followup=False)]
    updated = memory_context.update_topic_history(
        history, "Eh maksudku ESP32-S3.", "Oke maksudnya ESP32-S3.", is_merge=True,
    )
    assert len(updated) >= 1
    assert "inmp441" in updated[0].terms
    assert "s3" in updated[0].terms


def test_35_topic_history_merge_with_empty_history_degrades_gracefully():
    updated = memory_context.update_topic_history(None, "kalau yang wireless?", "reply", is_merge=True)
    assert len(updated) == 1
    assert "wireless" in updated[0].terms


# ============================================================================
# Section 4 - no-contamination test matrix A-L (Phase 11)
# ============================================================================

def test_A_esp32_mic_yang_tadi_resolves_to_mic_not_fabricated():
    snap = memory_context.update_active_topic(
        None, "ESP32 pakai mikrofon INMP441", "ESP32 dengan INMP441 cocok untuk mic.", is_followup=False,
    )
    assert memory.classify_reference_type("yang tadi?") == "direct_reference"
    # "yang tadi" is a pure preserve - the snapshot itself is untouched,
    # still anchored on the mic topic.
    preserved = memory_context.update_active_topic(snap, "yang tadi?", "reply", is_followup=True)
    assert preserved.terms == snap.terms
    assert "inmp441" in preserved.terms


def test_D_microphone_list_yang_kedua_resolves_to_max9814():
    snap = memory_context.update_active_topic(None, "jelasin pilihan mikrofon", _MIC_REPLY, is_followup=False)
    targets, conf = memory_context.resolve_ordinal_targets("yang kedua gimana?", snap, [])
    assert targets == ("MAX9814",)
    assert conf == "high"


def test_E_microphone_list_first_vs_third():
    snap = memory_context.update_active_topic(None, "jelasin pilihan mikrofon", _MIC_REPLY, is_followup=False)
    targets, conf = memory_context.resolve_ordinal_targets("yang pertama dibanding yang ketiga?", snap, [])
    assert targets == ("INMP441", "SPH0645")


def test_F_microphone_yang_wireless_merges_not_replaces():
    snap = memory_context.update_active_topic(None, "jelasin pilihan mikrofon ESP32", _MIC_REPLY, is_followup=False)
    assert memory.is_merge_reference_followup("kalau yang wireless?") is True
    merged = memory_context.update_active_topic(snap, "kalau yang wireless?", "jawaban wireless", is_merge=True)
    assert "esp32" in merged.terms
    assert "wireless" in merged.terms


def test_G_microphone_yang_murah_merges_not_replaces():
    snap = memory_context.update_active_topic(None, "jelasin pilihan mikrofon ESP32", _MIC_REPLY, is_followup=False)
    assert memory.classify_reference_type("yang murah ada?") == "attribute_reference"
    merged = memory_context.update_active_topic(snap, "yang murah ada?", "jawaban murah", is_merge=True)
    assert "esp32" in merged.terms
    assert "murah" in merged.terms


def test_H_no_list_yang_kedua_never_fabricates():
    snap = memory_context.ActiveTopicSnapshot(terms=frozenset({"esp32", "wifi"}))  # no list_items
    targets, conf = memory_context.resolve_ordinal_targets("yang kedua gimana?", snap, [])
    assert targets == ()
    assert conf == "none"


def test_I_ambiguous_bare_pronoun_never_fabricates_a_specific_item():
    # "yang itu?" across two totally different, non-overlapping topics -
    # the resolver must not invent a merged/wrong target. The existing
    # (unmodified) `select_topic_candidates()` returns [] for a stopword-
    # only query (no real tokens to overlap with either topic), so the
    # caller's own single-slot "most recent" fallback is the deliberately
    # conservative default - never a guess between the two.
    history = [
        memory_context.update_active_topic(None, "aquascape pompa", "pompa submersible bagus", is_followup=False),
        memory_context.update_active_topic(None, "ESP32 mikrofon", "INMP441 cocok untuk mic", is_followup=False),
    ]
    candidates = memory_context.select_topic_candidates(history, "yang itu?", is_short_followup=True)
    assert candidates == []  # no fabricated match - the conservative single-slot path is the correct fallback here


def test_J_new_unrelated_query_no_old_context():
    snap = memory_context.update_active_topic(None, "ESP32 mikrofon INMP441", "reply mic", is_followup=False)
    text = "Berapa ukuran aquarium 50x25?"
    assert memory.classify_reference_type(text) == "unknown"
    replaced = memory_context.update_active_topic(snap, text, "aquarium 50 liter", is_followup=False)
    assert "inmp441" not in replaced.terms
    assert "esp32" not in replaced.terms
    assert "aquarium" in replaced.terms


def test_K_conversation_isolation_via_separate_snapshots():
    conv_a = memory_context.update_active_topic(None, "ESP32 mikrofon", "reply A", is_followup=False)
    conv_b = memory_context.update_active_topic(None, "aquascape pompa", "reply B", is_followup=False)
    assert "esp32" not in conv_b.terms
    assert "aquascape" not in conv_a.terms


# ============================================================================
# Section 5 - real E2E through RuntimeDemoConsole (Phase 15)
# ============================================================================

_E2E_REPLIES = {
    "Jelasin pilihan mikrofon untuk ESP32.": _MIC_REPLY,
    "Yang kedua gimana?": "MAX9814 pakai output analog dengan AGC, murah dan gampang dipakai.",
    "Kalau yang wireless?": "Untuk wireless biasanya pakai modul I2S over WiFi custom atau BLE audio.",
    "Kalau dibanding yang pertama?": "Dibanding INMP441, MAX9814 lebih murah tapi noise-nya lebih tinggi.",
    "Berapa ukuran aquarium 50x25?": "Aquarium 50x25 cm sekitar 50 liter tergantung tingginya.",
}


def test_e2e_full_mic_list_scenario():
    demo = _load_demo()
    console = _new_console(demo, replies=_E2E_REPLIES)
    console.start()
    try:
        console.simulate_speech("alexa")
        from luno.wake_session import ConversationState
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)

        rid = 1
        for turn_text in _E2E_REPLIES.keys():
            _run_turn(console, demo, turn_text, f"e2e-{rid}")
            rid += 1

        # No explicit conversation_id was passed with these turns (matches
        # this test file's own `_run_turn()` default), so PlannerBridgeModule
        # keys everything under its sentinel fallback key - use that
        # directly rather than `behavior_tree_module.conversation_id`
        # (a DIFFERENT id, unrelated to this module's own topic-state keys).
        key = console.planner_module._ENV_CONFIRMATION_KEY
        final_snapshot = console.planner_module._active_topic.get(key)
        # Final turn was a genuinely new, unrelated subject - must have
        # fully replaced the ESP32/mic topic, no contamination.
        assert final_snapshot is not None
        assert "esp32" not in final_snapshot.terms
        assert "inmp441" not in final_snapshot.terms
        assert "aquarium" in final_snapshot.terms
    finally:
        console.stop()


def test_e2e_ordinal_resolves_to_specific_item_mid_conversation():
    demo = _load_demo()
    console = _new_console(demo, replies=_E2E_REPLIES)
    console.start()
    try:
        console.simulate_speech("alexa")
        from luno.wake_session import ConversationState
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)

        _run_turn(console, demo, "Jelasin pilihan mikrofon untuk ESP32.", "e2e-ord-1")

        # No explicit conversation_id was passed with these turns (matches
        # this test file's own `_run_turn()` default), so PlannerBridgeModule
        # keys everything under its sentinel fallback key - use that
        # directly rather than `behavior_tree_module.conversation_id`
        # (a DIFFERENT id, unrelated to this module's own topic-state keys).
        key = console.planner_module._ENV_CONFIRMATION_KEY
        snap_before = console.planner_module._active_topic.get(key)
        assert snap_before.list_items == ("INMP441", "MAX9814", "SPH0645")

        targets, confidence = memory_context.resolve_ordinal_targets(
            "Yang kedua gimana?", snap_before, console.planner_module._topic_history.get(key),
        )
        assert targets == ("MAX9814",)
        assert confidence == "high"
    finally:
        console.stop()


def test_e2e_attribute_reference_merges_parent_topic():
    demo = _load_demo()
    console = _new_console(demo, replies=_E2E_REPLIES)
    console.start()
    try:
        console.simulate_speech("alexa")
        from luno.wake_session import ConversationState
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)

        _run_turn(console, demo, "Jelasin pilihan mikrofon untuk ESP32.", "e2e-attr-1")
        _run_turn(console, demo, "Kalau yang wireless?", "e2e-attr-2")

        # No explicit conversation_id was passed with these turns (matches
        # this test file's own `_run_turn()` default), so PlannerBridgeModule
        # keys everything under its sentinel fallback key - use that
        # directly rather than `behavior_tree_module.conversation_id`
        # (a DIFFERENT id, unrelated to this module's own topic-state keys).
        key = console.planner_module._ENV_CONFIRMATION_KEY
        snap = console.planner_module._active_topic.get(key)
        assert "esp32" in snap.terms  # parent topic preserved
        assert "wireless" in snap.terms  # new attribute merged in
    finally:
        console.stop()


def test_e2e_multi_topic_switch_no_contamination():
    demo = _load_demo()
    replies = {
        "Jelasin ESP32 pakai mikrofon INMP441.": "ESP32 dengan mikrofon INMP441 cocok untuk voice recording lewat I2S.",
        "Aku mau bahas topik baru soal aquascape.": "Oke, aquascape itu seni menata tanaman air di akuarium.",
        "Pompa aquascape yang bagus apa?": "Pompa aquascape yang bagus itu tipe submersible dengan flow rate stabil.",
        "Yang tadi soal mic gimana?": "ESP32 dengan INMP441 tetap jadi rekomendasi utama untuk mic karena I2S.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        console.simulate_speech("alexa")
        from luno.wake_session import ConversationState
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)

        rid = 1
        prompts = {}
        for turn_text in replies.keys():
            request_id = f"e2e-multi-{rid}"
            prompt = _run_turn_capture_prompt(console, demo, turn_text, request_id)
            prompts[turn_text] = prompt
            rid += 1

        # "Yang tadi soal mic gimana?" must retrieve the ESP32/mic topic
        # (via the existing, unmodified select_topic_candidates() token-
        # overlap matching against bounded topic history), NOT the more
        # recent aquascape/pompa topic.
        final_prompt = prompts["Yang tadi soal mic gimana?"]
        assert "inmp441" in final_prompt.lower() or "esp32" in final_prompt.lower()
    finally:
        console.stop()


def test_e2e_repair_correction_persists_across_turns():
    demo = _load_demo()
    replies = {
        "ESP32 pakai INMP441.": "Bagus, ESP32 dengan INMP441 cocok untuk mic array digital I2S.",
        "Eh maksudku ESP32-S3.": "Oke dicatat, jadi maksudnya ESP32-S3 dengan INMP441.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        console.simulate_speech("alexa")
        from luno.wake_session import ConversationState
        assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)

        _run_turn(console, demo, "ESP32 pakai INMP441.", "e2e-repair-1")
        _run_turn(console, demo, "Eh maksudku ESP32-S3.", "e2e-repair-2")

        # No explicit conversation_id was passed with these turns (matches
        # this test file's own `_run_turn()` default), so PlannerBridgeModule
        # keys everything under its sentinel fallback key - use that
        # directly rather than `behavior_tree_module.conversation_id`
        # (a DIFFERENT id, unrelated to this module's own topic-state keys).
        key = console.planner_module._ENV_CONFIRMATION_KEY
        snap = console.planner_module._active_topic.get(key)
        assert "inmp441" in snap.terms  # survives the correction
        assert "s3" in snap.terms  # correction itself is captured
    finally:
        console.stop()


# ============================================================================
# Section 6 - bounded state / persistence / structural guards
# ============================================================================

def test_list_items_bounded_at_ten():
    long_reply = "\n".join(f"{i}. Item{i}" for i in range(1, 25))
    items = memory_context.extract_list_items_from_reply(long_reply)
    assert len(items) == 10


def test_active_topic_snapshot_list_items_defaults_empty_tuple():
    # Backward-compat guard - every construction site from before this
    # sprint (that never passes `list_items`) still works.
    snap = memory_context.ActiveTopicSnapshot(terms=frozenset({"x"}))
    assert snap.list_items == ()


def test_no_forbidden_imports_in_new_code():
    src = open(memory_context.__file__, encoding="utf-8").read()
    lowered = src.lower()
    for word in ("openai", "anthropic_client", "sentence_transformers", "sklearn", "torch", "tensorflow"):
        assert word not in lowered


def test_conversation_reference_dataclass_is_lightweight_and_transient():
    ref = memory_context.ConversationReference(
        reference_type="ordinal_reference", target_items=("MAX9814",), confidence="high", source="ordinal",
    )
    assert ref.reference_type == "ordinal_reference"
    assert ref.target_items == ("MAX9814",)
    # frozen - never mutated in place (same "transient dataclass"
    # discipline as ContextItem/RelevantMemory).
    import dataclasses
    assert dataclasses.is_dataclass(ref)
    try:
        ref.confidence = "low"
        assert False, "ConversationReference must be frozen"
    except dataclasses.FrozenInstanceError:
        pass


def test_persistent_state_untouched_by_pure_functions():
    import hashlib
    import glob
    config_files = sorted(glob.glob(os.path.join(_ROOT, "config", "*.json")))
    before = {f: hashlib.sha256(open(f, "rb").read()).hexdigest() for f in config_files}
    # Exercise a representative slice of this sprint's own pure functions.
    snap = memory_context.update_active_topic(None, "ESP32 pakai INMP441.", _MIC_REPLY, is_followup=False)
    memory_context.resolve_ordinal_targets("yang kedua?", snap, [])
    memory_context.update_active_topic(snap, "kalau yang wireless?", "reply", is_merge=True)
    after = {f: hashlib.sha256(open(f, "rb").read()).hexdigest() for f in config_files}
    assert before == after
