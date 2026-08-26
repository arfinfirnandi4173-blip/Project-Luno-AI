# Sprint 59 — Single-Room Home Assistant Group Control

**Status:** COMPLETE for the single room this project's own config
actually evidences ("kamar"). Multi-room is explicitly OUT OF SCOPE and
not implemented — any area word other than "kamar" is still refused
exactly as Sprint 58 already did.

## 1. Root cause / gap

Sprint 58 built group-all ("semua lampu") and explicit multi-target
("A dan B") commands, but explicitly deferred area-scoped groups
("semua lampu di kamar") — it detected the shape (`_GROUP_AREA_RE`) but
always refused it, having found zero structured area/room/zone field
anywhere in the config. Sprint 59's own Phase 0 re-ran that exact same
grep (`area|room|zone`, case-insensitive, across `config/*.json` and
`luno/devices.py`) and got the same empty result — no structured field
exists today, confirmed again, not assumed.

However, this session's own reconnaissance went one step further and
grepped for the literal word "kamar" (Indonesian for "room"/"bedroom")
across every config file, which surfaced converging **textual** evidence
that this project's entire currently-configured light registry lives in
exactly one identifiable room:

- `config/lights.config.json`: Main Lamp's own `entity_id` is literally
  `light.kamar_tidur_light_bulb` ("kamar_tidur" = "bedroom").
- `config/habit_memory.json` and `config/verified_facts.json` both
  independently reference the same `light.kamar_tidur_light_bulb`
  entity_id in unrelated (habit-tracking / verified-fact) records.
- `config/environment_triggers.json`'s own pre-existing, already-shipped
  `"sleepy"` trigger already groups **all three** currently-configured
  lights (`["Main Lamp", "RGB Strip", "RGB Computer"]`) as one unit for
  a single automation — real, human-authored, production evidence that
  this project's own maintainers already treat "every configured light"
  as one room's worth of lights, not a coincidence introduced by this
  sprint.
- `config/persona.json`'s own `smart_home_style` field uses "lampu
  kamar" as its own illustrative smart-home phrase.
- `config/environment_triggers.json` also references `"AC Kamar"` (an
  air conditioner) as an example trigger device name — but it is **not**
  in `luno.devices.LIGHTS`/`SWITCHES`/`SCRIPTS` at all, so it is outside
  this sprint's domain-only scope regardless (see §4).
- No config file anywhere references a second room (no "ruang tamu",
  "dapur", "garasi", ...) as a real, configured target.

**A second, independent finding from this same reconnaissance pass:**
the fuzzy resolver Sprint 52 already built, completely unmodified,
*already* resolves `"lampu kamar"` (no "semua") to a specific single
device today:

```
lampu_kamar -> fuzzy match -> light.kamar_tidur_light_bulb (Main Lamp)
               confidence 0.818, margin comfortably clear (next
               contender scores 0.364), candidate_count=1
```

This happens because `difflib.SequenceMatcher` scores `"lampu_kamar"`
against Main Lamp's own configured alias `"lampu utama"` — both begin
with `"lampu_"` — and that resemblance is large enough, and far enough
ahead of every other device, to resolve unambiguously via the EXISTING
tier-4 fuzzy path (Sprint 52), with zero new code. This directly
explains why the brief's own `"Nyalakan lampu kamar"` example (no
`"semua"`) is a *supported command* today, via precedence rule #1
(explicit entity) — not because this sprint added anything for it.

## 2. Architecture — one small, additive extension, zero new resolvers

The **entire** code change for this sprint is inside Sprint 58's own
`_apply_ha_group_resolution()` orchestrator, in the branch that handles
`command_kind == "group_all_light"` with a non-`None` `area_word`:

- **Before Sprint 59:** any `area_word` at all → unconditional refusal
  (`"not supported yet"`).
- **After Sprint 59:** `area_word` is compared (case-insensitively)
  against a single new constant, `_SINGLE_ROOM_NAME = "kamar"`. A match
  → the group is resolved **exactly the same way** Sprint 58's own plain
  `"semua lampu"` already was (enumerate `luno.devices.LIGHTS`, skip any
  entry with no real `entity_id`, dedupe by `entity_id`, build the
  canonical `"turn on/off <device>, ..."` text). No match → the exact
  same honest refusal as before, now naming which room *is* supported.

No new resolver, no new membership data structure, no new persistent
state. "kamar" is recognized as an **additional way of naming the exact
same set** Sprint 58 already computed for `"semua lampu"` — this is the
single most important design decision in this sprint: room membership
is never independently computed or stored; it is always, structurally,
"every light currently in the registry."

**Precedence (verified, not just designed):**

```
1. Explicit entity                  <- IntentParser's own captured target + Sprint 52 resolver
                                        ("lampu kamar" ALREADY resolves here - fuzzy match to Main Lamp)
2. Explicit multi-entity (Sprint 58) <- _ha_explicit_multi_target_shape()
3. Explicit room/group (Sprint 59)   <- _ha_group_all_lights_shape() + _is_single_room_word()
4. Sprint 56 differentiator          <- _narrow_by_query_differentiator()
5. Sprint 57 contextual reference    <- _apply_device_context()
6. Safe refusal
```

Enforced structurally: `_ha_group_all_lights_shape()` requires the text
to contain `"semua"/"all"/"every"` AND `"lampu"/"light(s)"` — a bare
`"lampu kamar"` (no "semua"/"all"/"every") never even reaches group
detection at all (`test_F`), so it falls through byte-for-byte
unchanged to the pre-existing single-target pipeline, where the
UNMODIFIED Sprint 52 resolver picks it up (rule #1) before rule #3 (this
sprint's own layer) would ever get a chance to misinterpret it as a
group. `_apply_ha_group_resolution()` also still runs BEFORE Sprint 57's
`_apply_device_context()` in `_handle_utterance()` (unchanged ordering
from Sprint 58), so rules #1–#3 always get first refusal over #4–#5.

## 3. Supported commands

| Utterance | Result |
|---|---|
| "Nyalakan semua lampu kamar" | All 3 lights ON (room recognized, no preposition) |
| "Matikan semua lampu kamar" | All 3 lights OFF |
| "Nyalakan semua lampu di kamar" | All 3 lights ON (room recognized, with preposition) |
| "Matikan semua lampu di kamar" | All 3 lights OFF |
| "Nyalakan lampu kamar" | Main Lamp ONLY — resolves as a single explicit entity (rule #1), not a group |
| "Nyalakan semua lampu" | All 3 lights ON — unchanged Sprint 58 behavior (no room word needed; this project has only one room, so this was already unambiguous) |

## 4. Unsupported commands (multi-room explicitly out of scope)

| Utterance | Result |
|---|---|
| "Nyalakan semua lampu di dapur" (or any room other than "kamar") | Refused honestly — "this project only has one identifiable room (\"kamar\") configured" |
| "Nyalakan semua lampu di kamar dan dapur" | Refused (routes through Sprint 58's own explicit-multi-target "dan" path, both fragments fail to resolve, zero HA calls — proven, not assumed) |
| "Nyalakan semua AC kamar" | Never even detected as a group shape at all (no "lampu"/"light" word) — falls through untouched, the existing single-entity resolver refuses honestly rather than guessing "AC" belongs to the light group |
| "Nyalakan lampu meja" | Never a group (no "semua"/"all"/"every") — single-device path, unaffected |

## 5. Safety model

Every FASE 4 scenario the brief required, verified with a dedicated
test:

- **Unknown room** → refused, zero HA calls (`test_J`, `test_O`).
- **Empty group** (registry has zero eligible lights) → refused as a
  safe no-op, zero HA calls (`test_K`) — same mechanism Sprint 58 already
  built for plain "semua lampu" with an empty registry.
- **Wrong domain** ("AC" is not a light) → never guessed into the group;
  not even detected as a group shape (`test_L`).
- **Ambiguous/multi-room mention** ("kamar dan dapur") → refused via
  Sprint 58's own explicit-multi-target path, zero HA calls (`test_M`).
- **Unavailable member** at runtime → handled independently per member
  by the existing, unmodified `RealHomeAssistantHandler.execute()`
  verification loop — one member failing never corrupts or blocks the
  others, and never silently reports success for it (`test_N`). This is
  the same explicit scope boundary Sprint 58 documented: the all-or-
  nothing guarantee is about RESOLUTION-time validation (are all targets
  real, unambiguous, correct-domain?), never about RUNTIME execution
  outcomes.
- **Invalid group membership** (a registry entry missing `entity_id`) →
  already excluded by Sprint 58's own fail-safe check, re-verified
  unaffected by this sprint's targeted regression run.
- **Zero HA calls on resolution failure** → proved directly against the
  real production gate (`IntentParser.parse("") == []` →
  `real_task_count == 0` → `self.planner.execute()` never reached),
  exactly like Sprint 58's own critical safety test (`test_O`).

## 6. A pre-existing test-fixture discrepancy found (documented, not fixed)

While building the realistic end-to-end test (Phase 6), this session
found that `tests/test_sprint52_ha_entity_resolution.py`'s own shared
`_REAL_LIGHTS` fixture (used via `_patch_real_devices()` by EVERY HA
test file since Sprint 52, including this one) defines RGB Computer's
`entity_id` as `light.kamar_tidur_pc`, while the REAL, live
`config/lights.config.json` in this checkout actually has
`"RGB Computer": {"entity_id": "light.komputer", ...}`. Per this
sprint's own Phase 0 mandate ("source code is authority if docs
differ"), the real config file is authoritative — but the shared test
fixture is a cross-sprint file used by every prior sprint's own test
suite; changing it is out of scope for a single-room-group sprint and
risks destabilizing unrelated, already-passing tests. This is
documented here as a known, pre-existing discrepancy (not introduced by
this sprint), with this sprint's own tests written to match whatever
`_patch_real_devices()` actually installs (the fixture), consistent
with every other sprint's tests.

## 7. Test results

`tests/test_sprint59_single_room_group_control.py` — new, 21 tests
covering scenarios A–Q plus a realistic Phase 6 end-to-end test. **21
passed, 0 failed.**

One pre-existing Sprint 58 test was updated (not broken):
`tests/test_sprint58_ha_multi_entity_commands.py::test_F_area_qualified_
group_is_honestly_refused_not_guessed` asserted that `"kamar"` was
refused — that was Sprint 58's own explicitly DEFERRED placeholder
behavior, which this sprint intentionally, correctly supersedes for
"kamar" specifically. The test was updated to use `"dapur"` (a genuinely
unsupported room) instead, with a comment explaining the supersession.
This is not a regression — Sprint 58's own STOP CONDITION and deferred-
scope language anticipated exactly this follow-up.

## 8. Targeted regression

`tests/test_sprint52_ha_entity_resolution.py` +
`tests/test_sprint56_ha_safety_matrix.py` +
`tests/test_sprint56_query_entity_differentiator.py` +
`tests/test_sprint57_contextual_ha_references.py` +
`tests/test_sprint57_ha_contextual_reference.py` +
`tests/test_sprint58_ha_multi_entity_commands.py` (with the one test
above updated) + `tests/test_sprint59_single_room_group_control.py` +
`tests/test_device_context.py` + `tests/test_runtime_demo.py` —
**261 passed, 0 failed.**

## 9. Full regression

Same established methodology as Sprint 58
(`--ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_bargein.py
--deselect tests/test_dashboard.py::test_36_audio_capture_store_unit_behavior
--timeout=60 --timeout-method=signal`, single-process): **3950 passed,
28 failed, 4 skipped, 1 deselected** in 707s.

Every one of the 28 failures was cross-checked against Sprint 58's own
already-completed, file-by-file/test-by-test isolation investigation
(same files: `test_mic_device_index.py`, `test_real_adapters.py`,
`test_production_launcher.py`, `test_llm_dashboard.py`, `test_llm_tts_
streaming_production.py`, `test_dashboard.py`, `test_emotion_engine.py`,
`test_streaming_e2e.py`, `test_streaming_speech_integration.py`,
`test_tts_chunk_pipelining.py`, `test_tts_e2e_pipeline.py`,
`test_voice_pipeline_latency.py`, `test_state_isolation.py`,
`test_runtime_demo.py`) — the same environment-specific / network-
dependent / full-suite-only thread-timing flake classes, re-confirmed
this session for the 3 specific tests that weren't already individually
verified in Sprint 58 (`test_production_launcher.py::test_24_
compressed_stability_simulation_many_scheduler_ticks`, `test_runtime_
demo.py::test_episodic_memory_end_to_end_detect_persist_retrieve_
alongside_existing_context`, `test_llm_tts_streaming_production.py::
test_13_cancellation_before_first_audio` — all 3 pass cleanly when run
together in isolation). **Zero genuine regressions.**

`tests/test_runtime_demo.py::test_mixed_utterance_real_command_still_
succeeds_despite_unknown_clause` (the regression Sprint 58 itself found
and fixed) is confirmed still passing — this sprint's own change never
touches `_ha_explicit_multi_target_shape()` at all.

## 10. Live verification status

**Not available.** Same sandbox limitation as every prior sprint (no
network egress to a real Home Assistant instance). This sprint's own
`test_end_to_end_realistic_single_room_all_lights_on` walks the full
production pipeline shape — utterance → group detection → room
resolution → group membership → existing Sprint 52 resolver → existing
parser → existing `RealHomeAssistantHandler.execute()` — against a
`FakeHAClient` (simulated HA). No live-HA claim is made anywhere in this
document or the test file.

## 11. Performance

Measured directly (`time.perf_counter()`, 200-iteration average, real
device registry, warm process):

- "nyalakan semua lampu di kamar" (room, with preposition): ~0.024ms
- "nyalakan semua lampu kamar" (room, no preposition): ~0.021ms
- "nyalakan semua lampu di dapur" (unsupported-room refusal path): ~0.022ms
- "nyalakan lampu kamar" (single-device, group layer no-op): ~0.001ms

All several orders of magnitude under the 5ms target. No polling, no
network request, no LLM call, no embedding, no blocking operation
anywhere in this sprint's own code (verified by direct source read —
the entire new logic is string comparison plus a dict iteration over
the in-memory `luno.devices.LIGHTS` registry).

## 12. Persistent-state verification

`config/lights.config.json`, `config/switches.config.json`,
`config/scripts.config.json` — MD5-identical before/after every check
this sprint ran, including a dedicated automated test
(`test_P_persistent_state_untouched`). No new `PlannerBridgeModule`
instance attribute is created by any call into this sprint's own code
(`test_P_no_new_persistent_state_attribute_introduced`).

## 13. Known limitations

- Multi-room support is explicitly NOT implemented — any room other
  than "kamar" is refused, by design, not by omission.
- "kamar" recognition relies on converging textual/contextual evidence
  (an entity_id substring, a pre-existing environment-trigger grouping,
  and the total absence of a second room anywhere), not a structured,
  per-device "area" field — because no such field exists anywhere in
  this project's config today. If a second room's worth of devices is
  ever added to this registry without a real area field, this sprint's
  "kamar = the whole light registry" equivalence would need to be
  revisited (see §14's own recommendation).
- The `light.komputer` vs `light.kamar_tidur_pc` test-fixture
  discrepancy documented in §6 was found but not fixed (out of scope,
  shared cross-sprint file).
- "AC Kamar" (an air conditioner referenced only in `environment_
  triggers.json`) is not part of this sprint's light-only domain scope,
  and is not itself a configured `luno.devices` entity at all.

## 14. Recommended next sprint

Add a real, structured `area`/`room` field to `config/lights.config.json`
(and `switches.config.json`) as its own small, standalone config-schema
sprint. This would let a future sprint (a) replace this sprint's
evidence-based "kamar = the whole registry" equivalence with an exact,
per-device field, and (b) finally unblock genuine multi-room support
(a second room's lights, with real membership data, rather than another
inferred single room). Until then, `config/long_term_memory.json`'s
still-outstanding corruption (Sprint 55/56/57) remains the other
standing recommendation.
