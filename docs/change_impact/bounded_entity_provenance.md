# Change Impact: Bounded Entity Provenance & Ambiguity Resolution (Sprint 48)

## Goal

Revisit Sprint 47's own central unfixed finding (`docs/project_
handover.md` §16 item 8): a curated-vocabulary single token ("board",
"mic", etc.) with ZERO grounding in EITHER of exactly 2 live topics
wrongly, confidently resolves to whichever topic is merely most recent,
instead of refusing. Sprint 47 tried and reverted two threshold-only
widenings of `is_active_topic_relevant_to_query()`'s `distinct_other_
count` guard - each fixed the "board" case but broke the "mic" case
(Sprint 46's own `test_27_e2e_no_contamination_reverse_direction`), an
IDENTICAL formal shape with the OPPOSITE correct answer. This sprint
was explicitly instructed NOT to repeat that same threshold-only
approach and to find a genuinely different, bounded mechanism.

## Phase 0 - reconnaissance

Read `docs/project_handover.md`, `docs/project_handover.json`,
`ARCHITECTURE_GUARD.md`, `docs/testing/regression_baseline.md`, and the
latest `docs/change_impact/*.md` files (`semantic_entity_identity.md`,
`entity_identity_semantic_alias_continuity.md`). Traced the full
architecture map (`classify_reference_type()` → `is_pure_reference_
followup()`/`is_merge_reference_followup()`/`is_sparse_unknown_
followup()`/`is_demonstrative_anchored_followup()` → `is_active_topic_
relevant_to_query()` → `select_topic_candidates()`/`select_temporal_
fallback_candidate()` → `update_active_topic()`/`update_topic_history()`
→ `assemble_context()`). Verified Sprint 45-47's own claimed work was
ACTUALLY present in the checkout (inspected the live source, not just
the docs) - no discrepancy found, proceeded without needing to "STOP
and document a discrepancy."

Confirmed entity identity is represented ONLY as a flat, unstructured
bag-of-terms (`ActiveTopicSnapshot.terms`, unchanged since Sprint 40) -
no separate entity/provenance data structure exists anywhere in the
pipeline.

## Phase 1-2 - live reproduction (all 8 scenarios, real `RuntimeDemoConsole`)

Used deliberately generic canned replies (Sprint 47's own leak-free-
reply discipline) so only the system's own actual candidate-injection
behavior (the rendered system prompt's `- Active conversation topic`/
etc. lines) was trusted as signal.

- **Scenario A** ("Pompanya gimana?" after "Aquascape A pakai pompa
  kecil." then "Aquascape B pakai pompa besar."): reproduces Sprint
  47's known limitation #9 - confidently resolves to Aquascape B. A
  DIFFERENT code path than Scenarios B/B-mirror below (the `active_
  score > 0` branch's `coverage > 0.5` lineage-skip check, not the
  `active_score == 0` branch) - see "Investigated and REJECTED" below
  for why this was not also fixed this sprint.
- **Scenario B** ("Board itu gimana?" after "ESP32 pakai INMP441."
  (older) then "Aquascape pakai pump kecil." (active)): a REAL,
  reproduced ambiguity-safety bug - confidently, wrongly resolves to
  Aquascape. Sprint 47's own Scenario 5. **FIXED this sprint.**
- **Scenario B-mirror** ("Mic-nya gimana?" after "Aquarium saya
  50x25." then "ESP32 pakai INMP441." - Sprint 46's own `test_27`):
  the textbook IDENTICAL formal shape to Scenario B (curated single
  token, zero grounding in the active OR the sole other topic, exactly
  1 other topic) - but the CORRECT answer is to trust recency. Must
  remain unchanged - **verified unchanged after the fix.**
- **Scenario C** ("INMP441-nya gimana?" - a term unique to the OLDER
  of the 2 topics): already resolves correctly regardless of recency
  position, via the pre-existing raw-token-overlap short-circuit
  (`if query_tokens & entry_terms: return True`, checked against
  `topic_history` inside `select_topic_candidates()`, not just the
  active snapshot). No bug, unmodified.
- **Scenario D** ("Board itu WiFi-nya gimana?" - demonstrative-
  anchored, but 2 real residual tokens, not 1): already correctly
  refuses via the pre-existing `len(query_tokens) != 1: return False`
  check - the same boundary Scenario 1 (Sprint 47) already exercises.
  No bug, unmodified.
- **Scenario E** (attribute continuity - "Tank itu pompanya kecil."
  then "Kalau filternya?" then a later query): the aquascape identity
  correctly survives across all 3 turns via Sprint 47's own Fix #1
  (`is_demonstrative_anchored_followup()`); a later, unrelated query
  correctly recovers the identity. No bug, unmodified - re-verified
  end-to-end as a regression lock (`test_17`).
- **Scenario F** (correction-driven identity - "Pakai ESP32." → "Eh
  maksudku ESP32-S3." → "Board itu RAM-nya berapa?" → a later query):
  the corrected identity ("ESP32-S3") correctly remains available for
  a later, unrelated query. No bug, unmodified - re-verified end-to-
  end as a regression lock (`test_18`).
- **Scenario G** ("Board itu gimana?" in a genuinely single-topic
  conversation - no other topic exists at all): correctly resolves via
  recency (Invariant 6 - "single-topic conversations may use stronger
  fallback"). Confirmed this sprint's own new gate does NOT fire here
  (its own `distinct_other_count >= 1` precondition never triggers with
  zero other topics). No bug, unmodified.
- **Scenario H** ("Board itu gimana?" with 3 competing topics live):
  already correctly refuses via the pre-existing `distinct_other_count
  >= 2` guard (Sprint 44 Phase 7). No bug, unmodified - this sprint's
  own new gate is scoped to the `== 1` case ONLY and never overrides or
  duplicates this tier.

## Phase 2 - blast-radius analysis

**Defect category:** an interaction between the "trust recency when
ambiguity is low" fallback tier and the `distinct_other_count`
ambiguity threshold - specifically the exact boundary condition
`distinct_other_count == 1` with zero lexical/normalized grounding in
either candidate. Not a lineage/coverage issue, not an alias-
normalization issue, not a candidate-confidence issue in isolation -
narrowly the recency-trust tier's own calibration at that one boundary.

**Regression tests protecting current behavior at this exact boundary**
(identified before writing any fix): `test_20_single_other_topic_no_
conflict_still_trusted` and `test_21_lineage_entries_not_counted_as_
distinct_others` (`tests/test_entity_concept_continuity.py`, Sprint
44); `test_27_e2e_no_contamination_reverse_direction` (`tests/test_
contextual_reference_robustness.py`, Sprint 46). These are the exact
three tests Sprint 47's own two rejected attempts broke (in various
combinations) - treated as hard regression boundaries for any new
mechanism, not just the old one.

## Root cause

The "mic" and "board" cases are LITERALLY indistinguishable using
`distinct_other_count`/lexical overlap alone - both are a curated
single token with zero grounding in either of exactly 2 live topics.
Direct token/regex inspection (not guesswork) of the two EXAMPLE
QUERIES found they are not textually identical: "Board itu gimana?"
places the demonstrative "itu" immediately after the sole content word
(the query's own 2nd word - `_DEMONSTRATIVE_ANCHORED_RE` matches);
"Mic-nya gimana?" does not (the clitic "-nya" is fused directly onto
the noun, no separate demonstrative word - the regex does not match).
In Indonesian, a demonstrative immediately after a lone noun
idiomatically marks a back-reference to something already established/
known in the conversation - not necessarily the most-recently-active
thing - while a bare possessive/clitic follow-up naturally continues
whatever is presently active. This is a GRAMMATICAL signal, computed
via a regex Sprint 47 already built for a different purpose (`is_
demonstrative_anchored_followup()`'s MERGE-eligibility decision), not
a new vocabulary/threshold mechanism.

This hypothesis was verified, not assumed: every existing test
exercising the `distinct_other_count == 1` "trust recency" tier
(`test_20`, `test_21`, `test_27`, plus `test_35_empty_query_is_always_
relevant`, `test_38`/`test_39`/`test_41` in `test_semantic_context_
bridging.py`, and the 3-way-ambiguous `test_20`/`test_35` in Sprint
47's own test file) was individually checked against `_DEMONSTRATIVE_
ANCHORED_RE` and the `active_score` branch it falls into, confirming
none of them would change behavior under the new gate before it was
even written.

## Before / after behavior

| Query | Topics | Before | After |
|---|---|---|---|
| "Board itu gimana?" | ESP32/INMP441 (older), Aquascape (active) | confidently resolves Aquascape (wrong) | refuses (no candidate) |
| "Mic-nya gimana?" | Aquarium (older), ESP32/INMP441 (active) | resolves ESP32/INMP441 (correct) | unchanged - resolves ESP32/INMP441 |
| "Filternya gimana?" (test_20 shape) | 1 other topic, no conflict | trusts recency | unchanged - trusts recency |
| "Yang murah?" (test_21 shape) | lineage entry doesn't count | trusts recency | unchanged - trusts recency |
| "Board itu gimana?" (single topic) | no other topic | trusts recency | unchanged - trusts recency |
| "Board itu gimana?" (3 topics) | already refuses via `>= 2` | refuses | unchanged - refuses |

## Provenance model

**No new provenance data structure was introduced.** `ActiveTopic
Snapshot`'s field set (`terms`, `turns_since_active`, `list_items`,
`status`, `source_sentence`) is unchanged - still the same flat bag-of-
terms representation confirmed sufficient by Sprints 44-47. The fix is
a single additive `if` inside the existing `is_active_topic_relevant_
to_query()` function, gated on a property of the QUERY TEXT itself
(demonstrative-anchoring), not on any new per-entry state. This
satisfies the sprint's own "smallest safe mechanism - only introduce a
new structure if the existing representation cannot solve the problem
safely" bar without needing to introduce anything new at all.

## Ambiguity policy (confirmed unchanged, restated for this sprint's own scope)

- **Strong single-lineage evidence** (raw token overlap, or a unique
  normalized-bridging match): resolve to that topic. Unchanged.
- **Multiple-lineage evidence, exactly 1 other topic, non-
  demonstrative query**: still resolves via recency (Sprint 44's own
  "nothing else it could mean" tier) - unchanged, verified via
  regression.
- **Multiple-lineage evidence, exactly 1 other topic, demonstrative-
  anchored query, zero grounding in either candidate**: NEW this
  sprint - refuses rather than trusting recency.
- **Multiple-lineage evidence, >= 2 other topics**: refuses regardless
  of demonstrative-anchoring - unchanged (the pre-existing `>= 2` tier
  already covers this; the new gate is scoped narrowly to the `== 1`
  case that tier deliberately leaves alone).
- **No lineage evidence at all** (empty query tokens): trusts recency
  (nothing to be irrelevant to) - unchanged.
- **Recency alone never overrides strong contradictory provenance**:
  unchanged - the new gate only ever REFUSES, never fabricates a
  competing resolution.
- **Generic shared term never identifies an entity by itself**:
  unchanged - this is exactly what the new gate strengthens for the
  demonstrative-anchored shape.

## E2E results

All 8 scenarios (A-H) reproduced and locked in as regression tests
(`tests/test_bounded_entity_provenance.py::test_12` through `test_20`).
Scenario B fixed; Scenario A confirmed unchanged/not-fixed (different
code path, see below); Scenarios C-H confirmed already-correct and
unmodified.

## Tests

`tests/test_bounded_entity_provenance.py` - 32 tests, all passing:

- 11 unit tests for the new gate (`test_01`-`test_11`), including 3
  explicit regression locks for the three hardest existing boundaries
  (`test_03` = `test_20`'s shape, `test_04` = `test_21`'s shape,
  `test_02`/`test_09` = `test_27`'s shape) and a source-inspection lock
  (`test_11`) confirming the fix reuses the existing regex and leaves
  the `>= 2` tier's own line untouched.
- 9 E2E tests for Scenarios A-H (`test_12`-`test_20`), each reading the
  actual rendered system prompt via `RuntimeDemoConsole`.
- 4 shared-alias/synonym-group interaction tests (`test_21`-`test_24`)
  - confirms the fix generalizes across every `_TOKEN_SYNONYM_GROUPS`
    member (board/mikrokontroler, gpu/vga, pompa/pump), not just the
    two worked examples.
- 2 bounded-state/isolation E2E tests (`test_25` topic-history
  eviction beyond `_TOPIC_HISTORY_MAX_ENTRIES`, `test_26` cross-
  conversation isolation) and 1 unrelated-query test (`test_27`).
- 2 performance tests (`test_28`, `test_29`).
- 3 tests locking in the investigated-and-rejected limitation #9
  approach (`test_30`-`test_32`).

## Regression results

Targeted core suite (Sprint 47's own 13-file list plus this sprint's
new file): **677 passed, 0 failed.** Full repository sweep (92/94
collectible files, `pytest -n 4`, run in 6 chunks to fit the sandbox's
own per-call wall-clock cap): **2822 passed, 12 failed** - 10 byte-for-
byte identical to the standing, already-documented baseline; the other
2 (`test_llm_tts_streaming_production.py::test_14_cancellation_during_
synthesis`, `test_streaming_e2e.py::test_D_barge_in_between_llm_and_
tts_chunk_never_plays`) were NOT silently classified as pre-existing -
re-run in ISOLATION (serial, not under `-n 4`) and both passed cleanly,
confirming parallel-execution timing contention (neither file, nor the
TTS/streaming subsystem either exercises, was touched by this sprint's
edit), not a real regression.

## Performance

The new gate measured directly (3,000-call average, using the exact
query/history shape that reaches it - the worst case) at well under
1ms/call, comfortably inside the 5ms/turn target. A second measurement
on the unaffected `active_score > 0` code path confirmed the fix adds
zero measurable cost to calls that never reach the new gate at all. No
network calls, no model inference, no embeddings.

## Persistent-state verification

Only `luno/memory_context.py` (source) and `tests/test_bounded_entity_
provenance.py` (new test file) were modified/created this sprint.
SHA256 hashes of all 15 top-level `config/*.json` files were captured
before any change. Isolated verification - running ONLY this sprint's
own new test file - confirmed all 15 files byte-identical before/after.
A second, stricter isolated run (this sprint's new file PLUS the full
Sprint 43-47 core entity/reference test suite) confirmed the same:
all 15 files unchanged. `_active_topic`/`_topic_history` confirmed to
remain plain, non-persistent, in-memory `dict`s. `luno/memory_
context.py` still never touches file I/O (per its own docstring,
unchanged).

## Known limitations (carried forward, one new item added)

All Sprint 47 limitations (#1-#7, unrelated to this sprint) remain
unchanged. Limitation #8 (the "board"/"mic" cross-topic-contamination
gap) is **RESOLVED** by this sprint's fix. Limitation #9 (Aquascape A/
B conflation) **remains open** - investigated this sprint (the
"distinguisher token" idea from the handover's own speculation was
tried and found unsafe, see "Investigated and REJECTED" below), not
fixed.

**Investigated and REJECTED - distinguisher-token signal for
limitation #9:** a short, capitalized, standalone letter/number token
("A"/"B") appearing in both entries' own terms but with DIFFERENT
values was hypothesized (per the handover's own speculative Sprint 49
candidate) to signal "these are explicitly, separately named" entities
more strongly than majority coverage alone. Direct tokenizer inspection
found this is NOT a reliable foundation: `analyze_query("Aquascape A
pakai pompa kecil.")` DROPS the single-letter token "a" entirely
(filtered as a stopword/too-short token upstream), while `analyze_
query("Aquascape B pakai pompa besar.")` KEEPS "b" (not a recognized
stopword) - a live, reproduced, asymmetric tokenizer behavior, not
speculation. Building a "these are different entities" signal on a
foundation that silently disappears for one of the two most natural
distinguisher letters ("A") but not the other ("B") would be
inconsistent and unsafe - concretely worse than the current, at-least-
CONSISTENT (if imperfect) majority-coverage heuristic. NOT implemented.

## Invariants (reaffirmed, one new item)

All Sprint 47 invariants hold unchanged. New this sprint: the
`distinct_other_count >= 1 and _DEMONSTRATIVE_ANCHORED_RE.search(text)`
gate inside `is_active_topic_relevant_to_query()`'s `active_score ==
0` branch must not be widened, generalized, or made to fire for
`distinct_other_count == 0` (would break Invariant 6 - single-topic
conversations may use stronger fallback) - any future change to this
gate must be re-verified against `test_20`/`test_21`/Sprint 46's
`test_27` AND this sprint's own Scenario B/B-mirror pair simultaneously.

## Recommended Sprint 49 investigation

Limitation #9 (Aquascape A/B conflation, `coverage > 0.5` lineage-skip
heuristic) remains the most promising next target - but the
"distinguisher token" approach is now a CONFIRMED dead end (see above),
so a genuinely different mechanism is needed, not a retry of the same
idea with a workaround for the tokenizer asymmetry (which would itself
risk becoming exactly the kind of fragile, letter-specific special-
casing this project's discipline forbids). Possible directions worth
investigating from first principles: whether the ORIGINAL, un-
tokenized user text (before stopword-filtering) could be consulted
narrowly and specifically for a trailing single-character/short-token
differentiator immediately after a shared noun, without altering the
shared tokenizer's own general-purpose stopword behavior anywhere else;
or whether a different, non-token-based signal (e.g. sentence-position
of the differentiator, or explicit user framing like "yang A" vs "yang
B") is more robust. Must be proven via live reproduction (both this
sprint's Scenario A AND every existing lineage-coverage regression test
as simultaneous guards) before implementation - same discipline every
sprint since 43 has followed.
