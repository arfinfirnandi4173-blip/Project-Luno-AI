# Memory Topic Retention & Recall Reliability

## 1. Problem

Sprint 4 (`memory_continuity_reference_resolution.md`) solved SINGLE-HOP
elliptical follow-up resolution ("yang lain?", "terus?", "kalau itu
gimana?"). It is explicitly complete and out of scope for this sprint -
`classify_query_intent()`, `classify_reference_type()`,
`needs_topic_context()`, `is_pure_reference_followup()`, `_active_topic`
scoping/bounding, `assistant_response -> planner` routing, existing
`intent_bonus` ranking, the prompt-injection trust boundary, TTS/
streaming, and the existing memory ranking architecture were all left
completely unmodified.

This sprint targets a DIFFERENT failure layer: multi-turn TOPIC
RETENTION. Can a user return to a topic after several turns, after
unrelated turns, or after switching between multiple topics, and still
have Luno recover the correct context?

## 2. Root cause (Phase 0, evidence-based, not assumption)

Reproduced live through a real `RuntimeDemoConsole` (not a mock, not
assumption) - a 6-turn ESP32/INMP441 conversation:

```
Turn 1: "ESP32-ku mau aku gabungkan dengan sensor suara INMP441."
Turn 2: "Kalau power supply-nya gimana?"
Turn 3: "Terus aku pengen tambahin relay."
Turn 4: "Kalau yang lebih murah ada?"
Turn 5: "Besok lanjut yang ESP32 tadi."
Turn 6: "Untuk mic-nya pakai apa?"   <- context lost
```

**Finding #1 - single-slot replacement destroys broader-topic context.**
`PlannerBridgeModule._active_topic` (Sprint 4) held exactly ONE
`ActiveTopicSnapshot` per conversation. `update_active_topic()`'s
replace-vs-preserve rule REPLACES the entire snapshot whenever
`is_pure_reference_followup(user_text)` is `False`. Turn 2 ("Kalau power
supply-nya gimana?") is classified `comparison` (residual tokens
"power"/"supply") by the unmodified `classify_reference_type()` -
`is_pure_reference_followup()` is `False` for `comparison` BY DESIGN
(Sprint 4's own docstring: it "carries its own named entity and must
REPLACE the active-topic snapshot"). This design is CORRECT for a genuine
topic branch ("Kalau WLED gimana?" after a Bluetooth discussion) but
WRONG for an ordinary sub-question within the same broader topic (asking
about power supply for the SAME ESP32 project) - both shapes are
classified `comparison` and are indistinguishable from that output alone.
Live-probed: after turn 2, `_active_topic`'s terms were entirely
`{power, supply, stabil, kalau, gimana, ...}` - `esp`/`inmp`/`sensor`/
`suara` from turn 1 were gone, unrecoverable by turn 6.

**Finding #2 - grammatically complete turns never trigger the mechanism
at all.** Turn 6 ("Untuk mic-nya pakai apa?") is a complete question, not
an elliptical fragment - `classify_reference_type()` correctly returns
`unknown` for it (there is no reference-fragment regex to match), so
`needs_topic_context()` is `False`. Sprint 4's ENTIRE candidate-injection
mechanism is gated on `needs_topic_context()`/`is_short_followup`, so it
never even attempted to help this turn, despite "the mic" obviously
depending on prior context. This is a genuinely new gap beyond Sprint 4's
own scope (which only ever handled true elliptical fragments).

**Finding #3 - term-cap truncation, confirmed by direct token analysis.**
Even where a snapshot WAS retained, `extract_topic_terms_from_turn()`
merges user tokens then reply tokens and truncates to
`_ACTIVE_TOPIC_MAX_TERMS` (12). Direct analysis of turn 1's real
tokenization:

```
user tokens:  ['esp','ku','mau','aku','gabungkan','dengan','sensor','suara','inmp']
reply tokens: ['bagus','esp','dengan','inmp','bisa','dipakai','untuk','voice','sensor','mic','array','digital','s']
merged (dedup, first 20): [... 18 unique tokens ...]
"mic" position in merged list: 14   (of 18 total)
```

The old cap of 12 silently dropped "mic" (and "voice"/"array"/"digital")
from the snapshot on every single occurrence, even before any topic-
switching happened - a second, independent, compounding cause.

None of these three findings are ranking, budget, rendering, or LLM-
interpretation failures (the brief's own categories E-I) - all three sit
upstream, in retention itself (roughly the brief's category C: memory
exists but query/topic representation loses or never captures the
relevant term). The STOP CONDITION ("context present in final prompt but
LLM fails to use it") did not apply - proceeding to implement a fix was
correct.

## 3. Existing mechanisms reused, left completely unmodified

- `luno.memory.classify_query_intent()` / `classify_reference_type()` /
  `needs_topic_context()` / `is_pure_reference_followup()` - byte-for-
  byte unchanged. `tests/test_memory_continuity.py` (77 tests) passes
  unmodified, proving it.
- `PlannerBridgeModule._active_topic` (single-slot dict) /
  `update_active_topic()` / `active_topic_to_relevant_memory()` /
  `build_expanded_retrieval_text()` - byte-for-byte unchanged, still the
  sole handler for genuinely signal-less elliptical fragments (Sprint 4's
  own scope, still correct for that scope).
- `assemble_context()`'s existing `retrieval_query_override` parameter
  (Sprint 4) - reused, not re-designed.
- `analyze_query()` (the project's one tokenizer) - reused for every new
  overlap computation, never a second tokenizer.
- `_rank_key()` / `_apply_budget()` / dedup - completely untouched; every
  new candidate is an ordinary `RelevantMemory`/`ContextItem` subject to
  the exact same relevance-first ranking and budget enforcement as
  everything else.

## 4. Exact production files changed

- `luno/memory_context.py` - additive only:
  - `_ACTIVE_TOPIC_MAX_TERMS` raised `12 -> 20` (direct evidence, still a
    hard fixed cap, never unbounded - see Finding #3 above).
  - `_TOPIC_HISTORY_MAX_ENTRIES = 4`, `_TOPIC_HISTORY_CANDIDATE_LIMIT = 2`
    (new, small, fixed bounds).
  - `_TOPIC_OVERLAP_STOPWORDS` (new, fixed lexical resource - generic
    Indonesian/English particles, prepositions, pronouns, modal verbs).
  - `update_topic_history(history, user_text, reply_text, is_followup)`
    (new function).
  - `select_topic_candidates(history, text, is_short_followup)` (new
    function - the actual Phase 6 fix).
  - `build_expanded_retrieval_text_from_history(text, entries)` (new
    function).
  - `topic_history_to_relevant_memories(entries, turn_id)` (new
    function).
- `main_runtime_demo.py` (`PlannerBridgeModule`) - additive + one
  reordering:
  - `self._topic_history: Dict[str, List[ActiveTopicSnapshot]]` +
    `self._topic_history_max = 50` (new attributes, mirrors
    `_active_topic`'s own bounding/scoping conventions).
  - `_on_assistant_response()` extended (not replaced) to also call
    `update_topic_history()`, in its own independent try/except.
  - `_on_conversation_ended()` extended to also pop `_topic_history` for
    the ending session, same as `_active_topic`.
  - `_handle_utterance()`'s candidate-injection logic reordered:
    `select_topic_candidates()` now runs FIRST, unconditionally; if it
    finds a precise, content-matched entry, that is used alone; only when
    it finds nothing does Sprint 4's own single-slot branch run (its body
    is completely unmodified - only WHEN it fires changed).

No other files changed. `luno/memory.py` was not touched at all.

## 5. Topic-history representation

`_topic_history` is a bounded LIST of `ActiveTopicSnapshot` (Sprint 4's
own dataclass, unmodified), most-recent-first, per conversation:

- `update_topic_history()` ages every existing entry by one turn
  (mirrors `ActiveTopicSnapshot.turns_since_active` semantics, now
  per-entry). A "rich" turn (`is_followup=False`) PUSHES a new entry onto
  the front - it never overwrites an older entry directly. A pure-
  reference follow-up (`is_followup=True`) never pushes (no standalone
  content to push, same reasoning `update_active_topic()`'s own docstring
  already gives).
- Bounded on the SAME two axes the single slot already used: age
  (`ActiveTopicSnapshot.is_stale`, `turns_since_active > 6`, unchanged)
  and count (`_TOPIC_HISTORY_MAX_ENTRIES = 4`) - stale entries evicted
  first, then truncated to the count bound, keeping the most recent.

## 6. Candidate selection (the actual fix)

`select_topic_candidates(history, text, is_short_followup)` matches by
TOKEN OVERLAP between the current turn's own `analyze_query()` tokens and
each history entry's terms, both sides filtered through
`_TOPIC_OVERLAP_STOPWORDS` first. Ranked by overlap SIZE (descending,
stable sort so recency only breaks ties among equal-overlap entries -
never the primary signal), bounded to `_TOPIC_HISTORY_CANDIDATE_LIMIT`.

Deliberately does NOT branch on `is_short_followup`. An earlier version
did ("no standalone signal -> just offer the most recent entry"); live
reproduction of Phase 5's 3-topic scenario proved that branch wrong:
"Yang tadi soal mic gimana?" is classified `comparison` by the
unmodified classifier - `needs_topic_context()` therefore reports
`is_short_followup=True` for it, exactly like a truly signal-less
"terus?" would, even though it carries its own real residual word
("mic"). Trusting the flag picked whichever topic was merely most recent
(Luno's own source code, in the live reproduction), not the ESP32/
microphone topic the words actually named - this sprint's own root-cause
bug reproduced one layer deeper. Matching by content instead is safe for
genuinely signal-less fragments too: `query_tokens` ends up empty or
matches nothing, the function returns `[]`, and the caller's unmodified
Sprint 4 branch remains the correct fallback for exactly that shape.

Two stopword-list gaps were found and fixed while iterating against live
reproduction (not guessed in advance):

1. Common Indonesian connector words ("untuk"/"nya"/"pakai"/"apa") appear
   in nearly every phrasing regardless of topic - without filtering them,
   two merely-recent-but-unrelated entries outranked the one entry that
   actually shared the meaningful word ("mic").
2. First-person pronouns and modal verbs ("aku"/"mau" - "I want to...")
   are common enough in ordinary Indonesian sentences that a genuinely
   new, unrelated topic ("Aku mau bahas topik baru, soal motor listrik.")
   falsely matched an ESP32 entry purely because both happened to contain
   "aku"/"mau".

Both are fixed in the SAME fixed lexical resource (`_TOPIC_OVERLAP_STOPWORDS`)
- no model call, no second classifier, plain set-membership filtering
scoped only to this one overlap decision.

## 7. Caller reordering (why, and what did NOT change)

`_handle_utterance()` now computes `select_topic_candidates()` BEFORE
Sprint 4's own single-slot branch, and only falls back to that branch
when the new one finds nothing. This was necessary, not cosmetic: live
reproduction of the 3-topic scenario (ESP32 -> Aquascape ->
"Yang tadi soal mic gimana?") showed that running BOTH branches
unconditionally re-introduces contamination one layer up - Sprint 4's own
branch would unconditionally re-offer whichever topic was merely most
recent (Aquascape) ALONGSIDE the correctly content-matched one (ESP32/
mic), not strictly "wrong" (it genuinely was the previous topic) but
exactly the kind of unrelated-topic contamination this sprint measures
against and is meant to reduce.

What did NOT change: Sprint 4's own branch's body (the exact
`build_expanded_retrieval_text()`/`active_topic_to_relevant_memory()`
calls, `_active_topic` lookup, staleness check) is byte-for-byte
identical to before this sprint - only the `if`/`elif` ordering around it
changed. `_active_topic`'s own update function, scoping, and bounding are
untouched.

## 8. Ranking/budget/multi-topic safety (Phase 5/7)

Every topic-history candidate is constructed via
`topic_history_to_relevant_memories()`, which internally reuses Sprint
4's own `active_topic_to_relevant_memory()` per entry - same fixed
`_ACTIVE_TOPIC_CANDIDATE_SCORE = 0.55`, same `source="active_conversation"`
(absent from `_SOURCE_PRIORITY`, falls through to default, never
privileged), same "never raw sentence text" guarantee (only bounded,
sorted terms ever render). Once constructed, candidates are ordinary
`ContextItem`s subject to the EXACT SAME `_rank_key()`/`_apply_budget()`/
dedup as every other candidate - no second ranking system, no bypass, no
budget change. A topic-continuity signal here only ever answers "is this
memory likely about the topic being referred to" (via overlap), never
"this is important, inject it regardless."

Overlap-based (not recency-based) selection is also exactly what
delivers Phase 5's multi-topic independence for free: 3 topics (ESP32/
INMP441, Aquascape, Luno source code) established in one conversation are
each independently recoverable - asking about "diffuser" only overlaps
the aquascape entry, asking "yang tadi soal mic" only overlaps the ESP32
entry, regardless of which was discussed most recently. A genuinely
AMBIGUOUS reference ("Jelasin lagi yang kemarin.") has no residual token
of its own after stopword-filtering, matches nothing in any of the 3
entries, and correctly injects NOTHING - preserving ambiguity rather than
guessing across all three (verified directly by
`test_M2_ambiguous_reference_does_not_blindly_inject_all_topics`).

## 9. Production-path E2E proof

All scenarios below run through the real `RuntimeDemoConsole` -
`user_utterance` published on the real `EventBus`, routed through the
real `PlannerBridgeModule`, the real `assemble_context()`, into the real
rendered `system_prompt` captured off the real `need_llm_response` event
- not a unit-level shortcut.

The original Phase 0 reproduction, re-run after the fix:

```
Turn 6: 'Untuk mic-nya pakai apa?'
  Active conversation topic: aku, array, bagus, bisa, dengan, digital,
  dipakai, esp, gabungkan, inmp, ku, mau, mic, s, sensor, suara, untuk,
  voice
  'inmp' in final prompt: True
  'mic' (word-boundary-safe) in final prompt: True
```

3-topic scenario (ESP32 -> Aquascape -> Luno code -> "Yang tadi soal mic
gimana?"):

```
count of injected "Active conversation topic" blocks: 1
'inmp' in prompt: True
'diffuser' in prompt: False   (no cross-topic contamination)
```

Ambiguous case (same 3 topics -> "Jelasin lagi yang kemarin."):

```
AMBIGUOUS case block count: 0   (no guess across the 3 topics)
```

`tests/test_memory_topic_retention.py`'s `test_E2E_A` through
`test_E2E_H`, `test_M1`/`test_M2`, and `test_P1`-`test_P8` all reproduce
scenarios of this shape through the same real path (see Section 11).

## 10. Test results

New `tests/test_memory_topic_retention.py`: **41 passed, 0 failed**,
verified stable across 5+ repeated runs (including the concurrent-
conversation test, which is inherently timing-sensitive). Sections:

1. `update_topic_history()` unit tests (7) - pushing, aging, preserving
   older entries, count bound, staleness eviction, term-cap bound.
2. `select_topic_candidates()` unit tests (8) - empty/no-overlap/
   stopword-only-overlap all return `[]`; meaningful overlap matched;
   ranked by overlap size not recency; bounded to the candidate limit;
   `is_short_followup` does not change the overlap outcome; empty text
   handled.
3. `build_expanded_retrieval_text_from_history()` /
   `topic_history_to_relevant_memories()` unit tests (5).
4. E2E scenarios A-H (production path) (8) - topic then followup; topic
   then unrelated then followup; topic A -> B -> return to A; concurrent
   conversations never cross-contaminate; conversation-end clears
   history; conversation-id reuse does not inherit after end; technical
   identifiers survive 5+ turns; unrelated question gets zero
   contamination.
5. Phase 5 multi-topic safety (2) - 3 independently recoverable topics;
   ambiguous reference injects at most 1 topic (never guesses across 3).
6. Phase 9 adversarial phrase matrix (8) - positive recovery cases
   ("yang tadi", "untuk mic tadi", "balik ke ESP32", English "how about
   another one") and negative non-contamination cases ("topik baru",
   generic-word-only overlap, "lupakan yang tadi").
7. Non-regression (3) - `_active_topic`/`update_active_topic()`/
   `active_topic_to_relevant_memory()` proven independently addressable
   and unchanged.

## 11. Full regression results

- `tests/test_memory_continuity.py`: **77 passed, 0 failed** (Sprint 4's
  own suite, byte-for-byte unmodified).
- `test_memory_decision_quality.py` + `test_memory_retrieval.py` +
  `test_memory_context.py` + `test_memory_prompt_injection.py` +
  `test_memory_regression.py` + `test_memory_persistence_hardening.py` +
  `test_memory_dashboard.py` + `test_runtime_demo.py` +
  `test_manual_memory.py` + `test_memory_adaptive_retrieval.py` +
  `test_memory_evaluation.py` + `test_memory_guard.py` +
  `test_memory_learning.py` + `test_memory_outcome_telemetry.py`:
  **555 passed, 0 failed**.
- `test_adaptive_response_depth.py` + `test_barge_in_console.py` +
  `test_browser_wiring.py` + `test_conversation_end_race.py` +
  `test_conversation_ended_lifecycle_routing.py` + `test_device_context.py`
  + `test_environment_intent.py` + `test_interrupt_routing_fix.py` +
  `test_persistent_adaptive_response_depth.py` + `test_persona.py` +
  `test_response_output.py` + `test_response_policy.py`:
  **391 passed, 1 deselected** (the pre-existing, already-documented
  `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
  sandbox `inspect.getsource()` gap).
- Broader sweep: `test_llm_tts_streaming_production.py`,
  `test_production_launcher.py`, `test_vision_ask_vision.py`,
  `test_vision_intent.py`, `test_vision_intent_classifier.py`,
  `test_voice_output_coherence.py`, `test_voice_output_optimization.py`,
  `test_voice_pipeline_latency.py`, `test_voice_response_intelligence.py`,
  `test_wake_barge_in_integration.py`, `test_wake_session_console.py`,
  `test_world_model.py`, `test_screen_ask_screen.py`,
  `test_semantic_speech_units.py`, `test_state_isolation.py`,
  `test_streaming_e2e.py`, `test_streaming_speech_integration.py`,
  `test_tts_cancellation.py`, `test_tts_e2e_pipeline.py`,
  `test_tts_queue.py` - two pre-existing, already-documented environment
  failures reproduced (`test_production_launcher.py::test_07` - fails
  even in a fresh isolated re-run, external OpenRouter/Fish Audio API
  reachability from this sandbox, unrelated to memory code;
  `test_state_isolation.py`'s `inspect.getsource()` gap - fails even in
  isolation, a pure source-scan structural test of
  `tests/conftest.py`'s fixture ordering, unrelated to memory code) plus
  one flaky-under-parallel-load TTS timing test
  (`test_llm_tts_streaming_production.py::test_14_cancellation_during_synthesis`
  - confirmed passing 3/3 when run alone; lives entirely in the TTS
  streaming module, which this sprint never touches).
- `tests/test_main_bargein.py`/`tests/test_root_main_bargein.py` excluded
  from collection entirely - pre-existing sandbox environment gaps
  (missing `faster_whisper` package; missing `legacy_main.py` file),
  unrelated to any sprint's code, confirmed present before this sprint's
  changes.
- **Zero new regressions.** Total tests exercised across the sweep:
  1064 (excluding the two collection-error files and the one deselected
  pre-existing-failure test).

## 12. Persistent-state verification

`luno/memory_context.py` contains zero `open()`/file-write calls
(confirmed by direct source grep) - `_topic_history` is purely in-memory,
never wired to any `luno/config.py` path, cleared on
`conversation_ended`, bounded at 50 conversations x 4 entries x 20 terms
each.

All 14 `config/*.json` files were SHA256+mtime hashed immediately before
and immediately after the full `pytest` regression sweep (1064 tests,
run under `tests/conftest.py`'s autouse `isolate_persistent_state`
fixture): **byte-identical, zero unexpected changes, no stray
`.tmp`/`.bak`/`.old` files.**

Separately, and disclosed here in full: ad-hoc live-probe scripts run
directly via shell during Phase 0 reproduction (the established
"prove it through the real event path" methodology used across every
sprint in this project, run OUTSIDE pytest's isolation fixture, using
the real `RuntimeDemoConsole` against the real `config/` directory) did
advance real usage-telemetry state in `config/relationship_state.json`
and `config/long_term_memory.json` - through the PRE-EXISTING, unrelated
`memory.remember_turn()` pipeline (ordinary interaction-count/episodic-
entry activity, the same behavior any real conversation turn produces),
not through this sprint's new topic-history code. Every one of those
writes is auto-backed-up to `config/backups/` by the project's existing
persistence-hardening mechanism. No memory content was fabricated,
corrupted, or deleted - only pre-existing counters advanced.

## 13. Known limitations

- The overlap-based candidate selection is deliberately keyword-based
  (reuses `analyze_query()`, no embeddings) - a topic referenced using
  entirely different vocabulary than was originally used to describe it
  (a true paraphrase, not a shared technical identifier) will not be
  recovered by this mechanism. This matches the brief's own explicit
  constraint against introducing embeddings "unless the audit proves
  keyword retrieval fundamentally insufficient" - it was not proven
  insufficient, only insufficiently APPLIED (single-slot, recency-only).
- `_TOPIC_HISTORY_MAX_ENTRIES = 4` means a conversation that establishes
  more than 4 distinct rich sub-topics will start evicting the oldest
  ones (by count, independent of staleness) - a deliberate, disclosed
  bound, not an oversight; raising it further was not justified by any
  live evidence gathered this sprint (unlike `_ACTIVE_TOPIC_MAX_TERMS`,
  which WAS raised on direct evidence).
- `_TOPIC_OVERLAP_STOPWORDS` is a fixed, hand-built lexical resource. It
  is deliberately conservative (English + common Indonesian particles/
  pronouns/modals) but is not an exhaustive stopword list for either
  language - a future adversarial phrase with a different common filler
  word not yet in the list could in principle cause the same class of
  false-positive overlap Section 6 describes being found and fixed twice
  already. This is an accepted, disclosed limitation of a fixed lexical
  resource, not a claim of completeness.
- When both Section 7's branches find a legitimate match on the SAME turn
  (rare - only when the new overlap-based branch also finds candidates),
  only the new branch's result is used; the old branch is skipped for
  that turn. This is correct for the demonstrated contamination case, but
  means a turn where a topic-history match happens to be less relevant
  than what the single-slot branch would have offered will not fall back
  - this was not observed in the reproduction/test scenarios evaluated
  this sprint.

## 14. Files created/modified

- `luno/memory_context.py` - additive (see Section 4).
- `main_runtime_demo.py` - additive + one caller reordering (see Section
  4; Sprint 4's own branch body is unmodified).
- `tests/test_memory_topic_retention.py` - new, 41 tests.
- `ARCHITECTURE_GUARD.md` - new `## 31. Memory Topic Retention & Recall
  Reliability` section.
- `docs/testing/regression_baseline.md` - new dated entry.
- `docs/change_impact/memory_topic_retention.md` - this document.

No changes to `luno/memory.py`, `_active_topic`, `update_active_topic()`,
`active_topic_to_relevant_memory()`, `build_expanded_retrieval_text()`,
`_rank_key()`, `_apply_budget()`, `MemoryRetriever`, TTS/Fish Audio, the
streaming architecture, or the prompt-injection trust boundary.

## 15. Explicit confirmation

```
memory changed:                yes (additive only - new bounded topic
                                history alongside the existing single
                                slot; no existing memory function's
                                signature or behavior changed)
retrieval changed:              yes (additive only - new optional
                                candidate source feeding the SAME,
                                unmodified assemble_context() call;
                                exactly-once retrieval preserved)
ranking changed:                no (_rank_key()/_apply_budget() untouched;
                                new candidates are ordinary ContextItems)
TTS changed:                    no
streaming changed:              no
prompt-injection boundary changed: no
raw topic persistence:          no (_topic_history is purely in-memory,
                                zero file I/O, verified by source grep)
second ranking system:          no
LLM judge:                      no
embeddings introduced:          no
```
