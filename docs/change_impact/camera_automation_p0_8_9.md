# LUNO P0.8.9 — Implement the Missing WLED OFF Automation Rule

## 1. Brief summary

The user asked, after P0.8.8's fix restored reliable WLED ON behavior:
"pas aku keluar kamera kenapa ngga otomatis mati?" (why doesn't it turn
off automatically when I leave the camera view?). Root cause was NOT a
bug: `config/automation_rules.json` simply never had an OFF rule for the
real `light.wled` entity. The only existing OFF rule
(`camera_test_automation_safety_action_off`) targets the mock
`light.test_camera_automation` entity, added in P0.8.2 purely to prove
the OFF-rule mechanism worked, and was never extended to the real light.

This sprint adds `camera_wled_human_cleared_off`: `light.wled` turns OFF
ten seconds after `human_cleared`, unless a fresh `human_confirmed`
arrives before that delay elapses, in which case the pending OFF is
cancelled and the light simply stays on.

## 2. Mechanism — no new scheduler/timer invented

`AutomationEngine` already reuses one project-wide `runtime.scheduler`
(`luno.core.scheduler.Scheduler`) for TIME triggers and cooldown cleanup
(`bind_scheduler()`, wired in `luno/bootstrap/modules.py`). That
scheduler already exposes exactly the two primitives a debounced,
cancellable delayed action needs: `schedule_once(name, fn, delay_s)` and
`cancel(job_id)`. Before this sprint, nothing in `AutomationEngine`'s own
action-dispatch path used them — every action dispatched synchronously,
inline, the moment its rule's conditions passed.

**Schema change** (`luno/automation/models.py`): one new optional action
parameter, `delay_seconds`, honored only for
`home_assistant.turn_on`/`turn_off` actions. `validate_action()` rejects
it on any other action type, rejects non-numeric/boolean/negative/
absurdly-large values (`MIN_DELAY_SECONDS`–`MAX_DELAY_SECONDS`, 0–300s),
and — critically — treats its absence exactly as before: every rule that
existed prior to this sprint has no `delay_seconds` key at all, so
`validate_action()`'s new checks are a guaranteed no-op for them.

**Dispatch change** (`luno/automation/engine.py`,
`_dispatch_home_assistant_action()`): if `delay_seconds > 0` and a
scheduler is bound, the action is scheduled via `Scheduler.schedule_once()`
rather than dispatched immediately; the returned `ActionResult` is marked
`status="completed", code="action_scheduled_delayed"` (an honest "the
schedule succeeded" signal, not "the device changed state" — see §7).
Genuine VERIFY/finalize (`_verify_and_finalize()`, unchanged) only runs
once the scheduled callback (`_fire()`) actually dispatches the real
`home_assistant.turn_off`, replacing the placeholder result with the
real one first.

**Cancellation — the key design decision.** Rather than couple the ON
and OFF rules by id, cancellation is keyed by the **target entity id**:
`_cancel_pending_delayed_action(target)` is called, unconditionally, at
the top of every `home_assistant.turn_on`/`turn_off` dispatch (immediate
or delayed), for every rule. It pops and cancels whatever delayed job (if
any) is currently pending for that same target, and honestly finalizes
the pre-empted execution as `SKIPPED`/`action_superseded` (never leaves
it silently `PENDING` forever, never claims it completed). This single,
generic rule produces both required behaviors with zero new coupling
between the two rules:

- A fresh `human_confirmed` → the existing ON rule's immediate `turn_on`
  for `light.wled` → cancels a pending OFF for `light.wled`.
- A repeated `human_cleared` while an OFF is already pending → the OFF
  rule's own new dispatch → cancels its own prior pending job and
  reschedules a fresh 10s window (never double-fires, never needs a
  separate "already pending" check).

For every action that never sets `delay_seconds` (i.e. every rule this
project had before P0.8.9), `_pending_delayed_actions` never has an entry
for that rule's target, so `_cancel_pending_delayed_action()` is a
guaranteed no-op — the entire change is dormant, byte-for-byte behavior-
preserving, for every pre-existing rule.

**Fallback when no scheduler is bound** (e.g. a bare/unit-test
construction of `AutomationEngine` that never calls `bind_scheduler()`):
dispatches immediately instead, logging the fact — fail-open (a device
that never turns off because a delay was silently dropped is worse than
one that turns off slightly early), same documented-degraded-mode
precedent `bind_scheduler()`'s own docstring already establishes for TIME
triggers. Production always binds the scheduler
(`luno/bootstrap/modules.py`), so this path is a defensive fallback only.

## 3. Files changed

- `luno/automation/models.py` — `MAX_DELAY_SECONDS`/`MIN_DELAY_SECONDS`
  constants; `validate_action()` gains the `delay_seconds` checks
  described above. No change to any existing dataclass field or to any
  other validation branch.
- `luno/automation/engine.py` — `_pending_delayed_actions` instance dict;
  `stop()` cancels any pending jobs (never leaves one orphaned, same
  discipline as `_time_job_ids`); `_run_execution()` defers
  `_verify_and_finalize()` when an action was scheduled rather than
  dispatched; `_dispatch_home_assistant_action()` gains the cancel-then-
  maybe-schedule logic; two new private methods,
  `_cancel_pending_delayed_action()` and `_schedule_delayed_ha_action()`;
  one new module-level helper, `_coerce_delay_seconds()`.
- `config/automation_rules.json` — one new rule,
  `camera_wled_human_cleared_off` (target `light.wled`, `delay_seconds:
  10.0`, trigger/conditions mirroring the existing mock-entity OFF rule
  exactly). The two pre-existing rules this sprint must not regress
  (`camera_human_detected_test_action`, `camera_test_automation_safety_
  action_off`) are untouched — see §7/§8 for the direct proof.

No other file was modified. YOLO/RTSP/Tapo/WLED/HA integration code, HA
credentials, and the P0.8.8 dedupe fix were not touched.

## 4. The new automation rule

```json
"camera_wled_human_cleared_off": {
  "name": "Camera human CLEARED -> RGB Strip light OFF, 10s debounce (P0.8.9 - real light.wled entity; cancelled if human is re-confirmed before the delay elapses)",
  "enabled": true,
  "trigger": "event:camera_automation.camera_event",
  "conditions": [
    {"type": "equals", "target": "event.kind", "value": "human_cleared"},
    {"type": "equals", "target": "event.available", "value": true},
    {"type": "equals", "target": "event.detection_error", "value": null}
  ],
  "actions": [
    {"type": "home_assistant.turn_off", "parameters": {"target": "light.wled", "delay_seconds": 10.0}}
  ],
  "cooldown_seconds": 0.0
}
```

`cooldown_seconds` is intentionally `0.0` — the debounce comes entirely
from the delayed-action/cancellation mechanism (§2), not from the rule's
own cooldown; the rule must be free to re-evaluate on every
`human_cleared` so a repeat can reset the pending job's window.

## 5. Delay duration

10.0 seconds, exactly as specified. Enforced by the schema
(`MIN_DELAY_SECONDS=0.0`, `MAX_DELAY_SECONDS=300.0`) and proven present
in the real config file directly (`test_B2_new_off_rule_uses_a_10_
second_delay`, loaded via `AutomationEngine`'s own real disk-loading
path in `test_B4`, not just `json.load`).

## 6. How pending OFF actions are cancelled/superseded

See §2's "Cancellation" paragraph. Concretely: `_pending_delayed_
actions: Dict[str, Tuple[job_id, execution, action_type]]`, keyed by
target entity id. Every Home Assistant action dispatch — immediate or
delayed, for ANY rule — calls `_cancel_pending_delayed_action(target)`
first. If an entry exists for that target, its scheduler job is
cancelled, and its `AutomationExecution` is finalized as
`SKIPPED`/`action_superseded` (observable via the real
`automation.skipped` event, asserted directly in
`test_C5_human_confirmed_during_pending_off_cancels_it`).

## 7. Honesty discipline — what this rule proves and does not prove

Following the same A–F staged framework this project has used since
P0.8.7/P0.8.8:

- **A/B (person/event → camera event published):** unchanged, P0.8.8's
  own fix.
- **C (AutomationEngine received the event):** proven — the OFF rule's
  conditions are evaluated synchronously at `human_cleared` arrival, and
  `automation.triggered`/`automation.condition_passed` fire immediately
  (`_run_actions` still runs at trigger time; only the FINAL Home
  Assistant dispatch is delayed).
- **D (HA turn_off command sent):** proven for the MOCK backend only —
  every test in this sprint's suite uses `MockHomeAssistantHandler`
  exclusively (asserted via `isinstance` check in the shared `_Harness`
  helper), consistent with every P0.8.x end-to-end suite before it. No
  test in this sprint issued a real Home Assistant API call.
  `test_D1`/`test_D2` prove the exact tool-call shape
  (`tool="home_assistant", action="turn_off", target="light.wled"`) that
  would be sent, and that it is sent at the right time (not immediately,
  not never) — via the real `VisionCameraEventBridge`, not a mocked
  helper, satisfying the brief's own "production call path" requirement.
- **E (HA fresh state verification):** out of this sprint's scope —
  unmodified from P0.8.7. The delayed dispatch reuses the exact same
  `_dispatch_tool_call()`/`ToolManagerBridgeModule`/`ToolManager` round
  trip every other action in this engine already uses, so P0.8.7's
  verification-freshness fix applies unchanged once a real
  `RealHomeAssistantHandler` is wired.
- **F (physical WLED illumination):** **NOT evidenced, and not
  claimed.** As in every prior P0.8.x change-impact doc, Luno has no
  optical/electrical sensing channel for any HA-controlled device. This
  sprint does not change that. Do not read any test in this suite as
  proof the physical light actually turned off ten seconds after the
  user left the camera's view — it proves Luno correctly *decided to*,
  and correctly *asked Home Assistant to*.

## 8. New regression test suite

`tests/test_p0_8_9_wled_off_debounce.py` — 25 tests (all passing):

- **Section A (8 tests)** — `delay_seconds` schema validation: valid
  value, zero, absent (backward compatibility), negative, too large,
  non-numeric, boolean, and rejected on a non-HA action type.
- **Section B (7 tests)** — real `config/automation_rules.json` sanity:
  the new rule exists and targets `light.wled` (not the mock entity),
  uses a 10.0s delay, triggers on `human_cleared`, loads cleanly through
  `AutomationEngine`'s own real disk-loading path, the two pre-existing
  rules (`camera_human_detected_test_action`,
  `camera_test_automation_safety_action_off`) are byte-for-byte
  unchanged, and the file has exactly the expected six rules (guards
  against an accidental duplicate/rename).
- **Section C (8 tests)** — end-to-end engine/scheduler behavior, real
  bootstrap (`register_all_modules`/`register_all_adapters`), the REAL
  `runtime.scheduler` (never a fake/simulated clock), MOCK HA backend
  only: `human_confirmed` → ON; `human_cleared` → no immediate OFF; WLED
  stays ON through most of the delay window; OFF fires once the delay
  elapses (with `automation.completed` for the OFF rule); a
  re-confirmation during the pending window cancels the OFF (and the
  cancellation is independently observable via `automation.skipped`/
  `action_superseded`); a genuine OFF followed by a later confirmation
  turns WLED on again; a repeated `human_cleared` resets the pending
  job's window without duplicating the OFF command (kept to two repeats,
  intentionally, to stay below this engine's own pre-existing, unrelated
  loop/cycle protection — see the test's own docstring); and a pending
  OFF for one target entity is unaffected by an unrelated dispatch for a
  different entity.
- **Section D (2 tests)** — the real production call path:
  `VisionCameraEventBridge._on_person_confirmed()`/`_on_person_left()`
  (not a mocked helper) driving the full ON → pending OFF → genuine OFF
  sequence, and the same path proving a real re-confirmation cancels a
  pending OFF.

## 9. Regression sweep results

**Focused suite** (this sprint's 25 new tests + every camera_automation/
Vision/AutomationEngine-adjacent suite): 460 passed, 1 skipped
(pre-existing), 3 failed — all three (`test_p0_camera_automation.py::
test_15_...`, `test_p0_5_camera_integration.py::test_35_...`,
`test_p0_5_3_vision_camera_bridge.py::test_22_...`) independently
confirmed to be a real `.env` condition unrelated to any code in this
sprint: `.env` now has `CAMERA_AUTOMATION_ENABLED=true` (previously
`False`, per `regression_baseline.md`'s own P0.6.1-era note) — these
three tests assert "fresh checkout, camera_automation disabled by
default." Re-running the exact same three tests with
`CAMERA_AUTOMATION_ENABLED=false` explicitly overridden reproduces a
clean 3/3 pass, proving this is purely an `.env` value, not a code
regression from this sprint (nothing in this sprint reads or writes that
variable). This is very likely a deliberate, persistent change from the
user's own recent live-troubleshooting session (making camera_automation
survive a real process restart) — see §11.

**Full repository sweep** (154 files, 8 parallel chunks, `-n 4
--timeout=90`): 4,325 passed, 1 skipped, 42 failed. Every failure traced
to an already-long-documented pre-existing category in
`docs/testing/regression_baseline.md` (LLM `max_completion_tokens`
`.env` override, `test_mic_device_index.py`/`list_microphones.py`
sandbox-has-no-audio-hardware, `test_real_adapters.py` whisper
`_device_index` construction gap, `test_production_launcher.py::test_
07`, `config/backups/`-accumulation forensic drift across
`test_sprint63/64/68`, `test_sprint60_area_schema.py`'s real
`light.main_light` config drift) plus the three `CAMERA_AUTOMATION_
ENABLED` failures above (newly explained, not newly caused). **Zero
failures touch `luno/automation/`, `luno/camera_automation/`, or
`luno/tool_manager/builtin/home_assistant.py`** beyond the three
already-explained `.env`-driven ones.

## 10. Result classification: STRONG

The missing capability the user asked for is now implemented, using the
project's own existing scheduler primitive rather than a new one,
additively (every pre-existing rule/test is provably byte-for-byte
unaffected), with full regression coverage including a genuine
production-call-path proof and a clean full-repository sweep. Stage F
(physical illumination) is explicitly not claimed — see §7.

## 11. Known limitations / notes for the next sprint

- `.env`'s `CAMERA_AUTOMATION_ENABLED=true` (see §9) should be
  double-checked with the user — if this was set ad hoc in a shell
  rather than persisted to `.env` on every machine/launch path they use,
  the same "silently disabled after a differently-launched restart" risk
  flagged during this sprint's own preceding conversation still applies.
  This sprint did not modify `.env` (out of scope, explicitly forbidden
  by the brief) and only confirms its current value.
- The delayed-action mechanism is intentionally general (keyed by target
  entity, not hardcoded to `light.wled`) — a future sprint could reuse
  `delay_seconds` for any other ON/OFF light pair without further engine
  changes, only a new rule in `config/automation_rules.json`.
- As in every P0.8.x sprint, this fix cannot and does not confirm
  physical device state — only that Luno correctly decides when to ask
  Home Assistant to turn `light.wled` off, and correctly cancels that
  decision when the person returns in time.
