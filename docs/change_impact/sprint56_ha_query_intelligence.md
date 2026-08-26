# Sprint 56 — Home Assistant + Query Intelligence

**Status:** COMPLETE for the phases with a safe, verifiable implementation
(Phase 12); COMPLETE as re-verification for Phases 9-11/15/17/18; Phase 13
completed as an evidence matrix with an explicit, reasoned DEFER decision
(no unsafe or half-built contextual-reference system shipped); Phase 10/14
completed at the depth this sandbox and this checkout's real 6-device
registry allow.

## Phase 9 — Sprint 52 takeover verification

No `docs/change_impact/sprint52_ha_entity_resolution.md` file exists in
this checkout (referenced by `ARCHITECTURE_GUARD.md` §53 and
`docs/project_handover.md` but apparently never delivered/retained) — this
is a pre-existing documentation gap, not something this sprint caused.
Per this project's own "source of truth is the code" rule, verification
was done directly against `ARCHITECTURE_GUARD.md` §53's detailed writeup,
`luno/tool_manager/builtin/real_home_assistant.py`, and
`tests/test_sprint52_ha_entity_resolution.py`.

**Confirmed matching the documentation exactly:** `_resolve_entity_tiered()`
wraps the unchanged `_resolve_entity_id()` (tiers 1-3: exact/alias/literal
entity_id), adds a bounded fuzzy tier 4 (`_score_candidates()`, stdlib
`difflib.SequenceMatcher` only, scored per distinct entity_id), gated by
`fuzzy_min_confidence`/`fuzzy_min_margin` (default 0.78/0.15). Two or more
distinct devices within the margin of the top score → tier 5, ambiguous,
refused. Re-ran `tests/test_sprint52_ha_entity_resolution.py` (29 tests) +
`luno/tool_manager/tests/test_real_home_assistant_verification.py` (39
tests, pre-existing, unmodified) — **68 passed, 0 failed**, exactly
matching the documented count.

**Real configured devices** (read live from `config/lights.config.json`,
`switches.config.json`, `scripts.config.json` — confirmed unchanged from
Sprint 52's own fixture): Main Lamp, RGB Strip, RGB Computer (lights);
Baterai, Aquascape (switches); gaming mode (script). Exactly 6 devices,
matching Sprint 52's documented registry.

## Phase 10 — Real HA command matrix

Sprint 52's own test file already implements categories A-V (exceeding
this sprint's own "A through L" minimum): exact match, case variation,
spacing variation, missing-character typo, transposition typo,
missing-trailing-character typo, alias exact, alias typo/fuzzy, script
alias exact (the bugfix target), script typo/fuzzy, switch exact, switch
typo/fuzzy (two shapes), two more Aquascape-specific typo shapes,
partial-name-resolves-unambiguously, alias/name dedup to the same entity,
too-little-information refusal, unknown-device no-crash, a direct
ambiguity-gate unit test, a full `execute()` end-to-end fuzzy-target
case, and a full exact-target regression battery — plus observability and
performance coverage. Re-run this sprint, all still passing (see Phase 9).

**New this sprint — Category L, "typo closer to a WRONG device"**, the
sprint's own critical-safety category, closed with a GENUINELY reproduced
natural-language case rather than only hand-crafted scores (Sprint 52's
own `test_T` used hand-crafted scores to exercise the gate directly). See
`tests/test_sprint56_ha_safety_matrix.py`:

- A `difflib` sweep across dozens of realistic corruptions of this
  checkout's two most textually similar real devices ("RGB Strip" / "RGB
  Computer") found NO natural typo that mis-ranks the wrong device as top
  scorer — every realistic corruption still ranks the intended device
  highest (`test_L_natural_typo_sweep_never_misranks_the_wrong_device`).
- A deliberately ADVERSARIAL corruption ("rgb cprip") was needed to
  produce a genuine near-tie (RGB Strip 0.67 vs RGB Computer 0.57 — within
  the 0.15 margin bar). Run live, end-to-end, through `execute()`: the
  resolver refuses (`success=False`), and **zero `call_service()` calls
  are made to either device** — not merely "the right one was picked",
  proof that neither device is ever touched when the evidence is
  genuinely insufficient (`test_L_adversarial_near_tie_refuses_never_misactivates`).

## Phase 11 — HA safety invariants (all 12 re-verified)

| # | Invariant | Verified how |
|---|-----------|---------------|
| 1 | exact > alias > fuzzy priority | `test_exact_alias_fuzzy_priority_order_is_still_respected` + unmodified Sprint 52 code path (fuzzy tier only reached when tiers 1-3 return nothing) |
| 2 | ambiguous/low-confidence MUST refuse | Sprint 52's `test_T` + this sprint's `test_ambiguity_gate_still_never_auto_resolves_two_distinct_contenders` + the new live Category L reproduction |
| 3 | a typo must NEVER activate a different device merely for highest similarity | This sprint's Category L natural sweep + adversarial near-tie, both live-executed, zero wrong-device calls |
| 4 | no domain-specific hardcoding (`if device == "..."`) | `test_no_domain_specific_hardcoding_in_resolver` — AST-level check for literal device-name comparisons in `_resolve_entity_tiered()`'s own logic (not mere docstring mentions) |
| 5 | no LLM call for simple resolution | `test_no_llm_or_network_call_in_the_resolution_path` — module import inspection, zero LLM/HTTP/network imports in `real_home_assistant.py` |
| 6 | no embeddings | Re-confirmed via Sprint 52's own `test_score_candidates_uses_only_stdlib_difflib`, still passing |
| 7 | no second parallel resolver | This sprint added ZERO code to `luno/tool_manager/`; the query-side differentiator (Phase 12) lives entirely in `luno/memory_context.py`, a different subsystem (conversational topic/context, not tool execution) — confirmed by file-level diff |
| 8 | Event Bus reused, not replaced | `EntityResolutionDecision`/`on_verification_event` unmodified, still passing observability tests |
| 9 | bounded, in-process, no network in the resolution path | Confirmed by Phase 11 item 5 above and Phase 17's own measurement |
| 10 | `MockHomeAssistantHandler` untouched | Unmodified this sprint; Sprint 52's own `test_mock_handler_untouched_by_sprint_52` still passing |
| 11 | no weakening of the confidence/margin thresholds | `config/*.json`/env defaults unchanged; `fuzzy_min_confidence`/`fuzzy_min_margin` values unmodified |
| 12 | script alias resolution bugfix (`_lookup_script()` aliases) stays fixed | Sprint 52's `test_I_script_alias_bugfix_exact` still passing |

## Phase 12 — Query-side entity differentiator (the main new code this sprint)

**Real gap found and closed**, live-reproduced against this checkout's own
tokenizer/topic-history machinery (not hypothetical): Sprint 49's
`_extract_entity_differentiator()` (a general-purpose, domain-agnostic
"standalone uppercase letter" structural signal read from a turn's raw
`source_sentence`) was used to fix the single-slot `is_active_topic_
relevant_to_query()` lineage check, but NOT `select_topic_candidates()`
(a separate call site — the bounded, multi-entry, token-overlap-ranked
topic-history matcher). Two history entries that both mention the same
generic noun ("... A pakai pompa kecil." / "... B pakai pompa besar.")
tie on raw token overlap and are BOTH returned — even when the CURRENT
QUERY itself explicitly names one via the identical "A"/"B" convention
("Pompa A gimana?"). Reproduced live:

```
Pompa gimana?    -> ['... A pakai pompa kecil.', '... B pakai pompa besar.']   (bare - both, unchanged)
Pompa A gimana?  -> ['... A pakai pompa kecil.']                                (was: both, now: A only)
Pompa B gimana?  -> ['... B pakai pompa besar.']                                (was: both, now: B only)
```

**The fix:** `luno/memory_context.py`, one new function,
`_narrow_by_query_differentiator(candidates, query_text)` (additive,
~45 lines including its docstring), wired into `select_topic_candidates()`'s
existing return statement (one line changed: `return matched[:limit]` →
`return _narrow_by_query_differentiator(matched[:limit], text)`). Reuses
`_extract_entity_differentiator()` directly — no second regex, no new
vocabulary, no new state. Narrows a tied result to exactly one entry ONLY
when: 2+ candidates are about to be returned, the query's own raw text
carries an unambiguous (exactly-one-candidate) differentiator label, and
EXACTLY ONE of the tied candidates' own `source_sentence` carries that
same label. Every other shape — bare query, no matching entry, an
ambiguous query differentiator (two letters, or a lowercase letter), a
single already-unambiguous candidate — is completely unchanged, preserving
this project's "insufficient evidence → do not narrow/guess" discipline
and the bare-query "existing ambiguity policy" (return all tied
candidates) the sprint brief explicitly asked to preserve.

**Generalization proven, not asserted:** the identical mechanism was
exercised across TWO unrelated synthetic vocabularies with zero shared
words (an aquarium-pump scenario and an unrelated "Server A/Server B"
scenario) and produces the identical narrowing behavior in both — and an
AST-level test (`test_16`/`test_17` in the new test file) proves the
production function contains no forbidden domain literal
(aquascape/esp32/pompa/mic/board/lampu/wled) and calls the existing
extractor rather than re-implementing a second pattern.

**Tests:** `tests/test_sprint56_query_entity_differentiator.py` (new, 17
tests) — unit tests for the new function in isolation (8), end-to-end
tests through the real `select_topic_candidates()` caller including a
second synthetic domain (7), and 2 no-hardcoding/no-duplicate-mechanism
structural checks. **17 passed, 0 failed.**

**Regression:** the full memory/topic/entity-continuity test surface (16
pre-existing files, 662 tests) plus the Sprint 52 HA suite (68 tests) —
**747 passed, 3 skipped (pre-existing, environment-specific), 0 failed**
— run together with the new file. A full 3880-test repository sweep was
additionally re-run after this change (see "Full regression" below);
zero new failures attributable to this change.

## Phase 13 — Contextual HA references (evidence matrix, DEFERRED)

Investigated whether "Terangin lagi."/"Matikan." after "Nyalain lampu
kamar." can be safely resolved today. Live-reproduced, not assumed:

| Input | `IntentParser.parse()` result | `execute()` result |
|-------|-------------------------------|---------------------|
| `"Nyalain lampu kamar."` | `('home_assistant', 'turn_on', 'lampu_kamar')` | resolves normally |
| `"Matikan."` | `('home_assistant', 'turn_off', None)` | **safe**: fails with "None is currently unavailable." (message quality is poor, but no device is ever touched) |
| `"Matikan itu."` | `('home_assistant', 'turn_off', 'itu')` | **safe**: "I couldn't find 'itu'..." refusal, no device touched |
| `"Terangin lagi."` | `('unknown', 'unknown', None)` | never reaches Home Assistant at all — a parser vocabulary gap, unrelated to contextual reference resolution |

**Finding: the current behavior is already SAFE (never activates the
wrong device, never silently guesses) but UNHELPFUL** (a real ellipsis
reference fails outright instead of resolving to the device the user
obviously means).

**Why this sprint does NOT implement contextual resolution:** there is no
existing, safe hook to build it on. `luno.memory_context`'s active-topic/
topic-history machinery (used for Phase 12 above) is scoped to the LLM
PROMPT-CONTEXT-INJECTION pipeline — a fuzzy, best-effort mechanism
designed to help the LLM answer conversational questions, explicitly not
wired to `luno.tool_manager`'s deterministic, safety-critical device
execution path (confirmed both by Sprint 52's own documented finding and
by this sprint's own re-inspection: `Planner._apply_context_shortcuts()`
is an exact-slugified-string no-op shortcut, not an entity resolver).
Building genuine contextual HA resolution would require either (a)
bridging the fuzzy conversational-memory system into the deterministic
tool-execution path — a real architectural coupling between two systems
deliberately kept separate — or (b) a brand-new, narrowly-scoped "last HA
target" tracker inside the Tool Manager itself, which needs its own
careful safety design (staleness/turn-boundary decay, behavior when
multiple devices were mentioned recently, interaction with the existing
ambiguity gate, its own observability). Either path is exactly the kind
of "second state system" risk this sprint's own brief explicitly warns
against building without a full design pass. Per the brief's own explicit
permission ("defer to Sprint 57 if a second state system would be
needed"), **this is deferred to Sprint 57** with this evidence matrix
as its starting point. No unsafe or half-built mechanism was shipped.

**One narrow, SAFE observation left for Sprint 57, not acted on this
sprint** (to stay strictly in scope — the ask was contextual resolution,
not message-quality polish): the `target=None` case's "None is currently
unavailable" message is confusing (it should read more like the existing
"which device did you mean?" refusal `_unknown_device_result()` already
produces for a present-but-unresolvable target). This is a message-
quality issue only — no safety impact, no device is ever touched in
either case.

## Phase 14 — Real-world HA dataset (capture/replay infrastructure)

Reused Sprint 50's `luno.test_capture`/`luno.replay` infrastructure
exactly as already verified working end-to-end in Sprint 55's own Phase
5 (candidate → reviewed → approved → replay → verdict, against a scratch
directory, never touching `tests/real_world/`). No HA-specific extension
to this infrastructure was needed — the same conversation-turn-level
capture already records whatever HA-flavored turns occur in it; a
dedicated HA capture session was not run this sprint (no live HA server
reachable in this sandbox — see Sprint 55's Phase 2 for the concrete
network-egress proof), but the mechanism itself needs no HA-specific
change to support it.

## Phase 15 — HA observability

Reuses Sprint 50's Event Bus/`on_verification_event` hook and Sprint 52's
own `"resolution"` stage / `EntityResolutionDecision` event — fired only
for `"fuzzy"`/`"ambiguous"` outcomes, silent for exact/alias/literal/
unknown (matching this module's pre-existing "no event for nothing new to
report" convention). Re-verified still passing this sprint: `test_
observability_fuzzy_resolution_emits_resolution_event`, `test_
observability_exact_match_emits_no_resolution_event`, `test_
observability_ambiguous_case_emits_resolution_event`. No new logging
framework, no secrets/unlimited payloads — unmodified from Sprint 52.

## Phase 16 — Testing (consolidated)

- New this sprint: `tests/test_sprint56_query_entity_differentiator.py`
  (17 tests), `tests/test_sprint56_ha_safety_matrix.py` (6 tests). **23
  passed, 0 failed.**
- Sprint 52's own suite + its downstream regression file: 68 passed.
- The full pre-existing memory/topic/entity-continuity surface (16
  files): 662 passed, 3 skipped.
- **Combined, all of the above together: 753 passed, 3 skipped, 0
  failed.**
- **Full repository sweep, re-run after this sprint's changes** (same
  fixed 3880-test collection Sprint 55 established): **3865 passed, 11
  failed, 4 skipped.** All 11 failures re-run in isolation and
  classified: 10 are byte-for-byte the same failures Sprint 55 already
  root-caused and documented (environment-specific/deferred/confirmed
  flakes); the 11th is one additional non-deterministic reproduction of
  the pre-existing, pre-Sprint-49-documented `test_streaming_e2e.py::
  test_D_barge_in_between_llm_and_tts_chunk_never_plays` timing flake
  (failed once in the parallel chunk run, then passed 4/4 in immediate
  isolated reruns — textbook non-determinism, not a regression; this
  project's own `project_handover.json` already lists it as a known,
  sometimes-non-reproducing flake). **Zero genuine new regressions.**

## Phase 17 — Performance

| Path | Result |
|------|--------|
| `RealHomeAssistantHandler._resolve_entity_tiered()`, fuzzy tier (worst case) | 0.175 ms/call (500 calls) |
| `select_topic_candidates()` including the new differentiator narrowing | 0.007 ms/call (2000 calls) |

Both far under the 5ms target. No network call in either path (confirmed
structurally in Phase 11 item 5/9).

## Phase 18 — Persistent state

All 15 `config/*.json` files verified byte-identical (SHA256) before and
after every test run and probe performed during Sprint 56 — this sprint
touched no config file (only `luno/memory_context.py`, two new test
files, and documentation).

## Files changed this sprint

**Modified (production code):** `luno/memory_context.py` — one new
function (`_narrow_by_query_differentiator`), one line changed at
`select_topic_candidates()`'s return statement. No other production file
under `luno/` or `main_runtime_demo.py` touched. `luno/tool_manager/
builtin/real_home_assistant.py` (Sprint 52's resolver) was **not**
modified this sprint — re-verified only.

**New:** `tests/test_sprint56_query_entity_differentiator.py`,
`tests/test_sprint56_ha_safety_matrix.py`, this document.

**Documentation-only:** `docs/project_handover.md`/`.json`,
`ARCHITECTURE_GUARD.md` §57, `docs/testing/regression_baseline.md`.
