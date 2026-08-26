# Change Impact: Memory Retrieval & Decision Quality (Intent Taxonomy + Topic Continuity)

## 1. Problem

A prior, full "LUNO SPRINT: MEMORY RETRIEVAL & DECISION QUALITY" audit
(Phase 0 only, read-only) found that the memory retrieval/ranking
pipeline was already mature: relevance-gated retrieval, importance,
recency, source-priority tie-breaking, deterministic conflict
resolution, cross-source deduplication, budget enforcement, and
context-specific evidence (`context_evidence`, from the earlier "Memory
Decision Quality & Adaptive Retrieval" sprint) were all already
implemented, tested, and in production. Two, and only two, genuine gaps
were confirmed:

1. `classify_query_context_category()` reuses the six
   `MANUAL_MEMORY_CATEGORIES` (a taxonomy built to classify STORED
   MEMORY CONTENT) as a proxy for the CURRENT TURN's intent - too coarse
   to distinguish troubleshooting/planning/casual-conversation/
   continuation-of-topic/explicit-recall/correction-update; most such
   turns collapsed into the catch-all `"other"`.
2. Memory recency (`RelevantMemory.stale`/score decay) is memory
   recency, not TOPIC recency - a turn like "lanjut coding Luno yang
   tadi" had no dedicated signal preferring context related to the
   immediately preceding topic.

This sprint closes exactly those two gaps and nothing else.

## 2. What was already reused (no reimplementation)

- `MemoryRetriever`/`make_manual_memory_source()`/
  `analyze_query()`/`token_overlap()` (`luno/memory_retrieval/*`) -
  completely untouched. Relevance gating still happens entirely upstream
  of anything this sprint added.
- `classify_query_context_category()`, `get_context_evidence_score()`,
  `evaluate_memory()`, `_get_importance()`/`_get_usefulness()`,
  `get_memory_retrieval_count()` (`luno/memory.py`) - completely
  untouched. `context_evidence` still means exactly what it meant
  before this sprint.
- `_classify_conflict()`, `group_ambiguous_conflict_entries()`,
  `_manual_memory_conflict_items()` - completely untouched. Conflicting
  memories still always render as one merged, hedged note; this sprint
  never arbitrates a winner.
- `deduplicate_context_items()`, `_apply_budget()` - completely
  untouched.
- `render_context_block()`'s prompt-injection trust boundary (previous
  sprint) - completely untouched; still wraps the whole assembled block.
- Recall/historical detection: `is_recall_command()`,
  `is_session_recall_command()`, `_is_historical_query()` - reused
  VERBATIM inside the new `classify_query_intent()`. No second recall/
  historical word list was created.
- Correction-language detection: `_CORRECTION_RE` (the same regex
  `_classify_conflict()` itself already keys off) - reused verbatim.
- The technical/project-context keyword tables
  (`_CATEGORY_KEYWORDS["technical_fact"]`/`["project_context"]`) - reused
  as-is for the new intent-preference bonus, not duplicated.
- `_jaccard()`/`_token_set()` (the SAME cross-source-dedup similarity
  primitives) - reused for the topic-continuity bonus, not a new
  similarity metric.
- `PlannerBridgeModule`'s existing per-conversation, bounded, in-memory,
  reset-at-conversation-end dict convention
  (`_session_feedback_target`/`_depth_preference`/`_last_turn_trace`) -
  the new `_last_topic_terms` dict follows this exact, already-proven
  pattern; no new state-management mechanism was invented.

## 3. New intent taxonomy

`luno.memory.classify_query_intent(text)` - a small, deterministic,
additive classifier, SEPARATE from `classify_query_context_category()`
(neither wraps nor changes the other; both remain independent signals).
Always returns one of `QUERY_INTENTS`: `explicit_recall`,
`correction_update`, `continuation_of_topic`, `troubleshooting`,
`planning`, `casual_conversation`, `other`.

Precedence (first match wins, documented in the function's own block
comment):

1. `explicit_recall` - `is_recall_command()` OR
   `is_session_recall_command()` OR `_is_historical_query()`.
2. `correction_update` - `_CORRECTION_RE.search()`.
3. `continuation_of_topic` - a small, conservative marker list
   ("lanjut"/"lanjutkan"/"terusin"/"continue"/"balik ke"/...), matched
   with WORD-BOUNDARY-SAFE regex (`_compile_word_boundary_marker_pattern()`)
   to avoid the false-positive substring collision a naive `in` check
   would produce (e.g. "lanjut" inside "sel-ANJUT-nya", one of this
   module's OWN planning markers - caught and fixed during this sprint's
   own test-writing, see Known limitations).
4. `troubleshooting` - "error"/"gagal"/"rusak"/"bug"/"crash"/"kenapa ga
   bisa"/... markers.
5. `planning` - "rencana"/"roadmap"/"langkah selanjutnya"/"strategi"/...
   markers.
6. `casual_conversation` - a deliberately SMALL list of clear
   small-talk/banter markers ("haha"/"wkwk"/"santai aja"/...) - never
   triggered merely by the ABSENCE of a task-shaped signal.
7. `other` - the safe fallback. Structurally a complete no-op: every
   downstream consumer treats `"other"`/`None` identically to "this
   sprint doesn't exist" (proven by
   `test_G_ambiguous_unknown_falls_back_to_other_existing_default_behavior`/
   `test_G2_none_intent_is_also_a_complete_no_op`).

The classifier is permitted to influence (per the sprint's own explicit
scope) ONLY: a bounded ranking-preference bonus, and source/keyword-based
tie-break behavior - never relevance itself, never `context_evidence`
(left completely untouched - a separate, pre-existing mechanism).

## 4. Continuation-of-topic mechanism

`luno.memory_context.extract_topic_terms(text, limit=8)` - reuses the
EXISTING tokenizer (`analyze_query(text).tokens`), returns a bounded,
deterministic `frozenset` of up to 8 signal tokens. No embeddings, no
second tokenizer, no vector store.

`PlannerBridgeModule._last_topic_terms: Dict[str, frozenset]`
(`main_runtime_demo.py`) - conversation-scoped, bounded at 50 entries
(FIFO eviction, same `_..._max` convention every sibling dict in this
class already uses), popped in `_on_conversation_ended()`. Fully
REPLACED every turn (never appended to) with THIS turn's own topic
terms, read once at the start of the NEXT turn. Never persisted to disk,
never holds raw utterance text - only the bounded token set.

The bonus itself: `_continuity_bonus(item_text, previous_topic_terms)`
in `memory_context.py` = `Jaccard(tokens(item_text), previous_topic_terms)
* 0.25` (capped at 0.25, reached only at perfect overlap). Applied ONLY
when the CURRENT turn's own intent is `continuation_of_topic` - a stale
previous topic never influences a turn the deterministic classifier
itself didn't recognize as a continuation (`apply_continuity` gate in
`_apply_decision_quality_bonus()`).

## 5. One new ranking signal, not two

Both mechanisms (intent-preference bonus and continuity bonus) feed
exactly ONE new, additive `ContextItem.intent_bonus` field
(`Optional[float] = None`, default no-op), computed once per turn in
`_apply_decision_quality_bonus(items, intent, previous_topic_terms)` -
applied AFTER every existing adapter (manual-memory, conflict-merged,
verified-fact) has built its full candidate pool, and BEFORE
`deduplicate_context_items()`/sorting, so a duplicate collision and the
final ranking both see the same, already-bonused value.

`_rank_key()`'s tuple grew by exactly one element:

- **Before:** `(relevance, importance, context_evidence, usefulness,
  evaluation, usage_count, priority)`
- **After:** `(relevance, importance, context_evidence, usefulness,
  evaluation, usage_count, intent_bonus, priority)`

`intent_bonus` sits strictly AFTER `usage_count` and strictly BEFORE
`priority` - the sprint's own required "bounded intent/continuity
preference -> existing source-priority tie-break" ordering. It can only
ever break a tie among items that already share every stronger-priority
`_rank_key()` value; it can never rescue an irrelevant item, and it can
never outrank a real importance/context-evidence/usefulness/evaluation
difference.

**CONTRACT CHANGE**, per this codebase's own established Strict Rule #15
precedent (the exact same handling `evaluation`'s own earlier addition to
this tuple received):
`tests/test_memory_evaluation.py::test_rank_key_reads_evaluation_only_as_a_low_priority_tiebreaker`
asserted an exact 7-element tuple; it now asserts the exact 8-element
tuple, with an added assertion that `self.intent_bonus` is read by
`_rank_key()`. Not deleted, not silently weakened - extended and
documented.

## 6. Bonus magnitudes and what triggers them

| Intent | Trigger | Bonus |
|---|---|---|
| `troubleshooting` | `item.source` in `{vision_memory_events, tool_execution, verified_facts}` OR item text matches the EXISTING `technical_fact` keyword table | **+0.15** |
| `planning` | `item.source == "planner_state"` OR item text matches the EXISTING `project_context` keyword table | **+0.15** |
| `casual_conversation` | item text matches the `technical_fact` OR `project_context` keyword table | **-0.15** (dampener, not exclusion) |
| `continuation_of_topic` | `Jaccard(item tokens, previous_topic_terms)` | **up to +0.25**, scaled by overlap |
| `explicit_recall` | - | **0.0** (reuses the existing recall/historical retrieval path) |
| `correction_update` | - | **0.0** (relies on ordinary relevance already surfacing the fact being corrected) |
| `other` / `None` | - | **0.0** (complete no-op) |

All magnitudes are small relative to a typical importance tier (0-4
integer steps) or a realistic `context_evidence`/`evaluation` swing
(0.0-1.0 floats used as earlier tuple positions) - by construction, and
proven directly (§7).

## 7. Relevance-first guarantee - proven, not assumed

Three levels, mirroring the discipline the "Memory Decision Quality &
Adaptive Retrieval" sprint's own change-impact doc already established
for `evaluation`/`context_evidence`:

1. **Tuple position** - `test_U_rank_key_position_zero_is_still_raw_relevance`
   and `test_U2_relevance_dominates_even_under_maximal_intent_bonus_pressure`
   (a low-relevance item with `intent_bonus=1.0` - far above any bonus
   this sprint can actually produce - still loses to a high-relevance
   item with `intent_bonus=0.0`).
2. **Real ranking under a realistic scenario** - the sprint's own worked
   example, reproduced exactly:
   `test_J_continuation_bonus_never_outranks_higher_relevance_candidate`
   (a highly relevant "ESP32 clap sensor" memory stays ranked above a
   weakly related "Luno coding" memory that DOES match the previous
   topic).
3. **Bounded effect even for a maximally continuation-shaped turn** -
   `test_K_lanjut_alone_has_bounded_effect_never_rescues_irrelevant_item`
   (the single word "lanjut", classified `continuation_of_topic`, against
   an unrelated stored memory - zero token overlap, so the continuity
   contribution is exactly `0.0`, not merely "small").

## 8. Production integration - still exactly-once retrieval

`PlannerBridgeModule._handle_utterance()` (`main_runtime_demo.py`):

1. `relevant_memories_early = self.memory_retriever.retrieve_memories(text)`
   - unchanged, still the ONE retrieval call per turn.
2. **NEW, additive:** `query_intent = memory.classify_query_intent(text)`
   and `previous_topic_terms = self._last_topic_terms.get(key)` (only
   when `query_intent == "continuation_of_topic"`) - both cheap,
   deterministic, no I/O, computed alongside the method's other early
   per-turn reads (own try/except, same "a bug here must never break a
   turn" convention as every other note in this method).
3. `memory_context.assemble_context(..., precomputed_relevant_memories=
   relevant_memories_early, intent=query_intent,
   previous_topic_terms=previous_topic_terms)` - the SAME single call
   site, now with two new optional keyword arguments.
4. **NEW, additive:** near `_update_session_feedback_target()`'s own
   call site, `self._last_topic_terms[key] = memory_context.
   extract_topic_terms(text)` replaces THIS conversation's topic snapshot
   for the NEXT turn.

No new `retrieve_memories()` call, no new LLM/API call, no new
tokenizer, no new ranking system, no change to conflict semantics, no
persistent-schema change, no new persistent state. Proven directly by
`test_T_exactly_one_retrieve_memories_call_per_turn` (monkeypatches
`retrieve_memories` with a call-counter and asserts exactly 1 through a
real turn) and the structural/source-inspection tests (`test_AA`/`test_AB`
below).

## 9. Conversation isolation

`_last_topic_terms` follows the EXACT scoping/bounding/reset convention
every sibling per-conversation dict in `PlannerBridgeModule` already
uses: keyed on `conversation_id` (falling back to
`_ENV_CONFIRMATION_KEY` for a caller that never sets one), bounded at 50
entries with FIFO eviction, popped in `_on_conversation_ended()`.
Proven, not assumed:

- `test_M_conversation_end_resets_topic_continuity` - a direct
  `_on_conversation_ended()` call clears the entry.
- `test_N_two_simultaneous_conversations_remain_isolated` - two REAL,
  concurrent-in-time conversations through the real Event Bus produce
  disjoint topic-term sets; Conversation A's terms never appear in
  Conversation B's.
- `test_AD_continuity_state_cleaned_at_conversation_end_new_conversation_does_not_inherit_old_topic` -
  a brand-new conversation that happens to REUSE an old, already-ended
  conversation_id string gets a completely fresh topic snapshot, never
  the old one.
- `test_E2E_new_conversation_topic_never_leaks_into_prior_conversation_prompt` -
  the REAL rendered system prompt for a different conversation's "lanjut
  yang tadi" never contains the first conversation's topic content.

## 10. Tests

`tests/test_memory_decision_quality.py` (new, 36 scenarios): **36
passed, 0 failed**.

- **Intent classification (A-G² ):** troubleshooting, planning, casual
  conversation, continuation-of-topic, explicit recall (both
  recall-command- and historical-query-shaped), correction/update,
  ambiguous/unknown falling back to `"other"` (and a direct proof that
  `"other"`/`None` are complete ranking no-ops).
- **Continuation (H-N):** the sprint's own worked "lanjut coding Luno
  yang tadi" example; continuity boosting a tied candidate; continuity
  never outranking higher relevance (the ESP32-clap-sensor-vs-Luno-coding
  example); "lanjut" alone producing a bounded/zero effect; a new,
  non-continuation query ignoring a stale previous topic; conversation-end
  reset; cross-conversation isolation.
- **Retrieval quality per intent (O-S):** troubleshooting favoring
  technical facts and event/tool-execution sources; planning favoring
  project context; casual conversation avoiding unrelated technical
  memory; correction/update relying on existing relevance (no new
  mechanism); explicit recall contributing no new signal (source-level
  proof that it delegates to the existing detectors).
- **Invariants (T-AD):** exactly-one retrieval call per turn (real E2E,
  call-counting monkeypatch); `_rank_key()[0]` still raw relevance, even
  under maximal bonus pressure; cross-source dedup unchanged; conflict
  resolution still merges both sides of an ambiguous conflict, never
  picks a winner; memory budget still caps item count; the
  prompt-injection boundary still wraps rendered output with intent
  active; no persistent-state mutation (`memory._save()` monkeypatched to
  raise) and no mutation of underlying manual-memory entries; no second
  tokenizer (source-inspection: `extract_topic_terms()`/
  `_continuity_bonus()` reuse `analyze_query`/`_jaccard`/`_token_set`);
  no LLM/network call anywhere in the new code (source-inspection for
  forbidden tokens); conversation-scoped continuity state is bounded and
  cleaned at conversation end.
- **Real production-path E2E:** two tests through `RuntimeDemoConsole`/
  `PlannerBridgeModule`, inspecting the REAL final system-prompt string -
  one confirming a continuation turn surfaces the previous topic's
  content in the real prompt, one confirming a different conversation's
  topic never leaks into another conversation's real prompt.

## 11. Regression

Combined targeted memory suite (11 files including the new one): **391
passed, 0 failed**. `test_runtime_demo.py`: **78 passed, 0 failed**.
Broader memory-adjacent suites (dashboard/maintenance/learning/outcome
telemetry/manual-memory/episodic/relationship/persistence-hardening):
**366 passed, 0 failed**. TTS/streaming: **72 passed, 0 failed**.
Wake/barge-in: **63 passed, 0 failed**. Full `tests/` tree (12
file-group batches): **1817 passed, 10 failed** - all 10 map exactly to
the already-documented pre-existing baseline (6x mic-device-index, 1x
production-launcher health-check, 2x real-adapters whisper gap, 1x
state-isolation sandbox gap). `python3 -m pytest luno/ -q`: 813 passed,
7 failed on both of two consecutive runs - all 7 in
`test_barge_in.py`/`test_text_normalizer.py`, both suites this sprint
has zero coupling to (confirmed via `grep`) and both pass 62/62
standalone immediately after - a load/contention artifact of the full
`luno/` sweep in this sandbox, not a regression. Full breakdown:
`docs/testing/regression_baseline.md`'s "Memory Retrieval & Decision
Quality" section.

## 12. Persistent-state verification

All 14 present `config/*.json` files SHA256- and mtime-identical before
and after this sprint's entire implementation and full test run. No
stray `.tmp`/`.bak`/`.old`/`.orig` files, no new production memory
files, no new persistent state introduced at all - `_last_topic_terms`
is transient, in-memory, per-process runtime state only.

## 13. Known limitations

- The intent taxonomy is a small, deliberately conservative keyword/regex
  classifier - like `classify_query_context_category()` before it, many
  turns still fall through to `"other"` (a complete no-op, not a
  mis-classification).
- Word-boundary-safe matching was REQUIRED, not optional: a naive
  substring check (`marker in lowered`) produced a real false positive
  during this sprint's own test-writing - "lanjut" (a continuation
  marker) matched inside "selanjutnya" (part of this module's OWN
  "langkah selanjutnya" planning marker), misclassifying a planning turn
  as continuation. Fixed with `\b`-anchored regex
  (`_compile_word_boundary_marker_pattern()`) before this sprint's
  implementation was considered complete - documented here rather than
  silently patched, per this project's own "report failures encountered
  and how resolved" convention.
- `casual_conversation`'s dampener only fires when a candidate's own text
  matches the existing technical/project keyword tables - a casual turn
  that surfaces a technical memory through some OTHER matched signal is
  not excluded, only mildly deprioritized (by design; the brief's own
  wording is "avoid aggressively injecting", not "forbid").
- Topic continuity is a bounded keyword-overlap heuristic (Jaccard
  against an 8-token snapshot of the previous turn), not real topic
  modeling - it can miss a genuine continuation phrased with entirely
  different vocabulary from the original turn. This is an accepted,
  documented limitation, not a defect - the sprint's own hard
  constraints forbid embeddings/vector search for this.
- `context_evidence` (the pre-existing, separate mechanism) was
  deliberately left untouched - the new intent taxonomy does not feed
  into it, per the minimal-footprint STOP-condition discipline this
  sprint operated under.

## 14. Scope / what was explicitly NOT changed

- `MemoryRetriever` ranking/scoring/dedup/limits
  (`luno/memory_retrieval/*`) - untouched.
- `classify_query_context_category()`/`context_evidence` - untouched.
- `_classify_conflict()`/conflict semantics - untouched.
- `deduplicate_context_items()`/`_apply_budget()` - untouched.
- The prompt-injection trust boundary (`render_context_block()`'s
  BEGIN/END markers) - untouched.
- No embeddings, vector search, LLM judge, or network call anywhere in
  the new code.
- No persistent memory schema field added/removed/renamed.
- TTS/streaming/response-depth/adaptive-depth/conversation-ended-race
  systems - untouched (this sprint's only integration point inside
  `PlannerBridgeModule` is additive reads/writes alongside the method's
  existing early-per-turn-read pattern).
