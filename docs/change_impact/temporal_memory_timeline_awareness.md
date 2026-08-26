# Change Impact: Temporal Memory & Timeline Awareness (Sprint 41)

## Goal

Make Luno's existing conversation/memory pipeline distinguish temporal
state - CURRENT, HISTORICAL, PLANNED, COMPLETED - without an LLM judge,
embeddings, a second memory store, a second ranking system, unrestricted
conversation storage, global topic state, raw conversation persistence,
or any change to TTS/streaming/response-output. This is not a new memory
system: it extends the existing active-topic/topic-history/conflict-
resolution pipeline Sprint 4/38/39/40 already built.

## Method

Phase 0 was a read-only audit confirming no existing mechanism classifies
a stored fact's temporal role beyond Sprint 40's binary active/superseded
axis - `_TEMPORARY_WORDING_RE` (importance/lifecycle decay) and
`_PLANNING_INTENT_MARKERS` (query-intent taxonomy) are both different,
non-reusable-as-is mechanisms serving different purposes. Phase 1 built
live E2E probes through the real `RuntimeDemoConsole` (not direct
function calls) for all 7 scenarios (A-G) the brief specified, before
writing any fix.

## Root cause (Phase 2)

Three independent, separately-reproduced defects, not one:

1. **Classification bug.** `luno.memory.is_correction_signal()`'s bare
   `"sekarang"` alternative fired on ordinary CURRENT-state QUESTIONS,
   not just declarative corrections. Live reproduction: "Sekarang aku
   pakai GPU apa?" (Scenario C, turn 3) wrongly triggered
   `update_topic_history()`'s supersession-tagging logic, marking a
   PLANNED entry ("Minggu depan... RTX 5070") as "superseded" merely
   because the question shared the token "rtx" with it - nothing was
   actually replaced.

2. **Candidate-eligibility gap.** `select_topic_candidates()` is pure
   lexical overlap - it only offers an entry when the CURRENT turn's own
   words literally share a non-generic token with that entry's stored
   terms. Live reproduction: "Sebelumnya aku pakai apa?" (Scenario B,
   turn 4) shares zero tokens with "Aku pakai RTX 3060 Ti." (the word
   "sebelumnya" never appears in the original statement); "Sekarang aku
   pakai board apa?" (Scenario D, turn 3) shares zero tokens with "Sudah
   aku pindah ke ESP32-S3." AND is a rich/non-followup turn, so the
   single-slot `is_short_followup`-gated fallback in `main_runtime_
   demo.py` never even attempts it either. Confirmed via live
   instrumentation that this is a genuine candidate ELIGIBILITY gap at
   the retrieval-branch level, not a classification, ranking, or
   rendering defect - the first loss stage is precisely "no branch ever
   attempts to offer this content."

3. **Whole-turn-only classification.** A single compound sentence naming
   multiple distinct temporal facts about the same subject ("Aku dulu
   pakai GTX 1070. Sekarang pakai RTX 3060 Ti. Bulan depan rencana
   upgrade ke RTX 5070.", Scenario F) collapsed into ONE topic-history
   entry with a SINGLE whole-turn status (`classify_temporal_status()`
   applied to the entire text, precedence order finds "planned" and
   stops there) - silently discarding the HISTORICAL and CURRENT facts
   the same sentence also names.

## Temporal state model (Phase 3-4)

Reuses the EXISTING `ActiveTopicSnapshot.status` field, extending its
value set from 2 (`"active"`/`"superseded"`) to 5:

- `"active"` - the current fact (unchanged default).
- `"superseded"` - replaced by newer information (Sprint 40, unchanged).
- `"planned"` (new) - an intent stated for the future; never the current
  state, never overwrites the current entry.
- `"completed"` (new) - a plan that was fulfilled; functionally CURRENT.
- `"cancelled"` (new) - a plan explicitly called off; never deleted,
  distinctly labeled so it is never mistaken for a live plan.

No new field was added - the sprint's own "prefer a bounded temporal
attribute attached to existing structures" instruction was followed
literally, avoiding the "four independent stores" anti-pattern the brief
explicitly warned against.

`luno.memory.classify_temporal_status(text)` (new) is a deterministic,
bounded classifier over a whole turn, returning exactly one of
`"cancelled"`/`"completed"`/`"planned"`/`"none"` (precedence in that
order - CURRENT statements return `"none"`, treated as an ordinary rich
turn exactly like every turn before this sprint). Marker lists:
`_PLANNED_TIME_MARKERS` (reuses `_TEMPORARY_WORDING_RE`'s own future-time
vocabulary as its base, plus "bulan depan"/"rencana"), `_PLANNED_INTENT_
MARKERS`, `_PLANNED_INTENT_VERB_RE` (a "mau/akan/bakal + change-verb"
combo, deliberately requiring co-occurrence per Sprint 39's own
established caution against a bare "mau" false-positive), `_COMPLETED_
MARKERS` ("sudah"/"udah"/"telah"/"selesai"/"baru saja"), `_CANCELLED_
MARKERS` ("batal"/"dibatalkan"/"gak jadi"/etc.).

`_classify_clause_temporal_role()` (new, `luno.memory_context`) is the
per-CLAUSE counterpart, used only by the compound-sentence split (below)
- reuses `classify_temporal_status()` for planned/completed/cancelled and
the new `luno.memory.is_historical_statement()` (reuses the EXISTING
`_TEMPORAL_OLD_MARKERS` word list `_is_temporal_change()` already uses,
not a new one) for HISTORICAL clauses.

## Root-cause fix 1: interrogative gating

`is_correction_signal()` now gates ONLY the bare `\bsekarang\b`
alternative of `_CORRECTION_RE` on the text NOT being interrogative
(`_is_interrogative()`, new - trailing "?" or common Indonesian/English
question words). Every OTHER correction phrase (explicit "ganti...
menjadi", "bukan...tapi", "actually", "correction", dual "dulu...
sekarang") remains an unconditional signal regardless of question shape,
via a new `_CORRECTION_RE_STRONG` regex derived PROGRAMMATICALLY from
`_CORRECTION_RE.pattern` (not hand-duplicated, to prevent drift). This
fix is scoped exclusively to `is_correction_signal()` - `_classify_
conflict()` (persistent layer) and `classify_query_intent()`'s
"correction_update" categorization both call `_CORRECTION_RE.search()`
directly and are unaffected.

## Root-cause fix 2: temporal conflict resolution (Phase 5)

`update_topic_history()`'s rich-turn push branch computes `temporal_
status = classify_temporal_status(user_text)` and `overlap` (against the
current front entry) up front, then dispatches:

- **"planned"** - pushes a new entry `status="planned"`, front NOT
  retagged (a stated plan never supersedes the current fact).
- **"completed"** AND front is `"planned"` AND real overlap - retags
  front to `"completed"`, pushes this turn's own content as the new
  `"active"` entry (PLANNED -> COMPLETED -> CURRENT, no duplicate
  contradictory current states).
- **"cancelled"** AND front is `"planned"` AND real overlap - retags
  front to `"cancelled"` (never deleted), pushes this turn's own content
  normally.
- **else** - Sprint 40's original `is_correction_signal()`-gated
  supersession logic, byte-for-byte unchanged, now reusing the
  pre-computed `front`/`overlap` variables.

## Root-cause fix 3: compound-sentence split (Phase 4/7, Scenario F)

New, narrowly-gated addition: `_split_temporal_clauses()` splits a turn's
text on `.`/`!`/`?` boundaries; `_build_compound_clause_entries()`
returns a list of `(terms, status, source_sentence)` tuples ONLY when
the turn has >=2 clauses that BOTH carry real topic terms AND resolve to
>=2 genuinely DIFFERENT temporal roles - `None` (fall through completely
unchanged to the existing whole-turn dispatch) for every ordinary
single-fact turn, and for a multi-clause turn whose clauses all share one
role. When triggered, one topic-history entry is pushed PER
differentiated clause instead of one blended entry, and the whole-turn
dispatch is skipped entirely for that turn (cross-turn retagging still
only ever happens on a SUBSEQUENT turn, exactly as before).

## Root-cause fix 4: retrieval fallback (Phase 6)

`select_temporal_fallback_candidate(history, text)` (new, `luno.memory_
context`) - a strictly LAST-RESORT fallback wired into `main_runtime_
demo.py` as a 4th `elif` branch in the existing retrieval call site
(after ordinal targets / topic-history candidates / single-slot
short-followup, never gated on `is_short_followup`). Fires ONLY when:

1. The turn's own wording unambiguously asks a current/historical/
   planned-state question (`is_current_state_query()`/`is_historical_
   query()`/`is_planned_query()` - reuses `is_historical_query()`,
   pre-existing; the other two are new, symmetric detectors).
2. `select_topic_candidates()`'s own lexical branch already found
   nothing (a genuine lexical match always wins).
3. (Phase 8 ambiguity-safety pre-check, added after a regression was
   caught against Sprint 40's OWN test suite - see below) the query
   carries at most 1 "extra" content word beyond stopwords and the
   matched temporal marker's own wording.

Among status-eligible entries (`_TEMPORAL_FALLBACK_ELIGIBLE_STATUS`:
current -> active/completed, historical -> superseded/cancelled, planned
-> planned), entries whose OWN `source_sentence` is itself a question
(a self-echo of an earlier turn's own query, pushed into topic history
like any rich turn) are excluded from eligibility - a question is never
itself a stated fact. Remaining eligible entries are ranked by real
(stopword-filtered) overlap with the query first, recency only breaking
ties; when no eligible entry overlaps the query at all, falls back to
recency ONLY among entries mutually related to each other (an evolving
single subject, e.g. Scenario D's planned->completed ESP32-S3 pair) -
returns `None` rather than guess across two mutually-unrelated
candidates.

## Two bugs found and fixed during Phase 7 (multi-topic safety testing)

**Bug A - pre-existing cross-topic contamination.** `"sekarang"` was NOT
previously in `_TOPIC_OVERLAP_STOPWORDS`. Live reproduction (this
sprint's own 5-domain matrix): two completely unrelated "Sekarang aku
pakai X." statements (different domains, stated back to back) falsely
registered "real overlap" purely via the shared word "sekarang",
triggering `is_correction_signal()`'s supersession retagging across
UNRELATED topics - a genuine PRE-EXISTING (Sprint 40) bug, newly exposed
by testing two current-state topics in the same conversation. Fixed by
adding `"sekarang"` to the shared `_TOPIC_OVERLAP_STOPWORDS` set - the
same "generic, subject-agnostic word" precedent `"aku"`/`"mau"`/`"soal"`
already established in that same set.

**Bug B - ambiguity-safety regression against Sprint 40's own test
suite.** Re-running the FULL existing regression suite caught
`test_memory_conflict_resolution.py::test_33_domain_generalization_
unrelated_query_no_injection[Aquascape]` newly failing:
"Berapa harga tiket bioskop sekarang?" - a fully independent question
about movie ticket prices, unrelated to any stored fact - was wrongly
classified `is_current_state_query()=True` (it contains "sekarang" and
is interrogative) and injected an unrelated aquascape-filter memory into
the prompt. This is exactly the "temporal wording becomes an excuse to
inject the most recent memory" failure Phase 8 explicitly warns against.
Fixed with a bounded residual-token pre-check in `select_temporal_
fallback_candidate()`: the fallback only fires when the query's content
- after removing stopwords AND every word from `is_current_state_
query()`/`is_historical_query()`/`is_planned_query()`'s own marker
tuples - is at most 1 token. Every real, reproduced case that legitimately
needs this fallback (Scenario B/D/F) has 0-1 residual content words; a
genuinely independent, unrelated question realistically carries several
("harga", "tiket", "bioskop" - 3 residual words, well above the floor).

## Retrieval semantics (Phase 6)

Temporal query wording now influences WHICH existing candidates are
offered, without a second ranking system: "sekarang pakai apa?" ->
CURRENT (active/completed); "sebelumnya pakai apa?" -> HISTORICAL
(superseded/cancelled); "rencana upgrade apa?" -> PLANNED. Temporal
status acts purely as candidate ELIGIBILITY/filter plus a same-mechanism
overlap-based tie-break - `_rank_key()`, `_apply_budget()`, and `render_
context_block()` are all untouched. RELEVANCE > secondary signals is
preserved: a genuine lexical match from `select_topic_candidates()`
always wins over the temporal fallback, which only activates as the last
resort.

## Ambiguity safety (Phase 8)

"Yang mana?"/"Kenapa?"/"Terus?"/"Masih ada?"/"Apa?"/"Gimana?"/"Yang
tadi?" with no usable signal still resolve to zero injection (fresh
console, single turn, matching Sprint 40's own established test
precedent exactly). The residual-token pre-check (Bug B fix above)
additionally proved, via a REAL regression catch against Sprint 40's own
existing test, that a temporal word alone must never force retrieval of
an unrelated memory.

## Multi-topic safety (Phase 7)

Verified across 5 unrelated domains (PC/GPU, IoT/microcontroller, Audio,
Aquascape, Software/network), each with CURRENT/HISTORICAL/PLANNED
statements, then cross-domain queries: a query about one domain's
current state never surfaces another domain's most-recently-stated
value merely because both share `status="active"` and generic temporal
wording.

## Domain generalization (Phase 9)

No hardcoded ESP8266/ESP32/INMP441/RTX/WLED/aquascape branch in any
Sprint 41 function - proven structurally (AST-based, docstrings/comments
stripped before scanning executable code only, same technique Sprint 40
established) via `test_62_temporal_code_has_no_hardcoded_entity_
branches`, plus a check that the new marker word lists themselves
(`_PLANNED_TIME_MARKERS`, `_PLANNED_INTENT_MARKERS`, `_COMPLETED_
MARKERS`, `_CANCELLED_MARKERS`) contain no entity tokens.

## Performance (Phase 10)

All new deterministic operations measured well under the 5ms/call
target: `classify_temporal_status()` well under 1ms/call,
`update_topic_history()`'s temporal dispatch under 5ms/call, the
compound-clause split under 5ms/call, `select_temporal_fallback_
candidate()` well under 1ms/call. No network, LLM, or embedding calls
anywhere in the new code paths.

## Full regression (Phase 11)

Full `tests/` tree (excluding the 3 pre-existing, documented
uncollectible/slow files - `test_main_bargein.py`/`test_root_main_
bargein.py`, missing `faster_whisper`; `test_dashboard.py`, run
separately per established precedent) - 2473 passed, 12 failed. Every
failure investigated and matched against the documented baseline, none
new:

- `test_mic_device_index.py` (6) - ENVIRONMENT-SPECIFIC, this sandbox's
  real `.env` sets `MIC_DEVICE_INDEX=1`.
- `test_production_launcher.py::test_07_health_checks_all_pass_in_
  default_mock_configuration` (1) - ENVIRONMENT-SPECIFIC, live
  credentials configured in this checkout.
- `test_real_adapters.py` (2) - same ENVIRONMENT-SPECIFIC class.
- `test_state_isolation.py::test_isolate_persistent_state_drains_
  stragglers_before_monkeypatch_reverts` (1) - documented, known
  scheduling-jitter flake across many prior sprints' baselines.
- `test_llm_tts_streaming_production.py::test_14_cancellation_during_
  synthesis` (1) - documented, known timing-sensitive flake; passes
  reliably in isolation (verified 1/1 this run).
- `test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_
  never_plays` (1) - documented, known timing-sensitive flake in this
  same file across many prior sprints' baselines.

The one genuine, newly-caught issue during this sweep (Bug B above,
`test_33` Aquascape) was investigated, root-caused, and fixed - not
dismissed as "probably pre-existing." Re-running confirmed it now
passes, and no other test regressed as a result of the fix.

## Persistent-state safety (Phase 12)

SHA256 + mtime of all 384 `config/*.json` files captured before and
after the full regression sweep (including this sprint's own new test
file, which exercises many "ingat ..."-adjacent code paths indirectly
via the shared `RuntimeDemoConsole` harness) - 0 new files, 0 removed
files, 0 content-changed files. All Sprint 41 state
(`ActiveTopicSnapshot.status`'s extended value set, the compound-clause
split, the retrieval fallback) remains entirely bounded, transient,
in-process (`_active_topic`/`_topic_history` dicts) - no raw
conversation dump, no new persistence.

## Known limitations

**Lexical/paraphrase mismatch (documented, not fixed).** A planned-
intent query using different vocabulary than the ORIGINAL planning
statement (e.g. asking with "beli" when the stored plan said "ganti")
can surface an irrelevant, lexically-matched entry ahead of the correct
temporal fallback, because `select_topic_candidates()`'s own lexical
branch "succeeds" (finds SOMETHING, even weakly matched) before the
fallback branch ever runs. This is the same class of limitation Sprint
40 already documented for two disjoint entity names ("ESP8266" vs
"ESP32") sharing no vocabulary. Not fixed: closing it deterministically
would require either a synonym dictionary or an embedding model, both
explicitly forbidden by this sprint's own constraints. The sprint's own
Critical Rule instructs documenting an unjustifiable feature as
unnecessary rather than inventing a complex workaround - a synonym/
embedding-based fix was assessed and rejected on exactly this basis.

**Self-echoed questions in topic history (pre-existing, mitigated for
this sprint's own retrieval fallback, not eliminated everywhere).** Any
rich (non-followup) turn - including the user's OWN question - gets
pushed into `_topic_history` as an ordinary entry (a pre-existing
Sprint 39/40 behavior, not introduced this sprint). `select_temporal_
fallback_candidate()` explicitly excludes entries whose `source_
sentence` is itself a question from its own eligibility, but `select_
topic_candidates()`'s own, separate, unmodified lexical-overlap branch
can still occasionally surface a self-echoed question ahead of the
correct fact when the echoed question happens to share a literal word
with the current query (see Scenario C turn 4's own pre-existing
limitation, documented in prior sprint output). Not modified this
sprint - Phase 7's own instruction is "if the existing architecture
already handles a scenario correctly, do not rewrite it," and `select_
topic_candidates()` was found to have no PROVEN defect of its own beyond
this narrow, already-limited interaction.

## Invariants

- Memory ranking (`_rank_key()`) - **unchanged**.
- Memory budget (`_apply_budget()`) - **unchanged**.
- Topic state model - **extended** (status value set 2 -> 5, same
  field, no new field).
- Retrieval pipeline - **extended** (one new last-resort fallback
  branch, gated behind existing branches).
- LLM judge - **not added**.
- Embedding model - **not added**.
- TTS - **unchanged**.
- Streaming - **unchanged**.
- Persistent raw conversation storage - **not added**.
- Global topic state - **not added** (state remains per-conversation,
  bounded, in-process).

## Files modified

- `luno/memory.py` - `_is_interrogative()`/`_INTERROGATIVE_RE` (new),
  `_CORRECTION_RE_STRONG` (new, derived programmatically), `is_
  correction_signal()` rewritten with interrogative gating,
  `classify_temporal_status()` (new) + `_PLANNED_TIME_MARKERS`/`_PLANNED_
  INTENT_MARKERS`/`_PLANNED_INTENT_VERB_RE`/`_COMPLETED_MARKERS`/
  `_CANCELLED_MARKERS` (new), `is_historical_statement()` (new),
  `is_current_state_query()` (new) + `_CURRENT_STATE_QUERY_MARKERS`
  (new), `is_planned_query()` (new) + `_PLANNED_QUERY_MARKERS` (new).
- `luno/memory_context.py` - `_CONFIDENCE_PLANNED`/`_CONFIDENCE_
  CANCELLED`/`_STATUS_CONFIDENCE` dict (new, replaces the Sprint 40
  if/elif chain), `_confidence_for_relevant_memory()` simplified,
  `_STATUS_LABELS` dict inside `active_topic_to_relevant_memory()`
  (new, replaces a single ternary), `relevant_memory_to_context_item()`'s
  `historical=` derivation extended to `"cancelled"`, `"sekarang"` added
  to `_TOPIC_OVERLAP_STOPWORDS`, `update_topic_history()`'s rich-turn
  push extended with the compound-clause-split check plus the planned/
  completed/cancelled dispatch, `_split_temporal_clauses()`/`_classify_
  clause_temporal_role()`/`_build_compound_clause_entries()` (new),
  `_TEMPORAL_FALLBACK_ELIGIBLE_STATUS`/`_TEMPORAL_QUERY_MARKER_TOKENS`/
  `_TEMPORAL_FALLBACK_MAX_RESIDUAL_TOKENS`/`select_temporal_fallback_
  candidate()` (new).
- `main_runtime_demo.py` - one new 4th `elif` branch in the existing
  retrieval call site (after ordinal targets / topic-history candidates
  / single-slot short-followup), calling `select_temporal_fallback_
  candidate()`.
- `ARCHITECTURE_GUARD.md` - new §41.
- `docs/testing/regression_baseline.md` - updated with this sprint's
  full-suite run and the 12 known baseline failures.

## Files created

- `tests/test_temporal_memory_timeline_awareness.py` (75 tests).
- `docs/change_impact/temporal_memory_timeline_awareness.md` (this
  file).

No changes to `assemble_context()`, `_apply_budget()`, `render_context_
block()`, `select_topic_candidates()`, `update_active_topic()`
(single-slot dispatch, deliberately left untouched per Phase 3's own
scoping decision - the new topic-history-based fallback was found
sufficient), the LLM model, TTS voice/model, streaming architecture, or
response-depth semantics.
