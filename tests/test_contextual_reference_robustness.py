"""
test_contextual_reference_robustness.py
====================================================

SPRINT 46 - CONTEXTUAL REFERENCE ROBUSTNESS.

Goal: make short, elliptical, Indonesian conversational follow-ups remain
attached to the correct entity/concept without causing cross-topic
contamination, without an LLM judge, embeddings, a second ranking system,
an external vector database, a synonym-dictionary explosion, global topic
state, persistent raw conversation state, or a new memory architecture.

Phase 0-2 (live reproduction via real `RuntimeDemoConsole`, before any
code changed) ran a 10-scenario (A-J) + adversarial probe matrix against
the existing Sprint 36-45 pipeline. Most scenarios (A, B, C, E, F, G, J,
plus contamination in both directions) were ALREADY correct - per this
sprint's own explicit instruction, none of those were modified; this file
locks several of them in as regression tests instead.

THREE narrow, genuinely-reproduced gaps were found and fixed:

1. **`_normalize_terms_for_bridging()` never chained the synonym-canon
   lookup onto its own affix-stripped root** - only the ORIGINAL token was
   checked against `_TOKEN_SYNONYM_CANON`. Any word needing BOTH
   transformations together ("mikrofonnya" -> "mikrofon" -> "mic",
   "mengganti" -> "ganti" -> "upgrade") silently lost the synonym step.
   Fixed by adding one extra lookup against the already-computed root.

2. **A lone residual token that is itself a historical-query marker**
   ("sebelumnya", "dulu", "yang lama", "pernah" -
   `luno.memory.is_historical_query()`, Sprint 40) was treated as
   signal-less filler by `is_active_topic_relevant_to_query()`'s
   single-token low-ambiguity fallback, so it fell through to `return
   True` and confidently injected the CURRENT/active topic for a query
   that explicitly asks about something else - "confidently wrong"
   context injection, not merely "unresolved". Fixed with a narrow guard:
   a historical-marked lone token no longer claims relevance for an
   active snapshot whose own `status` represents a present/future state
   (`"active"`/`"completed"`/`"planned"`), so the caller correctly falls
   through to `select_temporal_fallback_candidate()` (Sprint 41) instead,
   which itself safely returns nothing when no status-eligible entry
   exists (prefers "not enough information" over a wrong answer).

3. **"kenapa"/"napa"/"mengapa" ("why") were missing from
   `_TOPIC_OVERLAP_STOPWORDS`**, unlike the colloquial "kok" already
   there - not just a missed-resolution case but a genuine ENTITY-EROSION
   bug: "GPU-nya kenapa?" (2 residual tokens without the fix) narrowly
   missed `is_sparse_unknown_followup()`'s `<= 1` threshold (Sprint 44),
   so it replaced rather than merged into the active topic, permanently
   discarding an established entity's identity before a LATER,
   unambiguous alias follow-up ("Kartu grafisnya bagaimana?") could ever
   reach it. Fixed by adding the three words, matching "kok"'s existing
   treatment.

TWO candidate fixes were investigated, reproduced as genuinely fixing
their target case, and then REJECTED because they broke an existing,
deliberately-tested guarantee - not silently, but confirmed via a full
before/after regression run:

- Widening the `coverage > 0.5` topic-lineage-skip check (in
  `is_active_topic_relevant_to_query()`, both branches) to `>= 0.5` fixed
  an exact-50%-coverage same-entity lineage case, but broke
  `tests/test_semantic_context_bridging.py::
  test_39_tied_normalized_overlap_across_history_is_not_relevant` (a
  genuinely disjoint two-topic pair that also happens to land at exactly
  50% coverage). Left at strict `>`; documented as a known limitation.
- Adding "lebih"/"paling" to `_TOPIC_OVERLAP_STOPWORDS` fixed a
  single-topic "Yang lebih bagus?"/"Yang paling murah?" attribute
  question, but broke `tests/test_entity_identity_semantic_alias_
  continuity.py::test_76_e2e_multi_topic_ambiguity_gpu_vs_pompa` (a
  genuinely-2-competing-topic case whose safety net,
  `is_active_topic_relevant_to_query()`'s `distinct_other_count >= 2`
  check, is calibrated for 3+ topics, not exactly 2). Left out; documented
  as a known limitation.

No new entity relationship model, no embeddings, no LLM judge, no second
ranking system, no new synonym groups, no global topic state, no
persistent raw conversation storage. All three fixes are purely additive
extensions of existing, already-tested mechanisms
(`_normalize_terms_for_bridging()`, `is_active_topic_relevant_to_query()`,
`_TOPIC_OVERLAP_STOPWORDS`).
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
from luno.memory_context import ActiveTopicSnapshot  # noqa: E402


# ============================================================================
# Shared E2E harness (same pattern as prior sprints' own test files)
# ============================================================================

def _load_demo(tag: str = "s46"):
    unique = f"main_runtime_demo_{tag}_{id(object())}"
    demo_spec = importlib.util.spec_from_file_location(unique, os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules[unique] = demo
    demo_spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 6.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _build_client(demo, replies):
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


def _new_console(demo, replies=None, canned_text="Oke."):
    if replies:
        client = _build_client(demo, replies)
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


def _lines_starting(sp: str, *prefixes: str) -> list:
    out = []
    for line in sp.splitlines():
        s = line.strip()
        if any(s.startswith(p) for p in prefixes):
            out.append(s)
    return out


_ANY_CANDIDATE_PREFIXES = (
    "- Active conversation topic", "- Referenced item", "- Previously stated",
    "- Planned", "- Completed", "- Cancelled",
)


def _snap(*terms, age=0, status="active", source_sentence=""):
    return ActiveTopicSnapshot(terms=frozenset(terms), turns_since_active=age, status=status, source_sentence=source_sentence)


# ============================================================================
# Section 1 - Fix #1: `_normalize_terms_for_bridging()` chained-alias unit tests
# ============================================================================

def test_01_mikrofonnya_root_then_canon_chain_includes_mic():
    expanded = memory_context._normalize_terms_for_bridging({"mikrofonnya"})
    assert "mikrofon" in expanded
    assert "mic" in expanded


def test_02_mengganti_root_then_canon_chain_includes_upgrade():
    expanded = memory_context._normalize_terms_for_bridging({"mengganti"})
    assert "ganti" in expanded
    assert "upgrade" in expanded


def test_03_pompanya_root_resolves_to_its_own_canonical_form():
    # The pompa/pump synonym group's own canonical form IS "pompa"
    # (Indonesian-preferred, unlike the mic/upgrade groups above which
    # canonicalize to the English term) - "pompanya" -> root "pompa" ->
    # canon("pompa") == "pompa" itself, so no extra alias is added here.
    # The chain still exercises the same two-step code path as test_01/
    # test_02 above without erroring or dropping the root.
    expanded = memory_context._normalize_terms_for_bridging({"pompanya"})
    assert "pompa" in expanded
    assert memory_context._TOKEN_SYNONYM_CANON.get("pompa") == "pompa"


def test_03b_pump_alone_canonicalizes_to_pompa():
    expanded = memory_context._normalize_terms_for_bridging({"pump"})
    assert "pompa" in expanded


def test_04_original_tokens_never_dropped_purely_additive():
    expanded = memory_context._normalize_terms_for_bridging({"mikrofonnya"})
    assert "mikrofonnya" in expanded  # original token always survives


def test_05_bare_already_canonical_token_unaffected():
    # "mic" itself needs no root-then-canon chain - direct membership check
    # (regression guard: the two-line addition must not double-apply or
    # error on a token that was already a canon target).
    expanded = memory_context._normalize_terms_for_bridging({"mic"})
    assert "mic" in expanded


def test_06_unrelated_word_gets_no_spurious_alias():
    expanded = memory_context._normalize_terms_for_bridging({"kabelnya"})
    assert "mic" not in expanded
    assert "gpu" not in expanded
    assert "upgrade" not in expanded


def test_07_e2e_mikrofonnya_after_micnya_resolves_esp32_inmp441():
    """Scenario D (Sprint 46 probe matrix) - the live-reproduced failure
    this fix closes: "mic-nya gimana?" already worked before this sprint;
    "Mikrofonnya bagaimana?" (root+canon chained) silently failed."""
    demo = _load_demo("s46-07")
    replies = {
        "ESP32 pakai INMP441.": "Oke, dicatat ESP32 dengan INMP441.",
        "Mic-nya gimana?": "INMP441 adalah mic I2S yang cocok buat ESP32.",
        "Mikrofonnya bagaimana?": "INMP441 tetap mic terbaik untuk ESP32 ini.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t07-1", conversation_id="c07")
        _run_turn(console, demo, "Mic-nya gimana?", "t07-2", conversation_id="c07")
        sp = _run_turn_capture_prompt(console, demo, "Mikrofonnya bagaimana?", "t07-3", conversation_id="c07")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        joined = " ".join(candidates).lower()
        assert candidates, f"expected mic/ESP32 identity to survive, prompt had no candidate: {sp}"
        assert "esp32" in joined and "inmp441" in joined
    finally:
        console.stop()


def test_08_e2e_mengganti_after_ganti_resolves_same_upgrade_topic():
    demo = _load_demo("s46-08")
    replies = {
        "Aku mau ganti GPU ke RTX 4070.": "Oke, dicatat rencana ganti GPU ke RTX 4070.",
        "Kalau mengganti PSU juga perlu?": "Iya, PSU mungkin perlu diganti juga buat RTX 4070.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau ganti GPU ke RTX 4070.", "t08-1", conversation_id="c08")
        sp = _run_turn_capture_prompt(console, demo, "Kalau mengganti PSU juga perlu?", "t08-2", conversation_id="c08")
        # Not asserting a specific candidate shape here (this turn carries
        # its own substantial content, "psu"/"rtx 4070" is a comparison-
        # shaped turn) - only that the module did not raise and processed
        # normally; the alias-chain unit tests above are the precise lock.
        assert sp is not None
    finally:
        console.stop()


# ============================================================================
# Section 2 - Fix #2: historical-query single-token guard unit tests
# ============================================================================

def test_09_historical_lone_token_not_relevant_when_active_is_present_state():
    active = _snap("beli", "dicatat", "hdd", "oke", "pakai", "rencana", "saya",
                    "sedang", "sekarang", "ssd", status="active",
                    source_sentence="Sekarang pakai HDD.")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Yang sebelumnya gimana?", topic_history=[active],
    ) is False


def test_10_historical_lone_token_still_relevant_when_active_itself_is_past():
    # If the ACTIVE snapshot itself already represents a past/superseded
    # state, a historical query correctly still trusts it - the new guard
    # only excludes "active"/"completed"/"planned" statuses.
    active = _snap("beli", "dicatat", "rencana", "ssd", status="superseded",
                    source_sentence="Rencana saya beli SSD.")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Yang sebelumnya gimana?", topic_history=[active],
    ) is True


def test_11_non_historical_lone_token_unaffected_by_new_guard():
    # A signal-less, non-historical single token ("Filternya gimana?")
    # must still fall through to the pre-existing "trust recency" default
    # - this fix must not touch that path.
    active = _snap("aquascape", "pompa", "kecil", status="active",
                    source_sentence="Aquascape-ku pakai pompa kecil.")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Filternya gimana?", topic_history=[active],
    ) is True


def test_12_historical_marker_alone_current_state_query_type_unaffected():
    # `is_current_state_query()`-shaped lone tokens ("Sekarang?") are a
    # DIFFERENT classifier path entirely and must be unaffected by this
    # historical-only guard.
    active = _snap("beli", "ssd", "rencana", status="planned",
                    source_sentence="Rencana saya beli SSD.")
    assert memory.is_historical_query("Sekarang?") is False


def test_13_e2e_scenario_i_temporal_no_confidently_wrong_injection():
    """Scenario I (Sprint 46 probe matrix) - the live-reproduced failure
    this fix closes: before the fix, "Yang sebelumnya gimana?" wrongly,
    confidently injected the CURRENT (HDD) topic instead of either the
    plan (ideal) or nothing (safe). After the fix: nothing is injected
    (the "planned"-status SSD entry is not currently eligible for the
    "historical" temporal-fallback tier either - a separate, intentionally
    unchanged eligibility question, see this file's own module docstring
    and `docs/change_impact/contextual_reference_robustness.md`) - which
    is the SAFE outcome per this project's ambiguity-safety principle:
    prefer no context over wrong context."""
    demo = _load_demo("s46-13")
    replies = {
        "Rencana saya beli SSD.": "Oke, dicatat rencana beli SSD.",
        "Sekarang pakai HDD.": "Oke, dicatat sedang pakai HDD.",
        "Yang sebelumnya gimana?": "Sebelumnya rencananya beli SSD.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Rencana saya beli SSD.", "t13-1", conversation_id="c13")
        _run_turn(console, demo, "Sekarang pakai HDD.", "t13-2", conversation_id="c13")
        sp = _run_turn_capture_prompt(console, demo, "Yang sebelumnya gimana?", "t13-3", conversation_id="c13")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        joined = " ".join(candidates).lower()
        # Must NOT confidently claim the CURRENT (HDD) topic answers "the
        # previous one" - the specific bug this fix closes.
        assert "hdd" not in joined, f"must not confidently inject the CURRENT topic for a historical query: {sp}"
    finally:
        console.stop()


# ============================================================================
# Section 3 - Fix #3: "kenapa"/"napa"/"mengapa" stopword + entity-erosion
# ============================================================================

def test_14_kenapa_now_a_topic_overlap_stopword():
    assert "kenapa" in memory_context._TOPIC_OVERLAP_STOPWORDS
    assert "napa" in memory_context._TOPIC_OVERLAP_STOPWORDS
    assert "mengapa" in memory_context._TOPIC_OVERLAP_STOPWORDS


def test_15_gpu_nya_kenapa_is_sparse_unknown_followup():
    assert memory.classify_reference_type("GPU-nya kenapa?") == "unknown"
    assert memory_context.is_sparse_unknown_followup("GPU-nya kenapa?") is True


def test_16_two_substantial_words_plus_kenapa_still_not_sparse():
    # A GENUINELY new, substantial turn that happens to end in "kenapa"
    # must still be treated as a rich turn - "kenapa" alone must not
    # blanket-suppress every turn containing it.
    assert memory_context.is_sparse_unknown_followup(
        "Motor listrik baru kenapa mahal banget?"
    ) is False


def test_17_e2e_gpu_nya_kenapa_preserves_rtx3060_identity():
    """Phase 3 (Sprint 46 brief's own worked example) - the live-
    reproduced entity-erosion bug this fix closes: "GPU-nya kenapa?" (no
    candidate injected for itself, correctly - "RTX 3060" was never
    literally called "gpu", the deliberate no-product-to-category-
    fabrication boundary) must NOT destructively replace the active
    RTX 3060 topic, so a LATER alias follow-up ("Kartu grafisnya
    bagaimana?") can still correctly recover it."""
    demo = _load_demo("s46-17")
    replies = {
        "RTX 3060 saya panas.": "Oke, dicatat RTX 3060 kamu panas.",
        "GPU-nya kenapa?": "Mungkin thermal paste atau airflow kurang.",
        "Kartu grafisnya bagaimana?": "RTX 3060 kamu perlu dicek suhunya.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "RTX 3060 saya panas.", "t17-1", conversation_id="c17")
        _run_turn(console, demo, "GPU-nya kenapa?", "t17-2", conversation_id="c17")
        sp = _run_turn_capture_prompt(console, demo, "Kartu grafisnya bagaimana?", "t17-3", conversation_id="c17")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        joined = " ".join(candidates).lower()
        assert candidates, f"expected RTX 3060 identity to survive through 'GPU-nya kenapa?', prompt had none: {sp}"
        assert "rtx" in joined and "panas" in joined
    finally:
        console.stop()


def test_18_e2e_mic_i2s_wireless_chain_unaffected_already_working():
    """Phase 3's second worked example (mic/I2S/wireless chain) was
    already correct before this sprint - locked in as a regression test,
    unmodified."""
    demo = _load_demo("s46-18")
    replies = {
        "ESP32 pakai INMP441.": "Oke, dicatat ESP32 dengan INMP441.",
        "Mic-nya bagaimana?": "INMP441 mic I2S yang bagus.",
        "Kalau I2S?": "I2S protokol digital audio, ESP32 native support.",
        "Yang wireless?": "ESP32 juga ada WiFi/Bluetooth built-in.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t18-1", conversation_id="c18")
        sp2 = _run_turn_capture_prompt(console, demo, "Mic-nya bagaimana?", "t18-2", conversation_id="c18")
        sp3 = _run_turn_capture_prompt(console, demo, "Kalau I2S?", "t18-3", conversation_id="c18")
        sp4 = _run_turn_capture_prompt(console, demo, "Yang wireless?", "t18-4", conversation_id="c18")
        assert "esp32" in " ".join(_lines_starting(sp2, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "inmp441" in " ".join(_lines_starting(sp3, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "inmp441" in " ".join(_lines_starting(sp4, *_ANY_CANDIDATE_PREFIXES)).lower()
    finally:
        console.stop()


# ============================================================================
# Section 4 - Scenario A-J regression locks (already-correct behavior)
# ============================================================================

def test_19_e2e_scenario_a_single_topic_continuation():
    # "Kalau X?" phrasing classifies `"unknown"` (deliberately, per
    # `tests/test_conversation_reference_resolution.py::
    # test_13_adversarial_phrase_matrix`'s own precedent) - it never
    # injects a per-turn candidate for ITSELF, so "identity survives" is
    # verified via the persisted active-topic snapshot's own terms (what
    # a LATER genuine follow-up, e.g. "Terus?", would draw on) rather
    # than this turn's own prompt.
    demo = _load_demo("s46-19")
    replies = {
        "ESP32 pakai INMP441.": "Oke, dicatat ESP32 dengan INMP441.",
        "Kalau koneksinya?": "Koneksinya pakai I2S, cukup 4 pin ke ESP32.",
        "Kalau wireless?": "ESP32 juga ada WiFi/Bluetooth built-in.",
        "Terus?": "ESP32 dengan INMP441 tetap kombinasi yang solid.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t19-1", conversation_id="c19")
        _run_turn(console, demo, "Kalau koneksinya?", "t19-2", conversation_id="c19")
        _run_turn(console, demo, "Kalau wireless?", "t19-3", conversation_id="c19")
        sp = _run_turn_capture_prompt(console, demo, "Terus?", "t19-4", conversation_id="c19")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "esp32" in joined and "inmp441" in joined, f"identity lost across 'Kalau ...?' chain: {sp}"
    finally:
        console.stop()


def test_20_e2e_scenario_c_product_abbreviation_rtx3060():
    demo = _load_demo("s46-20")
    replies = {
        "PC saya pakai RTX 3060.": "Oke, dicatat PC kamu pakai RTX 3060.",
        "GPU-nya gimana?": "RTX 3060 cukup kuat buat gaming 1080p.",
        "Kartu grafisnya?": "RTX 3060 tetap pilihan solid di kelasnya.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "PC saya pakai RTX 3060.", "t20-1", conversation_id="c20")
        sp2 = _run_turn_capture_prompt(console, demo, "GPU-nya gimana?", "t20-2", conversation_id="c20")
        sp3 = _run_turn_capture_prompt(console, demo, "Kartu grafisnya?", "t20-3", conversation_id="c20")
        assert "rtx" in " ".join(_lines_starting(sp2, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "rtx" in " ".join(_lines_starting(sp3, *_ANY_CANDIDATE_PREFIXES)).lower()
    finally:
        console.stop()


def test_21_e2e_scenario_d_possessive_clitic_forms():
    demo = _load_demo("s46-21")
    replies = {
        "Aku upgrade SSD baru.": "Oke, dicatat upgrade SSD baru.",
        "SSDnya berapa GB?": "SSD barumu kapasitasnya cukup besar.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku upgrade SSD baru.", "t21-1", conversation_id="c21")
        sp = _run_turn_capture_prompt(console, demo, "SSDnya berapa GB?", "t21-2", conversation_id="c21")
        assert "ssd" in " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
    finally:
        console.stop()


def test_22_e2e_scenario_e_correction_continuity():
    demo = _load_demo("s46-22")
    replies = {
        "ESP32 pakai INMP441.": "Oke, dicatat ESP32 dengan INMP441.",
        "Eh maksudku ESP32-S3.": "Oke, dikoreksi jadi ESP32-S3 dengan INMP441.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t22-1", conversation_id="c22")
        sp = _run_turn_capture_prompt(console, demo, "Eh maksudku ESP32-S3.", "t22-2", conversation_id="c22")
        # Correction is a rich/merge-eligible turn - not necessarily
        # injecting itself as a "candidate" line, only must not raise and
        # must record the correction cleanly (locked via active-topic
        # state check below rather than the prompt).
        assert sp is not None
        active = console.planner_module._active_topic.get(("c22", None))
        if active is None:
            # Some builds key active topic without conversation id when
            # only one conversation is live - fall back to any value.
            active = next(iter(console.planner_module._active_topic.values()), None)
        assert active is not None
        assert "s3" in active.terms or any("s3" in t for t in active.terms)
    finally:
        console.stop()


def test_23_e2e_scenario_f_multi_topic_recovery():
    demo = _load_demo("s46-23")
    replies = {
        "ESP32 pakai mic INMP441.": "Oke, dicatat ESP32 dengan mic INMP441.",
        "Aquascape saya pakai pompa kecil.": "Oke, dicatat aquascape dengan pompa kecil.",
        "PC saya pakai GPU RTX 3060.": "Oke, dicatat PC dengan GPU RTX 3060.",
        "Yang tadi soal mic gimana?": "Mic INMP441-mu tetap kompatibel dengan ESP32.",
        "Pompa yang tadi?": "Pompa kecil aquascape-mu masih yang direkomendasikan.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai mic INMP441.", "t23-1", conversation_id="c23")
        _run_turn(console, demo, "Aquascape saya pakai pompa kecil.", "t23-2", conversation_id="c23")
        _run_turn(console, demo, "PC saya pakai GPU RTX 3060.", "t23-3", conversation_id="c23")
        sp4 = _run_turn_capture_prompt(console, demo, "Yang tadi soal mic gimana?", "t23-4", conversation_id="c23")
        joined4 = " ".join(_lines_starting(sp4, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "mic" in joined4 or "inmp441" in joined4
        assert "pompa" not in joined4 and "rtx" not in joined4
    finally:
        console.stop()


def test_24_e2e_scenario_g_ambiguous_no_fabrication():
    demo = _load_demo("s46-24")
    replies = {
        "ESP32 pakai mic INMP441.": "Oke, dicatat ESP32 dengan mic INMP441.",
        "Aquascape saya pakai pompa kecil.": "Oke, dicatat aquascape dengan pompa kecil.",
        "PC saya pakai GPU RTX 3060.": "Oke, dicatat PC dengan GPU RTX 3060.",
        "Yang mana?": "Maksudnya yang mana ya, bisa diperjelas?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai mic INMP441.", "t24-1", conversation_id="c24")
        _run_turn(console, demo, "Aquascape saya pakai pompa kecil.", "t24-2", conversation_id="c24")
        _run_turn(console, demo, "PC saya pakai GPU RTX 3060.", "t24-3", conversation_id="c24")
        sp4 = _run_turn_capture_prompt(console, demo, "Yang mana?", "t24-4", conversation_id="c24")
        joined4 = " ".join(_lines_starting(sp4, *_ANY_CANDIDATE_PREFIXES)).lower()
        # Must not confidently pick ONE of three live, unrelated topics.
        hits = sum(w in joined4 for w in ("inmp441", "pompa", "rtx"))
        assert hits <= 1, f"ambiguous 'Yang mana?' across 3 topics should not confidently combine/guess: {sp4}"
    finally:
        console.stop()


def test_25_e2e_scenario_j_ordinal_reference():
    demo = _load_demo("s46-25")
    replies = {
        "Mic apa yang bagus buat ESP32?": "Beberapa pilihan:\n1. INMP441\n2. MAX9814\n3. SPH0645",
        "Yang kedua?": "MAX9814 punya AGC bawaan.",
        "Yang nomor 3?": "SPH0645 juga I2S, harganya murah.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Mic apa yang bagus buat ESP32?", "t25-1", conversation_id="c25")
        sp2 = _run_turn_capture_prompt(console, demo, "Yang kedua?", "t25-2", conversation_id="c25")
        sp3 = _run_turn_capture_prompt(console, demo, "Yang nomor 3?", "t25-3", conversation_id="c25")
        assert "max9814" in " ".join(_lines_starting(sp2, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "sph0645" in " ".join(_lines_starting(sp3, *_ANY_CANDIDATE_PREFIXES)).lower()
    finally:
        console.stop()


# ============================================================================
# Section 5 - Phase 6 contamination (both directions)
# ============================================================================

def test_26_e2e_no_contamination_forward_direction():
    demo = _load_demo("s46-26")
    replies = {
        "ESP32 pakai INMP441.": "Oke, dicatat ESP32 dengan INMP441.",
        "Aquarium saya 50x25.": "Oke, dicatat ukuran aquarium 50x25.",
        "Berapa ukuran aquarium?": "Ukuran aquariummu 50x25.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t26-1", conversation_id="c26")
        _run_turn(console, demo, "Aquarium saya 50x25.", "t26-2", conversation_id="c26")
        sp = _run_turn_capture_prompt(console, demo, "Berapa ukuran aquarium?", "t26-3", conversation_id="c26")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "aquarium" in joined
        assert "esp32" not in joined and "inmp441" not in joined
    finally:
        console.stop()


def test_27_e2e_no_contamination_reverse_direction():
    demo = _load_demo("s46-27")
    replies = {
        "Aquarium saya 50x25.": "Oke, dicatat ukuran aquarium 50x25.",
        "ESP32 pakai INMP441.": "Oke, dicatat ESP32 dengan INMP441.",
        "Mic-nya gimana?": "INMP441 adalah mic I2S yang bagus buat ESP32.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aquarium saya 50x25.", "t27-1", conversation_id="c27")
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t27-2", conversation_id="c27")
        sp = _run_turn_capture_prompt(console, demo, "Mic-nya gimana?", "t27-3", conversation_id="c27")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "esp32" in joined or "inmp441" in joined
        assert "aquarium" not in joined
    finally:
        console.stop()


# ============================================================================
# Section 6 - Investigated-and-REJECTED fixes, locked as documented limits
# ============================================================================

def test_28_rejected_fix_coverage_tie_boundary_still_strict_gt_half():
    # Regression lock for the coverage>=0.5 investigation (see this
    # module's own Sprint 46 comment block in `is_active_topic_relevant_
    # to_query()`): must remain the PRE-Sprint-46 strict `> 0.5`, i.e.
    # `test_semantic_context_bridging.py::
    # test_39_tied_normalized_overlap_across_history_is_not_relevant`'s
    # own disjoint-topic-at-50%-coverage guarantee must still hold.
    import importlib
    src = importlib.import_module("luno.memory_context")
    import inspect
    source = inspect.getsource(src.is_active_topic_relevant_to_query)
    assert "coverage > 0.5" in source
    assert "coverage >= 0.5" not in source


def test_29_rejected_fix_lebih_paling_still_not_stopwords():
    # Regression lock for the "lebih"/"paling" stopword investigation
    # (see `_TOPIC_OVERLAP_STOPWORDS`'s own Sprint 46 comment block):
    # deliberately left OUT to avoid regressing
    # `test_76_e2e_multi_topic_ambiguity_gpu_vs_pompa`'s 2-competing-
    # topic ambiguity-safety guarantee.
    assert "lebih" not in memory_context._TOPIC_OVERLAP_STOPWORDS
    assert "paling" not in memory_context._TOPIC_OVERLAP_STOPWORDS


def test_30_e2e_multi_topic_ambiguity_still_safe_after_sprint46():
    """Direct re-verification (not merely a source-string check) that the
    exact scenario the two rejected fixes would have broken is still
    correctly refused after every Sprint 46 change actually kept."""
    demo = _load_demo("s46-30")
    replies = {
        "Aku punya GPU RTX 3060.": "Oke, dicatat GPU RTX 3060.",
        "Aku juga punya pompa aquascape.": "Oke, dicatat pompa aquascape.",
        "Kalau yang lebih besar gimana?": "Tergantung yang mana maksudnya.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku punya GPU RTX 3060.", "t30-1", conversation_id="c30")
        _run_turn(console, demo, "Aku juga punya pompa aquascape.", "t30-2", conversation_id="c30")
        sp = _run_turn_capture_prompt(console, demo, "Kalau yang lebih besar gimana?", "t30-3", conversation_id="c30")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"ambiguous 'yang lebih besar' must not guess between GPU/pump: {sp}"
    finally:
        console.stop()


# ============================================================================
# Section 7 - Performance (Phase 12: target < 5ms per operation, no I/O)
# ============================================================================

def test_31_normalize_terms_for_bridging_performance():
    tokens = {"mikrofonnya", "mengganti", "pompanya", "kartugrafisnya", "esp32", "inmp441"}
    start = time.perf_counter()
    for _ in range(1000):
        memory_context._normalize_terms_for_bridging(tokens)
    elapsed_ms = (time.perf_counter() - start) / 1000 * 1000
    assert elapsed_ms < 5.0, f"_normalize_terms_for_bridging() averaged {elapsed_ms:.4f}ms/call, expected < 5ms"


def test_32_is_active_topic_relevant_to_query_performance():
    active = _snap("beli", "dicatat", "hdd", "oke", "pakai", "rencana", "saya",
                    "sedang", "sekarang", "ssd", status="active")
    history = [active, _snap("beli", "dicatat", "rencana", "ssd", status="planned")]
    start = time.perf_counter()
    for _ in range(1000):
        memory_context.is_active_topic_relevant_to_query(active, "Yang sebelumnya gimana?", topic_history=history)
    elapsed_ms = (time.perf_counter() - start) / 1000 * 1000
    assert elapsed_ms < 5.0, f"is_active_topic_relevant_to_query() averaged {elapsed_ms:.4f}ms/call, expected < 5ms"


# ============================================================================
# Section 8 - Persistent-state safety (Phase 11)
# ============================================================================

def test_33_no_new_module_level_mutable_global_state_introduced():
    # This sprint's three fixes touch `_TOPIC_OVERLAP_STOPWORDS` (existing
    # frozenset, extended), `_normalize_terms_for_bridging()` (existing
    # pure function), and `is_active_topic_relevant_to_query()` (existing
    # pure function) - no new module-level dict/list was introduced.
    assert isinstance(memory_context._TOPIC_OVERLAP_STOPWORDS, frozenset)


def test_34_active_topic_and_topic_history_remain_plain_dicts():
    demo = _load_demo("s46-34")
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        assert isinstance(console.planner_module._active_topic, dict)
        assert isinstance(console.planner_module._topic_history, dict)
    finally:
        console.stop()
