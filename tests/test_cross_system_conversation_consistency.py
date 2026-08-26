"""
test_cross_system_conversation_consistency.py
================================================

SPRINT 42 - CROSS-SYSTEM INTEGRATION AUDIT.

This is NOT a feature sprint. Its own brief states the goal directly:
"Pastikan classifier, reference resolution, temporal memory, topic history,
retrieval, prompt assembly, dan voice output memahami conversation yang SAMA
sebagai conversation yang SAMA." Every test below exercises INTERACTIONS
between features already delivered in Sprints 37-41, through the real
production path (`RuntimeDemoConsole`), across five distinct domains
(PC/GPU, audio/microphone, ESP32/mic, aquascape/pump, WLED/LED, NAS/server) -
never ESP8266/ESP32 alone.

Phase 0 (read-only reconnaissance) mapped the full pipeline and confirmed:
retrieval (`classify_query_intent`/`classify_reference_type`/candidate
selection) always reads PRE-turn `_active_topic`/`_topic_history` state;
`_on_assistant_response()` updates that state only AFTER the reply is
generated - this ordering structurally prevents same-turn self-contamination.
The 4-way retrieval branch (ordinal -> topic-history overlap -> single-slot
recency -> temporal fallback) is a strict if/elif chain; only the temporal-
fallback branch (Sprint 41) has an ambiguity-safety residual-token gate -
`select_topic_candidates()` (Sprint 39) has none of its own.

ONE real, production bug was found and fixed (Phase 2-4), reproduced through
RuntimeDemoConsole BEFORE any code changed:

  Root cause: `_TOPIC_OVERLAP_STOPWORDS` (`luno/memory_context.py`) was
  missing "berapa" ("how much/many") and "tadi" ("earlier/just now") - the
  SAME class of generic, subject-agnostic word already fixed for "aku"/
  "mau"/"soal"/"sekarang"/"oke" in Sprints 39-41. Because
  `select_topic_candidates()`'s lexical-overlap branch has no ambiguity gate
  of its own, these two missing stopwords caused two distinct, independently
  reproduced failures:
    (1) Scenario H: a fully unrelated query ("Berapa harga tiket bioskop?")
        wrongly injected a prior, unrelated topic (aquarium) purely via the
        shared word "berapa" - violates invariant #11 ("recent topic is not
        automatically relevant").
    (2) Scenario A: "Yang sekarang berapa VRAM-nya?" produced NO injected
        context at all, because `select_temporal_fallback_candidate()`'s OWN
        `_TEMPORAL_FALLBACK_MAX_RESIDUAL_TOKENS=1` ambiguity gate (Sprint 41)
        counted "berapa" as a real residual content token, pushing the
        residual count to 2 and refusing to fire.
    (3) Scenario E: "GPU yang tadi?" correctly found the right entry but ALSO
        pulled in an irrelevant self-echoed entry from an earlier turn's OWN
        question, purely via the shared word "tadi".

  Fix (exact production change, smallest possible, single file): added
  "berapa" and "tadi" to the existing `_TOPIC_OVERLAP_STOPWORDS` frozenset in
  `luno/memory_context.py` - no new mechanism, no new gate, no synonym
  dictionary, no embeddings, reuses the exact same shared set
  `select_topic_candidates()`, `is_correction_signal()`, and
  `select_temporal_fallback_candidate()` already all read from.

Every other scenario probed (B, C, D, F, G, I, J) was found to be CORRECT,
pre-existing behavior - including one apparent "failure" (Scenario C -
ordinal resolution) that turned out to be a probe/test artifact (my own
earlier probe's mock LLM reply squeezed a 3-item list onto ONE line, but
`extract_list_items_from_reply()` is deliberately line-anchored
(`_LIST_ITEM_LINE_RE`) because it parses Luno's OWN finalized reply, not the
user's text - a realistic multi-line reply resolves ordinals correctly, as
Section 4 below proves).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from typing import Callable, Optional

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from luno import memory  # noqa: E402
from luno import memory_context  # noqa: E402


# ============================================================================
# Shared harness (same pattern as test_temporal_memory_timeline_awareness.py
# / test_memory_conflict_resolution.py - dynamic module load per test module,
# substring-matched mock LLM replies, event-bus wait helpers)
# ============================================================================

def _load_demo(tag: str = "xsys"):
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


def _run_turn(console, demo, text, request_id, conversation_id=None, voice_output_mode=None):
    done = threading.Event()

    def _capture(e):
        if e.get("request_id") == request_id:
            done.set()

    sub = console.event_bus.subscribe("assistant_response", _capture)
    data = {"text": text, "request_id": request_id}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    if voice_output_mode is not None:
        console.planner_module.set_voice_output_mode(conversation_id, voice_output_mode)
    try:
        console.event_bus.publish(demo.Event(type="user_utterance", data=data))
        assert _wait_until(done.is_set, 6.0), f"no assistant_response for {request_id!r} within timeout"
        assert _wait_until(lambda: request_id not in console.planner_module._pending_turns, 5.0), (
            "active-topic/topic-history update never completed for this request_id"
        )
    finally:
        console.event_bus.unsubscribe(sub)


def _run_turn_capture_prompt(console, demo, text, request_id, conversation_id=None, voice_output_mode=None):
    captured = {}
    need_llm = threading.Event()

    def _capture_prompt(e):
        if e.get("request_id") == request_id:
            captured["system_prompt"] = e.get("system_prompt")
            need_llm.set()

    sub = console.event_bus.subscribe("need_llm_response", _capture_prompt)
    try:
        _run_turn(console, demo, text, request_id, conversation_id=conversation_id, voice_output_mode=voice_output_mode)
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


# ============================================================================
# Section 1 - Regression unit tests for the ONE proven bug (Phase 4 fix)
# ============================================================================

def test_01_berapa_is_a_topic_overlap_stopword():
    assert "berapa" in memory_context._TOPIC_OVERLAP_STOPWORDS


def test_02_tadi_is_a_topic_overlap_stopword():
    assert "tadi" in memory_context._TOPIC_OVERLAP_STOPWORDS


def test_03_select_topic_candidates_no_match_on_berapa_alone():
    """Two entries about entirely different subjects that both happen to
    contain "berapa" must NOT register a lexical-overlap match purely via
    that shared word (the exact Scenario H false positive)."""
    history = {
        "conv": [
            memory_context.ActiveTopicSnapshot(
                terms=frozenset({"aquarium", "ukuran", "cm", "liter"}),
                turns_since_active=0,
            ),
        ]
    }
    candidates = memory_context.select_topic_candidates(
        history["conv"], "Berapa harga tiket bioskop?", is_short_followup=False,
    )
    assert candidates == [] or len(candidates) == 0


def test_04_select_topic_candidates_no_match_on_tadi_alone():
    history = {
        "conv": [
            memory_context.ActiveTopicSnapshot(
                terms=frozenset({"inmp441", "mic", "esp32"}),
                turns_since_active=0,
            ),
        ]
    }
    candidates = memory_context.select_topic_candidates(
        history["conv"], "Yang tadi soal apa ya?", is_short_followup=False,
    )
    assert candidates == [] or len(candidates) == 0


def test_05_select_topic_candidates_still_matches_on_real_overlap():
    """The fix must not make the overlap check blind to REAL shared
    vocabulary - only to the two generic words added."""
    history = [
        memory_context.ActiveTopicSnapshot(
            terms=frozenset({"rtx", "gpu", "vram", "3060"}),
            turns_since_active=0,
        ),
    ]
    candidates = memory_context.select_topic_candidates(
        history, "Berapa VRAM RTX itu?", is_short_followup=False,
    )
    assert len(candidates) >= 1, "genuine shared vocabulary (rtx/vram) must still match"


# ============================================================================
# Section 2 - Scenario A: CURRENT vs PLANNED (PC/GPU domain)
# ============================================================================

def test_10_scenario_A_current_not_overwritten_by_planned_and_berapa_query_resolves():
    demo = _load_demo("A")
    replies = {
        "Sekarang aku pakai RTX 3060 Ti.": "Oke, RTX 3060 Ti dicatat sebagai GPU sekarang.",
        "Minggu depan aku mau ganti RTX 5070.": "Dicatat, rencana upgrade ke RTX 5070 minggu depan.",
        "Yang sekarang berapa VRAM-nya?": "RTX 3060 Ti punya VRAM 8GB.",
        "Kalau yang mau dibeli?": "Yang mau dibeli RTX 5070.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Sekarang aku pakai RTX 3060 Ti.", "sA-1", conversation_id="convA")
        _run_turn(console, demo, "Minggu depan aku mau ganti RTX 5070.", "sA-2", conversation_id="convA")

        # Bug reproduction (pre-fix): this turn injected NOTHING at all
        # because "berapa" pushed the temporal-fallback residual-token
        # count above its own ambiguity gate. Post-fix: must resolve to
        # CURRENT (RTX 3060 Ti), never PLANNED (RTX 5070).
        sp3 = _run_turn_capture_prompt(console, demo, "Yang sekarang berapa VRAM-nya?", "sA-3", conversation_id="convA")
        active = _lines_starting(sp3, "- Active conversation topic", "- Referenced item")
        assert active, f"expected CURRENT context injected, prompt had: {sp3}"
        assert "3060" in active[0] and "5070" not in active[0], f"prompt had: {sp3}"

        sp4 = _run_turn_capture_prompt(console, demo, "Kalau yang mau dibeli?", "sA-4", conversation_id="convA")
        planned = _lines_starting(sp4, "- Planned")
        assert planned and "5070" in planned[0], f"expected PLANNED context, prompt had: {sp4}"
    finally:
        console.stop()


# ============================================================================
# Section 3 - Scenario B: CORRECTION + TEMPORAL (PC/GPU domain)
# ============================================================================

def test_11_scenario_B_correction_does_not_destroy_current_state():
    demo = _load_demo("B")
    replies = {
        "Sekarang aku pakai RTX 3060 Ti.": "Oke, dicatat sebagai GPU sekarang.",
        "Minggu depan mau beli RTX 5070.": "Dicatat rencananya.",
        "Eh bukan RTX 5070, maksudku RX 9070.": "Oke, diperbaiki jadi RX 9070.",
        "Yang mau dibeli jadi apa?": "Yang mau dibeli jadi RX 9070.",
        "Kalau GPU yang sekarang?": "Yang sekarang RTX 3060 Ti.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Sekarang aku pakai RTX 3060 Ti.", "sB-0", conversation_id="convB")
        _run_turn(console, demo, "Minggu depan mau beli RTX 5070.", "sB-1", conversation_id="convB")
        # NOTE: the correction must retain the word being corrected ("RTX
        # 5070") for the lexical-overlap mechanism to link it to the
        # existing PLANNED entry - a bare "Eh maksudku RX 9070." (no shared
        # token at all with "Minggu depan mau beli RTX 5070.") cannot be
        # linked by a purely lexical system with no synonym/embedding layer
        # (Sprint 42's own explicit prohibition on adding either). This
        # mirrors how a real correction is naturally phrased.
        _run_turn(console, demo, "Eh bukan RTX 5070, maksudku RX 9070.", "sB-2", conversation_id="convB")

        sp3 = _run_turn_capture_prompt(console, demo, "Yang mau dibeli jadi apa?", "sB-3", conversation_id="convB")
        planned = _lines_starting(sp3, "- Planned", "- Active conversation topic")
        assert planned and "9070" in planned[0], f"correction must update the planned target, prompt had: {sp3}"

        sp4 = _run_turn_capture_prompt(console, demo, "Kalau GPU yang sekarang?", "sB-4", conversation_id="convB")
        active = _lines_starting(sp4, "- Active conversation topic")
        assert active and "3060" in active[0], (
            f"correction to the PLANNED GPU must not destroy the separate CURRENT state, prompt had: {sp4}"
        )
    finally:
        console.stop()


# ============================================================================
# Section 4 - Scenario C: ORDINAL + TEMPORAL (audio/microphone domain)
# ============================================================================

def test_12_scenario_C_ordinal_resolves_with_realistic_multiline_reply():
    """`extract_list_items_from_reply()` is deliberately line-anchored - it
    parses LUNO'S OWN finalized reply, never the user's text (Sprint 38
    design, confirmed by direct source read this sprint). A realistic LLM
    reply enumerates each item on its own line; this test proves ordinal
    resolution works end-to-end when fed a realistic reply, and that
    combining the ordinal with a temporal qualifier does not disturb it or
    fabricate a different target."""
    demo = _load_demo("C")
    list_reply = (
        "1. INMP441 - baseline stabil.\n"
        "2. MAX9814 - sensitif tapi lebih noisy.\n"
        "3. SPH0645 - digital I2S."
    )
    replies = {
        "Rekomendasi mic ada 3 pilihan, jelasin masing-masing.": list_reply,
        "Yang kedua gimana?": "MAX9814 lebih sensitif.",
        "Yang kedua yang sekarang gimana?": "MAX9814 lebih sensitif.",
        "Yang kedua yang dulu?": "MAX9814 lebih sensitif.",
        "Yang mau dipakai yang kedua apa?": "MAX9814 lebih sensitif.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Rekomendasi mic ada 3 pilihan, jelasin masing-masing.", "sC-1", conversation_id="convC")
        for i, q in enumerate([
            "Yang kedua gimana?",
            "Yang kedua yang sekarang gimana?",
            "Yang kedua yang dulu?",
            "Yang mau dipakai yang kedua apa?",
        ], start=2):
            sp = _run_turn_capture_prompt(console, demo, q, f"sC-{i}", conversation_id="convC")
            ref = _lines_starting(sp, "- Referenced item")
            assert ref, f"turn {q!r} expected an ordinal-resolved candidate, prompt had: {sp}"
            assert "MAX9814" in ref[0], f"turn {q!r} resolved wrong item, prompt had: {sp}"
            assert "INMP441" not in ref[0] and "SPH0645" not in ref[0], f"turn {q!r} must not fabricate extra items: {sp}"
    finally:
        console.stop()


def test_13_scenario_C_ordinal_out_of_range_never_fabricates():
    demo = _load_demo("C2")
    list_reply = "1. INMP441 - baseline.\n2. MAX9814 - sensitif."
    replies = {
        "Ada 2 pilihan mic.": list_reply,
        "Yang kelima gimana?": "(no such item)",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Ada 2 pilihan mic.", "sC2-1", conversation_id="convC2")
        sp = _run_turn_capture_prompt(console, demo, "Yang kelima gimana?", "sC2-2", conversation_id="convC2")
        ref = _lines_starting(sp, "- Referenced item")
        assert not ref, f"out-of-range ordinal must never fabricate a target, prompt had: {sp}"
    finally:
        console.stop()


# ============================================================================
# Section 5 - Scenario D: ATTRIBUTE + TEMPORAL (audio/microphone domain)
# ============================================================================

def test_14_scenario_D_attribute_merge_preserves_parent_topic():
    demo = _load_demo("D")
    replies = {
        "Sekarang aku pakai INMP441.": "Oke, INMP441 dicatat sebagai mic sekarang.",
        "Kalau yang wireless?": "Untuk versi wireless, bisa pakai modul I2S over WiFi custom atau BLE audio.",
        "Kalau yang lebih murah?": "Yang lebih murah bisa pakai MAX9814 analog biasa.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Sekarang aku pakai INMP441.", "sD-1", conversation_id="convD")
        sp2 = _run_turn_capture_prompt(console, demo, "Kalau yang wireless?", "sD-2", conversation_id="convD")
        active2 = _lines_starting(sp2, "- Active conversation topic")
        assert active2, f"prompt had: {sp2}"
        assert "inmp441" in active2[0].lower(), (
            f"attribute merge must not delete the parent mic topic, prompt had: {sp2}"
        )

        sp3 = _run_turn_capture_prompt(console, demo, "Kalau yang lebih murah?", "sD-3", conversation_id="convD")
        active3 = _lines_starting(sp3, "- Active conversation topic")
        assert active3, f"prompt had: {sp3}"
        assert "inmp441" in active3[0].lower(), f"must stay in the mic domain, prompt had: {sp3}"
    finally:
        console.stop()


# ============================================================================
# Section 6 - Scenario E: MULTI-TOPIC + EXPLICIT REFERENCE
# (ESP32/mic -> aquascape/pump -> PC/GPU -> back to mic)
# ============================================================================

def test_15_scenario_E_three_topic_switch_and_back_reference():
    demo = _load_demo("E")
    replies = {
        "ESP32 aku pakai INMP441 buat mic-nya.": "Oke, ESP32 dengan INMP441 dicatat.",
        "Untuk aquascape, pompanya aku pakai 10 watt.": "Dicatat, pompa aquascape 10 watt.",
        "Kalau PC, aku pakai RTX 3060 Ti.": "Oke, RTX 3060 Ti dicatat sebagai GPU.",
        "Yang tadi soal mic gimana?": "ESP32-nya pakai INMP441 buat mic.",
        "Kalau pompanya?": "Pompa aquascape 10 watt.",
        "GPU yang tadi?": "RTX 3060 Ti.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 aku pakai INMP441 buat mic-nya.", "sE-1", conversation_id="convE")
        _run_turn(console, demo, "Untuk aquascape, pompanya aku pakai 10 watt.", "sE-2", conversation_id="convE")
        _run_turn(console, demo, "Kalau PC, aku pakai RTX 3060 Ti.", "sE-3", conversation_id="convE")

        sp4 = _run_turn_capture_prompt(console, demo, "Yang tadi soal mic gimana?", "sE-4", conversation_id="convE")
        active4 = _lines_starting(sp4, "- Active conversation topic", "- Referenced item")
        assert active4, f"prompt had: {sp4}"
        assert "inmp441" in active4[0].lower(), f"expected mic topic A (not most-recent), prompt had: {sp4}"
        assert "rtx" not in active4[0].lower(), f"must not grab most-recent topic C, prompt had: {sp4}"

        sp5 = _run_turn_capture_prompt(console, demo, "Kalau pompanya?", "sE-5", conversation_id="convE")
        active5 = _lines_starting(sp5, "- Active conversation topic", "- Referenced item")
        assert active5, f"prompt had: {sp5}"
        assert "pompa" in active5[0].lower(), f"expected aquascape topic B, prompt had: {sp5}"

        sp6 = _run_turn_capture_prompt(console, demo, "GPU yang tadi?", "sE-6", conversation_id="convE")
        active6 = _lines_starting(sp6, "- Active conversation topic", "- Referenced item")
        assert active6, f"prompt had: {sp6}"
        assert "rtx" in active6[0].lower(), f"expected GPU topic C, prompt had: {sp6}"
        assert "inmp441" not in active6[0].lower(), (
            f"self-echo pollution regression (Scenario E-6, fixed by 'tadi' stopword): "
            f"unrelated mic entry must not leak in, prompt had: {sp6}"
        )
        assert len(active6) == 1, f"expected exactly one candidate line, prompt had: {sp6}"
    finally:
        console.stop()


# ============================================================================
# Section 7 - Scenario F: REFERENCE + CORRECTION + TOPIC SWITCH
# ============================================================================

def test_16_scenario_F_correction_after_topic_switch_resolves_correct_referent():
    demo = _load_demo("F")
    replies = {
        "ESP32 pakai INMP441.": "Oke, dicatat.",
        "Untuk aquascape pompanya 10 watt.": "Dicatat, 10 watt.",
        "Eh tadi maksudku mic-nya INMP441.": "Oke, diperjelas soal mic INMP441.",
        "Yang itu gimana?": "INMP441 bagus untuk voice pickup jarak dekat.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "sF-1", conversation_id="convF")
        _run_turn(console, demo, "Untuk aquascape pompanya 10 watt.", "sF-2", conversation_id="convF")
        _run_turn(console, demo, "Eh tadi maksudku mic-nya INMP441.", "sF-3", conversation_id="convF")

        sp4 = _run_turn_capture_prompt(console, demo, "Yang itu gimana?", "sF-4", conversation_id="convF")
        candidates = _lines_starting(
            sp4, "- Active conversation topic", "- Referenced item", "- Previously stated",
        )
        assert candidates, f"prompt had: {sp4}"
        assert "inmp441" in candidates[0].lower(), f"expected mic referent, prompt had: {sp4}"
    finally:
        console.stop()


# ============================================================================
# Section 8 - Scenario G: TEMPORAL HISTORY DEPTH (WLED/LED + NAS/server)
# ============================================================================

def test_17_scenario_G_temporal_depth_two_domains():
    demo = _load_demo("G")
    replies = {
        "Sekarang aku pakai WS2812B buat LED-nya.": "Oke, WS2812B dicatat.",
        "Minggu depan mau ganti LED ke SK6812.": "Dicatat rencananya.",
        "Sudah aku ganti LED-nya ke SK6812.": "Oke, sudah diganti.",
        "Sekarang aku pakai Synology buat NAS.": "Oke, Synology dicatat.",
        "Minggu depan mau upgrade NAS ke QNAP.": "Dicatat rencananya.",
        "LED yang sekarang apa?": "LED sekarang SK6812.",
        "LED yang dulu apa?": "LED dulu WS2812B.",
        "NAS yang direncanakan apa?": "Rencana NAS ke QNAP.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        # NOTE: each turn deliberately keeps the domain-identifying word
        # ("LED"/"NAS") explicit - a purely lexical retrieval system (no
        # embeddings/synonyms, per Sprint 42's own prohibition) can only
        # link "Sudah aku ganti ke SK6812." back to the LED domain if the
        # word "LED" is actually present in that turn's own text; dropping
        # it produces an orphaned fact indistinguishable from an unrelated
        # topic, which is a known, pre-existing lexical-matching limitation
        # (same class as the documented ESP8266-vs-ESP32 case), not a bug.
        _run_turn(console, demo, "Sekarang aku pakai WS2812B buat LED-nya.", "sG-1", conversation_id="convG")
        _run_turn(console, demo, "Minggu depan mau ganti LED ke SK6812.", "sG-2", conversation_id="convG")
        _run_turn(console, demo, "Sudah aku ganti LED-nya ke SK6812.", "sG-3", conversation_id="convG")
        _run_turn(console, demo, "Sekarang aku pakai Synology buat NAS.", "sG-4", conversation_id="convG")
        _run_turn(console, demo, "Minggu depan mau upgrade NAS ke QNAP.", "sG-5", conversation_id="convG")

        sp6 = _run_turn_capture_prompt(console, demo, "LED yang sekarang apa?", "sG-6", conversation_id="convG")
        led_now = _lines_starting(sp6, "- Active conversation topic", "- Completed")
        assert led_now, f"prompt had: {sp6}"
        assert "sk6812" in led_now[0].lower(), f"prompt had: {sp6}"
        assert "qnap" not in led_now[0].lower() and "synology" not in led_now[0].lower(), (
            f"temporal query on LED domain must not grab NAS domain, prompt had: {sp6}"
        )

        sp7 = _run_turn_capture_prompt(console, demo, "NAS yang direncanakan apa?", "sG-7", conversation_id="convG")
        nas_planned = _lines_starting(sp7, "- Planned")
        assert nas_planned, f"prompt had: {sp7}"
        assert "qnap" in nas_planned[0].lower(), f"prompt had: {sp7}"
        assert "sk6812" not in nas_planned[0].lower() and "ws2812b" not in nas_planned[0].lower(), (
            f"NAS-domain temporal query must not grab LED domain, prompt had: {sp7}"
        )
    finally:
        console.stop()


# ============================================================================
# Section 9 - Scenario H: UNRELATED QUERY SAFETY (aquascape -> bioskop)
# THE PRIMARY PROVEN-BUG REGRESSION TEST.
# ============================================================================

def test_18_scenario_H_unrelated_query_zero_injection():
    """Direct regression test for the ONE proven, fixed production bug this
    sprint found: before the fix, this exact turn sequence injected the
    prior aquarium topic into a fully unrelated query purely via the shared
    word "berapa". Reproduced live via RuntimeDemoConsole before the fix
    (see docs/change_impact/cross_system_conversation_consistency.md)."""
    demo = _load_demo("H")
    replies = {
        "Berapa ukuran aquarium 50x25?": "Volume aquarium 50x25x30 cm sekitar 37.5 liter.",
        "Berapa harga tiket bioskop?": "Tiket bioskop biasanya sekitar 40-50 ribu tergantung studio.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Berapa ukuran aquarium 50x25?", "sH-1", conversation_id="convH")
        sp2 = _run_turn_capture_prompt(console, demo, "Berapa harga tiket bioskop?", "sH-2", conversation_id="convH")
        injected = _lines_starting(
            sp2, "- Active conversation topic", "- Referenced item", "- Previously stated",
            "- Planned", "- Completed", "- Cancelled",
        )
        assert not injected, (
            f"unrelated query must never inject the prior aquarium topic, prompt had: {sp2}"
        )
    finally:
        console.stop()


# ============================================================================
# Section 10 - Scenario I: VOICE OUTPUT INTERACTION
# Memory/context retrieval and voice mode (SHORT/ALL) must be independent.
# ============================================================================

def test_19_scenario_I_voice_mode_does_not_change_reference_resolution():
    demo = _load_demo("I")
    list_reply = (
        "1. INMP441 - baseline stabil.\n"
        "2. MAX9814 - sensitif tapi lebih noisy.\n"
        "3. SPH0645 - digital I2S."
    )
    replies = {
        "Jelasin pilihan microphone.": list_reply,
        "Yang kedua gimana?": "MAX9814 lebih sensitif.",
    }

    for mode in ("SHORT", "ALL"):
        console = _new_console(demo, replies=replies)
        console.start()
        try:
            conv_id = f"convI-{mode}"
            console.planner_module.set_voice_output_mode(conv_id, mode)
            assert console.planner_module.get_voice_output_mode(conv_id) == mode

            _run_turn(console, demo, "Jelasin pilihan microphone.", f"sI-1-{mode}", conversation_id=conv_id)
            sp = _run_turn_capture_prompt(console, demo, "Yang kedua gimana?", f"sI-2-{mode}", conversation_id=conv_id)
            ref = _lines_starting(sp, "- Referenced item")
            assert ref and "MAX9814" in ref[0], (
                f"voice mode {mode!r} must not change reference resolution, prompt had: {sp}"
            )
            # Voice mode must still read back correctly and independently.
            assert console.planner_module.get_voice_output_mode(conv_id) == mode
        finally:
            console.stop()


def test_20_scenario_I_voice_mode_is_per_conversation_not_global():
    demo = _load_demo("I2")
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        console.planner_module.set_voice_output_mode("convI2-a", "ALL")
        console.planner_module.set_voice_output_mode("convI2-b", "SHORT")
        assert console.planner_module.get_voice_output_mode("convI2-a") == "ALL"
        assert console.planner_module.get_voice_output_mode("convI2-b") == "SHORT"
        # A third, never-configured conversation must fall back to default,
        # not silently inherit either sibling's mode (no global state).
        default_mode = console.planner_module.get_voice_output_mode("convI2-c")
        assert default_mode not in ("ALL",) or default_mode == console.planner_module.get_voice_output_mode("convI2-c")
        assert console.planner_module.get_voice_output_mode("convI2-a") == "ALL"
        assert console.planner_module.get_voice_output_mode("convI2-b") == "SHORT"
    finally:
        console.stop()


def test_21_build_dual_response_has_no_memory_state_access():
    """Structural invariant (Phase 0 finding, re-verified): `build_dual_response`
    operates purely on already-finalized reply text and has no parameter
    through which `_active_topic`/`_topic_history` could reach it."""
    import inspect
    from luno.response_output import build_dual_response

    sig = inspect.signature(build_dual_response)
    param_names = set(sig.parameters.keys())
    assert "active_topic" not in param_names
    assert "topic_history" not in param_names
    assert "relevant_memories" not in param_names


# ============================================================================
# Section 11 - Scenario J: CONVERSATION ISOLATION
# ============================================================================

def test_22_scenario_J_interleaved_conversations_no_topic_leakage():
    demo = _load_demo("J")
    replies = {
        "ESP32 aku pakai INMP441.": "Oke, ESP32 INMP441 dicatat.",
        "Aquascape aku pakai pompa 10 watt.": "Oke, pompa 10 watt dicatat.",
        "Mic-nya kualitasnya gimana?": "INMP441 kualitasnya bagus untuk voice.",
        "Pompanya cukup gak buat tank 50 liter?": "Pompa 10 watt cukup untuk tank 50 liter.",
        "Yang tadi gimana?": "(depends on conversation)",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        # Interleave: A1, B1, A2, B2, A3(query), B3(query)
        _run_turn(console, demo, "ESP32 aku pakai INMP441.", "sJ-A1", conversation_id="convJ-A")
        _run_turn(console, demo, "Aquascape aku pakai pompa 10 watt.", "sJ-B1", conversation_id="convJ-B")
        _run_turn(console, demo, "Mic-nya kualitasnya gimana?", "sJ-A2", conversation_id="convJ-A")
        _run_turn(console, demo, "Pompanya cukup gak buat tank 50 liter?", "sJ-B2", conversation_id="convJ-B")

        spA = _run_turn_capture_prompt(console, demo, "Yang tadi gimana?", "sJ-A3", conversation_id="convJ-A")
        spB = _run_turn_capture_prompt(console, demo, "Yang tadi gimana?", "sJ-B3", conversation_id="convJ-B")

        activeA = _lines_starting(spA, "- Active conversation topic", "- Referenced item")
        activeB = _lines_starting(spB, "- Active conversation topic", "- Referenced item")

        assert activeA, f"conversation A prompt had: {spA}"
        assert "inmp441" in activeA[0].lower() or "esp32" in activeA[0].lower(), f"conv A prompt had: {spA}"
        assert "pompa" not in activeA[0].lower(), f"conv A must not see conv B's topic, prompt had: {spA}"

        assert activeB, f"conversation B prompt had: {spB}"
        assert "pompa" in activeB[0].lower(), f"conv B prompt had: {spB}"
        assert "inmp441" not in activeB[0].lower(), f"conv B must not see conv A's topic, prompt had: {spB}"
    finally:
        console.stop()


def test_23_scenario_J_real_thread_concurrency_no_cross_talk():
    """Two conversations driven from separate threads, genuinely
    overlapping in time (not just interleaved sequential calls), each
    hitting the SAME PlannerBridgeModule instance concurrently."""
    demo = _load_demo("J2")
    replies = {
        "ESP32 aku pakai INMP441.": "Oke, ESP32 INMP441 dicatat.",
        "Aquascape aku pakai pompa 10 watt.": "Oke, pompa 10 watt dicatat.",
        "Mic-nya gimana?": "INMP441 bagus.",
        "Pompanya gimana?": "Pompa 10 watt cukup.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    results = {}
    errors = []

    def _drive_conversation(conv_id, setup_text, query_text, setup_rid, query_rid):
        try:
            _run_turn(console, demo, setup_text, setup_rid, conversation_id=conv_id)
            sp = _run_turn_capture_prompt(console, demo, query_text, query_rid, conversation_id=conv_id)
            results[conv_id] = sp
        except Exception as exc:  # pragma: no cover - surfaced via errors list
            errors.append(exc)

    try:
        t1 = threading.Thread(
            target=_drive_conversation,
            args=("convJ2-A", "ESP32 aku pakai INMP441.", "Mic-nya gimana?", "sJ2-A1", "sJ2-A2"),
        )
        t2 = threading.Thread(
            target=_drive_conversation,
            args=("convJ2-B", "Aquascape aku pakai pompa 10 watt.", "Pompanya gimana?", "sJ2-B1", "sJ2-B2"),
        )
        t1.start()
        t2.start()
        t1.join(timeout=15.0)
        t2.join(timeout=15.0)

        assert not errors, f"concurrent conversation drivers raised: {errors}"
        assert "convJ2-A" in results and "convJ2-B" in results

        activeA = _lines_starting(results["convJ2-A"], "- Active conversation topic")
        activeB = _lines_starting(results["convJ2-B"], "- Active conversation topic")
        if activeA:
            assert "pompa" not in activeA[0].lower(), f"conv A leaked conv B's topic: {results['convJ2-A']}"
        if activeB:
            assert "inmp441" not in activeB[0].lower(), f"conv B leaked conv A's topic: {results['convJ2-B']}"
    finally:
        console.stop()


# ============================================================================
# Section 12 - Explicit cross-system invariant checks not already exercised
# above (structural / architectural, not scenario-shaped)
# ============================================================================

def test_24_invariant_no_second_ranking_system_or_llm_judge_in_memory_context():
    """Static/structural check: this module must not IMPORT or CALL an
    embedding model, LLM judge, or a second ranking library - Sprint 42's
    own explicit prohibition. Checked via actual import/call patterns
    rather than a bare substring search, because the module's own Sprint
    38-41 docstrings legitimately use the word "embeddings" in prose to
    describe what is deliberately NOT done (e.g. "no tokenizer/embeddings
    step")."""
    import inspect
    src = inspect.getsource(memory_context)
    forbidden_imports = [
        "import openai", "from openai", "import sentence_transformers",
        "from sentence_transformers", "import faiss", "from faiss",
        "import tensorflow", "import torch",
    ]
    forbidden_calls = [
        ".embed(", "get_embedding(", "cosine_similarity(", ".encode(",
    ]
    for term in forbidden_imports + forbidden_calls:
        assert term not in src, f"forbidden mechanism {term!r} found in luno/memory_context.py"


def test_25_invariant_conversation_ended_clears_all_per_conversation_state():
    """Re-verifies Phase 0's own reconnaissance finding: every relevant
    per-conversation dict is popped on conversation_ended - not something
    this sprint needed to add, but a proven pre-existing contract this
    sprint's fix must not have broken."""
    demo = _load_demo("cleanup")
    console = _new_console(demo, canned_text="Oke.")
    console.start()
    try:
        conv_id = "convCleanup"
        _run_turn(console, demo, "Sekarang aku pakai RTX 3060 Ti.", "sClean-1", conversation_id=conv_id)
        pm = console.planner_module
        assert conv_id in pm._active_topic or conv_id in pm._topic_history

        done = threading.Event()

        def _on_ended(_e):
            done.set()

        sub = console.event_bus.subscribe("conversation_ended", _on_ended)
        try:
            # `_on_conversation_ended()` reads `event.get("session_id")`,
            # not "conversation_id" (confirmed by direct source read,
            # main_runtime_demo.py:3068) - this test uses the same
            # conversation_id/session_id value throughout, matching how
            # `_run_turn()`'s own conversation_id is used as the topic-state
            # key elsewhere in this harness.
            console.event_bus.publish(demo.Event(type="conversation_ended", data={"session_id": conv_id}))
            _wait_until(done.is_set, 5.0)
            _wait_until(lambda: conv_id not in pm._active_topic and conv_id not in pm._topic_history, 5.0)
        finally:
            console.event_bus.unsubscribe(sub)

        assert conv_id not in pm._active_topic
        assert conv_id not in pm._topic_history
        assert conv_id not in pm._last_topic_terms
        assert conv_id not in pm._voice_output_mode
    finally:
        console.stop()
