# Sprint 62 — Multi-Domain Area Group Control

## 1. Objective

Extend Sprint 60/61's Structured Room/Area Schema so an area-qualified
Home Assistant group command works for HA domains beyond `light`,
*wherever a domain's actual registry structure makes that safe*.

## 2. Phase 0/1 finding — the real deliverable of this sprint

Phase 0 reconnaissance (`luno/devices.py`, `main_runtime_demo.py`,
`luno/tool_manager/builtin/real_home_assistant.py`, the real
`config/*.config.json` files, `_CONTEXT_FILL_COMPATIBLE_DOMAINS`) and
Phase 1's explicit per-domain evaluation produced one conclusion for
every domain considered:

| Domain | Registry exists? | Entry format allows `"area"`? | Resolver? | Execution path? | Verdict |
|---|---|---|---|---|---|
| `light` | Yes — `devices.LIGHTS` | Yes — dict-format entries, `"area"` added Sprint 60 | Yes — `_resolve_entity_tiered()` (Sprint 52) | Yes — `_execute_on_off()` | **SUPPORTED** (unchanged, reused as-is) |
| `switch` | Yes — `devices.SWITCHES` | **No** — `load_switches_config()` only ever produces a flat `name -> entity_id` STRING per entry; no dict form, no `"aliases"`, no way to attach `"area"` | Yes | Yes | **DEFERRED** |
| `fan` | No | N/A | No | No | **DEFERRED** |
| `climate` | No | N/A | No | Partial (`set_temperature` exists, no registry) | **DEFERRED** |
| `media_player` | No (only a single hardcoded `CAST_ENTITY_ID` in `.env`, not a name-keyed registry) | N/A | No | No | **DEFERRED** |

Evidence for `switch`, concretely (not assumed): `load_switches_config()`
(`luno/devices.py`) does `switches[name.strip().lower()] = entity_id`
unconditionally for every entry — no `isinstance(cfg, dict)` branch the
way `load_lights_config()` has had since Sprint 60. The real, on-disk
`config/switches.config.json` confirms this is exactly the shape in
production: `{"Baterai": "switch.tasmota_tasmota3", "Aquascape":
"switch.tasmota_tasmota2"}` — plain strings, not objects. There is
structurally nowhere to put an `"area"` key today without first changing
the loader's own parsing shape.

Evidence for `fan`/`climate`/`media_player`: no `devices.py` attribute,
no config loader, no resolver reads from any registry for these domains
at all. The only place they are even named is Sprint 57's own
`_CONTEXT_FILL_COMPATIBLE_DOMAINS` frozenset, whose own docstring already
says this: "included for correctness/forward-compatibility, not because
a real configured example... exists in this checkout."

This is a direct hit on **STOP CONDITION 1** ("Domain registry tidak
memiliki struktur aman untuk area metadata") for every domain except
`light`. Per the brief's own instruction ("Jika hanya `light` yang aman
berdasarkan source saat ini, pertahankan `light` dan dokumentasikan bahwa
domain lain deferred" / "jangan improvisasi arsitektur baru. Dokumentasikan
evidence dan tandai bagian tersebut DEFERRED"), no schema extension was
attempted for any domain but `light`.

## 3. Supported domains

- **`light`** — exactly as Sprint 60/61 left it. Zero functional changes
  in this sprint. Re-verified end to end (scenario A and the full
  Sprint 52–61 regression suite).

## 4. Deferred domains

- **`switch`** — blocked by loader schema (flat string format, no
  `"area"` capability). Re-enabling this would require extending
  `load_switches_config()` to accept an optional dict form (mirroring
  Sprint 60's own precedent for `light`), which is itself a real,
  separate schema-migration decision — out of scope for this sprint's
  conservative mandate ("jangan memperluas schema domain hanya demi
  memenuhi daftar domain").
- **`fan`**, **`climate`**, **`media_player`** — no registry of any kind
  exists yet; deferred even more clearly than `switch`.

## 5. Exact architecture change

**None, functionally.** `_apply_ha_group_resolution()`,
`_GROUP_LIGHT_WORD_RE`, `_GROUP_AREA_RE`, `devices.get_devices_by_area()`
/`get_device_area()` are all byte-for-byte unchanged from Sprint 61. The
only edit to `main_runtime_demo.py` is a documentation-only comment block
placed next to `_GROUP_LIGHT_WORD_RE`'s definition, recording this
sprint's Phase 0/1 domain evaluation and its conclusion — no logic, no
new regex, no new branch. No new helper (`get_switches_by_area()` etc.)
was created (Phase 2's own instruction: prefer the existing generic
helper, never add a domain-specific one without a real requirement — and
there is none here since no second domain reached "safe to extend").

## 6. Why an "unsupported domain" group command already refuses safely, unchanged

`_GROUP_LIGHT_WORD_RE = re.compile(r"\blampu\b|\blights?\b")` only ever
matches "lampu"/"light(s)". A command like `"matikan semua switch di
kamar"` or `"nyalakan semua AC di kamar"` never satisfies
`_ha_group_all_lights_shape()`'s own gate (`_GROUP_ALL_WORD_RE.search()`
AND `_GROUP_LIGHT_WORD_RE.search()` both required), so `command_kind`
stays `None` and `_apply_ha_group_resolution()` returns the text
**completely untouched** — proved directly (not just asserted) in this
sprint's own test file: `eff == text` for every unsupported-domain
phrasing tried.

The untouched text then flows into the same single-target pipeline every
non-group command already used before Sprint 58 ever existed:
`IntentParser.parse()` produces one `turn_on`/`turn_off` step whose
target is the whole remaining phrase (e.g. `"semua_switch_di_kamar"`),
and `RealHomeAssistantHandler._resolve_entity_tiered()` fails to match it
against any real light/switch/script (exact, alias, or fuzzy — no
device is plausibly named "semua switch di kamar"), returning
`resolution_method="unknown"`, `executable=False`. Traced one level
further, `RealHomeAssistantHandler.execute()` has its own explicit guard:

```python
if target and entity_id is None:
    return self._unknown_device_result(tool_call, target)
```

This returns **before** the `with self._lock:` block that would ever
reach `self._client.call_service(...)` — the actual, traced mechanism
that guarantees **zero Home Assistant calls**, proved directly against a
`FakeHAClient` in this sprint's own tests (`client.calls` stays empty).

No new detection code was needed to satisfy PHASE 3's "unsupported
domain refuses explicitly, zero HA calls" requirement — it was already
true, by construction, before this sprint began.

## 7. Group membership rules (unchanged)

Identical to Sprint 61: an area-qualified light group only includes
lights whose structured `"area"` exactly (case-insensitive) matches the
captured word, AND whose `entity_id` domain is `light`. A configured
switch (even one that happens to exist in the same physical room) never
joins a light area-group — proved directly in this sprint's tests using
a real switch fixture (`Baterai`) alongside the light fixtures.

## 8. Area without "semua" / multi-target / contextual precedence (unchanged)

All unchanged from Sprint 58/59/61 and re-verified in this sprint's own
test file: `"lampu kamar"` (no "semua") never enters group resolution at
all (Sprint 52 single-device path owns it, scenario F); Sprint 58's
explicit "A dan B" multi-target shape still works and is never captured
by area/group resolution (scenario G); Sprint 57's contextual
fill-in-the-blank still has correct precedence (scenario H); Sprint 58's
own mixed-utterance regression fix ("turn on the lights and how's the
weather today" must not become a 2-target group) remains fixed
(scenario O).

## 9. Safety guarantees (all proved by tests, not just asserted)

- Unknown area → refusal, zero HA calls (scenario C, reused from
  Sprint 61).
- Unsupported domain → refusal, zero HA calls, traced to
  `RealHomeAssistantHandler.execute()`'s own guard (scenario D — the new
  proof this sprint adds).
- Area valid but zero eligible lights → same safe refusal path as
  unknown area (scenario E).
- No fuzzy area matching, structurally and behaviorally (scenario K).
- No partial execution — an unresolved group always yields zero steps,
  never a subset (scenario M).
- No LLM/network/blocking call anywhere in resolution (unchanged from
  Sprint 61 — no new I/O was introduced).
- Invalid/missing `"area"` metadata (wrong type, or absent) never
  crashes and never matches (scenario P).
- Area normalization is case-insensitive, exact-match only, consistent
  across calls (scenario Q).

## 10. Test results

New file: `tests/test_sprint62_multi_domain_area_groups.py` — **26
tests, 0 failed**, covering scenarios A–R plus persistent-state and
end-to-end checks.

## 11. Targeted regression

`tests/test_sprint52_ha_entity_resolution.py` + `tests/test_sprint56_
ha_safety_matrix.py` + `tests/test_sprint56_query_entity_differentiator.py`
+ `tests/test_sprint57_contextual_ha_references.py` + `tests/
test_sprint57_ha_contextual_reference.py` + `tests/test_device_context.py`
+ `tests/test_sprint58_ha_multi_entity_commands.py` + `tests/
test_sprint59_single_room_group_control.py` + `tests/
test_sprint60_area_schema.py` + `tests/test_sprint61_generalized_area_
groups.py` + `tests/test_sprint62_multi_domain_area_groups.py` — **271
passed, 0 failed**. Related runtime/dashboard tests (`test_runtime_
demo.py` + `test_dashboard.py`, deselecting the one known-flaky test) —
**124 passed, 0 failed**.

## 12. Full repository regression

`python3 -m pytest tests/ -q --ignore=tests/test_main_bargein.py
--ignore=tests/test_root_main_bargein.py --timeout=60
--timeout-method=signal` (same methodology as Sprint 58–61): **3220
passed, 28 failed, 3 skipped** in 741s. Collection count (3251 =
3220+28+3) is consistent with Sprint 61's own 3225 plus this sprint's 26
new tests.

Every one of the 28 failures was individually re-run in isolation and
classified:

- **15 tests** (`test_llm_tts_streaming_production.py` ×4, `test_
  streaming_e2e.py` ×1, `test_streaming_speech_integration.py` ×1,
  `test_tts_chunk_pipelining.py` ×3, `test_tts_e2e_pipeline.py` ×2,
  `test_voice_pipeline_latency.py` ×3, plus `test_production_launcher.
  py::test_24`, `test_llm_dashboard.py` counted separately below) — all
  passed cleanly when re-run together in an isolated batch — confirmed
  **full-suite-only cross-test timing interference**, the same class
  documented continuously since Sprint 55.
- **5 tests** (`test_dashboard.py` ×2, `test_emotion_engine.py` ×1,
  `test_runtime_demo.py::test_episodic_memory_end_to_end_...` ×1 — the
  same test re-verified in isolation for the 4th consecutive sprint —
  and `test_state_isolation.py` ×1) — all passed when re-run in a small
  isolated batch; `test_state_isolation.py`'s own test additionally
  confirmed passing completely alone. Same timing-interference class.
  (`test_dashboard.py::test_36_audio_capture_store_unit_behavior` is the
  long-documented order-dependent flake normally excluded via
  `--deselect` in prior sprints' own full-sweep command — this sprint's
  sweep did not pass that flag, so it surfaced here instead of being
  deselected; re-verified passing in isolation, consistent with its
  pre-existing, already-documented nature.)
- **8 tests** (`test_mic_device_index.py` ×4, `test_real_adapters.py`
  ×2, `test_production_launcher.py::test_07`, `test_llm_dashboard.py`
  ×1) — failed even in isolation, confirmed genuine **environment/
  infrastructure** failures (missing audio hardware/`sounddevice`
  dependency, no local LLM/speech server reachable from this sandbox) —
  the same class documented since Sprint 55, unrelated to any file this
  sprint touched.

**Zero genuine regressions.** No file this sprint modified
(`main_runtime_demo.py`'s comment-only edit, the new test file) appears
in any failure's own traceback or import chain.

## 13. Live verification

Not performed and not claimed. This sandbox has no access to a real Home
Assistant server — every execution proof in this sprint (scenario D's
zero-HA-calls check, the end-to-end test) uses `FakeHAClient`, the same
convention every prior sprint in this series has used.

## 14. Performance

`_apply_ha_group_resolution()` for both the supported `light`
area-group path and the unsupported-domain fallthrough path: well under
the 5ms target (measured over 300 iterations each; see scenario R). No
network/HA API/LLM/embedding/blocking I/O call anywhere in resolution
(unchanged from Sprint 61 — this sprint added no new code on that path).

## 15. Persistent-state verification

`config/*.json` unchanged by any code path this sprint exercises — no
loader, no config file, and no write path was touched (this sprint's
only production-code edit is a comment). A dedicated automated test
(`test_persistent_state_unmodified_by_this_sprints_resolution_paths`)
hashes `LIGHTS_CONFIG_FILE`/`SWITCHES_CONFIG_FILE`/`SCRIPTS_CONFIG_FILE`
before and after exercising every resolution path this sprint's tests
cover (light area group, unsupported-domain group, unknown area) and
passed.

## 16. Known limitations

- `switch` area-group support remains unavailable until a real,
  deliberate decision is made to extend `load_switches_config()`'s own
  schema (dict-format entries, optional `"area"`/`"aliases"`) — a
  genuine, separate migration decision, not attempted here.
- `fan`/`climate`/`media_player` have no registry foundation at all;
  supporting them would require building that foundation first (a much
  larger scope than this sprint's mandate).
- No live Home Assistant server verification (sandbox has no HA
  access — same limitation as every prior sprint in this series).

## 17. STOP CONDITIONS — evaluated

STOP CONDITION 1 ("domain registry tidak memiliki struktur aman untuk
area metadata") applies to `switch`/`fan`/`climate`/`media_player`, and
was honored — no schema was forced onto any of them. None of the other 8
STOP CONDITIONS were triggered: no second resolver was created (§5), no
global `last_entity` was introduced, no LLM/embedding was used for area
detection, area ambiguity resolution remains fully deterministic
(exact-match only), Sprint 52/56/57 precedence was never at risk (no
detection-side change was made at all), Sprint 58's explicit
multi-target shape cannot be caught as a group (re-verified, scenario
G), unknown-area/unsupported-domain group commands cannot partially
execute (scenario M, and traced through `execute()`'s own guard), no
persistent config was modified at runtime, and the existing regression
set was fully classified with evidence (§12) — nothing was left
unexplained.

## 18. Next recommended sprint

If `switch` area-group support becomes a real priority: a dedicated
sprint to extend `load_switches_config()` to accept the same
short-form/long-form duality `load_lights_config()` already has (Sprint
60's own precedent), including an optional `"area"` field — then reuse
`devices.get_devices_by_area()`'s exact pattern (generalized to accept a
registry, or literally the same function if `SWITCHES`/`LIGHTS` are
ever unified into one generic device table) rather than a
switch-specific helper. Until that schema decision is made, `switch`
area-group commands will keep refusing safely exactly as they do today.
