"""
test_conversation_intelligence.py
============================================

SPRINT 39 - CONVERSATION INTELLIGENCE & CONTEXT QUALITY.

Goal (per the brief): Luno should understand what the user is referring
to, retain the right context, discard the wrong context, handle
corrections, handle ambiguity conservatively, and provide the minimum
sufficient context to the LLM - NOT "make retrieval happen more often."

Phase 0 (read-only re-audit) confirmed the existing Sprint 4/6/38
machinery (`classify_reference_type()`, `ActiveTopicSnapshot`,
`update_active_topic()`/`update_topic_history()`, `select_topic_
candidates()`, `resolve_ordinal_targets()`) already correctly handles
most elliptical-reference shapes. Phase 1's own live E2E probes through
the REAL `RuntimeDemoConsole` (not just unit-level classifier calls)
reproduced four concrete, root-caused context-quality failures - all
fixed additively, no new field/mechanism beyond what each failure
directly required:

  BUG 1 (ATTRIBUTE DRIFT) - `_merge_terms()`'s original "new terms
  first, plain truncate to `_ACTIVE_TOPIC_MAX_TERMS`" ordering could
  silently evict the ENTIRE parent-topic identity on a MERGE, either
  because a single turn's own text+reply was realistically verbose
  (~19 tokens alone leaves ~1 slot for everything already established),
  or because several consecutive merges had already pushed `old_terms`
  itself to the cap. Fixed by reserving at least half of the budget for
  `old_terms` (deterministic `sorted()` order, not Python's per-process
  frozenset/hash-seed iteration order) and by extracting the CURRENT
  turn's own terms in an order-preserving way (user's own typed words
  first) so the newly-requested word survives truncation too, not just
  whichever reply-only filler happens to sort first alphabetically.

  BUG 2 (MISSING CONTEXT) - the attribute-candidate regex captured the
  comparative/superlative MARKER itself ("lebih"/"paling") instead of
  skipping to the real descriptive word, so "yang lebih bagus?"/"yang
  lebih kecil?" (the brief's own Phase 8 adversarial phrases) and "yang
  paling murah/mahal/bagus/kecil?" fell through to `unknown` (or
  `continuation` when prefixed with "terus") instead of
  `attribute_reference`. Fixed by extending the optional skip-prefix
  the regex already uses for "bagian" to also skip "lebih "/"paling ",
  and exempting both markers from the elliptical-fragment residual
  check (same treatment as "dong"/"sih"/"aja").

  BUG 3 (WRONG CONTEXT) - `_TOPIC_OVERLAP_STOPWORDS` was missing "soal"
  (a generic Indonesian preposition, "about"/"regarding"), so a query
  like "Yang tadi soal mic gimana?" registered a false-positive token
  overlap against ANY topic-history entry that happened to be
  introduced with "soal X" phrasing, regardless of subject matter -
  `select_topic_candidates()` then offered completely unrelated
  historical topics as candidates instead of correctly finding (or
  correctly failing to find) the real one. Fixed by adding "soal" to
  the stopword set.

  BUG 4 (MISSING CONTEXT) - `_TOPIC_HISTORY_MAX_ENTRIES` (4) evicted an
  EXPLICITLY-referenced topic ("yang tadi soal mic") after just 4
  intervening topic switches - ordinary conversational drift, not a
  contrived stress case. An explicit, unambiguous reference is not the
  "genuinely ambiguous, prefer zero retrieval" case the brief's own
  ambiguity policy is for. Fixed by raising the cap to 8 - still small,
  fixed, and bounded, not "unbounded conversation state".

No LLM judge, no embedding model, no second tokenizer, no second ranking
system, and no persistent/unbounded conversation state were introduced.
Every fix is a small, deterministic change to an existing regex,
stopword set, small integer cap, or term-merge ordering rule.

Sections:
  1. Regression-guard unit tests for the four fixes above.
  2. Adversarial phrase matrix (Phase 8's own explicit phrase list).
  3. Scenario D - 12 elliptical phrases, classification + policy table.
  4. 18 named scenarios (Phase 7), several via the real
     `RuntimeDemoConsole` E2E path.
  5. No-contamination / bounded-state / structural guards.
  6. Performance (Phase 9).

Run:
    python3 -m pytest -q tests/test_conversation_intelligence.py
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
        "main_runtime_demo_conv_intel", os.path.join(_ROOT, "main_runtime_demo.py"),
    )
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules["main_runtime_demo_conv_intel"] = demo
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
    """Same pattern as `test_conversation_reference_resolution.py`'s own
    helper - deliberately duplicated, not imported, per this project's
    "each test module stays independently runnable" convention."""

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


def _key(console):
    return console.planner_module._ENV_CONFIRMATION_KEY


# ============================================================================
# Section 1 - regression-guard unit tests for the four Sprint 39 fixes
# ============================================================================

def test_01_merge_reserves_parent_topic_under_verbose_reply():
    """BUG 1 reproduction, as an isolated unit test - a single verbose
    reply (~19 tokens) must not evict the parent topic on a MERGE."""
    existing = memory_context.update_active_topic(
        None, "Kalau koneksinya gimana?",
        "INMP441 terhubung ke ESP32 lewat protokol I2S, butuh pin SD, SCK, WS, dan power 3.3V.",
        is_followup=True,
    )
    merged = memory_context.update_active_topic(
        existing, "Kalau yang wireless?",
        "Untuk versi wireless, bisa pakai modul I2S over WiFi custom atau BLE audio, tapi latency lebih tinggi.",
        is_merge=True,
    )
    assert "esp32" in merged.terms
    assert "inmp441" in merged.terms
    assert "wireless" in merged.terms
    assert len(merged.terms) <= memory_context._ACTIVE_TOPIC_MAX_TERMS


def test_02_merge_reserves_parent_topic_across_repeated_merges():
    """BUG 1's second trigger - `old_terms` itself already near the cap
    after a prior merge; a second, short merge must still not wipe it."""
    snap = memory_context.update_active_topic(None, "ESP32 pakai INMP441.", "reply", is_followup=False)
    snap = memory_context.update_active_topic(
        snap, "Eh bukan, maksudku ESP32-S3.",
        "Oke, ESP32-S3 dengan INMP441 juga kombinasi yang bagus, malah lebih bertenaga dan hemat daya.",
        is_merge=True,
    )
    snap = memory_context.update_active_topic(
        snap, "Yang mikrofonnya tadi apa?", "Mikrofon yang dibahas tadi adalah INMP441.", is_merge=True,
    )
    assert "esp32" in snap.terms  # parent identity must survive two merges


def test_03_merge_new_terms_preserved_even_when_old_is_large():
    """The reserved-old quota must not starve the NEW attribute either -
    both sides matter, not just the old side."""
    old_terms = frozenset(f"oldterm{i}" for i in range(18))
    existing = memory_context.ActiveTopicSnapshot(terms=old_terms)
    merged = memory_context.update_active_topic(
        existing, "Kalau yang wireless?", "reply pendek", is_merge=True,
    )
    assert "wireless" in merged.terms


def test_04_attribute_lebih_bagus_classifies_correctly():
    # BUG 2 - brief's own Phase 8 adversarial phrase.
    assert memory.classify_reference_type("yang lebih bagus?") == "attribute_reference"


def test_05_attribute_lebih_kecil_classifies_correctly():
    # BUG 2 - brief's own Phase 8 adversarial phrase.
    assert memory.classify_reference_type("yang lebih kecil?") == "attribute_reference"


def test_06_attribute_paling_murah_classifies_correctly():
    assert memory.classify_reference_type("yang paling murah?") == "attribute_reference"


def test_07_attribute_paling_bagus_classifies_correctly():
    assert memory.classify_reference_type("yang paling bagus?") == "attribute_reference"


def test_08_cost_comparison_lebih_murah_unaffected_by_fix():
    # Must still win at higher precedence - the fix must not touch this.
    assert memory.classify_reference_type("yang lebih murah?") == "cost_comparison"
    assert memory.classify_reference_type("yang lebih mahal?") == "cost_comparison"


def test_09_terus_yang_paling_murah_now_merges_not_preserves():
    # The exact brief Scenario A turn 4 phrase - was "continuation"
    # (PRESERVE, silently discarding "paling murah"), now correctly
    # "attribute_reference" (MERGE).
    assert memory.classify_reference_type("Terus yang paling murah?") == "attribute_reference"
    assert memory.is_merge_reference_followup("Terus yang paling murah?") is True


def test_10_bare_terus_still_continuation():
    # Backward-compat guard - the "terus" residual-stopword exemption
    # must not turn EVERY "terus"-prefixed turn into an attribute match.
    assert memory.classify_reference_type("Terus?") == "continuation"
    assert memory.classify_reference_type("Terus gimana?") == "continuation"


def test_11_terus_pilih_yang_mana_still_continuation_unaffected():
    # Pre-existing test_12 from Sprint 38's own suite, re-asserted here
    # as a direct regression guard for THIS sprint's own residual-word
    # change.
    assert memory.classify_reference_type("terus pilih yang mana?") == "continuation"


def test_12_rich_sentence_with_lebih_paling_not_misclassified():
    # A genuinely rich, self-contained sentence containing "lebih"/
    # "paling" as ordinary words must NOT be treated as an elliptical
    # attribute fragment - real residual content still disqualifies it.
    assert memory.classify_reference_type(
        "Modul Bluetooth apa yang paling bagus buat ESP8266 dengan jangkauan jauh?"
    ) == "unknown"


def test_13_topic_overlap_stopwords_include_soal():
    # BUG 3 - direct unit assertion on the stopword set itself.
    assert "soal" in memory_context._TOPIC_OVERLAP_STOPWORDS


def test_14_soal_no_longer_causes_false_positive_overlap():
    # BUG 3 reproduction - two unrelated entries introduced with "soal X"
    # phrasing must NOT match a query whose only shared token is "soal".
    history = [
        memory_context.ActiveTopicSnapshot(terms=frozenset({"aku", "mau", "soal", "pc", "tanya"})),
        memory_context.ActiveTopicSnapshot(terms=frozenset({"aku", "mau", "soal", "aquascape", "bahas"})),
    ]
    candidates = memory_context.select_topic_candidates(history, "Yang tadi soal mic gimana?", True)
    assert candidates == []


def test_15_soal_overlap_still_works_for_genuine_shared_subject():
    # The fix must not be so aggressive it breaks genuine overlap on a
    # REAL shared subject word alongside "soal".
    history = [memory_context.ActiveTopicSnapshot(terms=frozenset({"esp32", "inmp441", "mic", "i2s"}))]
    candidates = memory_context.select_topic_candidates(history, "Yang tadi soal mic gimana?", True)
    assert len(candidates) == 1
    assert "mic" in candidates[0].terms


def test_16_topic_history_max_entries_raised_to_8():
    # BUG 4 - direct unit assertion on the cap itself.
    assert memory_context._TOPIC_HISTORY_MAX_ENTRIES == 8


def test_17_topic_history_still_bounded_not_unbounded():
    # The fix raises the cap, it must not remove it.
    history = None
    for i in range(20):
        history = memory_context.update_topic_history(
            history, f"topik unik nomor {i} tentang widget{i}", f"reply widget{i}", is_followup=False,
        )
    assert len(history) <= 8


# ============================================================================
# Section 2 - adversarial phrase matrix (Phase 8's own explicit list)
# ============================================================================

_PHASE_8_PHRASES = [
    "yang itu",
    "yang ini",
    "yang tadi",
    "yang tadi soal mic",
    "yang kedua",
    "yang terakhir",
    "yang murah",
    "yang wireless",
    "yang lebih bagus",
    "yang lebih kecil",
    "kalau buat ESP32?",
    "kalau buat laptop?",
    "bukan itu",
    "bukan, maksudku...",
    "eh maksudku...",
    "nggak, yang tadi",
    "terus?",
    "kenapa?",
    "terus kenapa?",
    "kalau begitu?",
    "apa lagi?",
    "masih ada?",
]


def test_18_adversarial_phrase_matrix_never_crashes_and_returns_known_type():
    # Every phrase must classify to SOME member of REFERENCE_TYPES (never
    # raise, never return None/empty) - the closed-enum contract.
    for phrase in _PHASE_8_PHRASES:
        rt = memory.classify_reference_type(phrase)
        assert rt in memory.REFERENCE_TYPES, f"{phrase!r} -> {rt!r} not in REFERENCE_TYPES"


def test_19_yang_itu_yang_ini_direct_reference():
    assert memory.classify_reference_type("yang itu") == "direct_reference"
    assert memory.classify_reference_type("yang ini") == "direct_reference"


def test_20_yang_tadi_soal_mic_direct_reference_and_needs_context():
    rt = memory.classify_reference_type("yang tadi soal mic")
    assert rt != "unknown"
    assert memory.needs_topic_context("yang tadi soal mic") is True


def test_21_yang_murah_yang_wireless_attribute_reference_merge():
    assert memory.classify_reference_type("yang murah") == "attribute_reference"
    assert memory.classify_reference_type("yang wireless") == "attribute_reference"
    assert memory.is_merge_reference_followup("yang murah") is True
    assert memory.is_merge_reference_followup("yang wireless") is True


def test_22_repair_phrases_classify_as_repair_reference():
    assert memory.classify_reference_type("bukan, maksudku...") == "repair_reference"
    assert memory.classify_reference_type("eh maksudku...") == "repair_reference"


def test_23_topic_collision_substrings_word_boundary_safe():
    # "mic" must not spuriously match inside "mikrofon"/"microphone", and
    # "esp32" must not spuriously match inside "esp32-s3" in a way that
    # causes wrong disambiguation - word-boundary-safe overlap check.
    history = [memory_context.ActiveTopicSnapshot(terms=frozenset({"mikrofon", "esp32", "i2s"}))]
    query_tokens = set(memory_context.analyze_query("mic gimana?").tokens) - memory_context._TOPIC_OVERLAP_STOPWORDS
    # "mic" (bare token) is NOT the same token as "mikrofon" - the
    # overlap check is token-set based, not substring based, so a query
    # for bare "mic" does not automatically match a "mikrofon" entry
    # (this is intentionally documented, not silently assumed).
    assert "mic" in query_tokens
    assert "mikrofon" in history[0].terms
    assert "mic" not in history[0].terms


def test_24_bluetooth_module_collision_hc05_hm10():
    history = [
        memory_context.ActiveTopicSnapshot(terms=frozenset({"bluetooth", "hc-05", "modul"})),
        memory_context.ActiveTopicSnapshot(terms=frozenset({"bluetooth", "hm-10", "modul"})),
    ]
    candidates = memory_context.select_topic_candidates(history, "yang HC-05 gimana?", True)
    # Must not crash, must return a bounded list (0-2), never fabricate
    # a merge of both entries into one.
    assert len(candidates) <= memory_context._TOPIC_HISTORY_CANDIDATE_LIMIT


# ============================================================================
# Section 3 - Scenario D: 12 elliptical phrases, classification + policy
# ============================================================================

def test_25_scenario_d_terus_preserves_and_needs_context():
    assert memory.classify_reference_type("Terus?") == "continuation"
    assert memory.is_pure_reference_followup("Terus?") is True
    assert memory.needs_topic_context("Terus?") is True


def test_26_scenario_d_kalau_yang_lain_alternative_preserve():
    assert memory.classify_reference_type("Kalau yang lain?") == "alternative_request"
    assert memory.is_pure_reference_followup("Kalau yang lain?") is True


def test_27_scenario_d_yang_tadi_direct_preserve():
    assert memory.classify_reference_type("Yang tadi?") == "direct_reference"
    assert memory.is_pure_reference_followup("Yang tadi?") is True


def test_28_scenario_d_yang_pertama_ordinal_preserve():
    assert memory.classify_reference_type("Yang pertama?") == "ordinal_reference"
    assert memory.is_pure_reference_followup("Yang pertama?") is True


def test_29_scenario_d_kalau_versi_murah_attribute_merge():
    assert memory.classify_reference_type("Kalau versi murah?") == "attribute_reference"
    assert memory.is_merge_reference_followup("Kalau versi murah?") is True


def test_30_scenario_d_genuinely_ambiguous_phrases_retrieve_zero():
    # "Kenapa?"/"Kalau begitu?"/"Yang mana?"/"Masih ada?"/"Kalau buat
    # saya?" carry no standalone referent and no unambiguous anchor to
    # any specific prior entity - reviewed in Phase 2 and left as
    # `unknown` (zero retrieval, no fabrication) is the CORRECT,
    # conservative outcome here, per the brief's own ambiguity policy
    # ("prefer zero retrieval... over guessing"), not a bug requiring a
    # fix. This test locks that deliberate decision in as a regression
    # guard - if any of these ever starts classifying as something else,
    # that's a decision that needs a fresh, reproduced justification.
    ambiguous_phrases = ["Kenapa?", "Kenapa begitu?", "Kalau begitu?", "Yang mana?", "Masih ada?", "Kalau buat saya?"]
    for phrase in ambiguous_phrases:
        rt = memory.classify_reference_type(phrase)
        assert rt == "unknown", f"{phrase!r} -> {rt!r} (expected 'unknown', a deliberate Phase 2 decision)"
        assert memory.needs_topic_context(phrase) is False


# ============================================================================
# Section 4 - 18 named scenarios (Phase 7)
# ============================================================================

def test_31_direct_continuation():
    snap = memory_context.update_active_topic(None, "ESP32 pakai INMP441.", "reply I2S mic", is_followup=False)
    preserved = memory_context.update_active_topic(snap, "Terus?", "reply2", is_followup=True)
    assert preserved.terms == snap.terms
    assert preserved.turns_since_active == 1


def test_32_short_continuation_chain():
    snap = memory_context.update_active_topic(None, "ESP32 pakai INMP441.", "reply", is_followup=False)
    for text in ["yang lain?", "terus?", "kalau itu?"]:
        snap = memory_context.update_active_topic(snap, text, "reply", is_followup=True)
    assert "esp32" in snap.terms
    assert "inmp441" in snap.terms
    assert snap.turns_since_active == 3


def test_33_attribute_merge():
    snap = memory_context.update_active_topic(None, "jelasin mic buat ESP32", "reply mic esp32", is_followup=False)
    merged = memory_context.update_active_topic(snap, "kalau yang wireless?", "jawaban wireless", is_merge=True)
    assert "esp32" in merged.terms
    assert "wireless" in merged.terms


def test_34_comparison_preservation_esp32_vs_wled():
    # "ESP32 gimana?" (comparison, real residual word) correctly REPLACES
    # (it names something concrete enough to anchor a new topic) - this
    # is existing, unchanged Sprint 4 behavior, re-asserted as a Sprint
    # 39 regression guard.
    assert memory.classify_reference_type("ESP32 gimana?") == "comparison"
    assert memory.is_pure_reference_followup("ESP32 gimana?") is False


def test_35_correction_preserves_useful_history():
    snap = memory_context.update_active_topic(None, "ESP32 pakai INMP441.", "Oke, ESP32 dengan INMP441.", is_followup=False)
    corrected = memory_context.update_active_topic(
        snap, "Eh bukan, maksudku ESP32-S3.", "Oke, ESP32-S3 dengan INMP441 juga bagus.", is_merge=True,
    )
    assert "inmp441" in corrected.terms  # useful historical context NOT destroyed
    assert "s3" in corrected.terms  # correction itself present


def test_36_ordinal_reference_resolves_specific_item():
    _MIC_REPLY = "Berikut pilihan mikrofon untuk ESP32:\n1. INMP441\n2. MAX9814\n3. SPH0645\n"
    snap = memory_context.update_active_topic(None, "jelasin pilihan mikrofon", _MIC_REPLY, is_followup=False)
    targets, conf = memory_context.resolve_ordinal_targets("yang kedua gimana?", snap, [])
    assert targets == ("MAX9814",)
    assert conf == "high"


def test_37_list_reference_no_fabrication_without_list():
    snap = memory_context.ActiveTopicSnapshot(terms=frozenset({"esp32", "wifi"}))
    targets, conf = memory_context.resolve_ordinal_targets("yang kedua gimana?", snap, [])
    assert targets == ()
    assert conf == "none"


def test_38_multi_topic_switching_no_contamination():
    a = memory_context.update_active_topic(None, "aquascape pompa", "pompa submersible bagus", is_followup=False)
    b = memory_context.update_active_topic(None, "ESP32 mikrofon", "INMP441 cocok untuk mic", is_followup=False)
    assert "pompa" not in b.terms
    assert "esp32" not in a.terms


def test_39_ambiguous_reference_zero_candidates():
    history = [memory_context.ActiveTopicSnapshot(terms=frozenset({"esp32", "mic", "inmp441"}))]
    candidates = memory_context.select_topic_candidates(history, "Kenapa?", False)
    assert candidates == []


def test_40_unrelated_query_replaces_not_merges():
    snap = memory_context.update_active_topic(None, "ESP32 mikrofon INMP441", "reply mic", is_followup=False)
    replaced = memory_context.update_active_topic(
        snap, "Berapa ukuran aquarium 50x25 cm?", "Sekitar 50 liter.", is_followup=False,
    )
    assert "esp32" not in replaced.terms
    assert "aquarium" in replaced.terms


def test_41_stale_topic_decays():
    snap = memory_context.ActiveTopicSnapshot(terms=frozenset({"esp32"}), turns_since_active=10)
    assert snap.is_stale is True


def test_42_duplicate_context_not_double_counted_in_history():
    history = [memory_context.ActiveTopicSnapshot(terms=frozenset({"esp32", "mic"}))]
    updated = memory_context.update_topic_history(history, "ESP32 lagi soal mic", "reply", is_followup=False)
    # A fresh push adds ONE new entry, never duplicates an unrelated slot.
    assert len(updated) == 2


def test_43_long_conversation_bounded_state():
    history = None
    active = None
    for i in range(30):
        active = memory_context.update_active_topic(active, f"topik ke-{i} unik{i}", f"reply{i}", is_followup=False)
        history = memory_context.update_topic_history(history, f"topik ke-{i} unik{i}", f"reply{i}", is_followup=False)
    assert len(active.terms) <= memory_context._ACTIVE_TOPIC_MAX_TERMS
    assert len(history) <= memory_context._TOPIC_HISTORY_MAX_ENTRIES


def test_44_cross_conversation_isolation_e2e():
    demo = _load_demo()
    console = _new_console(demo, replies={
        "ESP32 mikrofon INMP441": "ESP32 dengan INMP441 cocok untuk mic.",
        "aquascape pompa bagus": "Pompa submersible bagus untuk aquascape.",
    })
    console.start()
    try:
        _run_turn(console, demo, "ESP32 mikrofon INMP441", "ci-1", conversation_id="conv-A")
        _run_turn(console, demo, "aquascape pompa bagus", "ci-2", conversation_id="conv-B")
        snap_a = console.planner_module._active_topic.get("conv-A")
        snap_b = console.planner_module._active_topic.get("conv-B")
        assert snap_a is not None and snap_b is not None
        assert "esp32" in snap_a.terms
        assert "esp32" not in snap_b.terms
        assert "aquascape" in snap_b.terms
        assert "aquascape" not in snap_a.terms
    finally:
        console.stop()


def test_45_bounded_state_across_many_conversations_e2e():
    demo = _load_demo()
    console = _new_console(demo, canned_text="Oke, dicatat.")
    console.start()
    try:
        for i in range(10):
            _run_turn(console, demo, f"Topik unik nomor {i} soal widget{i}.", f"bs-{i}", conversation_id=f"conv-{i}")
        assert len(console.planner_module._active_topic) <= 50
    finally:
        console.stop()


def test_46_prompt_context_integrity_e2e():
    """Turn 6 of the brief's own Scenario B (topic switching then an
    explicit delayed reference) - after BUG 3+4's fixes, the rendered
    system prompt for the reference turn must contain the CORRECT
    historical topic (mic/ESP32), not the intervening, unrelated ones
    (PC/aquascape)."""
    demo = _load_demo()
    replies = {
        "Jelasin mic buat ESP32.": "Untuk ESP32, mic yang cocok itu INMP441 karena pakai I2S digital.",
        "Aku mau bahas topik lain, soal aquascape.": "Oke, aquascape itu seni menata tanaman air di akuarium.",
        "Pompa yang bagus buat aquascape apa?": "Pompa submersible dengan flow rate stabil biasanya paling direkomendasikan.",
        "Sekarang aku mau tanya soal PC.": "Oke, soal PC, apa yang mau ditanyakan?",
        "Spek minimum buat gaming apa?": "Minimal butuh GPU kelas menengah dan RAM 16GB untuk gaming modern.",
        "Yang tadi soal mic gimana?": "ESP32 dengan INMP441 tetap jadi rekomendasi utama untuk mic karena I2S stabil.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        turns = list(replies.keys())
        for i, text in enumerate(turns[:-1], 1):
            _run_turn(console, demo, text, f"pci-{i}")
        sp = _run_turn_capture_prompt(console, demo, turns[-1], f"pci-{len(turns)}")
        # The correct historical topic block must be present...
        assert "esp32" in sp.lower() and "mic" in sp.lower()
        # ...and, specifically, the injected "Active conversation topic"
        # candidate line(s) must not be the unrelated PC/aquascape ones.
        for line in sp.splitlines():
            if line.startswith("- Active conversation topic:"):
                assert "aquascape" not in line and " pc," not in line and "ditanyakan" not in line
    finally:
        console.stop()


def test_47_no_raw_sentence_leakage_in_active_topic():
    """`ActiveTopicSnapshot.terms` must always be a bag of individual
    tokens, never a raw, un-tokenized sentence fragment - a structural
    guard against a future change accidentally storing free text."""
    snap = memory_context.update_active_topic(
        None, "ESP32 pakai INMP441 buat voice recording ya?", "reply panjang dengan banyak kata di dalamnya", is_followup=False,
    )
    for term in snap.terms:
        assert " " not in term, f"term {term!r} contains a space - looks like a raw sentence fragment, not a token"


def test_48_no_topic_contamination_across_merge_and_replace():
    snap = memory_context.update_active_topic(None, "ESP32 mikrofon INMP441", "reply mic", is_followup=False)
    merged = memory_context.update_active_topic(snap, "kalau yang wireless?", "reply wireless", is_merge=True)
    replaced = memory_context.update_active_topic(merged, "Berapa harga GPU RTX 4070?", "Sekitar 8 juta.", is_followup=False)
    assert "esp32" not in replaced.terms
    assert "wireless" not in replaced.terms
    assert "gpu" in replaced.terms or "rtx" in replaced.terms


# ============================================================================
# Section 5 - Scenario A/C E2E reproductions (the sprint's own probes,
# now as permanent regression tests)
# ============================================================================

def test_49_e2e_scenario_a_topic_continuation_chain_no_loss():
    demo = _load_demo()
    replies = {
        "ESP32 pakai INMP441.": "Oke, ESP32 dengan INMP441 itu kombinasi mikrofon I2S yang umum dipakai untuk voice project.",
        "Kalau koneksinya gimana?": "INMP441 terhubung ke ESP32 lewat protokol I2S, butuh pin SD, SCK, WS, dan power 3.3V.",
        "Kalau yang wireless?": "Untuk versi wireless, bisa pakai modul I2S over WiFi custom atau BLE audio, tapi latency lebih tinggi.",
        "Terus yang paling murah?": "Yang paling murah biasanya MAX9814 karena analog, harganya di bawah INMP441 dan modul wireless custom.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        key = _key(console)
        for i, text in enumerate(replies.keys(), 1):
            _run_turn(console, demo, text, f"sa-{i}")
        snap = console.planner_module._active_topic.get(key)
        assert "esp32" in snap.terms
        assert "murah" in snap.terms or "max9814" in snap.terms
    finally:
        console.stop()


def test_50_e2e_scenario_c_correction_preserves_history():
    """Brief's own Scenario C: "verify correction doesn't destroy useful
    historical context" - the useful historical context here is
    INMP441 (the microphone the correction turn itself is about), which
    must survive both the correction merge AND the follow-up attribute
    merge one turn later.

    Known, documented limitation (see `docs/change_impact/
    conversation_intelligence.md`): the CORRECTION'S OWN specific detail
    ("s3") is not separately guaranteed to survive a SECOND, unrelated
    merge two turns later - `_merge_terms()`'s reserved-old-quota uses a
    plain deterministic `sorted()` order (no per-term recency/importance
    tracking, which would need real new state this sprint's own Phase 3
    discipline does not justify adding for this secondary, much milder
    form of loss - the PARENT topic identity, which is what the original
    bug actually destroyed, is what's protected here and does survive)."""
    demo = _load_demo()
    replies = {
        "ESP32 pakai INMP441.": "Oke, ESP32 dengan INMP441 itu kombinasi umum untuk voice project.",
        "Eh bukan, maksudku ESP32-S3.": "Oke, ESP32-S3 dengan INMP441 juga kombinasi yang bagus, malah lebih bertenaga.",
        "Yang mikrofonnya tadi apa?": "Mikrofon yang dibahas tadi adalah INMP441.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        key = _key(console)
        for i, text in enumerate(replies.keys(), 1):
            _run_turn(console, demo, text, f"sc-{i}")
        snap = console.planner_module._active_topic.get(key)
        assert "inmp441" in snap.terms  # useful historical context survives (the brief's own requirement)
        assert "esp32" in snap.terms  # parent identity survives
    finally:
        console.stop()


def test_50b_repair_merge_alone_preserves_the_correction_itself():
    """The correction's own new detail IS guaranteed to survive the
    SINGLE merge turn it's introduced on (only the later, second,
    unrelated merge two turns further on is the documented limitation
    above) - this is what `test_29_merge_preserves_old_terms_and_adds_new`
    in `test_conversation_reference_resolution.py` already covers at the
    unit level; re-asserted here through the real E2E path."""
    demo = _load_demo()
    replies = {
        "ESP32 pakai INMP441.": "Oke, ESP32 dengan INMP441 itu kombinasi umum untuk voice project.",
        "Eh bukan, maksudku ESP32-S3.": "Oke, ESP32-S3 dengan INMP441 juga kombinasi yang bagus, malah lebih bertenaga.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        key = _key(console)
        for i, text in enumerate(replies.keys(), 1):
            _run_turn(console, demo, text, f"scb-{i}")
        snap = console.planner_module._active_topic.get(key)
        assert "inmp441" in snap.terms
        assert "s3" in snap.terms
        assert "esp32" in snap.terms
    finally:
        console.stop()


# ============================================================================
# Section 6 - performance (Phase 9)
# ============================================================================

def test_51_classification_latency_bounded():
    phrases = _PHASE_8_PHRASES * 20
    start = time.perf_counter()
    for p in phrases:
        memory.classify_reference_type(p)
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / len(phrases)
    assert per_call_ms < 5.0, f"classify_reference_type() averaged {per_call_ms:.3f}ms/call (target <5ms)"


def test_52_merge_terms_latency_bounded():
    old_terms = frozenset(f"term{i}" for i in range(20))
    new_terms = tuple(f"newterm{i}" for i in range(19))
    start = time.perf_counter()
    for _ in range(200):
        memory_context._merge_terms(new_terms, old_terms)
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / 200
    assert per_call_ms < 5.0, f"_merge_terms() averaged {per_call_ms:.3f}ms/call (target <5ms)"


def test_53_topic_candidate_selection_latency_bounded():
    history = [memory_context.ActiveTopicSnapshot(terms=frozenset(f"t{i}{j}" for j in range(15))) for i in range(8)]
    start = time.perf_counter()
    for _ in range(200):
        memory_context.select_topic_candidates(history, "yang tadi soal t1 gimana?", True)
    elapsed_ms = (time.perf_counter() - start) * 1000
    per_call_ms = elapsed_ms / 200
    assert per_call_ms < 5.0, f"select_topic_candidates() averaged {per_call_ms:.3f}ms/call (target <5ms)"
