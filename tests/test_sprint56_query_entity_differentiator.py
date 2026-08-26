"""
tests/test_sprint56_query_entity_differentiator.py
===================================================

Sprint 56 (Home Assistant + Query Intelligence), Phase 12 - "Query-side
entity differentiator". Covers `luno.memory_context._narrow_by_query_
differentiator()` (new) and its wiring into the pre-existing, otherwise
UNMODIFIED `select_topic_candidates()`.

Background (see that function's own new docstring for the full writeup):
Sprint 49 (Entity Provenance Disambiguation & Topic Lineage) built
`_extract_entity_differentiator()` - a general-purpose, domain-agnostic
extractor for a standalone uppercase letter ("A"/"B"/"X") in a turn's raw
`source_sentence` - and used it to fix a DIFFERENT code path: the
single-slot `is_active_topic_relevant_to_query()` lineage check (a
same-vs-different-entity boolean over the ACTIVE topic snapshot only).
It did not touch `select_topic_candidates()` (the bounded-history,
token-overlap-ranked candidate list, a separate call site with a
separate caller in `main_runtime_demo.py`), which had its own live,
reproduced gap: two topic-history entries that both mention the same
generic word ("pompa") tie on overlap score and are BOTH returned
(and therefore both injected into context by `topic_history_to_relevant_
memories()`), even when the CURRENT QUERY itself explicitly names one of
them via the exact same "A"/"B" convention ("Pompa A gimana?").

This suite proves: (1) the query-side differentiator narrows a tied
result to exactly one entry when the query unambiguously names it: (2)
every other shape - bare query, no matching entry, an ambiguous query
differentiator, a single already-unambiguous candidate - is completely
unaffected, preserving the pre-existing, already-tested "insufficient
evidence -> do not narrow/guess" behavior; (3) the mechanism is
GENERAL-PURPOSE, not hardcoded to any specific device/domain vocabulary
- proven by running the identical scenario shape across two unrelated,
synthetic domains that share no vocabulary with each other or with any
of this project's real configured Home Assistant devices.
"""

from __future__ import annotations

import inspect

from luno import memory_context


def _snap(sentence: str) -> "memory_context.ActiveTopicSnapshot":
    tokens = frozenset(memory_context.analyze_query(sentence).tokens)
    return memory_context.ActiveTopicSnapshot(terms=tokens, source_sentence=sentence)


# ─────────────────────────────────────────────
# Section 1 - unit tests for _narrow_by_query_differentiator() directly
# ─────────────────────────────────────────────

def test_01_fewer_than_two_candidates_is_a_no_op():
    one = [_snap("Server A pakai Ubuntu.")]
    assert memory_context._narrow_by_query_differentiator(one, "Server A gimana?") == one
    assert memory_context._narrow_by_query_differentiator([], "Server A gimana?") == []


def test_02_bare_query_with_no_differentiator_is_unchanged():
    pair = [_snap("Server A pakai Ubuntu."), _snap("Server B pakai Debian.")]
    result = memory_context._narrow_by_query_differentiator(pair, "Server-nya gimana?")
    assert result == pair


def test_03_query_differentiator_narrows_to_the_matching_entry():
    pair = [_snap("Server A pakai Ubuntu."), _snap("Server B pakai Debian.")]
    result = memory_context._narrow_by_query_differentiator(pair, "Server A gimana?")
    assert len(result) == 1
    assert result[0].source_sentence == "Server A pakai Ubuntu."


def test_04_query_differentiator_narrows_to_the_OTHER_matching_entry():
    pair = [_snap("Server A pakai Ubuntu."), _snap("Server B pakai Debian.")]
    result = memory_context._narrow_by_query_differentiator(pair, "Server B gimana?")
    assert len(result) == 1
    assert result[0].source_sentence == "Server B pakai Debian."


def test_05_query_differentiator_matching_nothing_falls_back_unchanged():
    pair = [_snap("Server A pakai Ubuntu."), _snap("Server B pakai Debian.")]
    result = memory_context._narrow_by_query_differentiator(pair, "Server C gimana?")
    assert result == pair


def test_06_query_differentiator_matching_BOTH_candidates_falls_back_unchanged():
    # Two entries that happen to share the SAME differentiator (a
    # correction/re-statement about "A" twice) - the query's own "A"
    # does not narrow between them; still ambiguous, do not guess.
    pair = [_snap("Server A pakai Ubuntu."), _snap("Server A pakai versi baru.")]
    result = memory_context._narrow_by_query_differentiator(pair, "Server A gimana?")
    assert result == pair


def test_07_query_with_two_or_more_letters_is_itself_ambiguous_no_narrowing():
    # `_extract_entity_differentiator()`'s own "exactly one candidate"
    # discipline applies symmetrically to the QUERY text too - "Server A
    # atau B gimana?" carries two standalone letters, so it extracts
    # None, and this function must not guess which one was meant.
    pair = [_snap("Server A pakai Ubuntu."), _snap("Server B pakai Debian.")]
    result = memory_context._narrow_by_query_differentiator(pair, "Server A atau B gimana?")
    assert result == pair


def test_08_lowercase_letter_in_query_never_triggers_narrowing():
    # Mirrors _extract_entity_differentiator()'s own documented
    # lowercase-never-matches rule (the shared tokenizer already strips
    # standalone lowercase "a" as an English-article stopword).
    pair = [_snap("Server A pakai Ubuntu."), _snap("Server B pakai Debian.")]
    result = memory_context._narrow_by_query_differentiator(pair, "server a gimana?")
    assert result == pair


# ─────────────────────────────────────────────
# Section 2 - end-to-end through select_topic_candidates(), the real
# (only) caller, proving the wiring (not just the helper in isolation)
# ─────────────────────────────────────────────

def test_09_e2e_select_topic_candidates_bare_query_returns_both_unchanged():
    history = [_snap("Aquascape A pakai pompa kecil."), _snap("Aquascape B pakai pompa besar.")]
    result = memory_context.select_topic_candidates(history, "Pompa gimana?", False)
    assert {e.source_sentence for e in result} == {
        "Aquascape A pakai pompa kecil.", "Aquascape B pakai pompa besar.",
    }


def test_10_e2e_select_topic_candidates_query_with_differentiator_resolves_one():
    history = [_snap("Aquascape A pakai pompa kecil."), _snap("Aquascape B pakai pompa besar.")]
    result = memory_context.select_topic_candidates(history, "Pompa A gimana?", False)
    assert len(result) == 1
    assert result[0].source_sentence == "Aquascape A pakai pompa kecil."


def test_11_e2e_select_topic_candidates_query_with_other_differentiator_resolves_other():
    history = [_snap("Aquascape A pakai pompa kecil."), _snap("Aquascape B pakai pompa besar.")]
    result = memory_context.select_topic_candidates(history, "Pompa B gimana?", False)
    assert len(result) == 1
    assert result[0].source_sentence == "Aquascape B pakai pompa besar."


def test_12_e2e_generalizes_to_a_second_unrelated_synthetic_domain():
    # A completely different vocabulary (no overlap with the Aquascape
    # scenario above, and none of this checkout's real Home Assistant
    # device names either) - proves the mechanism is a structural
    # signal, not a lookup keyed on any specific domain word.
    history = [_snap("Motor A dipakai buat balapan."), _snap("Motor B dipakai buat harian.")]
    bare = memory_context.select_topic_candidates(history, "Motor gimana?", False)
    assert len(bare) == 2
    narrowed = memory_context.select_topic_candidates(history, "Motor A gimana?", False)
    assert len(narrowed) == 1 and narrowed[0].source_sentence == "Motor A dipakai buat balapan."


def test_13_e2e_single_candidate_case_is_unaffected_by_query_differentiator():
    # Only ONE history entry overlaps at all - select_topic_candidates()
    # never even reaches the tie-narrowing path (len(candidates) < 2),
    # so an unrelated letter in the query changes nothing.
    history = [_snap("Server A pakai Ubuntu."), _snap("Router lampu depan mati.")]
    result = memory_context.select_topic_candidates(history, "Server X gimana?", False)
    assert len(result) == 1
    assert result[0].source_sentence == "Server A pakai Ubuntu."


def test_14_e2e_no_overlap_at_all_still_returns_empty():
    history = [_snap("Motor A dipakai buat balapan."), _snap("Motor B dipakai buat harian.")]
    result = memory_context.select_topic_candidates(history, "Liburan ke pantai enaknya kapan?", False)
    assert result == []


def test_15_e2e_empty_history_still_returns_empty():
    result = memory_context.select_topic_candidates([], "Motor A gimana?", False)
    assert result == []
    result2 = memory_context.select_topic_candidates(None, "Motor A gimana?", False)
    assert result2 == []


# ─────────────────────────────────────────────
# Section 3 - no hardcoding / forbidden-literal inspection, matching
# this project's own established convention (see Sprint 52's equivalent
# check on real_home_assistant.py) for proving a fix generalizes rather
# than special-casing specific product/device names.
# ─────────────────────────────────────────────

def test_16_narrow_by_query_differentiator_has_no_domain_hardcoding():
    source = inspect.getsource(memory_context._narrow_by_query_differentiator)
    forbidden = ["aquascape", "esp32", "pompa", "mic", "board", "lampu", "wled"]
    lowered = source.lower()
    for literal in forbidden:
        assert literal not in lowered, (
            f"_narrow_by_query_differentiator() must stay domain-agnostic - "
            f"found forbidden literal {literal!r} in its own source"
        )


def test_17_reuses_the_existing_extractor_not_a_second_regex():
    # This function must call the SAME `_extract_entity_differentiator()`
    # Sprint 49 built - not define or use a second, parallel pattern.
    source = inspect.getsource(memory_context._narrow_by_query_differentiator)
    assert "_extract_entity_differentiator(" in source
    assert "_ENTITY_DIFFERENTIATOR_RE" not in source, (
        "must not re-implement the regex directly - reuse the existing "
        "function, exactly like every other caller of it in this module"
    )
