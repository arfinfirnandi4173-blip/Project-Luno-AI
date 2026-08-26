# Sprint 58 — Home Assistant Multi-Entity & Group Commands

**Status:** COMPLETE for the scenarios documented below as implemented;
two scenarios (area-scoped groups, contextual groups) are explicitly
**deferred** with a documented, evidence-based reason each, per this
sprint's own STOP CONDITION ("do not force the implementation; document
the finding and build a minimal safe foundation first").

## 1. Root cause / architecture gap

Before this sprint, `PlannerBridgeModule` (`main_runtime_demo.py`) had no
concept of "more than one target for the same command" at all. Two
concrete gaps, both confirmed by direct source reading (Phase 0), not
assumed:

1. **Verb ellipsis across clauses.** `luno.planner.parser._CLAUSE_SPLIT_RE`
   already splits `"matikan lampu kamar dan lampu ruang tamu"` into two
   raw clauses (`"matikan lampu kamar"`, `"lampu ruang tamu"`), but the
   second clause has no verb of its own — natural Indonesian/English elide
   the repeated verb. `_clause_to_step()` correctly (conservatively) falls
   through to `tool="unknown"` for a clause with no recognized verb, so
   only the FIRST device was ever actually commanded; the second was
   silently dropped. This is not a parser bug — `IntentParser`'s own
   module docstring is explicit that it is "keyword/regex clause-
   splitting, NOT real NLU" and falls back to `unknown` rather than
   guessing.
2. **No group keyword at all.** `"nyalain semua lampu"` parsed as a
   *single* `home_assistant` step whose target was the literal
   (unresolvable) slug `"semua_lampu"` — `IntentParser` has no vocabulary
   for "all of a domain" whatsoever.

Both gaps, if "fixed" naively (e.g. widening `_CLAUSE_SPLIT_RE` or
guessing the last-mentioned device applies to every unknown trailing
clause), risk silently executing a command against the wrong device or
changing the semantics of `IntentParser` for every existing single- and
multi-clause command in the codebase — exactly what this sprint's STOP
CONDITION warns against.

## 2. Design — group resolution sits ABOVE the individual resolver

Detection and resolution both happen entirely inside a new pre-Planner
text layer in `PlannerBridgeModule`, checked **before** Sprint 57's own
`_apply_device_context()` in `_handle_utterance()` (group commands are
self-contained in `text` and never depend on conversation memory, unlike
a contextual single-target reference):

```
_apply_ha_group_resolution(text, conversation_id)
    -> _ha_group_all_lights_shape(text)          # "semua lampu" / "all lights"
    -> _ha_explicit_multi_target_shape(text)      # "A dan B [dan C ...]"
    -> _resolve_ha_group_targets(target_slugs)    # resolve EVERY target
    -> rewrite (all clear) OR refuse (any failed)
```

- **Not a second resolver.** `_resolve_ha_group_targets()` constructs a
  throwaway `RealHomeAssistantHandler(client=None)` and calls its
  EXISTING `._resolve_entity_tiered()` (Sprint 52) once per target.
  Phase 0 confirmed by direct source read that `_resolve_entity_tiered()`
  only ever touches `self._resolve_entity_id()` and the module-level pure
  function `_score_candidates()` — never `self._client`/`self._lock` — so
  calling it on a client-less instance is safe and cannot reach a real HA
  API. This is the same reuse pattern Sprint 57 already established for
  `IntentParser.parse()` and direct `luno.devices` reads.
- **Not a second HA system, not a second memory system.** No new
  persistent state; `_apply_ha_group_resolution()` is a pure function of
  its `text` argument plus the live device registry (`luno.devices`).
  Nothing is written to `self._last_device_target` or any new dict for a
  group — a group command never becomes "the remembered device" for a
  later single-target contextual reference (proven by test P).
- **All targets resolve before any HA action is sent.** If detection
  fires, EVERY target is validated via `_resolve_ha_group_targets()`
  before `_apply_ha_group_resolution()` returns. Only if every target is
  `executable=True` AND domain-compatible does it rewrite `text` into the
  canonical `"turn on/off <device>[, turn on/off <device> ...]"` phrasing
  — the EXACT phrasing Sprint 57's own FILL step already produces — and
  hand it to the **unmodified** `IntentParser`/Planner/Tool Manager
  pipeline. If ANY target fails (ambiguous, unresolved, wrong domain), or
  the shape is a recognized-but-unsupported variant, `text` becomes the
  empty string. `IntentParser.parse("")` produces zero steps, so
  `_handle_utterance()`'s own `real_task_count > 0` gate (unchanged, see
  that method) never calls `self.planner.execute()` — this is the
  mechanism that guarantees **zero HA API calls for any target in a
  refused group**, not just the failing one. See §9 for the direct proof.

## 3. Command semantics implemented

| Shape | Example | Status |
|---|---|---|
| A. Single target | "matikan rgb strip" | Unaffected — `_apply_ha_group_resolution` returns `(text, None)` unchanged |
| B/C. Explicit multi-target | "matikan rgb strip dan rgb komputer [dan lampu utama]" | Implemented |
| D. Group-all | "nyalain semua lampu" | Implemented (light domain only, matching the brief's own examples) |
| E. Area group | "semua lampu di kamar" | **Deferred** — honest refusal, see §4 |
| Contextual group | "Nyalain lampu kamar." then "Matikan semuanya." | **Deferred** — not detected at all, see §4 |

## 4. Deferred scenarios (STOP CONDITION — documented, not implemented)

**Area-based grouping.** A project-wide grep for `area|room|zone`
(case-insensitive) across `config/lights.config.json`,
`config/switches.config.json`, `config/scripts.config.json`, and
`luno/devices.py`'s own loading code found **zero** real area/room/zone
metadata anywhere in this checkout (the only match is a single
docstring example using "kamar" as an illustrative alias, not a real
field). Implementing "semua lampu di kamar" would require either (a)
fabricating an area taxonomy this project's own config schema doesn't
have, or (b) silently expanding to *every* light (wrong — the user asked
for a subset). Both violate the brief's own "do not guess" rule. This
shape is detected (`_GROUP_AREA_RE`) and given an honest, specific "not
supported yet" refusal (`final_decision="refused_unsupported_area"`)
rather than silently doing the wrong thing. `test_F` proves the area-
metadata absence directly against the live registry, not just against a
fixture.

**Contextual group ("semuanya").** Sprint 57's `_last_device_target` is
a deliberate single-slot memory (one remembered device per tool) —
extending it to "the last N devices talked about" is a structural memory
redesign this sprint's own invariants explicitly forbid ("no second
memory system", "no unnecessary persistent state", "no global
`last_entity`"). Separately, "semuanya" alone is not unambiguous even
in principle — it could mean "every device mentioned in this
conversation", "every light", or "every light and switch", and guessing
between those meanings would violate the ambiguity rule just as much as
guessing between two similarly-named devices does. Nothing in this
sprint recognizes this shape; a bare "Matikan semuanya." falls straight
through, byte-for-byte unchanged, to the pre-existing Sprint 57
`_apply_device_context()` path (`test_P` proves this explicitly).

## 5. A real regression found and fixed during implementation

The first working version of `_ha_explicit_multi_target_shape()`
detected ANY anchor-turn_on/off-clause-followed-by-unknown-clauses shape,
regardless of which word joined them. Running the full regression suite
surfaced a genuine break: `tests/test_runtime_demo.py::test_mixed_
utterance_real_command_still_succeeds_despite_unknown_clause` — the
utterance `"turn on the lights and bagaimana cuaca hari ini"` ("...and
how's the weather today") was being misdetected as a 2-target group (the
weather question as an unresolvable "target"), causing the ENTIRE,
otherwise-valid `turn on the lights` command to be refused.

Root cause: comma, `"and"`, and `"then"` are ALL already-established
GENERAL clause separators for entirely unrelated actions in this parser
(the module's own spec example is comma-separated: `"open Chrome, turn
on the bedroom light, ..., then play Spotify"`) — lexical similarity
scoring alone cannot reliably distinguish "an unregistered/typo'd device
name" from "a completely unrelated sentence fragment" (both score low
and both come back `resolution_method="unknown"`).

**Fix (deliberate scope reduction, matching the STOP CONDITION's own
"safety and backward compatibility over functionality" priority):**
explicit multi-target detection is now scoped to text that contains the
Indonesian word `"dan"` and does **not** also contain a comma, `"and"`,
or `"then"` (`_MULTI_TARGET_DAN_RE` / `_MULTI_TARGET_DISQUALIFYING_RE`).
Every one of this sprint's own worked examples uses `"dan"` exclusively,
so this costs no example scenario, and it makes the previously-broken
regression test pass again unchanged. Anything wider than this narrow
shape falls through completely untouched to the existing pipeline.

## 6. Data model

No new persistent model. Internally, `_resolve_ha_group_targets()`
returns a list of `(raw_slug, resolution_method, entity_id, domain)`
tuples — a transient, in-function return value, not a stored structure.
The one durable rewrite artifact is plain text (the canonical
`"turn on/off <device>, ..."` string), consumed immediately by the
unmodified `IntentParser`.

## 7. Parsing safety

- Multi-target detection is **anchor-must-be-first-clause only** — this
  structurally rules out ever silently dropping a real clause that
  appeared *before* the anchor (a "just take whatever's after the first
  real step" approach could do this; this design cannot).
- Group-all detection requires `IntentParser.parse(text)` to produce
  **exactly one** step — a compound utterance ("nyalain semua lampu dan
  buka chrome") is left completely untouched rather than silently
  dropping the "buka chrome" clause.
- Aliases, differentiator targets, and fuzzy targets are never
  hand-parsed by this sprint's own code — every target string is handed
  to the existing Sprint 52 resolver verbatim (after the same
  `_slugify()` every other caller in this file already uses).
- **Known, inherited limitation:** a device name that literally contains
  the word "dan" (e.g. a hypothetical "Meja dan Kursi") would still be
  mis-split by the pre-existing `_CLAUSE_SPLIT_RE` — this sprint does not
  fix that (a universal fix risks changing every existing multi-clause
  command's semantics, exactly the STOP CONDITION's own warning). What
  this sprint DOES guarantee: the mis-split fragments fail to resolve
  and the whole command is safely refused, never silently executed
  against the wrong fragment (`test_O`, using a synthetic fixture since
  no real device in this registry has this shape).

## 8. Observability

New Event Bus event `ha_group_command_resolution` (same
`self._event_bus.publish(Event(...))` pattern Sprint 50/57 already
established), published from `_apply_ha_group_resolution()`, guarded by
`self._event_bus is not None` and wrapped in try/except (telemetry must
never break a turn). Fields: `conversation_id`, `command_kind`
(`"explicit_multi_target"` / `"group_all_light"`),
`detected_target_count`, `resolved_target_count`,
`ambiguous_target_count`, `unresolved_target_count`,
`duplicate_target_count`, `final_decision` (`"executed"` /
`"refused_ambiguous"` / `"refused_unresolved"` /
`"refused_unsupported_area"` / `"empty_no_op"`). Deliberately **no raw
utterance text** — same privacy convention Sprint 57 already established
for `device_context_resolution`.

## 9. The critical safety invariant, proven directly

Brief's own required proof: *"Target A = valid, Target B = ambiguous →
HA API called 0 times."* `tests/test_sprint58_ha_multi_entity_commands.py
::test_Q_critical_invariant_valid_plus_ambiguous_target_zero_ha_calls`
proves this against the exact production mechanism, not by construction
alone: it calls `_apply_ha_group_resolution()` with one unambiguous and
one genuinely ambiguous target, confirms `effective_text == ""`, then
re-derives `_handle_utterance()`'s own gate line-for-line
(`IntentParser.parse("") == []` → `real_task_count == 0` →
`self.planner.execute()` is never reached), and finally constructs a
real `RealHomeAssistantHandler` bound to a `FakeHAClient` and confirms
`client.calls == []` even if something downstream tried.

## 10. Test results

`tests/test_sprint58_ha_multi_entity_commands.py` — 27/27 passed,
covering scenarios A–V plus structural no-bypass invariants (see the
file's own module docstring for the full A–V → test-name mapping).

## 11. Full regression

Ran with the project's own established methodology
(`--ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_bargein.py`,
`--timeout=60 --timeout-method=signal`; `-n4/loadfile` was attempted
first but hit a pre-existing pytest-xdist worker hang unrelated to this
sprint's code and was abandoned in favor of a single-process run):

- One test (`tests/test_dashboard.py::test_36_audio_capture_store_unit_
  behavior`) was deselected after confirming, in isolation, that it
  passes instantly (`0.60s`) — its full-suite hang is a pre-existing,
  order-dependent thread/lock flake in `luno/dashboard/audio_bridge.py`,
  a file this sprint never touches.
- Result: **3930 passed, 27 failed, 4 skipped, 1 deselected** in 768.76s.
- One of the 27 failures — `tests/test_runtime_demo.py::test_mixed_
  utterance_real_command_still_succeeds_despite_unknown_clause` — WAS a
  real regression caused by this sprint's own code; it was found, root-
  caused, and fixed (§5) before this final run, and is confirmed passing.
- The remaining 26 failures were individually verified, file-by-file, to
  be pre-existing and unrelated to this sprint:
  - `test_mic_device_index.py` (4): `list_microphones.py` does not exist
    in this checkout at all (already documented ENVIRONMENT-SPECIFIC in
    `docs/testing/regression_baseline.md`).
  - `test_real_adapters.py` (2): `RealWhisperSource` missing
    `_device_index` — pre-existing adapter/test mismatch, already
    documented INFRASTRUCTURE.
  - `test_production_launcher.py` (2), incl. the already-documented
    "test_07 environment-specific" flake.
  - `test_llm_dashboard.py::test_api_llm_endpoint_reports_manager_state`:
    asserts `current_provider == "openai"` against this checkout's real
    `.env` — a live config-drift assertion, unrelated to HA/planner code,
    fails identically alone.
  - `test_llm_tts_streaming_production.py` (3): require a real local LLM
    server on `localhost:1234` (`Connection refused` in this sandbox) —
    the same network-egress-blocked limitation already documented in
    every prior sprint's own report.
  - The rest (`test_emotion_engine.py`, `test_streaming_e2e.py`,
    `test_streaming_speech_integration.py`, `test_tts_chunk_pipelining.py`,
    `test_tts_e2e_pipeline.py`, `test_voice_pipeline_latency.py`,
    `test_state_isolation.py`, `test_dashboard.py::test_35...`) were
    individually re-run file-by-file (and, for the two that still showed
    a failure in per-file runs, as single isolated tests): every one
    passed cleanly alone — `test_dashboard.py` 47/47, `test_emotion_
    engine.py` 40/40, `test_streaming_speech_integration.py` 22/22,
    `test_tts_chunk_pipelining.py` 19/19, `test_tts_e2e_pipeline.py`
    3/3, `test_voice_pipeline_latency.py` 8/8,
    `test_state_isolation.py::test_planner_turn_thread_can_genuinely_
    outlive_console_stop` and `test_streaming_e2e.py::test_D_barge_in_
    between_llm_and_tts_chunk_never_plays` both pass when run as the
    single selected test. This is full-suite cross-test thread/timing
    interference, the exact class of flakiness `docs/testing/regression_
    baseline.md` already documents this project sampling test files in
    curated batches to avoid (see that file's own "`test_dashboard.py`
    individually exceeds the budget" note).
- Targeted HA/context regression (all together):
  `tests/test_sprint52_ha_entity_resolution.py` +
  `tests/test_sprint56_ha_safety_matrix.py` +
  `tests/test_sprint56_query_entity_differentiator.py` +
  `tests/test_sprint57_contextual_ha_references.py` +
  `tests/test_sprint57_ha_contextual_reference.py` +
  `tests/test_device_context.py` +
  `tests/test_sprint58_ha_multi_entity_commands.py` — **162/162 passed**.

## 12. Live verification status

**Not available.** This sandbox has no network egress to a real Home
Assistant instance (the same limitation documented in every Sprint 52/
55/56/57 report). Verification here is **simulated/integration-level
only**: real `IntentParser`, real `RealHomeAssistantHandler` resolution
logic, real `luno.devices` registry, against a `FakeHAClient` test
double. No claim of real-device verification is made.

## 13. Performance

Measured directly (`time.perf_counter()`, 100–200 iterations, warm
Python process, real device registry):

- Explicit multi-target (`_apply_ha_group_resolution`): ~0.085ms avg
- Group-all lights: ~0.026ms avg
- Non-group passthrough (single target, the common case): ~0.016ms avg

All several orders of magnitude under the 5ms target. No network calls
occur during resolution (`_resolve_entity_tiered()` is confirmed
client-free). No O(N²) behavior — target resolution is O(targets ×
registry size), and the registry is a small, in-memory dict.

## 14. Persistent-state verification

`config/lights.config.json`, `config/switches.config.json`,
`config/scripts.config.json` — MD5-identical before and after running
every group-resolution test scenario (`test_U`, plus a direct
before/after hash comparison during this sprint's own manual
verification). No new instance attribute is created on
`PlannerBridgeModule` by any group-resolution call (`test_U`
enumerates `vars(bridge)` before/after and asserts the key set is
unchanged). No new persistent file is written anywhere by this sprint.

## 15. Known limitations

- Area-scoped groups and contextual groups are not implemented (§4).
- "Semua lampu" only enumerates `luno.devices.LIGHTS` — there is no
  brief-provided example of "semua saklar" (all switches), so it was not
  added; the same `_ha_group_all_lights_shape()` pattern could be
  extended to switches later if a real need appears.
- Multi-target detection is scoped to pure Indonesian "dan"-joined text
  only (§5) — a comma/"and"/"then"-joined explicit list of devices
  (e.g. "matikan A, B, dan C" with an Oxford comma) is not detected as a
  group today; it falls through unchanged (the first clause's own device
  still executes normally, matching pre-existing single-clause-per-verb
  behavior — no regression, just no NEW group detection for that exact
  phrasing). This is a deliberate, documented scope reduction, not an
  oversight.
- Runtime unavailability (an entity that resolves cleanly but is
  reported "unavailable" by the live HA state at execution time) is
  handled per-target by the existing, unmodified `RealHomeAssistantHandler.
  execute()` verification loop, independent of the other targets in the
  group (`test_M`) — the all-or-nothing guarantee this sprint adds is
  about RESOLUTION-time validation, not RUNTIME execution outcomes; that
  distinction is deliberate and documented here so it is never mistaken
  for a broken guarantee.
- A device name that literally contains the word "dan" mis-splits due to
  a pre-existing, untouched parser limitation (§7) — fails safely
  (refuses) rather than executing against the wrong fragment, but does
  not resolve correctly either.

## 16. Recommended next sprint

Given the deferred scenarios above, two independent candidates:

1. **Area/room metadata for the device registry** — a genuinely new
   config schema addition (not an HA sprint at all), which would then
   unblock "semua lampu di kamar" as a follow-up group-command sprint
   without any guessing.
2. **`config/long_term_memory.json` recovery** — diagnosed but not fixed
   in Sprint 57 (corrupted, unknown encoding, no backup) — still
   outstanding.
