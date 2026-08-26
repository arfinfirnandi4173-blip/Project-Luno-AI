# LUNO P0.11 — Action Sequence Engine

## 1. Root cause / design decision

P0.11's brief asked `AutomationEngine` to support "Trigger -> Conditions
-> Action 1 -> Action 2 -> Action 3 -> Complete", with each action
finishing before the next begins, optional inter-action delays, and
strict stop-on-first-failure semantics.

Inspection of the existing engine (`luno/automation/engine.py`,
`luno/automation/models.py`) found this was **already partially true**:
`AutomationRule.actions` was already a `List[AutomationAction]`, and
`_run_actions()` already iterated it sequentially on a dedicated
per-execution `threading.Thread` (spawned once per `_trigger()` call).
What was genuinely missing: (1) stop-on-first-failure — the legacy path
always runs every action regardless of individual failure, then
classifies the whole execution as `COMPLETED`/`PARTIAL_FAILURE`/`FAILED`
by counting; and (2) any mechanism to pause between actions without
blocking the rest of the engine.

Rather than retrofit new semantics onto the legacy `actions` path (which
would risk changing behavior for every existing rule), P0.11 adds a new,
purely additive `sequence` field on `AutomationRule`, mutually exclusive
with `actions`. A rule defines exactly one of the two. `sequence`
reuses `AutomationAction`'s own `{type, parameters}` shape verbatim for
every existing device-action type, plus one new pseudo-type,
`{"type": "delay", "seconds": N}`. This satisfies the brief's own
"prefer additive schema evolution" and "if a better-established
convention already exists, follow that" instructions — no second action
schema was invented.

The delay step blocks only the calling execution's own thread
(`threading.Event().wait(seconds)`), never the AutomationEngine itself.
Because Sprint 72 already spawns one dedicated thread per execution, this
falls out "for free" — no new threading primitive was needed to keep
concurrent automations independent of each other.

## 2. Files changed

- `luno/automation/models.py` — added `sequence: List[AutomationAction]`
  to `AutomationRule` (default `[]`); added `current_step_index` /
  `total_steps` (both `Optional[int]`, default `None`) to
  `AutomationExecution`; added `SEQUENCE_STEP_TYPES`,
  `MAX_SEQUENCE_STEPS` constants; added `validate_sequence_step()`;
  rewrote the action-validation section of `validate_rule()` to enforce
  mutual exclusivity between `actions` and `sequence`; added
  `_sequence_step_from_raw()` and wired `rule_from_dict()` to build
  `sequence` from the raw dict's `"sequence"` key; extended
  `to_public_dict()` on both dataclasses.
- `luno/automation/engine.py` — added `_run_sequence()`,
  `_run_delay_step()`, `_run_action_step()`, `_wait_delay()`,
  `_verify_and_finalize_sequence()`, `_execution_duration_s()`, and the
  module-level `_coerce_sequence_delay_seconds()` helper; `_run_execution
  ()` now branches to `_run_sequence()` when `rule.sequence` is non-empty,
  otherwise falls through to the unchanged `_run_actions()` call;
  `_set_enabled()` and `_rule_to_storage_dict()` were extended to carry
  the `sequence` field through the one existing persistence write path.
- `tests/test_p0_11_action_sequence.py` (new) — 52 tests, sections
  A (Schema) through M (Architecture guards).

Nothing in `luno/vision.py`, `luno/vision_occupancy.py`,
`luno/adapters/vision.py`, `luno/camera_automation/`,
`config/automation_rules.json`, or the legacy `_run_actions()` /
`_verify_and_finalize()` / `_dispatch_action()` / `_dispatch_tool_call()`
methods were opened or modified.

## 3. Sequence schema

```json
{
  "my_rule": {
    "name": "my_rule",
    "trigger": "manual",
    "sequence": [
      {"type": "home_assistant.turn_on", "parameters": {"target": "Main Lamp"}},
      {"type": "delay", "seconds": 2},
      {"type": "home_assistant.turn_off", "parameters": {"target": "Main Lamp"}}
    ],
    "cooldown_seconds": 0.0
  }
}
```

A rule must define exactly one of `actions` (legacy, non-empty) or
`sequence` (new, non-empty) — both or neither is a validation error at
`validate_rule()` time. Each device-action step is validated by the
SAME `validate_action()` function the legacy path already uses (no
parallel validation logic). `delay` steps require a finite,
non-negative `seconds` value within the existing
`MIN_DELAY_SECONDS`/`MAX_DELAY_SECONDS` bounds (the same bounds P0.8.9's
own `delay_seconds` HA-action parameter uses) and no extra parameters.
A device-action step may not carry `delay_seconds` — that parameter
belongs only to P0.8.9's separate, async, single-action HA scheduling
mechanism (see §6 below); mixing the two would blur the sequential
guarantee. Sequence length is capped at `MAX_SEQUENCE_STEPS` (== the
existing `MAX_ACTIONS_PER_RULE`).

## 4. Execution behavior

`_run_sequence()` iterates `rule.sequence` in order on the execution's
own dedicated thread. For each step it publishes
`automation.step_started`, runs the step (delay or device action),
publishes `automation.step_completed`/`automation.step_failed`, and
appends an `ActionResult` to `execution.action_results`. `execution.
current_step_index` and `execution.total_steps` are updated live, so
`get_automation_status()` can observe an in-progress, multi-second
sequence's real position (proven in tests F1/F4). A step never starts
until the previous step's `ActionResult` has been recorded — proven via
event-timestamp ordering in tests C1/C2 (52-test suite, section C).

## 5. Failure behavior

The sequence loop stops at the first step whose result is not
`"completed"` — subsequent steps never dispatch (test E1: with a
turn_on/fail/turn_on sequence, exactly 2 tool calls occur, never 3).
`_verify_and_finalize_sequence()` is binary: `COMPLETED` only if every
result that ran is `"completed"`, otherwise `FAILED` — there is no
`PARTIAL_FAILURE` concept for a sequence, since execution halts early
(this differs deliberately from the legacy `actions` path's own
run-everything-then-classify policy, which is completely unmodified).
`execution.reason` identifies the failing step index, its action type,
and the original error text (test E3). No exception is swallowed.

## 6. Delay behavior

`{"type": "delay", "seconds": N}` is a NEW, separate mechanism from
P0.8.9's existing `delay_seconds` parameter on `home_assistant.turn_on`/
`turn_off` actions. The two are deliberately kept apart:

- P0.8.9's `delay_seconds` — async, `Scheduler.schedule_once()`-based,
  returns immediately with `code="action_scheduled_delayed"`, defers a
  SINGLE action's own dispatch.
- P0.11's `delay` step — synchronous, blocks the CURRENT execution's own
  thread via `threading.Event().wait(seconds)`, pausing the sequence
  itself before the next step runs. Never a busy loop, never
  `time.sleep()` (kept as `Event().wait()` specifically so a future
  cancellation token could `.set()` it early — P0.11 does not build that
  token itself, see §13).

A delay step produces its own `ActionResult` (`type="delay"`,
`code="delay_completed"`) — it is never represented as a fake device
action, and its own log line always says `type=delay`, distinct from
device-action log lines (brief Section 9/10).

## 7. ToolManager integration

Every device-action sequence step is dispatched through
`self._dispatch_action()` — the EXACT SAME method `_run_actions()`
already calls for the legacy path. No second dispatch method was added
(structurally proven by test H2, an AST walk over every method whose
name contains "dispatch"). `_dispatch_action()` still routes through
`_dispatch_tool_call()` -> `tool_requested` event -> `ToolManager` ->
the real handler (mocked in tests), unchanged. Test H1 subscribes to
`tool_requested` directly and confirms every non-delay sequence step
(and only those steps) publishes one.

## 8. Backward compatibility

- The real, shipped `config/automation_rules.json` (none of whose rules
  use `sequence`) loads and validates cleanly under the new, stricter
  `validate_rule()` (test I3).
- A legacy single-action rule still dispatches exactly once and
  completes (test I1).
- A legacy multi-action rule's existing `PARTIAL_FAILURE` classification
  (both actions dispatch regardless of the first one's outcome) is
  unchanged (test I2) — this is the one area P0.11 was explicitly told
  NOT to alter.
- A legacy execution's `current_step_index`/`total_steps` stay `None`
  (test F5) — the new fields are additive and never populated for the
  path that doesn't use them.
- `enable_automation()`/`disable_automation()` persist the `sequence`
  field through the one existing write path (test I4) — without this,
  toggling a sequence-based rule's enabled state would silently drop its
  sequence on the next write.

## 9. Tests added and results

`tests/test_p0_11_action_sequence.py` — 52 tests, sections A (Schema,
14), B (Device actions, 2), C (Ordering, 2), D (Delay, 6), E (Failure,
4), F (Execution state, 5), G (Logging, 3), H (ToolManager, 2),
I (Backward compatibility, 4), J (Concurrent execution, 1),
K (Cancellation — documents the honest absence, 2), M (Architecture
guards, 7). All 52 passing.

`tests/test_sprint72_automation_engine.py` (the pre-existing 78-test
AutomationEngine suite) — all 78 still passing, zero regression.

## 10. Regression results

Full 157-file repository sweep (8-chunk methodology): **4,454 passed, 2
skipped, 43 failed.** Every failure individually traced to an
already-documented pre-existing category in `docs/testing/
regression_baseline.md` — none new, none touching `luno/automation/`,
`luno/vision.py`, `luno/vision_occupancy.py`, or `luno/camera_
automation/`. See §11 below for the breakdown.

Targeted spot-check (before the full sweep): `test_sprint72_automation_
engine.py` + `test_p0_11_action_sequence.py` together — 130/130 passed.
ToolManager (`luno/tool_manager/tests/test_tool_manager.py`,
`test_camera_ptz.py`), all camera-automation suites (P0/P0.5.x/P0.6.x/
P0.7/P0.8.x/P0.9/P0.10), and all Vision suites — 650 passed, 2 failed
(both the already-documented `CAMERA_AUTOMATION_ENABLED=true` `.env`
drift, confirmed by re-running with `CAMERA_AUTOMATION_ENABLED=false`
override — both pass clean).

## 11. Known baseline failures (unchanged categories, re-encountered)

- `test_llm_max_completion_tokens_compatibility.py` (7) +
  `test_memory_session_summary_api_compatibility.py` (5) — `.env`'s
  `MAX_TOKENS_PARAM=max_tokens` override.
- `test_mic_device_index.py` (6) — no-audio-hardware / `.env` gap.
- `test_real_adapters.py` (2) — `RealWhisperSource._device_index`
  construction gap.
- `test_production_launcher.py::test_07_...` (1) — real credentials
  configured in this checkout's `.env`.
- `test_sprint60_area_schema.py` (2) — real `config/lights.config.json`
  `light.main_light` drift.
- `test_sprint63_long_term_memory_recovery.py` /
  `test_sprint64_memory_corruption_forensics.py` /
  `test_sprint68_mutation_audit_hardening.py` (17 total) — accumulated
  `config/backups/` forensic drift on this live-synced checkout.
- `test_sprint66_tool_boundary_hardening.py::test_performance_validate_
  download_directory_is_fast` (1) — documented timing-sensitive test.
- `test_p0_camera_automation.py::test_15_...` /
  `test_p0_5_3_vision_camera_bridge.py::test_22_...` (2) — the
  P0.8.9-documented `CAMERA_AUTOMATION_ENABLED=true` `.env` condition.

Total: 43, matching the chunked-sweep count exactly.

## 12. Physical-device verification status

**Not exercised.** Every test in this sprint (and every prior P0.x
sprint) routes through `MockHomeAssistantHandler`/`MockCameraPTZHandler`
via `ToolManager`. No real Home Assistant instance, physical WLED
device, or physical light was contacted or observed to illuminate. Test
H1's proof that a `tool_requested` event fires is proof the CALL was
correctly dispatched to ToolManager — it is not proof a physical device
responded.

## 13. Limitations

- **No cancellation.** `AutomationEngine` has no mechanism to cancel an
  in-progress execution (confirmed by full source inspection — the only
  related method, `_cancel_pending_delayed_action()`, cancels a
  not-yet-fired P0.8.9 SCHEDULED single action, a different concept).
  Per the brief's own Section 14, P0.11 does not build a cancellation
  framework. `_wait_delay()` is deliberately built on `threading.Event
  .wait()` rather than `time.sleep()` so a future sprint (the brief
  names P0.14) can add cancellation by threading an `Event` through and
  calling `.set()` on it — no call-site changes would be required.
- **No parallel execution.** Explicitly out of scope per the brief
  (reserved for P0.12) — a sequence's steps run strictly one at a time
  on one thread; verified structurally (no new `threading.Thread`,
  `ThreadPoolExecutor`, or `asyncio.gather` was introduced — tests M2,
  M6).
- **No IF/ELSE, loops, repeat, join, variables, or natural-language
  sequence authoring** — none were implemented, per the brief.

## 14. Recommended next phase

Per the user's own explicit closing instruction, this sprint stops here.
P0.12 (parallel execution) is the next phase named in the brief, but was
NOT started and should not be started without a new, separate user
request.
