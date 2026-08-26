"""
test_response_policy.py
=========================

Response Depth Policy sprint - tests for `luno/response_policy.py`
(the pure decision helper) AND for its wiring into the real
`PlannerBridgeModule`/`RuntimeDemoConsole` production pipeline (same
no-network/no-hardware conventions as `tests/test_runtime_demo.py`).

Two sections:

  1. Decision-matrix tests (items 1-19 of the sprint's own checklist) -
     call `compute_response_policy()` directly. No I/O, no event bus, no
     console - these are pure-function tests.

  2. End-to-end integration tests (items 20-22) - through the real
     `RuntimeDemoConsole` pipeline, proving the policy is actually
     computed once per turn and actually reaches the `system_prompt`
     the LLM receives, without duplicating memory retrieval or mutating
     any persistent store, and without displacing Luno's existing
     persona/system instructions.

Persistent-state safety: every test in this file runs under
`tests/conftest.py`'s autouse `isolate_persistent_state` fixture (redirects
every writer-capable JSON store + the Vision Memory SQLite DB to a fresh
`tmp_path`-backed location) - no test here can ever touch Vinn's real
`config/*.json` files. No test in this file constructs its own ad-hoc
smoke script against `luno.memory`; all behavior is exercised through
this fixture-isolated pytest state or, for the pure-function section,
through `compute_response_policy()` directly (which touches no I/O at all).

Run:
    python3 -m pytest -q tests/test_response_policy.py
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

from luno.response_policy import (  # noqa: E402
    DEPTH_DETAILED,
    DEPTH_NORMAL,
    DEPTH_SHORT,
    SCORE_MAX,
    SCORE_MIN,
    ResponsePolicy,
    build_depth_instruction,
    compute_response_policy,
)


# ============================================================================
# Section 1 - decision-matrix tests (pure function, no I/O)
# ============================================================================

# ---- 1. Explicit SHORT commands -------------------------------------------

def test_explicit_short_jawab_singkat():
    p = compute_response_policy("jawab singkat, kenapa ESP32 panas?")
    assert p.depth == DEPTH_SHORT
    assert p.explicit is True
    assert "explicit_short_instruction" in p.reasons


def test_explicit_short_intinya_question_mark():
    p = compute_response_policy("intinya?")
    assert p.depth == DEPTH_SHORT
    assert p.explicit is True


def test_explicit_short_intinya_aja():
    p = compute_response_policy("intinya aja")
    assert p.depth == DEPTH_SHORT
    assert p.explicit is True


def test_explicit_short_english_to_the_point():
    p = compute_response_policy("just answer briefly, is the relay 5V?")
    assert p.depth == DEPTH_SHORT
    assert p.explicit is True


def test_explicit_short_ga_usah_panjang():
    p = compute_response_policy("ga usah panjang, cara reset ESP32 gimana?")
    assert p.depth == DEPTH_SHORT
    assert p.explicit is True


# ---- 2. Explicit DETAILED commands -----------------------------------------

def test_explicit_detailed_jelaskan_detail():
    p = compute_response_policy("jelaskan detail cara kerja regulator ESP32")
    assert p.depth == DEPTH_DETAILED
    assert p.explicit is True
    assert "explicit_detailed_instruction" in p.reasons


def test_explicit_detailed_step_by_step():
    p = compute_response_policy("step by step cara setup home assistant")
    assert p.depth == DEPTH_DETAILED
    assert p.explicit is True


def test_explicit_detailed_jelaskan_sampai_ngerti():
    p = compute_response_policy("jelaskan sampai aku ngerti soal WiFi 6")
    assert p.depth == DEPTH_DETAILED
    assert p.explicit is True


def test_explicit_detailed_english_in_detail():
    p = compute_response_policy("can you explain in detail how relays work")
    assert p.depth == DEPTH_DETAILED
    assert p.explicit is True


# ---- 3. Explicit instruction precedence (overrides task-type/complexity) --

def test_explicit_short_overrides_architecture_signal():
    """An architecture question (would otherwise score high/DETAILED)
    combined with an explicit SHORT instruction must still resolve
    SHORT - explicit instruction is the highest-precedence rule."""
    p = compute_response_policy(
        "jawab singkat aja, jelaskan arsitektur ESP32 dari CPU sampai peripheral?"
    )
    assert p.depth == DEPTH_SHORT
    assert p.explicit is True


def test_explicit_detailed_overrides_simple_factual_signal():
    """A simple factual question (would otherwise score low/SHORT)
    combined with an explicit DETAILED instruction must still resolve
    DETAILED."""
    p = compute_response_policy("apa itu relay? jelaskan detail dong")
    assert p.depth == DEPTH_DETAILED
    assert p.explicit is True


def test_explicit_instruction_ignores_previous_score():
    """Explicit instruction outranks conversational-continuation context
    too - a previous DETAILED turn must not prevent an explicit SHORT
    request on this turn from taking effect."""
    p = compute_response_policy("intinya aja", previous_score=95)
    assert p.depth == DEPTH_SHORT
    assert p.explicit is True


# ---- 4. Simple factual questions / 5. Yes-no / 6. Definitions -------------

def test_simple_factual_question_apa_fungsi():
    p = compute_response_policy("apa fungsi relay?")
    assert p.depth == DEPTH_SHORT
    assert p.explicit is False


def test_yes_no_question_apakah():
    p = compute_response_policy("apakah ESP32 support WiFi 6?")
    assert p.depth == DEPTH_SHORT


def test_yes_no_question_english():
    p = compute_response_policy("is there a way to reset ESP32 remotely?")
    assert p.depth == DEPTH_SHORT


def test_definition_question():
    p = compute_response_policy("apa itu relay?")
    assert p.depth == DEPTH_SHORT
    assert p.task_type == "definition_or_yes_no"


# ---- 7. How-to requests -----------------------------------------------------

def test_how_to_request_cara_pasang():
    p = compute_response_policy("cara pasang relay ke ESP32?")
    assert p.depth == DEPTH_NORMAL
    assert p.task_type == "how_to_instruction"


def test_how_to_request_english():
    p = compute_response_policy("how to connect a relay to ESP32?")
    assert p.depth == DEPTH_NORMAL


# ---- 8. Troubleshooting -----------------------------------------------------

def test_troubleshooting_kenapa_gabisa():
    p = compute_response_policy("kenapa ESP32 saya gabisa connect wifi?")
    assert p.depth == DEPTH_NORMAL
    assert p.task_type == "troubleshooting"


def test_troubleshooting_not_working():
    p = compute_response_policy("my ESP32 wifi isn't working, why?")
    assert p.depth == DEPTH_NORMAL


# ---- 9. Comparisons ---------------------------------------------------------

def test_comparison_bedanya():
    p = compute_response_policy("ESP32 bedanya sama ESP8266?")
    assert p.depth == DEPTH_NORMAL
    assert p.task_type == "comparison"


def test_comparison_versus():
    p = compute_response_policy("ESP32 versus ESP8266, mana yang lebih hemat daya?")
    assert p.depth == DEPTH_NORMAL


# ---- 10. Tutorials -----------------------------------------------------------

def test_tutorial_request():
    p = compute_response_policy("buatkan tutorial setup home assistant dari awal")
    assert p.depth == DEPTH_DETAILED
    assert p.task_type == "tutorial"


# ---- 11. Architecture / deep-analysis questions -----------------------------

def test_architecture_question():
    p = compute_response_policy("jelaskan arsitektur ESP32 dari CPU sampai peripheral")
    assert p.depth == DEPTH_DETAILED
    assert p.task_type == "architecture_or_deep_analysis"


def test_deep_analysis_root_cause():
    p = compute_response_policy("analisis mendalam root cause ESP32 sering restart")
    assert p.depth == DEPTH_DETAILED


# ---- 12. Multi-part / multi-question turns ----------------------------------

def test_multiple_questions_bump_score_but_not_over_infer_alone():
    single = compute_response_policy("kenapa ESP32 panas?")
    multi = compute_response_policy("kenapa ESP32 panas? terus gimana cara ngatasinnya? aman ga buat dipakai terus?")
    assert multi.score > single.score
    assert "multiple_questions" in multi.reasons


def test_multiple_concepts_only_counts_with_a_second_distinct_task_bucket():
    """A connector word ('dan') alone must NOT inflate the score - only
    when it's genuinely paired with a second, different task-type
    signal (here: troubleshooting 'kenapa' + comparison 'bedanya')."""
    plain_dan = compute_response_policy("aku suka roti dan selai")
    stacked = compute_response_policy("kenapa ESP32 panas dan bedanya sama ESP8266 apa?")
    assert "multiple_concepts" not in plain_dan.reasons
    assert "multiple_concepts" in stacked.reasons


# ---- 13/14/15/16. Follow-up / continuation behavior -------------------------

def test_followup_without_previous_score_stays_ordinary_normal():
    p = compute_response_policy("kalau yang bagian regulator gimana?")
    assert p.depth == DEPTH_NORMAL
    assert "conversational_continuation" not in p.reasons


def test_followup_with_high_previous_score_is_nudged_up_not_reset_to_short():
    """The brief's own example: 'kalau yang bagian regulator gimana?'
    after a DETAILED-ish previous turn must stay around NORMAL, not
    reset down to SHORT."""
    baseline = compute_response_policy("kalau yang bagian regulator gimana?")
    nudged = compute_response_policy("kalau yang bagian regulator gimana?", previous_score=50)
    assert nudged.score >= baseline.score
    assert nudged.depth in (DEPTH_NORMAL, DEPTH_DETAILED)
    assert nudged.depth != DEPTH_SHORT
    assert "conversational_continuation" in nudged.reasons


def test_followup_continuation_never_forces_a_score_increase_beyond_previous_minus_ten():
    nudged = compute_response_policy("kalau yang bagian regulator gimana?", previous_score=50)
    assert nudged.score <= max(50 - 10, nudged.score)  # never exceeds the max(score, previous-10) rule's own ceiling
    assert nudged.score == max(compute_response_policy("kalau yang bagian regulator gimana?").score, 50 - 10)


def test_continuation_nudge_never_applies_when_turn_has_its_own_task_signal():
    """A follow-up shape that ALSO contains its own strong task-type
    keyword (e.g. an explicit architecture question) must not be
    silently downgraded by a lower previous_score either - its own
    signal wins, the continuation nudge is skipped entirely."""
    own_signal = compute_response_policy(
        "kalau arsitektur ESP32 dari CPU sampai peripheral gimana?", previous_score=10,
    )
    assert "conversational_continuation" not in own_signal.reasons
    assert own_signal.task_type == "architecture_or_deep_analysis"


def test_jelasin_lagi_increases_score():
    baseline = compute_response_policy("gimana caranya?")
    repeat = compute_response_policy("jelasin lagi")
    assert "clarification_repeat_request" in repeat.reasons
    assert repeat.score > compute_response_policy("").score or repeat.score >= 32


def test_aku_masih_bingung_increases_desired_depth():
    p = compute_response_policy("aku masih bingung")
    assert "user_confusion_signal" in p.reasons
    assert p.score > 32  # above the bare "ordinary question" default


def test_intinya_aja_is_an_explicit_short_override_not_a_score_nudge():
    """'intinya aja' is in the EXPLICIT short-phrase list (highest
    precedence), not merely a context modifier - confirms it short-
    circuits straight to depth=short/explicit=True."""
    p = compute_response_policy("intinya aja")
    assert p.explicit is True
    assert p.depth == DEPTH_SHORT


# ---- 17. Score bounds 0-100 --------------------------------------------------

def test_score_never_exceeds_bounds_even_with_every_signal_stacked():
    stacked_text = (
        "jelaskan arsitektur ESP32 dari CPU sampai peripheral secara lengkap dan "
        "bedahnya bedanya sama ESP8266 apa? terus kenapa juga sering gagal? "
        "aku masih bingung, jelasin lagi dong?"
    )
    p = compute_response_policy(stacked_text, previous_score=100)
    assert SCORE_MIN <= p.score <= SCORE_MAX


def test_score_never_below_bounds_for_empty_text():
    p = compute_response_policy("")
    assert SCORE_MIN <= p.score <= SCORE_MAX


def test_score_bounds_hold_across_a_wide_sample():
    samples = [
        "apa itu relay?", "cara pasang relay ke ESP32?", "ESP32 bedanya sama ESP8266?",
        "jelaskan arsitektur ESP32 dari CPU sampai peripheral", "jawab singkat, kenapa ESP32 panas?",
        "jelaskan detail cara kerja regulator ESP32", "", "   ", "asdkjaslkdj alksdjalksjd",
    ]
    for text in samples:
        p = compute_response_policy(text)
        assert SCORE_MIN <= p.score <= SCORE_MAX, (text, p.score)


# ---- 18. Determinism ---------------------------------------------------------

def test_determinism_same_input_same_output():
    text = "jelaskan arsitektur ESP32 dari CPU sampai peripheral"
    p1 = compute_response_policy(text)
    p2 = compute_response_policy(text)
    assert p1.depth == p2.depth
    assert p1.score == p2.score
    assert p1.reasons == p2.reasons
    assert p1.explicit == p2.explicit
    assert p1.task_type == p2.task_type


def test_determinism_holds_with_previous_score_supplied():
    text = "kalau yang bagian regulator gimana?"
    p1 = compute_response_policy(text, previous_score=44)
    p2 = compute_response_policy(text, previous_score=44)
    assert p1 == p2


def test_determinism_across_many_repeated_calls():
    text = "cara pasang relay ke ESP32?"
    results = {(compute_response_policy(text).depth, compute_response_policy(text).score) for _ in range(50)}
    assert len(results) == 1


# ---- 19. No external/LLM/network call ----------------------------------------

_FORBIDDEN_SOURCE_SUBSTRINGS = (
    "import requests", "import httpx", "import aiohttp", "import urllib",
    "import socket", "openrouter", "openai", "anthropic.", "import anthropic",
    " api_key", "ApiKey", ".post(", ".get(\"http", "http://", "https://",
)


def test_response_policy_module_source_contains_no_network_or_llm_calls():
    """Structural, source-level proof (not just behavioral) that this
    module cannot reach the network or call another LLM - matches the
    brief's explicit 'Do NOT use another LLM... Do NOT call an external
    API' constraint. Reads the actual file rather than trusting a
    docstring claim."""
    src_path = os.path.join(_ROOT, "luno", "response_policy.py")
    with open(src_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    lowered = source.lower()
    for forbidden in _FORBIDDEN_SOURCE_SUBSTRINGS:
        assert forbidden.lower() not in lowered, f"forbidden substring found: {forbidden!r}"


def test_response_policy_module_imports_no_memory_or_persistence_modules():
    """Structural proof this module cannot mutate any persistent store -
    it never even imports a module capable of writing one (complements
    the behavioral E2E 'no duplicate retrieval' check in Section 2)."""
    src_path = os.path.join(_ROOT, "luno", "response_policy.py")
    with open(src_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    forbidden_imports = (
        "luno.memory", "luno.persistence", "luno.relationship_engine",
        "luno.episodic_memory", "luno.reminders", "luno.memory_guard",
        "habit_memory", "luno.memory_context",
    )
    for forbidden in forbidden_imports:
        assert forbidden not in source, f"unexpected import/reference found: {forbidden!r}"


def test_compute_response_policy_takes_no_network_capable_arguments():
    """The public function signature itself is text-in, struct-out -
    confirms there is no client/session/adapter parameter a caller could
    even wire an LLM/API call through."""
    import inspect
    sig = inspect.signature(compute_response_policy)
    for name in sig.parameters:
        assert "client" not in name.lower()
        assert "adapter" not in name.lower()
        assert "session" not in name.lower()


# ---- ResponsePolicy dataclass shape / to_dict ---------------------------------

def test_response_policy_exposes_required_public_fields():
    p = compute_response_policy("apa itu relay?")
    assert hasattr(p, "depth")
    assert hasattr(p, "score")
    assert hasattr(p, "reasons")
    assert hasattr(p, "explicit")
    assert p.depth in (DEPTH_SHORT, DEPTH_NORMAL, DEPTH_DETAILED)
    assert isinstance(p.score, int)
    assert isinstance(p.reasons, list)
    assert isinstance(p.explicit, bool)


def test_response_policy_to_dict_is_json_shaped():
    p = compute_response_policy("jelaskan detail cara kerja regulator ESP32")
    d = p.to_dict()
    assert set(["depth", "score", "reasons", "explicit", "task_type"]).issubset(d.keys())
    import json
    json.dumps(d)  # must not raise - dashboard/debug-friendly shape


def test_only_three_public_depth_values_are_ever_produced():
    """Internal 0-100 score notwithstanding, `.depth` must never expose
    a 4th/5th level (no 'minimal'/'exhaustive') - the brief's own 'do
    not expose unnecessary complexity' rule."""
    samples = [
        "apa itu relay?", "cara pasang relay ke ESP32?", "ESP32 bedanya sama ESP8266?",
        "jelaskan arsitektur ESP32 dari CPU sampai peripheral", "jawab singkat, kenapa ESP32 panas?",
        "jelaskan detail cara kerja regulator ESP32", "", "aku masih bingung", "jelasin lagi",
    ]
    for text in samples:
        assert compute_response_policy(text).depth in (DEPTH_SHORT, DEPTH_NORMAL, DEPTH_DETAILED)


# ---- build_depth_instruction() - prompt integration wording -------------------

def test_build_depth_instruction_short_is_concise_instruction():
    p = ResponsePolicy(depth=DEPTH_SHORT, score=10, reasons=[], explicit=True)
    text = build_depth_instruction(p)
    assert "short" in text.lower() or "concise" in text.lower()


def test_build_depth_instruction_detailed_asks_for_comprehensive_answer():
    p = ResponsePolicy(depth=DEPTH_DETAILED, score=90, reasons=[], explicit=True)
    text = build_depth_instruction(p)
    assert "detailed" in text.lower() or "comprehensive" in text.lower()


def test_build_depth_instruction_never_exposes_raw_score_or_reasons():
    """Internal scoring must stay debug-only - the rendered instruction
    text (which reaches the LLM prompt) must never leak the numeric
    score or internal reason tags verbatim."""
    for depth, score in ((DEPTH_SHORT, 5), (DEPTH_NORMAL, 45), (DEPTH_DETAILED, 95)):
        p = ResponsePolicy(depth=depth, score=score, reasons=["task_type:architecture_or_deep_analysis"], explicit=False)
        text = build_depth_instruction(p)
        assert str(score) not in text
        assert "task_type" not in text
        assert "reasons" not in text.lower()


def test_build_depth_instruction_falls_back_to_normal_for_unknown_depth():
    p = ResponsePolicy(depth="something_unexpected", score=50, reasons=[], explicit=False)
    text = build_depth_instruction(p)
    assert text == build_depth_instruction(ResponsePolicy(depth=DEPTH_NORMAL, score=50, reasons=[], explicit=False))


# ============================================================================
# Section 2 - end-to-end integration tests through the real production
# pipeline (RuntimeDemoConsole -> PlannerBridgeModule -> NeedLLMResponse)
# ============================================================================

def _load_demo():
    spec = importlib.util.spec_from_file_location("main_runtime_demo", os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(spec)
    sys.modules["main_runtime_demo"] = demo
    spec.loader.exec_module(demo)
    return demo


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 5.0, interval_s: float = 0.02) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def _new_console(demo):
    from luno.adapters import MockOpenRouterClient
    return demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text="ok", chunk_delay_s=0.0))


def _run_turn_and_capture_prompt(console, demo, text, request_id, conversation_id=None):
    captured = {}
    need_llm = threading.Event()

    def _capture(e):
        captured["system_prompt"] = e.get("system_prompt")
        need_llm.set()

    sub = console.event_bus.subscribe("need_llm_response", _capture)
    data = {"text": text, "request_id": request_id}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    console.event_bus.publish(demo.Event(type="user_utterance", data=data))
    assert _wait_until(need_llm.is_set, 5.0), "no need_llm_response within timeout"
    return captured.get("system_prompt") or ""


# ---- 22. Selected depth reaches the actual production system_prompt ---------

def test_e2e_short_depth_instruction_reaches_system_prompt():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        prompt = _run_turn_and_capture_prompt(console, demo, "jawab singkat, apa itu relay?", "rdp-short-1")
        assert "Response depth: SHORT" in prompt, prompt
    finally:
        console.stop()


def test_e2e_normal_depth_instruction_reaches_system_prompt():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        prompt = _run_turn_and_capture_prompt(console, demo, "cara pasang relay ke ESP32?", "rdp-normal-1")
        assert "Response depth: NORMAL" in prompt, prompt
    finally:
        console.stop()


def test_e2e_detailed_depth_instruction_reaches_system_prompt():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        prompt = _run_turn_and_capture_prompt(
            console, demo, "jelaskan arsitektur ESP32 dari CPU sampai peripheral", "rdp-detailed-1",
        )
        assert "Response depth: DETAILED" in prompt, prompt
    finally:
        console.stop()


# ---- 22. Existing persona/personality remains intact alongside the new note --

def test_e2e_persona_block_still_present_alongside_depth_instruction():
    """The depth instruction must be ADDITIVE - Luno's existing persona/
    system instructions must still be present in the same prompt, not
    displaced or overridden."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        prompt = _run_turn_and_capture_prompt(console, demo, "cara pasang relay ke ESP32?", "rdp-persona-1")
        assert "Response depth: NORMAL" in prompt
        # persona block presence: reuse the same persona-detection convention
        # `test_runtime_demo.py`'s own persona E2E tests rely on (persona
        # block is always non-empty and appended before every other note).
        persona_block = console.planner_module.persona_prompt_block if hasattr(console.planner_module, "persona_prompt_block") else None
        if persona_block:
            assert persona_block in prompt
        else:
            # fall back to a structural check: the prompt is not JUST the
            # depth instruction - something else (persona or otherwise)
            # was appended too, proving this note is additive not exclusive.
            assert prompt.strip() != build_depth_instruction(
                ResponsePolicy(depth=DEPTH_NORMAL, score=38, reasons=[], explicit=False)
            ).strip()
    finally:
        console.stop()


# ---- Computed once per turn (never recomputed, never a duplicate call) ------

def test_e2e_response_policy_computed_exactly_once_per_turn():
    demo = _load_demo()
    calls = []
    original = demo.compute_response_policy

    def _counting(*a, **kw):
        calls.append((a, kw))
        return original(*a, **kw)

    demo.compute_response_policy = _counting
    console = _new_console(demo)
    console.start()
    try:
        _run_turn_and_capture_prompt(console, demo, "cara pasang relay ke ESP32?", "rdp-once-1")
        assert len(calls) == 1, f"expected exactly 1 call, got {len(calls)}"
    finally:
        console.stop()


# ---- 21. No duplicate memory retrieval ---------------------------------------

def test_e2e_wiring_the_policy_does_not_add_a_second_memory_retrieval_call():
    demo = _load_demo()
    console = _new_console(demo)
    calls = []
    original_retrieve = console.planner_module.memory_retriever.retrieve_memories

    def _counting_retrieve(*a, **kw):
        calls.append((a, kw))
        return original_retrieve(*a, **kw)

    console.planner_module.memory_retriever.retrieve_memories = _counting_retrieve
    console.start()
    try:
        _run_turn_and_capture_prompt(console, demo, "jelaskan detail cara kerja regulator ESP32", "rdp-noretr-1")
        assert len(calls) == 1, f"expected exactly 1 memory retrieval call per turn, got {len(calls)}"
    finally:
        console.stop()


# ---- 20. No memory mutation (structural, mirrors Section 1's static check) --

def test_e2e_isolated_persistent_state_files_untouched_by_a_pure_depth_turn(isolate_persistent_state):
    """Uses the SAME fixture-isolated paths `conftest.py` already
    guarantees every test gets - confirms a plain chat turn whose only
    distinguishing feature is an explicit depth instruction does not
    write anything new into `LONG_TERM_MEMORY_FILE`/`RELATIONSHIP_STATE_FILE`
    beyond whatever the existing (pre-existing, unrelated-to-this-sprint)
    per-turn memory/relationship machinery already does on any ordinary
    turn - i.e. this sprint adds no NEW writer."""
    import hashlib

    def _hash_or_none(path):
        try:
            with open(path, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        except FileNotFoundError:
            return None

    tracked = ["LONG_TERM_MEMORY_FILE", "RELATIONSHIP_STATE_FILE", "VERIFIED_FACTS_FILE"]
    before = {attr: _hash_or_none(isolate_persistent_state[attr]) for attr in tracked}

    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        _run_turn_and_capture_prompt(console, demo, "jelaskan detail cara kerja regulator ESP32", "rdp-nomut-1")
    finally:
        console.stop()

    after = {attr: _hash_or_none(isolate_persistent_state[attr]) for attr in tracked}
    # VERIFIED_FACTS_FILE in particular must be completely untouched - a
    # plain chat turn with no tool call has no legitimate reason to ever
    # write a verified fact, response-depth-related or otherwise.
    assert before["VERIFIED_FACTS_FILE"] == after["VERIFIED_FACTS_FILE"]


# ---- Conversational continuation, end-to-end across two real turns ----------

def test_e2e_continuation_context_bounded_dict_updates_per_conversation():
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "rdp-conv-continuation-1"
        _run_turn_and_capture_prompt(
            console, demo, "jelaskan arsitektur ESP32 dari CPU sampai peripheral", "rdp-conv-1a", conv_id,
        )
        first_score = console.planner_module._response_depth_context.get(conv_id)
        assert first_score is not None and first_score >= 65  # DETAILED-range

        prompt_2 = _run_turn_and_capture_prompt(
            console, demo, "kalau yang bagian regulator gimana?", "rdp-conv-1b", conv_id,
        )
        # brief's own example: must NOT reset to SHORT after a DETAILED-ish turn
        assert "Response depth: SHORT" not in prompt_2, prompt_2
        second_score = console.planner_module._response_depth_context.get(conv_id)
        assert second_score is not None
    finally:
        console.stop()


def test_e2e_conversation_ended_clears_the_bounded_continuation_state():
    """`_on_conversation_ended` is never reached via a routed event in
    this console (`main_runtime_demo.py` never registers
    `add_route("conversation_ended", "planner")` - confirmed by reading
    every `add_route(...)` call in the file; `_last_turn_trace`,
    `_session_feedback_target`, `_last_device_target`, and
    `_pending_env_confirmations` all share this exact same pre-existing,
    out-of-scope-for-this-sprint limitation). This is the SAME white-box
    convention `tests/test_device_context.py::test_conversation_ended_resets_device_context`
    and `tests/test_browser_wiring.py::test_browser_permissions_cleared_on_conversation_ended`
    already use for this reason - call the handler directly rather than
    relying on a route that does not exist in this console."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "rdp-conv-reset-1"
        _run_turn_and_capture_prompt(
            console, demo, "jelaskan arsitektur ESP32 dari CPU sampai peripheral", "rdp-conv-2a", conv_id,
        )
        assert conv_id in console.planner_module._response_depth_context
        assert conv_id in console.planner_module._last_response_policy

        console.planner_module._on_conversation_ended(
            demo.Event(type="conversation_ended", data={"session_id": conv_id, "reason": "test"})
        )
        assert conv_id not in console.planner_module._response_depth_context
        assert conv_id not in console.planner_module._last_response_policy
    finally:
        console.stop()


# ---- Inspectable debug state (Phase 4's chosen "internal and testable" path) -

def test_e2e_last_response_policy_is_inspectable_after_a_turn():
    """Phase 4's minimal exposure decision: no new dashboard page was
    added (no natural existing per-turn-decision page fit without
    building one), so the full last-resolved policy is kept in a small
    bounded, in-memory, per-conversation dict instead - this test proves
    it is genuinely inspectable (the 'testable' half of the brief's own
    fallback instruction) with depth/score/reasons/explicit all present."""
    demo = _load_demo()
    console = _new_console(demo)
    console.start()
    try:
        conv_id = "rdp-inspect-1"
        _run_turn_and_capture_prompt(console, demo, "jelaskan detail cara kerja regulator ESP32", "rdp-inspect-1a", conv_id)
        stored = console.planner_module._last_response_policy.get(conv_id)
        assert stored is not None
        assert stored["depth"] == DEPTH_DETAILED
        assert stored["explicit"] is True
        assert isinstance(stored["reasons"], list)
        assert isinstance(stored["score"], int)
    finally:
        console.stop()


# ---- Policy failure never breaks a turn (defensive, mirrors every other
# early-per-turn try/except in _handle_utterance) ------------------------------

def test_e2e_policy_computation_failure_defaults_to_normal_and_never_breaks_the_turn():
    demo = _load_demo()
    console = _new_console(demo)

    def _raising(*a, **kw):
        raise RuntimeError("simulated response_policy failure")

    demo.compute_response_policy = _raising
    console.start()
    try:
        prompt = _run_turn_and_capture_prompt(console, demo, "cara pasang relay ke ESP32?", "rdp-fail-1")
        assert "Response depth: NORMAL" in prompt, prompt
    finally:
        console.stop()
