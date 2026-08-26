# Change Impact: Entity Provenance Disambiguation & Topic Lineage (Sprint 49)

## Goal

Resolve Sprint 48's remaining known limitation #9: two distinct
entities/topics can become conflated when they share very high lexical
overlap ("Aquascape A"/"Aquascape B"). Not "make Luno guess harder" -
make Luno distinguish entity lineage ONLY when the conversation itself
contains sufficient evidence, and refuse (not silently guess) when it
doesn't.

## Phase 0 - cold-start reconnaissance

Read `docs/project_handover.md`, `docs/project_handover.json`,
`ARCHITECTURE_GUARD.md`, `docs/testing/regression_baseline.md`, and the
latest change-impact docs (Sprints 45-48). Verified against the actual
source, not merely trusted:

- Sprint 48's own demonstrative-anchoring gate (`distinct_other_count
  >= 1 and _DEMONSTRATIVE_ANCHORED_RE.search(text)`) is genuinely
  present in `luno/memory_context.py` at the documented location.
- `current_sprint`/`status` in `docs/project_handover.json` read `48`/
  `"complete"`, matching the handover's own claim.
- `config/*.json` contains 15 top-level files, matching the documented
  count.
- No uncommitted/stray production changes were found attributable to
  a prior, unfinished sprint.

No discrepancy between the handover documentation and the actual
checkout was found.

## Phase 1 - live reproduction (real `RuntimeDemoConsole`)

Reproduced Sprint 47/48's own documented limitation #9 BEFORE any code
change, using the exact conversation from the existing regression lock
(`tests/test_bounded_entity_provenance.py::test_31_known_limitation_
a9_still_unfixed_by_design`): "Aquascape A pakai pompa kecil." then
"Aquascape B pakai pompa besar." then "Pompanya gimana?" - confirmed it
STILL reproduces exactly as documented: the rendered system prompt
confidently injects "Active conversation topic: aquascape, b, besar,
dicatat, pakai, pompa (last stated as: 'Aquascape B pakai pompa
besar.')" with zero ambiguity signal, silently discarding Aquascape A
entirely. The bug was NOT invented - it is the same, still-present
defect Sprint 47/48 documented.

## Phase 2 - root-cause analysis

**Where two distinct lineages become indistinguishable:** `is_active_
topic_relevant_to_query()`'s `active_score > 0` branch (`luno/memory_
context.py`), specifically its `coverage > 0.5` "same lineage, already
merged in" check. When a history entry's own significant vocabulary is
more than 50% covered by the active snapshot's own terms, it is skipped
entirely - never even reaching the tie-check that would otherwise
detect real ambiguity between two competing candidates. "Aquascape A
pakai pompa kecil." (significant terms: aquascape, pakai, pompa, kecil)
is 75% covered by "Aquascape B pakai pompa besar." (aquascape, b,
pakai, pompa, besar, dicatat) - well over the threshold - so A is
wrongly treated as B's own already-absorbed lineage rather than a
distinct competitor.

1. **Why current evidence is insufficient:** the flat bag-of-terms
   representation (`ActiveTopicSnapshot.terms`) alone cannot distinguish
   "B is a corrected/renamed continuation of A" from "B is a completely
   separate entity that happens to share most of its generic
   vocabulary with A" - both produce identical high coverage.
2. **What evidence already exists but is currently discarded:** the
   user's own original text, verbatim, IS already captured in `Active
   TopicSnapshot.source_sentence` (Sprint 40) - but `is_active_topic_
   relevant_to_query()`'s coverage check never reads this field at all,
   only `.terms`.
3. **Minimal distinguishing provenance:** a standalone, single
   UPPERCASE letter appearing in a `source_sentence` ("Aquascape A ...",
   "Aquascape B ...") is a common, general, cross-domain labeling
   convention for disambiguating two instances of the same kind of
   thing - not specific to any product/domain.
4. **Why this is safe and deterministic:** it is derived purely from
   text the user actually typed (never inferred/fabricated), applied
   via a single fixed regex (no learning, no fuzzy matching), and
   bounded by the SAME size limit `source_sentence` already has
   (`_SOURCE_SENTENCE_MAX_CHARS`) - no new state, no new tokenization
   pass, no persistence.

**Why Sprint 48's own investigated-and-rejected approach doesn't
reapply here:** Sprint 48 already tried a token-based "distinguisher
letter" signal for this exact limitation and rejected it because the
shared, cross-cutting `luno.memory_retrieval.query._STOPWORDS` set
unconditionally drops the lowercase English-article stopword "a" while
keeping "b" (see `ARCHITECTURE_GUARD.md` SS48) - an asymmetric,
unsafe foundation if built on `analyze_query()`'s own token stream.
This sprint's fix avoids that trap entirely by reading `source_
sentence`'s RAW, case-preserved text directly with a dedicated regex,
never through `analyze_query()`'s lowercased/stopword-filtered tokens -
so "A" and "B" are found completely symmetrically. This is the
genuinely different mechanism Sprint 48's own "Recommended Sprint 49
investigation" note asked for.

## Phase 3 - provenance design

`_extract_entity_differentiator(source_sentence: str) -> Optional[str]`
(new function, `luno/memory_context.py`): returns the single standalone
uppercase letter (`\b[A-Z]\b`) found in `source_sentence`, or `None` if
zero or 2+ candidates are found (never guesses which one is the real
label when a sentence contains multiple standalone capitals).

Design properties, verified against every stated constraint:

- **Deterministic:** one fixed regex, no fuzzy matching, no learning.
- **Local to conversation state:** reads only the already-in-memory
  `ActiveTopicSnapshot.source_sentence` field - no new lookup, no
  cross-conversation access.
- **Bounded:** `source_sentence` is already capped at
  `_SOURCE_SENTENCE_MAX_CHARS` (160 chars); the regex match itself is
  O(n) in that bounded string.
- **Derived only from actual conversation evidence:** the letter must
  have been typed by the user themselves, verbatim, in the turn that
  established the snapshot - never inferred, never assumed.
- **No world-knowledge assumptions:** never hardcodes any product/
  domain name; works identically for "Aquascape A/B", "ESP32 A/B",
  "Server A/B", or any other domain.
- **No embeddings, no LLM judge, no network calls, no second ranking
  system, no global topic state, no persistent raw conversation
  storage, no unrestricted synonym dictionary:** none introduced.

Deliberately scoped narrowly for safety (see the function's own
docstring and the regex's own module comment for full reasoning):
UPPERCASE letters only (lowercase "a" collides with the shared,
untouchable English-article stopword), letters only, never digits (a
bare digit is an ordinary quantity in natural text, e.g. "beli 2
pompa", not a disambiguation label).

Wired into the existing `coverage > 0.5` check: when BOTH the active
snapshot and a history entry carry an unambiguous differentiator AND
those differentiators DISAGREE, the coverage-based lineage-skip is
bypassed - the entry falls through to the SAME tie-check every other
genuine competitor already goes through. Fully compatible with the
existing flat `ActiveTopicSnapshot`, the existing topic-history
architecture, the existing reference classifier, and the existing
candidate ranking/budget pipeline - zero changes to any of them.

## Phase 4 - hard boundary matrix

| # | Case | Classification | Status after Sprint 49 |
|---|---|---|---|
| 1 | Aquascape A vs Aquascape B | MUST REFUSE (no differentiator in the follow-up query itself) | **FIXED** - now refuses (was: silently resolved to B) |
| 2 | ESP32 vs ESP32-S3 (correction) | MUST PRESERVE corrected identity | Unchanged - Sprint 47 Fix #1 |
| 3 | ESP32 vs ESP8266 (2 live topics, ambiguous word) | MUST REFUSE | Unchanged - Sprint 44/48 |
| 4 | microphone vs INMP441 | MUST REFUSE (never fabricate product-category) | Unchanged - Sprint 45 |
| 5 | GPU vs "graphics card" (kartu grafis) | MUST RESOLVE via phrase table | Unchanged - Sprint 43 |
| 6 | pump vs pompa | MUST RESOLVE via synonym group | Unchanged - Sprint 43 |
| 7 | two devices, near-identical description, WITH explicit "A"/"B" labels | MUST REFUSE (ambiguous without a query-side differentiator) | **FIXED** - generalizes case 1 |
| 8 | demonstrative "ini" (2nd word, unknown-classified, <=3 residual) | MUST MERGE | Unchanged - Sprint 47 Fix #1 |
| 9 | demonstrative "itu" (2nd word, single token, zero grounding, 1 other topic) | MUST REFUSE | Unchanged - Sprint 48 |
| 10 | "yang tadi" | MUST RESOLVE (most recent matching entry) | Unchanged - Sprint 39/43 |
| 11 | "yang kedua" | MUST RESOLVE (ordinal) | Unchanged - Sprint 38 |
| 12 | "yang wireless" (2+ distinct topics, zero grounding) | MUST REFUSE | Unchanged - Sprint 44 Phase 7 |
| 13 | "yang lebih bagus" (multi-topic) | MUST REFUSE (known limitation #5, deliberately unfixed) | Unchanged - Sprint 46 |
| 14 | correction / reference repair | MUST PRESERVE corrected entity | Unchanged - Sprint 38/47 |
| 15 | planned/current/historical temporal reference | MUST RESOLVE to status-eligible entry, or MUST REFUSE if none eligible | Unchanged - Sprint 41/46 |
| 16 | sparse unknown follow-up (<=1 real token) | MUST MERGE | Unchanged - Sprint 44 |
| 17 | single-topic ambiguity (no other topic) | MUST RESOLVE (stronger fallback, Invariant 6) | Unchanged |
| 18 | two-topic ambiguity, zero grounding, non-demonstrative | MUST RESOLVE via recency (Sprint 46's own mic/test_27 case) | Unchanged |
| 19 | three-topic ambiguity | MUST REFUSE | Unchanged - Sprint 44 Phase 7 |
| 20 | cross-conversation isolation | MUST NOT LEAK | Unchanged, re-verified this sprint |

Every "Unchanged" row above was re-verified end-to-end or via a direct
regression-lock test this sprint (see Tests below) - none were assumed
"probably okay" without a test.

## Fix

One new function (`_extract_entity_differentiator()`) plus one new
regex constant (`_ENTITY_DIFFERENTIATOR_RE`), and one small, additive
modification to the existing `coverage > 0.5` check inside `is_active_
topic_relevant_to_query()`'s `active_score > 0` branch (`luno/memory_
context.py`). No other production file touched.

## Provenance model

No new data structure. `ActiveTopicSnapshot`'s field set is unchanged
(still `terms`, `turns_since_active`, `list_items`, `status`, `source_
sentence` - confirmed via `dataclasses.fields()` in `test_30`). The
"provenance" is derived on-demand from the already-existing, already-
bounded `source_sentence` field - nothing new is stored, computed once
per turn, or persisted.

## Ambiguity policy

- **Both entries carry an unambiguous, disagreeing differentiator:**
  the coverage-based lineage-skip is bypassed; the entries are treated
  as genuine competitors subject to the existing tie-check. A bare
  follow-up with no differentiator of its own produces a TIE and
  therefore a REFUSAL.
- **Only one entry (or neither) carries a differentiator:** behavior is
  UNCHANGED from before this sprint - the original majority-coverage
  lineage-skip still applies. One-sided evidence is never treated as
  sufficient.
- **Both entries share the SAME differentiator** (not a disagreement):
  UNCHANGED - still treated as the same lineage.
- **The follow-up query itself names a specific differentiator**
  ("Pompa A gimana?"): NOT specially resolved by this sprint's fix -
  documented as an explicit scope boundary, see Known limitations.

## E2E results

Limitation #9 fixed and locked in (`test_15`, generalized to a
different domain vocabulary in `test_16`). Negative control confirms
the fix does not fabricate evidence that was never stated (`test_17`).
All 20 hard-boundary-matrix cases re-verified (`test_18`-`test_28`,
plus reused Sprint 44-48 test suites). Cross-conversation isolation
re-verified with the new differentiator-aware logic specifically
(`test_29`).

## Tests

`tests/test_entity_provenance_disambiguation.py` - 34 tests, all
passing: 9 unit tests for `_extract_entity_differentiator()` (including
acronym/hyphenated-compound/digit/lowercase/word-boundary edge cases),
5 unit tests for the new gate inside `is_active_topic_relevant_to_
query()` (including explicit regression locks for `test_15`'s and
Sprint 46's `test_39`'s own shapes), 3 E2E tests for the fix itself
(including a cross-domain generalization test and a negative control),
2 E2E regression locks for existing lineage/coverage boundaries, 9
hard-boundary-matrix E2E/unit tests, 2 bounded-state/cross-conversation-
isolation tests, 2 performance tests, 2 known-limitation regression
locks.

## Regression results

Targeted core suite (Sprint 48's own 14-file list plus this sprint's
new file): **711 passed, 0 failed.** Full repository sweep (95/97
collectible files, `pytest -n 4`, run in 8 chunks to fit the sandbox's
own per-call wall-clock cap): **2889 passed, 11 failed** - 10 identical
to the standing baseline; the other 1 (`test_streaming_e2e.py::test_D_
barge_in_between_llm_and_tts_chunk_never_plays`, not in a file touched
by this sprint) re-run in ISOLATION (serial) and passed cleanly,
confirming parallel-execution timing contention rather than a
regression. 2900 total tests collected (95 collectible files of 97, 2
pre-existing uncollectible - `test_main_bargein.py`/`test_root_main_
bargein.py`, same documented cause as every prior sprint).

## Performance

`is_active_topic_relevant_to_query()` (the worst-case path that reaches
the new gate, 5,000-call measurement): mean 0.043ms, min 0.035ms, max
0.513ms - well under the 5ms/call target. `_extract_entity_
differentiator()` alone (10,000-call measurement): mean 0.0017ms, min
0.0015ms, max 0.022ms. No network calls, no model inference, no
embeddings.

## Persistent-state verification

SHA256 hashes of all 15 top-level `config/*.json` files captured before
any production edit. Two independent isolated verification runs: (1)
this sprint's own new test file alone, and (2) that file plus the full
Sprint 45-48 core entity/reference suite. BOTH runs confirmed all 15
files byte-identical immediately before/after. A separate comparison
against the very start-of-sprint baseline (captured before touching any
source) DOES show `config/relationship_state.json` and `config/long_
term_memory.json` changed - traced to the intervening FULL 8-chunk
regression sweep, which (per the same pattern independently confirmed
every sprint since 43) exercises OTHER, pre-existing tests that
legitimately write the real persistence layer, unrelated to this
sprint's own source edit (`luno/memory_context.py` still never touches
file I/O, per its own docstring). No new persistent files were created.
The new provenance signal is computed on-demand and never stored.

## Known limitations

1. **Query-side differentiator matching not implemented.** A follow-up
   that itself names a specific differentiator ("Pompa A gimana?") is
   NOT specially resolved to A by this sprint's fix - it still refuses,
   identically to a bare "Pompanya gimana?". This is a deliberate scope
   boundary (see `test_33`), not an oversight: extending the same raw-
   text regex to the QUERY text itself is a natural, minimal next step,
   but was left out this sprint to keep the change small and to avoid
   expanding blast radius before a live-reproduced need is confirmed.
   See Sprint 50 recommendation.
2. **Lowercase differentiators are never recognized** ("aquascape a
   pakai pompa kecil.") - a deliberate scope restriction (see `test_
   06`/`test_34`) for the same reason Sprint 48's own token-based
   approach was rejected: a lowercase "a" is indistinguishable from the
   shared English-article stopword without touching that shared,
   cross-cutting list, which remains out of scope for this narrow fix.
3. **No differentiator at all (e.g. "Aquascape depan"/"Aquascape
   belakang")** still conflates exactly as before this sprint (`test_
   17`) - there is genuinely no evidence in the conversation to
   distinguish them without the general differentiator convention this
   fix targets; this is not a regression, the system correctly still
   refuses to fabricate evidence.
4. All Sprint 43-48 known limitations not superseded above remain
   unchanged (see `docs/project_handover.md` SS16).

## Invariants (reaffirmed, one new item)

All Sprint 43-48 invariants hold unchanged. New this sprint: `_extract_
entity_differentiator()` must remain UPPERCASE-letter-only and digit-
free (do not widen to lowercase or digits without a fundamentally new
safety analysis - see Phase 3's own reasoning for why each restriction
exists) and must continue to return `None` (never guess) for 2+
candidates in one `source_sentence`.

## Recommended Sprint 50 investigation

Extend the same differentiator signal to the QUERY text itself (not
just stored `source_sentence` entries), so "Pompa A gimana?" can
resolve directly to Aquascape A rather than merely refusing - the
natural, minimal next step from this sprint's own known limitation #1
above. Must be proven via live reproduction, and must not weaken the
existing refusal behavior for a bare, non-differentiated follow-up.
Alternatively/additionally: revisit known limitation #1 from `docs/
project_handover.md` SS16 (bare compound-noun "-nya" declaratives,
unfixed since Sprint 44) with a position-aware check, as previously
recommended and still open.
