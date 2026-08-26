# LUNO P0 — Camera Automation / Safe Integration & Non-Regression Protocol

**Date:** 2026-08-20
**Status:** COMPLETE
**Scope:** new `luno/camera_automation/` package (`__init__.py`, `config.py`, `module.py`), new `tests/test_p0_camera_automation.py`, additive-only changes to `luno/bootstrap/modules.py`.
**Explicitly NOT touched:** the Event Bus (`luno/core/event_bus.py`), the scheduler (`luno/core/scheduler.py`), `HomeAssistantAdapter`/`HomeAssistantSource`/`HomeAssistantClient` (`luno/adapters/home_assistant.py`), `AutomationEngine`/`conditions.py`/`models.py` (`luno/automation/`), `CameraPatrolModule` (`luno/camera_patrol/`), `ToolManagerBridgeModule`, any `ToolManager` handler including `luno/tool_manager/builtin/home_assistant.py`, any memory/persistence system, the LLM/voice/TTS/STT/wake-word pipeline, the dashboard, the planner/NLP parser, `luno/bootstrap/launcher_config.py`, and every `config/*.json` file — zero lines changed in any of these, confirmed by re-running their own dedicated test suites unmodified.

## 1. Goal and governing constraint

This sprint's own brief was explicitly a non-regression protocol, not a feature checklist: treat the entire existing codebase as protected infrastructure, and for every existing file touched, answer why it had to change, what behavior depends on it, and what test proves that behavior is unchanged. The feature itself — letting an allowlisted set of Home Assistant camera/motion entities feed Sprint 72's automation engine — was secondary to proving it could be added without moving anything already working.

## 2. Architecture — reuses the existing Event Bus and the existing automation engine, verbatim

`HomeAssistantAdapter.on_state_changed()` (`luno/adapters/home_assistant.py`, untouched) already and unconditionally publishes a `device_state_changed` event (`data={"entity_id", "old_state", "new_state"}`) onto the existing Event Bus for every entity, for both the mock and real HA backends — this was true before this sprint and required zero changes to make it so.

`CameraAutomationModule` (`luno/camera_automation/module.py`) is a `Module`, the same interface `CameraPatrolModule`/`AutomationEngine` already implement, bound to the SAME `event_bus` every other module uses. It adds itself as one more subscriber to the already-existing, already-multi-consumer `device_state_changed` event (Event Bus subscriptions are natively fan-out — subscribing a second time changes nothing about the first subscriber's behavior). After filtering against an operator-configured allowlist, deduping no-op re-fires, and applying a per-entity cooldown, it republishes a new, distinctly-namespaced `camera_automation.state_changed` event onto the SAME Event Bus.

Sprint 72's `AutomationEngine` already supports triggering a rule off an arbitrary event-name string (`{"trigger": {"type": "event", "parameters": {"event_name": "camera_automation.state_changed"}}}` in `config/automation_rules.json`) and already has `home_assistant.turn_on`/`home_assistant.turn_off`/`camera.*` actions allowlisted. The result: the full `TRIGGER → CONDITION → ACTION → VERIFY → COOLDOWN` pipeline for a camera-triggered automation works end to end from a plain JSON rule, with zero lines changed in `AutomationEngine`. This is proven, not just asserted, by `tests/test_p0_camera_automation.py::test_18_camera_automation_event_triggers_existing_automation_engine_rule_e2e`.

Final flow:

```
Camera / motion sensor (Home Assistant)
        ↓
HomeAssistantAdapter.on_state_changed()   [existing, untouched]
        ↓  publishes "device_state_changed"
Existing Luno Event Bus                    [existing, untouched]
        ↓  CameraAutomationModule subscribes (new)
CameraAutomationModule                     [new — allowlist, dedupe, cooldown]
        ↓  publishes "camera_automation.state_changed"
Existing Luno Event Bus                    [existing, untouched]
        ↓  AutomationEngine subscribes (existing "*" tap, untouched)
AutomationEngine                           [existing, untouched]
        ↓  home_assistant.turn_on/off action (existing, untouched)
tool_requested → ToolManagerBridgeModule → ToolManager → home_assistant handler [existing, untouched]
```

## 3. Change boundary decision (Section 4 of the brief)

Category A (new isolated code) was sufficient for the entire feature. No Category B adapter/wrapper was needed beyond the module itself, because the existing `device_state_changed` event already carries everything a camera-domain consumer needs (`entity_id`/`old_state`/`new_state`) — there was no translation gap to bridge. Category C (existing core modification) was never invoked: the one existing file touched, `luno/bootstrap/modules.py`, is the established module-wiring entry point every prior sprint (Camera Patrol, Automation Engine Dasar) has also used for exactly this purpose, and every edit to it is a pure addition (new import line, new construction block, one new entry in each of two existing loops, one new dict key) — no existing line was altered, reordered, or removed.

## 4. Feature flag & fail-safe (Sections 10/11 of the brief)

`CameraAutomationConfig.enabled` defaults to `False` (read from `CAMERA_AUTOMATION_ENABLED`, unset by default) — a fresh checkout behaves exactly as before this package existed. When disabled, `start()` does not even call `event_bus.subscribe()` — verified directly by `test_15_disabled_by_default_real_bootstrap_no_subscription_footprint`, which asserts `_bus_sub_id is None` after a real `runtime.start()`. When enabled but the entity allowlist (`CAMERA_AUTOMATION_ENTITIES`) is empty — also the default — every `device_state_changed` event is filtered out immediately.

Every line of the Event Bus callback path (`_on_device_state_changed`) runs inside a `try/except` that logs and swallows any exception rather than re-raising it into the Event Bus's own dispatch loop — verified by `test_12_fail_safe_exception_in_handler_is_isolated`, which forces the internal handler to raise and confirms nothing propagates. This is intentionally redundant with the Event Bus's own existing subscriber self-healing (a subscriber that raises repeatedly is automatically marked degraded and backed off, never unsubscribed, never crashes the process) — not a replacement for it, proof that this module cannot bring down the Event Bus, LLM, TTS, STT, memory, existing automations, or existing HA controls even in a total internal failure.

## 5. Memory, voice, and dependencies (Sections 8/9/12 of the brief)

No memory system was touched. `_last_state`/`_cooldown_until` are small, bounded, purely in-memory dicts (at most one entry per allowlisted entity_id) — nothing is written to disk, and camera events remain ephemeral, matching the brief's own default for P0.

No voice/LLM/TTS/STT/wake-word code was touched, and camera events are never forced into the voice pipeline.

No new external dependency was introduced. `time.monotonic()` (standard library) covers the cooldown; nothing else was needed.

## 6. Data compatibility (Section 14 of the brief)

Zero new `config/*.json` files. The allowlist is env-var-only (`CAMERA_AUTOMATION_ENTITIES`, comma-separated), a deliberate choice: Sprint 72's own `automation_rules.json` addition required a forward-fix to `tests/test_sprint68_mutation_audit_hardening.py`'s hardcoded config-file-count test; this sprint avoids that class of coupling entirely by not adding a config file at all. Re-verified: that test still passes at the same count Sprint 72 last set.

## 7. Tests

New file: `tests/test_p0_camera_automation.py` — 23 tests.

- **A. Config (pure, no bootstrap):** default values, `from_env()` defaults, `from_env()` parsing (enabled/entities/cooldown), falsy-string variants for `enabled`.
- **B. Module in isolation (fake event bus, no bootstrap):** disabled-by-default zero subscription; enabled subscribes only to `device_state_changed`; non-allowlisted entity ignored; allowlisted entity publishes `camera_automation.state_changed` with the correct payload; dedupe suppresses an identical repeat state; cooldown suppresses a rapid second change; a missing `entity_id` does not raise; a forced internal exception is isolated (§11); `stop()` unsubscribes cleanly even against a no-op fake; `health()` reports enabled state and allowlist size.
- **C. Real bootstrap, E2E:** disabled-by-default has genuinely zero Event Bus footprint after `runtime.start()`; the EXISTING `HomeAssistantAdapter`'s inbound (`device_state_changed`/`automation_triggered`) and outbound (`tool_requested` → `call_service`) behavior is byte-for-byte unaffected by this module's presence — the same assertions `luno/adapters/tests/test_adapters.py::test_home_assistant_event` makes, run again here with the new module registered and enabled alongside it; an allowlisted entity's real state change (via `MockHomeAssistantSource.simulate_state_change()`) publishes exactly one `camera_automation.state_changed` event, while a non-allowlisted entity on the same real adapter produces none; and the full `device_state_changed → camera_automation.state_changed → AutomationEngine rule → home_assistant.turn_on → MockHomeAssistantHandler` pipeline fires end to end from a plain JSON rule with zero engine code changes.

23/23 passing.

## 8. Regression

**Baseline (before any change), full repository sweep, same 8-chunk methodology every prior sprint's baseline uses:** 4510 tests collected (`tests/` + `luno/`, same 2 pre-existing uncollectible files as every prior sprint — `test_main_bargein.py`/`test_root_main_bargein.py`, a sandbox-session-path artifact, not a code defect). 4480 passed, 30 failed, 0 skipped.

| Category | Files | Count | Cause |
|---|---|---|---|
| environment gap | `test_llm_max_completion_tokens_compatibility.py` | 7 | pre-existing OpenRouter/OpenAI `max_tokens`-param mock assertion, self-contained, zero camera/HA relation |
| environment gap | `test_mic_device_index.py` | 11 | sandbox has no `list_microphones.py` at repo root and no audio hardware |
| environment gap | `test_production_launcher.py::test_07_...`, `test_real_adapters.py` (2) | 3 | real-whisper attribute / this sandbox's outbound HTTPS proxy returns 403 for `api.openai.com` |
| environment gap | `test_sprint63_long_term_memory_recovery.py` (2), `test_sprint64_memory_corruption_forensics.py` (3) | 5 | `config/backups/` has grown past Sprint 72's own "43 before, 43 after" snapshot from real work in this persistent folder since then |
| environment gap | `test_sprint68_mutation_audit_hardening.py` | 2 | same `config/backups/` accumulation |
| full-suite-only timing flake | `luno/barge_in/tests/test_barge_in.py` | 2 | fails under full-suite parallel load only, 2/2 pass standalone |

Zero of these 30 relate to camera, Home Assistant, the Event Bus, or the automation engine.

**Targeted, spot-verified individually before writing the mandated final report:** `test_sprint71_camera_patrol.py`, `test_sprint71_dashboard_startup_recovery.py`, `test_sprint72_automation_engine.py`, `luno/adapters/tests/test_adapters.py`-equivalent assertions (via this sprint's own `test_16`), and every existing test file that calls `register_all_modules` — the one function this sprint's single existing-file edit lives in — `test_dashboard.py`, `test_production_launcher.py`, `test_proactive.py`, `test_state_isolation.py`, `test_conversation_ended_lifecycle_routing.py`, `test_memory_dashboard.py`, `test_routing_dashboard.py`, `test_llm_dashboard.py`. All pass, zero new failures.

**Full sweep (after), re-run with the identical chunk boundaries as the baseline:** 4533 tests collected (4510 + this sprint's own 23 new tests). 4503 passed, 30 failed, 0 skipped — the exact same 30 tests, same assertions, same root causes as the baseline table above. Zero new failures. Zero previously-failing tests newly passing (no incidental fix claimed).

## 9. Persistent state

Zero new `config/*.json` files. `tests/test_sprint68_mutation_audit_hardening.py`'s config-file-count test re-verified passing at the same count Sprint 72 last set (this sprint's env-var-only allowlist design never needed a config file). No existing config file was opened for writing by any code this sprint added.

## 10. Diff review (Section 16 of the brief)

Every modified existing file, answered directly:

- **`luno/bootstrap/modules.py`** — why changed: this is the established, project-wide extension point every prior sprint (Camera Patrol, Automation Engine Dasar) has used to construct and register a new `Module` instance; there is no alternative wiring location that doesn't require touching this file, since it is the single place `runtime.register_module()`/`bind_event_bus()` calls are made for every subsystem. What behavior could this affect: none of the four edits (one new import line, one new construction block with a comment, one new tuple entry in the existing `bind_event_bus` loop plus one new `runtime.register_module()` call, one new returned-dict key) removes, reorders, or alters any existing line, argument, or call — they are pure insertions. What test protects existing behavior: every test file that calls `register_all_modules()` (listed in §8 above) was spot-verified to pass unchanged before and after this edit; the config/dict-shape is additive so no existing caller reading `modules["..."]` by key is affected.

No other existing file was modified. The diff is exactly: 4 new files, 1 modified file (4 additive edits).

## 11. Known limitations

- The camera-relevant entity allowlist is manually operator-configured (`CAMERA_AUTOMATION_ENTITIES`) — no automatic inference from entity_id naming (e.g. treating every `binary_sensor.*motion*` as camera-relevant) was built. This is a deliberate P0 scope boundary: guessing risks silently reacting to an unrelated sensor, which the brief's own §10/§11 fail-safe posture argues against.
- Deduplication compares only the immediately previous `new_state` per entity (a single-slot memory, not a windowed history) — a rapid A→B→A oscillation outside the cooldown window will publish twice, once for each genuine transition. This matches the brief's own P0 scope ("ephemeral... unless persistence already exists for equivalent events") rather than building a new bounded-history mechanism.
- No dashboard panel was added for this module (not required by this brief, unlike Sprint 72's own explicit dashboard requirement) — keeping the diff as small as the brief's own Final Principle demands. `CameraAutomationModule.health()` is implemented and would support one if added later, following Sprint 72's own `collect_automation()` precedent, without any change to this sprint's own files.
- Live Home Assistant hardware verification was not performed — this sandbox has no route to the user's LAN, the same structural limitation every prior camera/HA sprint has documented. Every test in this sprint dispatches through the real `device_state_changed`/`camera_automation.state_changed`/`tool_requested`/`ToolManagerBridgeModule`/`ToolManager` round trip via `MockHomeAssistantSource.simulate_state_change()`; only the final hardware hop uses the existing mock `home_assistant` handler.
