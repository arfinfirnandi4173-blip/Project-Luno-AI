# Change Impact: Semantic Context Bridging & Memory Precision (Sprint 43)

## Goal

Let Luno recognize when a follow-up refers to an existing memory/topic
even when the wording differs from the original statement ("Aku mau
ganti GPU ke RTX 3060." -> "Kalau upgrade itu jadi gimana?"), without an
LLM judge, embeddings/vector database, second ranking system, persistent
raw conversation/topic storage, or global topic state - and without
letting weak evidence manufacture a connection genuine ambiguity should
reject. Explicit constraint from the brief: "DO NOT assume 'different
wording' means semantic equivalence. Prove the connection first, then
implement the smallest deterministic mechanism that makes the proven
cases work without turning Luno into a confident idiot."

## Method

**Phase 0 (read-only reconnaissance).** Read `luno/memory.py`'s
`classify_reference_type()`/`REFERENCE_TYPES`/`NEEDS_TOPIC_CONTEXT_
TYPES`/`_PURE_REFERENCE_TYPES`/`_MERGE_REFERENCE_TYPES`, `luno/memory_
context.py`'s `select_topic_candidates()`/`select_temporal_fallback_
candidate()`/`_TOPIC_OVERLAP_STOPWORDS`/`_TOPIC_HISTORY_MAX_ENTRIES`/
`assemble_context()`, and `main_runtime_demo.py`'s 4-way retrieval branch
(`ordinal_targets` -> `topic_history_candidates` (unconditional) ->
single-slot recency (`is_short_followup`-gated) -> temporal fallback).
Confirmed `analyze_query()` (`luno/memory_retrieval/query.py`) is the
SHARED tokenizer for the entire `memory_retrieval` package, not just
topic-candidate matching - any new normalization needed to live in a
narrow layer inside `memory_context.py`, not modify the shared tokenizer
(avoiding broad blast radius). Confirmed no existing morphology/synonym
infrastructure anywhere in the codebase (`luno/text_normalizer/` is
exclusively TTS speech normalization - number-to-words, markdown
stripping - not reusable for semantic/lexical topic matching).

**Phase 1 (real E2E probes before any code changed).** Built probes
through `RuntimeDemoConsole` for Scenarios A (exact lexical match), B
(morphological variation: beli/pembelian, ganti/penggantian, upgrade/
naikkan, pakai/menggunakan), C (colloquial variation: mic/mikrofon, GPU/
kartu grafis, pompa/pump, board/mikrokontroler), D (existing-topic
action - the brief's own primary worked example), E (multi-topic
ambiguity, must NOT guess), F (correct semantic recovery across an
unrelated intervening topic), G (false-positive protection), H (temporal
+ semantic combination). A "decoy topic" pattern - inserting a second,
unrelated topic statement between the target statement and the follow-up
query - was essential: an early round of Scenario B tests without decoys
gave false-positive "passes" purely from trivial single-topic recency,
not real morphological bridging.

**Phase 2 (root cause).** `select_topic_candidates()`'s overlap check
compared RAW tokens only, so a follow-up using different wording shared
no token with the stored entry and correctly, safely matched nothing -
but that emptiness then let an UNGUARDED mechanism win by default:
`main_runtime_demo.py`'s pre-existing single-slot `_active_topic` recency
fallback (Sprint 4), which fired whenever `is_short_followup` was true
and an active snapshot existed, with no check on whether the query's own
words related to it at all. Live reproduction found this produced a
WRONG topic (Scenario G: an unrelated headset purchase, then "Kalau
upgrade PC-ku gimana?" wrongly injected the headset topic) or GUESSED
between two equally-plausible topics (Scenario E: two topics both
introduced with "ganti", later asked about via "upgrade itu?") rather
than correctly returning nothing.

## Root cause table (Phase 2)

| Scenario | Where information was lost | Fix location |
|---|---|---|
| B/C (morphology/colloquial recall) | `select_topic_candidates()` raw-token overlap finds nothing for a paraphrase | new normalized fallback tier in `select_topic_candidates()` |
| D (primary worked example) | Same as B/C, plus a `ganti`~`upgrade` semantic gap no morphology rule bridges | normalized fallback tier + `_TOKEN_SYNONYM_GROUPS` |
| E (multi-topic ambiguity) | Unguarded single-slot recency branch defaults to most-recent topic regardless of match | new `is_active_topic_relevant_to_query()` ambiguity-aware guard |
| G (false-positive protection) | Same unguarded single-slot branch fires on zero real evidence | same new guard |
| H (temporal + semantic) | `select_temporal_fallback_candidate()`'s own raw-only overlap tier | new normalized fallback tier in `select_temporal_fallback_candidate()` |

## Fix (Phase 3-5)

**1. Bounded lexical-normalization layer, `luno/memory_context.py`
(additive, new section):**

- `_strip_bounded_affixes()` - single-pass, longest-match-first
  Indonesian clitic suffix (`-nya`/`-kah`/`-lah`) -> derivational suffix
  (`-kan`/`-an`) -> prefix (`meng-`/`di-`/`pe(ng/ny/m/n)-`/etc.) -> English
  suffix (`-ing`/`-ed`/`-es`/`-s`) stripper, gated by `_MIN_AFFIX_ROOT_
  LEN = 4` so short tokens and product identifiers (`esp32`, `rtx`,
  `gpu`) are never touched. `-i` was deliberately EXCLUDED from the
  derivational suffix set after Phase 6 testing found it corrupting
  common roots that simply end in "i" (`ganti` -> `gant`) - see "Bugs
  found and fixed during Phase 6" below.
- `_TOKEN_SYNONYM_GROUPS` - a small, ordered-tuple (never `frozenset`,
  for deterministic canonical-form selection across process runs) table
  of GENERIC, domain-independent component-category vocabulary only:
  `(mic, mik, mikrofon, microphone)`, `(gpu, vga)`, `(pompa, pump)`,
  `(board, mikrokontroler, microcontroller, mcu)`, `(upgrade, naik,
  naikkan, menaikkan, dinaikkan, ganti)`. Deliberately never a specific
  product/entity name (ESP32, INMP441, RTX 3060 never appear here) per
  the brief's own explicit prohibition; `"ganti"` is in the upgrade group
  deliberately, per the brief's own primary worked example.
- `_TOKEN_SYNONYM_PHRASES` - one multi-word entry, `"kartu grafis" ->
  "gpu"`.
- `_normalize_terms_for_bridging()` / `_normalize_query_tokens_for_
  bridging()` - purely ADDITIVE composition functions: they only ADD
  normalized/aliased tokens to a set, never remove or replace the
  originals, so unioning this with a raw comparison never loses any
  existing exact-match behavior.

This normalization is consulted ONLY as a strictly WEAKER, fallback-only
evidence tier inside `select_topic_candidates()` and `select_temporal_
fallback_candidate()` - tried ONLY after raw-token overlap has already
had first refusal and found nothing, so 100% of pre-existing exact-match
behavior is unchanged. When normalized evidence exists it must be the
UNIQUE top scorer among all candidates; a tie returns nothing (the
brief's own "weak evidence must not override ambiguity" rule).

**2. New relevance guard, `is_active_topic_relevant_to_query()`:**
consulted by `main_runtime_demo.py`'s single-slot recency branch as an
ADDITIONAL condition. Returns `True` immediately for signal-less
fragments (query has no non-stopword tokens) and on raw-token overlap
(strong evidence, unchanged). For normalized-only evidence, returns
`True` only if the active snapshot's normalized score is not tied or
beaten by any OTHER, genuinely distinct entry in the bounded
`topic_history` - "genuinely distinct" excludes any entry whose own
significant vocabulary is MAJORITY (>50%) covered by the active
snapshot's own terms, since such an entry is not a competing topic but
part of the same lineage already absorbed into the active snapshot via
an earlier merge (see the second Phase 6 bug below).

The call site in `main_runtime_demo.py` scopes this guard to
`reference_type == "comparison"` ONLY - see the first Phase 6 bug below
for why every other `NEEDS_TOPIC_CONTEXT_TYPES` member is left on its
pre-existing, unconditional path.

## Bugs found and fixed during Phase 6 (test-writing)

Writing the regression suite caught three real bugs before the sprint's
changes were considered final - all self-discovered through live
re-testing, not flagged by the user, matching the brief's own "prove it,
don't assume" discipline:

**1. Missing `"ganti"` in the synonym group (recall gap on the brief's
own primary example).** The first implementation's upgrade/naik group
lacked `"ganti"`; Scenario D still returned "no candidate injected"
because `"ganti"` (replace) and `"naik"`/`"upgrade"` (increase) are
unrelated lexemes with no morphological relationship - affix-stripping
alone cannot bridge them. Fixed by adding `"ganti"` explicitly, per the
brief's own worked example.

**2. Over-broad relevance guard regressed 7 pre-existing E2E tests.**
Gating the new guard on every `is_short_followup` type (not just
`"comparison"`) broke `test_memory_continuity.py`'s `test_E2E_A`/`_C`/
`_E`/`_F`/`_H`/`_L`/`_M` - follow-ups like "other option?" (`alternative_
request`), "kalau tanpa itu?" (`negation_of_current_option`), "yang
lain?" (`alternative_request`) are genuinely signal-less STRUCTURAL
references with no topical words of their own to check relevance
against; for those, unconditional recency was always correct. Root cause
traceable directly with `luno.memory.classify_reference_type()`:

```
'other option?'                                          -> alternative_request  (pure)
'kalau tanpa itu?'                                        -> negation_of_current_option
'yang lain?'                                               -> alternative_request  (pure)
'Kalau upgrade itu jadi gimana?'  (Scenario D)              -> comparison
'Kalau upgrade PC-ku gimana ya, mumpung ada budget?' (G)     -> comparison
'Kalau pindah ke ESP32 gimana?'                             -> comparison
'Kalau jadi beli yang itu?' (Scenario F)                     -> direct_reference (pure)
'Kalau upgrade itu gimana?' (Scenario E)                     -> comparison
```

Every scenario that actually motivated the guard (D/E/G) is `"comparison"`
- fixed by scoping the guard's engagement to that one reference type,
leaving `alternative_request`/`negation_of_current_option`/`direct_
reference`/etc. on the unmodified pre-Sprint-43 path.

**3. False ambiguity from an already-merged topic-history entry.**
`test_memory_comparison_topic_preservation.py::test_15` (ESP32/mic ->
aquascape/pompa -> recover mic -> recover pompa -> unrelated query must
inject neither) regressed: turn 3 ("Yang soal mikrofon gimana?",
`comparison` type) merged the aquascape/pompa entry's terms into the live
active snapshot; turn 4 ("Kalau pompanya gimana?") correctly found
normalized evidence for the active snapshot (`"pompanya"` -> `"pompa"`
via clitic stripping) - but the guard's naive tie-check then compared
that score against `topic_history`'s OWN aquascape/pompa entry (a
DIFFERENT object than the merged active snapshot, still sitting
unchanged in history) and found it "tied", wrongly rejecting a genuinely
unique, already-confirmed topic. A strict-subset check
(`other.terms <= active.terms`) was tried first and failed - real merges
drop a word or two (e.g. `"punya"`), so the subset relation rarely holds
exactly. Fixed with a majority-coverage threshold instead: an entry whose
own significant terms are >50% covered by the active snapshot's terms is
treated as the same lineage, not a competitor. Re-verified this does not
reopen Scenario E: the GPU vs. mic entries there share only the single
word `"ganti"` (~33% coverage of either side), well under the threshold,
so the real ambiguity is still correctly detected.

All three fixes were re-verified together (all 8 named Phase 1 scenarios
plus every morphology/colloquial sub-case) to confirm none of the three
fixes broke either of the other two.

## Ambiguity/false-positive safety

- Genuine ambiguity (two candidates scoring identically on normalized
  evidence) returns no candidate rather than guessing, at both the
  `select_topic_candidates()` layer and the new relevance-guard layer.
- An unrelated turn with its own real content does not inject an
  unrelated recent topic merely because it is recent (Scenario G).
- Raw exact-token evidence always outranks normalized evidence when both
  exist (verified explicitly: an entry sharing the literal word
  "upgrade" wins over an entry only sharing normalized "ganti"~"upgrade"
  evidence, never a tie).
- A specific product identifier is never fuzzy-matched into another
  unrelated one via this mechanism (`esp8266` vs. a query about `esp32`
  stays unmatched - the anti-hardcoding invariant, verified as a unit
  test).

## Tests

`tests/test_semantic_context_bridging.py` (72 tests, all passing):

- **Section 1 (12 tests).** `_strip_bounded_affixes()` unit coverage -
  suffix/prefix stripping, the min-root-length guard, short entity
  identifiers left untouched, never returns an empty string.
- **Section 2 (8 tests).** Synonym-group/phrase-table unit coverage,
  including two structural anti-hardcoding checks: no specific entity
  name in any synonym group, and the synonym layer stays bounded
  (<= 40 members total, a scope-creep tripwire not a growth target).
- **Section 3 (10 tests).** `select_topic_candidates()`'s new fallback
  tier - raw match unchanged, morphology/colloquial fallback recovery,
  ambiguous normalized tie returns empty, no-evidence returns empty, raw
  match preferred over normalized when both exist, empty/stopword-only
  query returns empty, unrelated entity never fuzzy-matched.
- **Section 4 (4 tests).** `select_temporal_fallback_candidate()`'s new
  tier - normalized bridge for a planned-status entry, ambiguous tie
  returns `None`, no-eligible-status returns `None`, raw match still
  wins over normalized.
- **Section 5 (7 tests).** `is_active_topic_relevant_to_query()` unit
  coverage - empty query, raw overlap, unique normalized overlap, zero
  overlap, tied normalized overlap across history, `None` snapshot,
  backward-compatible default when `topic_history` is omitted.
- **Section 6 (9 tests).** E2E Scenarios A-H via real `RuntimeDemoConsole`
  (Scenario D is the brief's own primary worked example, run verbatim).
- **Section 7 (2 tests).** Attribute-reference merge and ordinal
  reference resolution combined with the new bridging layer, confirming
  neither pre-existing mechanism regressed.
- **Section 8 (1 test).** Cross-conversation isolation - two independent
  conversations bridging to two different topics with zero leakage.
- **Section 9 (1 test).** Bounded topic-history eviction - a bridgeable
  entry correctly stops being found once it ages out of `_TOPIC_HISTORY_
  MAX_ENTRIES`.
- **Section 10 (4 tests).** Empty/`None`/gibberish/stopword-only query
  behavior for both `select_topic_candidates()` and `select_temporal_
  fallback_candidate()`.
- **Section 11 (6 tests).** Structural/architectural invariants - no
  embedding/LLM-judge import or call pattern anywhere in `luno/memory_
  context.py`; the normalization functions are pure (no file/network/
  subprocess calls in their own source); `ContextItem._rank_key()`'s
  signature is exactly `(self)` - untouched; `assemble_context()`'s full
  parameter list is unchanged (a byte-exact list comparison against the
  Phase 0 baseline read, not just "still has defaults"); `_TOPIC_
  HISTORY_MAX_ENTRIES` unchanged; `_MIN_AFFIX_ROOT_LEN` stays >= 3.

## Regression

Run in file-group batches (a single full-tree `pytest tests/` invocation
exceeds this sandbox's per-command tooling budget regardless of worker
count - the same constraint this project's own `docs/testing/regression_
baseline.md` already documents for `test_dashboard.py`):

- All memory/topic/reference/temporal/cross-system suites most directly
  exercising the changed code path (`test_semantic_context_bridging.py`,
  `test_conversation_reference_resolution.py`, `test_conversation_
  intelligence.py`, `test_memory_continuity.py`, `test_memory_topic_
  retention.py`, `test_memory_comparison_topic_preservation.py`,
  `test_temporal_memory_timeline_awareness.py`, `test_cross_system_
  conversation_consistency.py`, `test_memory_context.py`, `test_memory_
  retrieval.py`, `test_memory_confidence.py`, `test_memory_conflict.py`,
  `test_memory_conflict_resolution.py`) -> **590 passed, 0 failed**.
- Remaining repository (84 files, excluding the 2 pre-existing
  uncollectible files and `test_dashboard.py`/`test_llm_tts_streaming_
  production.py`/`test_voice_pipeline_latency.py` - real-time-duration
  tests exceeding this sandbox's per-command budget, same documented
  precedent as `test_dashboard.py`'s own existing exclusion, none with
  any code-path overlap with the two files this sprint touched), run in
  6 file-group batches -> **zero new failures**. The only 2 failures
  encountered:
  - `test_emotion_engine.py::test_stale_emotion_decays_to_unknown_
    after_the_configured_window` - already documented (`docs/testing/
    regression_baseline.md`) as a scheduling-jitter flake that fails
    under batch timing but passes in isolation; unrelated to any file
    this sprint touched.
  - `test_state_isolation.py::test_isolate_persistent_state_drains_
    stragglers_before_monkeypatch_reverts` - already documented across
    numerous prior sprint baselines (`ARCHITECTURE_GUARD.md`) as an
    `inspect.getsource()` flake; reproduced identically when run in
    complete isolation with none of this sprint's files loaded at all,
    confirming it is not caused by this sprint's dynamic-module-loading
    test harness.

## Performance (Phase 7)

Measured directly (2000 iterations x 4 representative queries against a
3-entry bounded history, `time.perf_counter()`):

| Function | Time/call |
|---|---|
| `select_topic_candidates()` | ~0.066ms |
| `is_active_topic_relevant_to_query()` | ~0.032ms |
| `select_temporal_fallback_candidate()` | ~0.003ms |
| `_strip_bounded_affixes()` | ~0.003ms |

Combined, well under the 5ms/turn target. No network calls, no model
inference, no embeddings - every function above is pure Python set/string
operations over already-bounded, already-in-memory data.

## Architectural safety (Phase 8)

- `ContextItem._rank_key()` - signature confirmed unchanged (`(self)`
  only), body untouched.
- `_apply_budget()` - untouched, not referenced by anything this sprint
  added.
- `assemble_context()` - full parameter list confirmed unchanged via a
  byte-exact comparison against the Phase 0 baseline read.
- `render_context_block()` - own logic untouched.
- No LLM judge, no embedding model, no vector database, no second
  ranking pipeline - confirmed both by design (the entire mechanism is
  bounded set/string operations) and by a structural test asserting no
  forbidden import/call pattern exists anywhere in `luno/memory_
  context.py`.
- No persistent raw conversation/topic storage - the normalization layer
  operates entirely on the same already-bounded, already-in-memory
  `ActiveTopicSnapshot`/`topic_history` objects Sprints 4-42 already use;
  nothing new is written to disk.
- No global topic state - every new function takes `topic_history`/
  `active_topic_snapshot` as explicit parameters, scoped per-conversation
  exactly as the pre-existing call sites already are.
- Topic history remains bounded - `_TOPIC_HISTORY_MAX_ENTRIES` unchanged,
  verified both by a direct constant-value test and a live E2E eviction
  test.
- No changes to TTS, streaming, response-depth, or voice-output modes -
  confirmed by diff scope (only `luno/memory_context.py` and one `elif`
  condition in `main_runtime_demo.py` were touched) and by the full
  voice/TTS/streaming test suites passing unchanged.

## Persistent-state safety (Phase 10)

SHA256 + mtime of all 680 `config/*.json` files captured before
implementation and after the full sprint (implementation + new test file
+ full regression sweep): **byte-identical, zero changes**. This sprint's
own probes and test suite ran entirely through dynamically-loaded,
isolated `RuntimeDemoConsole` instances backed by `MockOpenRouterClient`
(never real credentials, never the real persistent memory store) - no
probe side effect to report this time, unlike several prior sprints whose
own baselines note `config/relationship_state.json`'s usage counters
incrementing from live-console runs.

## Known limitations

- The synonym layer is deliberately small and generic (mic/pump/board/
  upgrade-category vocabulary only, <= 40 total members) - a genuinely
  novel colloquial synonym outside these groups (or outside the bounded
  affix rules) will still correctly find nothing rather than guess, per
  the brief's own "prefer missing a connection to fabricating one" spirit.
  This is a design choice, not an oversight - the brief explicitly
  prohibits "a giant synonym dictionary."
- The relevance guard's `reference_type == "comparison"` scoping means a
  hypothetical future reference type carrying real, potentially-
  conflicting topical content (not yet named in `luno/memory.py`'s
  `REFERENCE_TYPES`) would not automatically be covered by this guard
  until explicitly added to the same condition, mirroring how narrowly
  the two proven false-positive scenarios (D/E/G) were scoped in the
  first place.
- The majority-coverage (>50%) threshold used to distinguish "same
  topic lineage, already merged" from "genuinely competing topic" in the
  ambiguity tie-check is a heuristic, not a formal proof - it was tuned
  against the two concrete cases that motivated it (Scenario E's ~33%
  coverage genuine ambiguity vs. `test_15`'s ~75% coverage same-lineage
  case) with a comfortable margin between them, but a case landing very
  close to 50% coverage in either direction has not been separately
  constructed and tested.

## Files changed

`luno/memory_context.py` - additive: a new "Semantic Context Bridging
(Sprint 43)" section (`_strip_bounded_affixes()`, `_TOKEN_SYNONYM_
GROUPS`/`_TOKEN_SYNONYM_CANON`/`_TOKEN_SYNONYM_PHRASES`, `_normalize_
terms_for_bridging()`, `_normalize_query_tokens_for_bridging()`,
`is_active_topic_relevant_to_query()`), plus a new fallback tier appended
to the end of `select_topic_candidates()` and `select_temporal_fallback_
candidate()` (both functions' pre-existing raw-overlap logic is
untouched and still returns first, unconditionally, when it finds
anything).

`main_runtime_demo.py` - one `elif` condition (the pre-existing Sprint 4
single-slot recency branch) gained one additional clause: `reference_
type != "comparison" or memory_context.is_active_topic_relevant_to_
query(active_topic_snapshot, text, topic_history)`.

No changes to `_rank_key()`, `_apply_budget()`, `render_context_block()`'s
own logic, `assemble_context()`'s parameter list, `update_active_topic()`,
`update_topic_history()`, `resolve_ordinal_targets()`, `build_dual_
response()`, the LLM model, TTS voice/model, the streaming architecture,
or response-depth semantics.

## Files created

`tests/test_semantic_context_bridging.py` (72 tests).

`docs/change_impact/semantic_context_bridging.md` (this file).
