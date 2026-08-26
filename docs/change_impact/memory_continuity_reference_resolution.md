# Memory Continuity & Short Follow-up Reference Resolution (Sprint 4)

## 1. Problem

Production symptom: user asks "ESP8266 bisa Bluetooth nggak?", Luno
correctly answers (no Bluetooth, use ESP32 or an external module HC-05/
HM-10). User then says "other option?" and Luno gives a vague reply that
acknowledges the INTENT (wants alternatives) but fails to bind the
reference to the active topic (Bluetooth options for ESP8266). Twelve
target short-follow-up phrases: "yang lain?", "yang tadi?", "terus?",
"kalau itu gimana?", "kalau yang lain?", "ada opsi lain?", "terus pilih
yang mana?", "gimana kalau itu?", "what about that?", "other option?",
"and then?", "what about the other one?".

## 2. Root cause (Phase 0, evidence-based, not assumption)

Two independent, compounding gaps, both confirmed via live probes through
the real `RuntimeDemoConsole` event path:

**Gap 1 - intent-classification never fires for this symptom class.**
`luno.memory.classify_query_intent()`'s `_CONTINUATION_INTENT_MARKERS`
list (`lanjut`, `lanjutkan`, `lanjutin`, `terusin`, `terusan`, `terusannya`,
`continue`, `keep going`, `carry on`, `balik ke`, `balik lagi ke`,
`back to`) was built for a narrower "please continue" signal. Running all
12 of the brief's own target phrases through the real classifier returned
`intent="other"` for every single one. This means the EXISTING
`continuation_of_topic` -> `_last_topic_terms` -> `intent_bonus` pipeline
(built in the prior Memory Retrieval & Decision Quality sprint) never
activates for exactly the symptom class this sprint targets.

**Gap 2 - a genuine, previously-undiscovered missing-route bug.** A live
probe published a real turn through the actual event bus, confirmed
`AssistantResponse` was delivered to a direct test subscriber, then
inspected `PlannerBridgeModule._pending_turns` - the entry for that
request_id was STILL PRESENT, unpopped. Tracing why revealed that neither
`main_runtime_demo.py` nor the canonical `luno/bootstrap/modules.py` ever
called `runtime.add_route("assistant_response", "planner")`. Every route
table in the project wires `"user_utterance"` and `"conversation_ended"`
to `"planner"`, but never `"assistant_response"`. This means
`PlannerBridgeModule._on_assistant_response()` - which pairs a turn's
user text with its finalized reply and calls `memory.remember_turn()` -
was dead code via the real, Coordinator-routed event path. This is the
exact same shape of bug as the prior "Conversation_ended Lifecycle
Routing" sprint (§21) fixed for a different event type. It was NOT called
out by that sprint's own audit, and would have silently defeated this
sprint's entire planned mechanism (which depends on
`_on_assistant_response()` firing) had it not been found and fixed first.

**Supporting finding (not a gap, but load-bearing for the design):** a
live probe confirmed `messages` sent to the LLM adapter is ALWAYS
`[{"role": "user", "content": text}]` - the current turn's raw text only,
never conversation history. The entire channel for prior-turn context
reaching the LLM is `system_prompt`, built from `notes`, which only
includes memory context when `assemble_context()` produces non-empty
output. This confirms the brief's own STOP CONDITION did NOT apply here:
context was genuinely ABSENT from the rendered system prompt for a short
follow-up, not present-but-ignored-by-the-LLM. Proceeding to implement a
new mechanism (rather than stopping and blaming prompt instructions) was
the evidence-supported path.

A third static finding, also load-bearing: `assemble_context()`'s hard
early exit (`if not config.enabled or not query.has_any_signal: return
AssembledContext(items=[], ...)`) is computed from `analyze_query(text)`
against the RAW, un-expanded current turn's text, and runs BEFORE
`precomputed_relevant_memories` is ever inspected. For a fully-stopword
follow-up ("what about that?" - every token an English stopword,
`has_any_signal=False`), simply appending a candidate to the caller's
retrieved-memories list would not help, because `assemble_context()`
returns empty before that list is ever read. This had to be addressed for
the mechanism to cover the brief's own "what about that?" example, not
just the phrases with at least one non-stopword token.

## 3. Existing mechanisms reused, left completely unmodified

- `analyze_query()` / `token_overlap()` (`luno/memory_retrieval/query.py`)
  - the ONLY tokenizer used anywhere in this sprint's new code.
- `_compile_word_boundary_marker_pattern()` (`luno/memory.py`) - the same
  regex-compilation helper `classify_query_intent()` already uses, reused
  verbatim by the new `classify_reference_type()`.
- `ContextItem`, `_rank_key()`, `_apply_decision_quality_bonus()`,
  `deduplicate_context_items()`, `_apply_budget()`, `render_context_block()`
  (`luno/memory_context.py`) - completely unmodified. The new synthetic
  active-topic candidate is an ordinary `RelevantMemory` ->
  `relevant_memory_to_context_item()` -> `ContextItem`, flowing through
  the exact same pipeline as every other candidate.
- `_last_topic_terms` / `continuation_of_topic` / `intent_bonus`
  (Memory Retrieval & Decision Quality sprint) - completely unmodified.
  Not read, not written, not repurposed by this sprint's new code. A
  SEPARATE, additional `_active_topic` dict was added alongside it because
  `_last_topic_terms` is gated on an intent value (Gap 1) that never fires
  for this symptom class - widening that gate would have changed an
  already-tested contract other sprints pin.
- `MemoryRetriever.retrieve_memories()` - called exactly once per turn,
  unchanged. The synthetic candidate is constructed directly (never via a
  second registered `MemorySource`) and appended to a caller-side list,
  never triggering a second retrieval call.
- `relevant_memory_to_context_item()`'s `relevance=rm.score` (never
  recomputed from keyword overlap) - the specific property that makes it
  possible for a signal-less "other option?" to surface topic content it
  doesn't itself lexically match, while still being fully subject to
  ordinary relevance-first ranking once converted to a `ContextItem`.
- `render_context_block()`'s existing fenced-section rendering / prompt-
  injection trust boundary - unmodified, and verified (via a new test)
  that a malicious phrase inside a stored active-topic candidate still
  renders as inert quoted content, never escaping into the top-level
  final-instruction block.

## 4. Exact production files changed

- `luno/memory.py` - additive: `REFERENCE_TYPES`,
  `NEEDS_TOPIC_CONTEXT_TYPES`, `classify_reference_type()`,
  `needs_topic_context()`, `_PURE_REFERENCE_TYPES`,
  `is_pure_reference_followup()`.
- `luno/memory_context.py` - additive: `_ACTIVE_TOPIC_MAX_TERMS`,
  `_ACTIVE_TOPIC_MAX_AGE_TURNS`, `_ACTIVE_TOPIC_CANDIDATE_SCORE`,
  `ActiveTopicSnapshot`, `extract_topic_terms_from_turn()`,
  `update_active_topic()`, `build_expanded_retrieval_text()`,
  `active_topic_to_relevant_memory()`; `assemble_context()` gained one new
  optional keyword parameter, `retrieval_query_override: Optional[str] =
  None` (default preserves every existing call site byte-for-byte).
- `main_runtime_demo.py` (`PlannerBridgeModule`) - `_pending_turns` type
  changed from `Dict[str, str]` to `Dict[str, Tuple[str, Optional[str]]]`
  (now also stores `conversation_id`); new `_active_topic: Dict[str,
  ActiveTopicSnapshot]` + `_active_topic_max`; `_remember_pending_turn()`
  gained an optional `conversation_id` parameter; `_on_assistant_response()`
  extended to update `_active_topic` after `remember_turn()`;
  `_on_conversation_ended()` extended with one new `.pop()` line for
  `_active_topic`; `_handle_utterance()` extended to classify the turn,
  read the snapshot, and (only when relevant) build the expanded query and
  synthetic candidate before the existing `assemble_context()` call; ONE
  new route, `self.runtime.add_route("assistant_response", "planner")`.
- `luno/bootstrap/modules.py` - the same one new route, kept byte-for-byte
  in sync per this file's own established mirroring convention.
- `tests/test_memory_continuity.py` - new, 60 tests.

## 5. Short-follow-up detection design

`classify_reference_type(text)` - deterministic, regex-based, precedence
order (first match wins): `negation_of_current_option` (`tanpa X`/
`without X`) -> `cost_comparison` (`lebih murah`/`cheaper`) ->
`alternative_request` (`yang lain`/`other option`/`the other one`) ->
`continuation` (bare `terus`/`and then`) -> `comparison` (a comparison
marker like `gimana`/`vs` PLUS a residual named entity, e.g. "ESP32
gimana?") -> `direct_reference` (a comparison marker with NO residual
entity, e.g. "kalau itu gimana?", or standalone `kalau itu`/`what about
that`/`yang tadi`) -> `unknown` (no pattern matched at all - includes
every genuinely rich, self-contained question).

Two derived predicates serve two DIFFERENT questions, deliberately kept
separate after an early implementation bug (see §14 Known Limitations):

- `needs_topic_context(text)` = `True` for every type except `unknown` -
  "does this turn benefit from retrieval-query expansion?" Used to decide
  whether to build the expanded retrieval string / inject the candidate
  for THIS turn's own retrieval (Phase 4).
- `is_pure_reference_followup(text)` = `True` only for
  `alternative_request` / `continuation` / `direct_reference` /
  `cost_comparison` - types that, by construction of their own regexes,
  carry NO standalone named entity of their own. `comparison` and
  `negation_of_current_option` are excluded because their regexes
  REQUIRE a residual real word ("WLED", "MQTT") - such a turn should
  REPLACE the active-topic snapshot, not merely borrow from it. Used to
  decide whether `update_active_topic()` should REPLACE or PRESERVE the
  snapshot (Phase 3/5/6).

## 6. Active-topic representation

`memory_context.ActiveTopicSnapshot(terms: frozenset, turns_since_active:
int = 0)`, plus an `is_stale` property (`turns_since_active >
_ACTIVE_TOPIC_MAX_AGE_TURNS = 6`). Held in `PlannerBridgeModule._active_topic:
Dict[str, ActiveTopicSnapshot]`, keyed by `conversation_id` (same sentinel
fallback as `_last_topic_terms`), bounded at 50 conversations (same
eviction pattern), popped on `conversation_ended`. Purely in-memory,
never wired to any `luno/config.py` persistent path (verified by grep),
never holds raw utterance/reply text - only a bounded `frozenset` of
tokens (max 12), extracted via the existing tokenizer from BOTH the
user's text and the assistant's finalized reply (`extract_topic_terms_from_turn()`),
merged and de-duplicated, order-preserving up to the limit.

**Update rule** (`update_active_topic()`, called from
`_on_assistant_response()` - the one place both this turn's user text and
finalized reply are simultaneously available): a "rich" turn
(`is_pure_reference_followup()` false) REPLACES the snapshot entirely,
resetting `turns_since_active` to 0; a pure-reference follow-up PRESERVES
the existing terms and increments `turns_since_active` by 1. This single
rule, with no special-case code, is what makes topic decay (§8), branch
switching (§8), and false-carry-over safety (§9 of the original brief)
all work correctly - verified via dedicated live E2E probes for each
before being formalized into the test suite.

## 7. Retrieval expansion behavior

`build_expanded_retrieval_text(text, snapshot)` appends the snapshot's
bounded terms to (never replaces) the current turn's own text, returning
a plain string used ONLY for retrieval matching. `active_topic_to_relevant_memory(snapshot,
turn_id)` constructs a `RelevantMemory(source="active_conversation",
score=0.55 fixed, text="Active conversation topic: <sorted terms>")`.
Both are `None`/pass-through no-ops when there is no snapshot, an empty
snapshot, or a stale one.

In `PlannerBridgeModule._handle_utterance()`, this only ever engages when
`needs_topic_context(text)` is true AND a non-stale snapshot exists for
the conversation. The synthetic candidate is appended to a NEW list
(`relevant_memories_for_context`), deliberately never written back into
`relevant_memories_early` itself - every OTHER consumer of that turn's
retrieval this method already has (usage tracking, session-feedback-
target, the routing Decision Engine, turn-trace telemetry, response
selection) never sees the synthetic candidate. The ORIGINAL, un-expanded
`text` is what gets sent to the LLM (`messages = [{"role": "user",
"content": text}]`, unchanged) and is the only thing threaded through
every other part of the turn - the expansion is used exclusively as
`assemble_context()`'s new `retrieval_query_override` argument, never
persisted, never logged as user-authored text.

`assemble_context(retrieval_query_override=...)` substitutes the expanded
string for the `has_any_signal` gate and for the retriever-call query
(when no `precomputed_relevant_memories` was supplied) ONLY -
`query_category` classification and everything else continues to use the
original `text`. Omitting the parameter (every caller/test before this
sprint) is byte-for-byte identical to previous behavior - verified by a
dedicated backward-compatibility unit test.

## 8. Topic decay / branch switching

Both fall out of the single replace-vs-preserve rule in §6, with no
dedicated decay/branch-tracking code:

- **Decay** (5-turn scenario: ESP8266/Bluetooth -> "yang lain?" (preserve)
  -> WLED (rich, replace) -> MQTT (rich, replace) -> "yang lain?"
  (preserve)) - verified live: the final turn's rendered prompt contains
  MQTT/broker/Mosquitto terms and does NOT contain "bluetooth".
- **Branching** ("ESP8266 bisa Bluetooth?" -> "Kalau WLED gimana?" (a
  `comparison`-typed turn - rich by `is_pure_reference_followup()`,
  replaces) -> "yang lain?") - verified live: the final prompt contains
  WLED/LED terms and does NOT contain "bluetooth". An early
  implementation attempt used `needs_topic_context()` (not
  `is_pure_reference_followup()`) as the replace/preserve gate, which
  incorrectly PRESERVED the Bluetooth snapshot through the WLED turn
  (since `comparison` also satisfies `needs_topic_context()`) - caught
  immediately by this exact live probe and fixed by introducing the
  narrower `is_pure_reference_followup()` predicate (§5).
- **Bounded age decay** - `_ACTIVE_TOPIC_MAX_AGE_TURNS = 6`; a snapshot
  older than that is `is_stale` and both `build_expanded_retrieval_text()`
  and `active_topic_to_relevant_memory()` treat it as absent.

## 9. Ranking/budget impact

The synthetic candidate is an ordinary `ContextItem` once constructed -
zero special-casing in `_rank_key()`, `deduplicate_context_items()`, or
`_apply_budget()` (none of these three functions were touched). Verified
by unit test: a real, higher-relevance memory (`score=0.95`) ranks first
ahead of the synthetic candidate (`score=0.55` fixed) even when both
share the same expanded retrieval query; and under a tight budget
(`max_results=1`) with several higher-relevance real items present, the
synthetic candidate is the one dropped, never privileged. Continuity can
only ever surface a candidate for consideration - relevance-first ranking
and budget enforcement decide whether it survives, exactly as every other
candidate.

## 10. Production-path E2E proof

16 tests run through a real `RuntimeDemoConsole` (Event Bus ->
`PlannerBridgeModule` -> real `assemble_context()` -> the actual rendered
`system_prompt`, inspected directly - not helper-unit-only), exceeding
the brief's own "at least 2" requirement several times over:

1. ESP8266 -> Bluetooth -> "other option?" - `bluetooth` present in the
   follow-up's rendered prompt.
2. WLED -> controller -> "yang lain?" - `wled`/`esp32` present.
3. HA VPS -> Tailscale -> "kalau tanpa itu?" - `tailscale` present.
4. Fully signal-less "what about that?" - proves the
   `retrieval_query_override`/`has_any_signal` gate fix specifically
   matters end-to-end (without it this scenario would still fail).
5. 5-turn topic decay (§8).
6. Branch switching (§8).
7. Two false-carry-over scenarios (aquascape, GPU) - the new topic's
   terms present, the superseded topic's terms absent.
8. Conversation isolation - a second, unrelated conversation never
   inherits the first's active topic.
9. Conversation-ID reuse after `conversation_ended` - the reused ID does
   not inherit the ended conversation's topic.
10. Empty-topic pure follow-up (no prior turn at all) - safe no-op, no
    crash.
11. English ("Can the ESP8266 do Bluetooth?" -> "other option?").
12. Mixed Indonesian/English ("ESP8266 support Bluetooth ga sih?" ->
    "what about the other one?").
13. Explicit new subject overriding old topic via an ordinary rich turn
    (not a reference shape at all).
14. Bounded state (50-conversation eviction) confirmed on the real
    `PlannerBridgeModule` instance.
15. Prompt-injection inertness - an injected-instruction-shaped phrase
    inside a stored assistant reply is confirmed to never appear after
    the real `system_prompt`'s own "IMPORTANT, FINAL INSTRUCTION"
    boundary.

## 11. Test results

`tests/test_memory_continuity.py`: **60 passed, 0 failed** (41 unit-level
+ 16 real production-path E2E + 3 structural "no second tokenizer"/"no
LLM judge"/"candidate never privileged" guards). Full run time ~5s.

## 12. Full regression results

Targeted, high-relevance suites run in batches (all memory-related files,
`test_runtime_demo.py`, `test_response_output.py`,
`test_voice_output_coherence.py`, `test_voice_response_intelligence.py`,
`test_voice_output_optimization.py`, `test_voice_pipeline_latency.py`,
`test_conversation_ended_lifecycle_routing.py`, `test_state_isolation.py`,
`test_response_policy.py`, `test_proactive.py`,
`test_wake_barge_in_integration.py`, `test_production_launcher.py`, plus
the new file - roughly 1050 tests total): only the SAME 2
already-documented, pre-existing failures reproduced -
`test_production_launcher.py::test_07_health_checks_all_pass_in_default_mock_configuration`
(environment-specific: this sandbox has no live network access to
openrouter.ai/api.fish.audio) and
`test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`
(sandbox `inspect.getsource()` gap, documented since early in
`docs/testing/regression_baseline.md`). **Zero new failures.** Relevance
remains the dominant `_rank_key()` signal, importance remains subordinate,
source priority/dedup/conflict-handling/exactly-once-retrieval/prompt-
injection-boundary all unchanged and re-verified passing by the existing
suites that pin them.

## 13. Persistent-state verification

`_active_topic` confirmed purely in-memory (grepped `luno/config.py` for
any wiring - none found). All 14 `config/*.json` files SHA256+mtime
snapshotted before/after the formal `pytest` run (which executes under
`tests/conftest.py`'s autouse `isolate_persistent_state` fixture) -
byte-identical, zero unexpected changes, no stray `.tmp`/`.bak`/`.old`
files, no new config file introduced for topic state.

**Disclosed transparently:** this sprint's own ad-hoc live-probe scripts
(run directly via shell for Phase 0/5/6/9/10 verification - the
established "prove it through the real event path" methodology every
prior sprint in this project has also used) constructed
`RuntimeDemoConsole` instances against the REAL project `config/`
directory rather than an isolated temp path (unlike the formal `pytest`
suite, which is always isolated). This advanced real usage-telemetry
counters - `config/relationship_state.json`'s `interaction_count` and
`last_interaction_timestamp`, and a few `config/long_term_memory.json`
entries' `retrieval_count`/`last_retrieved_at`/`usefulness_score` fields
(pre-existing entries about aquascape/RGB/Unity, retrieved because this
sprint's own false-carry-over test scenarios legitimately mention those
topics). Verified programmatically: `long_term_memory.json`'s entry count
is unchanged (5) and every entry's `text` field is byte-identical to
before - no memory was fabricated, corrupted, or deleted, only pre-
existing usage counters advanced, consistent with ordinary conversational
use of the real console.

## 14. Known limitations

- **Hyphenated compound identifiers lose their numeric suffix in topic
  terms.** `analyze_query()`'s existing tokenizer splits "HC-05" into
  "hc" and drops the bare "05" (a pure-digit token); the active-topic
  snapshot therefore stores "hc"/"hm" rather than "hc-05"/"hm-10". The
  mechanism still works (the co-occurring terms `bluetooth`/`modul`/
  `murah` remain distinctive enough, verified live), but a test asserting
  the literal hyphenated string will not find it. Not fixed - the
  existing tokenizer is shared, load-bearing infrastructure across this
  entire project; changing its splitting behavior was out of scope
  ("no second tokenizer" also implies not silently changing the one
  tokenizer's behavior for one caller) and risked broad regressions.
- **Indonesian function words are not stripped from topic terms** (the
  existing `_STOPWORDS` set in `luno/memory_retrieval/query.py` is
  English-only, unmodified by this or any sprint) - words like "atau",
  "yang", "kalau" appear in the active-topic candidate's rendered text
  alongside the genuinely distinctive terms. Harmless for retrieval
  (candidate relevance is a fixed score, not keyword-matched) and for the
  LLM (extra low-information words in a labeled "Active conversation
  topic:" line), but noted as a cosmetic imprecision.
- **`is_pure_reference_followup()`'s replace/preserve rule is turn-shape-
  based, not semantic.** A turn that happens to match no reference
  pattern (`unknown`) is always treated as "rich" and replaces the
  snapshot - including a short, low-content but non-matching utterance
  ("hi", "ok", "haha"). In practice this is safe (nothing meaningful is
  lost, and the OLD snapshot simply becomes unavailable one turn earlier
  than ideal), but it is a coarser signal than true semantic richness.
- **The missing-route fix (Gap 2) has a broader blast radius than this
  sprint's own mechanism** - `memory.remember_turn()` / `session_log`
  (which feeds end-of-session summarization, `summarize_and_archive_session()`)
  was ALSO dead code via the real routed path before this fix, for every
  prior sprint's lifetime. This fix makes session-summary archiving work
  correctly in production for the first time, which is a beneficial but
  out-of-brief side effect worth flagging explicitly.
- **Ad-hoc live-probe scripts used the real `config/` directory** (see
  §13) - a testing-methodology limitation of this environment rather than
  of the shipped mechanism, but disclosed for completeness.

## 15. Files created/modified

Created: `tests/test_memory_continuity.py`,
`docs/change_impact/memory_continuity_reference_resolution.md` (this
file). Modified: `luno/memory.py`, `luno/memory_context.py`,
`main_runtime_demo.py`, `luno/bootstrap/modules.py`,
`ARCHITECTURE_GUARD.md` (new §30), `docs/testing/regression_baseline.md`
(new dated entry).

## 15b. Follow-up round addendum

The same sprint was re-issued with the same objective, additional target
phrases, and an explicit 30-item test matrix. Per its own instruction
("Phase 0 audit already established the root cause... do not repeat the
entire reconnaissance"), no new reconnaissance was performed - the
existing fix (§1-16 above) was verified still in place, then tested
against the brief's own NEW target phrases.

**Real classifier gaps found:** "anything else?", "what else?", "kalau
alternatifnya?" matched nothing (`unknown`, `needs_topic_context()=False`
- these follow-ups would have been treated as ordinary independent
queries). "yang lainnya gimana?" and "how about another one?" matched
`comparison` instead of `alternative_request` - because `comparison`
requires (and found) a residual non-filler token ("lainnya", "another"/
"one"), which is EXCLUDED from `_PURE_REFERENCE_TYPES`, so
`is_pure_reference_followup()` would have wrongly returned `False` for
these two phrases - meaning `update_active_topic()` would REPLACE the
active topic instead of preserving it, silently discarding the real
context these follow-ups need. Fixed by extending
`_ALTERNATIVE_REQUEST_RE` (`luno/memory.py`) with `yang lainnya`/`opsi
lainnya`/`pilihan lainnya`/`alternatifnya`/`another one`/`anything else`/
`what else` (7 new alternation branches) - `alternative_request` has
higher precedence than `comparison` in `classify_reference_type()`'s own
chain, so these phrases are now caught before ever reaching the
comparison check. Re-verified all 13 previously-passing worked examples
unaffected.

**Expanded test suite:** `tests/test_memory_continuity.py` grew from 60
to 77 tests. New coverage: the fixed phrases + a regression guard for the
existing mappings; explicit-continuation-of-topic backward compatibility;
independent-query non-detection; word-boundary adversarial negatives
("lanjut" vs "selanjutnya", "other" vs "brother"/"otherwise",
"alternatifnya" vs an unrelated sentence); `remember_turn()` called
exactly once per turn (via a counting `monkeypatch`); a literal
reproduction of the ORIGINAL missing-route bug (captures the real
`Coordinator` subscription id for `"assistant_response" -> "planner"` by
wrapping `add_route()` before console construction, unsubscribes it, then
proves `memory.session_log` stays empty for that turn - the exact pre-fix
production behavior); real-thread concurrent multi-conversation isolation;
and a dedicated structural assertion that `ActiveTopicSnapshot` can never
contain a raw, multi-word sentence.

**Two test-authoring bugs found and fixed while writing this batch** -
both are worth recording explicitly, because they are the SAME class of
mistake ("avoid substring collision") the original brief itself warned
about for the classifier, just found in test code instead:

1. The first draft of the concurrent-isolation test subscribed to
   `need_llm_response` without filtering by `request_id`. Under true
   thread concurrency, one thread's subscriber callback could capture the
   OTHER thread's event (the event bus delivers one global event stream;
   nothing about the subscription itself scopes it to "the request this
   particular call published"). This produced a false "conversation A
   leaked conversation B's topic" failure that looked exactly like a
   production isolation bug. Root-caused by writing a standalone
   reproduction script outside pytest first (which passed cleanly),
   proving the underlying mechanism was correct and the bug was in the
   test harness. Fixed by filtering `_capture()` on `e.get("request_id")
   == request_id`.
2. Several E2E assertions used a naive `"wled" in prompt.lower()`
   substring check. This is UNCONDITIONALLY `True` for every single
   rendered prompt in this project, regardless of actual topic content,
   because the static, always-present persona block contains the English
   word "knowledgeable" - `kno`**`wled`**`geable`. This means
   `test_E2E_B`/`test_E2E_F` (and the concurrent-isolation test) had been
   silently passing "for the wrong reason" - not actually proving
   WLED-specific content ever reached the prompt. Found by tracing an
   unexpected exact match through raw log output line-by-line rather than
   assuming the mechanism was at fault. Fixed with a new `_word_in()`
   regex word-boundary helper (`\bword\b`), applied everywhere a short,
   collision-prone term ("wled", "led") was checked. Re-verified the
   concurrent test passes consistently across repeated runs, not as a
   one-off.

**Regression (this round):** ~1200 tests across all memory-related
suites, `test_runtime_demo.py`, `test_response_output.py`,
`test_voice_output_coherence.py`, `test_voice_response_intelligence.py`,
`test_voice_output_optimization.py`, `test_voice_pipeline_latency.py`,
`test_conversation_ended_lifecycle_routing.py`, `test_state_isolation.py`,
`test_response_policy.py`, `test_proactive.py`,
`test_wake_barge_in_integration.py`, `test_production_launcher.py` - only
the same 2 already-documented pre-existing failures. Persistent state:
all 14 `config/*.json` files SHA256+mtime byte-identical before/after
this round's test run, no stray temp/backup files.

## 16. Explicit confirmation

```
Production code changed: YES
Memory retrieval changed: NO (MemoryRetriever/retrieve_memories() untouched; a synthetic candidate is constructed alongside it, never a second retrieval call)
Memory ranking changed: NO (_rank_key()/_apply_budget()/deduplicate_context_items() untouched)
Topic state changed: YES (new, additive, in-memory-only _active_topic; existing _last_topic_terms untouched)
Persistent memory schema changed: NO
TTS changed: NO
Streaming changed: NO
Second retriever introduced: NO
Second ranker introduced: NO
LLM judge introduced: NO
Second tokenizer introduced: NO
```
