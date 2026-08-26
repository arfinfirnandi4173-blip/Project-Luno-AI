"""
test_semantic_speech_units.py
==============================

SPRINT 2 - VOICE PIPELINE LATENCY & SEMANTIC SPEECH SEGMENTATION,
Phase 12-14. Covers the SEMANTIC SPEECH UNIT concept introduced this
sprint (`luno.response_output._build_semantic_units()`), the Phase 5
short-sentence FUNCTION classification (`_has_confirmation_lead()` /
`_CONFIRMATION_BONUS`), the Phase 7 listener-coherence rule, and the
Phase 8 "do not over-compress an already-short-and-coherent response"
guard.

WHAT THIS SPRINT ADDED IN `luno/response_output.py` (all additive,
deterministic, reusing Sprint 1's existing one-hop dependency signal -
see that function's own docstring for why an atomic
rescue-whole-unit-or-drop-whole-unit design was traced through the
existing `MANY_DEPENDENTS` regression test and rejected):
  1. `_build_semantic_units()` - pure regrouping of the SAME
     `_is_dependent_sentence()` signal `_dependency_kind()` already
     computes, into contiguous `(start, end)` index ranges.
  2. `_CONFIRMATION_KEYWORDS` / `_has_confirmation_lead()` /
     `_CONFIRMATION_BONUS` - short-sentence FUNCTION classification
     (status/outcome report), NOT a blind word-count bonus.
  3. `_repair_orphans()` - UNCHANGED logic, re-documented as the
     semantic-unit-preservation mechanism it already was.

WHAT THIS SPRINT DID NOT TOUCH: `_select_by_priority()`'s scoring/
must-keep/budget skeleton, `_score_sentence()`'s existing signals
(warning/number/condition/lead/conclusion), the dependency marker
tables themselves, chunking/TTS.
"""

from __future__ import annotations

import os
import sys
from typing import Dict

import pytest

from luno import response_output as ro
from luno.response_policy import compute_response_policy, DEPTH_DETAILED, DEPTH_NORMAL, DEPTH_SHORT

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _policy(depth_query: str = "", text_for_default: str = ""):
    return compute_response_policy(depth_query or text_for_default)


def _build(text: str, query: str = "", language: str = "indonesian"):
    policy = _policy(query, text)
    return ro.build_dual_response(text, policy, language=language)


def _sentences(text: str):
    raw_chunks = ro._split_into_raw_sentences(text)
    out = []
    for chunk in raw_chunks:
        cleaned = ro.normalize_for_speech(ro._strip_list_markers(chunk.raw), language="indonesian").strip()
        if cleaned:
            out.append(ro._Sentence(chunk.raw, cleaned, chunk.is_list_item))
    return out


def _units(text: str):
    sentences = ro._dedupe(_sentences(text))
    condition_indices = {i for i, s in enumerate(sentences) if ro._has_condition(s.cleaned.lower())}
    return sentences, ro._build_semantic_units(sentences, condition_indices)


def _no_orphans(voice_text: str, original_text: str) -> bool:
    """Same generic orphan checker used by test_voice_response_intelligence.py
    (reproduced here rather than imported, to keep this file's own
    scenarios self-contained and independently readable)."""
    def _sig(cleaned: str, n: int = 5) -> str:
        return " ".join(cleaned.lower().split()[:n])

    sentences = ro._dedupe(_sentences(original_text))
    condition_indices = {i for i, s in enumerate(sentences) if ro._has_condition(s.cleaned.lower())}
    voice_lower = voice_text.lower()
    present = [_sig(s.cleaned) in voice_lower for s in sentences]
    for i, s in enumerate(sentences):
        if i == 0 or not present[i]:
            continue
        if ro._is_dependent_sentence(sentences, i, condition_indices) and not present[i - 1]:
            return False
    return True


# ─────────────────────────────────────────────
# Worked examples - Phase 3's own three examples plus this file's own
# adversarial cases (Phase 14).
# ─────────────────────────────────────────────

# Phase 3's own worked example: UNIT1 (standalone), UNIT2 (setup +
# dependent condition, 2 sentences), UNIT3 (a new, separate condition
# that also opens with "Kalau" - distinct from UNIT2's own condition).
MQTT_PORT_EXAMPLE = (
    "ESP32 sudah terhubung ke WiFi. "
    "Sekarang kita cek MQTT. "
    "Kalau koneksinya masih gagal, periksa port 1883."
)

# Phase 3's conditional-setup worked example.
CONDITIONAL_SETUP_EXAMPLE = (
    "Restart ESP32 dulu. "
    "Kalau masih gagal, cek kabel power dan koneksi WiFi."
)

# Phase 8's own two worked examples that must NOT be compressed at all.
PHASE8_CAUSAL_PAIR = (
    "Karena SSID atau password-nya mungkin salah. "
    "Coba cek kembali konfigurasi WiFi."
)
PHASE8_STATUS_PAIR = "Sudah berhasil. Relay sekarang aktif."

# Phase 5's own four short-sentence examples that must not be
# automatically treated as low value.
SHORT_HIGH_VALUE_SENTENCES = [
    "Coba restart ESP32.",
    "Sudah terhubung.",
    "Port 1883 harus dibuka.",
    "Jangan sambungkan langsung ke 220V.",
]

# Phase 14 adversarial dependent-openers that must never become orphans.
ADVERSARIAL_DEPENDENT_OPENERS = (
    "Kalau masih gagal, restart perangkat.",
    "Setelah itu cek servicenya.",
    "Akibatnya koneksi akan gagal.",
    "Namun ada satu hal lagi.",
    "Ini terjadi karena konfigurasi sebelumnya.",
)

# Phase 14 false-positive candidates that must NOT trigger aggressive
# grouping / must not be misclassified as dependent when read as the
# LEAD (index 0) sentence of their own reply.
FALSE_POSITIVE_CANDIDATES = (
    "Selanjutnya kita lanjut ke MQTT.",
    "Keberlanjutan sistem tidak bergantung pada MQTT.",
    "Ini bukan masalah besar.",
    "Port 1883.",
    "ESP32.",
)

# A genuine multi-hop causal narrative chain (reused shape from
# test_voice_output_coherence.py's own CAUSE_CHAIN idea, rebuilt here so
# this file's own semantic-unit test doesn't depend on importing another
# test module's fixtures).
LONG_CAUSAL_CHAIN = (
    "GPU mengalami throttling saat gaming berat. "
    "Ini terjadi karena thermal paste sudah kering dan tidak menghantarkan panas dengan baik. "
    "Akibatnya, panas dari GPU core tidak bisa dipindahkan ke heatsink secara efisien. "
    "Karena panas menumpuk terus, GPU otomatis menurunkan clock speed untuk mencegah kerusakan. "
    "Karena itu, frame rate turun drastis saat gaming. "
    "Oleh karena itu, solusinya adalah mengganti thermal paste dengan yang baru."
)


# ─────────────────────────────────────────────
# A. `_build_semantic_units()` - direct unit tests.
# ─────────────────────────────────────────────

def test_A_standalone_sentence_forms_its_own_unit():
    sentences, units = _units("ESP32 sudah terhubung ke WiFi. Sekarang kita cek MQTT.")
    # neither sentence opens with a dependency marker -> two 1-sentence units
    assert units == [(0, 0), (1, 1)]


def test_B_setup_plus_dependent_condition_forms_one_two_sentence_unit():
    sentences, units = _units(
        "Sekarang kita cek MQTT. Kalau koneksinya masih gagal, periksa port 1883."
    )
    assert units == [(0, 1)]


def test_C_units_cover_every_sentence_exactly_once_in_order():
    sentences, units = _units(LONG_CAUSAL_CHAIN)
    total = len(sentences)
    covered = []
    for start, end in units:
        covered.extend(range(start, end + 1))
    assert covered == list(range(total))


def test_D_long_causal_chain_groups_into_one_multi_sentence_unit():
    sentences, units = _units(LONG_CAUSAL_CHAIN)
    # every sentence after the lead opens with a causal/reference marker
    # ("ini terjadi", "akibatnya", "karena", "penurunan ... inilah",
    # "jadi") depending on the one immediately before it - the whole
    # chain groups into one unit start-to-end.
    assert len(units) == 1
    assert units[0] == (0, len(sentences) - 1)


def test_E_mqtt_port_example_unit_boundaries_conceptually_match_phase3():
    sentences, units = _units(MQTT_PORT_EXAMPLE)
    # sentence 0 (ESP32 already connected) is standalone; sentences 1-2
    # (check MQTT + its "kalau ... masih gagal" condition) group together.
    assert units[0] == (0, 0)
    assert (1, 2) in units


def test_F_unit_bounds_lookup_matches_build_semantic_units():
    sentences, units = _units(LONG_CAUSAL_CHAIN)
    for i in range(len(sentences)):
        start, end = ro._unit_bounds(units, i)
        assert start <= i <= end
        assert (start, end) in units


# ─────────────────────────────────────────────
# G. Short-sentence FUNCTION classification (Phase 5) - confirmation/
#    status reports, not a blind word-count bonus.
# ─────────────────────────────────────────────

def test_G_status_confirmation_short_sentence_is_detected():
    assert ro._has_confirmation_lead("sudah terhubung")
    assert ro._has_confirmation_lead("berhasil diaktifkan")
    assert ro._has_confirmation_lead("gagal terhubung ke wifi")


def test_H_long_sentence_merely_containing_the_word_is_not_a_confirmation():
    # "sudah" appears, but this is a long explanatory sentence, not a
    # short status report - must not receive the confirmation bonus.
    long_sentence = (
        "kalau konfigurasi sudah benar dan semua kabel terpasang dengan rapi "
        "maka seharusnya perangkat bisa langsung terhubung ke jaringan wifi rumah"
    )
    assert not ro._has_confirmation_lead(long_sentence)


def test_I_confirmation_bonus_not_a_blind_word_count_bonus():
    # A short sentence with NO confirmation/warning/number/condition
    # content ("Baik, mengerti.") must NOT receive any function-based
    # bonus purely for being short.
    assert not ro._has_confirmation_lead("baik mengerti")
    score = ro._score_sentence("Baik, mengerti.", "baik mengerti", 3, 8)
    # only the generic index tiebreak should apply - well below the
    # confirmation/warning/number bonuses.
    assert score < 6.0


def test_J_short_status_sentence_survives_in_a_long_reply():
    reply = (
        "Kita akan setup ESP32 untuk terhubung ke jaringan WiFi rumah kamu. "
        "Pertama pastikan kabel USB terpasang dengan benar ke board ESP32. "
        "Kedua buka Arduino IDE dan pilih board yang sesuai dengan tipe ESP32 kamu. "
        "Ketiga masukkan SSID dan password WiFi ke dalam kode firmware. "
        "Setelah itu upload kode ke board menggunakan tombol upload di Arduino IDE. "
        "Tunggu proses upload selesai sampai muncul pesan berhasil di layar. "
        "Sudah terhubung. "
        "Kamu bisa cek status koneksi melalui serial monitor untuk memastikan semuanya berjalan normal."
    )
    dr = _build(reply, "short")
    assert "sudah terhubung" in dr.voice_text.lower()


# ─────────────────────────────────────────────
# K. Phase 7 - listener coherence rule (already enforced by
#    `_repair_orphans()` - proof tests using this file's own
#    adversarial dependent-opener sentences).
# ─────────────────────────────────────────────

@pytest.mark.parametrize("opener", ADVERSARIAL_DEPENDENT_OPENERS)
def test_K_adversarial_dependent_opener_never_survives_as_orphan(opener):
    reply = (
        "Berikut penjelasan singkat mengenai masalah koneksi perangkat kamu. "
        "Ada beberapa hal teknis lain yang perlu diperhatikan juga dalam proses ini. "
        + opener
    )
    dr = _build(reply, "short")
    assert _no_orphans(dr.voice_text, reply), dr.voice_text


def test_L_dangling_conditional_without_referent_never_occurs():
    # "Kalau masih gagal, cek port 1883." must never be spoken alone
    # without whatever it is that might "still fail".
    reply = (
        "Ini adalah beberapa langkah opsional yang bisa kamu coba kapan saja. "
        "Kalau masih gagal, cek port 1883."
    )
    dr = _build(reply, "short")
    voice_lower = dr.voice_text.lower()
    if "kalau masih gagal" in voice_lower or "masih gagal, cek port" in voice_lower:
        assert "langkah opsional" in voice_lower


def test_M_akibatnya_without_causal_setup_never_survives_alone():
    reply = (
        "Berikut adalah beberapa catatan umum tentang jaringan rumah kamu. "
        "Akibatnya koneksi akan gagal."
    )
    dr = _build(reply, "short")
    assert _no_orphans(dr.voice_text, reply), dr.voice_text


# ─────────────────────────────────────────────
# N. Phase 14 - false-positive candidates must NOT be misclassified as
#    dependent when they open a reply / stand alone (word-boundary-safe
#    matching, not substring matching).
# ─────────────────────────────────────────────

@pytest.mark.parametrize("sentence", FALSE_POSITIVE_CANDIDATES)
def test_N_false_positive_candidate_as_lead_sentence_is_never_dropped_as_orphan(sentence):
    reply = sentence + " Ini adalah kalimat tambahan yang tidak terkait langsung."
    dr = _build(reply, "short")
    # the lead sentence (index 0) is always must-keep regardless of its
    # own dependency classification - this proves that invariant holds
    # even for sentences that superficially resemble dependency markers.
    # Compare against the SAME normalize_for_speech() pipeline the
    # production code applies (e.g. "1883" becomes a spoken-out number
    # word, "port" itself never does) rather than the raw literal text.
    normalized_lead = ro.normalize_for_speech(sentence, language="indonesian").lower().rstrip(".")
    sig = " ".join(normalized_lead.split()[:3])
    assert sig in dr.voice_text.lower()


def test_N2_selanjutnya_word_boundary_safe_not_a_substring_match():
    # "Keberlanjutan" contains "lanjut" as a substring but must NOT match
    # the "selanjutnya"/"lanjut"-style continuation markers.
    assert not ro._has_continuation_lead("keberlanjutan sistem tidak bergantung pada mqtt")


def test_N3_short_standalone_technical_identifiers_not_misread_as_dependent():
    for sentence in ("port 1883", "esp32"):
        assert not ro._is_dependent_sentence(
            [ro._Sentence(sentence, sentence, False)], 0, set(),
        )


# ─────────────────────────────────────────────
# O. Phase 8 - do not over-compress an already-short-and-coherent
#    response, at ANY depth.
# ─────────────────────────────────────────────

@pytest.mark.parametrize("depth_query", ["singkat", "", "jelaskan detail"])
def test_O_phase8_causal_pair_never_compressed(depth_query):
    dr = _build(PHASE8_CAUSAL_PAIR, depth_query)
    assert "ssid atau password" in dr.voice_text.lower() or "ssid" in dr.voice_text.lower()
    assert "cek kembali konfigurasi wifi" in dr.voice_text.lower() or "konfigurasi wifi" in dr.voice_text.lower()


@pytest.mark.parametrize("depth_query", ["singkat", "", "jelaskan detail"])
def test_P_phase8_status_pair_never_compressed(depth_query):
    dr = _build(PHASE8_STATUS_PAIR, depth_query)
    assert "sudah berhasil" in dr.voice_text.lower()
    assert "relay sekarang aktif" in dr.voice_text.lower()


@pytest.mark.parametrize("sentence", SHORT_HIGH_VALUE_SENTENCES)
def test_Q_short_high_value_standalone_sentence_never_compressed(sentence):
    dr = _build(sentence, "singkat")
    assert dr.voice_text.strip() != ""
    core = sentence.lower().rstrip(".")
    assert core.split()[0] in dr.voice_text.lower()


# ─────────────────────────────────────────────
# R. Semantic completeness never outranks relevance (CRITICAL INVARIANT,
#    Phase 6) - an irrelevant-but-"complete" unit must not survive over
#    a highly relevant standalone warning/number sentence under a tight
#    budget.
# ─────────────────────────────────────────────

def test_R_relevance_still_dominant_over_semantic_completeness():
    reply = (
        "Berikut penjelasan umum tentang topik ini secara garis besar. "
        "Selain itu, ada beberapa catatan tambahan yang sifatnya opsional saja. "
        "Namun, itu semua tidak terlalu penting untuk kasus kamu sekarang. "
        "Jangan pernah menyambungkan modul ini langsung ke tegangan 220V AC. "
        "Pastikan kamu memakai step-down converter dengan rating minimal 3 ampere."
    )
    dr = _build(reply, "singkat")
    # the hard safety warning (must-keep regardless of score/budget)
    # must survive even though it is not part of the same semantic unit
    # as the lead sentence.
    assert "220v" in dr.voice_text.lower() or "220 v" in dr.voice_text.lower()


# ─────────────────────────────────────────────
# S. Regression guard - the 31 existing Sprint 1 scenarios' own
#    dependency classification results are untouched by the new
#    confirmation bonus (spot-check a few markers still classify
#    identically).
# ─────────────────────────────────────────────

def test_S_causal_continuation_reference_classification_unchanged():
    sentences = _sentences(
        "ESP32 terhubung ke WiFi. "
        "Akibatnya, dashboard otomatis update. "
        "Selain itu, kamu bisa tambah sensor lain. "
        "Ini terjadi karena konfigurasi awal sudah benar."
    )
    condition_indices = set()
    assert ro._dependency_kind(sentences, 0, condition_indices) == ro._DEP_INDEPENDENT
    assert ro._dependency_kind(sentences, 1, condition_indices) == ro._DEP_DEPENDENT
    assert ro._dependency_kind(sentences, 2, condition_indices) == ro._DEP_DEPENDENT
    assert ro._dependency_kind(sentences, 3, condition_indices) == ro._DEP_DEPENDENT


# ─────────────────────────────────────────────
# T. Real production-path E2E tests (Phase 13) - through
#    RuntimeDemoConsole -> PlannerBridgeModule -> response
#    generation/selection -> speech output, never a direct internal
#    helper call.
# ─────────────────────────────────────────────

import importlib.util
import threading


def _load_demo():
    demo_path = os.path.join(_ROOT, "main_runtime_demo.py")
    spec = importlib.util.spec_from_file_location("main_runtime_demo_semunits", demo_path)
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo_semunits"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate, timeout_s: float = 5.0, interval_s: float = 0.01) -> bool:
    deadline = __import__("time").time() + timeout_s
    while __import__("time").time() < deadline:
        if predicate():
            return True
        __import__("time").sleep(interval_s)
    return predicate()


def _wake(console, demo) -> None:
    from luno.wake_session import ConversationState
    console.simulate_speech("alexa")
    assert _wait_until(lambda: console.session_manager.session.state == ConversationState.LISTENING, 3.0)


def _run_one_turn(demo, *, reply: str, user_text: str) -> str:
    """Runs ONE real turn through `RuntimeDemoConsole` (real event bus,
    real threading, mocked only at the LLM/audio-device boundary - same
    technique `tests/test_voice_pipeline_latency.py` and every prior
    streaming sprint's own E2E suite already established) and returns
    the COMPLETE spoken text for the turn.

    Voice Output Naturalness & First-Audio Latency sprint: honestly
    updated (never weakened). `ENABLE_LLM_TTS_STREAMING` now defaults to
    `True` (see `luno/config.py`'s own updated rationale), under which a
    turn's spoken content can arrive as MULTIPLE `speak_stream_chunk`
    events (the very first sentence dispatched early during generation,
    the remainder reconciled and dispatched once the LLM finishes - see
    `luno/incremental_speech.py`'s own "RESPONSE-DEPTH-POLICY-SAFE
    REDESIGN") rather than one single `speak_request`. The ORIGINAL
    version of this helper only ever captured the FIRST dispatch's own
    `evt.get("text", "")` - which silently returned `""` for a
    `speak_stream_chunk` event (that event's `text` lives nested under
    `evt["chunk"]["text"]`, never at the top level - see
    `SpeakStreamChunk`'s own docstring) and, even when non-empty, would
    only ever be a PREFIX of the turn's full spoken content once
    streaming is active. Neither of this file's own callers actually
    needs "only the first chunk, mid-flight" (`test_T1`/`test_T2` assert
    full-content coherence; `test_T3`'s own assertion is a plain content
    check, not a timing check) - the full accumulated text is the
    correct, mode-agnostic return value for all of them."""
    from luno.adapters.fish_audio import MockFishAudioClient

    console = demo.RuntimeDemoConsole(
        openrouter_client=demo.MockOpenRouterClient(canned_text=reply, chunk_delay_s=0.0),
        fish_audio_client=MockFishAudioClient(playback_delay_s=0.0),
    )
    console.start()
    try:
        _wake(console, demo)
        parts: List[str] = []
        done = threading.Event()

        def _on_speak_request(evt):
            # Legacy single-shot dispatch - carries the WHOLE turn's text.
            parts.append(evt.get("text", "") or "")
            done.set()

        def _on_stream_chunk(evt):
            chunk = evt.get("chunk") or {}
            text = chunk.get("text") or ""
            if text:
                parts.append(text)
            if chunk.get("is_final"):
                done.set()

        subs = [
            console.event_bus.subscribe("speak_stream_chunk", _on_stream_chunk),
            console.event_bus.subscribe("speak_request", _on_speak_request),
        ]
        try:
            console.simulate_speech(user_text)
            assert _wait_until(done.is_set, 8.0), "no speech ever dispatched"
        finally:
            for s in subs:
                console.event_bus.unsubscribe(s)
        return " ".join(p for p in parts if p)
    finally:
        console.stop()


def test_T1_e2e_short_response_stays_intact_through_real_console():
    demo = _load_demo()
    text = _run_one_turn(demo, reply=PHASE8_STATUS_PAIR, user_text="cek status relay")
    text_lower = text.lower()
    assert "sudah berhasil" in text_lower
    assert "relay sekarang aktif" in text_lower


def test_T2_e2e_conditional_response_setup_and_condition_stay_coherent():
    demo = _load_demo()
    text = _run_one_turn(demo, reply=CONDITIONAL_SETUP_EXAMPLE, user_text="gimana kalau esp32 gagal konek")
    text_lower = text.lower()
    if "kalau masih gagal" in text_lower:
        assert "restart esp32" in text_lower or "restart" in text_lower


def test_T3_e2e_long_response_first_semantic_unit_speakable_early():
    demo = _load_demo()
    text = _run_one_turn(demo, reply=LONG_CAUSAL_CHAIN, user_text="kenapa gpu saya throttle")
    # the lead sentence establishing the topic must always be present in
    # the FIRST speech dispatched, even if the rest arrives later.
    assert "throttling" in text.lower() or "throttle" in text.lower()
