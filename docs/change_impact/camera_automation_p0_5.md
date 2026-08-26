# LUNO P0.5 — Real Camera Integration

**Date:** 2026-08-21
**Status:** COMPLETE
**Scope:** new `luno/camera_automation/cameras.py`, new `config/camera_automation.json`, new `ha_camera_discovery.py`, new `tests/test_p0_5_camera_integration.py`, additive changes to `luno/camera_automation/config.py`/`module.py`/`__init__.py`, one literal update in `tests/test_sprint68_mutation_audit_hardening.py`.
**Explicitly NOT touched:** the Event Bus, the existing Home Assistant integration (`luno/adapters/home_assistant.py`), `AutomationEngine`, `luno/bootstrap/modules.py` (P0's own wiring already registers `CameraAutomationModule` — nothing new to wire), memory/LLM/voice/STT/TTS, any other existing config schema, `luno/tool_manager/builtin/real_camera_ptz.py`/`pytapo` PTZ integration (a completely separate subsystem — see Discovery below).

## 1. Goal

Connect the P0 Camera Automation Core to real Home Assistant camera-related entities and normalize their state changes into the semantically clean camera event kinds (`motion_detected`/`motion_cleared`/`human_detected`/`human_cleared`/`camera_online`/`camera_offline`) the P0.5 brief specifies — as an integration sprint on top of P0's already-shipped, already-regression-tested module, not an architecture rewrite.

## 2. Baseline

Full repository sweep before any P0.5 change (identical to P0's own final numbers, re-confirmed via targeted spot-checks this sprint): 4533 collected, 4503 passed, 30 failed, 0 skipped — all 30 pre-existing/environment-specific, none touching camera/HA/automation code. `tests/test_p0_camera_automation.py` (23/23) and `luno/adapters/tests/test_adapters.py` (15/15) re-run clean immediately before writing any P0.5 code.

## 3. Discovery — no live HA access, and an important clarification

This sandbox has real `HA_URL`/`HA_TOKEN` values configured in `.env` (`HOME_ASSISTANT_BACKEND=real`), so a genuine attempt at read-only live discovery was made using the EXISTING `luno.ha_client.HomeAssistantClient` (`connect()` + `get_states()`, read-only, zero service calls — the same class `luno/bootstrap/adapters.py`'s real HA backend already uses, no new HA communication logic written). The attempt failed: `proxy rejected connection: HTTP 403` — this sandbox's own outbound network policy, the same structural limitation every prior camera/HA sprint (69 through P0) has documented for the Tapo LAN connection, now confirmed to apply to this project's separate Home Assistant connection too.

**LIVE VERIFICATION: NOT AVAILABLE.** No real Home Assistant entity_id was observed. None was invented either — `config/camera_automation.json` ships with every entity-role field `null`.

**Important clarification found during discovery:** this project's existing Tapo C212 integration (Sprint 69–72, `luno/tool_manager/builtin/real_camera_ptz.py`) is a **direct `pytapo` connection** to the camera's own LAN API (`TAPO_HOST=192.168.1.4`) for PTZ movement — entirely separate from, and independent of, Home Assistant. Whether this same physical Tapo C212 is *also* registered as a Home Assistant entity (via HA's own Tapo/Onvif/generic camera integration) is unknown and can only be confirmed by live HA discovery, which this sandbox cannot perform. `ha_camera_discovery.py` (new this sprint, see §6) is the tool to close that gap on the user's real machine.

## 4. Architecture — the "Camera Integration Adapter" is generic, not vendor-specific

`CameraProfile` (`luno/camera_automation/cameras.py`) IS the brief's own generic `CameraIntegration` concept. There is no `TapoC212Adapter` class containing Tapo-specific protocol code, because there is none to write honestly: Home Assistant has already translated the vendor's own protocol into generic `binary_sensor`/`camera` entities and plain on/off/unavailable states before `HomeAssistantAdapter.on_state_changed()` (untouched) ever sees them. "Tapo C212 as one provider" is realized as one CONFIGURED `CameraProfile` instance (`camera_id="tapo_c212"`); a second camera brand needs only a second profile entry, zero new code.

Final flow:

```
Tapo C212 (or any HA-exposed camera)
        ↓
Home Assistant (existing, untouched)
        ↓  HomeAssistantAdapter.on_state_changed() (existing, untouched)
        ↓  publishes "device_state_changed"
Existing Luno Event Bus (untouched)
        ↓  CameraAutomationModule subscribes (P0, unchanged subscription)
        ↓  entity_id looked up against configured CameraProfiles (P0.5, new)
classify_state_change() (P0.5, new, pure, stateless)
        ↓  normalized CameraEvent (motion_detected/motion_cleared/
        ↓  human_detected/human_cleared/camera_online/camera_offline)
CameraAutomationModule's own shared dedupe/cooldown (P0, unchanged logic, now keyed by (camera_id, kind) for this path)
        ↓  publishes "camera_automation.camera_event"
Existing Luno Event Bus (untouched)
        ↓  AutomationEngine subscribes (existing "*" tap, untouched)
AutomationEngine → home_assistant.turn_on/off (existing, untouched)
```

P0's own flat-allowlist `camera_automation.state_changed` relay is UNCHANGED and coexists — see §5.

## 5. What's new vs. what's untouched inside `luno/camera_automation/`

- **`cameras.py` (new, entirely new file):** `CameraProfile`, `CameraEvent`, `build_entity_role_index()`, `classify_state_change()`, `load_camera_profiles()`. Pure/stateless translation logic plus one small, defensive JSON loader — no Event Bus, no Module lifecycle, no I/O beyond reading its own config file.
- **`config.py` (modified, additive only):** one new field, `cameras_path` (default `config/camera_automation.json`), one new env var `CAMERA_AUTOMATION_CAMERAS_PATH`. `entities`/`CAMERA_AUTOMATION_ENTITIES`/`enabled`/`cooldown_s` — every P0 field — byte-for-byte unchanged.
- **`module.py` (modified, additive only):** `_handle()` gained one new branch (classify against `CameraProfile` entities first) ahead of the EXISTING, unmodified flat-allowlist branch; a configured-but-unclassified-transition entity returns without falling through to "unknown" (a genuine correctness requirement, not new business logic layered onto old). Dedupe/cooldown is the SAME two dicts, SAME `_publish_if_not_suppressed` logic, now used by both branches via a `(camera_id, kind)` vs. `entity_id` key — never duplicated, per the brief's own Section 11/13. New `reload_cameras()` (mirrors `AutomationEngine.reload_rules()`), new `CAMERA_EVENT_TYPE` constant, `health()` now also reports configured camera count.
- **`__init__.py` (modified, additive only):** new exports only.

`luno/bootstrap/modules.py` was **not touched this sprint** — `CameraAutomationModule` is already constructed and registered there since P0; P0.5 only changes what happens inside that already-wired module and its own config file.

## 6. New standalone file: `ha_camera_discovery.py`

A strictly read-only discovery script (same precedent as Sprint 70's own `tapo_ptz_diagnostic.py`), run on the user's real machine to close the live-verification gap this sandbox cannot close: connects via the EXISTING `luno.ha_client.HomeAssistantClient` (read-only `get_states()`, zero service calls, zero `HA_TOKEN` printed), and prints every entity whose id/friendly_name matches a broad camera/motion/person keyword filter — output the user copies, by hand, into `config/camera_automation.json`. Never writes any file itself.

## 7. Entity mapping / event conversion rules

| HA state change | Role | Normalized `CameraEvent.kind` |
|---|---|---|
| motion entity `off→on` | motion | `motion_detected` |
| motion entity `on→off` | motion | `motion_cleared` |
| human entity `off→on` | human | `human_detected` |
| human entity `on→off` | human | `human_cleared` |
| dedicated availability entity `→on` | availability | `camera_online` |
| dedicated availability entity `→off`/unavailable | availability | `camera_offline` |
| any role entity `→unavailable`/`unknown`, no dedicated availability entity configured | motion/human/camera (fallback) | `camera_offline` |
| any role entity recovering from unavailable, no dedicated availability entity configured | motion/human (fallback) | the role's own `*_detected`/`*_cleared` (see §10 Known Limitations — NOT `camera_online`) |
| motion/human entity `→unavailable` WITH a dedicated availability entity configured | motion/human | none (deferred to the availability entity, avoids double-reporting) |
| unknown entity (not in any configured profile, not in the flat allowlist) | — | none — ignored, logged once at debug level |

Confidence is **always `None`** — `HomeAssistantAdapter.on_state_changed(entity_id, old_state, new_state)` (the existing, protected inbound interface) carries no attributes/confidence parameter at all; extracting one would require changing that protected signature, out of scope per the brief's own Section 1/17. Not invented, per Section 10's own instruction.

## 8. Tests

New file: `tests/test_p0_5_camera_integration.py` — 36 tests (Section A entity mapping, B event conversion including the motion-vs-offline distinction and the unavailable-fallback, C `load_camera_profiles` malformed-input safety, D metadata/stable-camera-id, E the module in isolation — new branch, shared dedupe/cooldown, unknown-entity logging, disabled-inert, malformed-config-file doesn't block `start()`, fail-safe exception isolation, F real-bootstrap E2E — HA event through the real adapter to a normalized `CameraEvent`, the full pipeline through to an existing `AutomationEngine` rule with zero engine changes, unknown entity never triggers automation, disabled remains fully inert, and the EXACT SAME `HomeAssistantAdapter` inbound/outbound assertions P0's own `test_16`/P0's predecessor `test_adapters.py::test_home_assistant_event` make, re-run here with the new classification branch actually configured and active). 36/36 passing. `tests/test_p0_camera_automation.py`'s own 23 tests re-run unmodified and still pass 23/23.

## 9. Regression

Full repository sweep after: 4569 collected (4533 + this sprint's own 36), 4538 passed, 31 failed. 30 of the 31 are the exact same pre-existing failures from the baseline in §2. The 31st, `tests/test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_plays`, was investigated rather than assumed pre-existing: re-run in isolation it passes 6/6, and it is the EXACT SAME test this project's own `docs/project_handover.json` has documented, by name, as a non-deterministic full-suite/parallel-timing flake dating to Sprint 49, recurring intermittently across many unrelated sprints since. `luno/camera_automation/` shares zero code, zero imports, zero subsystem with TTS/barge-in/LLM streaming. Not a regression from this sprint.

`tests/test_sprint68_mutation_audit_hardening.py::test_baseline_config_json_count_is_18` (renamed from `..._is_17`, forward-fixed for this sprint's own sanctioned new `config/camera_automation.json`, the exact same precedent Sprint 71/72 each already established once) re-verified passing.

## 10. Diff audit

| File | Why changed | Existing behavior affected | Regression test |
|---|---|---|---|
| `luno/camera_automation/config.py` | Add `cameras_path` field/env var for the new per-camera config file | None — new field only, all P0 fields/defaults unchanged | `tests/test_p0_camera_automation.py` (23/23 unmodified), P0.5 §A |
| `luno/camera_automation/module.py` | Add the classification branch + `reload_cameras()` | `_handle()`'s existing flat-allowlist branch is byte-for-byte unchanged and only reached when the entity is NOT in a `CameraProfile`; dedupe/cooldown dict keys now include a new key SHAPE (tuple) alongside the existing string keys — no collision possible | `tests/test_p0_camera_automation.py` (23/23), P0.5 §E/F |
| `luno/camera_automation/__init__.py` | Export the new names | None — additive only | import smoke-tested in every P0.5 test file |
| `tests/test_sprint68_mutation_audit_hardening.py` | This sprint's own sanctioned `config/camera_automation.json` moved the real config-file count from 17 to 18 | None outside this one assertion | re-verified passing |

No other existing file was modified. `luno/bootstrap/modules.py`, `luno/adapters/home_assistant.py`, `luno/automation/*`, and the Event Bus are untouched — confirmed by not appearing in this diff at all.

## 11. Known limitations

- The "recovery from unavailable" signal is asymmetric when no dedicated `availability_entity` is configured for a profile: going unavailable is correctly reported as `camera_offline` (a documented HA convention, not invented), but recovering FROM unavailable back to a normal motion/human state is classified as that role's own `motion_cleared`/`motion_detected`/etc., not as a `camera_online` — a deliberate simplification rather than adding cross-role "was this profile previously offline" tracking this sprint. Configuring a dedicated `availability_entity` (recommended) avoids this asymmetry entirely.
- Confidence is always `None` (see §7) — the existing, protected `HomeAssistantAdapter.on_state_changed()` signature has no attribute/confidence channel; adding one is a P0.5-brief-forbidden core modification, not attempted.
- `config/camera_automation.json` ships with a single `tapo_c212` entry, every field `null` — genuinely inert until an operator fills in real entity ids (via `ha_camera_discovery.py` on their real machine, or Home Assistant's own Developer Tools).
- No PTZ, snapshot intelligence, object recognition, person identification, auto-tracking, or LLM scene analysis was implemented — explicitly out of scope per Section 22 of the brief.
- Live Home Assistant verification was not performed — see §3.

## 12. Next sprint recommendation (not implemented)

Once `config/camera_automation.json` is filled in with real entity ids from `ha_camera_discovery.py`'s output, the next logical step is a small, isolated `config/automation_rules.json` rule (or a set of them) that actually consumes `camera_automation.camera_event` — e.g. "turn on the porch light when `human_detected` fires for `tapo_c212`" — which requires zero new code (the pipeline already supports it end to end, proven by `test_33` in this sprint's own suite). No further Luno source changes would be required for that first real automation; it is purely a configuration exercise.
