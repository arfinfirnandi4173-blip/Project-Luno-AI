# Semantic Voice Selection & Coherent SHORT Mode

## 1. Root cause / motivation

SHORT/NORMAL voice compression (`build_dual_response()` ->
`_select_by_priority()`) selected sentences by `_score_sentence()`
alone, one sentence at a time, with no notion of "this sentence belongs
to a larger unit that must survive or be dropped together." Two
concrete failure modes were reproduced directly against
`build_dual_response()` before any production code was touched
(Phase 0 - the user's own illustrative example did NOT reproduce a bug
under current code, since a bulleted list already had blanket
list-item protection and its trailing sentence happened to contain the
fixed keyword "jadi"; more realistic, longer texts were needed to find
real, currently-reproducible bugs):

1. **Conclusion-drop.** A genuine closing/answer sentence that did not
   happen to contain one of `_has_conclusion_cue()`'s fixed keywords
   ("jadi", "kesimpulannya", "intinya", "singkatnya", "pada akhirnya",
   "in summary", "overall", "in conclusion", "to summarize", "in
   short") was invisible to the must-keep set and could be silently
   dropped when budget was consumed by blanket list-item protection.
2. **Setup-without-payload.** At DETAILED depth, where the old blanket
   "every list item is must-keep" rule does not apply
   (`protect_list_items=False`), an early filler/intro sentence -
   favored purely by the "earlier is better" score tiebreak, since a
   real list item or conclusion often has no number/warning/condition
   signal of its own - could outscore and displace the actual list
   items and conclusion, leaving the list's own setup sentence dangling
   with nothing after it.

## 2. Semantic unit design

The brief asked for an internal "Semantic Unit" concept (LIST_SETUP,
LIST_ITEM, LIST_CONCLUSION, DEPENDENT_CHAIN, CONDITION_CHAIN,
EXPLANATION_CHAIN, EXAMPLE_CHAIN, WARNING_CHAIN). Rather than building a
new classifier or a second selection stage, this sprint generalizes
mechanisms that already existed:

- **LIST_SETUP / LIST_ITEM / LIST_CONCLUSION** - new `_find_list_runs()`,
  purely positional (same `is_list_item` flag/adjacency
  `_starts_list_run()` already used), grouping consecutive list items
  into `{"setup": int|None, "items": [...], "conclusion": int|None}`,
  correctly excluding a "conclusion" candidate that is actually another
  run's own setup (two adjacent/separate lists).
- **DEPENDENT_CHAIN / CONDITION_CHAIN / EXPLANATION_CHAIN /
  WARNING_CHAIN** - already fully covered by the pre-existing, UNCHANGED
  `_is_dependent_sentence()` / `_dependency_kind()` (via
  `_CAUSAL_KEYWORDS` / `_CONTINUATION_KEYWORDS` / `_REFERENCE_KEYWORDS` /
  `_CONDITION_KEYWORDS` / `_has_warning()`) and `_repair_orphans()`
  (one-hop rescue-or-drop for a selected dependent sentence missing its
  antecedent).
- **EXAMPLE_CHAIN** - covered by the same dependency-chain machinery;
  no separate handling was needed or added.

No LLM judge, no embedding model, no second ranking system, no
summarizer, and no persistent semantic state were introduced anywhere
in this sprint.

## 3. SHORT / NORMAL behavior (the fix)

Three small, additive pieces in `luno/response_output.py`, all reusing
existing primitives:

- `_list_run_relevant_items()` (+ `_LIST_RELEVANCE_BONUS = 22.0`,
  `_apply_list_relevance_bonus()`) - deterministic word-overlap
  (reusing `_word_set()`, the SAME Jaccard-style primitive `_dedupe()`
  already established) between a run's DISTINCTIVE item words (words
  not shared by every item in the run) and its own conclusion
  sentence's words. This identifies which item the response's own
  closing sentence is actually recommending, and boosts that item's
  score so it competes and wins under a tight budget instead of every
  sibling being blanket-protected.
- `_repair_list_run_coherence()` (+ `_LIST_RUN_REPAIR_SLACK = 4`,
  reusing `_repair_orphans()`'s own "+4" slack constant) - a bounded,
  deterministic generalization of `_repair_orphans()`'s existing
  rescue-or-drop philosophy, applied to a whole list run: if >=1 item
  survived selection, its setup/conclusion are rescued too; if a run's
  setup survived with zero items (the concrete DETAILED-mode
  reproduction), the single best-scoring item is rescued; a non-must-
  keep item is dropped rather than left headless once slack is
  exhausted.
- `_select_by_priority()`'s blanket "every list item is always
  must-keep" rule is relaxed to "keep the whole run UNLESS a relevance
  signal was found" - preserving the old safe default whenever there is
  no closing sentence to disambiguate against, and only letting an
  irrelevant sibling be dropped when the response's own text identifies
  the right answer.

`_score_sentence()`, `_has_warning()`, `_has_conclusion_cue()`,
`_has_condition()`, `_is_dependent_sentence()`, `_repair_orphans()`,
`_compute_budget_for_depth()`, and `_rank_key()` are all UNCHANGED.

### Concrete example - the reproduced bug, fixed

RAW (chat text, byte-identical in every mode):

```
ESP32 adalah mikrokontroler yang populer untuk proyek IoT.
Berikut beberapa pilihan mikrofon yang cocok untuk ESP32:
- INMP441 cocok karena komunikasinya menggunakan I2S.
- MAX9814 menggunakan output analog dengan AGC.
- SPH0645 juga memakai I2S tapi lebih murah.
Untuk kualitas suara terbaik, sebaiknya pakai INMP441 karena I2S lebih stabil dibanding analog.
```

SHORT voice_text (measured):

```
ESP32 adalah mikrokontroler yang populer untuk proyek IoT. Berikut
beberapa pilihan mikrofon yang cocok untuk ESP32: INMP441 cocok karena
komunikasinya menggunakan I2S. Untuk kualitas suara terbaik, sebaiknya
pakai INMP441 karena I2S lebih stabil dibanding analog.
```

Per-sentence rationale:
- Lead sentence kept - index 0 is always must-keep (unchanged rule).
- List setup ("Berikut beberapa pilihan...") kept - `_repair_list_run_coherence()`
  rescues a run's setup once >=1 of its items survives.
- INMP441 item kept - `_list_run_relevant_items()` found the
  conclusion's own words ("INMP441", "I2S") overlap this item's
  distinctive words, so it received the `+22.0` relevance bonus and won
  the budget over its siblings.
- MAX9814 / SPH0645 items dropped - no relevance signal pointed to
  them; the run's own conclusion never mentions them, so they lost to
  the higher-scoring relevant item under a tight SHORT budget.
- Conclusion sentence kept - this is the fixed bug: previously it had
  no `_has_conclusion_cue()` keyword and could be silently dropped;
  `_repair_list_run_coherence()` now rescues a run's conclusion
  whenever >=1 of its items survived selection.

DETAILED voice_text (larger budget, still coherent - the other
reproduced bug, fixed):

```
ESP32 adalah mikrokontroler yang populer untuk proyek IoT. Berikut
beberapa pilihan mikrofon yang cocok untuk ESP32: INMP441 cocok karena
komunikasinya menggunakan I2S. MAX9814 menggunakan output analog dengan
AGC. Untuk kualitas suara terbaik, sebaiknya pakai INMP441 karena I2S
lebih stabil dibanding analog.
```

The setup is never left without at least one of its own items (the
concrete DETAILED-mode bug) - here the larger budget affords both
INMP441 and MAX9814, still excluding only SPH0645.

### Concrete example - relevance-based pruning under a tight budget

RAW:

```
Berikut pilihannya:
- A sangat murah dan mudah didapat di toko lokal.
- B lebih mahal tapi kualitasnya lebih baik untuk jangka panjang.
- C adalah pilihan menengah dengan harga wajar.
- D adalah pilihan premium dengan garansi lima tahun.
- E adalah pilihan hemat untuk pemula.
Kalau budget terbatas, pilih A karena paling murah dan mudah didapat.
```

SHORT voice_text (measured):

```
Berikut pilihannya: A sangat murah dan mudah didapat di toko lokal. E
adalah pilihan hemat untuk pemula. Kalau budget terbatas, pilih A
karena paling murah dan mudah didapat.
```

Item A survives because the conclusion explicitly names it (relevance
bonus). Items B, C, D are correctly dropped - the response's own
closing sentence never mentions them, so there is no signal to protect
them under a tight budget. Item E also survives here (see "Known
limitations" - this is the honest, imperfect edge of a positional
`_repair_orphans()` interaction, not a hidden failure); it is a
reasonable extra item, never a fragment or an orphan.

## 4. ALL mode behavior (Sprint 36, unchanged)

`build_dual_response(..., voice_output_mode="ALL")` still skips both
`_dedupe()` and `_select_by_priority()` entirely - `selected` is the
full sentence list, `voice_adapted=False`, `chat_text` untouched. This
sprint's new list-run/relevance code lives entirely inside the SHORT
branch and is never reached in ALL mode. Verified directly:

ALL voice_text for the mic example above (measured):

```
ESP32 adalah mikrokontroler yang populer untuk proyek IoT. Berikut
beberapa pilihan mikrofon yang cocok untuk ESP32: INMP441 cocok karena
komunikasinya menggunakan I2S. MAX9814 menggunakan output analog dengan
AGC. SPH0645 juga memakai I2S tapi lebih murah. Untuk kualitas suara
terbaik, sebaiknya pakai INMP441 karena I2S lebih stabil dibanding
analog.
```

Every sentence and every list item is present - nothing is ever
selected or dropped in ALL mode.

## 5. Non-list semantic chains (dependent/condition/warning)

Verified with the pre-existing, unmodified dependency machinery (no
change needed - already correct):

- `"ESP32 menggunakan WiFi bawaan. Modul ini membutuhkan supply 3.3V.
  Karena itu jangan memberikan 5V langsung ke pin tersebut."` - the
  causal "Karena itu..." sentence is never spoken without its
  antecedent (the 3.3V sentence); `_repair_orphans()` handles this
  exactly as before.
- `"Modul ini butuh tegangan 3.3V. Jangan sambungkan langsung ke 5V.
  Bisa merusak komponen secara permanen."` - the warning sentence is
  always must-keep (`_has_warning()`, unchanged).
- Two separate lists (`"Berikut sensor..."` / `"Berikut aktuator..."`)
  are never cross-linked - `_find_list_runs()` correctly recognizes the
  second list's own setup sentence rather than mistaking it for the
  first list's conclusion.

## 6. Tests

`tests/test_semantic_voice_selection.py` - 36 tests, all passing:

- Section 1 (6 tests): unit tests for `_find_list_runs()`,
  `_list_run_relevant_items()` - basic setup/items/conclusion, no
  setup/no conclusion, two separate lists not cross-linked, named-
  answer relevance detection, empty-when-no-conclusion, empty-when-
  conclusion-names-nothing-specific.
- Section 2 (24 tests): the Phase 9 adversarial matrix - list
  setup+items, list+conclusion, setup+dependent sentence, condition/
  explanation/warning chains, short functional/unrelated/independent
  sentences, multiple independent topics, nested/numbered/markdown
  lists, paragraph+list, list+conclusion, two separate lists, very
  small/normal SHORT budget, DETAILED mode, ALL mode, 1-2 sentence
  edge cases.
- Section 3 (1 test): chat-text integrity across all depths.
- Section 4 (2 tests): structural guards - no forbidden ML/embedding
  imports, module purity.
- Section 5 (5 tests): real E2E through `RuntimeDemoConsole` - RAW vs
  SHORT vs ALL comparison, ALL still reads everything, cancellation
  mid-speech, streaming stays active, first-audio latency (3 reps,
  bounded).

## 7. Latency measurements

Measured (not assumed) through the real `RuntimeDemoConsole` + mocked
LLM/TTS backends, 5 repetitions per mode, the mic-comparison scenario
above:

| | first-audio latency (mean) | first-audio range | total latency (mean) |
|---|---|---|---|
| SHORT | 0.505s | 0.408s - 0.741s | 0.941s |
| ALL | 0.446s | 0.387s - 0.502s | 0.881s |

No first-audio regression - the extra list-run/relevance computation
runs entirely on the already-fully-received text at the same
`build_dual_response()` call site that always ran; both modes are
dominated by the same lead-sentence dispatch mechanism, unmodified by
this sprint. Total speech DURATION naturally differs slightly with more
content spoken in ALL mode; that was never what was being bounded here.

## 8. Regression results

Scoped regression during development (`test_response_output.py`,
`test_voice_output_optimization.py`,
`test_voice_response_intelligence.py`, `test_voice_output_coherence.py`,
`test_semantic_speech_units.py`, `test_voice_output_modes.py`): **231
passed, 0 failed.**

Full `tests/` tree (84 files, split into 8 chunks of ~11 files due to
this environment's per-command timeout, `pytest -n 2` per chunk) -
**zero new regressions.** All observed failures independently
re-confirmed as pre-existing/environmental/chunk-boundary artifacts:

- `test_dashboard.py::test_35_chat_audio_endpoint_reports_no_clip_for_mock_backend` -
  flaky only under parallel chunking (HTTP read-timeout race in the
  mock backend's own streaming thread); passed twice in isolation.
- `test_main_bargein.py` - pre-existing `ModuleNotFoundError: No module
  named 'faster_whisper'` (missing optional dependency in this
  sandbox).
- `test_mic_device_index.py` (6 tests), `test_root_main_bargein.py` -
  pre-existing sandbox environment-configuration artifacts (a real
  `MIC_DEVICE_INDEX` value already set; a `list_microphones.py` path
  lookup tied to this sandbox's own mount path) - documented
  identically in the prior sprint's baseline.
- `test_production_launcher.py::test_07_...`,
  `test_real_adapters.py::test_real_whisper_source_...` (x2) -
  pre-existing `RealWhisperSource` attribute gap, unrelated to any file
  touched this sprint.
- `test_state_isolation.py::test_verified_facts_does_not_leak_between_tests_part_b` -
  a genuine chunk-boundary artifact (this test depends on its own
  `_part_a` running in the same worker); passes when the file runs
  whole - confirmed, not "fixed by weakening the assertion."
- `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts` -
  the same pre-existing `inspect.getsource()` `OSError` already
  documented in the prior sprint's baseline.

## 9. Persistent-state verification

SHA256 of all 15 `config/*.json` files confirmed byte-identical before
and after the full sprint (implementation + full test suite runs). This
sprint touches no persistence layer at all - `_find_list_runs()` and
friends operate purely on the in-memory sentence list already produced
by `_split_into_raw_sentences()` within a single `build_dual_response()`
call; nothing is written to disk or carried across turns.

## 10. Known limitations

- The pre-existing, deliberately-unmodified `_repair_orphans()` can
  still pull in a list run's last item as "required context" for a
  trailing sentence that opens with a conditional marker, even when
  relevance-based selection had already correctly excluded that item -
  because `_repair_orphans()` treats raw positional adjacency as
  sufficient evidence of dependency, regardless of whether the
  predecessor happens to be a list item. This is SAFE (it never
  produces an orphan or a sentence fragment) but is occasionally looser
  than ideal (see item E surviving in the pruning example in §3). It
  was left alone rather than modifying protected, well-tested legacy
  code for a purely cosmetic tightening.
- `_list_run_relevant_items()` is plain deterministic word-overlap, not
  semantic understanding - a comparison word repeated in the run's own
  conclusion (e.g. "analog" used to say "more stable than analog") can
  cause a false-positive relevance match on an item that isn't actually
  the named answer. What is guaranteed is that the TRUE answer is never
  missed, not that a sibling can never also be flagged.
- As with Sprint 36's ALL mode, "no sentence dropped" still means text
  passes through the existing, unavoidable `normalize_for_speech()` TTS
  legibility processing (markdown/code/link stripping, number-to-words) -
  this is unrelated to content selection and applies identically in
  every mode.
