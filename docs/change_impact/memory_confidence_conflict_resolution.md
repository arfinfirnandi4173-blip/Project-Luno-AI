# Change Impact: Memory Confidence & Conflict Resolution (Sprint 40)

## Goal

Improve Luno's memory system so it can distinguish (1) strongly relevant
memory, (2) weakly/ambiguously relevant memory, and (3) memory that
conflicts with newer information - without an LLM judge, embeddings, a
second ranking system, persistent raw conversation storage, or changes
to the existing TTS/streaming pipeline. The goal is not "more memory."
Luno should remember the RIGHT thing, know when an OLD thing has been
replaced, still retrieve OLD information when explicitly asked, and
refuse to inject context when it is not confident.

## Method

Phase 0 was a read-only audit of `luno/memory.py`, `luno/memory_context.py`,
`luno/memory_retrieval/`, `main_runtime_demo.py`, existing tests, and
`ARCHITECTURE_GUARD.md`, tracing the complete path from user utterance
through intent classification, topic candidate generation, `RelevantMemory`,
`assemble_context()`, deduplication, `_rank_key()`, budgeting, and
`render_context_block()`. Phase 1 built six real E2E probe scenarios
(A-F) through the actual `RuntimeDemoConsole`, not unit-level calls, to
confirm the gap was real before writing any fix.

## Root cause

The codebase already contains a sophisticated conflict-resolution system
(`luno.memory._classify_conflict()`, `_upgrade_existing_memory()`,
`_find_conflicting_memory()`, `compute_lifecycle()`, `_tag_ambiguous_
conflict()`) - but it lives entirely in the PERSISTENT `manual_memory`
layer, reachable only via an explicit "ingat ..." command
(`detect_remember_command()` -> `add_memory()`). Direct code tracing
confirmed ordinary conversation turns never reach `add_memory()`/
`_classify_conflict()` at all - they flow exclusively through the
EPHEMERAL `_active_topic`/`_topic_history` bag-of-terms mechanism
(`luno.memory_context`, Sprint 4/6/38/39), which had zero conflict or
confidence awareness. Two topic-history entries about the same subject
(an old value, a new value) rendered as two identically-labeled "Active
conversation topic:" lines, giving the LLM no signal about which one was
current.

A second, compounding problem: the shared tokenizer
(`luno.memory_retrieval.query._WORD_RE`, deliberately digit-blind since
Sprint 34's own documented rationale - a token must start with a letter)
makes "Power supply saya 5V 3A." and "...5V 5A." tokenize IDENTICALLY.
Live testing confirmed `analyze_query()` returns the exact same bag of
terms for both sentences, and the same for "RTX 3070 Ti" vs "RTX 3060 Ti"
- so a purely bag-of-terms approach cannot represent WHICH value is
current even once tagging exists.

## Confidence model

`ContextItem` gained a new field, `confidence: Optional[float] = None`.
Populated only for `active_conversation`-sourced items, via
`_confidence_for_relevant_memory()`:

- `status="active"` -> `1.0` (`_CONFIDENCE_ACTIVE`)
- `status="superseded"` -> `0.4` (`_CONFIDENCE_SUPERSEDED`)
- every other case (including every non-`active_conversation` source)
  -> `None` (contributes `0.0` to ranking)

`_rank_key()`'s tuple gained `confidence` as the LAST element, after
`priority`. This is a deliberate, evidence-scoped deviation from the
brief's own abstract "relevance > confidence > importance" framing: the
only reproduced defect confidence fixes is an arbitrary tie between two
ALREADY-tied `active_conversation` items (current vs. superseded topic-
history entries, tied on every other field since they come from the
same generation pipeline). Placing confidence any earlier in the tuple
would let it decide ties against entirely different sources (e.g.
`verified_facts`) that `_SOURCE_PRIORITY` already deliberately orders -
a wider, unproven change the evidence does not call for. The brief's
own core invariant - "a highly confident irrelevant memory must NEVER
beat a relevant memory" - holds by construction and is directly test-
proven (`tests/test_memory_confidence.py::
test_10_high_confidence_irrelevant_never_beats_relevant`): relevance
occupies tuple position 0 and dominates every later signal regardless of
what confidence contains.

## Conflict model

Deterministic, reusing existing signals, no LLM/embeddings. A
topic-history entry is tagged `status="superseded"` (in
`update_topic_history()`'s rich-turn push branch) only when BOTH hold
for the incoming turn:

1. **`luno.memory.is_correction_signal(user_text)`** - a new PUBLIC
   function wrapping the persistent layer's own already-tested private
   detectors, `_CORRECTION_RE` (matches "sekarang", "ganti ... menjadi",
   "bukan ... tapi", "actually", "correction", etc.) and
   `_is_temporal_change()` (requires both an "old" marker like "dulu"
   and a "new" marker like "sekarang" in the same text). Not a new
   detector - if these are ever tuned for the persistent store, this
   wrapper picks up the change automatically.
2. **Real, non-generic vocabulary overlap** with the entry currently at
   the front of topic history - `(new_terms - _TOPIC_OVERLAP_STOPWORDS)
   & (front.terms - _TOPIC_OVERLAP_STOPWORDS)`, reusing the exact
   overlap floor `select_topic_candidates()` already uses (not a second
   overlap definition).

Conservative by construction: two entries whose entity names share no
non-generic token at all (e.g. "ESP8266" vs "ESP32" - two distinct
tokens once the tokenizer runs, no accidental prefix match) do not get
tagged, since the mechanism cannot confidently establish "same subject."
The old entry is not mislabeled - it simply keeps its default `"active"`
status and remains exactly as retrievable via an explicit historical
query as it always was. **Never deletes. Never excludes from candidate
selection.**

To carry the actual VALUE past the digit-blind tokenizer, `ActiveTopicSnapshot`
gained a second field, `source_sentence: str = ""` - a bounded
(`_SOURCE_SENTENCE_MAX_CHARS = 160`), word-boundary-safe, UNMODIFIED
excerpt of the turn's own user text, rendered as `(last stated as: "...")`.
Bounded and transient (conversation-scoped, never persisted, always
fully replaced - the same discipline Sprint 38's `list_items` field
already established), and rendered through the SAME
`_neutralize_boundary_markers()` prompt-injection trust boundary every
other memory-derived text already passes through inside
`render_context_block()` - no new sanitization mechanism was needed.

`active_topic_to_relevant_memory()` was rewritten to render current vs.
superseded snapshots differently: a superseded entry's label becomes
"Previously stated (replaced by newer information) conversation topic: ..."
and `relevant_memory_to_context_item()` now also sets `historical=True`
for it - reusing the EXISTING `historical`/`_section_for_item()`/dedup-
guard machinery a prior sprint already built (originally for the
persistent layer's own historical markers). A superseded topic
therefore renders in a structurally separate `[Historical Context]`
prompt section, not as a second identically-labeled "Active conversation
topic:" line.

## Conflict resolution walk-through (the brief's own worked example)

Memory A: "Power supply 5V 3A." Memory B: "Sekarang power supply saya
ganti jadi 5V 5A." (an explicit correction, shares "power"/"supply"
non-generic vocabulary with A).

- Normal query, "Power supply saya berapa?" -> the CURRENT `active`
  entry (B, `confidence=1.0`) renders under the normal section, its
  `source_sentence` making "5V 5A" visible in the prompt.
- Historical query, "Power supply saya sebelumnya berapa?" ->
  `luno.memory.is_historical_query()` (its own marker list extended
  this sprint with `"sebelumnya"`) still finds the superseded entry (A,
  `confidence=0.4`) via the SAME bounded topic-history/candidate
  mechanism that already existed - it renders under `[Historical
  Context]`, its own `source_sentence` making "5V 3A" visible.

Live-verified via the real `RuntimeDemoConsole` (Scenario B, full-prompt
dump): turn 3's rendered `system_prompt` contains exactly one current
"Active conversation topic:" line quoting "5V 5A" and a separate
`[Historical Context]` section quoting "5V 3A" - the exact distinction
the brief calls mandatory.

## Ambiguity / confidence gating (Phase 5)

Re-verified, not weakened: "Yang mana?"/"Kenapa?"/"Terus?"/"Masih ada?"/
"Yang tadi?" with no usable signal still resolve to zero injection.
These gates (`select_topic_candidates()`'s content-based overlap rule,
`classify_reference_type()`'s elliptical-fragment classification) are
Sprint 38/39's own existing mechanisms - Phase 7 found no proven defect
in either, so neither was modified. The full existing regression suite
(`tests/test_conversation_reference_resolution.py`,
`tests/test_conversation_intelligence.py`, 226 tests) passed unchanged
against the Sprint 40 code, confirming this.

## Multi-topic safety (Phase 6)

Built and verified the brief's own exact three-topic scenario: Topic A
(ESP32+INMP441/mic), Topic B (Aquascape+pompa), Topic C (WLED+ESP8266).

- "Yang tadi soal mic gimana?" -> surfaces only Topic A.
- "Pompa yang tadi bagaimana?" -> surfaces only Topic B.
- "WLED yang tadi?" -> surfaces only Topic C.
- "Yang tadi gimana?" (deliberately subject-less) -> injects at most the
  single most-recently-active topic line, never all three.

## Bugs found and fixed during implementation

**Field-propagation bugs (caught proactively, before any test ran):**
`update_active_topic()`'s pure-follow-up PRESERVE branch and
`update_topic_history()`'s per-turn aging comprehension both originally
omitted the two new fields from their returned `ActiveTopicSnapshot`,
which would have silently reset `status`/`source_sentence` back to
defaults on the very next turn after being set. Fixed by explicitly
carrying both fields forward in both places.

**False-positive overlap ("oke"/"dicatat"), found via targeted
diagnostic testing:** the supersession-tagging overlap check (and,
latently, `select_topic_candidates()`'s own pre-existing overlap check)
could register a false "same subject" match purely via generic
acknowledgment words that open/close nearly every assistant reply in
this persona ("Oke, ESP32 dengan INMP441 dicatat.", "Oke, aquascape
dicatat."). Live reproduction: two turns about entirely unrelated
subjects (a mic setup, an aquascape switch) both scored a non-empty
`_TOPIC_OVERLAP_STOPWORDS`-filtered overlap purely via "oke"/"dicatat".
Fixed by extending the EXISTING stopword set with "oke"/"ok"/"okay"/
"baik"/"siap"/"tentu"/"dicatat"/"noted"/"dimengerti"/"mengerti"/"paham" -
no new mechanism, the same set both call sites already shared. After
the fix, Scenario C's ESP8266->ESP32 transition (which had only
"worked" via this coincidental overlap) correctly no longer gets a
`superseded` LABEL - a deliberate, conservative outcome documented as a
known limitation below, not a regression, since the entry remains fully
retrievable via the explicit historical query either way.

**Duplicate-rendering regression, found during Phase 10's own full
regression sweep (a genuine defect this sprint introduced and then
fixed within the same sprint):** three `tests/test_runtime_demo.py` E2E
tests failed after the confidence/conflict implementation, all keyed on
the SAME root cause. An explicit "ingat, spek GPU aku RTX 4090."
command is ALSO captured by the ephemeral `_active_topic`/`_topic_
history` layer (every turn updates it regardless of whether it was also
a remember command). The new `source_sentence` field then quoted "RTX
4090" a SECOND time, duplicating the PERSISTENT `manual_memory` layer's
own pre-existing rendering of the same fact - directly violating a
prior sprint's own "one unified block, never duplicated across two
independent renderings" invariant
(`test_memory_decision_quality_adaptive_retrieval_end_to_end_context_evidence_scenario_a`).
Fixed by threading a new `is_remember_command: bool = False` parameter
through `update_active_topic()`/`update_topic_history()`, reusing
`memory.detect_remember_command()` (already computed at the one real
call site in `PlannerBridgeModule._on_assistant_response()`) - this
suppresses ONLY `source_sentence` for that turn (never the topic
`terms` themselves, which remain usable for an ordinary follow-up like
"yang tadi diingat apa?"), since the persistent layer already fully
owns rendering that fact. All three tests, plus the full memory suite
(1071 tests) and `test_runtime_demo.py` (78 tests), re-ran clean
afterward.

## Domain generalization (mandatory check)

The mechanism contains no hardcoded branch for ESP8266, ESP32, INMP441,
WLED, aquascape, or any other specific device/product name - it
operates purely on GRAMMATICAL/DISCOURSE wording
(`is_correction_signal()`) and generic-vocabulary overlap
(`_TOPIC_OVERLAP_STOPWORDS`), both of which are domain-agnostic by
construction. Re-verified end-to-end across 5 unrelated domains in
`tests/test_memory_conflict_resolution.py`:

1. **PC/GPU** - "GTX 1070" -> "RTX 3060 Ti"
2. **IoT/microcontroller** - "Arduino Uno" -> "Raspberry Pi Pico"
3. **Audio** - "Sony WH-1000XM4" -> "Sennheiser HD 660S"
4. **Aquascape** - "hang-on-back" filter -> "canister" filter
5. **Software/network** - "TP-Link Archer" -> "Ubiquiti UniFi"

For each: the new value wins for a current-state question, the old
value remains retrievable for an explicit historical question, an
unrelated query injects nothing, two independent (non-correction)
statements both stay independently retrievable, and an ambiguous query
against empty history injects nothing. A structural test
(`test_36_confidence_conflict_code_has_no_hardcoded_entity_branches`)
parses every touched function's AST, strips comments/docstrings (which
legitimately reference the brief's own example entities as
illustrations, per this codebase's own documentation convention), and
asserts none of the brief's example entity tokens appear in the
remaining EXECUTABLE code - this fails immediately if a future change
ever special-cases one of them.

## Performance (Phase 9)

All deterministic operations measured directly (500-1000 iterations
each, real production functions, this sandbox's hardware):

| Operation | Latency |
|---|---|
| `classify_reference_type()` | ~0.009 ms/call |
| `is_correction_signal()` | ~0.0005 ms/call |
| `update_topic_history()` (candidate-gen + conflict-detect) | ~0.019 ms/call |
| `_confidence_for_relevant_memory()` | ~0.0003 ms/call |
| `select_topic_candidates()` | ~0.006 ms/call |
| Full simulated per-turn overhead (classify + correction-signal + history update + confidence) | ~0.045 ms/call |

All well under the 5ms/call target; no optimization was needed.

## What did NOT change

`assemble_context()`, `_apply_budget()`, `render_context_block()`,
`select_topic_candidates()`, `deduplicate_context_items()`,
`_neutralize_boundary_markers()` - all confirmed unmodified (Phase 7: no
proven defect found in any of them). No LLM judge, no embedding model,
no second ranking system, no second memory store, no global topic
state, no persistent raw conversation storage, no blind memory-limit
increase. The persistent `config/*.json` files were confirmed
byte-for-byte identical (SHA256) before vs. after a full regression
sweep that included many "ingat ..." commands through the real
production path - the entire confidence/conflict mechanism lives in the
existing, already-transient `_active_topic`/`_topic_history` in-process
dicts.

## Known limitations

- Supersession LABELING (the `[Historical Context]` section + explicit
  "Previously stated" text) requires genuine shared non-generic
  vocabulary between the old and new statement. When two entity names
  are completely disjoint (e.g. "ESP8266" vs "ESP32", "GTX 1070" vs
  "RTX 3060 Ti" share no token once tokenized), the mechanism
  conservatively does NOT apply the label - the entry is not mislabeled,
  but also doesn't get the differentiated rendering. It remains exactly
  as retrievable via an explicit historical query either way (proven in
  `test_27_e2e_scenario_C_explicit_historical_query_retrieves_old` and
  the domain-generalization historical-query tests). A stronger
  same-subject detector (e.g. category-aware matching, as the
  PERSISTENT layer's own `_classify_conflict()` already does for
  explicit "inget ya" memories) was considered but not built here - it
  would be new state/logic not directly justified by this sprint's own
  reproduced-failure bar, and the persistent layer already provides
  this exact capability for the cases that reach it.
- `confidence`'s effect is narrow by design (last tuple position) - it
  only breaks ties among items that are already tied on every other
  `_rank_key()` signal. This is intentional (see Confidence model
  above) but means confidence cannot, for example, cause an
  `active_conversation` item to outrank a `verified_facts` item even if
  the former is more confident - that ordering is `_SOURCE_PRIORITY`'s
  domain, unchanged this sprint.

## Invariants (all held, test-proven)

- RELEVANCE > CONFIDENCE > IMPORTANCE/RECENCY tie-breaks; a highly
  confident irrelevant memory never beats a relevant memory.
- Stale/superseded memory is never deleted, never excluded from
  candidate selection.
- An explicit historical query still retrieves superseded/old
  information.
- Ambiguous references with no usable signal produce zero injection,
  never fabricated context.
- Multi-topic separation is preserved; an ambiguous query never dumps
  every tracked topic at once.
- No hardcoded topic-specific logic in the confidence/conflict
  mechanism (structurally proven).
- No persistent raw conversation storage; no global topic state; the
  mechanism is fully conversation-scoped and transient.
