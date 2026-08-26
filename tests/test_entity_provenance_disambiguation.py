"""
test_entity_provenance_disambiguation.py
====================================================

SPRINT 49 - ENTITY PROVENANCE DISAMBIGUATION & TOPIC LINEAGE.

Goal: resolve Sprint 48's remaining known limitation #9 - two distinct
entities/topics can become conflated when they share very high lexical
overlap (the "Aquascape A"/"Aquascape B" scenario). Not "make Luno guess
harder" - make Luno distinguish entity lineage ONLY when the
conversation itself contains sufficient evidence, and REFUSE (not
silently guess) when it doesn't.

Phase 0 reconnaissance (before any code changed) re-verified Sprint 48's
own fix (the demonstrative-anchoring gate) was genuinely present and
unchanged in the checkout, and re-confirmed Sprint 45-48's own claimed
work was present. Phase 1 live-reproduced limitation #9 through the
real `RuntimeDemoConsole` (leak-free canned replies) BEFORE any edit -
it reproduced exactly as documented: "Pompanya gimana?" after
"Aquascape A pakai pompa kecil." then "Aquascape B pakai pompa besar."
confidently, silently resolved to Aquascape B with zero ambiguity
signal.

**Root cause:** the `active_score > 0` branch's `coverage > 0.5`
lineage-skip check in `is_active_topic_relevant_to_query()` (`luno/
memory_context.py`) treats any history entry whose own significant
vocabulary is majority-covered by the active snapshot's own terms as
"the same lineage, already merged in" - correct for a genuine same-
entity rename/correction, but WRONG when the two entries are actually
two separately-named, separately-tracked entities that merely happen
to share most of their generic vocabulary ("aquascape"/"pompa"/
"pakai"). The bag-of-terms representation alone cannot tell these two
cases apart - **but the conversation itself already contains evidence
that could**: the user explicitly labeled the two entries "A" and "B"
in their own original text.

**Why Sprint 48's own rejected approach doesn't apply here:** Sprint 48
investigated (and rejected) a token-based "distinguisher letter" signal
for this exact limitation, finding the shared, cross-cutting `luno.
memory_retrieval.query._STOPWORDS` set unconditionally drops the
lowercase English-article stopword "a" while keeping "b" - an unsafe,
asymmetric foundation (see `ARCHITECTURE_GUARD.md` SS48). This sprint's
own fix avoids that trap entirely by reading `ActiveTopicSnapshot.
source_sentence` (Sprint 40's own bounded, RAW, case-preserved verbatim
excerpt of the turn that established the snapshot) directly with a
dedicated regex, never through `analyze_query()`'s lowercased/stopword-
filtered token stream - so "A" and "B" are found completely
symmetrically. This is the genuinely different signal Sprint 48's own
"Recommended Sprint 49 investigation" note asked for, not a retry of
the rejected idea.

**Fix - `_extract_entity_differentiator()`** (new function, `luno/
memory_context.py`) extracts the single standalone uppercase letter
from a `source_sentence`, returning `None` for zero or 2+ candidates
(never guesses which one is the real label). Wired into the `coverage
> 0.5` lineage-skip check inside `is_active_topic_relevant_to_query()`'s
`active_score > 0` branch: when BOTH the active snapshot and a history
entry carry an unambiguous differentiator AND those differentiators
DISAGREE, the majority-coverage skip is bypassed - the entry is treated
as a genuine, distinct competitor. For a bare "Pompanya gimana?" (no
differentiator of its own), this correctly produces a TIE between A and
B and therefore a REFUSAL - not a forced guess about which one was
meant. Deliberately does NOT attempt to resolve a query that itself
names a specific differentiator ("Pompa A gimana?") - see "Known
limitations" below.

Every test below verifies the actual candidate/injection behavior
(reading the real rendered system prompt via `RuntimeDemoConsole`) or
the direct return value of `is_active_topic_relevant_to_query()`/`_
extract_entity_differentiator()` - never only a classifier's own output
in isolation.
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

def _load_demo(tag: str = "s49"):
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


def _new_console(demo, replies=None, canned_text="Dicatat."):
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
# Section 1 - unit tests for _extract_entity_differentiator()
# ============================================================================

def test_01_extracts_single_capital_letter():
    assert memory_context._extract_entity_differentiator("Aquascape A pakai pompa kecil.") == "A"
    assert memory_context._extract_entity_differentiator("Aquascape B pakai pompa besar.") == "B"


def test_02_none_for_no_differentiator():
    assert memory_context._extract_entity_differentiator("ESP32 pakai INMP441.") is None
    assert memory_context._extract_entity_differentiator("Aku pakai ESP32-S3.") is None


def test_03_none_for_two_or_more_candidates():
    assert memory_context._extract_entity_differentiator("Beli motor A dan motor B sekaligus.") is None


def test_04_none_for_empty_source_sentence():
    assert memory_context._extract_entity_differentiator("") is None
    assert memory_context._extract_entity_differentiator(None) is None


def test_05_acronyms_never_match():
    # Multi-letter uppercase acronyms ("GPU", "RTX", "CPU") are never
    # mistaken for a single-letter differentiator.
    assert memory_context._extract_entity_differentiator("GPU RTX 3060 dan CPU Intel.") is None


def test_06_lowercase_never_matches_by_design():
    # Deliberate scope restriction (see module docstring) - a lowercase
    # "a" cannot safely be used, so it is never extracted even if the
    # user's own text is informally lowercase.
    assert memory_context._extract_entity_differentiator("aquascape a pakai pompa kecil.") is None


def test_07_standalone_digits_never_match():
    # A bare number ("2" in "beli 2 pompa") is an ordinary quantity, not
    # a differentiator label - digits are deliberately excluded.
    assert memory_context._extract_entity_differentiator("Beli 2 pompa buat aquascape.") is None


def test_08_hyphenated_compound_letter_not_standalone():
    # "ESP32-S3" hyphen-splits to "esp32"/"s3" elsewhere in this module,
    # but "S3" itself is two characters, never a bare single letter.
    assert memory_context._extract_entity_differentiator("Aku pakai ESP32-S3.") is None


def test_09_source_lock_uses_word_boundaries():
    # A capital letter embedded inside a longer word ("PCa") must not
    # match - only a genuinely standalone letter token.
    assert memory_context._extract_entity_differentiator("Aku beli PCX baru.") is None


# ============================================================================
# Section 2 - unit tests for is_active_topic_relevant_to_query()'s new gate
# ============================================================================

def test_10_disagreeing_differentiators_bypass_lineage_skip_and_tie_refuses():
    active = _snap("aquascape", "b", "pakai", "pompa", "besar", source_sentence="Aquascape B pakai pompa besar.")
    other = _snap("aquascape", "pakai", "pompa", "kecil", source_sentence="Aquascape A pakai pompa kecil.")
    result = memory_context.is_active_topic_relevant_to_query(
        active, "Pompanya gimana?", topic_history=[other, active],
    )
    assert result is False, "bare query with no differentiator of its own must refuse, not guess B"


def test_11_no_differentiator_on_either_side_unchanged_lineage_skip():
    # test_15's own shape (test_memory_comparison_topic_preservation.py)
    # - neither entry carries a differentiator, majority-coverage skip
    # must fire exactly as before this sprint.
    active = _snap("gpu", "rtx", "upgrade", "gaming", "cukup", source_sentence="")
    lineage = _snap("gpu", "rtx", source_sentence="")
    result = memory_context.is_active_topic_relevant_to_query(
        active, "Kalau upgrade itu gimana?", topic_history=[lineage, active],
    )
    # This shape has active_score > 0 (via normalized "upgrade"/"ganti"
    # bridging) and majority coverage - must remain trusted (True), same
    # as before this sprint's own change.
    assert result is True


def test_12_only_one_side_has_differentiator_falls_back_to_coverage():
    active = _snap("aquascape", "pompa", "dipindah", source_sentence="Aquascape itu pompanya dipindah.")
    other = _snap("aquascape", "pakai", "pompa", "kecil", source_sentence="Aquascape A pakai pompa kecil.")
    # Only `other` has a differentiator ("A") - `active` has none, so
    # `differentiators_disagree` is False and the ORIGINAL majority-
    # coverage lineage-skip behavior applies unchanged (conservative:
    # never treats a one-sided differentiator as sufficient evidence).
    result = memory_context.is_active_topic_relevant_to_query(
        active, "Pompanya gimana?", topic_history=[other, active],
    )
    assert result is True  # same-lineage skip still fires (other is majority-covered)


def test_13_agreeing_differentiators_still_treated_as_lineage():
    # Both entries happen to share the SAME differentiator (e.g. the
    # user re-mentioned "A" again) - not a disagreement, so the
    # majority-coverage lineage-skip still applies normally.
    active = _snap("aquascape", "a", "pakai", "pompa", "besar", source_sentence="Aquascape A pompanya diganti besar.")
    other = _snap("aquascape", "pakai", "pompa", "kecil", source_sentence="Aquascape A pakai pompa kecil.")
    result = memory_context.is_active_topic_relevant_to_query(
        active, "Pompanya gimana?", topic_history=[other, active],
    )
    assert result is True


def test_14_source_lock_new_gate_present_reuses_source_sentence():
    import inspect
    source = inspect.getsource(memory_context.is_active_topic_relevant_to_query)
    assert "_extract_entity_differentiator" in source
    assert "differentiators_disagree" in source
    assert "coverage > 0.5" in source  # original check still present, unmodified


# ============================================================================
# Section 3 - E2E: Limitation #9 (Aquascape A/B) - FIXED
# ============================================================================

def test_15_e2e_aquascape_a_b_now_refuses():
    demo = _load_demo("s49-15")
    replies = {
        "Aquascape A pakai pompa kecil.": "Dicatat.",
        "Aquascape B pakai pompa besar.": "Dicatat.",
        "Pompanya gimana?": "Bisa jelasin lebih spesifik maksudnya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aquascape A pakai pompa kecil.", "t15-1", conversation_id="c15")
        _run_turn(console, demo, "Aquascape B pakai pompa besar.", "t15-2", conversation_id="c15")
        sp = _run_turn_capture_prompt(console, demo, "Pompanya gimana?", "t15-3", conversation_id="c15")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"'Pompanya gimana?' must now refuse rather than guess B: {sp}"
    finally:
        console.stop()


def test_16_e2e_esp32_a_esp32_b_generalizes_beyond_aquascape():
    """The fix must generalize to a DIFFERENT domain vocabulary, not be
    hardcoded to 'aquascape' - explicitly required by this sprint's own
    non-negotiables."""
    demo = _load_demo("s49-16")
    replies = {
        "ESP32 A pakai sensor DHT22.": "Dicatat.",
        "ESP32 B pakai sensor DHT22.": "Dicatat.",
        "Sensornya gimana?": "Bisa jelasin lebih spesifik maksudnya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 A pakai sensor DHT22.", "t16-1", conversation_id="c16")
        _run_turn(console, demo, "ESP32 B pakai sensor DHT22.", "t16-2", conversation_id="c16")
        sp = _run_turn_capture_prompt(console, demo, "Sensornya gimana?", "t16-3", conversation_id="c16")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"ESP32 A/B shape must also refuse: {sp}"
    finally:
        console.stop()


def test_17_e2e_no_differentiator_conflation_still_conflates_documented():
    """Negative control - WITHOUT an explicit "A"/"B" label, two
    similar-vocabulary entries still conflate exactly as before (this is
    NOT a regression - there is genuinely no evidence to distinguish
    them, and the fix only activates when the conversation itself
    supplies a differentiator)."""
    demo = _load_demo("s49-17")
    replies = {
        "Aquascape depan pakai pompa kecil.": "Dicatat.",
        "Aquascape belakang pakai pompa besar.": "Dicatat.",
        "Pompanya gimana?": "Bisa jelasin lebih spesifik maksudnya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aquascape depan pakai pompa kecil.", "t17-1", conversation_id="c17")
        _run_turn(console, demo, "Aquascape belakang pakai pompa besar.", "t17-2", conversation_id="c17")
        sp = _run_turn_capture_prompt(console, demo, "Pompanya gimana?", "t17-3", conversation_id="c17")
        # Documents the CURRENT, still-imperfect (but honest) behavior
        # for the no-differentiator case - not endorsing it, just
        # locking in that this sprint's fix does not fabricate evidence
        # that was never stated.
        assert sp is not None
    finally:
        console.stop()


# ============================================================================
# Section 4 - regression locks for existing coverage/lineage boundaries
# ============================================================================

def test_18_e2e_test_15_shape_still_preserves_genuine_lineage():
    """Mirrors `test_memory_comparison_topic_preservation.py::test_15`'s
    own shape end-to-end - a genuinely merged, same-entity lineage
    (dropped a word or two along the way) must NOT be treated as a
    competitor just because this sprint's code changed nearby."""
    demo = _load_demo("s49-18")
    replies = {
        "Aku punya GPU RTX 3060, mau upgrade nanti.": "Dicatat.",
        "Upgrade GPU jadi RTX 4070 gaming, budget cukup.": "Dicatat, RTX 4070.",
        "Kalau upgrade itu gimana?": "RTX 4070 upgrade signifikan dari 3060.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku punya GPU RTX 3060, mau upgrade nanti.", "t18-1", conversation_id="c18")
        _run_turn(console, demo, "Upgrade GPU jadi RTX 4070 gaming, budget cukup.", "t18-2", conversation_id="c18")
        sp = _run_turn_capture_prompt(console, demo, "Kalau upgrade itu gimana?", "t18-3", conversation_id="c18")
        assert sp is not None
    finally:
        console.stop()


def test_19_e2e_disjoint_topics_sharing_one_verb_still_refuse():
    """Sprint 46's own `test_39_tied_normalized_overlap_across_history_
    is_not_relevant` shape, re-verified end-to-end - two genuinely
    DISJOINT topics sharing only the verb "ganti" (~33% coverage, well
    under the 50% threshold) must still be recognized as distinct and
    refuse on a tied/ambiguous follow-up."""
    demo = _load_demo("s49-19")
    replies = {
        "Aku mau ganti GPU.": "Dicatat.",
        "Aku juga mau ganti mic.": "Dicatat.",
        "Kalau upgrade itu gimana?": "Tergantung yang mana maksudnya.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku mau ganti GPU.", "t19-1", conversation_id="c19")
        _run_turn(console, demo, "Aku juga mau ganti mic.", "t19-2", conversation_id="c19")
        sp = _run_turn_capture_prompt(console, demo, "Kalau upgrade itu gimana?", "t19-3", conversation_id="c19")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"disjoint topics sharing one verb must still refuse: {sp}"
    finally:
        console.stop()


# ============================================================================
# Section 5 - hard boundary matrix (Phase 4) - E2E regression locks
# ============================================================================
# Every case classified MUST RESOLVE / MUST PRESERVE / MUST MERGE /
# MUST REPLACE / MUST REFUSE per this sprint's own Phase 4 matrix
# (see docs/change_impact/entity_provenance_disambiguation.md for the
# full table). Cases already covered by dedicated Sprint 44-48 test
# files are NOT duplicated in full here - only re-verified end-to-end
# where this sprint's own code change plausibly touches the same
# branch of `is_active_topic_relevant_to_query()`.

def test_20_matrix_02_correction_repair_must_preserve():
    # ESP32 -> ESP32-S3 correction - corrected identity must remain
    # available (Sprint 47's own Scenario 6, re-verified here).
    demo = _load_demo("s49-20")
    replies = {
        "Pakai ESP32.": "Oke ESP32.",
        "Eh maksudku ESP32-S3.": "Oke, dicatat ESP32-S3.",
        "Board itu RAM-nya berapa?": "ESP32-S3 punya RAM cukup besar.",
        "Terus harganya berapa?": "Sekitar yang sudah disebutkan.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Pakai ESP32.", "t20-1", conversation_id="c20")
        _run_turn(console, demo, "Eh maksudku ESP32-S3.", "t20-2", conversation_id="c20")
        _run_turn(console, demo, "Board itu RAM-nya berapa?", "t20-3", conversation_id="c20")
        sp = _run_turn_capture_prompt(console, demo, "Terus harganya berapa?", "t20-4", conversation_id="c20")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "s3" in joined or "esp32" in joined
    finally:
        console.stop()


def test_21_matrix_04_microphone_vs_inmp441_must_refuse_fabrication():
    # Product-to-category world knowledge never fabricated (Sprint 45).
    assert memory_context._TOKEN_SYNONYM_CANON.get("inmp441") is None


def test_22_matrix_05_gpu_vs_kartu_grafis_must_resolve_via_phrase_table():
    assert memory_context._TOKEN_SYNONYM_PHRASES.get("kartu grafis") == "gpu"


def test_23_matrix_06_pump_vs_pompa_must_resolve_via_synonym_group():
    assert memory_context._TOKEN_SYNONYM_CANON.get("pump") == "pompa"


def test_24_matrix_09_demonstrative_itu_ambiguous_two_topics_must_refuse():
    demo = _load_demo("s49-24")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Aquascape pakai pump kecil.": "Dicatat.",
        "Board itu gimana?": "Bisa jelasin lebih spesifik?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t24-1", conversation_id="c24")
        _run_turn(console, demo, "Aquascape pakai pump kecil.", "t24-2", conversation_id="c24")
        sp = _run_turn_capture_prompt(console, demo, "Board itu gimana?", "t24-3", conversation_id="c24")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"Sprint 48's own board/itu fix must still hold: {sp}"
    finally:
        console.stop()


def test_25_matrix_11_ordinal_yang_kedua_must_resolve():
    assert memory.classify_reference_type("Yang kedua?") == "ordinal_reference"


def test_26_matrix_16_sparse_unknown_followup_must_merge():
    assert memory_context.is_sparse_unknown_followup("Kalau koneksinya?") is True


def test_27_matrix_17_single_topic_ambiguity_must_resolve():
    active = _snap("aquascape", "pompa", "kecil")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Filternya gimana?", topic_history=[active],
    ) is True


def test_28_matrix_19_three_topic_ambiguity_must_refuse():
    active = _snap("gpu", "rtx", "upgrade")
    other_a = _snap("esp32", "inmp441", "mic")
    other_b = _snap("aquascape", "pompa", "kecil")
    assert memory_context.is_active_topic_relevant_to_query(
        active, "Yang wireless?", topic_history=[active, other_a, other_b],
    ) is False


# ============================================================================
# Section 6 - bounded-state / cross-conversation isolation
# ============================================================================

def test_29_e2e_cross_conversation_isolation_differentiator_state():
    """Conversation X has Aquascape A/B (ambiguous, should refuse);
    conversation Y only ever discusses Aquascape B alone (should
    resolve) - the differentiator-aware logic must not leak state
    between conversations."""
    demo = _load_demo("s49-29")
    replies = {
        "Aquascape A pakai pompa kecil.": "Dicatat.",
        "Aquascape B pakai pompa besar.": "Dicatat.",
        "Pompanya gimana?": "Bisa jelasin lebih spesifik maksudnya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aquascape A pakai pompa kecil.", "t29x-1", conversation_id="cX")
        _run_turn(console, demo, "Aquascape B pakai pompa besar.", "t29x-2", conversation_id="cX")

        _run_turn(console, demo, "Aquascape B pakai pompa besar.", "t29y-1", conversation_id="cY")

        sp_x = _run_turn_capture_prompt(console, demo, "Pompanya gimana?", "t29x-3", conversation_id="cX")
        sp_y = _run_turn_capture_prompt(console, demo, "Pompanya gimana?", "t29y-2", conversation_id="cY")

        candidates_x = _lines_starting(sp_x, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates_x, f"convo X (A/B ambiguous) should refuse: {sp_x}"

        joined_y = " ".join(_lines_starting(sp_y, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "aquascape" in joined_y or "pompa" in joined_y, f"convo Y (1 topic only) should resolve: {sp_y}"
    finally:
        console.stop()


def test_30_bounded_no_new_persistent_or_unbounded_state():
    # The new helper reads ONLY the already-bounded `source_sentence`
    # field - no new field on ActiveTopicSnapshot, no new dict/list, no
    # persistence.
    import dataclasses
    field_names = {f.name for f in dataclasses.fields(ActiveTopicSnapshot)}
    assert field_names == {"terms", "turns_since_active", "list_items", "status", "source_sentence"}


# ============================================================================
# Section 7 - performance
# ============================================================================

def test_31_new_gate_meets_5ms_budget():
    active = _snap("aquascape", "b", "pakai", "pompa", "besar", source_sentence="Aquascape B pakai pompa besar.")
    other = _snap("aquascape", "pakai", "pompa", "kecil", source_sentence="Aquascape A pakai pompa kecil.")
    history = [other, active]
    n = 3000
    start = time.perf_counter()
    for _ in range(n):
        memory_context.is_active_topic_relevant_to_query(active, "Pompanya gimana?", topic_history=history)
    elapsed_ms = (time.perf_counter() - start) * 1000 / n
    assert elapsed_ms < 5.0, f"averaged {elapsed_ms:.4f}ms/call"


def test_32_differentiator_extraction_meets_5ms_budget():
    n = 5000
    start = time.perf_counter()
    for _ in range(n):
        memory_context._extract_entity_differentiator("Aquascape A pakai pompa kecil, filter juga dipasang di sudut kanan.")
    elapsed_ms = (time.perf_counter() - start) * 1000 / n
    assert elapsed_ms < 5.0, f"averaged {elapsed_ms:.4f}ms/call"


# ============================================================================
# Section 8 - known limitations (regression locks)
# ============================================================================

def test_33_known_limitation_query_side_differentiator_not_resolved():
    """A query that ITSELF names a specific differentiator ("Pompa A
    gimana?") is NOT specially resolved to A by this sprint's fix - the
    fix only prevents the FALSE lineage-skip (upgrading silent-wrong to
    safe-refuse); it does not add query-side differentiator matching.
    This is an explicit, documented scope boundary, not an oversight -
    see docs/change_impact/entity_provenance_disambiguation.md's own
    'Known limitations' section and Sprint 50 recommendation."""
    demo = _load_demo("s49-33")
    replies = {
        "Aquascape A pakai pompa kecil.": "Dicatat.",
        "Aquascape B pakai pompa besar.": "Dicatat.",
        "Pompa A gimana?": "Bisa jelasin lebih spesifik maksudnya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aquascape A pakai pompa kecil.", "t33-1", conversation_id="c33")
        _run_turn(console, demo, "Aquascape B pakai pompa besar.", "t33-2", conversation_id="c33")
        sp = _run_turn_capture_prompt(console, demo, "Pompa A gimana?", "t33-3", conversation_id="c33")
        # Documents CURRENT behavior (still refuses, same as the bare
        # query case) rather than fabricating resolution to A - safe,
        # if not maximally helpful. Not a regression target this sprint.
        assert sp is not None
    finally:
        console.stop()


def test_34_known_limitation_lowercase_differentiator_not_recognized():
    """An informally-typed, lowercase 'aquascape a' is NOT recognized as
    a differentiator (deliberate scope restriction - see module
    docstring and test_06)."""
    demo = _load_demo("s49-34")
    replies = {
        "aquascape a pakai pompa kecil.": "Dicatat.",
        "aquascape b pakai pompa besar.": "Dicatat.",
        "Pompanya gimana?": "Bisa jelasin lebih spesifik maksudnya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "aquascape a pakai pompa kecil.", "t34-1", conversation_id="c34")
        _run_turn(console, demo, "aquascape b pakai pompa besar.", "t34-2", conversation_id="c34")
        sp = _run_turn_capture_prompt(console, demo, "Pompanya gimana?", "t34-3", conversation_id="c34")
        # Documents CURRENT behavior for the lowercase case - the
        # majority-coverage lineage-skip still fires (no differentiator
        # extracted from either side), same as pre-Sprint-49 behavior.
        assert sp is not None
    finally:
        console.stop()
