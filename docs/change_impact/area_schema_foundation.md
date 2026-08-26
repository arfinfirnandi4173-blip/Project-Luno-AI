# Sprint 60 — Structured Room/Area Schema Foundation

## 1. Objective

Build a minimal, additive, backward-compatible **schema foundation** for
room/area metadata in the Home Assistant device registry, so a future
sprint can generalize Sprint 59's single-room ("kamar") group control
into true multi-room control — **without** implementing multi-room
control itself, and **without** changing any command's observable
behavior today. This is a schema + registry sprint only.

## 2. Root cause / architecture finding

Phase 0 reconnaissance (re-)confirmed what Sprint 58/59 already found:
zero structured area/room/zone field exists anywhere in this project's
device registry or config files. `luno.devices.LIGHTS` — populated from
`config/lights.config.json` by `load_lights_config()` — is the one
canonical, already-in-use source every HA-adjacent module reads
directly:

- `luno/tool_manager/builtin/real_home_assistant.py`'s `_lookup_light()`,
  `_all_known_device_names()`, `_all_known_device_entities()`.
- `main_runtime_demo.py`'s `PlannerBridgeModule._apply_ha_group_
  resolution()` (Sprint 58/59's own group-expansion loop).
- `luno/environment_intent.py`.
- `luno/main.py` (legacy assistant entry point).

Every `LIGHTS` entry is **already a dict** — even the "short format"
(bare `entity_id` string) is normalized into one at load time inside
`load_lights_config()` — so no consumer anywhere validates or iterates
the dict's *key set*; every consumer reads named keys via `.get(...)`.
This means adding one more optional key is **purely additive**: nothing
downstream can break just because a new key exists.

`SWITCHES` (`config/switches.config.json`) is a flat `name -> entity_id`
mapping with **no per-device dict at all** — extending it to carry area
metadata would be a structural format change, not an additive one.
Since Sprint 58/59's group-domain scope is `light` only (`GROUP DOMAIN
= light`, per Sprint 59's own brief), and neither configured switch
(`Baterai`, `Aquascape`) has any location evidence at all, extending
`SWITCHES`' schema was **out of scope** for this sprint — see §12.

**Conclusion: `luno.devices.LIGHTS` is the one canonical, safe source
for this schema work.** None of Sprint 60's 7 STOP CONDITIONS were
triggered — see §14.

## 3. Schema final

An optional string field `"area"` was added to `config/lights.config.
json`'s per-light dict format (`luno/devices.py::load_lights_config()`):

```json
"Main Lamp": {
    "entity_id": "light.kamar_tidur_light_bulb",
    "max_brightness": 125,
    "fade_transition": 0,
    "aliases": ["lampu utama"],
    "area": "kamar"
}
```

Naming: `"area"` (not `"room"`/`"zone"`) was chosen to match this
project's own **pre-existing** vocabulary already used throughout
`main_runtime_demo.py`'s Sprint 58/59 code (`area_word`, `_GROUP_AREA_
RE`, "area-scoped groups") — not a new convention invented for this
sprint.

**Validation** (`luno/devices.py::_normalize_optional_area()`,
`load_lights_config()`) — never fails the whole device, never crashes
the loader:

| Input | Result |
|---|---|
| `"area"` key absent | `None` (valid — pre-Sprint-60 shape) |
| `"area": ""` / whitespace-only | `None` (same convention as empty `aliases` entries) |
| `"area": "  Kamar  "` | `"kamar"` (stripped + lowercased, matching every other name/key/alias in this file) |
| `"area": 123` / `[...]` / `{...}` (non-string) | Logged warning, ignored → `None`. The device itself is **still registered** — an optional field's bad type never drops an otherwise-valid device (unlike a missing `entity_id`, which is mandatory and does skip the entry). |
| short-format entry (bare `entity_id` string) | `None` — no per-device object exists to carry an area at all |

Two new, pure, read-only helper functions (`luno/devices.py`):

```python
get_device_area(name_or_alias) -> Optional[str]
get_devices_by_area(area) -> List[str]   # canonical LIGHTS names, not aliases
```

Both re-normalize (case/whitespace) on every call rather than trusting
`LIGHTS`' keys/values are already normalized — defensive against a
registry populated by something other than `load_lights_config()` (e.g.
a test, or a future alternate loader). Neither touches HA, the network,
an LLM, or any persistent store beyond the already-loaded in-process
`LIGHTS` dict.

## 4. Registry support (Phase 2)

- A device **without** `"area"` still registers exactly as before —
  proved by `tests/test_sprint60_area_schema.py::test_A_old_device_
  without_area_still_loads`.
- `entity_id` lookup, exact/alias/fuzzy resolution
  (`RealHomeAssistantHandler._resolve_entity_tiered()`), and domain
  derivation are **completely untouched** — none of that code reads
  `"area"` at all. Proved by tests D–H.
- Duplicate-`entity_id` dedup behavior (Sprint 58's own
  `seen_entities`/`duplicate_count` mechanism) is unmodified.
- Serialization: `config/lights.config.json` remains plain JSON: adding
  one key per entry does not change the file's structure, encoding, or
  how `json.load`/`json.dump` handle it.

## 5. Current single-room migration (Phase 3)

`config/lights.config.json` was updated to tag the 3 lights **already
proven** (Sprint 59's own converging evidence, unchanged) to be in
"kamar" with `"area": "kamar"`:

| Light | `entity_id` | `area` (Sprint 60) |
|---|---|---|
| Main Lamp | `light.kamar_tidur_light_bulb` | `kamar` |
| RGB Strip | `light.wled` | `kamar` |
| RGB Computer | `light.komputer` | `kamar` |

`entity_id`, `aliases`, `max_brightness`, `fade_transition`, and device
name were **not touched** for any of the three.

**Deliberately NOT given an area** (insufficient evidence, per this
sprint's own principle "JANGAN assign area jika evidence tidak
cukup"):

- `Baterai` / `Aquascape` (`config/switches.config.json`) — no location
  evidence exists anywhere for either switch, AND the switches config
  format has no per-device object to carry the field at all (see §2).
- `gaming mode` (`config/scripts.config.json`) — a script, not a
  physical device with a location; out of scope for an area field by
  definition.

## 6. Compatibility with Sprint 59 (Phase 4)

`_apply_ha_group_resolution()`'s existing `group_all_light` branch in
`main_runtime_demo.py` now prefers structured area metadata as the
source of truth for **which lights are in scope**, wherever that data
actually exists, while leaving the **shape-detection** step (Sprint
58's own `_GROUP_ALL_WORD_RE`/`_GROUP_LIGHT_WORD_RE`/`_GROUP_AREA_RE`,
and Sprint 59's own `_is_single_room_word()`) completely untouched —
this sprint only changed what happens *after* a room word is already
recognized as "kamar", never how a room word is detected in the first
place.

```
any_structured_area = any(cfg.get("area") for cfg in devices.LIGHTS.values())

if area_word is not None and any_structured_area:
    allowed_names = set(devices.get_devices_by_area("kamar"))
else:
    allowed_names = None   # None = every configured light (Sprint 58/59's original, unconditional behavior)
```

Three cases, all proved by dedicated tests:

1. **`area_word is None`** (plain "semua lampu", no room word at all) —
   `allowed_names` stays `None` **unconditionally**, regardless of
   whether area metadata exists — "semua lampu" always means "every
   configured light", byte-for-byte the same as Sprint 58/59. This is
   this sprint's own principle 4 ("SINGLE ROOM MUST REMAIN IDENTICAL"),
   satisfied **structurally**, not just today, by construction — proved
   by `test_Q_nyalakan_semua_lampu_unchanged_even_with_a_light_in_
   another_area`.
2. **`area_word == "kamar"` AND no light anywhere has `"area"` set**
   (an unmigrated registry) — falls back to `allowed_names = None`
   (Sprint 59's original full-registry behavior), so an unmigrated
   config regresses nothing. Proved by `test_R_matches_sprint59_
   behavior_when_no_structured_area_data_exists`.
3. **`area_word == "kamar"` AND structured area data exists** — uses
   `get_devices_by_area("kamar")` as the allowed set. For THIS
   project's real, migrated config (all 3 lights tagged `"kamar"`),
   this returns exactly the same 3 lights the old unconditional loop
   already produced — **identical output**, proved by `test_R_
   nyalakan_semua_lampu_kamar_unchanged` and the end-to-end test
   against the real, on-disk config. For a **hypothetical** future
   light tagged with a different area, this correctly **excludes** it
   — the forward-looking value of the schema — proved by `test_R_
   area_metadata_excludes_lights_in_other_areas`.

**Note on "semua lampu kamar" (no preposition):** `_GROUP_AREA_RE`
(Sprint 58's own regex, untouched) only captures an area word when
`"di"`/`"in"` immediately follows `"lampu"`/`"light(s)"`. "semua lampu
kamar" (no preposition) therefore never captures an area word at all —
`area_word` is `None` — and is always treated as the unqualified
"every light" shape, exactly as Sprint 59 already did (this is why
`test_C_semua_lampu_kamar_no_preposition` in Sprint 59's own test file
still passes unmodified). Only `"...lampu di kamar"` (WITH preposition)
actually exercises the new area-metadata-aware filtering path.

Precedence (explicit target → explicit multi-target → explicit room/
group → differentiator → contextual → refusal) is **completely
unchanged** — this sprint only modified device SELECTION inside one
already-reached branch, never the ORDER in which branches are tried.

## 7. Safety (Phase 6)

- Area metadata **never bypasses** the entity resolver
  (`_resolve_entity_tiered()`/`_resolve_entity_id()`) — it is only ever
  read to build `allowed_names`, a plain `set` of canonical light
  *names*; `entity_id` resolution for anything downstream (single-
  target execution, explicit multi-target) is untouched.
- Area metadata **never bypasses domain checking** — a `LIGHTS` entry
  with an `"area"` but no real `entity_id` is still skipped by the
  existing fail-safe check (`if not entity_id: continue`), proved by
  `test_invariant_area_metadata_never_bypasses_domain_check`.
- An unknown room still produces **zero HA calls** — `_is_single_room_
  word()` (Sprint 59, untouched) still refuses any area word other than
  `"kamar"` before this sprint's new code is ever reached. Proved by
  `test_invariant_unknown_room_still_zero_ha_calls`.
- A device without `"area"` is **never** automatically swept into
  `"kamar"` once ANY structured area data exists in the registry —
  proved by `test_S_device_without_area_is_never_accidentally_assigned`
  (a mixed registry: one `"kamar"`-tagged light, one area-less light —
  only the tagged one is included).
- No guessing based on device name — `get_devices_by_area()` only ever
  compares the literal `"area"` field, never a device's name/alias
  string.
- No LLM/embedding call anywhere — `get_device_area()`/`get_devices_
  by_area()` are ordinary synchronous functions (`inspect.
  iscoroutinefunction()` is `False` for both — proved by `test_
  invariant_helpers_are_synchronous_pure_functions`), and neither
  imports anything network- or LLM-related.
- No network call during area resolution — both helpers only ever read
  the already-loaded, in-process `LIGHTS` dict.
- No new persistent state — `"area"` lives inside the exact same
  `config/lights.config.json` file/format Sprint 52 already reads from;
  no new file, no database, no cache.

## 8. Test results (Phase 5)

New file: `tests/test_sprint60_area_schema.py` — **27 tests, 0
failed.** Covers scenarios A–T from the brief's own matrix (several with
extra coverage beyond the minimum, e.g. J/Q/R each have 2 tests) plus 4
safety-invariant tests, 1 performance test, and 1 realistic end-to-end
test against the REAL, migrated `config/lights.config.json`.

L–P (existing Sprint 52/56/57/58/59 suites still pass) are proved via
the targeted regression run below, not duplicated as new assertions —
matching the exact convention Sprint 58/59's own test files already
established.

## 9. Targeted regression

```
tests/test_sprint52_ha_entity_resolution.py +
tests/test_sprint56_ha_safety_matrix.py +
tests/test_sprint56_query_entity_differentiator.py +
tests/test_sprint57_contextual_ha_references.py +
tests/test_sprint57_ha_contextual_reference.py +
tests/test_device_context.py +
tests/test_sprint58_ha_multi_entity_commands.py +
tests/test_sprint59_single_room_group_control.py +
tests/test_sprint60_area_schema.py
```

**210 passed, 0 failed.**

One necessary Sprint 58 test update (not a regression): `test_sprint58_
ha_multi_entity_commands.py::test_F_area_qualified_group_is_honestly_
refused_not_guessed` originally asserted **no** `LIGHTS` entry carried
any `area`/`room`/`zone` key at all (Sprint 58's own gap-analysis
proof). Sprint 60 deliberately adds that exact field, so the assertion
was updated to assert the real registry now carries `"area": "kamar"`
on every light (and still no `"room"`/`"zone"` keys) — a documentation-
of-fact update. The actual command behavior this test exists to prove
(an unsupported area word is still honestly refused, never guessed) is
unchanged.

## 10. Full repository regression

Same single-process workaround established by Sprint 58/59
(`--ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_
bargein.py --timeout=60 --timeout-method=signal`, `test_dashboard.
py::test_36_audio_capture_store_unit_behavior` deselected for the same
pre-existing thread/lock flake, unrelated to this sprint).

**Actual, currently-verified collection for this checkout: 3190 tests**
(`pytest --collect-only` confirms this independently of any run). This
is materially fewer than the 3983 previously documented by Sprint 59's
own regression baseline — that discrepancy could not be explained from
within this sprint (no test file was removed by Sprint 60; the
collection count was independently re-verified via `--collect-only`
before drawing any conclusion) and is called out here explicitly rather
than silently reconciled; it does not indicate any Sprint 60 code
issue, since `--collect-only` shows the same 3190 total both with and
without Sprint 60's changes present.

Two runs were performed. The first run (680.57s) overlapped, unnoticed
until mid-run, with a **stale, leftover full-suite pytest process from
a prior session** still running in the background (confirmed via `ps`,
killed once discovered) — a likely source of extra timing contention.
A second, clean run (679.53s, no concurrent process) was performed
afterward and is the one reported below:

**3158 passed, 28 failed, 3 skipped, 1 deselected** (3190 total,
matching `--collect-only`).

Every failure was classified, and none touch any file this sprint
modified (`luno/devices.py`, `main_runtime_demo.py`'s HA group-
resolution branch, `config/lights.config.json`, or the two test files
touched):

- `test_mic_device_index.py` (4) — environment-specific (already
  documented since Sprint 52/54).
- `test_real_adapters.py` (2) — infrastructure (already documented).
- `test_production_launcher.py` (2, incl. the already-documented
  `test_07` flake) — environment-specific.
- `test_llm_dashboard.py::test_api_llm_endpoint_reports_manager_state`
  (1) — no local LLM server reachable on `localhost:1234` (already
  documented, unchanged since Sprint 54).
- `test_llm_tts_streaming_production.py` (5 this run — `test_03`,
  `test_13`, `test_14`, `test_E2E_3`, `test_latency_regression`) —
  network-dependent (already documented; re-verified directly this
  sprint by running the file's 5 failing tests together in isolation
  with the same `--timeout=60 --timeout-method=signal` flags: only 1 of
  the 5 failed that time, `test_13`, with the console log showing a
  real `SpeechStreamIdleTimeout`/`fish_audio` "no chunk arrived within
  30.0s" error — direct proof this whole file is genuinely attempting,
  and failing, real network I/O in this sandbox, not a code defect;
  WHICH specific test(s) fail varies run-to-run by network-retry
  timing, consistent with every prior sprint's documentation of this
  exact file).
- `test_dashboard.py`, `test_emotion_engine.py`, `test_streaming_e2e.
  py`, `test_streaming_speech_integration.py`, `test_tts_chunk_
  pipelining.py`, `test_tts_e2e_pipeline.py`, `test_voice_pipeline_
  latency.py`, `test_state_isolation.py` (13 combined) — full-suite-
  only cross-test thread/timing interference (already documented class
  since Sprint 55; the exact test(s) failing within `test_dashboard.py`
  shifted from `test_36` (deselected, pre-verified) to `test_35` this
  run — consistent with genuine timing-order flakiness, not a fixed
  regression).
- `test_runtime_demo.py::test_episodic_memory_end_to_end_detect_
  persist_retrieve_alongside_existing_context` (1) — **new to this
  sprint's own regression, individually re-verified twice**: passes in
  isolation (`pytest tests/test_runtime_demo.py::test_episodic_
  memory_end_to_end_...` → 1 passed, 0.80s) AND passes when its entire
  home file is run standalone (`pytest tests/test_runtime_demo.py` →
  **78 passed, 0 failed**, 19.11s). This sprint's code changes never
  touch episodic memory, `test_runtime_demo.py`, or anything it
  exercises — classified as the same full-suite-only cross-test timing
  interference class as the files immediately above, not a Sprint 60
  regression, on the strength of this direct re-verification (not
  asserted without evidence, per this sprint's own Phase 8
  instruction).

**Zero genuine regressions.**

## 11. Live verification

**Not performed.** This sandbox has no network egress to a real Home
Assistant server or a real LLM/speech provider (the same limitation
documented by every prior sprint, and directly re-confirmed this sprint
by `test_llm_tts_streaming_production.py`'s own real `SpeechStreamIdle
Timeout` network error in §10). All verification in this sprint is
unit, integration, and simulated (`FakeHAClient`) testing — clearly
distinguished from live HA verification throughout §8–§10 and the test
file itself.

## 12. Performance

| Operation | Measured (2000-call average) | Target |
|---|---|---|
| `devices.get_device_area()` | ~0.0006 ms/call | <5ms |
| `devices.get_devices_by_area()` | ~0.0007 ms/call | <5ms |
| `_apply_ha_group_resolution()` (area-metadata path) | ~0.026 ms/call | <5ms |

No polling, no network request, no LLM/embedding call, no blocking
operation anywhere in the area-lookup path — both helpers are plain
synchronous in-process dict lookups (see §7's `inspect.
iscoroutinefunction()` proof).

## 13. Persistent-state verification

MD5 of `config/lights.config.json`, `switches.config.json`, `scripts.
config.json`, `environment_triggers.json`, `persona.json` captured
before and after the clean full-repository regression run (§10) —
**byte-identical**. A dedicated test (`test_T_no_config_corruption`)
additionally hashes these files before/after exercising every Sprint
60 read path (the loader, both helpers, and three `_apply_ha_group_
resolution()` calls) directly, proving those operations are read-only
by construction, not just by incidental observation.

The one **deliberate**, one-time, additive edit this sprint made to
`config/lights.config.json` (adding `"area": "kamar"` to the 3 proven
lights — §5) happened once, BEFORE any test ran, and is the new
baseline every persistent-state check above verifies against — not a
mutation any test itself performs.

## 14. STOP CONDITIONS — none triggered

1. No single canonical safe registry source — **false**;
   `luno.devices.LIGHTS` is canonical and already universally used
   (§2).
2. Major architecture change required — **false**; one optional field
   + two pure helper functions + a device-selection change inside one
   already-existing branch.
3. Risk of breaking existing entity resolution — **false**, verified by
   210/210 targeted regression (§9) and a clean 3158/3190-passing full
   sweep with zero genuine regressions (§10).
4. A device whose real location can't be proven — **false**; only the
   3 lights with Sprint 59's own documented evidence were tagged;
   `Baterai`/`Aquascape`/`gaming mode` deliberately left untagged (§5).
5. Requires a second resolver/memory system — **false**; both helpers
   read the exact same `LIGHTS` dict every other lookup already reads.
6. Requires a persistent-state architecture change — **false**; same
   file, same JSON format, one additive optional key.
7. Requires guessing a room from a device name without evidence —
   **false**; migration used only the same evidence Sprint 59 already
   documented, nothing new was guessed.

## 15. Known limitations

- Only `luno.devices.LIGHTS` (the `light` domain) supports the new
  `"area"` field. `SWITCHES`' flat `name -> entity_id` format has no
  per-device object to carry it — extending that format is a separate,
  larger schema change, and no switch has location evidence anyway
  (§2/§5).
- This sprint does **not** implement multi-room command detection or
  execution. `_GROUP_AREA_RE`/`_is_single_room_word()` (Sprint 58/59,
  untouched) still only ever recognize "kamar" as a valid room word —
  a light tagged with a different `"area"` value today has no command
  surface that can target it by that area name yet. That capability
  (recognizing an ARBITRARY area word, not just "kamar", against
  structured metadata) is exactly what the schema built here now makes
  possible for a future sprint — see §16.
- `"semua lampu kamar"` (no preposition) never captures an area word at
  all (`_GROUP_AREA_RE` requires `"di"`/`"in"`) and is therefore always
  treated as the unqualified "every light" shape — pre-existing Sprint
  58 behavior, unchanged and out of scope for this sprint to alter (see
  §6's note).
- The pre-existing `_REAL_LIGHTS` test-fixture discrepancy found by
  Sprint 59 (RGB Computer's `entity_id` differs between the real config
  and the shared cross-sprint fixture in `tests/test_sprint52_ha_
  entity_resolution.py`) remains unfixed — still out of scope, still
  documented, unrelated to this sprint's own local fixture (which uses
  the correct real value throughout).
- The full-repository test count discrepancy noted in §10 (3190
  actually collected vs. 3983 previously documented) could not be
  explained from within this sprint's own scope and is reported as an
  open, unexplained environment/documentation discrepancy rather than
  silently corrected or ignored.

## 16. Recommended next sprint

**Sprint 61 candidate: Multi-room command detection and execution**,
now that the schema foundation exists. Concretely: generalize `_GROUP_
AREA_RE`'s captured `area_word` to be checked against `devices.get_
devices_by_area(area_word)` directly (instead of only ever comparing
against the single hardcoded `"kamar"` string via `_is_single_room_
word()`), so that once a second room's lights are added to `config/
lights.config.json` with a real `"area"` value (e.g. `"dapur"`), "semua
lampu di dapur" could resolve correctly instead of refusing. This would
be a genuinely small, mostly command-detection-layer change **on top
of** this sprint's already-built, already-tested registry/helper
foundation — not a schema change. A smaller, unrelated alternative,
unchanged since Sprint 57/59: `config/long_term_memory.json` recovery,
if an out-of-band backup can ever be located on the real `E:\Luno Evo`
device.
