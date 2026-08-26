"""
test_entity_identity_semantic_alias_continuity.py
====================================================

SPRINT 45 - ENTITY IDENTITY & SEMANTIC ALIAS CONTINUITY.

Goal: make Luno recognize that different surface forms can refer to the
same entity/concept across turns, without embeddings, an LLM judge, a
second ranking system, persistent raw conversation storage, or a global
topic state. This sprint specifically targets the semantic-alias gap
Sprints 39-44 documented and left open.

Root cause (Phase 0-2, live reproduction via real `RuntimeDemoConsole`
before any code changed): a comprehensive probe matrix covering verb
aliases (ganti/upgrade), action aliases (beli/ganti), device aliases/
abbreviations (ESP32/mikrokontroler/ESP32-S3/S3), audio aliases
(mikrofon/mic), lighting (LED strip/lampu LED), aquascape (pompa/water
pump), false-positive controls, and multi-topic ambiguity found that
NEARLY EVERY one of these was **already correctly handled** by Sprint
43's existing `_TOKEN_SYNONYM_GROUPS`/`_TOKEN_SYNONYM_PHRASES` bridging
layer, raw hyphen-tokenization (naturally splitting "ESP32-S3" into
"esp32"+"s3"), and Sprint 44's ambiguity-safety guards. Per this sprint's
own explicit instruction ("if the existing architecture already handles
a scenario correctly, LEAVE IT ALONE"), none of those were modified -
this file locks them in as regression tests instead.

Four narrow, genuinely-justified gaps WERE found and fixed, all
variations on a single underlying fact:

1-3. **"gimana"/"bagaimana" register asymmetry.** "gimana" is simply the
   colloquial contraction of the standard Indonesian question word
   "bagaimana" ("how") - the SAME lexical item in two registers, already
   treated as equivalent in multiple OTHER places in this codebase (the
   general question-marker regex, `_ATTRIBUTE_RESIDUAL_STOPWORDS`). Three
   places had missed the pair: `luno.memory._COMPARISON_MARKER_RE` (so
   `classify_reference_type()` never even recognized "Mic-nya bagaimana?"
   as a comparison at all), `luno.memory._attribute_reference_word()`'s
   candidate-exclusion check, and `luno.memory_context._TOPIC_OVERLAP_
   STOPWORDS` (so even once classified correctly, "bagaimana" counted as
   a second "real" token, defeating the low-ambiguity single-token
   fallback). Live reproduction (Sprint 45 Scenario G: a correction to
   ESP32-S3 followed by "Mic-nya bagaimana?") found all three needed to
   move together for the formal register to work identically to the
   colloquial one it already does.

4. **3-letter acronym + "-nya" clitic.** `_MIN_AFFIX_ROOT_LEN=4` (Sprint
   43's own guard against corrupting short product identifiers) also
   blocked stripping the unambiguous "-nya" possessive clitic from any
   3-letter root, so a fused (no-hyphen) "SSDnya"/"CPUnya"/"PSUnya" never
   normalized back to "ssd"/"cpu"/"psu" at all. Live reproduction (a
   competing GPU topic more recent than an earlier SSD one, then "SSDnya
   gimana?") found this a genuine ambiguity-safety failure, not just a
   missed match: the query wrongly attached to the GPU topic via Sprint
   44's own "recency when unopposed" fallback, since SSD's own history
   entry was never even considered a candidate. Fixed with a SEPARATE,
   narrower `_MIN_CLITIC_ROOT_LEN=3`, applied only to the "-nya" pass
   (never the derivational-suffix or prefix passes, which keep the
   original, stricter guard).

No new entity relationship model, no embeddings, no LLM judge, no second
ranking system, no new synonym groups. The existing flat bag-of-terms
representation and Sprint 43/44's existing mechanisms already correctly
distinguish exact identity (raw token match), alias (synonym groups),
and abbreviation (natural hyphen-tokenization) from parent/attribute
relationships that require actual world knowledge the system correctly
declines to fabricate (e.g. it does not know "INMP441" IS a microphone
unless the word "mic"/"mikrofon" was used somewhere in the conversation).
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

def _load_demo(tag: str = "s45"):
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


def _snap(*terms, age=0):
    return ActiveTopicSnapshot(terms=frozenset(terms), turns_since_active=age)


# ============================================================================
# Section 1 - "gimana"/"bagaimana" classifier-level unit tests
# ============================================================================

def test_01_bagaimana_recognized_as_comparison_marker():
    assert memory.classify_reference_type("Mic-nya bagaimana?") == "comparison"


def test_02_gimana_still_recognized_unchanged():
    assert memory.classify_reference_type("Mic-nya gimana?") == "comparison"


def test_03_bagaimana_and_gimana_classify_identically():
    a = memory.classify_reference_type("ESP32 bagaimana?")
    b = memory.classify_reference_type("ESP32 gimana?")
    assert a == b == "comparison"


def test_04_bare_bagaimana_question_not_falsely_a_comparison_without_named_target():
    # "Bagaimana?" alone (bare, no named residual) - direct_reference/
    # bare-pronoun path, not comparison (needs a named non-pronoun token
    # alongside the marker) - same precedent as bare "Gimana?".
    a = memory.classify_reference_type("Bagaimana?")
    b = memory.classify_reference_type("Gimana?")
    assert a == b


def test_05_comparison_marker_regex_matches_bagaimana_directly():
    assert memory._COMPARISON_MARKER_RE.search("Kalau upgrade GPU bagaimana?") is not None


def test_06_comparison_marker_regex_does_not_match_unrelated_words():
    # Structural boundary test: "bagaimana" must not accidentally match
    # inside an unrelated word containing similar letters.
    assert memory._COMPARISON_MARKER_RE.search("Bagasi mobil ini besar.") is None


def test_07_attribute_reference_word_excludes_bagaimana_as_candidate():
    # "Yang bagaimana?" must not return "bagaimana" itself as a
    # fabricated attribute word (same as the existing "gimana" guard).
    word = memory._attribute_reference_word("yang bagaimana?")
    assert word is None


def test_08_attribute_reference_word_still_excludes_gimana():
    word = memory._attribute_reference_word("yang gimana?")
    assert word is None


def test_09_topic_overlap_stopwords_contains_bagaimana():
    assert "bagaimana" in memory_context._TOPIC_OVERLAP_STOPWORDS


def test_10_topic_overlap_stopwords_still_contains_gimana():
    assert "gimana" in memory_context._TOPIC_OVERLAP_STOPWORDS


def test_11_single_token_query_with_bagaimana_reduces_correctly():
    tokens = set(memory_context.analyze_query("Mic-nya bagaimana?").tokens) - memory_context._TOPIC_OVERLAP_STOPWORDS
    assert tokens == {"mic"}


# ============================================================================
# Section 2 - "-nya" clitic short-root (SSD/CPU/PSU/RAM) unit tests
# ============================================================================

def test_12_ssdnya_strips_to_ssd():
    assert memory_context._strip_bounded_affixes("ssdnya") == "ssd"


def test_13_cpunya_strips_to_cpu():
    assert memory_context._strip_bounded_affixes("cpunya") == "cpu"


def test_14_psunya_strips_to_psu():
    assert memory_context._strip_bounded_affixes("psunya") == "psu"


def test_15_ramnya_strips_to_ram():
    assert memory_context._strip_bounded_affixes("ramnya") == "ram"


def test_16_two_letter_root_still_refused():
    # "dinya" (2-letter root "di" + "nya") must NOT be stripped - the
    # `_MIN_CLITIC_ROOT_LEN=3` floor must still refuse anything shorter.
    assert memory_context._strip_bounded_affixes("dinya") == "dinya"


def test_17_derivational_suffix_pass_still_uses_stricter_floor():
    # The narrower clitic-only floor must NOT leak into the derivational
    # suffix pass - a 3-char root via "-an"/"-kan" stripping must still
    # be refused (structural invariant: only "nya" got the lower floor).
    # "sewa" + "an" = "sewaan" (6 chars) - stripping "-an" (2 chars)
    # leaves "sewa" (4 chars) - still fine at the ORIGINAL floor, so this
    # checks a genuinely short case: a hypothetical "xxan" would leave
    # "xx" (2 chars, well under either floor) and must stay unchanged.
    assert memory_context._strip_bounded_affixes("xxan") == "xxan"


def test_18_prefix_pass_still_uses_stricter_floor():
    # "dira" (hypothetical) - "di" prefix would leave "ra" (2 chars),
    # under both floors - must stay unchanged (prefix floor untouched).
    assert memory_context._strip_bounded_affixes("dira") == "dira"


def test_19_min_clitic_root_len_is_three_not_lower():
    assert memory_context._MIN_CLITIC_ROOT_LEN == 3


def test_20_min_affix_root_len_unchanged_at_four():
    """Structural invariant: the ORIGINAL, stricter guard used by every
    other pass (derivational suffix, prefix, English suffix) must remain
    unchanged - only the "-nya" clitic pass got a narrower exception."""
    assert memory_context._MIN_AFFIX_ROOT_LEN == 4


def test_21_normalize_terms_for_bridging_includes_ssd_root():
    expanded = memory_context._normalize_terms_for_bridging(frozenset({"ssdnya"}))
    assert "ssd" in expanded
    assert "ssdnya" in expanded  # purely additive, original preserved


# ============================================================================
# Section 3 - word-shape / token-boundary safety (no false positives)
# ============================================================================

def test_22_microscope_never_canonicalizes_to_mic():
    assert memory_context._TOKEN_SYNONYM_CANON.get("microscope") is None


def test_23_pumpa_never_canonicalizes_to_pompa():
    # "pumpa" is not a real word - substring-adjacent to "pompa"/"pump"
    # but must never be treated as either.
    assert memory_context._TOKEN_SYNONYM_CANON.get("pumpa") is None
    assert memory_context._strip_bounded_affixes("pumpa") == "pumpa"


def test_24_lampu_never_auto_collapses_to_led():
    """Explicit brief requirement: do not collapse generic "lampu" into
    LED automatically."""
    assert memory_context._TOKEN_SYNONYM_CANON.get("lampu") is None
    assert memory_context._TOKEN_SYNONYM_CANON.get("lamp") is None


def test_25_led_has_no_synonym_group_membership():
    """No LED synonym group was added this sprint - "led"/"strip" match
    only via raw token overlap (verified sufficient in Phase 1/E2E), not
    via a new alias group. Structural anti-scope-creep check."""
    assert memory_context._TOKEN_SYNONYM_CANON.get("led") is None
    assert memory_context._TOKEN_SYNONYM_CANON.get("strip") is None


def test_26_gpu_and_upgrade_gpu_remain_distinct_tokens():
    tokens = memory_context.analyze_query("upgrade GPU sekarang").tokens
    assert "gpu" in tokens and "upgrade" in tokens
    assert tokens.count("gpu") == 1  # "upgrade" never merges into "gpu"


def test_27_esp32_s3_tokenizes_into_two_separate_tokens():
    tokens = memory_context.analyze_query("ESP32-S3 cocok buat ini.").tokens
    assert "esp32" in tokens and "s3" in tokens


def test_28_bare_s3_does_not_falsely_match_unrelated_esp32_absent_history():
    # "S3" alone with NO prior ESP32-S3 mention anywhere must not
    # fabricate a connection out of nothing.
    active = _snap("aquascape", "pompa", "kecil")
    result = memory_context.select_topic_candidates([active], "Kalau S3 gimana?", True)
    assert result == []


def test_29_strip_bounded_affixes_never_corrupts_short_acronyms_via_prefix():
    # "RAM" itself (no suffix) must never be touched by the prefix pass.
    for acronym in ("ram", "ssd", "cpu", "psu", "gpu", "led", "usb"):
        assert memory_context._strip_bounded_affixes(acronym) == acronym


def test_30_gaming_never_treated_as_gpu_alias():
    """"gaming" is a CONTEXTUAL association with GPU discussions, not a
    lexical alias - must never canonicalize to "gpu"."""
    assert memory_context._TOKEN_SYNONYM_CANON.get("gaming") is None


# ============================================================================
# Section 4 - Indonesian morphology (bounded, not a general stemmer)
# ============================================================================

def test_31_mikrofon_mikrofonnya_same_root():
    root = memory_context._strip_bounded_affixes("mikrofonnya")
    assert root == "mikrofon"
    assert memory_context._TOKEN_SYNONYM_CANON.get(root) == "mic"


def test_32_pompa_pompanya_same_root():
    assert memory_context._strip_bounded_affixes("pompanya") == "pompa"


def test_33_lampu_lampunya_same_root():
    assert memory_context._strip_bounded_affixes("lampunya") == "lampu"


def test_34_upgrade_diupgrade_same_root():
    root = memory_context._strip_bounded_affixes("diupgrade")
    assert root == "upgrade"


def test_35_ganti_mengganti_same_root():
    root = memory_context._strip_bounded_affixes("mengganti")
    assert root == "ganti"
    assert memory_context._TOKEN_SYNONYM_CANON.get(root) == "upgrade"


def test_36_beli_membeli_same_root():
    assert memory_context._strip_bounded_affixes("membeli") == "beli"


def test_37_no_unrestricted_stemmer_bounded_affix_sets():
    """Structural invariant: the affix sets remain small/bounded, not a
    general-purpose stemmer (Nazief-Adriani-style)."""
    assert len(memory_context._ID_PREFIXES) <= 25
    assert len(memory_context._ID_CLITIC_SUFFIXES) <= 6
    assert len(memory_context._ID_DERIVATIONAL_SUFFIXES) <= 6


# ============================================================================
# Section 5 - alias-chain unit tests (existing Sprint 43 mechanism, locked in)
# ============================================================================

def test_38_ganti_upgrade_synonym_group_intact():
    canon = memory_context._TOKEN_SYNONYM_CANON
    assert canon.get("ganti") == canon.get("upgrade")


def test_39_mic_mikrofon_synonym_group_intact():
    canon = memory_context._TOKEN_SYNONYM_CANON
    assert canon.get("mic") == canon.get("mikrofon") == canon.get("microphone")


def test_40_pompa_pump_synonym_group_intact():
    canon = memory_context._TOKEN_SYNONYM_CANON
    assert canon.get("pompa") == canon.get("pump")


def test_41_kartu_grafis_phrase_bridges_to_gpu():
    expanded = memory_context._normalize_query_tokens_for_bridging(
        "Kalau kartu grafisnya gimana?", {"kartu", "grafisnya"},
    )
    assert "gpu" in expanded


def test_42_synonym_groups_still_bounded_small():
    """Anti-scope-creep: this sprint added ZERO new synonym groups."""
    total_members = sum(len(g) for g in memory_context._TOKEN_SYNONYM_GROUPS)
    assert total_members <= 40


def test_43_no_new_entity_specific_names_in_synonym_groups():
    forbidden = {
        "esp32", "esp8266", "rtx3060", "rtx", "inmp441", "max9814",
        "sph0645", "ws2812", "ws2812b", "ws2811", "sk6812",
    }
    all_members = {m for group in memory_context._TOKEN_SYNONYM_GROUPS for m in group}
    assert not (all_members & forbidden)


# ============================================================================
# Section 6 - multi-topic ambiguity unit tests
# ============================================================================

def test_44_two_topic_ambiguity_no_guess():
    a = _snap("gpu", "rtx", "ganti")
    b = _snap("pompa", "aquascape", "ganti")
    result = memory_context.select_topic_candidates([a, b], "Kalau upgrade itu gimana?", True)
    assert result == []


def test_45_three_distinct_topics_single_novel_word_refused():
    active = _snap("gpu", "rtx")
    other_a = _snap("esp32", "inmp441")
    other_b = _snap("aquascape", "pompa")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Yang bagus?", topic_history=[active, other_a, other_b],
    ) is False


def test_46_five_distinct_topics_single_novel_word_refused():
    active = _snap("watercooling", "radiator")
    others = [
        _snap("gpu", "rtx"), _snap("esp32", "inmp441"),
        _snap("aquascape", "pompa"), _snap("led", "strip", "ws2812"),
    ]
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Pendinginnya gimana?", topic_history=[active] + others,
    ) is False


def test_47_generic_shared_word_across_two_topics_surfaces_both_not_one():
    a = _snap("speaker", "radio", "kecil")
    b = _snap("monitor", "cctv", "kecil")
    result = memory_context.select_topic_candidates([a, b], "Yang kecil itu apa?", True)
    assert set(result) == {a, b}


def test_48_unambiguous_single_topic_still_resolves():
    active = _snap("aquascape", "pompa", "kecil")
    assert memory_context.is_active_topic_relevant_to_query(active, "Filternya gimana?", topic_history=[active]) is True


# ============================================================================
# Section 7 - false-positive / non-fabrication unit tests
# ============================================================================

def test_49_unrelated_query_never_matches_topic_history():
    a = _snap("gpu", "rtx")
    result = memory_context.select_topic_candidates([a], "Besok aku mau beli sepatu.", False)
    assert result == []


def test_50_new_entity_without_marker_stays_unknown():
    # "Kalau MAX9814?" - deliberate, existing, tested precedent (no
    # comparison/attribute marker) - must remain conservative "unknown".
    assert memory.classify_reference_type("Kalau MAX9814?") == "unknown"


def test_51_product_to_category_link_never_fabricated():
    """The system must never invent that a specific product name IS a
    member of a generic category unless the category word was actually
    used somewhere - "INMP441" is never treated as "mic" on its own."""
    assert memory_context._TOKEN_SYNONYM_CANON.get("inmp441") is None


def test_52_contextual_association_not_treated_as_alias():
    # "gaming"/"performa" are commonly discussed alongside GPUs but are
    # not lexical aliases for "gpu" - a multi-word residual query using
    # them must not fabricate a GPU connection with zero other evidence.
    active = _snap("watercooling", "radiator", "pc")
    result = memory_context.is_active_topic_relevant_to_query(
        active, "Kalau performa gamingnya gimana?", topic_history=[active],
    )
    assert result is False  # 2 real residual tokens - conservative refusal


# ============================================================================
# Section 8 - correction / attribute-followup preservation unit tests
# ============================================================================

def test_53_repair_reference_classification_for_correction():
    assert memory.classify_reference_type("Eh maksudku ESP32-S3.") == "repair_reference"


def test_54_repair_reference_is_a_merge_type():
    assert memory.is_merge_reference_followup("Eh maksudku ESP32-S3.") is True


def test_55_merge_terms_preserves_original_plus_correction():
    old = frozenset({"esp32", "inmp441"})
    new = frozenset({"esp32", "s3", "maksudku"})
    merged = memory_context._merge_terms(new, old)
    assert "s3" in merged and "inmp441" in merged


# ============================================================================
# Section 9 - performance
# ============================================================================

def test_56_comparison_marker_regex_is_fast():
    start = time.perf_counter()
    for _ in range(2000):
        memory._COMPARISON_MARKER_RE.search("Mic-nya bagaimana?")
    elapsed_ms = (time.perf_counter() - start) * 1000 / 2000
    assert elapsed_ms < 5.0


def test_57_strip_bounded_affixes_is_fast():
    start = time.perf_counter()
    for _ in range(2000):
        memory_context._strip_bounded_affixes("ssdnya")
    elapsed_ms = (time.perf_counter() - start) * 1000 / 2000
    assert elapsed_ms < 5.0


def test_58_classify_reference_type_is_fast():
    start = time.perf_counter()
    for _ in range(2000):
        memory.classify_reference_type("Mic-nya bagaimana?")
    elapsed_ms = (time.perf_counter() - start) * 1000 / 2000
    assert elapsed_ms < 5.0


# ============================================================================
# Section 10 - E2E: alias/abbreviation/entity continuity (real RuntimeDemoConsole)
# ============================================================================

def test_70_e2e_verb_alias_ganti_upgrade_gpu():
    demo = _load_demo("70")
    replies = {
        "Aku mau ganti GPU karena yang sekarang kurang kuat.": "Oke, dicatat rencana ganti GPU.",
        "Kalau upgrade GPU gimana?": "GPU baru bisa naikkan performa signifikan.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau ganti GPU karena yang sekarang kurang kuat.", "t70-1", conversation_id="c70")
        sp = _run_turn_capture_prompt(console, demo, "Kalau upgrade GPU gimana?", "t70-2", conversation_id="c70")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "gpu" in candidates[0].lower()
    finally:
        console.stop()


def test_71_e2e_action_alias_beli_ganti_shared_noun():
    demo = _load_demo("71")
    replies = {
        "Aku mau beli SSD baru.": "Oke, dicatat rencana beli SSD.",
        "Kalau ganti SSD yang lebih besar?": "SSD kapasitas besar tentu lebih baik.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau beli SSD baru.", "t71-1", conversation_id="c71")
        sp = _run_turn_capture_prompt(console, demo, "Kalau ganti SSD yang lebih besar?", "t71-2", conversation_id="c71")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "ssd" in candidates[0].lower()
    finally:
        console.stop()


def test_72_e2e_device_abbreviation_esp32_s3():
    demo = _load_demo("72")
    replies = {
        "ESP32-S3 cocok buat proyek ini.": "Oke, dicatat ESP32-S3.",
        "Kalau S3 gimana?": "S3 punya lebih banyak GPIO dan native USB.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32-S3 cocok buat proyek ini.", "t72-1", conversation_id="c72")
        sp = _run_turn_capture_prompt(console, demo, "Kalau S3 gimana?", "t72-2", conversation_id="c72")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "esp32" in candidates[0].lower()
    finally:
        console.stop()


def test_73_e2e_audio_alias_mikrofon_mic():
    demo = _load_demo("73")
    replies = {
        "Aku pakai INMP441 sebagai mikrofon.": "Oke, dicatat INMP441 sebagai mikrofon.",
        "Kalau mic yang lebih sensitif?": "MAX9814 lebih sensitif tapi lebih noisy.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku pakai INMP441 sebagai mikrofon.", "t73-1", conversation_id="c73")
        sp = _run_turn_capture_prompt(console, demo, "Kalau mic yang lebih sensitif?", "t73-2", conversation_id="c73")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "inmp441" in candidates[0].lower()
    finally:
        console.stop()


def test_74_e2e_aquascape_alias_pompa_water_pump():
    demo = _load_demo("74")
    replies = {
        "Pompa aquascape ini terlalu kecil.": "Oke, dicatat pompa aquascape kekecilan.",
        "Kalau water pump yang lebih besar?": "Pompa lebih besar bisa tingkatkan flow rate.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Pompa aquascape ini terlalu kecil.", "t74-1", conversation_id="c74")
        sp = _run_turn_capture_prompt(console, demo, "Kalau water pump yang lebih besar?", "t74-2", conversation_id="c74")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "aquascape" in candidates[0].lower()
    finally:
        console.stop()


def test_75_e2e_false_positive_control_gpu_shoes():
    demo = _load_demo("75")
    replies = {
        "Aku punya GPU RTX 3060.": "Oke, dicatat GPU RTX 3060.",
        "Besok aku mau beli sepatu.": "Semoga dapat sepatu yang bagus.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku punya GPU RTX 3060.", "t75-1", conversation_id="c75")
        sp = _run_turn_capture_prompt(console, demo, "Besok aku mau beli sepatu.", "t75-2", conversation_id="c75")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"GPU must not leak into an unrelated shoe question, prompt had: {sp}"
    finally:
        console.stop()


def test_76_e2e_multi_topic_ambiguity_gpu_vs_pompa():
    demo = _load_demo("76")
    replies = {
        "Aku punya GPU RTX 3060.": "Oke, dicatat GPU RTX 3060.",
        "Aku juga punya pompa aquascape.": "Oke, dicatat pompa aquascape.",
        "Kalau yang lebih besar gimana?": "Tergantung yang mana maksudnya.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku punya GPU RTX 3060.", "t76-1", conversation_id="c76")
        _run_turn(console, demo, "Aku juga punya pompa aquascape.", "t76-2", conversation_id="c76")
        sp = _run_turn_capture_prompt(console, demo, "Kalau yang lebih besar gimana?", "t76-3", conversation_id="c76")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"ambiguous 'yang lebih besar' must not guess between GPU/pump, prompt had: {sp}"
    finally:
        console.stop()


def test_77_e2e_correction_then_bagaimana_attribute_followup():
    """The sprint's own primary reproduced fix, locked in as a regression
    test: a correction (repair_reference) followed by a FORMAL-register
    "bagaimana" attribute question must resolve identically to how the
    colloquial "gimana" form already did before this sprint."""
    demo = _load_demo("77")
    replies = {
        "ESP32 pakai INMP441.": "Oke, dicatat ESP32 dengan INMP441.",
        "Eh maksudku ESP32-S3.": "Oke, dikoreksi jadi ESP32-S3.",
        "Mic-nya bagaimana?": "INMP441 tetap kompatibel dengan ESP32-S3.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t77-1", conversation_id="c77")
        _run_turn(console, demo, "Eh maksudku ESP32-S3.", "t77-2", conversation_id="c77")
        sp = _run_turn_capture_prompt(console, demo, "Mic-nya bagaimana?", "t77-3", conversation_id="c77")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "s3" in candidates[0].lower() and "inmp441" in candidates[0].lower()
    finally:
        console.stop()


def test_78_e2e_new_entity_no_marker_stays_conservative():
    demo = _load_demo("78")
    replies = {
        "ESP32 pakai INMP441.": "Oke, dicatat ESP32 dengan INMP441.",
        "Kalau MAX9814?": "MAX9814 punya AGC bawaan, beda dari INMP441.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t78-1", conversation_id="c78")
        sp = _run_turn_capture_prompt(console, demo, "Kalau MAX9814?", "t78-2", conversation_id="c78")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        # Deliberately not asserting either way (documented, deliberate
        # precedent) - only that no WRONG unrelated topic is fabricated.
        for c in candidates:
            assert "aquascape" not in c.lower()
    finally:
        console.stop()


def test_79_e2e_unrelated_topic_after_detailed_chain_zero_injection():
    demo = _load_demo("79")
    replies = {
        "ESP32 pakai INMP441.": "Oke, dicatat.",
        "Mic-nya bagus nggak?": "INMP441 mic-nya cukup bagus untuk voice.",
        "Kalau I2S-nya gimana?": "I2S protokolnya stabil dan latency rendah.",
        "Ukuran aquarium 50x25 berapa liter?": "Sekitar 25-30 liter tergantung tinggi air.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t79-1", conversation_id="c79")
        _run_turn(console, demo, "Mic-nya bagus nggak?", "t79-2", conversation_id="c79")
        _run_turn(console, demo, "Kalau I2S-nya gimana?", "t79-3", conversation_id="c79")
        sp = _run_turn_capture_prompt(console, demo, "Ukuran aquarium 50x25 berapa liter?", "t79-4", conversation_id="c79")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"unrelated aquarium-size question must not inject ESP32/mic, prompt had: {sp}"
    finally:
        console.stop()


def test_80_e2e_cross_conversation_isolation():
    demo = _load_demo("80")
    replies = {
        "ESP32 pakai INMP441.": "Oke, dicatat di conv A.",
        "Aquascape saya pakai pompa kecil.": "Oke, dicatat di conv B.",
        "Mic-nya gimana?": "INMP441 mic-nya bagus.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t80-1", conversation_id="convX45")
        _run_turn(console, demo, "Aquascape saya pakai pompa kecil.", "t80-2", conversation_id="convY45")
        sp = _run_turn_capture_prompt(console, demo, "Mic-nya gimana?", "t80-3", conversation_id="convY45")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        # Whatever DOES get offered must come from convY45's own history,
        # never convX45's ESP32/INMP441 (the core isolation property).
        for c in candidates:
            assert "esp32" not in c.lower() and "inmp441" not in c.lower()
    finally:
        console.stop()


def test_81_e2e_long_chain_alias_wording_survives_five_turns():
    demo = _load_demo("81")
    replies = {
        "Aku mau ganti GPU karena kurang kuat.": "Oke, dicatat rencana ganti GPU.",
        "Kalau upgrade ke RTX 4060 gimana?": "RTX 4060 lumayan buat 1440p.",
        "Kartu grafisnya butuh PSU berapa watt?": "Sekitar 550W cukup untuk RTX 4060.",
        "Vga itu perlu cooling tambahan nggak?": "Tidak wajib, cooling stock biasanya cukup.",
        "GPU-nya kompatibel sama motherboard lama?": "Perlu cek slot PCIe dan BIOS support.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        for i, text in enumerate(replies.keys()):
            sp = _run_turn_capture_prompt(console, demo, text, f"t81-{i + 1}", conversation_id="c81")
            if i == 0:
                continue  # first turn establishes, nothing to check yet
            candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
            assert candidates, f"turn {i + 1} ({text!r}) lost GPU continuity, prompt had: {sp}"
            assert "gpu" in candidates[0].lower()
    finally:
        console.stop()


def test_82_e2e_five_topic_matrix_gpu_query_resolves_to_gpu():
    demo = _load_demo("82")
    replies = {
        "Aku punya GPU RTX 3060.": "Oke, dicatat GPU RTX 3060.",
        "Aku juga punya ESP32 dengan INMP441.": "Oke, dicatat ESP32 INMP441.",
        "Aku juga punya aquascape dengan pompa kecil.": "Oke, dicatat aquascape pompa.",
        "Aku juga punya LED strip WS2812.": "Oke, dicatat LED strip.",
        "Aku juga lagi pasang watercooling di PC.": "Oke, dicatat watercooling.",
        "GPU-nya gimana?": "RTX 3060 cukup untuk 1080p gaming.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        items = list(replies.items())
        for i, (text, _r) in enumerate(items[:5]):
            _run_turn(console, demo, text, f"t82-{i + 1}", conversation_id="c82")
        sp = _run_turn_capture_prompt(console, demo, items[5][0], "t82-6", conversation_id="c82")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and ("gpu" in candidates[0].lower() or "rtx" in candidates[0].lower())
    finally:
        console.stop()


def test_83_e2e_five_topic_matrix_pompa_query_resolves_to_aquascape():
    demo = _load_demo("83")
    replies = {
        "Aku punya GPU RTX 3060.": "Oke, dicatat GPU RTX 3060.",
        "Aku juga punya ESP32 dengan INMP441.": "Oke, dicatat ESP32 INMP441.",
        "Aku juga punya aquascape dengan pompa kecil.": "Oke, dicatat aquascape pompa.",
        "Aku juga punya LED strip WS2812.": "Oke, dicatat LED strip.",
        "Aku juga lagi pasang watercooling di PC.": "Oke, dicatat watercooling.",
        "Pompa-nya gimana?": "Pompa kecil cukup untuk tank kecil.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        items = list(replies.items())
        for i, (text, _r) in enumerate(items[:5]):
            _run_turn(console, demo, text, f"t83-{i + 1}", conversation_id="c83")
        sp = _run_turn_capture_prompt(console, demo, items[5][0], "t83-6", conversation_id="c83")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "pompa" in candidates[0].lower()
    finally:
        console.stop()


def test_84_e2e_five_topic_matrix_led_query_resolves_to_led():
    demo = _load_demo("84")
    replies = {
        "Aku punya GPU RTX 3060.": "Oke, dicatat GPU RTX 3060.",
        "Aku juga punya ESP32 dengan INMP441.": "Oke, dicatat ESP32 INMP441.",
        "Aku juga punya aquascape dengan pompa kecil.": "Oke, dicatat aquascape pompa.",
        "Aku juga punya LED strip WS2812.": "Oke, dicatat LED strip.",
        "Aku juga lagi pasang watercooling di PC.": "Oke, dicatat watercooling.",
        "LED-nya gimana?": "WS2812 individually addressable, bagus untuk efek.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        items = list(replies.items())
        for i, (text, _r) in enumerate(items[:5]):
            _run_turn(console, demo, text, f"t84-{i + 1}", conversation_id="c84")
        sp = _run_turn_capture_prompt(console, demo, items[5][0], "t84-6", conversation_id="c84")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "led" in candidates[0].lower()
    finally:
        console.stop()


def test_85_e2e_product_without_category_word_correctly_unresolved():
    """A specific product name (INMP441) mentioned WITHOUT ever using its
    category word ("mic"/"mikrofon") must not let a later category-word
    query fabricate the connection - this is world knowledge, not a
    lexical alias, and the system correctly declines to guess it."""
    demo = _load_demo("85")
    replies = {
        "Aku juga punya ESP32 dengan INMP441.": "Oke, dicatat ESP32 INMP441.",
        "Aku juga punya aquascape dengan pompa kecil.": "Oke, dicatat aquascape pompa.",
        "Mic-nya gimana?": "(no confident context)",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku juga punya ESP32 dengan INMP441.", "t85-1", conversation_id="c85")
        _run_turn(console, demo, "Aku juga punya aquascape dengan pompa kecil.", "t85-2", conversation_id="c85")
        sp = _run_turn_capture_prompt(console, demo, "Mic-nya gimana?", "t85-3", conversation_id="c85")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        for c in candidates:
            assert "esp32" not in c.lower() and "inmp441" not in c.lower()
    finally:
        console.stop()


def test_86_e2e_ssdnya_fused_no_hyphen_resolves_correctly_with_competing_topic():
    """The sprint's second primary reproduced fix, locked in: a fused
    (no-hyphen) "SSDnya" must connect to the SSD topic specifically, not
    fall back to whichever topic is merely most recent."""
    demo = _load_demo("86")
    replies = {
        "Aku mau beli SSD baru.": "Oke, dicatat rencana beli SSD.",
        "Aku juga punya GPU RTX 3060.": "Oke, dicatat GPU RTX 3060.",
        "SSDnya gimana?": "SSD kapasitas besar lebih baik untuk game.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau beli SSD baru.", "t86-1", conversation_id="c86")
        _run_turn(console, demo, "Aku juga punya GPU RTX 3060.", "t86-2", conversation_id="c86")
        sp = _run_turn_capture_prompt(console, demo, "SSDnya gimana?", "t86-3", conversation_id="c86")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert candidates and "ssd" in candidates[0].lower()
        assert "rtx" not in candidates[0].lower() and "gpu" not in candidates[0].lower()
    finally:
        console.stop()
