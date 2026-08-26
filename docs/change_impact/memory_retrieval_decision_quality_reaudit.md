# Memory Retrieval & Decision Quality (re-audit) - change impact

**Sprint:** independent, evidence-first re-verification of the already-shipped
memory/topic pipeline (ARCHITECTURE_GUARD.md §25 Memory Retrieval & Decision
Quality / Intent Taxonomy + Topic Continuity, §30 Memory Continuity & Short
Follow-up Reference Resolution, §31 Memory Topic Retention & Recall
Reliability). Explicit constraint from the brief: do not assume this is an
LLM problem, do not modify production code before reproducing with hard
evidence, do not rewrite the existing systems.

## Phase 0 - pipeline map (file paths / line ranges as of this checkout)

- `luno/memory.py::classify_query_intent()` (line 2040), `classify_reference_type()`
  (line 2191), `needs_topic_context()` (line 2226), `is_pure_reference_followup()`
  (line 2254).
- `luno/memory_context.py`: `ActiveTopicSnapshot`/`update_active_topic()`,
  `update_topic_history()` (line 918), `select_topic_candidates()` (line 956),
  `build_expanded_retrieval_text_from_history()` (line 1018),
  `topic_history_to_relevant_memories()` (line 1039), `_apply_decision_quality_bonus()`
  (line 1112), `deduplicate_context_items()` (line 1166), `_apply_budget()`
  (line 1258), `render_context_block()` (line 1384), `assemble_context()`
  (line 1418, the top-level entry point).
- `luno/memory_context.py::ContextItem._rank_key()`: an 8-tuple
  `(relevance, importance, context_evidence, usefulness, evaluation,
  usage_count, intent_bonus, priority)`, compared lexicographically -
  relevance dominates by construction; `intent_bonus` can only ever break a
  tie among items with identical relevance/importance/context_evidence/
  usefulness/evaluation/usage_count. Re-inspected, unmodified.
- `luno/memory_retrieval/retriever.py::MemoryRetriever.retrieve_memories()`:
  early exit `if not query.has_any_signal: return []`; `_apply_recency_and_staleness()`;
  `_deduplicate()`; `_apply_limits()` (count cap then a token-budget loop that
  `break`s, not `continue`s, on the first item that would exceed budget).
- `luno/memory_retrieval/query.py::analyze_query()`: the ONE shared tokenizer
  (`_WORD_RE`) every stage above reuses - retrieval scoring, topic-term
  extraction, and topic-candidate content matching all call this same
  function, by design ("one tokenizer" - module's own docstring).
- Call site: `main_runtime_demo.py::PlannerBridgeModule._handle_utterance()`
  (line 3071) reads `_topic_history`/`_active_topic`, calls
  `classify_query_intent()` (3136), `classify_reference_type()` (3155),
  `select_topic_candidates()` + `build_expanded_retrieval_text_from_history()`/
  `topic_history_to_relevant_memories()` (4134-4187), then
  `memory_context.assemble_context()` (4212-4223). `_on_assistant_response()`
  (line 2863) writes `_active_topic`/`_topic_history` for the NEXT turn via
  `update_active_topic()`/`update_topic_history()` (2905/2923), gated on
  `memory.is_pure_reference_followup(user_text)` (NOT `needs_topic_context()` -
  a naming discrepancy from generic briefs; the actual gate function is
  `is_pure_reference_followup()`, confirmed via `main_runtime_demo.py`'s own
  comments at lines 2895/2900).
- Final prompt: `memory_context_block` (assembled_context.render()) is
  appended to a `notes` list (line 4274-4275), joined into `system_note`
  (line 4419), sent as `NeedLLMResponse.data["system_prompt"]` (line 4435-4460).
  `MockOpenRouterClient.calls[-1]["system_prompt"]` / the `need_llm_response`
  event's own `system_prompt` field is the exact, real, final LLM input -
  used throughout this sprint's reproduction.

## Phase 1-2 - reproduction (real `RuntimeDemoConsole`, not helper functions)

Two scenarios driven through the real production event path (`user_utterance`
-> `need_llm_response`, `MockOpenRouterClient(canned_text=None)` so the
"reply" echoes the user's own text, keeping topic-term extraction
deterministic without per-turn scripting):

1. The brief's own 8-turn scenario (mic/ESP32/INMP441, ESP32 voice system,
   ESP8266/Bluetooth, aquascape, "what about the mic?", "which one was it
   again?", "what did I use for the ESP32?", "how does that connect?").
2. An A/B/C multi-topic scenario (A: ESP32/INMP441 mic, B: aquascape/pump,
   C: WLED/LED strip), each followed by A2/B2/C2, then an A3 exercising the
   A->B->C->A case.

### Before the fix (raw trace, `/tmp/memory_pipeline_trace.json` first run)

| Turn | Text | reference_type | is_short_followup | Candidates reaching `assemble_context()` |
|---|---|---|---|---|
| 5 | "Anyway, what about the mic?" | unknown | False | 1 (correct - matched on "mic") |
| 6 | "Which one was it again?" | **unknown** | **False** | **0** |
| 7 | "What did I use for the ESP32?" | unknown | False | 1 - **WRONG topic** (ESP8266/Bluetooth entry, both had tokenized to "esp") |
| 8 | "How does that connect?" | **unknown** | **False** | **0** |

Turns 6 and 8 reached `assemble_context()` with **zero** candidates - the
rendered `system_prompt` had no `[BEGIN STORED MEMORY CONTEXT]` block at
all. Turn 7 reached it with exactly one candidate, but it was the wrong
one: the ESP32/mic entry (turns 1-2) had already aged out of the bounded
4-entry `_topic_history`, leaving only the ESP8266/Bluetooth entry (turn 3),
which matched purely because `analyze_query("ESP32...")` and
`analyze_query("ESP8266...")` both reduced to the shared token `"esp"`.

### Root cause, proven with evidence

**1. Tokenizer digit loss (`luno/memory_retrieval/query.py::_WORD_RE`).**
Before: `re.compile(r"[a-zA-Z']+")`. `_WORD_RE.findall("ESP32")` ->
`["esp"]`; `_WORD_RE.findall("ESP8266")` -> `["esp"]`;
`_WORD_RE.findall("INMP441")` -> `["inmp"]`. Three distinct hardware
identifiers collapse onto two shared, truncated tokens, and this single
function is reused by retrieval scoring, `extract_topic_terms_from_turn()`,
and `select_topic_candidates()`'s own token-overlap ranking - the collision
propagates through every stage that depends on token identity. Classified
as **Failure Class C/D** (retrieved, but the WRONG candidate wins, because
the underlying token identity was itself lossy - not a ranking bug;
`_rank_key()` correctly ranked the one candidate it was given, the
candidate pool itself was corrupted upstream).

**2. Classifier coverage gap (`luno/memory.py::classify_reference_type()`).**
"Which one was it again?" and "How does that connect?" match none of
`_NEGATION_REFERENCE_RE`, `_COST_COMPARISON_RE`, `_ALTERNATIVE_REQUEST_RE`,
`_CONTINUATION_REFERENCE_RE`, `_COMPARISON_MARKER_RE`, or
`_DIRECT_REFERENCE_RE` (which requires one of a fixed set of idioms: "about
that", "kalau itu", "yang tadi", etc. - a bare "was it"/"does that" is not
among them). Both fall through to `"unknown"`. `NEEDS_TOPIC_CONTEXT_TYPES`
excludes `"unknown"` by construction, so `is_short_followup` is `False` for
both -> the single-slot `_active_topic` fallback (gated on
`is_short_followup`) never fires. `select_topic_candidates()` ALSO correctly
returns `[]` for both (genuinely zero token overlap with any topic-history
entry - this part of the pipeline was working exactly as designed). Result:
**zero candidates reach `assemble_context()`, by construction** - Failure
Class B (never retrieved - the candidate is never generated in the first
place, not filtered/outranked/budget-cut afterward).

## Phase 6-8 - ranking / budget / rendering audit

`_rank_key()` re-inspected: relevance is the dominant, first tuple element;
`intent_bonus` (the prior sprint's own intent/continuity signal) can only
ever break a tie among items with otherwise-identical relevance/importance/
context_evidence/usefulness/evaluation/usage_count - it structurally cannot
rescue a lower-relevance candidate, and it did not do so anywhere in this
sprint's own reproduction (every turn above had 0 or 1 candidates; the one
A/B/C-scenario case with 2 candidates, turn 6 below, was already correctly
ordered by relevance/overlap-size alone). **`_rank_key()` was not modified.**

`_apply_budget()` (`memory_context.py`) was exercised in every E2E turn
above and never dropped a correct, present candidate - all turns had at
most 2 candidates, well under `config.max_results`/`max_tokens`. No budget-
pressure failure was found or reproduced in this sprint's own scenarios;
Phase 7's adversarial "correct memory is candidate #1, many irrelevant
memories follow, budget intentionally constrained" case is already covered
by `tests/test_memory_continuity.py::test_AN_budget_pressure_can_still_drop_the_active_topic_candidate`
from the prior Sprint 4 and was not touched.

Context rendering (`render_context_block()`): technical identifiers survive
into the rendered block correctly ONCE the tokenizer fix is in place -
`"esp32"`/`"inmp441"` appear whole in `[Relevant Memories]` entries; this was
the exact rendering-visible symptom of root cause 1 above, not a separate
rendering bug (`render_context_block()` itself renders whatever token text
`ContextItem.text` already contains, unchanged).

## Phase 9 - fix (both additive, both extend an existing mechanism)

1. `luno/memory_retrieval/query.py::_WORD_RE`: `[a-zA-Z']+` ->
   `[a-zA-Z][a-zA-Z0-9']*`. A token must still start with a letter (a bare
   `"5"` or `"441"` still never matches on its own - `test_3_empty_retrieval_for_no_signal_query`
   ("What's 5 + 5?" -> no signal) reconfirmed unaffected), but once started,
   digits are no longer stripped, so `"esp32"`, `"esp8266"`, `"inmp441"` each
   stay one whole, distinct token.
2. `luno/memory.py`: new `_BARE_PRONOUN_REFERENCE_RE` (narrow bigrams -
   "which one", "was/is/does/did it", "how does/do/did it/that/this", "what
   was/is it"), consulted in `classify_reference_type()` at the lowest
   precedence tier (after `_DIRECT_REFERENCE_RE`, before the final
   `"unknown"` fallthrough), resolving to the same `"direct_reference"`
   result the existing pure-reference machinery already handles. No residual-
   word gate (unlike the existing `_COMPARISON_MARKER_RE` branch) - the
   bigram's own phrase-specificity is the false-positive guard (see the
   pattern's own docstring for the "how does ESP32 handle low power mode?"
   worked non-match example).

Neither `_rank_key()`, `_apply_budget()`, `select_topic_candidates()`,
`update_active_topic()`/`update_topic_history()`, the intent/continuity
bonus, nor the prompt-injection trust boundary were modified.

## After the fix (same two scenarios, re-run)

| Turn | Text | reference_type | is_short_followup | Result |
|---|---|---|---|---|
| 2 | "I use ESP32 for the voice system." | unknown | False | 1 candidate, `esp32`/`inmp441` preserved whole |
| 3 | "Bluetooth isn't available on the plain ESP8266." | unknown | False | 0 candidates (correct - no longer falsely matches "esp32" via a shared "esp" token; this turn is itself a fresh, standalone statement) |
| 5 | "Anyway, what about the mic?" | unknown | False | 1 candidate, correct (mic/ESP32/INMP441) |
| 6 | "Which one was it again?" | **direct_reference** | **True** | 1 candidate via the single-slot fallback (mic topic) - **was 0 before** |
| 7 | "What did I use for the ESP32?" | unknown | False | 1 candidate, correct ESP32 entry - **ESP8266/Bluetooth text confirmed absent** |
| 8 | "How does that connect?" | **direct_reference** | **True** | 1 candidate (most recent active topic) - **was 0 before** |

Confirmed at the actual, final LLM-facing `system_prompt` (not just an
intermediate data structure) for turns 6, 7, 8 - excerpt for turn 7:

```
[BEGIN STORED MEMORY CONTEXT - ...]
[Relevant Memories]
- Active conversation topic: esp32, mock, reply, system, use, voice
[END STORED MEMORY CONTEXT]
```

No `bluetooth`/`esp8266` text present.

A/B/C multi-topic scenario (turns 4/5/6/7 = A2/B2/C2/A3): A2 correctly
surfaces the mic topic (not pump), B2 the pump topic, C2 the LED/WLED topic
(plus one extra, lower-ranked, non-contaminating "mic" entry from shared
token "use" - see Known Limitations), A3 (the A->B->C->A case) correctly
surfaces the mic topic again, not pump/WLED.

## Retrieval Metrics

Computed over the 9 turns (across both scenarios) that legitimately need a
memory candidate (excludes turns that are themselves fresh/standalone
statements, where 0 candidates is the correct outcome):

- Recall@1: 9/9 (100%)
- Recall@3: 9/9 (100%)
- Recall@5: 9/9 (100%)
- Correct-candidate survival (retrieval -> ranking -> budget): 9/9 (100%) -
  no budget-pressure drop was observed or reproduced in this sprint's own
  scenarios (see Phase 6-8 above for the existing, unmodified budget-
  pressure test already covering that case from Sprint 4)
- Prompt survival (final `ContextItem` -> actual `system_prompt` string):
  9/9 (100%), directly confirmed by string-searching the real
  `system_prompt` for turns 6/7/8 and via the new E2E test suite

Sample size is intentionally small (real production-path E2E turns, not a
synthetic benchmark) - see Known Limitations.

## Tests

New: `tests/test_memory_retrieval_decision_quality_reaudit.py` (22 tests -
5 tokenizer unit tests including the "5 + 5" no-signal regression guard and
a digit-leading-identifier known-limitation test; 10 classifier unit tests
including 3 false-positive/precedence guards; 5 production-path E2E tests
reproducing the 8-turn scenario turns 6/7/8, the A/B/C scenario including
A->B->C->A, and an unrelated-question adversarial case). All scoped to the
rendered `[BEGIN STORED MEMORY CONTEXT]...[END STORED MEMORY CONTEXT]`
block only, never the whole `system_prompt` - Luno's own static persona
text separately lists "ESP32/Arduino" under its own always-present
"Knowledgeable about:" line, which would otherwise false-positive an
unscoped substring check regardless of what memory was actually retrieved
(the same pitfall `tests/test_memory_continuity.py`'s own `_word_in` helper
already documents for "knowledgeable" containing "wled").

Existing: `tests/test_memory_topic_retention.py` - 2 assertions updated (not
rewritten) from the old, buggy `"esp"` expectation to the corrected
`"esp32"` (see that file's own inline comments for the full justification).

Full regression: `tests/` tree, 2147 collected (excluding
`test_main_bargein.py`/`test_root_main_bargein.py`, pre-existing
uncollectible), run in 4 chunks under `-n 2`. 15 failures, ALL pre-existing
and already documented in `docs/testing/regression_baseline.md`: 4 timing/
scheduling-jitter flakes (reconfirmed passing 100% in serial isolation) and
11 environment-specific failures (mic-device-index/.env-credential/sandbox-
introspection classes). Zero new regressions in any file this sprint did
not touch.

## Persistent State

**Incident (found during Phase 0-2, before isolation was added to the raw
reproduction script):** the script directly instantiated `RuntimeDemoConsole`
without going through `tests/conftest.py`'s `isolate_persistent_state`
fixture (which only applies inside pytest). One run wrote through to:

- `config/relationship_state.json` - `interaction_count` incremented
  319->335, `familiarity` 0.0->0.03, `shared_experience_count` 0->1,
  `last_interaction_timestamp` updated. No fabricated content, purely
  bookkeeping counters. **Restored** from the nearest pre-run backup
  (`config/backups/relationship_state.20260813T071359103336.json`),
  verified byte-identical via `diff` after restore.
- `config/episodic_memory.json` - one fabricated entry written via the
  real "device_configured" episodic-memory ingestion path:
  `{"summary": "My mic is an INMP441 connected to an ESP32.", "category":
  "device_configured", "source": "conversation", ...}` - verbatim text from
  this sprint's own test scenario, timestamp exactly matching the
  unsafe-run window. No backup existed for this file; **restored to `[]`**,
  the module's own documented default for a missing/malformed/empty store
  (`luno/episodic_memory.py::load()`'s own fallback).

All OTHER `config/*.json` files (`long_term_memory.json`, `verified_facts.json`,
`session_summaries.json`, `habit_memory.json`, `reminders.json`,
`lights.config.json`, `persona.json`, `switches.config.json`) were checked
by mtime and confirmed untouched (all predate this sprint's start) - the
"wled"/"aquascape" text found in several of them via a broad content grep is
Vinn's own pre-existing, real data (consistent with the entire project's own
domain - Luno genuinely controls a real WLED light and has a real
aquascape), not contamination.

Every reproduction run AFTER this incident was isolated (either via a
`luno.config` attribute redirect mirroring `tests/conftest.py`'s own
`_WRITABLE_STATE_ATTRS` list exactly, in the raw script, or via pytest's own
autouse fixture for the final test file) - mtime-verified unchanged before/
after every subsequent run.

## Known Limitations

- A digit-LEADING identifier ("3D", "24/7") still does not tokenize as its
  own signal token (same behavior as before this fix, not a regression) -
  every real-world identifier this sprint's own reproduction and the
  brief's own worked examples use (ESP32, ESP8266, INMP441, WLED, MQTT) is
  letter-leading, so this is not believed to affect the reported symptom.
- `_TOPIC_HISTORY_CANDIDATE_LIMIT = 2` can surface a second, lower-ranked,
  genuinely less-relevant topic entry alongside the correct one when both
  share an ordinary content word (observed: "What did I use for the LED
  strip?" also surfaced an unrelated "mic" entry that shared the token
  "use") - the correct entry still ranks first and nothing incorrect is
  ever chosen INSTEAD of it, but the rendered context is not maximally
  precise in this case. Not fixed by this sprint (would require either a
  minimum-overlap threshold or IDF-style down-weighting of very common
  content words - out of scope for "smallest proven fix", and no evidence
  this precision loss changes any answer's correctness in the sprint's own
  reproduction).
- `_BARE_PRONOUN_REFERENCE_RE` has no residual-word gate, so a sentence that
  matches one of its bigrams AND separately names a new entity (e.g. "How
  does that connect to the ESP32?") is still classified `"direct_reference"`
  rather than splitting into "resolve 'that' AND also anchor to the new
  ESP32 mention" - an accepted, documented edge case, not exercised by
  either of this sprint's own reproduction scenarios.
- Retrieval metrics (Recall@1/3/5) are computed over 9 real production-path
  E2E turns, not a large synthetic benchmark - appropriate for proving the
  two specific, reproduced root causes are fixed, not a general-purpose
  retrieval-quality score.

## Invariants (unchanged by this sprint)

- memory: no new store, no new persistence format.
- retrieval: `MemoryRetriever`'s own architecture, dedup, and limit logic
  unchanged.
- ranking: `_rank_key()` unchanged - relevance-first, `intent_bonus`
  tie-break-only.
- budget: `_apply_budget()` unchanged.
- TTS / streaming: untouched.
- topic history: `update_active_topic()`/`update_topic_history()`/
  `select_topic_candidates()` unchanged - only their INPUT (tokenized text)
  and their CALLER's gating condition (`is_short_followup`, via the
  classifier fix) changed.
- prompt-injection boundary: `render_context_block()`'s
  `_MEMORY_CONTEXT_BOUNDARY_OPEN`/`_CLOSE` wrapping unchanged.
- persistent raw memory: still never persisted by `memory_context.py`
  itself; the two files touched by the Phase 0-2 incident were restored.
- second ranking system: none introduced.
- LLM judge: none introduced.
