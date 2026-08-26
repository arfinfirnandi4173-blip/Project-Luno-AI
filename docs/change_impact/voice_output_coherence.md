# Change Impact: Voice Output Coherence (orphaned-conditional + `_has_warning` false-positive fix)

## 1. Problem

User report: after the TTS Chunk Pipelining sprint's fix (physical
audio gaps/jitter substantially improved, must not regress), long
spoken responses sometimes still sound semantically disconnected - the
first sentence is natural, but later sentences can feel like an abrupt
summary or unrelated continuation.

Phase 0 required determining, BEFORE touching any production code,
whether this is introduced before TTS (chat/voice split, compression,
chunking) or by TTS itself. Full path traced:

```
LLM response -> compute_response_policy() -> build_dual_response()
             -> voice compression (_select_by_priority) -> sentence/chunk
             splitting -> FishAudioAdapter -> pipelined playback
```

## 2. Reproduction (Phase 0 - before any production edit)

Built a real reproduction harness calling `compute_response_policy()`
and `build_dual_response()` directly (the actual production functions,
not a simulation) against representative long Indonesian responses:
cause->explanation->consequence, problem->diagnosis->solution, a
prerequisite chain, a numbered list, a conclusion depending on prior
explanation, and a safety/prohibition example - at SHORT/NORMAL/
DETAILED depth and under explicit "singkat aja"/"jelaskan semuanya"
instructions.

**Point where chat text diverges from voice text (item A):** exactly
`build_dual_response()`'s call to `_select_by_priority()`
(`luno/response_output.py`), well BEFORE `_group_sentences_into_chunk_pairs()`
(chunking) and entirely before any TTS/Fish Audio code runs. Confirmed
directly: `voice_chunks`/`voice_text` are ALWAYS derived from the
IDENTICAL `selected` sentence list (per that module's own docstring and
verified by source reading) - chunking never re-selects or reorders
anything.

**Concrete evidence (item B/C/D) - the problem->diagnosis->solution
example**, NORMAL depth, budget=5 of 7 sentences, scored (pre-fix):

| idx | sentence (truncated) | score | has_number | has_warning | has_cond |
|---|---|---|---|---|---|
| 0 | ESP32 kamu tidak bisa connect ke MQTT broker. | 105.0 (lead, must-keep) | | | |
| 1 | Masalah ini biasanya muncul karena kredensial WiFi... | 4.9 | | | |
| 2 | Selain itu, broker MQTT mungkin memerlukan autentikasi... | 4.8 | | | |
| 3 | Firewall di jaringan lokal juga bisa memblokir port 1883... | 44.7 | **True** | | |
| **4** | **Untuk mendiagnosis lebih lanjut, coba periksa serial monitor...** | **4.6** | | | |
| **5** | **Jika WiFi sudah terhubung tapi MQTT masih gagal, periksa kembali...** | **24.5** | | | **True** |
| 6 | Setelah kredensial diperbaiki, ESP32 seharusnya bisa terhubung... | 39.4 (false must-keep, see §3.2) | | **True (bug)** | |

Pre-fix selection (top-5 by score, plus lead): `{0, 1, 3, 5, 6}` -
**sentence 4 (the diagnostic prerequisite: "check the serial monitor
for WiFi errors first") was dropped, while sentence 5 (the conditional
"if WiFi is already connected...") survived** purely via its own
`_has_condition()` +20 boost. The resulting voice output jumps straight
from "the firewall might block port 1883" to "if WiFi is already
connected but MQTT still fails..." - a conditional referring to a check
the listener was never told how to perform. This is EXACTLY the
reported symptom: a conclusion/conditional retained without its
supporting explanation, an abrupt-sounding jump.

**Answering the audit's own checklist (item C):**
- Dropped prerequisite context: **YES** (sentence 4 above).
- Dropped transitional sentences: **YES** (same mechanism - any plain
  connective/explanatory sentence with no number/warning/condition
  scores near-zero).
- Retained conclusions while removing their supporting explanation:
  **YES** (the conditional in sentence 5 survives without its setup).
- Reordered sentences: **NO** - `sorted(keep)` at the end of
  `_select_by_priority()` always preserves original index order; every
  reproduction example confirmed strict order preservation.
- Excessive compression: **NOT the primary cause** - NORMAL's budget
  (5 of 7 here) is a reasonable target; the problem is WHICH 5 get
  picked, not that only 5 survive.
- Sentence selection making individually valid sentences semantically
  disconnected: **YES** - this is the core finding.

**Comparing SHORT/NORMAL/DETAILED (item D):** SHORT is the most
aggressive and, for the cause-chain example, happened to keep a
coherent (if terse) cause+solution pair by chance; DETAILED (implicit,
non-explicit-instruction case) still compresses using the SAME scoring
function and is subject to the identical orphaning risk at a larger
budget; an EXPLICIT "jelaskan semuanya"/"secara lengkap" instruction
(`ResponsePolicy.explicit and depth == "detailed"`) already skips
compression entirely (pre-existing behavior, confirmed unaffected by
this sprint) - the orphaning risk exists ONLY when compression actually
runs, i.e. NORMAL/SHORT always, DETAILED only without an explicit
"give me everything" instruction.

**Is the existing compression budget responsible (item E)?** Partially
a CONTRIBUTING factor (a tighter budget means more sentences compete for
fewer slots, so orphaning is more likely to matter) but **NOT the root
cause** - the root cause is WHICH sentences the SCORING function favors,
not how many survive. Confirmed by design: even a generously large
budget doesn't fix orphaning if the scoring function has no notion that
sentence 5 depends on sentence 4 - it would just take longer to
manifest (only when the budget is tight enough to force a choice
between them).

**Root cause classification against the audit's own A-F hypotheses:**
**B** (priority scoring selecting the wrong sentences) and **D**
(compression treating explanatory sentences as low priority) are the
primary causes, **C** (dropping prerequisite/context sentences) is the
direct symptom of B+D, plus a genuinely separate, independently
reproduced bug: a false-positive `_has_warning()` substring match
("harus" inside "seharusnya") that inflates an unrelated sentence's
priority. **E** (chunking creating discontinuity) was directly ruled
out - TTS chunking always operates on the already-fixed `selected` list.
**A** (budget too aggressive) is a contributing, not primary, factor.

## 3. Fix - minimal, additive, reuses existing detectors

Per the brief's own "prefer a minimal modification to the EXISTING
voice optimization mechanism" instruction and explicit prohibition on
simply raising the budget:

### 3.1 `_CONDITION_SETUP_BONUS` (new, bounded, +12)

A candidate sentence whose IMMEDIATE successor carries its own soft
conditional (`_has_condition()` - the SAME existing detector, not a new
classifier) receives a bounded score bonus in `_select_by_priority()`,
via a new helper `_select_scores_with_setup_bonus()`. A soft
conditional clause ("Jika WiFi sudah terhubung tapi MQTT masih gagal,
...") almost always presupposes the sentence right before it just
described the state/action/check the condition refers to. Deliberately
smaller than `_has_condition`'s own +20 (this is an INDIRECT signal
about the NEXT sentence, not the candidate's own content), one-hop only
(no lookahead across a dropped sentence, no dependency graph, no
semantic understanding), and never applied to a sentence that is itself
a conditional (would double-count two adjacent conditionals).

### 3.2 `_has_warning()` word-boundary fix

Replaced naive `any(kw in cleaned_lower for kw in _WARNING_KEYWORDS)`
with a compiled, word-boundary-safe regex (`_WARNING_RE`, built by
`_compile_word_boundary_marker_pattern()`) - reusing the EXACT technique
`luno.memory._compile_word_boundary_marker_pattern()` already
established for the identical class of bug ("lanjut" matching inside
"selanjutnya", found and fixed in the Memory Retrieval & Decision
Quality sprint). Fixes the concrete, reproduced false positive:
"seharusnya" ("should"/"supposedly" - an ordinary probabilistic
statement) no longer matches the "harus" ("must") warning keyword.
Verified every EXISTING `_WARNING_KEYWORDS` entry still matches its own
genuine, whole-word usage after the fix
(`test_genuine_warning_keywords_still_detected_after_word_boundary_fix`).

### 3.3 What was deliberately NOT changed

- Budgets (`_compute_budget_for_depth`, all three depths' floors/ratios)
  - unchanged, per the brief's own "do not blindly solve by increasing
    budget" instruction.
- The must-keep rule set (lead sentence, warning sentences, list items
  when `protect_list_items`, last-sentence-with-conclusion-cue) -
  unchanged in shape, only `_has_warning`'s own matching precision
  improved.
- Order preservation (`sorted(keep)`) - unchanged, still guarantees
  original sentence order.
- The explicit-DETAILED compression skip - unchanged.
- `chat_text` - always exactly `response_text`, unchanged (directly
  re-verified, `test_16_chat_text_always_byte_identical_to_input_at_every_depth`).
- TTS chunking, TTS chunk pipelining, `IncrementalSpeechBuffer`/
  `StreamingSpeechCoordinator`, Fish Audio, the Event Bus - none
  imported, none touched. Confirmed by a structural guard test
  (`test_17_tts_chunk_pipelining_module_not_imported_or_modified_by_this_fix`)
  and by re-running the full TTS Chunk Pipelining suite unchanged (§5).
- No new tokenizer, no LLM call, no second summarizer/classifier -
  confirmed by a structural guard
  (`test_18_no_second_classifier_llm_or_retrieval_module_imported`).

## 4. Before/after example

**Input (problem->diagnosis->solution, NORMAL depth, 7 sentences,
budget=5):**

```
ESP32 kamu tidak bisa connect ke MQTT broker. Masalah ini biasanya
muncul karena kredensial WiFi yang salah dimasukkan ke firmware. Selain
itu, broker MQTT mungkin memerlukan autentikasi username dan password
yang belum kamu konfigurasi. Firewall di jaringan lokal juga bisa
memblokir port 1883 yang digunakan MQTT secara default. Untuk
mendiagnosis lebih lanjut, coba periksa serial monitor untuk melihat
pesan error koneksi WiFi terlebih dahulu. Jika WiFi sudah terhubung
tapi MQTT masih gagal, periksa kembali username dan password broker
kamu. Setelah kredensial diperbaiki, ESP32 seharusnya bisa terhubung ke
broker tanpa masalah.
```

**BEFORE (voice_text, pre-fix):**

```
ESP32 kamu tidak bisa connect ke MQTT broker. Masalah ini biasanya
muncul karena kredensial WiFi yang salah dimasukkan ke firmware.
Firewall di jaringan lokal juga bisa memblokir port seribu delapan
ratus delapan puluh tiga yang digunakan MQTT secara default. Jika WiFi
sudah terhubung tapi MQTT masih gagal, periksa kembali username dan
password broker kamu. Setelah kredensial diperbaiki, ESP32 seharusnya
bisa terhubung ke broker tanpa masalah.
```

(the diagnostic instruction - "check the serial monitor first" - is
missing; "if WiFi is already connected" arrives with no setup)

**AFTER (voice_text, post-fix):**

```
ESP32 kamu tidak bisa connect ke MQTT broker. Masalah ini biasanya
muncul karena kredensial WiFi yang salah dimasukkan ke firmware.
Firewall di jaringan lokal juga bisa memblokir port seribu delapan
ratus delapan puluh tiga yang digunakan MQTT secara default. Untuk
mendiagnosis lebih lanjut, coba periksa serial monitor untuk melihat
pesan error koneksi WiFi terlebih dahulu. Jika WiFi sudah terhubung
tapi MQTT masih gagal, periksa kembali username dan password broker
kamu.
```

(the diagnostic step now survives immediately before its own
conditional; the response now ends on an actionable instruction instead
of an orphaned conditional - the false-positive `_has_warning()` match
on sentence 6 is also gone, so that sentence competes on its own
genuine merits rather than an accidental must-keep)

`chat_text` is byte-identical to the input in both cases (unaffected by
this sprint, verified directly).

## 5. Tests

`tests/test_voice_output_coherence.py` (new, 23 scenarios, all passing,
stable across 3 consecutive runs):

- **Phase 0 proof tests (2):** the orphaned-conditional reproduction
  (confirmed FAILING against the pre-fix code before implementation
  began) and the `_has_warning` false-positive reproduction (also
  confirmed FAILING pre-fix).
- **Sanity (1):** every existing `_WARNING_KEYWORDS` entry still matches
  its own genuine usage after the word-boundary fix.
- **The brief's 18-scenario matrix:** long explanatory response;
  cause->explanation->consequence order preservation; problem->
  diagnosis->solution no-orphaned-conditional; prerequisite chain lead/
  order preservation; list items kept together and in order; a
  conclusion that depends on prior explanation surviving via its
  existing conclusion-cue must-keep; SHORT/NORMAL/DETAILED depth
  behavior (3 tests); explicit DETAILED skipping compression entirely;
  explicit "singkat aja" still compressing; Indonesian responses; mixed
  short/long sentences; a safety/prohibition sentence surviving at
  EVERY depth (SHORT/NORMAL/DETAILED); sentence order never reordered
  (checked across 4 different examples); `chat_text` byte-identical to
  input at every depth and instruction shape (checked across 6
  scenarios); a structural guard proving no TTS/Fish Audio import; a
  structural guard proving no second classifier/LLM/retrieval import.
- **2 real E2E tests** through `RuntimeDemoConsole`/
  `PlannerBridgeModule`/`BehaviorTreeModule._speak()` - the orphaned-
  conditional fix holds end-to-end through the real production bridge,
  and a safety/prohibition example survives end-to-end with `chat_text`
  confirmed byte-identical to the canned LLM response.

Tests distinguish "shorter" from "coherent" throughout - the majority of
assertions check dependency preservation (a conditional never survives
without its own prerequisite), order preservation, and prohibition
preservation, not merely word/sentence counts.

## 6. Regression

- Targeted suite (`test_response_output.py` + `test_response_policy.py`
  + `test_voice_output_optimization.py` + `test_voice_output_coherence.py`
  + TTS pipelining/chunking/queue/cancellation/e2e + streaming/
  incremental-speech + runtime-demo + barge-in): **394 passed, 0
  failed** (371 pre-change baseline + 23 new, exact match - zero
  regressions).
- Broader memory suite: **168 passed, 0 failed**.
- Full `tests/` tree (9 batches): **1859 passed, 10 failed** - the exact
  same 10 pre-existing failures documented in
  `docs/testing/regression_baseline.md` (mic-device-index/real-adapters-
  whisper-gap/production-launcher/state-isolation sandbox artifacts),
  23 more passed than the immediately-prior sprint's 1836 baseline,
  matching this sprint's 23 new tests exactly.
- Full `luno/` tree (2 batches): **813 passed, 7 failed** - the EXACT
  same count and the SAME 7 failures (2x `test_barge_in.py` timing
  flakes, 5x `test_text_normalizer.py` `LUNO_LANGUAGE` env-leak) as the
  immediately-prior TTS Chunk Pipelining sprint's own documented
  baseline - zero coupling to this sprint's files.
- Fish Audio custom-runner suites: 14/14, 8/8, unchanged.

## 7. Persistent-state verification

All 14 present `config/*.json` files SHA256- and mtime-identical before
and after this sprint's entire implementation and full test run. No
stray `.tmp`/`.bak`/`.old`/`.orig` files.

## 8. Known limitations

- The `_CONDITION_SETUP_BONUS` is a ONE-HOP, keyword-adjacency heuristic
  - it fixes the specific, reproduced "conditional orphaned from its
    prerequisite" pattern, not general discourse coherence. A
    dependency spanning MORE than one sentence away, or a dependency not
    signaled by a soft-conditional keyword at all (e.g. a pronoun
    reference like "itu"/"ini" to an earlier, non-adjacent sentence), is
    not addressed by this fix - would require real dependency/discourse
    modeling, explicitly out of scope (no LLM judge permitted).
- List-run compression still has the SAME documented limitation as the
  prior Voice Output Optimization sprint - when `protect_list_items`
  fills the ENTIRE budget (a small list plus a small budget), a genuine
  closing/wrap-up sentence after the list can still be dropped if it
  doesn't happen to contain a `_CONCLUSION_CUES` keyword. Observed
  during this sprint's own reproduction (a 4-item list at NORMAL depth
  dropped its closing "once everything's ready, start assembling"
  sentence) but NOT fixed here - out of this sprint's narrow,
  evidence-driven scope (the user's report was specifically about
  orphaned conclusions/conditionals in prose, not list-adjacent
  closings), and fixing it risks the exact "budget tuning whack-a-mole"
  the brief explicitly warned against. Documented here as a known,
  observed, not-yet-fixed gap for a future, narrowly-scoped follow-up.
- The fix is still a deterministic, keyword/position-based heuristic,
  not semantic understanding - it improves recall for the SPECIFIC
  orphaning pattern reproduced and tested, but cannot guarantee every
  possible discourse-dependency pattern survives compression.

## 9. Files changed

- Modified: `luno/response_output.py` (`_compile_word_boundary_marker_pattern()`/
  `_WARNING_RE`/`_has_warning()` reworked to word-boundary matching;
  `_CONDITION_SETUP_BONUS`/`_select_scores_with_setup_bonus()` new;
  `_select_by_priority()`'s scoring call site updated).
- Created: `tests/test_voice_output_coherence.py`,
  `docs/change_impact/voice_output_coherence.md` (this file).
- Deleted: none.
- NOT modified: `luno/response_policy.py`, `luno/adapters/fish_audio.py`,
  `luno/adapters/fish_audio_real.py`, `luno/incremental_speech.py`,
  `luno/speech_chunk.py`, any Event Bus code.
