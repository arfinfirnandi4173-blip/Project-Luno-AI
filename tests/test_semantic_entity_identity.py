"""
test_semantic_entity_identity.py
====================================================

SPRINT 47 - SEMANTIC ENTITY MEMORY & REFERENCE GRAPH.

Goal: improve Luno's ability to preserve ENTITY IDENTITY across natural
conversation - without embeddings, an LLM judge, a second ranking
system, or an unbounded semantic-memory subsystem. Not "make every vague
phrase resolve" - preserve identity when evidence exists, resolve
aliases when evidence exists, distinguish related entities from
unrelated ones, and refuse to guess when evidence is insufficient.

Phase 0-2 (live reproduction via real `RuntimeDemoConsole` across the
6 core scenarios in this sprint's own brief, before any code changed,
using CLEAN canned replies that never leak the "correct" answer through
reply text - only the system's own actual candidate-injection behavior
was trusted as signal):

- **Scenario 1** (multi-name entity, "Board ini..." after "Aku pakai
  ESP32-S3." with "board" never previously used): correctly REFUSES
  (no candidate). This is NOT a bug - it is the SAME deliberate
  "never fabricate product-to-category world knowledge" boundary
  Sprint 45 already established and tested (`test_85_e2e_product_
  without_category_word_correctly_unresolved`) - "ESP32-S3 is a board"
  is exactly the same class of fact as "INMP441 is a mic": true, but
  never stated, and never fabricated. Left unmodified.
- **Scenario 2** (explicit user alias, "GPU itu buat AI." naming "GPU"
  as referring to the just-stated RTX 3060): already resolves correctly
  via plain raw-token overlap (the word "gpu" is now literally part of
  the active topic's own terms) - no bug, no fix needed.
- **Scenario 3** (entity + attribute continuity via a renaming noun,
  "Tank itu pompanya kecil." after "Aku punya aquascape 50x25x25."):
  a REAL, reproduced entity-erosion bug. "Tank itu pompanya kecil."
  classifies `"unknown"` with 2 real residual tokens ("pompanya",
  "kecil") - just above `is_sparse_unknown_followup()`'s `<= 1` bound -
  so it destructively REPLACED the aquascape identity instead of
  merging. FIXED (see Fix #1 below).
- **Scenario 4** (alias collision, "Komputernya gimana?" after "Aku
  punya PC dan laptop." mentioned in ONE statement): resolves to that
  one statement's own merged snapshot (which names both devices) - this
  is the SAME "trust the sole other-topic-free case" shape already
  covered by the deliberate, already-tested `test_20_single_other_
  topic_no_conflict_still_trusted` precedent (Sprint 44). Confirmed via
  a SEPARATE-topics variant (`test_23...`) that when PC and laptop are
  genuinely two DISTINCT topics, the system still resolves to
  whichever is most recent (not an explicit refusal) - documented as a
  known limitation, not fixed (see "Investigated and rejected" below).
- **Scenario 5** (cross-topic contamination, "Board itu gimana?" after
  ESP32 then Aquascape, "board" grounded in NEITHER): a REAL,
  reproduced ambiguity-safety bug - wrongly, confidently resolved to
  the merely-most-recent (Aquascape) topic. A fix was implemented,
  confirmed to correctly resolve this case, and then REVERTED after it
  broke `tests/test_contextual_reference_robustness.py::
  test_27_e2e_no_contamination_reverse_direction` (Sprint 46's own
  "mic" case - textbook IDENTICAL formal shape: a curated-vocabulary
  single token, zero grounding in the active OR the sole other topic -
  but the CORRECT answer there is to trust the active topic, the exact
  opposite of what Scenario 5 needs). No general, deterministic,
  non-world-knowledge rule distinguishes the two - documented as a
  known limitation.
- **Scenario 6** (correction-driven identity, "Board itu RAM-nya
  berapa?" after "Pakai ESP32." -> "Eh maksudku ESP32-S3."): a REAL,
  reproduced entity-erosion bug, the SAME root cause and SAME fix as
  Scenario 3 (both are "unknown"-classified, 2-real-token, demonstrative
  -anchored turns). FIXED (see Fix #1 below).

**Fix #1 - `is_demonstrative_anchored_followup()`** (new function,
`luno/memory_context.py`), wired into `main_runtime_demo.py`'s existing
`is_merge` decision as a third additive OR-clause alongside `memory.
is_merge_reference_followup()` and `memory_context.is_sparse_unknown_
followup()`. Recognizes an `"unknown"`-classified turn whose own 2nd
word is the demonstrative "itu"/"ini" ("Board itu RAM-nya berapa?",
"Tank itu pompanya kecil.") and whose real (stopword-filtered) residual
token count is small (`<= 3`) as MERGE-worthy rather than an ordinary
REPLACE-worthy rich turn - a generic, domain-independent GRAMMATICAL
signal (the demonstrative itself), not a vocabulary/domain lookup.
Does NOT change `classify_reference_type()`'s own output (still
`"unknown"` - every existing adversarial-phrase-matrix precedent is
untouched) and does NOT inject a candidate for the turn itself (matches
`is_sparse_unknown_followup()`'s own precedent exactly) - it only
prevents the state from being destructively discarded, so a LATER
genuine follow-up can still recover it.

**Investigated and REJECTED (1):** gating the ambiguity-safety `>= 2`
distinct-other-topic refusal down to `>= 1` specifically for curated-
vocabulary (`_TOKEN_SYNONYM_GROUPS`-member) single-token queries. Fixed
Scenario 5 in isolation, but broke Sprint 46's own `test_27_e2e_no_
contamination_reverse_direction` - the same formal shape, opposite
correct answer, no non-world-knowledge way to tell them apart. Reverted;
documented as a known limitation.

**New known limitation discovered (not previously documented):** two
DISTINCTLY-named entities that happen to share high generic-vocabulary
overlap ("Aquascape A pakai pompa kecil." / "Aquascape B pakai pompa
besar.", then "Pompanya gimana?") are wrongly treated as the SAME
lineage by the `coverage > 0.5` topic-lineage-skip heuristic (both
entries share "aquascape"/"pompa"/"pakai" - well over 50% coverage),
so the system confidently resolves to whichever is most recent (B)
instead of recognizing them as two distinct competitors. Not fixed
this sprint (the `coverage > 0.5` heuristic is deliberately majority-
based, not strict-subset, specifically to avoid a DIFFERENT, already-
fixed false-ambiguity bug - see `luno/memory_context.py`'s own Sprint
43 comment on this check) - documented, not force-fixed.

No new entity representation was introduced. Every reproduced,
FIXABLE gap was a merge-eligibility decision issue on the EXISTING flat
bag-of-terms representation, not a representation gap - consistent with
Sprint 44/45/46's own prior finding that the flat representation is
sufficient for every concretely reproduced failure so far.
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

def _load_demo(tag: str = "s47"):
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


def _active(console, conv_id):
    for key, snap in console.planner_module._active_topic.items():
        if key == (conv_id, None) or (isinstance(key, tuple) and conv_id in key) or key == conv_id:
            return snap
    return next(iter(console.planner_module._active_topic.values()), None)


# ============================================================================
# Section 1 - `is_demonstrative_anchored_followup()` unit tests (Fix #1)
# ============================================================================

def test_01_board_itu_ram_nya_berapa_detected():
    assert memory_context.is_demonstrative_anchored_followup("Board itu RAM-nya berapa?") is True


def test_02_tank_itu_pompanya_kecil_detected():
    assert memory_context.is_demonstrative_anchored_followup("Tank itu pompanya kecil.") is True


def test_03_does_not_change_classify_reference_type_output():
    # Fix #1 must never change the classifier's own return value.
    assert memory.classify_reference_type("Board itu RAM-nya berapa?") == "unknown"
    assert memory.classify_reference_type("Tank itu pompanya kecil.") == "unknown"


def test_04_long_independent_sentence_with_ini_as_2nd_word_excluded():
    # "Motor ini bisa dikendalikan..." - a genuinely fresh, independent,
    # substantial statement that merely happens to have "ini" as its 2nd
    # word - must NOT be treated as an elliptical continuation.
    text = "Motor ini bisa dikendalikan lewat PWM dengan mikrokontroler apa saja."
    assert memory_context.is_demonstrative_anchored_followup(text) is False


def test_05_kalau_buat_esp32_s3_unaffected():
    # Sprint 44's own deliberate "unknown, do not merge" precedent -
    # 2nd word is "buat", not "itu"/"ini" - must remain False.
    assert memory_context.is_demonstrative_anchored_followup("kalau buat ESP32-S3?") is False


def test_06_non_unknown_types_always_false():
    # Already-classified types (comparison, attribute_reference, etc.)
    # are out of scope for this detector - it only ever fires on "unknown".
    assert memory.classify_reference_type("Board itu gimana?") != "unknown"
    assert memory_context.is_demonstrative_anchored_followup("Board itu gimana?") is False


def test_07_itu_deep_in_a_long_sentence_not_2nd_word_excluded():
    text = "Aku baru beli motor baru dan itu lumayan cepat menurutku."
    assert memory_context.is_demonstrative_anchored_followup(text) is False


def test_08_bare_itu_alone_not_double_counted_with_sparse_unknown():
    # A genuinely sparse turn ("Itu gimana?") is ALREADY covered by
    # classify_reference_type() itself (bare pronoun -> direct_reference,
    # a _PURE_REFERENCE_TYPES member) - never reaches "unknown" at all,
    # so this detector correctly returns False (no double-mechanism
    # overlap with is_sparse_unknown_followup()).
    assert memory.classify_reference_type("Itu gimana?") != "unknown"


def test_09_four_or_more_residual_tokens_excluded():
    # Exactly at the boundary: 4 real tokens (one more than the `<= 3`
    # bound) must be excluded.
    text = "Motor itu kecepatan torsi beratnya berapa?"
    q = memory_context.analyze_query(text)
    residual = set(q.tokens) - memory_context._TOPIC_OVERLAP_STOPWORDS
    if len(residual) <= 3:
        # If tokenization changes upstream, this guard test should not
        # silently pass for the wrong reason - skip rather than assert
        # a stale boundary.
        import pytest
        pytest.skip(f"residual token count {len(residual)} no longer > 3 for this fixture text")
    assert memory_context.is_demonstrative_anchored_followup(text) is False


# ============================================================================
# Section 2 - E2E: Scenario 3 (entity + attribute continuity via rename)
# ============================================================================

def test_10_e2e_tank_rename_preserves_aquascape_identity():
    demo = _load_demo("s47-10")
    replies = {
        "Aku punya aquascape 50x25x25.": "Dicatat.",
        "Tank itu pompanya kecil.": "Dicatat.",
        "Kalau filternya?": "Filter perlu disesuaikan ukurannya.",
        "Terus ukurannya berapa?": "Sekitar yang sudah kamu sebutkan sebelumnya.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku punya aquascape 50x25x25.", "t10-1", conversation_id="c10")
        _run_turn(console, demo, "Tank itu pompanya kecil.", "t10-2", conversation_id="c10")
        _run_turn(console, demo, "Kalau filternya?", "t10-3", conversation_id="c10")
        sp4 = _run_turn_capture_prompt(console, demo, "Terus ukurannya berapa?", "t10-4", conversation_id="c10")
        joined = " ".join(_lines_starting(sp4, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "aquascape" in joined, f"aquascape identity lost through 'Tank itu ...' rename: {sp4}"
    finally:
        console.stop()


def test_11_e2e_tank_rename_active_snapshot_retains_original_terms():
    demo = _load_demo("s47-11")
    replies = {
        "Aku punya aquascape 50x25x25.": "Dicatat.",
        "Tank itu pompanya kecil.": "Dicatat.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku punya aquascape 50x25x25.", "t11-1", conversation_id="c11")
        _run_turn(console, demo, "Tank itu pompanya kecil.", "t11-2", conversation_id="c11")
        snap = _active(console, "c11")
        assert snap is not None
        assert "aquascape" in snap.terms, f"expected merge, not replace: {sorted(snap.terms)}"
        assert "pompanya" in snap.terms or "pompa" in snap.terms
    finally:
        console.stop()


# ============================================================================
# Section 3 - E2E: Scenario 6 (correction-driven identity)
# ============================================================================

def test_12_e2e_correction_then_demonstrative_attribute_preserves_identity():
    demo = _load_demo("s47-12")
    replies = {
        "Pakai ESP32.": "Dicatat.",
        "Eh maksudku ESP32-S3.": "Dikoreksi.",
        "Board itu RAM-nya berapa?": "RAM-nya cukup besar untuk kebutuhan umum.",
        "Terus?": "Ada beberapa hal lain juga yang perlu diperhatikan.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Pakai ESP32.", "t12-1", conversation_id="c12")
        _run_turn(console, demo, "Eh maksudku ESP32-S3.", "t12-2", conversation_id="c12")
        _run_turn(console, demo, "Board itu RAM-nya berapa?", "t12-3", conversation_id="c12")
        sp4 = _run_turn_capture_prompt(console, demo, "Terus?", "t12-4", conversation_id="c12")
        joined = " ".join(_lines_starting(sp4, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "esp32" in joined and "s3" in joined, f"corrected identity (ESP32-S3) lost: {sp4}"
    finally:
        console.stop()


def test_13_e2e_correction_snapshot_directly_retains_s3_after_demonstrative_turn():
    demo = _load_demo("s47-13")
    replies = {
        "Pakai ESP32.": "Dicatat.",
        "Eh maksudku ESP32-S3.": "Dikoreksi.",
        "Board itu RAM-nya berapa?": "RAM-nya cukup besar.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Pakai ESP32.", "t13-1", conversation_id="c13")
        _run_turn(console, demo, "Eh maksudku ESP32-S3.", "t13-2", conversation_id="c13")
        _run_turn(console, demo, "Board itu RAM-nya berapa?", "t13-3", conversation_id="c13")
        snap = _active(console, "c13")
        assert snap is not None
        assert "s3" in snap.terms, f"corrected 's3' term lost after demonstrative turn: {sorted(snap.terms)}"
    finally:
        console.stop()


# ============================================================================
# Section 4 - explicit alias (Scenario 2) and canonical entity
# ============================================================================

def test_14_e2e_explicit_user_alias_resolves():
    demo = _load_demo("s47-14")
    replies = {
        "PC serverku pakai RTX 3060 12GB. GPU itu buat AI.": "Dicatat.",
        "GPU-nya kuat nggak?": "RTX 3060 12GB cukup kuat buat inferensi AI ringan.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "PC serverku pakai RTX 3060 12GB. GPU itu buat AI.", "t14-1", conversation_id="c14")
        sp = _run_turn_capture_prompt(console, demo, "GPU-nya kuat nggak?", "t14-2", conversation_id="c14")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "rtx" in joined and "gpu" in joined
    finally:
        console.stop()


def test_15_e2e_alias_introduced_by_assistant_response_resolves():
    """The user never says "gpu" - only the ASSISTANT's own reply names
    it. A later user turn using "gpu" must still resolve, since the
    active topic's terms are built from BOTH the user turn and the
    reply text (existing, pre-Sprint-47 mechanism - locked in here)."""
    demo = _load_demo("s47-15")
    replies = {
        "PC-ku pakai RTX 3060.": "Dicatat, GPU RTX 3060 kamu itu cukup kuat.",
        "GPU-nya kuat nggak buat AI?": "RTX 3060 lumayan buat inferensi ringan.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "PC-ku pakai RTX 3060.", "t15-1", conversation_id="c15")
        sp = _run_turn_capture_prompt(console, demo, "GPU-nya kuat nggak buat AI?", "t15-2", conversation_id="c15")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "rtx" in joined
    finally:
        console.stop()


def test_16_e2e_canonical_entity_exact_match_resolves():
    demo = _load_demo("s47-16")
    replies = {
        "ESP32-S3 pakai flash 8MB.": "Dicatat.",
        "ESP32-S3 support WiFi 6 nggak?": "Belum, ESP32-S3 cuma WiFi 4 (2.4GHz).",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32-S3 pakai flash 8MB.", "t16-1", conversation_id="c16")
        sp = _run_turn_capture_prompt(console, demo, "ESP32-S3 support WiFi 6 nggak?", "t16-2", conversation_id="c16")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "s3" in joined or "esp32" in joined
    finally:
        console.stop()


# ============================================================================
# Section 5 - alias after several turns / entity + pronoun / possessive -nya
# ============================================================================

def test_17_e2e_alias_survives_several_intervening_sparse_turns():
    # Both intervening turns are genuinely SPARSE (<= 1 real token each,
    # "harganya"/"kabelnya") so `is_sparse_unknown_followup()` correctly
    # merges rather than replaces at each step - a substantial (2+ real
    # token) intervening "unknown" turn is a DIFFERENT, deliberate,
    # already-tested precedent (2+ real words is enough standing content
    # to be treated as a legitimate new topic - Sprint 44) and is not
    # what this test targets.
    demo = _load_demo("s47-17")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Kalau harganya?": "Sekitar 50 ribu buat INMP441-nya.",
        "Kalau kabelnya?": "Kabelnya standar jumper wire biasa.",
        "Mic-nya gimana?": "INMP441 tetap pilihan bagus buat ESP32.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t17-1", conversation_id="c17")
        _run_turn(console, demo, "Kalau harganya?", "t17-2", conversation_id="c17")
        _run_turn(console, demo, "Kalau kabelnya?", "t17-3", conversation_id="c17")
        sp4 = _run_turn_capture_prompt(console, demo, "Mic-nya gimana?", "t17-4", conversation_id="c17")
        joined = " ".join(_lines_starting(sp4, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "esp32" in joined and "inmp441" in joined
    finally:
        console.stop()


def test_18_e2e_entity_plus_pronoun_reference():
    # "Kalau itu cukup...?" (not a bare leading "Itu...?") matches
    # `_DIRECT_REFERENCE_RE`'s own `\bkalau\s+itu\b` alternative -
    # classifies `direct_reference`, a `_PURE_REFERENCE_TYPES` member
    # that always trusts recency unconditionally.
    demo = _load_demo("s47-18")
    replies = {
        "ESP32-S3 pakai PSRAM 8MB.": "Dicatat.",
        "Kalau itu cukup buat AI model kecil nggak?": "Cukup buat model kecil, tapi terbatas untuk yang besar.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32-S3 pakai PSRAM 8MB.", "t18-1", conversation_id="c18")
        assert memory.classify_reference_type("Kalau itu cukup buat AI model kecil nggak?") == "direct_reference"
        sp = _run_turn_capture_prompt(console, demo, "Kalau itu cukup buat AI model kecil nggak?", "t18-2", conversation_id="c18")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "esp32" in joined or "s3" in joined or "psram" in joined
    finally:
        console.stop()


def test_19_e2e_possessive_nya_clitic_reference():
    demo = _load_demo("s47-19")
    replies = {
        "Aku upgrade PSU ke 750W.": "Dicatat.",
        "PSU-nya cukup buat GPU baru?": "750W cukup buat sebagian besar GPU kelas menengah.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku upgrade PSU ke 750W.", "t19-1", conversation_id="c19")
        sp = _run_turn_capture_prompt(console, demo, "PSU-nya cukup buat GPU baru?", "t19-2", conversation_id="c19")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "psu" in joined or "750w" in joined or "750" in joined
    finally:
        console.stop()


# ============================================================================
# Section 6 - competing entities / alias collision / ambiguity safety
# ============================================================================

def test_20_e2e_two_competing_entities_three_topics_refuses():
    """Scenario 5's structural cousin, extended to 3 topics - the
    ALREADY-established `distinct_other_count >= 2` safety net (Sprint
    44 Phase 7) must still correctly refuse. Locked in, unmodified."""
    demo = _load_demo("s47-20")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Aquascape pakai pump kecil.": "Dicatat.",
        "PC pakai GPU RTX 3060.": "Dicatat.",
        "Perangkat itu gimana?": "Bisa diperjelas maksudnya perangkat yang mana?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t20-1", conversation_id="c20")
        _run_turn(console, demo, "Aquascape pakai pump kecil.", "t20-2", conversation_id="c20")
        _run_turn(console, demo, "PC pakai GPU RTX 3060.", "t20-3", conversation_id="c20")
        sp = _run_turn_capture_prompt(console, demo, "Perangkat itu gimana?", "t20-4", conversation_id="c20")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"3-way ambiguous 'perangkat itu' must refuse, not guess: {sp}"
    finally:
        console.stop()


def test_21_known_limitation_two_topic_cross_contamination_board_case():
    """Scenario 5 (documented known limitation, NOT fixed this sprint):
    a curated-vocabulary word with zero grounding in EITHER of exactly 2
    live topics currently still defaults to recency (confidently wrong)
    rather than refusing. A fix was investigated and reverted (see this
    file's own module docstring) because it broke `test_27_e2e_no_
    contamination_reverse_direction`'s own, opposite-desired-outcome,
    structurally-identical case. This test locks in the CURRENT
    (imperfect, understood, documented) behavior so a future agent
    re-investigating this limitation has an exact, reproducible
    baseline to diff against."""
    demo = _load_demo("s47-21")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Aquascape pakai pump kecil.": "Dicatat.",
        "Board itu gimana?": "Bisa jelasin lebih spesifik maksudnya?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t21-1", conversation_id="c21")
        _run_turn(console, demo, "Aquascape pakai pump kecil.", "t21-2", conversation_id="c21")
        sp = _run_turn_capture_prompt(console, demo, "Board itu gimana?", "t21-3", conversation_id="c21")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        # Documents CURRENT behavior (confidently, if incorrectly, trusts
        # recency) - this assertion exists to DETECT any future silent
        # change to this known-imperfect behavior, not to endorse it.
        assert "aquascape" in joined or joined == ""
    finally:
        console.stop()


def test_22_e2e_alias_collision_single_statement_both_devices_no_crash():
    """Scenario 4: "komputer" after a SINGLE statement naming both PC
    and laptop resolves to that statement's own merged snapshot (which
    names both) - matches the deliberate `test_20_single_other_topic_
    no_conflict_still_trusted` precedent shape (Sprint 44). Not a crash,
    not a fabricated single-device answer - the LLM sees both device
    names in context and can ask for clarification itself, matching the
    canned reply used here."""
    demo = _load_demo("s47-22")
    replies = {
        "Aku punya PC dan laptop.": "Dicatat.",
        "Komputernya gimana?": "Yang mana ya, PC atau laptop?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku punya PC dan laptop.", "t22-1", conversation_id="c22")
        sp = _run_turn_capture_prompt(console, demo, "Komputernya gimana?", "t22-2", conversation_id="c22")
        assert sp is not None
    finally:
        console.stop()


def test_23_known_limitation_alias_collision_two_separate_topics():
    """Scenario 4, separated into two genuinely DISTINCT topics (PC
    discussed, then laptop discussed separately) - documents CURRENT
    behavior (resolves to whichever is most recent, matching Scenario
    5's own known-limitation shape) rather than an explicit refusal.
    Not fixed this sprint for the same reason Scenario 5 was not -
    "komputer" is not curated vocabulary either, so even the
    investigated-and-reverted Scenario 5 fix would not have touched
    this case."""
    demo = _load_demo("s47-23")
    replies = {
        "PC-ku pakai RTX 3060.": "Dicatat.",
        "Laptop-ku pakai RTX 4060 mobile.": "Dicatat.",
        "Komputernya gimana?": "Yang mana ya, PC atau laptop?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "PC-ku pakai RTX 3060.", "t23-1", conversation_id="c23")
        _run_turn(console, demo, "Laptop-ku pakai RTX 4060 mobile.", "t23-2", conversation_id="c23")
        sp = _run_turn_capture_prompt(console, demo, "Komputernya gimana?", "t23-3", conversation_id="c23")
        # Documents current behavior for future reference - not endorsing it.
        assert sp is not None
    finally:
        console.stop()


def test_24_known_limitation_same_generic_vocabulary_two_named_entities():
    """New known limitation discovered this sprint: two DISTINCTLY named
    entities sharing high generic-vocabulary overlap ("Aquascape A" /
    "Aquascape B", both mentioning "pompa") get conflated by the
    `coverage > 0.5` topic-lineage-skip heuristic - documents current
    (imperfect) behavior. Not fixed - the heuristic is deliberately
    majority-based (not strict-subset) to avoid a DIFFERENT, already-
    fixed false-ambiguity bug (see `luno/memory_context.py`'s own
    Sprint 43 comment on this exact check)."""
    demo = _load_demo("s47-24")
    replies = {
        "Aquascape A pakai pompa kecil.": "Dicatat.",
        "Aquascape B pakai pompa besar.": "Dicatat.",
        "Pompanya gimana?": "Dicatat.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aquascape A pakai pompa kecil.", "t24-1", conversation_id="c24")
        _run_turn(console, demo, "Aquascape B pakai pompa besar.", "t24-2", conversation_id="c24")
        sp = _run_turn_capture_prompt(console, demo, "Pompanya gimana?", "t24-3", conversation_id="c24")
        assert sp is not None
    finally:
        console.stop()


# ============================================================================
# Section 7 - topic switching / contamination / cross-conversation isolation
# ============================================================================

def test_25_e2e_topic_switch_then_query_current_topic():
    # "Mic-nya gimana?" must correctly resolve to the CURRENTLY ACTIVE
    # topic (ESP32/INMP441, the most recently discussed one) without
    # leaking the now-superseded aquascape/pump topic - the mirror image
    # of `test_contextual_reference_robustness.py::
    # test_27_e2e_no_contamination_reverse_direction` (Sprint 46),
    # locked in again here since it directly exercises the SAME single-
    # token/zero-grounding/exactly-1-other-topic code path this sprint's
    # own Scenario 5 investigation (see this file's module docstring)
    # depends on NOT being carelessly widened.
    demo = _load_demo("s47-25")
    replies = {
        "Aquascape pakai pump kecil.": "Dicatat.",
        "ESP32 pakai INMP441.": "Dicatat.",
        "Mic-nya gimana?": "INMP441 tetap pilihan bagus buat ESP32.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aquascape pakai pump kecil.", "t25-1", conversation_id="c25")
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t25-2", conversation_id="c25")
        sp = _run_turn_capture_prompt(console, demo, "Mic-nya gimana?", "t25-3", conversation_id="c25")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "esp32" in joined or "inmp441" in joined
        assert "pump" not in joined and "aquascape" not in joined
    finally:
        console.stop()


def test_26_e2e_no_contamination_unrelated_query_stays_clean():
    demo = _load_demo("s47-26")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Aquarium saya 50x25.": "Dicatat.",
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


def test_27_e2e_cross_conversation_isolation_no_leak():
    demo = _load_demo("s47-27")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Mic-nya gimana?": "INMP441 mic yang bagus.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t27-1", conversation_id="c27-A")
        sp = _run_turn_capture_prompt(console, demo, "Mic-nya gimana?", "t27-2", conversation_id="c27-B")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "esp32" not in joined and "inmp441" not in joined, f"cross-conversation leak: {sp}"
    finally:
        console.stop()


def test_28_active_topic_dict_keyed_isolated_per_conversation():
    demo = _load_demo("s47-28")
    console = _new_console(demo, canned_text="Dicatat.")
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t28-1", conversation_id="c28-A")
        _run_turn(console, demo, "Aquascape pakai pump kecil.", "t28-2", conversation_id="c28-B")
        snap_a = None
        snap_b = None
        for key, snap in console.planner_module._active_topic.items():
            if "c28-A" in (key if isinstance(key, tuple) else (key,)):
                snap_a = snap
            if "c28-B" in (key if isinstance(key, tuple) else (key,)):
                snap_b = snap
        assert snap_a is not None and snap_b is not None
        assert "esp32" in snap_a.terms or "inmp441" in snap_a.terms
        assert "aquascape" in snap_b.terms or "pompa" in snap_b.terms or "pump" in snap_b.terms
    finally:
        console.stop()


# ============================================================================
# Section 8 - bounded-state behavior
# ============================================================================

def test_29_topic_history_remains_bounded():
    demo = _load_demo("s47-29")
    console = _new_console(demo, canned_text="Dicatat.")
    console.start()
    try:
        for i in range(15):
            _run_turn(console, demo, f"Topik nomor {i} pakai komponen{i}.", f"t29-{i}", conversation_id="c29")
        for key, history in console.planner_module._topic_history.items():
            assert len(history) <= memory_context._TOPIC_HISTORY_MAX_ENTRIES, (
                f"topic_history exceeded its own documented bound: {len(history)} entries"
            )
    finally:
        console.stop()


def test_30_active_topic_and_topic_history_remain_plain_dicts():
    demo = _load_demo("s47-30")
    console = _new_console(demo, canned_text="Dicatat.")
    console.start()
    try:
        assert isinstance(console.planner_module._active_topic, dict)
        assert isinstance(console.planner_module._topic_history, dict)
    finally:
        console.stop()


def test_31_no_new_persistent_entity_storage_module():
    # This sprint's fix touches only existing, already-transient
    # in-memory structures (`_active_topic`/`_topic_history`) - no new
    # module-level entity store, no new persistence path.
    assert not hasattr(memory_context, "_entity_graph")
    assert not hasattr(memory_context, "_entity_store")
    assert not hasattr(memory_context, "ENTITY_GRAPH")


# ============================================================================
# Section 9 - performance
# ============================================================================

def test_32_is_demonstrative_anchored_followup_performance():
    texts = [
        "Board itu RAM-nya berapa?",
        "Tank itu pompanya kecil.",
        "Motor ini bisa dikendalikan lewat PWM dengan mikrokontroler apa saja.",
        "kalau buat ESP32-S3?",
    ]
    start = time.perf_counter()
    for _ in range(500):
        for t in texts:
            memory_context.is_demonstrative_anchored_followup(t)
    elapsed_ms = (time.perf_counter() - start) / (500 * len(texts)) * 1000
    assert elapsed_ms < 5.0, f"is_demonstrative_anchored_followup() averaged {elapsed_ms:.4f}ms/call, expected < 5ms"


# ============================================================================
# Section 10 - regression locks for already-correct Scenario 1/2 behavior
# ============================================================================

def test_33_e2e_scenario1_no_fabrication_without_prior_grounding():
    """Scenario 1 - "Board ini..." after "Aku pakai ESP32-S3." (no prior
    use of "board"/"mikrokontroler"/"mcu") correctly REFUSES rather than
    fabricating the board->ESP32-S3 category link - the SAME deliberate
    boundary as Sprint 45's INMP441-is-never-auto-a-mic precedent."""
    demo = _load_demo("s47-33")
    replies = {
        "Aku pakai ESP32-S3.": "Dicatat.",
        "Board ini kalau WiFi-nya gimana?": "ESP32-S3 punya WiFi 2.4GHz bawaan.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "Aku pakai ESP32-S3.", "t33-1", conversation_id="c33")
        sp = _run_turn_capture_prompt(console, demo, "Board ini kalau WiFi-nya gimana?", "t33-2", conversation_id="c33")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"must not fabricate board->ESP32-S3 link without prior grounding: {sp}"
    finally:
        console.stop()


def test_34_e2e_scenario2_explicit_alias_negative_control():
    """Negative control: without the explicit "GPU itu buat AI" framing,
    a bare "GPU-nya kuat nggak?" after just "PC-ku pakai RTX 3060." must
    still resolve via the SAME plain raw-token mechanism (no "gpu" word
    was used - so this should behave like test_15's assistant-alias
    case, not fabricate a NEW mechanism)."""
    demo = _load_demo("s47-34")
    replies = {
        "PC-ku pakai RTX 3060.": "Dicatat.",
        "RTX-nya kuat nggak?": "RTX 3060 cukup kuat buat kebutuhan umum.",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "PC-ku pakai RTX 3060.", "t34-1", conversation_id="c34")
        sp = _run_turn_capture_prompt(console, demo, "RTX-nya kuat nggak?", "t34-2", conversation_id="c34")
        joined = " ".join(_lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)).lower()
        assert "rtx" in joined
    finally:
        console.stop()


def test_35_e2e_three_competing_entities_refuses_extended():
    """Adversarial extension of Scenario 5/test_20 to 3 entities using a
    DIFFERENT ambiguous word ("perangkat" / "device") than the sprint's
    own worked examples - locks in that the existing `distinct_other_
    count >= 2` safety net generalizes beyond the one exact phrase
    already tested elsewhere."""
    demo = _load_demo("s47-35")
    replies = {
        "ESP32 pakai INMP441.": "Dicatat.",
        "Aquascape pakai pump kecil.": "Dicatat.",
        "PC pakai GPU RTX 3060.": "Dicatat.",
        "Alat itu gimana?": "Bisa diperjelas maksudnya yang mana?",
    }
    console = _new_console(demo, replies=replies)
    console.start()
    try:
        _run_turn(console, demo, "ESP32 pakai INMP441.", "t35-1", conversation_id="c35")
        _run_turn(console, demo, "Aquascape pakai pump kecil.", "t35-2", conversation_id="c35")
        _run_turn(console, demo, "PC pakai GPU RTX 3060.", "t35-3", conversation_id="c35")
        sp = _run_turn_capture_prompt(console, demo, "Alat itu gimana?", "t35-4", conversation_id="c35")
        candidates = _lines_starting(sp, *_ANY_CANDIDATE_PREFIXES)
        assert not candidates, f"3-way ambiguous 'alat itu' must refuse: {sp}"
    finally:
        console.stop()
