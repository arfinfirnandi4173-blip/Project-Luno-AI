# Sprint 61 — Generalized Area-Aware Home Assistant Group Command

## 1. Objective

Generalize the Home Assistant group command handling that Sprint 59/60
still hardcoded to the single area `"kamar"` into a truly area-aware
mechanism, using Sprint 60's structured area metadata
(`devices.get_device_area()`/`get_devices_by_area()`) as the ONLY
source of truth for room/area membership — for ANY area word, not just
"kamar". This is a `main_runtime_demo.py`-internal generalization only:
single-domain (`light`) group scope, and full compatibility with
Sprint 52–60, per this sprint's own explicit focus.

## 2. Root cause / Phase 0 finding

Phase 0 reconnaissance (re-)read `docs/project_handover.md`, `docs/
project_handover.json`, `ARCHITECTURE_GUARD.md` §59–61, `docs/change_
impact/area_schema_foundation.md`, `ha_single_room_group_control.md`,
`ha_multi_entity_commands.md`, and the source of `luno/devices.py`,
`main_runtime_demo.py`, and `luno/tool_manager/builtin/real_home_
assistant.py`.

**Finding: the ONLY hardcoding was inside `_apply_ha_group_resolution()`
itself**, not in the parser/detection layer. `_GROUP_AREA_RE` (Sprint
58) already captured ANY area word generically:

```python
_GROUP_AREA_RE = re.compile(r"\b(?:lampu|lights?)\b\s+(?:di|in)\s+(\w+)", re.IGNORECASE)
```

The hardcoding was entirely in what happened AFTER capture — Sprint 59
added `_SINGLE_ROOM_NAME = "kamar"` and `_is_single_room_word()`, and
`_apply_ha_group_resolution()`'s `group_all_light` branch compared the
captured `area_word` against that one literal string, refusing any
other value regardless of what Sprint 60's structured metadata said.
This meant Sprint 60's own `get_devices_by_area()` helper existed and
was even called — but only ever with the literal argument `"kamar"`,
never with the actually-captured `area_word` for a different area.

**Conclusion: no parser change was needed at all.** The entire
generalization is contained inside `_apply_ha_group_resolution()`'s
existing `group_all_light` branch.

## 3. Exact architecture change

`_apply_ha_group_resolution()`'s `group_all_light` branch now does:

```python
if area_word is None:
    allowed_names = None          # unconditionally every configured light
    area_recognized = True
else:
    matched_names = devices.get_devices_by_area(area_word)   # Sprint 60, exact match, case-insensitive
    allowed_names = set(matched_names)
    area_recognized = bool(matched_names)

if area_word is not None and not area_recognized:
    # refuse - zero HA calls, no fallback, no fuzzy "closest area" guess
    ...
else:
    # expand allowed_names via Sprint 58's own unchanged enumeration/
    # dedup loop, with a defensive `domain == "light"` re-check added
```

`_SINGLE_ROOM_NAME` and `_is_single_room_word()` were **removed
entirely** — confirmed via `grep` that neither had any consumer
anywhere else in this codebase (`main_runtime_demo.py`, any test file,
any other module) before removal. A new test
(`test_invariant_single_room_hardcoding_fully_removed`) asserts both
attributes no longer exist on `PlannerBridgeModule`, so this cannot
silently regress back in later.

**A defensive domain check was added** inside the enumeration loop
(`if domain != "light": continue`) — currently unreachable in practice
(`devices.LIGHTS` only ever contains `light.*` entity_ids by
construction), but makes the "GROUP DOMAIN = light" scope explicit and
future-proof rather than merely implicit, matching the flow diagram in
this sprint's own brief (PHASE 2: "validate domain" as an explicit
step).

**Event Bus field renamed**: `room_word_recognized` → `area_recognized`
(clearer post-generalization; no test or consumer read the old key by
name, confirmed via `grep` before renaming — see §8).

## 4. Area normalization

Unchanged from Sprint 60 — `devices.get_devices_by_area()` itself does
the normalization (`.strip().lower()`), exact match only, no fuzzy
matching anywhere in the path. `"Kamar"`, `"kamar"`, `"KAMAR"`,
`"KaMaR"` all normalize identically and resolve to the same device set
(proved by tests E/F). A thematically-similar area name (e.g. "ruang
makan" vs "dapur") is never conflated (proved by `test_no_fuzzy_room_
matching_between_similar_area_names`).

## 5. Unknown-area behavior (PHASE 8's safety rule)

An area word that exact-matches **zero** lights' `"area"` field — for
ANY reason (a genuinely different, unconfigured room; a typo; or a
registry that was never migrated to carry any `"area"` metadata at
all) — **always refuses**, unconditionally:

- `effective_text` is set to the empty string.
- `IntentParser.parse("")` produces zero steps.
- `_handle_utterance()`'s own pre-existing `real_task_count > 0` gate
  never calls `self.planner.execute()` — the actual mechanism
  guaranteeing zero HA calls, proved against this real gate (not just
  asserted) by `test_H_unknown_area_zero_ha_calls`.
- The refusal message now enumerates the actually-known area(s) in the
  live registry (e.g. `"known area(s) right now: dapur, kamar"`) rather
  than a single hardcoded room name, so the honest explanation stays
  accurate as more areas are configured over time.

**This is a genuine, deliberate behavior change from Sprint 60** — see
§13.

## 6. Group membership rules (PHASE 4)

For `"nyalakan semua lampu di kamar"`: only devices with `area ==
"kamar"` (exact, case-insensitive) AND `domain == "light"` join the
group. For `"...di dapur"`: only `area == "dapur"` AND `domain ==
"light"`. A device in a different area, a device with no area at all,
and a non-light-domain device (even if it happens to carry a matching
`"area"` tag) are all excluded — every one of these is a dedicated,
passing test (J, L, `test_Y_untagged_light_never_joins_any_area_
group`).

PHASE 4's own worked example is a literal test
(`test_D_matikan_semua_lampu_di_dapur_excludes_kamar_lights` and its
sibling `test_D_matikan_semua_lampu_di_kamar` -style Main Lamp/RGB
Computer/Kitchen Lamp fixture): `"matikan semua lampu di kamar"` yields
exactly Main Lamp + RGB Computer, never Kitchen Lamp.

## 7. Area WITHOUT "semua" (PHASE 5)

Unchanged. `_ha_group_all_lights_shape()` (Sprint 58, untouched)
requires BOTH `_GROUP_ALL_WORD_RE` ("semua"/"all"/"every") AND
`_GROUP_LIGHT_WORD_RE` ("lampu"/"light(s)") to match at all — a bare
`"lampu kamar"` (no "semua") never enters this shape, and is instead
resolved directly by the completely unmodified Sprint 52 fuzzy resolver
(to a single specific device, not a group) — the exact same precedence
proof Sprint 59 already established, unaffected by this sprint.

Also unchanged: `_GROUP_AREA_RE` only captures an area word when "di"/
"in" immediately follows "lampu"/"light(s)" — `"semua lampu kamar"` (no
preposition) and `"semua lampu dapur"` (no preposition) both still
never capture an area word at all, and are both still treated as the
unqualified "every configured light" shape, exactly as Sprint 58/59
already established (proved by `test_A_semua_lampu_kamar_still_works`
and `test_C_semua_lampu_dapur_no_preposition_is_the_unqualified_
shape`). This sprint deliberately did NOT touch `_GROUP_AREA_RE` — per
PHASE 5's own instruction not to change existing parser semantics
globally.

## 8. Multi-target compatibility and precedence (PHASE 6)

Sprint 58's explicit multi-target shape (`_ha_explicit_multi_target_
shape()`) is a structurally SEPARATE, mutually-exclusive check from
`group_all_light` — `_apply_ha_group_resolution()` tries `_ha_group_
all_lights_shape()` FIRST, and only falls through to `_ha_explicit_
multi_target_shape()` if that returns `None`. Neither shape check was
modified by this sprint. `"nyalakan lampu kamar dan lampu ruang
tamu"` — an explicit "A dan B" utterance — is proved (`test_P_multi_
target_never_caught_as_an_area_group`) to still be handled by the
explicit-multi-target path (refusing as an unresolved multi-target
when neither name is a registered device in the test fixture), never
silently reinterpreted as an area-group command.

**On the brief's own listed precedence** (explicit target → exact/
alias → fuzzy → differentiator → contextual → area/group → refusal):
this codebase's actual, pre-existing architecture (unchanged since
Sprint 58) checks `_apply_ha_group_resolution()` (covering BOTH
group-all AND explicit-multi-target) BEFORE Sprint 57's contextual
layer in `_handle_utterance()`'s call order. Per this sprint's own
PHASE 6 instruction ("Jika source code memiliki precedence berbeda yang
sengaja dirancang sebelumnya, pertahankan architecture tersebut dan
dokumentasikan alasannya"), this ordering was preserved, not
restructured — because it is safe by construction, not merely by
convention: `_ha_group_all_lights_shape()` requires `IntentParser.
parse(text)` to yield **exactly one step**, and `_ha_explicit_multi_
target_shape()` requires the Indonesian "dan"-only phrasing with a
real, non-filler anchor target. Both shape checks return `None`
immediately for anything that doesn't unambiguously match — including
every single-device command, every explicit multi-target the
differentiator/contextual layers might otherwise handle, and every bare
contextual reference (`"Matikan."`) — so in practice, checking the
group layer "first" in code order never actually pre-empts a case the
later tiers could have handled, because "semua lampu"/area-qualified
group shapes and "A dan B" explicit-multi-target shapes could never
have been meaningfully resolved by exact/alias/fuzzy/differentiator/
contextual resolution anyway (they don't name one single, already-
known device). This was already true and tested since Sprint 58; Sprint
61 only re-verified it holds under the new generalized area logic too
(tests M–P).

## 9. Existing single-room preserved (PHASE 9)

`"nyalakan semua lampu kamar"` and `"nyalakan semua lampu di kamar"`
(Sprint 59's own examples) still work, now through the SAME general
`devices.get_devices_by_area()` mechanism every other area word uses —
no special-casing of the string `"kamar"` remains anywhere in
`main_runtime_demo.py` (`test_Q_sprint59_kamar_behavior_still_works_
without_special_casing`, plus the removed-symbol invariant test in §3).

**A required, additive test-fixture update**: the shared `_REAL_LIGHTS`
fixture in `tests/test_sprint52_ha_entity_resolution.py` (used by
`_patch_real_devices()`, which every one of Sprint 59's own "kamar"-
scoped tests patches) had no `"area"` field at all. Under this sprint's
new PHASE 8 safety rule, an area-qualified command against a registry
with **zero** structured area data anywhere now correctly refuses
(§13) rather than falling back to Sprint 60's "unmigrated registry"
special case — which would have made every Sprint 59 test relying on
that fixture start failing, NOT because of a real regression, but only
because this test-only fixture had never been given the `"area"` field
the REAL, on-disk `config/lights.config.json` has carried since Sprint
60's own migration. `"area": "kamar"` was therefore added to all 3
`_REAL_LIGHTS` entries — purely additive (no `entity_id`/alias
changed), and confirmed (§10) to not affect any Sprint 52/56/57/58
test's outcome, since none of those tests' own logic ever reads the
`"area"` key.

## 10. Test results

New file: `tests/test_sprint61_generalized_area_groups.py` — **34
tests, 0 failed.** Covers scenarios A–Y from the brief's own matrix
(several with 2 tests each) plus a removed-symbol invariant, a
performance test, and a realistic end-to-end test using a two-area
fixture executed against a simulated (`FakeHAClient`) HA backend.

One necessary Sprint 59 test update (not a regression, a deliberate,
documented behavior change — see §13):
`tests/test_sprint59_single_room_group_control.py::test_K_empty_room_
group_is_a_safe_no_op` — an area-qualified command against a
COMPLETELY EMPTY registry now refuses with a different (still safe,
still zero-HA-calls) message than before. A new sibling test,
`test_K_empty_area_word_semua_lampu_still_uses_the_original_empty_
no_op_message`, was added to prove the ORIGINAL "empty_no_op" message
is still fully reachable, unchanged, for the non-area-qualified "semua
lampu" shape.

## 11. Targeted regression

```
tests/test_sprint52_ha_entity_resolution.py +
tests/test_sprint56_ha_safety_matrix.py +
tests/test_sprint56_query_entity_differentiator.py +
tests/test_sprint57_contextual_ha_references.py +
tests/test_sprint57_ha_contextual_reference.py +
tests/test_device_context.py +
tests/test_sprint58_ha_multi_entity_commands.py +
tests/test_sprint59_single_room_group_control.py +
tests/test_sprint60_area_schema.py +
tests/test_sprint61_generalized_area_groups.py
```

**245 passed, 0 failed.**

Related runtime/dashboard tests (`tests/test_runtime_demo.py` +
`tests/test_dashboard.py`, PHASE 11 item 8): **125 passed, 0 failed**
(one background-thread `ConnectionError` warning from an unrelated
streaming test's cleanup, not a test failure).

## 12. Full repository regression

Same single-process workaround established since Sprint 58
(`--ignore=tests/test_main_bargein.py --ignore=tests/test_root_main_
bargein.py --timeout=60 --timeout-method=signal`, `test_dashboard.
py::test_36_audio_capture_store_unit_behavior` deselected for the
pre-existing thread/lock flake).

**3193 passed, 28 failed, 3 skipped, 1 deselected** (3225 total — up
from Sprint 60's 3190, consistent with this sprint's 34 new tests + 1
new Sprint 59 test = 35 net new).

Every failure was individually classified against the SAME class
already documented since Sprint 55/58/59/60 — none touch any file this
sprint modified (`luno/devices.py` was NOT touched this sprint at all;
`main_runtime_demo.py`'s HA group-resolution code, `tests/test_
sprint52_ha_entity_resolution.py`'s fixture, and `tests/test_sprint59_
single_room_group_control.py`'s one test were the only files changed):

- `test_mic_device_index.py` (4), `test_real_adapters.py` (2), `test_
  production_launcher.py` (2) — already-documented environment/
  infrastructure.
- `test_llm_dashboard.py` (1), `test_llm_tts_streaming_production.py`
  (4 this run) — no local LLM/speech server, already documented.
- `test_dashboard.py`, `test_emotion_engine.py`, `test_streaming_e2e.
  py` (2 this run — `test_A` AND `test_D`), `test_streaming_speech_
  integration.py`, `test_tts_chunk_pipelining.py`, `test_tts_e2e_
  pipeline.py`, `test_voice_pipeline_latency.py`, `test_state_
  isolation.py`, `test_runtime_demo.py`'s episodic-memory test —
  full-suite-only cross-test timing interference, already documented
  since Sprint 55/60. TWO specific tests were newly re-verified this
  sprint per PHASE 11's own "jangan menyebut pre-existing tanpa bukti"
  instruction: `test_streaming_e2e.py` (all 6 tests, including the
  newly-seen `test_D`, pass cleanly in isolation — `6 passed` — proving
  `test_D`'s failure is the same file-level flakiness class, not new);
  `test_runtime_demo.py::test_episodic_memory_end_to_end_...` (passes
  in isolation again, `1 passed`, 3rd consecutive re-verification
  across Sprint 60 and this sprint).

**Zero genuine regressions.**

## 13. Exact behavior change

Only ONE observable behavior changed, and it is a deliberate,
documented safety improvement, not an accidental side effect:

**Before (Sprint 60):** an area-qualified group command naming "kamar"
specifically, run against a registry with **zero** structured area
metadata anywhere (an "unmigrated" config), silently succeeded by
falling back to "every configured light" (Sprint 59's original
behavior, kept as a backward-compatibility net).

**After (Sprint 61):** the SAME scenario now refuses — "kamar" is no
longer recognized as inherently special; area recognition is derived
entirely from `devices.get_devices_by_area()`, which requires at least
one light actually tagged with that area to return anything. Both
before and after are SAFE (zero HA calls either way) — only the
refusal reason/wording differs (`"empty_no_op"` → `"refused_unsupported_
area"` for this one specific combination: area-qualified + completely
empty registry). This is the direct, intended consequence of PHASE 8's
explicit safety rule ("unknown area = refusal, never a silent
fallback") applied consistently to every area word, including "kamar".

For every OTHER scenario — this project's real, already-migrated
config (all 3 lights tagged `"area": "kamar"`), and any registry with
at least some structured area data — output is byte-for-byte identical
to Sprint 59/60, proved by tests, not just asserted (`test_Q_sprint59_
kamar_behavior_still_works_without_special_casing`).

## 14. Safety verification (PHASE 7)

- Never a second resolver — `devices.get_devices_by_area()` (Sprint 60,
  unchanged) is the only membership source; `_resolve_ha_group_
  targets()`/`RealHomeAssistantHandler._resolve_entity_tiered()`
  (Sprint 52/58) are completely untouched and used only by the separate
  `explicit_multi_target` branch.
- Never fuzzy area matching — confirmed structurally (`"difflib"` not
  present in `get_devices_by_area()`'s source —
  `test_S_no_fuzzy_room_matching_structurally`) and behaviorally
  (`test_no_fuzzy_room_matching_between_similar_area_names`).
- Never an LLM/embedding call — both area helpers are non-coroutine,
  synchronous functions; `_apply_ha_group_resolution()`'s own source
  contains no LLM/embedding-related token (`test_T_no_llm_call_
  structurally`).
- Never a network/HA API call during resolution — proved with a bridge
  that has no HA client anywhere reachable (`test_U_no_network_call_
  during_resolution`).
- Unknown area → provably zero HA calls, via the real production gate
  mechanism (`test_H`, see §5).
- No persistent-state mutation — `config/*.json` MD5-identical
  before/after the full regression sweep (§15), plus a dedicated test
  exercising area resolution, group resolution, and refusal paths
  directly (`test_V_no_persistent_state_mutation`).
- No new persistent state/database of any kind was created.

## 15. Persistent-state verification

MD5 of `config/lights.config.json`, `switches.config.json`, `scripts.
config.json`, `environment_triggers.json`, `persona.json` captured
before and after the full-repository regression run (§12) —
**byte-identical**. No file was modified by this sprint's own code
changes (unlike Sprint 60, this sprint made no deliberate config
migration — `luno/devices.py` and `config/lights.config.json` were
both left completely untouched; only `main_runtime_demo.py`, one
shared test fixture, one existing test's assertion, and new
documentation/tests were changed).

## 16. Performance

| Operation | Measured (2000-call average) | Target |
|---|---|---|
| `devices.get_devices_by_area()` | ~0.0007 ms/call | <5ms |
| `_apply_ha_group_resolution()` (known area, "kamar") | ~0.027 ms/call | <5ms |
| `_apply_ha_group_resolution()` (unknown area, "garasi") | ~0.022 ms/call | <5ms |

No network, HA API, LLM, embedding, or blocking call anywhere in the
area-resolution path — both known-area and unknown-area (refusal)
paths measured, both far under target.

## 17. Known limitations

- Only the `light` domain participates in area groups (unchanged scope
  decision from Sprint 59/60 — `GROUP DOMAIN = light`). A defensive
  domain check was added (§3) but remains structurally unreachable
  today since `devices.LIGHTS` only ever contains `light.*` entities.
- `"semua lampu <area>"` without a "di"/"in" preposition still never
  scopes to that specific area — it is always the unqualified "every
  light" shape (§7), unchanged since Sprint 58. This could surprise a
  user who omits the preposition expecting area-scoping; documented,
  not changed, per this sprint's own PHASE 5 instruction.
- `SWITCHES`' flat `name -> entity_id` format still has no per-device
  object to carry an `"area"` field — unchanged from Sprint 60, still
  out of scope.
- The pre-existing `_REAL_LIGHTS` fixture's `RGB Computer` `entity_id`
  discrepancy (`light.kamar_tidur_pc` vs. the real config's `light.
  komputer`) remains, deliberately left untouched (§9) — only `"area"`
  was added to that shared fixture this sprint.
- This sprint did not need, and did not perform, any config migration
  — Sprint 60 already migrated the real `config/lights.config.json`;
  Sprint 61 is a pure `main_runtime_demo.py`-internal generalization.

## 18. STOP CONDITIONS — none triggered

1. Second resolver required — **false**; `get_devices_by_area()`
   (Sprint 60) is the only membership source, reused unchanged.
2. Fuzzy area matching required — **false**; exact match only,
   confirmed structurally and behaviorally.
3. Existing parser can't accept a generalized area without major
   change — **false**; `_GROUP_AREA_RE` already captured any area word
   generically; zero parser changes were needed.
4. Risk of changing Sprint 52 semantics — **false**; the single-device
   resolver was not touched at all, verified by 245/245 targeted
   regression including all of Sprint 52's own tests.
5. Risk of breaking Sprint 57 contextual reference — **false**;
   `_apply_device_context()` was not touched; group shape detection's
   own narrow guards (unchanged) still make it a complete no-op for any
   contextual-reference text.
6. Risk of Sprint 58 explicit multi-target being caught as a group —
   **false**; the two shape checks remain structurally separate and
   mutually exclusive, proved by `test_P_multi_target_never_caught_
   as_an_area_group`.
7. Cannot guarantee unknown area → zero HA calls — **false**;
   guaranteed structurally (empty `effective_text` → zero parsed steps
   → the pre-existing `real_task_count > 0` gate never fires), proved
   against the real gate mechanism.
8. New persistent state required — **false**; zero new files, zero new
   config keys, zero new in-memory caches — reads only the exact same
   `LIGHTS` dict Sprint 60 already populated.

## 19. Next recommended sprint

With generalized area-group commands now in place, a natural next step
(only if this project ever gains a genuinely SECOND physical room) is
adding a real, structured `"area"` value to a NEW light in `config/
lights.config.json` — no further code change would be required; the
generalized mechanism built this sprint already supports it end to end.
Smaller, unrelated alternatives, unchanged since Sprint 57/59/60:
`config/long_term_memory.json` recovery, or fixing the pre-existing
`_REAL_LIGHTS`/`RGB Computer` `entity_id` discrepancy as its own tiny,
isolated cleanup sprint.
