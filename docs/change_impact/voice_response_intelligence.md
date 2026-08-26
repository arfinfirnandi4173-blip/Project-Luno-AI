# Voice Response Intelligence (Sprint 1 - Context-Preserving Response Selection)

## 1. Problem

After the Voice Output Coherence sprint (see `docs/change_impact/voice_output_coherence.md`)
fixed the specific case of an orphaned soft-conditional clause, a
broader version of the same symptom was reported: even when every
surviving sentence individually "scores well" (carries a number, is the
lead, etc.), the SET of surviving sentences can still fail to read as a
coherent group. Two new worked examples made this concrete:

- An ESP32 WiFi auto-reconnect explanation, where "Selain itu, kamu
  bisa menambahkan watchdog timer..." ("In addition, you can add a
  watchdog timer...") could survive compression without the sentence
  it's adding ONTO.
- A Home Assistant/MQTT auto-discovery explanation, where "Akibatnya,
  Home Assistant otomatis mendeteksi entity baru dari topic yang
  dipublish itu." ("As a result, Home Assistant automatically detects a
  new entity from the topic that was published.") could survive without
  ever having told the listener what "the topic" or "the broker" was.

The common shape: a sentence opens with a causal ("Akibatnya, ...",
"karena", "as a result"), continuation ("Selain itu, ...", "Setelah
...", "Namun, ..."), or backward-reference ("Ini terjadi karena...",
"Entity ini...") discourse marker, presupposing its own immediate
predecessor - and that predecessor doesn't always score well enough on
its own merits to also survive a tight budget.

## 2. What was already existing (reused, not rebuilt)

- **Response Depth Decision** (`luno.response_policy.compute_response_policy()`)
  - explicit phrase tables, task-type/complexity scoring, three depth
  strings. Confirmed via Phase 0 audit to already fully satisfy this
  sprint's own objective 1. **Zero changes.**
- **TTS chunking/pipelining/Fish Audio playback** - `voice_chunks` is
  still always derived from the exact same already-selected sentence
  list (`_group_sentences_into_chunk_pairs()`, unmodified) - chunking
  never re-selects or reorders. **Zero changes**, per this sprint's own
  explicit "do NOT implement TTS changes" scope.
- **Memory retrieval/decision quality, cancellation/pause handling** -
  entirely untouched; `response_output.py` imports nothing beyond the
  standard library plus `luno.text_normalizer`/`luno.response_policy`.
- **`_has_condition()`** (soft-conditional whole-sentence detector) and
  **`_CONDITION_SETUP_BONUS`/`_select_scores_with_setup_bonus()`**
  (one-hop adjacency bonus mechanism) from the Voice Output Coherence
  sprint - reused and GENERALIZED, not duplicated.
- **`_compile_word_boundary_marker_pattern()`** - the same word-boundary
  regex helper `_WARNING_RE` already used, reused for the three new
  marker tables below.

## 3. Root cause / mechanism gap

`_select_scores_with_setup_bonus()` only protected a sentence that
precedes a soft CONDITIONAL. It had no concept of a sentence that
precedes a CAUSAL, CONTINUATION, or REFERENCE opener - so those openers
competed for budget purely on their own (usually near-zero, since they
rarely carry a number/warning) score, with no bonus pulling their
predecessor along, and no guarantee - even where a bonus DID apply - that
the predecessor would actually win a spot under a tight budget.

## 4. Fix

All changes are inside `luno/response_output.py`, additive, deterministic,
`re`-only (no LLM judge, no embeddings, no second tokenizer, no second
summarizer):

1. **Three new leading-window marker tables** - `_CAUSAL_KEYWORDS`
   (`"karena"`, `"akibatnya"`, `"sehingga"`, `"as a result"`,
   `"therefore"`, ...), `_CONTINUATION_KEYWORDS` (`"selanjutnya"`,
   `"selain itu"`, `"setelah"`, `"namun"`, `"however"`, `"moreover"`,
   ...), `_REFERENCE_KEYWORDS` (`"itu"`, `"ini"`, `"tersebut"`,
   `"ini terjadi"`, `"this"`, `"that"`, `"it"`, ...). Matched via
   `_has_leading_marker()` - ANCHORED to the very start of the sentence
   (`.match()`, not `.search()`) across a generous
   `_DEPENDENCY_LEADING_WINDOW_WORDS = 6`-word window (wide enough to
   hold a multi-word phrase like "oleh karena itu", but the marker must
   still open the sentence - a word merely appearing somewhere in the
   first 6 words does NOT trigger this). This anchoring is what keeps an
   ordinary sentence like "Konfigurasi ini bisa diubah kapan saja."
   (where "ini" is the 2nd word, an ordinary noun modifier) from being
   misclassified - only a sentence that genuinely STARTS with a
   backward-referencing/contrastive/causal word triggers it.
2. **`_dependency_kind(sentences, i, condition_indices)`** - classifies
   sentence `i` as `INDEPENDENT` / `SUPPORTING` / `DEPENDENT`.
   `DEPENDENT` = opens with a causal/continuation/reference marker, OR
   carries a soft conditional (`_has_condition()`, unchanged, reused).
   `SUPPORTING` = not itself dependent, but sentence `i + 1` IS - i.e.
   `i` is the predecessor a `DEPENDENT` sentence needs to remain
   understandable. Index 0 (the lead) is always `INDEPENDENT`. One-hop
   only (`i` and `i + 1`, never further).
3. **`_select_scores_with_setup_bonus()` generalized** - same function
   name, same `_CONDITION_SETUP_BONUS = 12.0` constant, same guard shape
   (never applied to a sentence that is itself dependent) - now checks
   `_is_dependent_sentence()` (any of the four categories) on the
   successor instead of only `_has_condition()`. Purely additive superset
   of triggers: every scenario the Voice Output Coherence sprint's own
   23-test suite covers keeps behaving identically (re-run unmodified,
   still 23/23 passing).
4. **`_repair_orphans()`** (new) - a deterministic POST-selection pass
   called from the tail of `_select_by_priority()`. Iterates to a fixed
   point (bounded by `total` iterations): on each pass, finds the
   highest-scored selected `DEPENDENT` sentence whose immediate
   predecessor is NOT selected, and either admits the predecessor
   (bounded by `cap = min(total, budget + 4)` - a small, deliberate slack
   so ordinary 1-2-orphan replies fully rescue their pairs, without
   turning the voice budget into an unlimited free-for-all) or, if even
   that slack is exhausted, drops the orphan permanently. Rescuing a
   predecessor can itself introduce a new one-hop-further orphan (a run
   of "Selanjutnya, langkah N..." sentences each depending on the one
   before it) - the fixed-point loop catches this on the next iteration,
   using the exact same one-hop check each time (never a dependency
   graph, never a lookahead beyond one hop).

Everything else - budgets (`_compute_budget_for_depth`), the must-keep
set (lead/warning/list-items/conclusion-cue), order preservation
(`sorted(keep)`), the explicit-DETAILED compression skip, `chat_text`
always byte-identical to the input - is completely unchanged.

## 5. Before/after example

Text (Home Assistant/MQTT auto-discovery, SHORT depth):

```
Home Assistant bisa menampilkan status semua device ESP32 dalam satu dashboard.
Kamu perlu menginstal integrasi MQTT terlebih dahulu di Home Assistant.
Integrasi ini butuh broker MQTT yang sudah berjalan di jaringan yang sama.
Setelah broker terpasang, ESP32 bisa publish data sensor ke topic tertentu.
Akibatnya, Home Assistant otomatis mendeteksi entity baru dari topic yang dipublish itu.
Entity ini kemudian bisa ditambahkan ke dashboard secara manual atau otomatis.
Namun, kamu perlu memastikan format payload MQTT sesuai standar auto-discovery Home Assistant.
Kalau formatnya salah, entity tidak akan muncul di dashboard sama sekali.
```

Before this sprint (illustrative - the "Akibatnya" causal opener could
win a budget slot purely via `_score_sentence`'s lead/position/number
scoring without its antecedent ever surviving): a bare "Akibatnya, Home
Assistant otomatis mendeteksi entity baru dari topic yang dipublish
itu." heard with no prior mention of "broker" or "topic" at all.

After this sprint:

```
Home Assistant bisa menampilkan status semua device ESP32 dalam satu dashboard.
Integrasi ini butuh broker MQTT yang sudah berjalan di jaringan yang sama.
Namun, kamu perlu memastikan format payload MQTT sesuai standar auto-discovery Home Assistant.
Kalau formatnya salah, entity tidak akan muncul di dashboard sama sekali.
```

"Broker" is introduced before anything references it; the causal
"Akibatnya, ..." sentence itself didn't make the cut under SHORT's tight
budget this time, but nothing SURVIVING depends on something absent -
exactly the invariant this sprint enforces (compression amount is
unaffected by design; WHICH sentences survive together is what changed).

## 6. Why this preserves coherence, not just brevity

`_repair_orphans()` operates strictly on the ALREADY-selected candidate
set - it never invents wording, never reorders sentences (`sorted(keep)`
at the very end, same as before), and never trades a warning/prohibition
sentence away (the must-keep set is computed first and is untouched by
this pass). The dependency-marker tables are a heuristic, not semantic
understanding - documented false-negative and false-positive risk below
- but every trigger is anchored (must open the sentence) and reuses the
exact same word-boundary regex technique already validated for
`_has_warning()`.

## 7. Tests

`tests/test_voice_response_intelligence.py` - 31 tests: 2 response-depth
reuse sanity checks, 6 `_dependency_kind()`/leading-marker unit tests, 7
context-preserving integration tests through `build_dual_response()`
across SHORT/NORMAL/DETAILED on the two new ESP32/WiFi/MQTT worked
examples, 4 voice-budget-as-information-budget tests (including a
13-sentence pathological many-dependent-openers case proving the repair
cap actually bounds growth rather than defeating compression), 3
short-response-quality tests, 6 adversarial false-positive/false-negative
guards (`"keberlanjutan"`/`"kelanjutan"` not matching `"selanjutnya"`,
ordinary noun-modifier `"ini"` not matching leading-reference,
`"menyebabkan"` not matching bare `"sebab"` - the exact word-boundary
substring-collision class the Voice Output Coherence sprint fixed for
`_has_warning()`), 2 structural no-second-classifier/no-persistent-state
guards, and 2 real E2E tests through `RuntimeDemoConsole`/
`PlannerBridgeModule`. Does not duplicate
`tests/test_voice_output_coherence.py`'s own 23 scenarios - that suite
is reused as-is for regression (still 23/23 passing, unmodified).

## 8. Regression

Targeted 16-file suite: **419 passed, 0 failed** (388 pre-change + 31
new). Full `tests/` tree (73 files, batched): only the same 4
already-documented pre-existing failure groups reproduce (6x
`test_mic_device_index.py`, 1x `test_production_launcher.py::test_07_...`,
2x `test_real_adapters.py`, 1x `test_state_isolation.py::test_isolate_persistent_state_drains_stragglers_before_monkeypatch_reverts`)
plus 2 sandbox-environment-only collection errors unrelated to this
module. Full `luno/` tree (37 files, batched): all pass. Fish Audio
custom-runner suites: 14/14, 8/8, unchanged. See
`docs/testing/regression_baseline.md`'s own dated section for the full
breakdown, including the 4 pre-existing test-harness bugs found and
fixed during Phase 0 baseline capture (unrelated `language=` pinning
issue, see that section for detail).

## 9. Persistent-state verification

All 14 `config/*.json` files hashed (SHA256 + mtime) before and after.
The 4 files real E2E-pipeline test runs always touch as an expected side
effect (`habit_memory.json`, `long_term_memory.json`,
`relationship_state.json`, `verified_facts.json`) changed, exactly as
every prior sprint's own E2E tests also do; the other 10 - including
`response_depth_preference.json` - were byte-identical. No stray
`.tmp`/`.bak`/`.old`/`.orig` files.

## 10. Known limitations

- **Heuristic, not semantic.** The four marker categories are a
  deterministic, keyword/position-based approximation of "this sentence
  needs its predecessor" - like every mechanism in this module, it can
  both over-trigger (an ordinary self-contained sentence that happens to
  open with "Ini ...") and under-trigger (a genuinely dependent sentence
  using phrasing not in any of the four tables). Documented as an
  accepted trade-off, consistent with this module's existing
  `_has_condition()`/`_has_warning()` design.
- **One-hop only, by design.** `_repair_orphans()`'s fixed-point loop
  resolves chains of dependent sentences (verified by the 13-sentence
  pathological test), but it still only ever reasons about a sentence
  and its immediate predecessor - it will not reach back two DISTINCT
  antecedents referenced by two different pronouns in the same sentence,
  for example. No dependency graph is built, by explicit design
  constraint.
- **`cap = budget + 4` is a fixed constant, not adaptive.** A reply with
  many independent chains of dependent openers could still see some
  orphans permanently dropped once the cap is exhausted, rather than
  every chain being fully rescued. This is an intentional bound (voice
  budget as an information budget, not an unlimited one) - see
  `ARCHITECTURE_GUARD.md` §27 for the explicit "don't just raise this"
  guidance for future reports.
- **List-item budget crowding** (documented in the Voice Output
  Coherence sprint's own change-impact doc) remains open and unrelated -
  this sprint did not touch `protect_list_items`.
