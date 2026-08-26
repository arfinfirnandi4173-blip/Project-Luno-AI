# Conversation Reference Resolution

## 1. Root cause

Phase 0's audit re-verified the existing reference/topic pipeline
(`classify_reference_type()`, `is_pure_reference_followup()`,
`ActiveTopicSnapshot`, `update_active_topic()`, `update_topic_history()`,
`select_topic_candidates()` - built across three prior sprints: "Memory
Continuity", "Memory Topic Retention", "Context-Aware Comparison Topic
Preservation") and found it already correctly resolves "yang lain?",
"terus?", "kalau itu?", "ESP32 gimana?", and already correctly keeps
independent subtopics (mic vs pompa) from contaminating each other via
bounded, token-overlap-matched topic history. None of that machinery was
touched by this sprint.

Two concrete gaps were found, both reproduced live (direct calls through
`luno.memory`/`luno.memory_context`, and through the real
`RuntimeDemoConsole` event path) before any production code was written:

**Gap A - ordinal/list reference.** `ActiveTopicSnapshot` only ever
stored an unordered `frozenset` of topic terms. When Luno itself
enumerated a list ("1. INMP441 / 2. MAX9814 / 3. SPH0645"), there was no
way to resolve "yang kedua gimana?" to the actual second item - the best
the old mechanism could do was re-offer the whole undifferentiated bag
("mikrofon", "esp32", "inmp441", "max9814", "sph0645"), never the one
item the user meant.

**Gap B - attribute reference.** "kalau yang wireless?", "yang murah?"
(without "lebih"), "kalau versi Bluetooth?" matched no existing
`classify_reference_type()` pattern and fell to `"unknown"`.
`is_pure_reference_followup()` correctly treats `"unknown"` as a RICH
turn (has its own standalone content) - so `update_active_topic()`
REPLACED the entire snapshot with `{"kalau", "yang", "wireless"}`,
losing "esp32"/"mikrofon" outright. Reproduced live: an ESP32-microphone
conversation followed by "Kalau yang wireless?" lost every ESP32/mic
term from the snapshot by the very next turn.

## 2. Reference model

No second memory system. A lightweight, transient, in-memory-only
`memory_context.ConversationReference` dataclass exists purely for
observability/testing (mirrors this project's `ContextItem`/
`RelevantMemory` "small dataclass, never persisted" convention):
`reference_type`, `target_topic` (bag-of-terms), `target_items`
(resolved ordinal target(s)), `confidence` (`"high"`/`"none"`),
`source`. Every field is derived from state the existing
`ActiveTopicSnapshot`/topic-history mechanism already owns.

Ten reference types now exist (`luno.memory.REFERENCE_TYPES`), seven
unchanged from prior sprints plus three new:

- `repair_reference` (new) - "eh maksudku ESP32-S3", "bukan yang itu,
  yang satunya" - explicit self-correction/rejection.
- `ordinal_reference` (new) - "yang kedua", "nomor tiga", "opsi 2" -
  refers to a position in something already enumerated.
- `attribute_reference` (new) - "kalau yang wireless?", "yang murah?",
  "kalau versi X?" - asks for the active topic filtered/modified by one
  new descriptive word.
- `negation_of_current_option`, `cost_comparison`, `alternative_request`,
  `continuation`, `comparison`, `direct_reference`, `unknown`
  (unchanged).

Also closed while extending `direct_reference`'s own coverage: bare
"yang itu?"/"yang ini?" (one of the brief's own primary target phrases)
previously fell through to `"unknown"` - every existing branch required
"kalau"/"gimana" framing or the word "tadi". Now correctly resolves.

## 3. Resolver rules

Priority order, checked in `classify_reference_type()` (first match
wins, deterministic `re`, no LLM/embedding classifier):

```
repair_reference > negation_of_current_option > cost_comparison >
alternative_request > ordinal_reference > attribute_reference >
continuation > comparison > direct_reference > unknown
```

For STATE UPDATE (`update_active_topic()`/`update_topic_history()`),
each type maps to one of three behaviors:

- **REPLACE** (`is_followup=False`, `is_merge=False`) - a rich turn
  (`unknown`, or a `comparison`/`negation` whose residual doesn't
  overlap the active topic) fully replaces the snapshot.
- **PRESERVE** (`is_followup=True`) - `alternative_request`/
  `continuation`/`direct_reference`/`cost_comparison`/`ordinal_reference`
  (new) - the turn has no standalone entity of its own, snapshot
  unchanged, `turns_since_active += 1`.
- **MERGE** (`is_merge=True`, new) - `repair_reference`/
  `attribute_reference` - UNIONS the new turn's terms into the existing
  snapshot (`_merge_terms()`, new-terms-first ordering so truncation
  favors the correction/attribute) rather than replacing (loses the
  parent topic) or preserving (drops the correction/attribute itself).

For RETRIEVAL (this turn's own `assemble_context()` call), Phase 8's
required pipeline order is: resolve the reference target FIRST, THEN
retrieve. `PlannerBridgeModule._handle_utterance()`'s new ordinal branch
runs before the existing topic-history/active-topic branches; when it
resolves a target it skips the coarser branches entirely for that turn
(same "skip the coarser branch once a precise one fired" discipline the
topic-history/active-topic branches already use between themselves).
Memory ranking (`_rank_key()`/`_apply_budget()`) is completely
unchanged - a resolved ordinal candidate is an ordinary `ContextItem`
once constructed, never a privileged bypass.

## 4. Ambiguity handling

`resolve_ordinal_targets()` NEVER fabricates: returns `((), "none")`
whenever `text` names no ordinal at all, there is no usable
(non-stale, non-empty) `list_items` to resolve against, or every
requested index is out of range. Verified: `ActiveTopicSnapshot` with no
`list_items` + "yang kedua gimana?" -> `((), "none")` - the caller's
existing bag-of-terms fallback (if any) is left completely untouched.

For a bare "yang itu?" across two genuinely unrelated, non-overlapping
topics (no residual entity to disambiguate with), the existing,
unmodified `select_topic_candidates()` returns `[]` (a stopword-only
query has no real tokens to overlap with either topic) - the
conservative single-slot "most recent" fallback is the deliberate,
non-guessing default. No new ambiguity-detection machinery was built;
the existing conservative behavior already satisfies Phase 9's "don't
inject aggressively when ambiguous" requirement.

## 5. List/ordinal resolution

`extract_list_items_from_reply()` (new, `luno/memory_context.py`) parses
a numbered (`"1. X"`, `"1) X"`) or bulleted (`"- X"`, `"* X"`, `"• X"`)
line at the start of a line in Luno's own finalized reply, bounded at 10
items. `parse_ordinal_indices()` extracts every 1-based position named
in the user's text (word-form ordinals via `memory.ORDINAL_WORD_MAP`,
cardinal-after-"nomor"/"opsi"/"pilihan"/"item" via
`memory.CARDINAL_WORD_MAP`, or bare digits), in order of first
appearance, de-duplicated. `resolve_ordinal_targets()` searches the
current `ActiveTopicSnapshot`'s own `list_items` first, falling back to
the most recent bounded topic-history entry that itself carries a
non-stale list.

Verified (direct calls + real E2E):
- "Yang kedua gimana?" against `["INMP441", "MAX9814", "SPH0645"]` ->
  `("MAX9814",)`, confidence `"high"`.
- "Yang pertama dibanding yang ketiga?" -> `("INMP441", "SPH0645")`.
- No list available -> `((), "none")` - never fabricates.

## 6. Entity/attribute resolution

`attribute_reference`'s classifier includes an elliptical-fragment
residual guard: a candidate word from "yang X"/"kalau versi X" is only
accepted when the REST of the sentence (minus the candidate and ordinary
connective/question boilerplate) has no other substantial content. This
is what correctly keeps "Modul Bluetooth apa yang bagus buat ESP8266?" -
a full, self-contained, rich question that happens to contain "yang
bagus" - classified `unknown`, not misclassified as an elliptical
ATTRIBUTE_REFERENCE fragment.

Once classified, the MERGE update rule (§3) is what achieves "ESP32
microphone" + "wireless" -> "ESP32 wireless microphone" rather than a
new unrelated topic - verified live: after merging, the snapshot
contains both "esp32" (parent, preserved) and "wireless" (new, added).

## 7. Multi-topic handling

No new code was needed here - Phase 7's requirement was already
satisfied by the prior "Memory Topic Retention" sprint's own
`select_topic_candidates()` (token-overlap matching against a bounded,
per-conversation topic history, unmodified by this sprint). Re-verified
via a fresh E2E scenario for this sprint: ESP32/mic -> aquascape ->
pompa -> "Yang tadi soal mic gimana?" correctly retrieves the ESP32/mic
history entry (matched via the residual word "mic"), never the more
recent pompa/aquascape entry; a following "Kalau pompanya?" would
correctly retrieve the pompa entry instead (same overlap-based
mechanism, direction-agnostic).

## 8. Before / after examples

**"Yang kedua gimana?"** (after Luno enumerated "1. INMP441 / 2. MAX9814
/ 3. SPH0645"):

- BEFORE this sprint: `classify_reference_type()` returned `"unknown"`
  (no ordinal pattern existed at all) - the turn was treated as a fresh,
  rich question, replacing the active topic with junk tokens extracted
  from "Yang kedua gimana?" itself, with no connection to the
  microphone list whatsoever.
- AFTER: `reference_type=ordinal_reference`, `resolve_ordinal_targets()`
  resolves `("MAX9814",)` with confidence `"high"` - the retrieval query
  is expanded to include "MAX9814" specifically (plus the parent ESP32/
  mikrofon terms), and a synthetic candidate naming MAX9814 is offered
  to `assemble_context()`.

**"Yang tadi soal mic gimana?"** (after ESP32/mic -> aquascape -> pompa):
resolves via `select_topic_candidates()` to the bounded history entry
whose terms include `{esp32, mikrofon, inmp441, i2s, voice, recording,
...}` - confirmed via a real rendered `system_prompt` inspection
containing "inmp441"/"esp32", never "pompa"/"aquascape"/"submersible".

**"Kalau yang wireless?"** (after "Jelasin pilihan mikrofon untuk
ESP32."):

- BEFORE this sprint: fell to `"unknown"`, replacing the snapshot with
  `{"kalau", "yang", "wireless"}` - "esp32"/"mikrofon" gone.
- AFTER: `reference_type=attribute_reference`,
  `is_merge_reference_followup()=True` - the snapshot after this turn
  contains BOTH the original ESP32/mikrofon terms AND "wireless".

**"Eh maksudku ESP32-S3."** (after "ESP32 pakai INMP441."):

- BEFORE this sprint: fell to `"unknown"` (a rich turn by construction),
  replacing the snapshot with `{"eh", "maksudku", "esp32", "s3"}` -
  "inmp441" gone; a following "Kalau mikrofonnya?" would have resolved
  to ESP32-S3 with NO memory of INMP441 at all.
- AFTER: `reference_type=repair_reference`, MERGE - the snapshot after
  this turn contains "esp32", "inmp441" (preserved) AND "s3" (the
  correction) - a following "Kalau mikrofonnya?" correctly resolves to
  ESP32-S3 + INMP441 together.

## 9. E2E results

5 real E2E tests through `RuntimeDemoConsole`
(`tests/test_conversation_reference_resolution.py`):

1. Full mic-list scenario (5 turns: list -> ordinal -> attribute ->
   comparison-ordinal -> unrelated aquarium query) - final snapshot
   confirmed to contain ONLY aquarium terms, zero ESP32/INMP441
   contamination.
2. Ordinal resolution mid-conversation - `list_items` correctly captured
   from the real streamed/finalized reply and resolved to `("MAX9814",)`.
3. Attribute reference merges the parent topic - both "esp32" and
   "wireless" present in the post-turn snapshot.
4. Multi-topic switch (ESP32/mic -> aquascape -> pompa -> "yang tadi
   soal mic") - the rendered `system_prompt` for the final turn contains
   "inmp441"/"esp32", not the intervening pompa/aquascape topic.
5. Repair correction persists across turns - "inmp441" and "s3" both
   present in the snapshot after the correction turn.

## 10. Regression results

Scoped regression during development (7 files including every
pre-existing memory/topic-history/comparison-preservation suite plus the
new file): 297 passed, 0 failed. Full `tests/` tree (85 files, 12
chunks, `pytest -n 2`): zero new regressions - the only failures observed
were independently re-confirmed pre-existing/environmental/parallel-load
timing flakes (documented in `docs/testing/regression_baseline.md`'s
dated entry for this sprint), matching the SAME failure classes every
prior sprint's own baseline already documents. Persistent state
(`config/*.json` SHA256, all 15 files) confirmed byte-identical before
and after the full sprint.

## 11. Known limitations

- **`_merge_terms()`'s bounded truncation can drop older terms once the
  20-term cap is reached** across several consecutive merges in one
  conversation - new/correction terms are always prioritized (ordered
  first), so the practical impact is limited to very long chains of
  attribute/repair turns, not the common case.
- **Ordinal resolution only recognizes explicit position markers** (word
  or digit forms after "yang"/"nomor"/"opsi"/"pilihan"/"item") - a
  purely descriptive reference to a list item ("yang analog", without an
  explicit position) is handled by `attribute_reference`'s relevance-
  style matching in the VOICE OUTPUT layer (Sprint 37), not by this
  sprint's ordinal resolver; the two are complementary, not overlapping.
- **`attribute_reference`'s elliptical-fragment guard is a residual word
  count, not true semantic understanding** - it is a deterministic
  heuristic (same class of primitive as every other classifier in this
  module), not immune in principle to an adversarial phrasing that
  happens to use only boilerplate words alongside a real new subject;
  no such case was found in this sprint's own adversarial testing, but
  none is claimed to be provably impossible.
- **The "yang ... tadi" bounded-gap extension to `direct_reference`**
  (added for the brief's own "yang buat mic tadi" example) tolerates up
  to 3 intervening words between "yang" and "tadi" - a coarse bound
  chosen to cover the brief's own examples without over-generalizing;
  a longer, unrelated "yang ... tadi" span more than 3 words apart in a
  genuinely rich sentence would not match (a conservative miss, not a
  false positive).
- **Multi-topic protection (Phase 7) required no new code** because the
  prior "Memory Topic Retention" sprint's `select_topic_candidates()`
  already solved it - this sprint only re-verified it, it did not
  design or test it from scratch, so its own coverage is inherited, not
  new.
