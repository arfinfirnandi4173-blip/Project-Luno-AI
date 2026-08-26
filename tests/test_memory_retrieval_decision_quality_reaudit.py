"""
test_memory_retrieval_decision_quality_reaudit.py
====================================================

MEMORY RETRIEVAL & DECISION QUALITY (re-audit) sprint.

This is an independent, evidence-first re-verification of the existing,
already-shipped memory/topic pipeline (Sprint 4's `_active_topic`/
`_topic_history`, the earlier "Memory Retrieval & Decision Quality"
sprint's intent/continuity bonus, the Memory & Voice Observability
Dashboard's funnel counters) - NOT a rewrite of any of it. Phase 0-2's own
live reproduction through the REAL `RuntimeDemoConsole` production path
(see `docs/change_impact/memory_retrieval_decision_quality_reaudit.md` for
the full trace) found the correct memory was never even reaching
`assemble_context()`'s candidate pool for several ordinary follow-up
shapes - a Failure Class B problem (never retrieved), not a ranking or
budget problem. `luno.memory_context._rank_key()`'s own 8-tuple
(`relevance` first, `intent_bonus` a low-priority tie-breaker only) was
re-inspected and found to already be correctly relevance-first; it was
left completely untouched by this sprint.

Two, and only two, narrow root causes were found and fixed, both by
EXTENDING an existing mechanism, never replacing one:

  1. `luno.memory_retrieval.query._WORD_RE` (the ONE tokenizer this whole
     pipeline shares - retrieval scoring, topic-term extraction, topic-
     candidate content matching) used to be `[a-zA-Z']+` - digits were
     silently dropped, so "ESP32", "ESP8266", and "INMP441" all collapsed
     onto colliding/truncated tokens ("esp", "esp", "inmp"). Fixed to
     `[a-zA-Z][a-zA-Z0-9']*` - a leading letter followed by letters/digits/
     apostrophes stays ONE whole token, while a token that is ALL DIGITS
     still never matches on its own (preserves the existing "no signal for
     pure math" contract - `test_3_empty_retrieval_for_no_signal_query` in
     `tests/test_memory_retrieval.py` is unaffected, reconfirmed below).

  2. `luno.memory.classify_reference_type()` had no pattern at all for a
     bare pronoun used as the grammatical SUBJECT/OBJECT of a short
     question ("which one was it again?", "how does that connect?", "is
     it still on?") - only fixed-idiom framings ("about that", "kalau
     itu", "yang tadi") were recognized. These phrasings classified as
     "unknown" -> `is_short_followup` was `False` -> NEITHER the content-
     match path (`select_topic_candidates()`, correctly `[]`, no token
     overlap) NOR the single-slot `_active_topic` fallback (gated on
     `is_short_followup`) ever fired -> zero memory candidates, by
     construction, for either phrasing. Fixed by adding
     `_BARE_PRONOUN_REFERENCE_RE`, consulted at the SAME "direct_reference"
     result, lowest precedence tier (right before the final "unknown"
     fallthrough) - every existing precedence ordering is unchanged.

This file does NOT re-test relevance matching, importance/lifecycle,
conflict classification, deduplication, budget enforcement, the intent/
continuity bonus, or ranking itself - all unchanged by this sprint and
already covered by `tests/test_memory_context.py`, `tests/
test_memory_retrieval.py`, `tests/test_memory_decision_quality.py`, `tests/
test_memory_continuity.py`, `tests/test_memory_topic_retention.py`
(two of that file's own assertions were updated, not rewritten, to the
corrected tokenization - see its own inline comments).

`tests/conftest.py`'s autouse `isolate_persistent_state` fixture redirects
every writer-capable persistent-state file to an isolated temp path for
every test in this file - no test here can ever touch Vinn's real
production data (Phase 12's own hard requirement, doubly true after this
sprint's own Phase 0-2 raw-script reproduction was found to have briefly
written to the real `config/relationship_state.json` and `config/
episodic_memory.json` before isolation was added to that script - both
restored byte-identical; see the change-impact doc's own "Persistent
State" section for the full incident record).
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import threading
import time
from typing import Callable

import luno.memory as memory
import luno.memory_context as memory_context
from luno.memory_retrieval.query import analyze_query

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS_DIR)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ============================================================================
# SECTION 1 - tokenizer fix (`analyze_query` / `_WORD_RE`)
# ============================================================================

def test_A_esp32_tokenizes_whole_not_truncated_to_esp():
    tokens = analyze_query("My mic is an INMP441 connected to an ESP32.").tokens
    assert "esp32" in tokens
    assert "inmp441" in tokens
    # The bug this sprint fixed: neither digit-stripped fragment should
    # appear as its OWN separate token.
    assert "esp" not in tokens
    assert "inmp" not in tokens


def test_B_esp32_and_esp8266_no_longer_collide_on_one_token():
    esp32_tokens = set(analyze_query("I use ESP32 for the voice system.").tokens)
    esp8266_tokens = set(analyze_query("Bluetooth isn't available on the plain ESP8266.").tokens)
    assert "esp32" in esp32_tokens
    assert "esp8266" in esp8266_tokens
    # This is the exact collision Phase 0-2's own live reproduction found:
    # both used to reduce to the shared token "esp". They must not share
    # ANY token now (aside from ordinary stopword-filtered function words,
    # which "esp32"/"esp8266" are not).
    assert esp32_tokens.isdisjoint(esp8266_tokens)


def test_C_pure_digit_token_still_produces_no_signal_on_its_own():
    # Regression guard for the module's own documented contract
    # (`query.py`'s own docstring, `tests/test_memory_retrieval.py::
    # test_3_empty_retrieval_for_no_signal_query`) - a leading letter is
    # still REQUIRED, so "5" alone never becomes its own token.
    q = analyze_query("What's 5 + 5?")
    assert q.has_any_signal is False
    assert q.tokens == []


def test_D_alphanumeric_identifier_with_leading_digit_still_unmatched():
    # Documented known limitation (see change-impact doc): a token that
    # STARTS with a digit ("3D", "24/7") still does not tokenize as its
    # own signal token - same, pre-existing behavior, not a regression.
    q = analyze_query("I want a 3D printer running 24/7.")
    assert "3d" not in q.tokens
    assert "24" not in q.tokens
    assert "printer" in q.tokens
    assert "running" in q.tokens


def test_E_wled_and_mqtt_style_identifiers_unaffected():
    # Sanity: identifiers that were ALREADY letters-only (no digits) are
    # completely unaffected by this fix.
    q = analyze_query("I set up WLED with MQTT.")
    assert "wled" in q.tokens
    assert "mqtt" in q.tokens


# ============================================================================
# SECTION 2 - classify_reference_type() bare-pronoun fix
# ============================================================================

def test_F_which_one_was_it_again_is_direct_reference():
    assert memory.classify_reference_type("Which one was it again?") == "direct_reference"


def test_G_how_does_that_connect_is_direct_reference():
    assert memory.classify_reference_type("How does that connect?") == "direct_reference"


def test_H_is_it_still_on_is_direct_reference():
    assert memory.classify_reference_type("Is it still on?") == "direct_reference"


def test_I_what_was_it_called_is_direct_reference():
    assert memory.classify_reference_type("What was it called?") == "direct_reference"


def test_J_did_it_work_is_direct_reference():
    assert memory.classify_reference_type("Did it work?") == "direct_reference"


def test_K_how_does_it_connect_is_direct_reference():
    assert memory.classify_reference_type("How does it connect?") == "direct_reference"


def test_L_needs_topic_context_true_for_all_new_bare_pronoun_phrasings():
    for phrase in (
        "Which one was it again?", "How does that connect?", "Is it still on?",
        "What was it called?", "Did it work?",
    ):
        assert memory.needs_topic_context(phrase) is True, phrase


def test_M_false_positive_guard_fresh_technical_question_with_named_device():
    # "How does ESP32 handle low power mode?" contains no "does it/that/
    # this" BIGRAM (it's "does ESP32", not "does it") - the pattern's own
    # phrase-specificity is the false-positive guard, not a residual-word
    # check (see the pattern's own docstring in luno/memory.py for why).
    assert memory.classify_reference_type("How does ESP32 handle low power mode?") == "unknown"


def test_N_false_positive_guard_who_created_it_unaffected():
    # "who created it" contains no "was it"/"is it"/"does it"/"did it"/
    # "how does .../which one" bigram - must stay "unknown" exactly as
    # before this sprint (this literal phrase appears in
    # tests/test_wake_session_console.py as a generic simulated utterance,
    # unrelated to reference-type classification - reconfirmed here that
    # this sprint's addition does not flip it).
    assert memory.classify_reference_type("who created it") == "unknown"


def test_O_precedence_unchanged_alternative_request_still_wins_over_bare_pronoun():
    # "yang lain" (alternative_request) has HIGHER precedence than the new
    # bare-pronoun pattern - a sentence matching both must still resolve to
    # the higher-precedence type, proving the new pattern was inserted at
    # the correct (lowest, pre-"unknown") tier.
    assert memory.classify_reference_type("Is it the other option, yang lain?") == "alternative_request"


def test_P_precedence_unchanged_direct_reference_fixed_idiom_still_wins():
    # "about that" (the ORIGINAL `_DIRECT_REFERENCE_RE`) is checked before
    # the new bare-pronoun pattern; both produce "direct_reference" anyway,
    # but this proves the original pattern still fires first, unmodified.
    assert memory.classify_reference_type("What about that?") == "direct_reference"


def test_Q_existing_worked_examples_from_sprint_4_all_unaffected():
    # A representative sample from tests/test_memory_continuity.py's own
    # A-BD suite, reconfirmed unaffected by this sprint's addition.
    assert memory.classify_reference_type("yang lain?") == "alternative_request"
    assert memory.classify_reference_type("terus?") == "continuation"
    assert memory.classify_reference_type("kalau itu gimana?") == "direct_reference"
    assert memory.classify_reference_type("ESP32 gimana?") == "comparison"
    assert memory.classify_reference_type("tanpa MQTT?") == "negation_of_current_option"
    assert memory.classify_reference_type("yang lebih murah?") == "cost_comparison"


# ============================================================================
# SECTION 3 - production-path E2E (real RuntimeDemoConsole, isolated state)
# ============================================================================

def _load_demo(name):
    demo_spec = importlib.util.spec_from_file_location(name, os.path.join(_ROOT, "main_runtime_demo.py"))
    demo = importlib.util.module_from_spec(demo_spec)
    sys.modules[name] = demo
    demo_spec.loader.exec_module(demo)
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
    # canned_text=None -> echoes the user's own text back, so the reply
    # itself always carries the same entities the user just said - keeps
    # topic-term extraction (`extract_topic_terms_from_turn`, which merges
    # user text + reply text) deterministic across this file's turns
    # without needing per-turn scripted replies.
    return demo.RuntimeDemoConsole(openrouter_client=MockOpenRouterClient(canned_text=None, chunk_delay_s=0.0))


def _memory_block(prompt):
    """Scopes an assertion to ONLY the rendered `[BEGIN STORED MEMORY
    CONTEXT]...[END STORED MEMORY CONTEXT]` block, never the whole system
    prompt. Required because Luno's own STATIC persona text separately
    lists "ESP32/Arduino" under its own always-present "Knowledgeable
    about:" skills line - a plain substring/word-boundary check against
    the FULL prompt would false-positive on that unrelated, always-present
    text regardless of what memory was actually retrieved this turn (the
    exact same pitfall `tests/test_memory_continuity.py`'s own `_word_in`
    docstring already documents for "knowledgeable" containing "wled")."""
    i = prompt.find("BEGIN STORED MEMORY CONTEXT")
    j = prompt.find("END STORED MEMORY CONTEXT")
    if i == -1 or j == -1:
        return ""
    return prompt[i:j]


def _run_turn(console, demo, text, request_id, conversation_id=None):
    captured = {}
    need_llm = threading.Event()

    def _capture(e):
        if e.get("request_id") != request_id:
            return
        captured["system_prompt"] = e.get("system_prompt")
        need_llm.set()

    sub = console.event_bus.subscribe("need_llm_response", _capture)
    data = {"text": text, "request_id": request_id}
    if conversation_id is not None:
        data["conversation_id"] = conversation_id
    try:
        console.event_bus.publish(demo.Event(type="user_utterance", data=data))
        assert _wait_until(need_llm.is_set, 5.0), "no need_llm_response within timeout"
        assert _wait_until(lambda: request_id not in console.planner_module._pending_turns, 5.0), (
            "assistant_response / topic-history update never completed for this request_id"
        )
    finally:
        console.event_bus.unsubscribe(sub)
    return captured.get("system_prompt") or ""


def test_E2E_A_eight_turn_scenario_turn6_which_one_was_it_again_surfaces_mic_topic():
    """The brief's own 8-turn reproduction, turns 1-6. Before this sprint's
    fix, turn 6 ("Which one was it again?") reached `assemble_context()`
    with ZERO candidates (Failure Class B) - the rendered prompt had no
    memory block at all. After the fix, `reference_type` is
    "direct_reference" (`is_short_followup=True`), the single-slot
    `_active_topic` fallback fires, and the ESP32/mic topic reaches the
    real system_prompt."""
    demo = _load_demo("main_runtime_demo_reaudit_e2e_a")
    console = _new_console(demo)
    console.start()
    try:
        turns = [
            "My mic is an INMP441 connected to an ESP32.",
            "I use ESP32 for the voice system.",
            "Bluetooth isn't available on the plain ESP8266.",
            "I also have an aquascape.",
            "Anyway, what about the mic?",
            "Which one was it again?",
        ]
        prompt = ""
        for i, text in enumerate(turns, start=1):
            prompt = _run_turn(console, demo, text, f"e2e-a-{i}", "conv-a")
        block = _memory_block(prompt)
        assert block, "turn 6 must carry a memory block - got none"
        assert re.search(r'\bmic\b', block, re.IGNORECASE), (
            "turn 6's rendered memory block must mention the mic topic"
        )
    finally:
        console.stop()


def test_E2E_B_eight_turn_scenario_turn7_esp32_question_does_not_leak_esp8266():
    """Turn 7 ("What did I use for the ESP32?") - before this sprint's
    fix, "esp32" and "esp8266" both tokenized to "esp", and (once the
    original ESP32/mic entry aged out of the bounded 4-entry topic
    history) the turn incorrectly retrieved the ESP8266/Bluetooth entry
    instead. After the fix, the two never share a token, so the correct
    ESP32-mentioning entry is retrieved and the ESP8266/Bluetooth text
    does not appear."""
    demo = _load_demo("main_runtime_demo_reaudit_e2e_b")
    console = _new_console(demo)
    console.start()
    try:
        turns = [
            "My mic is an INMP441 connected to an ESP32.",
            "I use ESP32 for the voice system.",
            "Bluetooth isn't available on the plain ESP8266.",
            "I also have an aquascape.",
            "Anyway, what about the mic?",
            "Which one was it again?",
            "What did I use for the ESP32?",
        ]
        prompt = ""
        for i, text in enumerate(turns, start=1):
            prompt = _run_turn(console, demo, text, f"e2e-b-{i}", "conv-b")
        block = _memory_block(prompt)
        assert block
        assert re.search(r'\besp32\b', block, re.IGNORECASE)
        assert not re.search(r'\bbluetooth\b', block, re.IGNORECASE), (
            "turn 7 must not surface the unrelated ESP8266/Bluetooth topic"
        )
        assert not re.search(r'\besp8266\b', block, re.IGNORECASE)
    finally:
        console.stop()


def test_E2E_C_eight_turn_scenario_turn8_how_does_that_connect_surfaces_a_topic():
    """Turn 8 ("How does that connect?") - before this sprint's fix,
    `reference_type` was "unknown" and zero memory reached the prompt.
    After the fix, SOME topic (whatever the active/most recent one is)
    reaches the prompt via the single-slot fallback - not a no-op turn
    anymore."""
    demo = _load_demo("main_runtime_demo_reaudit_e2e_c")
    console = _new_console(demo)
    console.start()
    try:
        turns = [
            "My mic is an INMP441 connected to an ESP32.",
            "I use ESP32 for the voice system.",
            "Bluetooth isn't available on the plain ESP8266.",
            "I also have an aquascape.",
            "Anyway, what about the mic?",
            "Which one was it again?",
            "What did I use for the ESP32?",
            "How does that connect?",
        ]
        prompt = ""
        for i, text in enumerate(turns, start=1):
            prompt = _run_turn(console, demo, text, f"e2e-c-{i}", "conv-c")
        assert _memory_block(prompt), "turn 8 must carry a memory block - got none"
    finally:
        console.stop()


def test_E2E_D_topic_switch_a_b_c_a_each_follow_up_resolves_to_its_own_topic():
    """Topic A (ESP32/mic), Topic B (aquascape/pump), Topic C (WLED/LED
    strip), interleaved A1/B1/C1/A2/B2/C2/A3 - each follow-up must resolve
    to ITS OWN topic's content, not a neighboring one, proving no cross-
    topic contamination when the distinguishing words are NOT numeric
    (the tokenizer fix's own scope) - this was already correct before this
    sprint (content-based `select_topic_candidates()` matching), reconfirmed
    unaffected here."""
    demo = _load_demo("main_runtime_demo_reaudit_e2e_d")
    console = _new_console(demo)
    console.start()
    try:
        turns = [
            "My mic is an INMP441 connected to an ESP32.",                # A1
            "I also have an aquascape with a pump running 24/7.",         # B1
            "I set up WLED on an LED strip in my room.",                  # C1
            "Which mic did I use again?",                                  # A2
            "How often does the pump run?",                                # B2
            "What did I use for the LED strip?",                          # C2
            "Anyway, what about the mic again?",                          # A3
        ]
        blocks = []
        for i, text in enumerate(turns, start=1):
            blocks.append(_memory_block(_run_turn(console, demo, text, f"e2e-d-{i}", "conv-d")))

        # A2 (turn 4) must mention the mic topic, not pump/wled.
        assert re.search(r'\bmic\b', blocks[3], re.IGNORECASE)
        assert not re.search(r'\bpump\b', blocks[3], re.IGNORECASE)
        # B2 (turn 5) must mention the pump topic.
        assert re.search(r'\bpump\b', blocks[4], re.IGNORECASE)
        # C2 (turn 6) must mention the LED/WLED topic.
        assert re.search(r'\b(led|wled)\b', blocks[5], re.IGNORECASE)
        # A3 (turn 7, A after B and C - the A->B->C->A case) must mention
        # mic again, not the intervening B/C topics.
        assert re.search(r'\bmic\b', blocks[6], re.IGNORECASE)
        assert not re.search(r'\bpump\b', blocks[6], re.IGNORECASE)
    finally:
        console.stop()


def test_E2E_E_unrelated_new_question_does_not_recover_old_topic():
    """Adversarial (Phase 10, item 2): a genuinely fresh, unrelated
    question must NOT accidentally pull in an old topic just because one
    exists - `select_topic_candidates()`'s own content-based matching (not
    "always keep recent memory") is what guarantees this; reconfirmed
    unaffected by this sprint's two fixes."""
    demo = _load_demo("main_runtime_demo_reaudit_e2e_e")
    console = _new_console(demo)
    console.start()
    try:
        turns = [
            "My mic is an INMP441 connected to an ESP32.",
            "What's the weather like on Mars?",
        ]
        prompt = ""
        for i, text in enumerate(turns, start=1):
            prompt = _run_turn(console, demo, text, f"e2e-e-{i}", "conv-e")
        # Scoped to the memory block only - the STATIC persona text
        # separately lists "ESP32/Arduino" under its own always-present
        # "Knowledgeable about:" skills line, which would otherwise
        # false-positive this assertion regardless of what memory was
        # actually retrieved (see `_memory_block()`'s own docstring).
        block = _memory_block(prompt)
        assert not re.search(r'\b(esp32|inmp441|mic)\b', block, re.IGNORECASE), (
            "an unrelated fresh question must not recover the old ESP32/mic topic"
        )
    finally:
        console.stop()
