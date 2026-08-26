# Change Impact: Contextual Reference Robustness (Sprint 46)

## Goal

Improve Luno's conversational reference handling so that short,
elliptical, Indonesian conversational follow-ups remain attached to the
correct entity/concept without causing cross-topic contamination, without
introducing an LLM judge, an embedding model, a second ranking system, an
external vector database, a synonym-dictionary explosion, global topic
state, persistent raw conversation state, or a new memory architecture.

## Phase 0 - Reconnaissance (read-only)

Read `luno/memory.py`, `luno/memory_context.py`, `main_runtime_demo.py`,
`luno/response_output.py`, `luno/incremental_speech.py`,
`docs/project_handover.md`, `docs/project_handover.json`,
`ARCHITECTURE_GUARD.md` §43-45, `docs/testing/regression_baseline.md`'s
latest section, and the latest `docs/change_impact/*.md` files. Confirmed
`luno/response_output.py` (TTS/voice-output sentence splitting) and
`luno/incremental_speech.py` (streaming/cancellation) are unrelated to
reference resolution - no changes anticipated or made there. The pipeline
map used throughout: `classify_reference_type()` ->
`is_pure_reference_followup()` / `is_merge_reference_followup()` ->
`update_active_topic()` -> `update_topic_history()` ->
`select_topic_candidates()` -> temporal fallback / ordinal resolution ->
`topic_history_to_relevant_memories()` / `active_topic_to_relevant_
memory()` -> `assemble_context()` -> ranking -> budget -> render -> final
system prompt.

## Phase 1-2 - E2E probe matrix (Scenarios A-J) and gap identification

Built and ran a probe matrix through the real `RuntimeDemoConsole` event
flow (not direct unit calls), covering all 10 scenarios named in this
sprint's brief plus several adversarial combinations. Findings:

- **Scenarios A, C, E, F, G, J: already correct.** No changes made -
  locked in as regression tests (`test_contextual_reference_robustness.py`
  §4, tests 19-25).
- **Scenario B / D (alias continuity): one real bug found and fixed**
  (fix #1 below).
- **Scenario H (attribute references, "yang lebih X?"/"yang paling X?"):
  one real bug found, a fix was implemented, reproduced as correctly
  fixing the target case, and then REJECTED after it broke an existing
  test** (see "Investigated and rejected" below). Documented as a known
  limitation.
- **Scenario I (temporal references): one real bug found and fixed**
  (fix #2 below).
- **Phase 3's own worked GPU/RTX3060 example: one real bug found and
  fixed** (fix #3 below). Phase 3's own mic/I2S/wireless example was
  already correct.
- **Phase 6 (contamination), both directions: already correct.** No
  changes made - locked in as regression tests (tests 26-27).

## Fixes applied (3)

### Fix #1 - `_normalize_terms_for_bridging()` chained-alias gap

**File:** `luno/memory_context.py`, function `_normalize_terms_for_
bridging()`.

**Root cause:** the function only ever looked up `_TOKEN_SYNONYM_CANON`
against the ORIGINAL token, never against the token's own affix-stripped
root computed two lines above. Any word needing BOTH transformations
chained together ("mikrofonnya" -> root "mikrofon" -> canon "mic",
"mengganti" -> root "ganti" -> canon "upgrade") silently lost the
synonym step, even though "mic-nya gimana?" (root alone, no synonym
step needed beyond the literal stripped form) already worked. Live
reproduction: "ESP32 pakai INMP441 sebagai mic." -> "mic-nya gimana?"
(correctly resolved) -> "Mikrofonnya bagaimana?" (silently failed).

**Fix:** added one extra `_TOKEN_SYNONYM_CANON.get(root)` lookup against
the already-computed root, purely additive (never removes or replaces
existing behavior).

**Before:** `_normalize_terms_for_bridging({'mikrofonnya'})` ==
`frozenset({'mikrofonnya', 'mikrofon'})` (missing 'mic').
**After:** `frozenset({'mikrofonnya', 'mikrofon', 'mic'})`.

### Fix #2 - historical-query single-token guard

**File:** `luno/memory_context.py`, function `is_active_topic_relevant_
to_query()`, the `active_score == 0` / single-residual-token branch.

**Root cause:** a lone residual token that is ITSELF a historical-query
marker ("sebelumnya", "dulu", "yang lama", "pernah" -
`luno.memory.is_historical_query()`, Sprint 40) was treated as neutral,
signal-less filler the same way "gimana?"/"terus?" are - it fell through
to `return True` whenever fewer than 2 distinct OTHER topics existed in
`topic_history`, confidently trusting the CURRENT active topic even
though the query explicitly asks about something else. Live
reproduction: "Rencana saya beli SSD." -> "Sekarang pakai HDD." -> "Yang
sebelumnya gimana?" confidently injected the CURRENT (HDD) topic as the
answer to a query about the PREVIOUS state - "confidently wrong" context
injection, the exact failure mode Phase 5's own ambiguity-safety
principle forbids.

**Fix:** a narrow guard - when the query is historical-marked
(`memory_module.is_historical_query(text)`) AND the active snapshot's own
`status` represents a present/future state (`"active"`, `"completed"`, or
`"planned"` - i.e. it is not itself already a past/superseded entry),
the single-token fallback now returns `False` instead of `True`. This
does not fabricate a replacement candidate - it only stops the
single-slot branch from claiming relevance, so `main_runtime_demo.py`'s
own `elif`/`else` chain falls through to `select_temporal_fallback_
candidate()` (Sprint 41), which then either finds a genuinely
status-eligible historical entry, or - as in this exact reproduced case,
since the SSD entry's own status is `"planned"`, not `"superseded"`/
`"cancelled"`, a separate, intentionally UNCHANGED eligibility question -
correctly injects nothing rather than the wrong topic.

**Before:** Scenario I's turn 3 injected "Sekarang pakai HDD." as the
active-conversation-topic candidate.
**After:** no candidate injected (safe - "not enough information" rather
than "confidently wrong").

**Known residual gap (not fixed, out of scope):** the ideal answer would
resolve to the actual SSD plan entry, not merely refuse. That would
require additionally widening `_TEMPORAL_FALLBACK_ELIGIBLE_STATUS`'s
`"historical"` tier to include `"planned"` status entries - a separate,
larger semantic decision (is a not-yet-superseded PLAN the same thing as
a "historical" fact?) not attempted this sprint. Documented as a known
limitation below.

### Fix #3 - "kenapa"/"napa"/"mengapa" stopword gap (entity erosion)

**File:** `luno/memory_context.py`, constant `_TOPIC_OVERLAP_STOPWORDS`.

**Root cause:** "kenapa"/"napa"/"mengapa" ("why", formal/colloquial/
clipped-informal variants) were missing from this stopword list, unlike
the colloquial "kok" already present. This was not merely a
missed-resolution case but a genuine ENTITY-EROSION bug: "GPU-nya
kenapa?" has 2 real residual tokens ("gpu", "kenapa") without this fix,
narrowly missing `is_sparse_unknown_followup()`'s (Sprint 44) `<= 1`
threshold. Since it also classifies `"unknown"` and "gpu" was never
literally said before ("RTX 3060" was never called "gpu" - the
deliberate no-product-to-category-fabrication boundary correctly refuses
to inject a candidate for THIS turn), it fell through to an ordinary
RICH-turn REPLACE, permanently discarding the RTX 3060/"panas" identity
- so a LATER, unambiguous alias follow-up ("Kartu grafisnya bagaimana?",
which correctly canonicalizes "kartu grafis"->"gpu") wrongly resolved to
this turn's own disconnected replacement snapshot instead of the
original RTX 3060 topic. Live reproduction: "RTX 3060 saya panas." ->
"GPU-nya kenapa?" (correctly, no candidate injected for itself) ->
"Kartu grafisnya bagaimana?" (wrongly resolved to a disconnected
snapshot with no RTX 3060/"panas" identity).

**Fix:** added "kenapa", "napa", "mengapa" to `_TOPIC_OVERLAP_STOPWORDS`,
matching "kok"'s existing treatment. With "kenapa" filtered, "GPU-nya
kenapa?"'s residual drops to `{"gpu"}` (1 token), so `is_sparse_unknown_
followup()` now correctly recognizes it as an elliptical fragment and
merges into (rather than replaces) the active topic.

**Before:** "Kartu grafisnya bagaimana?" (3rd turn) resolved to a
disconnected snapshot containing only "GPU-nya kenapa?"'s own words plus
its canned reply text - no "rtx"/"panas".
**After:** correctly resolves to a snapshot containing "rtx"/"panas"
merged with the GPU turn's own words.

## Investigated and REJECTED (2)

Both were reproduced as correctly fixing their target case, then found
via full regression to break an existing, deliberately-tested guarantee,
and were reverted. Both are documented in-place with explanatory
comments at the exact code location, and locked in as regression tests
(`test_contextual_reference_robustness.py` §6, tests 28-30) so a future
agent does not silently re-attempt the same fix without understanding
why it fails.

### Rejected #1 - widening the `coverage > 0.5` lineage-skip check

`is_active_topic_relevant_to_query()` has two separate `coverage > 0.5`
checks (one in the zero-score branch's `distinct_other_count` loop, one
in the non-zero-score branch's tie-check loop) that treat a topic-history
entry whose own significant terms are MOSTLY already covered by the
active snapshot as "same lineage, not a distinct competitor". Widening
both to `>= 0.5` fixed a genuine same-entity lineage case landing at
EXACTLY 50% coverage, but broke `tests/test_semantic_context_bridging.py
::test_39_tied_normalized_overlap_across_history_is_not_relevant` - a
DIFFERENT, genuinely-disjoint two-topic pair that also happens to land at
exactly 50% coverage for an unrelated reason (two separate topics
coincidentally sharing one verb, "ganti"). `>= 0.5` cannot distinguish
the two cases from coverage alone. **Left at strict `>`.**

### Rejected #2 - adding "lebih"/"paling" to `_TOPIC_OVERLAP_STOPWORDS`

"lebih"/"paling" ("more"/"most") are comparative-intensifier boilerplate
in a "yang lebih X?"/"yang paling X?" fragment, matching the treatment
`luno.memory._ATTRIBUTE_RESIDUAL_STOPWORDS` already gives them. Adding
them fixed a single-topic "ESP32 pakai INMP441." -> "Yang lebih bagus?"/
"Yang paling murah?" case (both wrongly refused today, purely because
"lebih"/"paling" inflate the real-token count to 2, defeating the
Sprint 44 single-token low-ambiguity fallback), but broke
`tests/test_entity_identity_semantic_alias_continuity.py::
test_76_e2e_multi_topic_ambiguity_gpu_vs_pompa` - a genuinely-2-
competing-topic case ("Aku punya GPU RTX 3060." + "Aku juga punya pompa
aquascape." -> "Kalau yang lebih besar gimana?", must NOT inject a
candidate). Stripping "lebih" drops that query to a single real token
("besar"), routing into the single-token fallback's own
`distinct_other_count >= 2` ambiguity refusal - which is calibrated for
3+ live topics (Sprint 44 Phase 7's own reproduction used 3), not exactly
2, so the genuinely-2-topic case silently fell through to `return True`.
Widening that threshold to `>= 1` to close this gap was investigated but
not attempted - a much broader-blast-radius change (every single-other-
topic case in the whole suite currently relies on being trusted, not
refused) than this sprint's own scope justifies for one reproduced case.
**Left out.**

## Known limitations (new, this sprint)

1. "Yang lebih bagus?"/"Yang paling murah?" (and other "yang lebih/
   paling X?" attribute questions) in a genuinely single-topic
   conversation are not resolved - Rejected fix #2 above. Would require
   either a narrower stopword condition scoped to exactly this shape, or
   widening `distinct_other_count`'s threshold with its own dedicated
   regression sweep against every test currently relying on the `>= 2`
   boundary.
2. A historical query ("Yang sebelumnya gimana?") after a `"planned"`
   statement (never explicitly superseded/cancelled by a later,
   unrelated-vocabulary statement) correctly refuses to inject the wrong
   (current) topic, but does not resolve to the actual planned entry
   either - Fix #2's own residual gap above. Would require widening
   `_TEMPORAL_FALLBACK_ELIGIBLE_STATUS["historical"]` to include
   `"planned"`, a separate semantic decision not attempted this sprint.
3. An exact-50%-coverage same-entity topic-history lineage case is not
   recognized as lineage (falls back to the safer "ambiguous, refuse"
   path instead) - Rejected fix #1 above.
4. The flat bag-of-terms representation (unchanged since Sprint 4) still
   cannot represent "what value did this field hold before" - once two
   turns about the same evolving subject are merged into one snapshot,
   the specific PRIOR value is not separately queryable from that merged
   snapshot alone (only from a still-distinct `topic_history` entry, when
   one exists and is reachable by the branches above).
5. 10 pre-existing, environment-specific test failures unrelated to any
   Sprint 43-46 code remain (see `docs/testing/regression_baseline.md`'s
   Sprint 46 entry for the exact list).

## Tests

`tests/test_contextual_reference_robustness.py` - 35 tests, all passing:
6 unit tests for fix #1 (§1) + 2 E2E, 4 unit tests for fix #2 (§2) + 1
E2E, 5 unit/E2E tests for fix #3 (§3), 7 E2E regression locks for
already-correct Scenarios A/C/E/F/G/J (§4), 2 E2E contamination tests
both directions (§5), 3 regression locks for the 2 rejected fixes (§6),
2 performance tests (§7), 2 persistent-state tests (§8).

## Regression

Core suite (`test_contextual_reference_robustness.py` + the 10
memory/topic/reference/temporal/semantic-bridging/entity-continuity
suites already used by Sprints 43-45): **610 passed, 0 failed.**

Full repository sweep (92 collectible files, `pytest -n 4`, 2
pre-existing uncollectible files excluded - `test_main_bargein.py`,
`test_root_main_bargein.py`): **2784 passed, 15 failed.** Of the 15: 10
are byte-for-byte identical to the standing, already-documented baseline
(6x `test_mic_device_index.py`, 1x `test_production_launcher.py`, 2x
`test_real_adapters.py`, 1x `test_state_isolation.py`). The other 5
(`test_llm_tts_streaming_production.py::test_14_cancellation_during_
synthesis`, `test_streaming_e2e.py::test_A_normal_stream_chunk_before_
llm_finished_and_chat_response_complete`, `test_streaming_speech_
integration.py::test_21_voice_chunks_are_incremental_not_one_giant_
block`, `test_verification_dashboard.py::test_api_verification_reports_
a_successful_verified_action_end_to_end`, `test_voice_pipeline_latency.
py::test_E_default_path_pipelining_synth_overlaps_playback`) were
re-run in isolation (serial, not under `-n 4`) and **all 5 passed
cleanly** - confirmed parallel-execution timing contention, not a
regression from this sprint's changes (none of these files or the
subsystems they test - TTS streaming, voice pipeline latency,
verification dashboard - were touched by any Sprint 46 edit).

## Performance

Measured directly (1000-iteration average, `tests/test_contextual_
reference_robustness.py` tests 31-32): `_normalize_terms_for_bridging()`
well under 5ms/call; `is_active_topic_relevant_to_query()` well under
5ms/call. No network calls, no model inference, no embeddings.

## Persistent state

Only `luno/memory_context.py` (source) and `tests/test_contextual_
reference_robustness.py` (new test file) were modified/created this
sprint. `config/*.json` (14 of 15 top-level files) confirmed unmodified
by mtime (all predate this session). `config/relationship_state.json`
is actively rewritten during any test run by its own pre-existing,
unrelated subsystem (confirmed unrelated to topic/reference resolution -
this behavior predates Sprint 46 and is not part of the memory_context.py
architecture). `_active_topic`/`_topic_history` confirmed to remain
plain, non-persistent, in-memory `dict`s (`test_contextual_reference_
robustness.py` test 34). No new module-level mutable global state was
introduced (test 33).

## Files changed

- `luno/memory_context.py` - 3 additive fixes (see above), plus
  in-place comments documenting the 2 investigated-and-rejected fix
  attempts at their exact code locations.
- `tests/test_contextual_reference_robustness.py` - new, 35 tests.
- `docs/change_impact/contextual_reference_robustness.md` - new (this
  file).
- `ARCHITECTURE_GUARD.md` - new §46.
- `docs/testing/regression_baseline.md` - new Sprint 46 section.
- `docs/project_handover.md`, `docs/project_handover.json` - updated.

No changes to `_rank_key()`, `_apply_budget()`, `assemble_context()`'s
parameter list, `ActiveTopicSnapshot`'s field set, `update_active_
topic()`, `update_topic_history()`, `select_topic_candidates()`,
`select_temporal_fallback_candidate()`'s own eligibility table (beyond
what's documented as a known limitation, not attempted), `_TOKEN_
SYNONYM_GROUPS`/`_TOKEN_SYNONYM_PHRASES` (zero new synonym groups), TTS,
streaming, voice selection, cancellation semantics, or response/memory
ranking.
