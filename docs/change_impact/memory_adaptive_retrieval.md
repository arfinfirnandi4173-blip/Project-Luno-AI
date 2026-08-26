# Change Impact Analysis - Memory Decision Quality & Adaptive Retrieval

## 0. What this sprint is, and its unusual history

This sprint adds one genuinely new capability - CONTEXT-SPECIFIC memory
evidence (a memory can be "reliably useful when the conversation is
about GPUs" without that fact being visible in any global score) - and
folds it, plus the existing global `evaluation_score`, into
`ContextItem._rank_key()` as low-priority, relevance-subordinate
ranking signals.

Implementation began, was interrupted mid-way (Phase 4 of the original
plan) by an unrelated incident - an ad-hoc smoke-test script accidentally
overwrote production `config/long_term_memory.json` - and the sprint was
explicitly paused while that incident was disclosed, audited, and
recovered from (`docs/change_impact/memory_recovery.md`). This document
covers the RESUMED sprint: the pre-pause implementation was inspected,
verified against this document's own architecture audit, test-covered,
and documented - not redesigned or rewritten from scratch, per the
resume instruction's own Phase 1 ("do not redesign the entire memory
architecture").

## 1. Architecture audit (Phase 2) - confirmed decision order

Traced `luno/memory.py`, `luno/memory_context.py`,
`luno/memory_retrieval/*`, `main_runtime_demo.py`, the maintenance
planner, evaluation/self-calibration, outcome telemetry, usefulness,
importance, lifecycle, conflict resolution, `VerifiedFactStore`, and
Episodic Memory. Confirmed decision order, enforced across two stages:

1. **Relevance** - `MemoryRetriever`/`make_manual_memory_source()`
   decide what is even a candidate this turn; `ContextItem.relevance`
   (from `RelevantMemory.score`) is compared FIRST, always, in
   `_rank_key()`.
2. **Lifecycle** - archived manual memories never become candidates
   (`compute_lifecycle(m) != "archived"` filters, both in
   `make_manual_memory_source()` and `_manual_memory_conflict_items()`).
   Not re-decided by ranking.
3. **Conflict handling** - ambiguous conflict groups are merged into one
   hedged `ContextItem` upstream, in `assemble_context()`/
   `_manual_memory_conflict_items()`, before ranking ever runs.
4. **Importance**
5. **Context-specific evidence** (NEW this sprint)
6. **Usefulness, then evaluation** (evaluation newly added this sprint)
7. **Usage-count tie-breaker**
8. **Source priority**, then **budget** (`_apply_budget()`, downstream of
   sorting, unchanged)

No second ranking engine, tokenizer, importance scale, or memory store
was created. No duplicate of evaluation/usefulness/outcome telemetry
exists - every new mechanism reuses an existing accessor or computation.

## 2. Old ranking contract vs. new ranking contract

**Old** (Memory Evaluation & Self-Calibration sprint, and every sprint
before it): `ContextItem._rank_key()` returned
`(relevance, importance, usefulness, priority)`. `evaluation_score` was
explicitly, deliberately kept OUT of ranking - "transient metadata only,
never a ranking signal" - and `tests/test_memory_evaluation.py` encoded
this as a hard structural invariant
(`test_context_item_has_no_evaluation_field_at_all`,
`test_rank_key_source_never_reads_evaluation`).

**New** (this sprint, approved and confirmed twice by Vinn - once in the
original 14-phase sprint spec, once again explicitly in the resume
message): `ContextItem._rank_key()` returns `(relevance, importance,
context_evidence, usefulness, evaluation, usage_count, priority)`.
`evaluation` and `context_evidence` are both new, both `Optional`
(default `None` -> contributes `0.0`, so any source/caller untouched by
this sprint ranks EXACTLY as it did before), and both sit STRICTLY AFTER
importance and (for evaluation) after usefulness - so they can only ever
break a tie among items that already share every stronger-priority
value.

**Why the old test invariant was superseded, not just deleted:** the old
test was correct FOR ITS OWN sprint's scope - at that time,
`evaluation_score` genuinely had no ranking role, and the test protected
that. This sprint's own, later-approved specification deliberately
changes that scope. Per Strict Rule #15 ("if the sprint specification
and an existing test conflict, determine the intended contract first,
then update the test with an explicit documented reason"), the two
tests were REWRITTEN, not deleted or silently weakened:

- `test_context_item_has_no_evaluation_field_at_all` ->
  `test_context_item_evaluation_field_holds_the_shared_evaluate_memory_score`
  - now asserts `evaluation` DOES exist as a field, reusing
    `evaluate_memory()`'s own score, and that no SECOND,
    parallel evaluation-shaped field (`evaluation_confidence`,
    `evaluation_score`) was ever added - the part of the old invariant
    that Adaptive Retrieval does NOT change (confidence still has no
    ranking role at all).
- `test_rank_key_source_never_reads_evaluation` ->
  `test_rank_key_reads_evaluation_only_as_a_low_priority_tiebreaker` -
  now asserts, via a direct structural check of the tuple itself (not
  just a source-text search), that `evaluation` sits strictly AFTER
  relevance/importance/context_evidence/usefulness in the tuple.

Two further tests in the same file (`test_irrelevant_memory_...`,
`test_importance_still_outranks_usefulness_...`) had stale DOCSTRINGS
(claiming "evaluation is not a ranking signal at all" / "tuple order is
completely unchanged") even though their numeric assertions still
passed. Both were renamed and strengthened - the irrelevant-memory test
now ACTIVELY sets evaluation to 1.0 on the low-relevance item and 0.0 on
the high-relevance item (rather than relying on the field being absent)
to prove the guarantee holds even under active pressure to be rescued.

## 3. Relevance-first guarantee (Phase 3) - proven, not assumed

"An irrelevant memory must never become relevant merely because
importance/usefulness/evaluation/retrieval frequency/feedback is high"
is proven at three levels:

1. **Tuple position** - `tests/test_memory_evaluation.py`'s
   `test_irrelevant_memory_cannot_be_rescued_by_high_evaluation`, direct
   `ContextItem` construction.
2. **Real budget pressure** - `tests/test_memory_adaptive_retrieval.py`
   Sections A/B/C: a low-relevance candidate with maximal importance /
   usefulness / evaluation (built via 5 real `record_outcome_evidence()`
   calls, not a hand-set field) loses to a high-relevance, low-signal
   candidate under `max_results=1`, through the real `assemble_context()`.
3. **Real production bridge** - `tests/test_runtime_demo.py`'s
   `test_memory_decision_quality_adaptive_retrieval_end_to_end_relevance_gate_scenario_b`:
   a saved memory with 5 rounds of positive context evidence and
   importance=5 never appears in the rendered LLM system prompt for an
   unrelated query.

## 4. Evaluation integration (Phase 4)

Per the resume message's explicit confirmation, evaluation was
integrated into the EXISTING ranking mechanism (`_rank_key()`), reusing
the EXISTING `evaluation_score` (`evaluate_memory(raw)["score"]` -
`_evaluation_for_relevant_memory()` in `memory_context.py`, computed
once per candidate, never a second evaluation formula). It remains
separate from truth (evaluation still never mutates memory text/
history/importance - only ever read, never written, by anything in this
sprint), separate from Verified Facts (§6), and separate from importance
(a distinct tuple position, distinct accessor, never blended). It cannot
override relevance (§3). The existing bounded `evaluate_memory()`
behavior (0.0-1.0 score, evidence-based, deterministic) is unchanged -
this sprint only adds a NEW READ SITE for its output.

## 5. Context-specific evidence (new mechanism, justified per Phase 3's
"if a new field is genuinely necessary, justify it first")

None of the 13 pre-existing signals (`retrieval_success_count`,
`positive_feedback_count`, `usefulness_score`, `evaluation_score`, etc.)
can represent "useful when the conversation is about X, not when it's
about Y" - they are all single global scalars. `context_evidence` closes
that specific gap:

- **Storage:** `{category: {"positive": int, "negative": int}}` on the
  manual-memory entry, bounded to the 6 fixed `MANUAL_MEMORY_CATEGORIES`
  values - never unbounded, never a free-text key.
- **Score:** `get_context_evidence_score(entry, category)` - pure,
  deterministic, ALWAYS recomputed fresh from the counters (never itself
  persisted) - same "persist evidence, derive score on demand"
  philosophy `evaluate_memory()` already established, specifically to
  avoid creating a second, competing "memory quality" field.
- **Category source:** `classify_query_context_category(text)` - a thin
  wrapper reusing the SAME deterministic keyword classifier every manual
  memory's own `category` is already computed from
  (`_classify_memory_category()`), applied to the current query text
  instead. Deliberately NOT a second tokenizer/embeddings/LLM
  classifier. Known limitation: coarse-grained - many turns fall into
  the catch-all `"other"` category, and a memory's own `category` and
  the query's classified category are independent dimensions (not
  required to match, by design).
- **Attribution:** `PlannerBridgeModule._session_feedback_context`
  captures the SURFACING turn's query category at the moment a memory is
  retrieved, so a LATER confirmation/correction turn's own (often
  near-meaningless) text never gets misclassified and misattributed -
  mirrors `_session_feedback_target`'s exact bounded/reset/pop lifecycle.
  Proven end-to-end in
  `tests/test_runtime_demo.py::test_memory_decision_quality_adaptive_retrieval_end_to_end_context_evidence_scenario_a`,
  which explicitly checks that `"other"` (what `"iya benar"` alone would
  classify to) is NOT what gets credited.

## 6. Usefulness / importance / lifecycle / conflict / budget interaction

- **Usefulness** - unchanged mechanism (`_get_usefulness()`), now one
  tuple position later than context_evidence; both remain strictly
  subordinate to importance.
- **Importance** - unchanged mechanism, still tuple position 1 (right
  after relevance), still outranks every signal this sprint adds -
  proven in `test_E_full_tier_order_...` and
  `test_importance_still_outranks_usefulness_and_evaluation`.
- **Lifecycle** - unchanged; archived entries still never become
  candidates via either the generic adapter path or the conflict-group
  path (`tests/test_memory_adaptive_retrieval.py::test_F_...`).
- **Conflict handling** - unchanged; ambiguous conflicts still always
  render as ONE merged, hedged note (never arbitrated), now carrying
  "best of group" values for the two new signals too, following the
  exact convention `best_importance`/`best_usefulness` already
  established (`tests/test_memory_adaptive_retrieval.py::test_G_...`).
- **Budget** - unchanged mechanism (`_apply_budget()`, `max_results`/
  `max_tokens` from `MemoryRetrievalConfig`); this sprint's new signals
  only affect WHICH items survive budget trimming, never the trimming
  logic itself (`test_K_...`).

## 7. Adaptive behavior safety properties (Phase 5)

Every property required by the sprint spec, and where it's proven:

| property | how it's guaranteed |
|---|---|
| deterministic | `get_context_evidence_score()`/`evaluate_memory()` are pure functions of stored state; `test_L_M_repeated_identical_inputs_produce_identical_ranking` runs the same inputs 3x and asserts identical output order |
| bounded | context_evidence deltas are ±0.12/event, symmetric, clamped to `[MEMORY_USEFULNESS_MIN, MEMORY_USEFULNESS_MAX]`; counters capped at `_MAX_RETRIEVAL_COUNT`; category keys bounded to 6 |
| explainable | `get_memory_selection_explanation()` extended with a "Context-specific evidence" section; `context_specialization` dashboard field |
| relevance-gated | §3 above |
| persistence-safe | reuses the existing, hardened `_save()`/backup/atomic-write mechanism (Memory Recovery sprint); no new persistence path |
| reversible | evidence counters, never memory text/history - nothing here is a one-way mutation of content |
| testable | 18 dedicated scenarios (§9) plus 2 E2E scenarios |
| no LLM judge | `classify_query_context_category()` is the existing keyword classifier, not an LLM call |
| no retrieval-time mutation | `test_N_assemble_context_never_mutates_manual_memory_entries` |
| no persistence drift from retrieval | `test_O_assemble_context_never_triggers_a_save` (monkeypatches `memory._save` to raise if called) |

## 8. Dashboard (Phase 8)

The original 14-phase sprint spec explicitly required Adaptive Retrieval
observability (its own Phase 9). One new, read-only panel was added to
the EXISTING Memory Dashboard (no second dashboard page):

- `collect_memory_detail()` gained a `context_specialization` field -
  thin passthrough of the already-implemented
  `get_memory_context_specialization_summary()`.
- New collector `collect_memory_context_leaderboard()`, new route
  `GET /api/memory/context_leaderboard` (`category`/`order`/`limit`
  query params, bounded to 100 rows, same discipline as
  `/api/memory/list`) - passthrough of the already-implemented
  `list_context_specialized_memories()`.
- Explicitly labeled and tested as EVIDENCE, never truth: a high
  `context_score` is never presented as a factual-correctness claim, and
  is kept visually/structurally distinct from `evaluation`/`usefulness`
  (both already-existing, already-labeled-as-evidence fields on the same
  detail view).

## 9. Tests added/changed

- **New:** `tests/test_memory_adaptive_retrieval.py` - 18 scenarios,
  lettered A-Q per the sprint's own checklist (irrelevant-excluded-under-
  budget x3, adaptive ranking among relevant candidates, full tier-order
  proof, lifecycle/conflict/historical/Verified-Facts/Episodic-Memory/
  budget/determinism/no-mutation/no-persistence-drift/backward-
  compatibility/legacy-defaults).
- **New:** 2 E2E scenarios in `tests/test_runtime_demo.py`, through the
  real `PlannerBridgeModule` - context-evidence attribution round-trip,
  and the relevance gate under real importance/context-evidence
  pressure.
- **New:** 2 scenarios in `tests/test_memory_dashboard.py` - detail-view
  context specialization, leaderboard ranking/bounding/unknown-category
  handling.
- **Changed (rewritten, documented, per Strict Rule #15):** 4 tests in
  `tests/test_memory_evaluation.py`'s Section F (§2 above).
- **Unchanged, still green, confirming isolation:**
  `test_verified_facts_remain_separate_from_evaluation`,
  `test_assemble_context_still_gates_on_relevance_before_anything_else`,
  and every test in `tests/test_memory_persistence_hardening.py`.

## 10. Backward compatibility

Every new parameter is additive and optional:
`relevant_memory_to_context_item(rm, query_category=None)`,
`_manual_memory_conflict_items(query, get_memories, query_category=None)`,
`record_outcome_evidence(memory_id, outcome, context_category=None)`,
`build_turn_trace(..., query_text=None)`,
`_update_session_feedback_target(..., query_text=None)`. A caller that
doesn't know about `query_category` gets `context_evidence=None` on
every item (contributes `0.0`, i.e. behaves exactly as before this
sprint) - proven in `test_P_relevant_memory_to_context_item_without_query_category_still_works`
and `test_P_assemble_context_without_manual_memories_or_verified_facts_still_works`.
A pre-sprint entry with no `context_evidence`/`evaluation_score` keys at
all gets neutral defaults and ranks/renders normally -
`test_Q_legacy_entry_with_no_adaptive_fields_gets_neutral_defaults`.

## 11. Regression & persistent-state verification

Full sweep across all 1353 collected tests (49 files, 2 known-
uncollectible INFRASTRUCTURE files excluded): 1344 passed, 9 failed, all
9 pre-existing/environment-specific (network-blocked health checks,
`list_microphones.py`/`legacy_main.py` absent from this checkout,
`speech_recognition`/`sounddevice` unavailable) - zero new regressions.
Full numbers, per-suite breakdown, and persistent-state SHA256/mtime
verification (all 6 other tracked `config/*.json` files byte-identical
before/after; `long_term_memory.json` unchanged at the post-recovery
hash throughout): `docs/testing/regression_baseline.md`'s "Memory
Decision Quality & Adaptive Retrieval" section.

## 12. Known limitations / technical debt

- `classify_query_context_category()` is coarse (6 categories, many
  turns fall into `"other"`) - a genuine limitation, not a bug; a future
  sprint that wants finer-grained context would need to design a new
  taxonomy without violating the "no second tokenizer" constraint.
- Context-specific evidence is scoped to Manual Memory only (same
  scoping every usefulness/evaluation signal already follows) - Vision/
  Episodic Memory/planner-state sources have no context-evidence concept
  and never will under the current architecture.
- The dashboard leaderboard scans all `_memories` in Python
  (`list_context_specialized_memories()`) - fine at current scale
  (single-digit-to-low-hundreds of manual memories), would need
  indexing if that scale changed by orders of magnitude - not a concern
  raised or required by this sprint.
- Explicit-command feedback (`_handle_explicit_memory_command()`'s
  mark-useful/mark-not-useful) deliberately does NOT record
  context-specific evidence (no `context_category` passed) - these
  commands aren't tied to a specific turn's retrieval context, an
  intentional scope decision, not an oversight.
