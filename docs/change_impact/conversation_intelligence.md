# Change Impact: Conversation Intelligence & Context Quality (Sprint 39)

## Goal

Not "make retrieval happen more often." Luno should understand what the
user is referring to, retain the right context, discard the wrong
context, handle corrections, handle ambiguity conservatively, and
provide the minimum sufficient context to the LLM.

## Method

Phase 0 was a read-only re-audit of the full turn pipeline (`luno/memory.py`,
`luno/memory_context.py`, `main_runtime_demo.py`, existing tests,
`ARCHITECTURE_GUARD.md`, prior `docs/change_impact/*`) - not trusting
prior sprint reports, re-verifying claims via live Python calls. Phase 1
built a deterministic E2E probe suite through the REAL
`RuntimeDemoConsole` (not just unit-level classifier calls) covering the
brief's own four scenarios (A: topic continuation chain, B: topic
switching + delayed explicit reference, C: correction, D: 12 elliptical
phrases). These probes surfaced four concrete, reproducible,
root-caused context-quality failures - documented below with the exact
reproduction, root cause, and fix for each.

## Failure 1 - ATTRIBUTE DRIFT (`_merge_terms()` eviction)

**Reproduction (Scenario A, turn 3):** "ESP32 pakai INMP441." ->
"Kalau koneksinya gimana?" -> "Kalau yang wireless?". The third turn
correctly classifies as `attribute_reference` and correctly triggers the
MERGE update path. Expected: the post-turn `ActiveTopicSnapshot.terms`
should contain both the parent identity ("esp32", "inmp441") and the new
attribute ("wireless"). Actual (before fix): "esp32" and "inmp441" were
completely absent.

**Root cause:** `_merge_terms(new_terms, old_terms, limit=20)` put
`new_terms` first, then `old_terms`, then truncated to 20. A single
turn's own text+reply can easily reach ~19 tokens on its own (verified:
`extract_topic_terms_from_turn("Kalau yang wireless?", "Untuk versi
wireless, bisa pakai modul I2S over WiFi custom atau BLE audio, tapi
latency lebih tinggi.")` returns 19 terms), leaving ~1 slot for
everything already established - the exact opposite of what a merge is
supposed to guarantee. A second trigger for the same root cause was also
found (Scenario C, turn 3): `old_terms` itself can reach the cap after
just two prior merges, with the same eviction effect on a SHORT new
turn. Additionally, `frozenset` iteration order for strings depends on
Python's per-process hash seed, so which specific old terms survived was
not even reproducible run-to-run before this fix.

**Fix (`luno/memory_context.py`):**
- New `_extract_topic_terms_from_turn_ordered()` - same tokenization as
  `extract_topic_terms_from_turn()` (reuses `analyze_query()`, no second
  tokenizer), but returns an untruncated, order-preserving tuple (user's
  own typed words first, then the reply's) instead of a bounded
  frozenset.
- `_merge_terms()` rewritten: reserves at least half of `limit` for
  `old_terms` (via a deterministic `sorted()` order, not hash-seed
  luck), with any unused old-side budget returned to the new side.
  Accepts either a bare set/frozenset (legacy contract - `sorted()`
  applied) or an order-preserving sequence (the new helper above), so
  the user's own typed words - almost always the specific new
  attribute/correction - are prioritized over incidental reply-only
  filler on the new side.
- Both merge call sites (`update_active_topic()`, `update_topic_history()`)
  updated to pass the order-preserving extraction.

**Verified after fix:** the exact Scenario A reproduction now retains
"esp32", "inmp441", AND "wireless" (20 terms total, at the cap but
correctly populated). `tests/test_conversation_intelligence.py::
test_01`/`test_02`/`test_03` lock this in as permanent regression
guards.

**Known limitation:** the reserved-old-quota's `sorted()` tie-break has
no notion of per-term recency/importance. A SECONDARY correction detail
(e.g. "s3" in "ESP32-S3", introduced by a repair merge) can still be
squeezed out by a LATER, unrelated merge two turns further on, purely
because it sorts late alphabetically among the reserved old-side slots -
even though the PARENT topic identity ("esp32"/"inmp441" - what the
original bug actually destroyed) reliably survives every case tested.
Not fixed further this sprint: it would require new per-term state
(recency/generation tracking per term) that is not justified by a strong
enough reproduced failure against this sprint's own bar for new state
("reproduced failure + deterministic use + bounded lifetime + a test").
See `test_50_e2e_scenario_c_correction_preserves_history`'s own
docstring.

## Failure 2 - MISSING CONTEXT (comparative/superlative classification gap)

**Reproduction:** the brief's own Phase 8 adversarial phrases "yang
lebih bagus?"/"yang lebih kecil?" classified as `unknown`. "yang paling
murah/mahal/bagus/kecil?" also classified as `unknown`. "Terus yang
paling murah?" (Scenario A, turn 4) classified as `continuation`
(PRESERVE), silently discarding the turn's real content ("paling
murah") instead of merging it in.

**Root cause:** `_ATTRIBUTE_REFERENCE_CANDIDATE_RE` captured the word
immediately after "yang" as the candidate - for "yang lebih bagus?" that
word is "lebih" (the comparative marker itself), which
`_attribute_reference_word()` then correctly rejects as content-free,
never reaching "bagus". For "Terus yang paling murah?", even after
fixing the candidate-word extraction, the leading "terus" counted as
disqualifying "extra residual content" in the elliptical-fragment guard,
so the match was rejected and the turn fell through to bare
`continuation`.

**Fix (`luno/memory.py`):**
- `_ATTRIBUTE_REFERENCE_CANDIDATE_RE` extended with an optional
  `(?:lebih\s+|paling\s+)?` skip prefix - the same shape the regex
  already uses for `(?:bagian\s+)?`.
- `_ATTRIBUTE_RESIDUAL_STOPWORDS` extended with `"lebih"`/`"paling"`
  (so the markers, now skipped by the candidate regex, don't
  disqualify their own match via the separate full-sentence residual
  check) and `"terus"` (as a leading discourse-particle exemption,
  the same treatment "dong"/"sih"/"aja" already receive - a genuinely
  rich sentence starting with "terus" still has OTHER real residual
  words that correctly disqualify it, verified via
  `test_12_rich_sentence_with_lebih_paling_not_misclassified`).

**Verified after fix:** all of the above now classify as
`attribute_reference` (MERGE); "yang lebih murah?"/"yang lebih mahal?"
(the pre-existing `cost_comparison` phrasing) are unaffected, still
classify at higher precedence as before; "terus pilih yang mana?" and
bare "Terus?"/"Terus gimana?" are unaffected, still classify as
`continuation`.

## Failure 3 - WRONG CONTEXT (`soal` stopword gap)

**Reproduction (Scenario B, turn 6):** "Jelasin mic buat ESP32." ->
"Aku mau bahas topik lain, soal aquascape." -> "Pompa yang bagus buat
aquascape apa?" -> "Sekarang aku mau tanya soal PC." -> "Spek minimum
buat gaming apa?" -> "Yang tadi soal mic gimana?". The rendered system
prompt for the final turn contained TWO "Active conversation topic:"
lines - the PC topic and the aquascape topic - neither of which shares
any real subject-matter overlap with "mic".

**Root cause:** `select_topic_candidates()` computes token overlap
between the query and each history entry after stripping
`_TOPIC_OVERLAP_STOPWORDS`. "soal" ("about"/"regarding") was missing
from that stopword set. Both the PC turn ("...soal PC.") and the
aquascape turn ("...soal aquascape.") happened to be introduced with
"soal X" phrasing (an entirely ordinary, natural way to say "about X" in
Indonesian) - registering a false-positive overlap with the query "Yang
tadi soal mic gimana?" purely on that shared preposition.

**Fix (`luno/memory_context.py`):** added `"soal"` to
`_TOPIC_OVERLAP_STOPWORDS`.

**Verified after fix:** re-running the same probe, the rendered prompt
for the final turn now contains exactly ONE "Active conversation topic:"
line, and it is the correct mic/ESP32/INMP441 entry from turn 1.
Confirmed the fix does not break genuine overlap when "soal" appears
alongside a REAL shared subject word (`test_15_soal_overlap_still_works_for_genuine_shared_subject`).

## Failure 4 - MISSING CONTEXT (`_TOPIC_HISTORY_MAX_ENTRIES` too small)

**Reproduction:** in the same Scenario B, `_TOPIC_HISTORY_MAX_ENTRIES=4`
meant the mic/ESP32 entry (turn 1) was evicted from bounded history by
the time the user explicitly circled back to it at turn 6 - just 4
topic-switches later, ordinary conversational drift, not a contrived
stress case. The user's own words ("yang tadi soal mic") are an
unambiguous, explicit reference - not the genuinely-ambiguous case the
Phase 4 ambiguity policy's "prefer zero retrieval" is meant for; losing
the target entirely here is a real MISSING CONTEXT failure.

**Fix (`luno/memory_context.py`):** raised `_TOPIC_HISTORY_MAX_ENTRIES`
from 4 to 8. Still small, fixed, and bounded - not "unbounded
conversation state" (the brief's own prohibited class): at most 8 small
`ActiveTopicSnapshot` entries (<=20 terms each) per conversation: this
only doubles the size of one small per-conversation list, it does not
remove or loosen any other bound (per-conversation tracking limits in
`PlannerBridgeModule` are unchanged).

**Verified after fix (combined with Failure 3's fix):** the same
Scenario B probe now correctly surfaces the mic/ESP32 topic at turn 6,
with `topic_history size=6` confirming the entry was not evicted.
`test_16_topic_history_max_entries_raised_to_8`/
`test_17_topic_history_still_bounded_not_unbounded` lock the new value
and its bound in as regression guards.

## Ambiguity policy - re-verified, unchanged

Scenario D's 12 elliptical phrases were classified and reviewed against
the brief's own policy questions (should retrieve context? should
preserve? should merge? should replace? should remain ambiguous? should
retrieve zero candidates?). Six of the twelve carry no standalone
referent and no unambiguous anchor to a specific prior entity: "Kenapa?",
"Kenapa begitu?", "Kalau begitu?", "Yang mana?", "Masih ada?", "Kalau
buat saya?". These remain `unknown` -> zero retrieval, no fabrication.
This was reviewed as a deliberate decision, not a bug: per the brief's
own "for AMBIGUOUS cases: DO NOT GUESS... prefer zero retrieval", a
genuinely signal-less fragment should not be force-classified into
something with a fabricated referent. Locked in as a permanent
regression guard (`test_30_scenario_d_genuinely_ambiguous_phrases_retrieve_zero`)
so a future change can't silently start guessing here without a fresh,
reproduced justification.

## Explicitly NOT changed

- No LLM judge, no embedding model, no second tokenizer, no second
  ranking system.
- No persistent raw conversation storage, no global topic state - every
  fix operates on the SAME existing, already-bounded, conversation-scoped
  `_active_topic`/`_topic_history` structures.
- `assemble_context()`'s own ranking (`_rank_key()`), budget, and
  rendering: untouched. Only WHICH bag-of-terms candidates reach that
  pipeline changed (via the four fixes above).
- `main_runtime_demo.py`: not modified this sprint - every fix lives
  entirely inside `luno/memory.py`/`luno/memory_context.py`.
- TTS, Fish Audio, streaming, cancellation, prompt-injection boundary:
  untouched.
- Response depth (`SHORT`/`NORMAL`/`DETAILED`/`ALL`) and voice output
  mode: untouched.

## Files changed

- `luno/memory.py` - `_ATTRIBUTE_REFERENCE_CANDIDATE_RE` extended
  (optional "lebih "/"paling " skip prefix), `_ATTRIBUTE_RESIDUAL_STOPWORDS`
  extended (`"terus"`, `"lebih"`, `"paling"`). No new reference types, no
  precedence order changes.
- `luno/memory_context.py` - new `_extract_topic_terms_from_turn_ordered()`;
  `_merge_terms()` rewritten (reserved-old-quota + deterministic order,
  order-preserving-sequence support); `_TOPIC_OVERLAP_STOPWORDS` +
  `"soal"`; `_TOPIC_HISTORY_MAX_ENTRIES` 4 -> 8; both merge call sites in
  `update_active_topic()`/`update_topic_history()` updated to use the new
  ordered extraction.

## Files created

- `tests/test_conversation_intelligence.py` (54 tests) - regression
  guards for all four fixes (Section 1), the brief's own Phase 8
  adversarial phrase matrix (Section 2), Scenario D's 12-phrase
  classification/policy table (Section 3), 18 named scenarios several
  via the real `RuntimeDemoConsole` (Section 4), no-contamination/
  bounded-state/structural guards (Section 5), and per-call latency
  measurements (Section 6, all `<5ms`/call, target met).
- This document.

## Test results

- `tests/test_conversation_intelligence.py`: 54/54 passed.
- `tests/test_conversation_reference_resolution.py` (Sprint 38's own
  suite, unmodified): 54/54 passed - confirms the four fixes are
  backward-compatible with the merge/preserve/replace contracts Sprint
  38 established.
- `tests/test_memory_continuity.py` + `tests/test_memory_topic_retention.py`
  + `tests/test_memory_decision_quality.py`: 154/154 passed.
- Full targeted sweep (`-k "memory or topic or reference or context_aware
  or comparison"`, 1007 tests across the whole suite): 1007/1007 passed.
- Full suite (84 files, 8 sequential chunks): zero new regressions: 10
  pre-existing failures, all matching the documented baseline exactly
  (see `docs/testing/regression_baseline.md`'s own Sprint 39 entry for
  the itemized list).

## Performance

`classify_reference_type()`, `_merge_terms()`, and
`select_topic_candidates()` all measured at well under the 1ms mark per
call in this environment (target was <5ms average additional
deterministic processing per turn) - no optimization was performed since
none was needed.

## Persistent state safety

`config/*.json` (185 files, including every timestamped backup) SHA256
confirmed byte-identical before vs. after the full sprint (Phase 0
reconnaissance through the final full regression run). No new files, no
raw conversation/topic persistence, no global topic state - every fix
operates purely on the existing in-memory, conversation-scoped,
already-bounded `_active_topic`/`_topic_history` dictionaries.
