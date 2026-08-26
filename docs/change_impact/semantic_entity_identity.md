# Change Impact: Semantic Entity Memory & Reference Graph (Sprint 47)

## Goal

Improve Luno's ability to preserve ENTITY IDENTITY across natural
conversation - without embeddings, an LLM judge, a second ranking
system, or an unbounded semantic-memory subsystem. Not "make every
vague phrase resolve" - preserve identity when evidence exists, resolve
aliases when evidence exists, distinguish related entities from
unrelated ones, and refuse to guess when evidence is insufficient.

## Phase 0 - Takeover reconnaissance

Read `docs/project_handover.md`, `docs/project_handover.json`,
`ARCHITECTURE_GUARD.md` (through §46), `docs/testing/regression_
baseline.md` (Sprint 46 section), `docs/change_impact/contextual_
reference_robustness.md`. Verified Sprint 45/46 work is actually
present in the checkout (not merely claimed by the docs): confirmed
`_normalize_terms_for_bridging()`'s root->canon chain, the historical-
query guard in `is_active_topic_relevant_to_query()`, and "kenapa"/
"napa"/"mengapa" in `_TOPIC_OVERLAP_STOPWORDS` all present in `luno/
memory_context.py`; confirmed `tests/test_contextual_reference_
robustness.py` exists and passes (35/35); ran the documented core
regression command and got the exact documented baseline (610 passed,
0 failed). No discrepancy between the handover documentation and the
actual checkout - proceeded without needing to stop and report one.

## Phase 1-2 - E2E probe matrix (6 scenarios) and gap identification

Built real E2E probes through `RuntimeDemoConsole` for all 6 scenarios
named in this sprint's brief, using CLEAN canned replies (never
containing the "correct" answer, so only the system's own actual
candidate-injection behavior was trusted as signal - an earlier probe
round using richer, more natural-sounding replies was discarded after
noticing the replies themselves could leak the right answer into the
merged topic snapshot, producing false-positive "it already works"
readings).

- **Scenario 1** (multi-name entity, "board" never previously
  grounding "ESP32-S3"): correctly refuses. NOT a bug - see "Findings"
  below.
- **Scenario 2** (explicit user alias, "GPU itu buat AI."): already
  resolves via plain raw-token overlap. No bug.
- **Scenario 3** (entity + attribute continuity via a renaming noun,
  "Tank itu pompanya kecil." after an aquascape statement): REAL bug -
  entity erosion. Fixed.
- **Scenario 4** (alias collision, "komputer" for PC+laptop): resolves
  to the SAME shape as an existing, deliberately-tested Sprint 44
  precedent. Not a new bug; a known limitation when the two devices are
  genuinely separate topics (see below).
- **Scenario 5** (cross-topic contamination, "board" grounded in
  NEITHER of 2 live topics): REAL bug - wrongly, confidently resolved
  to the merely-most-recent topic. Investigated a fix, reproduced it as
  correct for this case, then REJECTED after it broke an existing
  guarantee (see below).
- **Scenario 6** (correction-driven identity, "Board itu RAM-nya
  berapa?" after a correction): REAL bug - entity erosion, same root
  cause and same fix as Scenario 3.

## Fix #1 - `is_demonstrative_anchored_followup()`

**Files:** `luno/memory_context.py` (new function + 2 new module-level
constants), `main_runtime_demo.py` (one new `or` clause in the existing
`is_merge` decision).

**Root cause:** `is_sparse_unknown_followup()` (Sprint 44) already
recognizes an `"unknown"`-classified turn with `<= 1` real residual
token as merge-worthy rather than replace-worthy. Live reproduction
found a DIFFERENT, common shape falls just outside that bound: "Board
itu RAM-nya berapa?" (residual `{"board", "ram"}` - 2 real, substantive
tokens, neither filler) and "Tank itu pompanya kecil." (similar shape).
Both destructively REPLACED the established active topic (ESP32-S3 in
Scenario 6, aquascape in Scenario 3) with a fresh, disconnected
snapshot - discarding the entity identity before a later, unambiguous
follow-up could recover it.

**Fix:** a new function recognizes this SECOND shape via a GRAMMATICAL,
domain-independent signal rather than a vocabulary lookup: an
`"unknown"`-classified turn whose own 2nd word is the demonstrative
"itu"/"ini" ("that NOUN's ATTRIBUTE is how much?"), bounded to `<= 3`
real residual tokens (one tier looser than the sparse check's `<= 1`,
since this shape needs room for both a referring noun and an
attribute). Wired into `main_runtime_demo.py`'s existing `is_merge`
decision as a third additive `or` clause. Does NOT change `classify_
reference_type()`'s own output and does NOT inject a candidate for the
turn itself (matches `is_sparse_unknown_followup()`'s own precedent
exactly) - it only prevents the destructive state loss.

**Two guards against over-firing**, verified via both direct unit tests
and the full regression sweep:
1. Position-anchored (`itu`/`ini` must be the sentence's own 2nd word)
   - "yang itu"/"kalau itu"-shaped turns already classify at higher
   precedence (`direct_reference`) and never reach this check.
2. Bounded residual-token cap (`<= 3`) - a genuinely fresh, substantial,
   independent sentence that merely happens to have "ini"/"itu" as its
   2nd word ("Motor ini bisa dikendalikan lewat PWM dengan
   mikrokontroler apa saja." - 4+ real content words) is correctly
   excluded.

**Before:** "Terus?" after "Board itu RAM-nya berapa?" resolved to a
snapshot containing only that turn's own words - no "esp32"/"s3".
**After:** correctly resolves to a snapshot retaining "esp32"/"s3"
merged with the new turn's own words. Same before/after shape verified
for Scenario 3 (aquascape identity survives the "Tank itu..." rename).

## Investigated and REJECTED (1)

### Curated-vocabulary-gated ambiguity refusal (Scenario 5)

`is_active_topic_relevant_to_query()`'s single-token/zero-score branch
falls through to `return True` (trust recency) whenever fewer than 2
distinct OTHER topics exist in `topic_history` - this is what let
Scenario 5's "board" (grounded in NEITHER ESP32 nor the more-recent
aquascape topic) wrongly, confidently resolve to aquascape. Two
candidate fixes were investigated:

1. **Globally widening `distinct_other_count >= 2` to `>= 1`.** Fixed
   Scenario 5 in isolation, but broke `tests/test_entity_concept_
   continuity.py::test_20_single_other_topic_no_conflict_still_
   trusted` (Sprint 44's own deliberately-tested precedent: "Filternya
   gimana?" with aquascape active and an unrelated GPU topic as the
   sole "other" MUST still trust recency) and 2 further tests. Reverted
   immediately after the regression run.
2. **Gating the refusal to `distinct_other_count >= 1` ONLY when the
   query's single token is a member of the curated `_TOKEN_SYNONYM_
   GROUPS` table** (a narrower, more targeted attempt - "board" is a
   group member, "filter" is not, so this should in principle leave
   Sprint 44's own precedent test alone). Fixed Scenario 5 correctly,
   but broke `tests/test_contextual_reference_robustness.py::
   test_27_e2e_no_contamination_reverse_direction` (Sprint 46's own
   test: "Aquarium saya 50x25." -> "ESP32 pakai INMP441." -> "Mic-nya
   gimana?" must resolve to ESP32/INMP441). "mic" IS a curated
   vocabulary member too, and this case has the IDENTICAL formal shape
   as Scenario 5 (a curated single token, zero grounding in the active
   topic, zero grounding in the sole other topic, exactly 1 other
   topic) - but the CORRECT answer is the OPPOSITE: here recency SHOULD
   win (ESP32/INMP441 genuinely is what "mic" refers to, it just was
   never literally spelled out with that word), whereas in Scenario 5
   recency should NOT win (aquascape genuinely has nothing to do with
   "board"). No general, deterministic, non-world-knowledge rule
   distinguishes these two cases from the query text and topic history
   alone - telling them apart requires knowing that INMP441 is
   plausibly a "mic" and aquascape/pump is not plausibly a "board",
   exactly the kind of semantic/world knowledge this project's
   architecture deliberately does not have. Reverted.

**Left unfixed; documented as a known limitation** (see below). Both
attempts are removed from the source with no residual code trace, since
the second one's failure mode is now understood well enough that a
comment-only marker was judged less valuable than this document plus
the explicit regression-lock test (`test_21_known_limitation_two_
topic_cross_contamination_board_case`) that pins the CURRENT behavior
for a future agent to diff against.

## New known limitation discovered (not previously documented)

**Two distinctly-named entities sharing high generic-vocabulary
overlap are conflated by the `coverage > 0.5` lineage-skip heuristic.**
"Aquascape A pakai pompa kecil." then "Aquascape B pakai pompa besar."
then "Pompanya gimana?": entry A's significant terms are well over 50%
covered by entry B's (both share "aquascape"/"pompa"/"pakai"), so the
existing lineage-skip check (`luno/memory_context.py`, both branches of
`is_active_topic_relevant_to_query()`, already discussed extensively in
`ARCHITECTURE_GUARD.md` §46) treats A as "the same lineage, already
merged in" rather than a genuinely distinct competitor - even though A
and B are explicitly, separately named. The query then resolves to B
(the merely more recent) with no ambiguity signal at all. NOT fixed
this sprint: the `coverage > 0.5` heuristic is deliberately majority-
based (not strict-subset) specifically to avoid a DIFFERENT, earlier,
already-fixed false-ambiguity bug (`test_15` in `tests/test_memory_
comparison_topic_preservation.py`, referenced in the Sprint 43 comment
on this exact check) where a merge-derived entry legitimately drops a
few incidental words relative to its own origin. Tightening the
threshold risks reintroducing that bug; loosening the "is this the same
entity" signal in a different way would need some notion of "these are
explicitly, separately named" which the flat bag-of-terms
representation doesn't currently distinguish from ordinary descriptive
vocabulary. Documented, locked in as `test_24_known_limitation_same_
generic_vocabulary_two_named_entities`, left for a future sprint.

## Entity representation decision

**No new entity representation was introduced.** Every reproduced,
FIXABLE gap (Scenarios 3 and 6) was a MERGE-ELIGIBILITY decision issue
on the EXISTING flat bag-of-terms `ActiveTopicSnapshot` - not a
representation gap. The unfixed gaps (Scenario 5's cross-topic
contamination, the new two-named-entities limitation) are AMBIGUITY-
RESOLUTION issues that would require either genuine semantic/world
knowledge (out of scope, forbidden) or a much larger, riskier change to
already-load-bearing ambiguity-safety thresholds (rejected after
concrete regression evidence) - neither is "the flat representation is
structurally insufficient", which is the bar this sprint's own brief
sets for introducing a new structure. Consistent with Sprint 44/45/46's
own prior finding.

## Alias resolution rules (confirmed, not modified)

The evidence hierarchy already implicit in the existing pipeline was
confirmed, not changed:
1. Explicit user correction (`repair_reference`, Sprint 38) - highest
   precedence, always wins.
2. Explicit user alias declaration ("GPU itu buat AI.") - becomes part
   of the topic's own raw terms the instant it's stated; resolves via
   ordinary raw-token overlap, no special mechanism needed.
3. Exact canonical-name match (raw token overlap).
4. Existing bounded alias group (`_TOKEN_SYNONYM_GROUPS`/`_PHRASES`,
   Sprint 43).
5. Strong lexical overlap with the active entity (normalized bridging).
6. Recent topic evidence (the single-token low-ambiguity fallback,
   Sprint 44) - now also correctly triggered for the demonstrative-
   anchored shape this sprint added (Fix #1), even at 2 real tokens.
7. Cross-topic evidence (`topic_history` candidate selection,
   unconditional, Sprint 43).

Recency alone is never used to manufacture an entity relationship when
2+ genuinely distinct competing topics exist (`distinct_other_count >=
2`, Sprint 44 Phase 7, unmodified). The `>= 1`/curated-vocabulary
narrowing investigated this sprint remains explicitly NOT implemented
(see above).

## Ambiguity policy (confirmed, extended by test coverage only)

Prefer correct resolution or explicit refusal over confident wrong
resolution - re-verified via 3-topic ("ESP32/aquascape/PC" then
"Perangkat itu gimana?"/"Alat itu gimana?") refusal tests, both
correctly refuse. The 2-topic gap (Scenario 5) and the two-named-
entities gap remain open, both explicitly documented above rather than
silently left untested.

## Tests

`tests/test_semantic_entity_identity.py` - 35 tests, all passing: 9
unit tests for Fix #1 (`is_demonstrative_anchored_followup()`), 4 E2E
tests locking in Fix #1's actual behavior (Scenarios 3 and 6), 3 tests
for explicit/assistant-introduced alias and canonical-entity matching,
3 tests for alias-after-several-turns/pronoun/possessive-nya, 5 tests
for competing-entity ambiguity (2 known-limitation regression locks +
1 collision-no-crash test + 2 refusal tests), 4 tests for topic-
switching/contamination/cross-conversation isolation, 3 tests for
bounded-state behavior, 1 performance test, 3 regression-lock tests for
already-correct Scenario 1/2 behavior.

## Regression

Core suite (`test_semantic_entity_identity.py` + `test_contextual_
reference_robustness.py` + the 10 memory/topic/reference/temporal/
semantic-bridging/entity-continuity suites already used by Sprints
43-46): **645 passed, 0 failed.**

Full repository sweep (92 collectible files, `pytest -n 4`, 2
pre-existing uncollectible files excluded): **2817 passed, 17 failed.**
Of the 17: 10 are byte-for-byte identical to the standing,
already-documented baseline. The other 7 (`test_runtime_demo.py::
test_episodic_memory_end_to_end_detect_persist_retrieve_alongside_
existing_context`, `test_streaming_e2e.py::
test_D_barge_in_between_llm_and_tts_chunk_never_plays`, 3x `test_tts_
chunk_pipelining.py`, 2x `test_voice_pipeline_latency.py`) were NOT
silently classified as pre-existing - re-run in ISOLATION (serial, not
under `-n 4`) and **all 7 passed cleanly**, confirming parallel-
execution timing contention (none of these files or the subsystems
they test - episodic memory persistence timing, TTS chunk pipelining,
barge-in, voice latency - were touched by any Sprint 47 edit), not a
real regression.

## Performance

`is_demonstrative_anchored_followup()` measured directly (20,000-call
average): 0.023ms/call - well under the 5ms/turn target. No network
calls, no model inference, no embeddings.

## Persistent state

Only `luno/memory_context.py` and `main_runtime_demo.py` (source) and
`tests/test_semantic_entity_identity.py` (new test file) were
modified/created this sprint. Isolated verification (running ONLY this
sprint's own new/touched test files) confirmed `config/long_term_
memory.json` and `config/relationship_state.json` are byte-identical
before and after. During the FULL repository sweep, both files DID
change - traced to OTHER, pre-existing tests elsewhere in the 92-file
suite that legitimately exercise the real persistence layer (e.g.
episodic/long-term memory end-to-end tests), confirmed unrelated to
this sprint's own source edits (neither touched file is written to by
any code path in `luno/memory_context.py`, which this sprint's own
docstring already states "never touches file I/O"). The other 13 of 15
top-level `config/*.json` files are unmodified. `_active_topic`/
`_topic_history` confirmed to remain plain, non-persistent, in-memory
`dict`s (test 30). No new module-level entity store/graph was
introduced (test 31).

## Files changed

- `luno/memory_context.py` - 1 new function (`is_demonstrative_
  anchored_followup()`) + 2 new module-level constants
  (`_DEMONSTRATIVE_ANCHORED_RE`, `_DEMONSTRATIVE_ANCHORED_MAX_
  RESIDUAL_TOKENS`), purely additive.
- `main_runtime_demo.py` - 1 new `or` clause in the existing `is_merge`
  decision (function unchanged otherwise).
- `tests/test_semantic_entity_identity.py` - new, 35 tests.
- `docs/change_impact/semantic_entity_identity.md` - new (this file).
- `ARCHITECTURE_GUARD.md` - new §47.
- `docs/testing/regression_baseline.md` - new Sprint 47 section.
- `docs/project_handover.md`, `docs/project_handover.json` - updated.

No changes to `_rank_key()`, `_apply_budget()`, `assemble_context()`'s
parameter list, `ActiveTopicSnapshot`'s field set, `classify_reference_
type()`'s own output for any existing phrase, `update_active_topic()`/
`update_topic_history()`'s own bodies, `select_topic_candidates()`,
`select_temporal_fallback_candidate()`, `is_active_topic_relevant_to_
query()` (both investigated changes there were reverted), `_TOKEN_
SYNONYM_GROUPS`/`_TOKEN_SYNONYM_PHRASES` (zero new synonym groups), TTS,
streaming, voice selection, cancellation semantics, or response/memory
ranking.
