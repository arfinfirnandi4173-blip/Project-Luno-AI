# Change Impact: Cross-System Integration Audit (Sprint 42)

## Goal

This is an AUDIT, not a feature sprint. Its own brief states the goal
directly: ensure the classifier, reference resolution, temporal memory,
topic history, retrieval, prompt assembly, and voice output layers built
across Sprints 37-41 all understand the SAME conversation as the SAME
conversation - especially when several of these features interact at
once, across domains other than ESP8266/ESP32. Explicit constraints: no
LLM judge, no embedding model, no second ranking system; every prior
sprint's invariant must be preserved; nothing may change before root
cause is proven via the real production path (`RuntimeDemoConsole`); the
sprint must not become an exercise in adding features to satisfy a probe.

## Method

**Phase 0 (read-only reconnaissance).** Mapped the full per-turn pipeline
and every relevant piece of state by direct source read - no code
changed. Confirmed: retrieval (`classify_query_intent()`/`classify_
reference_type()`/`needs_topic_context()`/`is_pure_reference_followup()`/
`is_merge_reference_followup()`/the 4-way candidate-selection branch)
always reads PRE-turn `_active_topic`/`_topic_history` state, because
`_on_assistant_response()` only updates that state AFTER the reply is
generated, using its own separately-computed `is_followup`/`is_merge`.
This ordering structurally prevents same-turn self-contamination. The
4-way retrieval branch in `main_runtime_demo.py` (ordinal ->
topic-history overlap -> single-slot recency -> temporal fallback) is a
strict if/elif chain; `resolve_ordinal_targets()` is checked first (most
precise), then `select_topic_candidates()` (Sprint 39, pure lexical
overlap, computed unconditionally regardless of `is_short_followup`),
then the original Sprint 4 single-slot fallback, then Sprint 41's
`select_temporal_fallback_candidate()` (last resort). Only the LAST
branch has an ambiguity-safety residual-token gate
(`_TEMPORAL_FALLBACK_MAX_RESIDUAL_TOKENS`) - `select_topic_candidates()`
has none of its own. `build_dual_response()` (voice output) confirmed
structurally isolated from memory/topic state - it operates purely on
the already-finalized reply text; its signature has no parameter through
which `_active_topic`/`_topic_history` could reach it. `_on_conversation_
ended()` confirmed to pop every relevant per-conversation dict
(`_active_topic`, `_topic_history`, `_last_topic_terms`, `_voice_output_
mode`, plus `_pending_env_confirmations`/`_last_device_target`/etc.),
keyed by `session_id` - a pre-existing, complete, correct contract.

**Phase 1 (cross-system E2E probe matrix).** Built real, production-path
probes (not unit-level shortcuts) through `RuntimeDemoConsole` for all 10
scenarios (A-J) specified in the brief, across 5 domains: PC/GPU
(RTX 3060 Ti/RTX 5070/RX 9070), audio/microphone (INMP441/MAX9814/
SPH0645), ESP32/mic + aquascape/pump + PC/GPU (3-topic multi-domain),
WLED/LED (WS2812B/SK6812) + NAS/server (Synology/QNAP), and aquascape/
aquarium vs. an unrelated bioskop-ticket query.

**Phase 2 (trace every finding, classify before touching code).** Every
apparent failure was traced to the exact layer, and explicitly checked
against the alternative explanation that it was a probe/test artifact
rather than a production bug, per the brief's own instruction to
distinguish real bugs, intentional limitations, and test/probe
weaknesses.

## Root cause (Phase 2) - the ONE proven, fixed bug

`_TOPIC_OVERLAP_STOPWORDS` (`luno/memory_context.py`) was missing
`"berapa"` ("how much/many") and `"tadi"` ("earlier/just now") - the SAME
class of generic, subject-agnostic word already fixed for `"aku"`/
`"mau"`/`"soal"`/`"sekarang"`/`"oke"` in Sprints 39-41 (each addition
documented in-line in that same frozenset with its own precedent
comment). Because `select_topic_candidates()`'s lexical-overlap branch
has no ambiguity gate of its own, this single gap caused three distinct,
independently reproduced failures, live, before any fix:

1. **False injection (Scenario H).** "Berapa harga tiket bioskop?" (a
   fully unrelated query, no domain vocabulary, no temporal marker)
   wrongly injected a prior AQUARIUM topic into the prompt, purely
   because both turns happened to contain the word "berapa".
   `topic_history_candidates` went from `0` (correct) to `1` (wrong) in
   direct, reproduced live-console output. Violates "recent topic is not
   automatically relevant" and the scenario's own explicit expectation
   ("zero unrelated topic injection").

2. **False non-injection (Scenario A).** "Yang sekarang berapa
   VRAM-nya?" (asking about the CURRENT GPU's VRAM, after a CURRENT
   RTX 3060 Ti + PLANNED RTX 5070 statement) produced NO injected
   context at all. Root cause: `select_temporal_fallback_candidate()`'s
   own `_TEMPORAL_FALLBACK_MAX_RESIDUAL_TOKENS=1` ambiguity gate (added
   in Sprint 41 specifically to prevent an unrelated-topic false
   positive) counted "berapa" as a real residual content token alongside
   "vram", pushing the residual count to 2 and refusing to fire - the
   SAME missing-stopword gap that broke Scenario H also silently starved
   this branch of context it should have supplied.

3. **Self-echo pollution (Scenario E).** After a 3-topic switch
   (ESP32/mic -> aquascape/pump -> PC/GPU), "GPU yang tadi?" correctly
   found the GPU entry but ALSO pulled in an irrelevant, self-echoed
   entry from an earlier turn's OWN question ("Yang tadi soal mic
   gimana?"), purely via the shared word "tadi" - the exact self-echo
   pollution pattern Sprint 41 first identified and partially mitigated
   (but only for the temporal-fallback branch's own residual-token
   check, not for `select_topic_candidates()`'s separate, unguarded
   branch).

All three were reproduced live through `RuntimeDemoConsole`, captured via
the `[MemoryContinuity]` debug line and the raw rendered `system_prompt`,
BEFORE any code changed.

## Fix (Phase 4)

Added `"berapa"` and `"tadi"` to the existing `_TOPIC_OVERLAP_STOPWORDS`
frozenset in `luno/memory_context.py` - the smallest possible additive
change: one file, two words, reusing the exact same shared set
`select_topic_candidates()`, `is_correction_signal()`'s retagging-overlap
check, and `select_temporal_fallback_candidate()` already all read from.
No new mechanism, no new gate, no synonym dictionary, no embeddings, no
change to ranking, budget, rendering, or prompt assembly.

Re-verified live, before considering the fix complete, that this
addition:

- Resolves Scenario H (`topic_history_candidates` drops back to `0` for
  the unrelated bioskop query).
- Resolves Scenario A (the "berapa VRAM" query now correctly injects
  CURRENT RTX 3060 Ti, never PLANNED RTX 5070 - independently confirming
  Sprint 41's own CURRENT-vs-PLANNED protection is untouched by this
  fix).
- Resolves Scenario E's self-echo pollution (the full A->B->C->A 3-topic
  switch sequence - mic, then pump, then GPU, then back to mic reference
  - now resolves cleanly with zero cross-topic leakage at every step).
- Does NOT make the overlap check blind to genuine shared vocabulary
  (a dedicated regression test confirms a query that shares a real
  content word, e.g. "rtx"/"vram", with a stored entry still matches).

## Investigated and found to be CORRECT pre-existing behavior (not bugs)

**Scenario C (ordinal + temporal) - a probe/test artifact, not a
production bug (category M).** An early probe run made ordinal
resolution look broken for "yang kedua"-style queries. Root cause of the
APPEARANCE of a bug: the probe's own mock LLM reply squeezed a 3-item
numbered list onto a single line. `extract_list_items_from_reply()` is
deliberately line-anchored (`_LIST_ITEM_LINE_RE = re.compile(r'^(?:\d{1,2}
[.)]\s+|[-*•]\s+)(.+)$')`) because, by design since Sprint 38, it parses
Luno's OWN finalized reply - never the user's text - and expects each
list item on its own line (the realistic shape of an LLM's enumerated
answer). Re-tested with a realistic multi-line mock reply: all four
ordinal+temporal phrasing combinations ("Yang kedua gimana?", "Yang kedua
yang sekarang gimana?", "Yang kedua yang dulu?", "Yang mau dipakai yang
kedua apa?") consistently and correctly resolved to the same item
(MAX9814), with zero fabrication of the other list items, and an
out-of-range ordinal ("Yang kelima gimana?" against a 2-item list)
correctly resolved to nothing at all rather than guessing. Conclusion:
ordinal resolution correctly ignores an added temporal qualifier when the
underlying list has no distinct per-item temporal state of its own - this
is correct, not ambiguous, and the original "failure" was purely a probe
instrumentation gap, now fixed in the new test file's own mock replies.

**Scenario G (temporal history depth) - a probe-phrasing artifact, not a
production bug (category M).** An early probe constructed a CURRENT ->
PLANNED -> COMPLETED chain where the COMPLETED turn's own text ("Sudah
aku ganti ke SK6812.") never mentioned the domain word "LED" at all.
Because retrieval here is purely lexical (no embeddings, no synonym
layer, per this sprint's own explicit prohibition), a query like "LED
yang sekarang apa?" can only find entries whose OWN stored text literally
contains "LED" - and the completion turn's entry, lacking that word,
could never be linked back to the LED domain, so the query surfaced the
stale original CURRENT statement (WS2812B) instead of the intended
completed value (SK6812). This is the exact same class of pre-existing,
already-documented lexical-matching limitation as Sprint 40's
"ESP8266" vs "ESP32" precedent (disjoint token sets for the same real
subject) - not a new defect, and not something addable without exactly
the synonym dictionary or embedding model this sprint is explicitly
forbidden from introducing. Re-tested with the domain word retained in
every turn ("Sudah aku ganti LED-nya ke SK6812.") and the full
CURRENT -> PLANNED -> COMPLETED -> (different domain) CURRENT -> PLANNED
chain resolves correctly at every query, with zero cross-domain leakage
between the LED and NAS domains.

## Known limitation (pre-existing, unchanged, still applies)

The disjoint-vocabulary lexical-matching gap documented in `docs/
change_impact/temporal_memory_timeline_awareness.md` (§41) - a
planned-intent query using different vocabulary than the original
planning statement can still surface an irrelevant lexically-matched
entry ahead of the correct temporal fallback - is unchanged by this
sprint. Closing it would require a synonym dictionary or an embedding
model, both explicitly forbidden by Sprint 42's own constraints.

## Tests

`tests/test_cross_system_conversation_consistency.py` (25 new tests, all
passing):

- **Section 1 (3 tests).** Unit regression for the stopword fix,
  including a "still matches on real overlap" guard so the fix cannot be
  over-broadened into blindness to genuine shared vocabulary.
- **Sections 2-9 (Scenarios A-H, 9 tests).** Real `RuntimeDemoConsole`
  E2E: CURRENT-vs-PLANNED with the exact proven-bug query; correction
  updating a PLANNED target without destroying the separate CURRENT
  state; ordinal resolution across all four temporal-qualified phrasings
  plus an out-of-range no-fabrication check; attribute merge preserving
  the parent topic across two follow-up attribute questions; a 3-topic
  switch-and-back reference sequence with an explicit self-echo-pollution
  regression assertion; reference+correction+topic-switch; 2-domain
  temporal-depth (LED + NAS, 7 turns); the primary proven-bug regression
  test (Scenario H, zero unrelated-topic injection).
- **Section 10 (Scenario I, 3 tests).** Voice mode (SHORT/ALL) does not
  change reference resolution; voice mode is per-conversation, not
  global; `build_dual_response()`'s signature has no memory-state
  parameter (structural invariant).
- **Section 11 (Scenario J, 2 tests).** Interleaved two-conversation
  isolation (A1,B1,A2,B2,A3,B3 with no cross-talk); a genuine two-thread
  concurrent conversation-isolation test hitting the same
  `PlannerBridgeModule` instance simultaneously.
- **Section 12 (2 tests).** Structural invariant checks: no embedding/
  LLM-judge import or call pattern in `luno/memory_context.py`;
  `conversation_ended` still clears every per-conversation dict
  (`_active_topic`/`_topic_history`/`_last_topic_terms`/`_voice_output_
  mode`).

## Regression

Full `tests/` tree (excluding the 2 pre-existing uncollectible files -
`test_main_bargein.py`/`test_root_main_bargein.py`, missing
`faster_whisper`/`legacy_main.py`):

`python3 -m pytest tests/ -q --ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_bargein.py -n 4 --dist loadfile`

-> **2543 passed, 10 failed** (2553 collected total, including this
sprint's own 21-then-25-test file). All 10 failures exactly match this
project's own documented, pre-existing, environment-coupled baseline:

- `tests/test_mic_device_index.py` (6 tests) - `MIC_DEVICE_INDEX`-set-in-
  real-`.env` environment-specific failures every prior sprint's baseline
  notes.
- `tests/test_production_launcher.py::test_07_health_checks_all_pass_in_
  default_mock_configuration` - the same pre-existing, already-documented
  environment-specific failure.
- `tests/test_real_adapters.py` (2 tests) - the same pre-existing
  environment-coupled failures documented since §15.
- `tests/test_state_isolation.py::test_isolate_persistent_state_drains_
  stragglers_before_monkeypatch_reverts` - the same pre-existing,
  documented scheduling-jitter flake.

A separate, wider `-n 8` sweep (heavier parallelism than the established
`-n 4 --dist loadfile` baseline command) additionally surfaced 6
timing-sensitive TTS/streaming test failures
(`test_runtime_demo.py`/`test_tts_chunk_pipelining.py`/`test_voice_
pipeline_latency.py`) - all 6 re-ran and passed cleanly in isolation
(`6 passed in 12.25s`), confirming these were parallel-worker resource
contention flakes (the same documented class every prior sprint's own
baseline notes for similarly timing-sensitive files), not regressions
from this sprint's fix, which touches only a stopword set in
`luno/memory_context.py` with zero relationship to TTS/streaming/audio.

The 472 tests in the memory/topic/reference/temporal-focused files most
directly exercising the changed code path
(`test_memory_conflict_resolution.py`, `test_memory_confidence.py`,
`test_temporal_memory_timeline_awareness.py`, `test_conversation_
reference_resolution.py`, `test_memory_continuity.py`, `test_memory_
decision_quality.py`, `test_memory_evaluation.py`, `test_conversation_
intelligence.py`) were additionally run in isolation: **472 passed**.

## Persistent-state safety

SHA256 + size of all 15 top-level `config/*.json` files (excluding
`config/backups/`) captured before the fix and after the full sprint
(implementation + this sprint's own new test file + the full regression
sweep, which drives many real turns through the production path):

- 14 of 15 files: byte-identical (SHA256 unchanged).
- `config/relationship_state.json`: `interaction_count` and
  `last_interaction_timestamp` changed (size unchanged, 196 bytes both
  before and after). This is the well-precedented PROBE SIDE EFFECT
  every prior sprint's own baseline has also observed - running many
  turns through the real `RuntimeDemoConsole` production path
  legitimately increments this real usage counter and timestamp. Per
  this sprint's own Phase 6 instruction, this is reported separately as
  a probe side effect, not treated as a sprint-caused production change.

## Files changed

`luno/memory_context.py` - additive only: `"berapa"` and `"tadi"` added
to the existing `_TOPIC_OVERLAP_STOPWORDS` frozenset, with an in-line
comment following this file's own established precedent-documentation
convention. One code change, one file.

No changes to `assemble_context()`, `_apply_budget()`,
`render_context_block()`, `select_topic_candidates()`'s own overlap
logic, `update_active_topic()`, `update_topic_history()`, `select_
temporal_fallback_candidate()`, `resolve_ordinal_targets()`, `ordinal_
targets_to_relevant_memory()`, `build_dual_response()`, the LLM model,
TTS voice/model, the streaming architecture, or response-depth
semantics.
