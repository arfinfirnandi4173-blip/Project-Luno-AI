# Entity & Concept Continuity (Sprint 44)

## Goal

Improve Luno's conversational memory so it can maintain continuity of the
same entity/concept across turns even when the user's own wording
changes, without an ever-growing synonym dictionary, embeddings, an LLM
judge, or a second ranking system. This is a production-code task:
nothing was implemented before reconnaissance (Phase 0, read-only) and
live reproduction of the exact failures through the real
`RuntimeDemoConsole` path (Phase 1).

## Root cause

Phase 0 (read-only reconnaissance of `luno/memory.py`, `luno/memory_
context.py`, `main_runtime_demo.py`, and every prior sprint's own
change-impact docs) confirmed `ActiveTopicSnapshot` is, and has always
been, a flat, unstructured bag-of-terms - no entity/attribute/relation
distinction exists anywhere in the codebase. Phase 1 reproduced all 10
named scenarios (A-J) via the real `RuntimeDemoConsole` E2E harness
(dynamically-loaded module instances backed by `MockOpenRouterClient`,
never the plain helper-function-only style of testing). Phase 2 then
isolated exactly where correct information first disappeared, finding
the flat representation itself was NOT the problem - three narrower,
more specific defects were:

1. **Entity-identity erosion.** A turn classified `"unknown"` by the
   existing, unmodified `luno.memory.classify_reference_type()` (a
   deliberate, already-tested precedent - see `tests/test_conversation_
   reference_resolution.py::test_13_adversarial_phrase_matrix`'s
   `_ADVERSARIAL_PHRASES`, which explicitly asserts `("kalau buat
   ESP32-S3?", "unknown")` and `("kalau koneksinya?", "unknown")` as a
   deliberate "don't guess" default, not a gap) was still treated as
   REPLACE-worthy by the pre-existing merge/replace decision whenever it
   carried its own sparse (<=1 real, stopword-filtered token) content.
   Live reproduction: Scenario A ("ESP32 pakai INMP441." -> "Mic-nya
   bagusnya gimana?" -> "Kalau koneksinya?"), extended to a 4th turn
   ("Terus?"), showed the active snapshot had been silently REPLACED by
   turn 3's own sparse content by the time turn 4 arrived, losing
   "inmp441"/"mic" (only "esp32"/"i2s" happened to survive because the
   mocked reply text repeated them).

2. **An overly strict Sprint-43 guard.** `is_active_topic_relevant_to_
   query()`'s `active_score == 0` branch (added in Sprint 43, Semantic
   Context Bridging) unconditionally returned `False` whenever a query
   had zero raw or normalized-synonym overlap with the active topic -
   correct for a rich, multi-signal turn pointing elsewhere, but too
   strict for a genuine single-word elliptical attribute question in a
   low-ambiguity, single-topic conversation. Live reproduction: Scenario
   D ("Aquascape-ku pakai pompa kecil." -> "Filternya gimana?") -
   "filter" has no lexical or synonym relation to "aquascape"/"pompa"
   this project's own bounded synonym table could ever be expected to
   cover (and per this sprint's own explicit constraint, should not try
   to), yet it is the single most plausible target in a conversation
   that has discussed nothing else.

3. **A gating gap, found by Phase 7's cross-topic adversarial testing.**
   `main_runtime_demo.py`'s single-slot recency branch only ever
   consulted the Sprint-43 guard when `reference_type == "comparison"`.
   Live reproduction: three unrelated topics established in the same
   conversation (ESP32/INMP441, aquascape/pompa, GPU/RTX3060), then
   "Yang wireless?" (classified `attribute_reference`, not
   `comparison`) - bypassed the guard entirely and unconditionally
   trusted recency, injecting the currently-active (by then a merged
   blob of mic+GPU terms) topic despite "wireless" having zero grounding
   anywhere in the conversation.

None of the three originates in ranking, budget, or `assemble_context()`
- confirmed by direct code reading (Phase 0) and by the fact that every
fix below operates strictly upstream of candidate selection, in the
merge-decision and relevance-guard layers.

## Before / after

**Before:** a chain of short follow-ups touching the same entity via a
sparse, unmarked fragment ("Kalau koneksinya?") could silently lose the
entity's own established terms one or two turns later. A single-word
elliptical attribute question in a conversation that had only ever
discussed one topic ("Filternya gimana?") was refused even though there
was nothing else it could plausibly mean. An ungrounded single-word
question asked inside a demonstrably multi-topic conversation ("Yang
wireless?") was answered by blindly trusting whichever topic happened to
be most recently active, regardless of whether the word had ever been
mentioned.

**After:** the entity's own terms survive a sparse follow-up via a
narrow, additive merge trigger. A genuine single-word elliptical
question resolves correctly when there is truly nothing else it could
mean (exactly one topic ever discussed, or no genuinely distinct
competing topic). The same single-word question is correctly refused
once the conversation is demonstrably juggling two or more distinct,
unrelated topics, rather than guessing which one a totally ungrounded
word belongs to.

## Entity/concept model

**No new representation was introduced.** Phase 3-4 explicitly
evaluated whether the reproduced gaps required a richer entity/concept
structure (canonical identity, observed terms, attributes, actions/
relations, source turn, recency, confidence - the brief's own candidate
field list) and found they did not: both defects were merge-decision and
relevance-guard bugs operating on the EXISTING flat bag-of-terms, not
representation gaps. `ActiveTopicSnapshot`'s field set is unchanged from
Sprint 40 (`terms`, `turns_since_active`, `list_items`, `status`,
`source_sentence` - verified via `tests/test_entity_concept_continuity.
py::test_79_no_new_entity_dataclass_introduced`).

## Resolution rules

This sprint did not add a new resolution mechanism; it repaired two
existing links in the priority chain the codebase already implements
(explicit reference -> `ConversationReference` -> Sprint 43's lexical/
semantic bridge -> shared attributes/topic-history overlap -> recency
only when ambiguity is low), specifically the last tier:

- `is_sparse_unknown_followup(text)` (new, `luno/memory_context.py`):
  `True` only when `luno.memory.classify_reference_type(text) ==
  "unknown"` AND the stopword-filtered (`_TOPIC_OVERLAP_STOPWORDS`) real
  token count is exactly 1. Consulted only by `main_runtime_demo.py`'s
  `is_merge` computation:
  `is_merge = memory.is_merge_reference_followup(user_text) or
  memory_context.is_sparse_unknown_followup(user_text)`
  Never changes `classify_reference_type()`'s own output; never touches
  `is_pure_reference_followup()`/`is_merge_reference_followup()`
  themselves.

- `is_active_topic_relevant_to_query()`'s `active_score == 0` branch
  (extended, `luno/memory_context.py`): now trusts recency for exactly
  ONE real query token when both hold: (a) no OTHER genuinely distinct
  `topic_history` entry has lexical or normalized overlap with that
  token (same "genuinely distinct" majority-coverage lineage skip Sprint
  43 already established, reused unchanged), AND (b) fewer than 2 OTHER
  genuinely distinct topics are live in the bounded history at all. (b)
  is this sprint's own addition (Phase 7) - recency-as-last-resort is
  safe in a single-topic conversation (nothing else it could mean) but
  not inside a demonstrably multi-topic one, even when no single other
  topic happens to lexically conflict.

- `main_runtime_demo.py`'s single-slot recency branch guard gate:
  widened from `reference_type != "comparison"` to `reference_type not
  in ("comparison", "attribute_reference")` - `attribute_reference`
  turns carry their own real residual content (the matched descriptive
  word) exactly like `comparison` turns do, so the same relevance
  question Sprint 43 asked of `comparison` applies here too.

Priority order is otherwise exactly as before this sprint (explicit
reference first, ConversationReference, lexical/semantic bridge, topic-
history overlap, recency last and only when ambiguity is low - never
recency alone to fabricate an entity). When two candidates are equally
plausible, the system still returns unresolved/ambiguous rather than
guessing (unchanged, pre-existing `select_topic_candidates()` behavior
at the normalized-evidence tier; the raw-overlap tier surfaces every
matching entry instead of guessing one, a different but equally safe
non-fabrication strategy - see `test_58_raw_token_tie_surfaces_both_
rather_than_guessing`).

## Ambiguity policy

Unchanged in spirit, extended in coverage. A query with more than one
real residual token never qualifies for the low-ambiguity fallback
(Sprint 43's own precedent, re-verified: "Kalau upgrade PC-ku gimana ya,
mumpung ada budget?" is still correctly excluded). A single real token
that has zero grounding anywhere in a demonstrably multi-topic
conversation is now correctly refused rather than defaulted to recency
(Phase 7's own fix). A tie between two OR more equally-plausible topics
returns nothing at the normalized-evidence tier, or surfaces all tied
entries at the raw-overlap tier (both are "do not silently pick a single
wrong answer," implemented via different, both pre-existing, mechanisms).

## Multi-topic safety

Verified via a dedicated Phase 7 adversarial reproduction: three
unrelated topics (ESP32/INMP441, aquascape/pompa, GPU/RTX3060)
established in sequence in the same conversation, then five follow-up
questions:

- "Mic-nya gimana?" -> resolves only to the ESP32/INMP441 topic.
- "Pompa yang tadi?" -> resolves only to the aquascape/pompa topic.
- "GPU-nya?" -> resolves only to the GPU/RTX3060 topic.
- "Yang wireless?" -> retrieves nothing (no grounding anywhere in the
  conversation for "wireless" - the exact scenario the Phase 7 gating
  fix above addresses).
- "Besok hujan nggak?" -> retrieves nothing (completely unrelated
  domain), regardless of how many topics are live in history.

All five are locked in as real `RuntimeDemoConsole` E2E tests
(`test_73` through `test_77` in `tests/test_entity_concept_continuity.
py`). Entity continuity never becomes a justification for injecting the
most recent topic into a question that has no evidence connecting it to
that topic.

## Performance

Measured directly (2000 iterations, 3-entry bounded topic history):
`is_active_topic_relevant_to_query()` ~0.068ms/call, `is_sparse_unknown_
followup()` ~0.014ms/call. Both well under the 5ms/turn target. No
network calls, no model inference, no embeddings - both functions are
pure, deterministic, and operate only on already-computed token sets
(structurally verified: `test_81_is_sparse_unknown_followup_never_
imports_ml_or_network_libs`).

## Persistent state

`_active_topic` and `_topic_history` remain plain, bounded, in-memory
`Dict` attributes on `PlannerBridgeModule` (`main_runtime_demo.py`) -
every reference to either was grepped and confirmed to never touch any
file I/O path. No new persistent state, no raw conversation persistence,
no global entity state was introduced. `config/*.json` (top-level, 15
files) confirmed present with no unexpected structural changes; this
sprint's probes and test suite ran entirely through dynamically-loaded,
isolated `RuntimeDemoConsole` instances backed by `MockOpenRouterClient`,
never touching the real persistent config files.

## Test results

New: `tests/test_entity_concept_continuity.py` - **72 passed, 0
failed**. Breakdown: unit tests for `is_sparse_unknown_followup()` (10),
the `"buat"` stopword parity fix (4), `is_active_topic_relevant_to_
query()`'s new fallback tier (10), `is_merge` integration (4),
exact/attribute-reference continuity (4), adversarial precedent
preservation (4), multi-topic isolation unit tests (5), temporal
interaction (2), bounded-memory behavior (4), performance (2), 19 E2E
scenarios via real `RuntimeDemoConsole` (10 named Scenarios A-J, 3
extended multi-turn chains, 5 Phase 7 cross-topic adversarial tests,
cross-conversation isolation, the documented known-limitation lock-in),
and structural/anti-scope-creep invariants (3).

## Regression

Full memory/topic/reference/temporal/semantic-bridging suite most
directly exercising the changed code path: **500 passed, 0 failed**
(`tests/test_entity_concept_continuity.py`, `test_conversation_
reference_resolution.py`, `test_conversation_intelligence.py`, `test_
memory_continuity.py`, `test_memory_comparison_topic_preservation.py`,
`test_memory_topic_retention.py`, `test_temporal_memory_timeline_
awareness.py`, `test_cross_system_conversation_consistency.py`, `test_
semantic_context_bridging.py`, `test_memory_retrieval_decision_quality_
reaudit.py`). Remaining repository (89 files, excluding the 2
pre-existing uncollectible `test_main_bargein.py`/`test_root_main_
bargein.py`) swept in file-group batches, `test_llm_tts_streaming_
production.py` run with `pytest -n 4` per the standing precedent for that
file - zero new failures. The only failures encountered (6x `test_mic_
device_index.py`, 1x `test_production_launcher.py::test_07_health_
checks_all_pass_in_default_mock_configuration`, 2x `test_real_adapters.
py`, 1x `test_state_isolation.py::test_isolate_persistent_state_drains_
stragglers_before_monkeypatch_reverts`) are all identical to the
standing, already-documented baseline (§15 and every prior sprint's own
regression entry), independently confirmed unrelated to any file this
sprint touched.

## Known limitations

A bare, unmarked declarative continuation whose own anaphoric "-nya"
marker attaches to a two-word compound noun ("LED strip-nya 430.",
"Power supply-nya?" - no "kalau"/"yang"/"gimana" marker at all) is
classified `"unknown"` and carries 2 real residual tokens, so it does not
qualify for either fix in this sprint (both require exactly 1 real
token, a deliberately conservative bound preserved from Sprint 43's own
regression precedent). It therefore REPLACES the active topic rather
than merging.

This was investigated and NOT fixed. The only general, cross-domain,
non-hardcoded signal available - a token ending in "-nya" near the
sentence's own subject position - is also how extremely common
Indonesian discourse connectives are formed: "soalnya" (because),
"katanya" (apparently/they say), "sepertinya" (it seems), "akhirnya"
(finally), "biasanya" (usually). None of these mark an anaphoric
reference to a prior entity at all. A heuristic broad enough to catch
"LED strip-nya"/"Power supply-nya" would also fire on ordinary sentences
using those connectives, reintroducing exactly the kind of ungrounded-
recency fabrication this sprint's own ambiguity-safety requirement
forbids. Locked in and documented, not silently dropped: `tests/test_
entity_concept_continuity.py::test_82_known_limitation_bare_compound_
noun_nya_statement_replaces`. The same underlying entity DOES connect
correctly once the turn is legibly marked (adding a "gimana" comparison
marker: `test_64_scenario_E_iot_chain_via_recognized_gimana_shape`).

Per this sprint's own explicit instruction, no other proposed scenario
was modified merely to grow scope; Scenarios A/B/C/F/G/H/I/J from the
original brief, and the other 8 of the Phase 7 cross-topic properties,
were confirmed already correctly handled by the existing, pre-Sprint-44
architecture (topic-history overlap matching, the raw-token-tie
"surface everything" strategy, and the unrelated-query zero-injection
path) and are locked in as regression tests rather than re-implemented.

## Invariants preserved

No embeddings, no LLM judge, no second ranking system, no growth of the
bounded synonym table (`_TOKEN_SYNONYM_GROUPS`/`_TOKEN_SYNONYM_PHRASES`
untouched this sprint). `_rank_key()`, `_apply_budget()`, `assemble_
context()`'s parameter list, `ActiveTopicSnapshot`'s field set,
`classify_reference_type()`, `is_pure_reference_followup()`, `is_merge_
reference_followup()`, `update_active_topic()`, `update_topic_history()`
all unchanged. `_TOPIC_HISTORY_MAX_ENTRIES` (8) and `_ACTIVE_TOPIC_MAX_
TERMS` (20) unchanged - topic history and per-topic term budgets remain
exactly as bounded as before. Sprint 36 ALL/SHORT voice behavior,
streaming/cancellation behavior, TTS, and voice selection were not
touched and have no code-path overlap with either file this sprint
modified. Cross-topic isolation and cross-conversation isolation both
re-verified via dedicated tests (`test_73`-`test_77`, `test_78`).

## Files modified

- `luno/memory_context.py` - added `"buat"` to `_TOPIC_OVERLAP_
  STOPWORDS`; added the new `is_sparse_unknown_followup()` function;
  extended `is_active_topic_relevant_to_query()`'s existing
  `active_score == 0` branch with the low-ambiguity fallback tier
  (single real token, no genuinely distinct lexical conflict, fewer than
  2 other genuinely distinct topics live).
- `main_runtime_demo.py` - `_on_assistant_response()`'s `is_merge`
  computation gained one additional `or memory_context.is_sparse_
  unknown_followup(user_text)` clause; the single-slot recency branch's
  guard gate widened from `reference_type != "comparison"` to
  `reference_type not in ("comparison", "attribute_reference")`.

## Files created

- `tests/test_entity_concept_continuity.py` (72 tests).
- `docs/change_impact/entity_concept_continuity.md` (this file).
- `ARCHITECTURE_GUARD.md` §44 (appended).
- `docs/testing/regression_baseline.md` Sprint 44 entry (appended).
