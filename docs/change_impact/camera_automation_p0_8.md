# LUNO P0.8.0 — Camera Automation → Home Assistant Action Safety Pipeline

## 1. Objective (restated)

Build the safety layer required before enabling real camera-driven
light automation: validate every camera-triggered `home_assistant.
turn_on`/`turn_off` action (allowlisted type, well-formed single
entity id, a valid/healthy triggering `CameraEvent`) before it ever
reaches the existing Home Assistant dispatcher, reuse the existing
cooldown/state-reading mechanisms rather than build new ones, and prove
all of it with mocked Home Assistant calls only. **No real Home
Assistant action was performed during this sprint** - see Section 10.

## 2. Baseline

Targeted set (P0/P0.5.x/P0.6.x/P0.7/Sprint 72/`test_real_adapters.py`/
Sprint 52/56/57/58 HA-adjacent tests), measured before any P0.8.0 file
was touched: **329 passed, 1 skipped, 0 failed.**

Full chunked sweep (10 file groups + the two isolated groups this
project's own convention requires), measured as the immediately-prior
state (end of P0.7): **4,018 passed** across the main chunks +
`test_vision_sprint8.py` (32, isolated) + the sprint69 pair (37,
isolated).

## 3. Architecture audit (Section 1)

Traced directly from the real code before writing anything:

- `luno/automation/engine.py::_dispatch_home_assistant_action()` is the
  ONE place any `home_assistant.turn_on`/`turn_off` `AutomationAction`
  (camera-triggered or not) is turned into a `tool_requested` Event Bus
  publish, picked up by the EXISTING `ToolManagerBridgeModule` ->
  `ToolManager` round trip - the same path a manual voice command,
  Sprint 71's `CameraPatrolModule`, and every other automation action
  already use. This is the ONE correct integration point for a safety
  gate - confirmed there is no second dispatch path anywhere.
- `AutomationEngine._trigger()` already has a complete cooldown
  (`_cooldown_until`, Phase 8) and loop-protection (`_recent_firings`,
  Phase 9) mechanism - three `human_detected` events in a row already
  only re-trigger a rule once per `cooldown_seconds` window, entirely
  independent of what this sprint adds.
- `RealHomeAssistantHandler._execute_on_off()` (`luno/tool_manager/
  builtin/real_home_assistant.py`) ALREADY has an "already in the
  requested state -> skip the service call" shortcut, driven by
  `RealHomeAssistantClient.get_entity_state()` - but this is buried
  inside the Tool Manager layer, never exposed to `AutomationEngine`
  itself. `MockHomeAssistantHandler` (the handler this project's tests
  and default/mock backend always use) has no such capability at all.
- `AutomationEngine.__init__` already accepts a `state_readers: Dict[str,
  Callable]` (Sprint 72) - but as of P0.7's own "Next Recommended
  Sprint" note, no Home Assistant "get current state" entry had ever
  been wired into it (`luno/bootstrap/modules.py` only wires
  `"camera_patrol"`).
- `AutomationRule.trigger.parameters["event_name"]` already carries the
  exact string `"camera_automation.camera_event"` for every camera-
  triggered rule (`event:camera_automation.camera_event` in
  `config/automation_rules.json`) - the mechanism this sprint uses to
  tell a camera-triggered rule's HA action apart from every other
  rule's, with zero new plumbing.

**Conclusion: no second Vision/YOLO/RTSP pipeline, no second Home
Assistant client, no second AutomationEngine, no second Event Bus, and
no second cooldown implementation were needed or added.** This sprint
is a single new, small, pure validation module plus a small number of
additive call-sites in the ONE existing dispatch path.

## 4. Safety gate implementation (Section 2/3/7)

New: `luno/automation/camera_action_safety.py` - `validate_camera_ha_
action(action_type, target, event_data, ha_state_reader=None) ->
SafetyCheckResult`. Pure, synchronous, no Event Bus subscription, no
Home Assistant client, no camera/vision code.

Called from EXACTLY one place: `AutomationEngine._dispatch_home_
assistant_action()`, and ONLY when `AutomationEngine._is_camera_
triggered_rule(rule)` is true (`rule.trigger.type == "event"` and
`rule.trigger.parameters["event_name"] == "camera_automation.camera_
event"`). Every other automation action (time/manual triggers, or an
event trigger on any other event type) is byte-for-byte unaffected -
confirmed by `tests/test_p0_8_0_camera_action_safety.py`'s own Section
E (non-camera automation tests) and by the full regression sweep
(Section 8).

Checks run in strict order, first failure wins, fail closed (never
"proceeds cautiously"):

1. **Action type allowlist** (Section 3A) - only `home_assistant.
   turn_on`/`turn_off` (`CAMERA_HA_ACTION_TYPES`, its own small,
   independent constant - not derived from `models.ACTION_TYPES`, so a
   future sprint that grants the general engine a new HA action type
   does not silently also grant it to camera-triggered actions).
2. **Target/entity id** (Section 3B) - `None`, empty, non-string,
   `list`/`tuple`/`set` (multiple targets), the literal wildcard `"*"`,
   or anything not matching `^[a-z][a-z0-9_]*\.[a-z0-9_]+$` (stricter
   than `models.py::validate_action()`'s own load-time check, safe to
   be stricter here since every real entity id this project has ever
   used already matches it) is refused as `invalid_target`.
3. **Camera event validity** (Section 3C) - a missing/non-dict
   `event_data` is `missing_event_context`; a missing/empty/non-string
   `kind` is `malformed_camera_event`.
4. **Vision-state safety** (Section 3D) - a truthy `detection_error` is
   `detection_error_present`; `kind == "camera_offline"` is
   `camera_offline`; `available is False` is `camera_unavailable`. None
   of these ever infer "human_cleared" from a failure/offline signal -
   they simply refuse the action outright (Section 3D's own explicit
   warning, the same principle P0.6.2-FIX/P0.6.3/P0.7 already
   established one layer down in the Vision pipeline itself).
5. **State-aware skip** (Section 5, optional) - only attempted when the
   caller wired `ha_state_reader` (see Section 6 below). A reader that
   raises is `ha_state_lookup_failed` (fail closed - Section 7 item 7);
   a reader that reports the entity already in the desired state
   returns `allowed=True, skip_dispatch=True, code="already_in_desired_
   state"` - the caller records this as a completed, no-op action
   (matching the precedent `RealHomeAssistantHandler._execute_on_off()`
   already set for the identical scenario) WITHOUT ever calling the
   dispatcher.

## 5. Allowed actions

Exactly two: `home_assistant.turn_on`, `home_assistant.turn_off` -
`CAMERA_HA_ACTION_TYPES` in `camera_action_safety.py`. No other Home
Assistant service (`toggle`, `run_script`, `set_temperature`,
`set_color`, `set_brightness` - all of which `MockHomeAssistantHandler`/
`RealHomeAssistantHandler` already support for voice commands) is
reachable from a camera-triggered rule - refused as
`unsupported_action_type`.

## 6. Blocked conditions

Every one of Section 7's required "no device action" cases, verified by
a dedicated test:

| # | Condition | Code | Test |
|---|---|---|---|
| 1 | camera_offline | `camera_offline` | `test_03`, `test_15` |
| 2 | detection_error | `detection_error_present` | `test_02`, `test_14`, `test_28` |
| 3 | malformed CameraEvent | `malformed_camera_event` | `test_04`, `test_16` |
| 4 | invalid entity id | `invalid_target` | `test_05` (8 parametrized shapes) |
| 5 | unsupported HA action | `unsupported_action_type` | `test_06`, `test_06b` |
| 6 | missing event context | `missing_event_context` | `test_04b`, `test_04c` |
| 7 | HA state lookup failure | `ha_state_lookup_failed` | `test_11`, `test_23` |
| 8 | automation rule disabled | (pre-existing engine check, unaffected) | `test_18` |
| 9 | camera automation disabled | (pre-existing bridge `is_enabled()` gate, unaffected) | `test_17` |

Items 8/9 needed no new code - they were already fail-safe before this
sprint (a disabled rule never reaches `_on_bus_event()`'s dispatch loop;
a disabled `CameraAutomationModule`/`VisionCameraEventBridge` never even
subscribes to publish a `camera_automation.camera_event` in the first
place, per the `is_enabled()` gate P0.5.3 already established). This
sprint's own tests re-confirm both remain true, not re-implement them.

## 7. State/cooldown behavior (Section 4/5)

- **Cooldown/duplicate protection**: entirely `AutomationEngine._trigger
  ()`'s existing `_cooldown_until` mechanism (Sprint 72, Phase 8) - the
  new `camera_test_automation_safety_action` rule sets `cooldown_
  seconds: 30.0` (matching `camera_human_detected_test_action`'s own
  precedent). `tests/test_p0_8_0_camera_action_safety.py::test_19`
  publishes three `human_detected` events back to back and confirms
  exactly ONE `home_assistant` `tool_requested` call for this rule's
  entity, not three. No second cooldown implementation was added.
- **State-aware skip**: `AutomationEngine` gained one new OPTIONAL
  public attribute, `ha_state_reader: Optional[Callable[[str],
  Optional[str]]]` (same "wired post-construction" convention P0.7's
  `vision_status_reader` already established), consumed only by
  `camera_action_safety.validate_camera_ha_action()`. Wired by a new
  bootstrap function, `luno/bootstrap/adapters.py::register_camera_
  action_ha_state_reader()` - same post-hoc "needs output from BOTH
  `register_all_modules()` and `register_all_adapters()`" pattern as
  `register_vision_context_reader()`. It closes over the SAME real
  `RealHomeAssistantClient.get_entity_state()` `RealHomeAssistantHandler`
  itself already calls; no-ops harmlessly (leaves `ha_state_reader`
  `None`) on the mock backend (`MockHomeAssistantClient` has no
  `get_entity_state` at all - `callable(...)` is naturally `False` for
  it) or when the real backend never connected. `test_21`/`test_22`
  prove the skip/proceed behavior end to end with an injected FAKE
  reader (never a real HA call); `test_24`/`test_24b` prove the
  bootstrap wiring itself no-ops correctly on the mock backend and on
  missing modules/adapters.

## 8. Tests

`tests/test_p0_8_0_camera_action_safety.py` - 48 tests:

- **0. Fixture sanity** (1) - the mock, not a real client, is what every
  other test in this file exercises.
- **A. `validate_camera_ha_action()` pure units** (Section 3/5/7) (19) -
  every allow/block code path, in isolation, no bootstrap.
- **B. Real-bootstrap Event Bus** (Section 10) (6) - a real Runtime/
  AutomationEngine/CameraAutomationModule stack, a real `camera_
  automation.camera_event` published directly onto the real Event Bus
  (per the brief's own worked example), observed end to end through
  the mocked HA dispatcher.
- **C. Cooldown/duplicate protection** (Section 4) (2).
- **D. State-aware skip** (Section 5) (5) - already-on/already-off/
  different-state/unwired/lookup-failure, all via the real bootstrap +
  an injected fake `ha_state_reader`.
- **E. Non-camera automation unaffected** (Section 9 item 13) (2) - a
  manual-trigger rule's HA action bypasses the gate entirely; the
  pre-existing P0.6.2 rule still fires.
- **F. Attribution** (Section 6) (2) - the full `automation.triggered`
  -> `condition_passed` -> `action_started` -> `tool_requested` ->
  `automation.completed` trail is observable with no credential-shaped
  string anywhere in it; a refusal's `code` is recoverable from the
  engine's own execution history.
- **G. Architecture guard** (Section 12) (6) - no direct HA API call, no
  Event Bus subscription/camera-vision code, in `camera_action_safety.
  py`; exactly one `AutomationEngine(`/`RealHomeAssistantClient(`
  construction site repo-wide; the rules file has exactly the expected
  four rules; no `human_cleared`-triggered rule was added.

**Every test proves no real HA service is called** - `test_00` asserts
the fixture itself uses `MockHomeAssistantHandler`; `register_real_tool_
handlers()` is never called anywhere in this file (grep-verifiable);
blocked-action tests additionally assert zero `tool_requested` events
with `tool == "home_assistant"` were ever published.

## 9. Regression result

- Targeted HA/automation/camera set: **329 -> 377 passed** (+48 new),
  1 skipped (unchanged), 0 failed.
- Full chunked sweep: **4,018 -> 4,066 passed** (+48 new: 40 chunk +
  8 elsewhere is not the actual split - see the exact per-chunk numbers
  below), isolated groups unchanged (32/32, 37/37).

**Three genuine, EXPECTED test-staleness updates** (pre-existing tests
whose hardcoded assumptions were correctly invalidated by this sprint's
own intentional additive changes - same category of fix P0.6.2-FIX/
P0.6.3/P0.7 already established as acceptable when the underlying
change is deliberate):

1. `tests/test_sprint72_automation_engine.py::test_38_unknown_action_
   type_refused_defensively` / `test_39_automation_log_action_never_
   dispatches_a_tool_call` - both called the private `_dispatch_action()`
   directly with its OLD 2-argument signature; updated to pass a
   (trigger-less) `AutomationRule`, since `_dispatch_action()` now takes
   a leading `rule` parameter (+ trailing optional `event_data`) so the
   safety gate can tell a camera-triggered action apart from every
   other one.
2. `tests/test_sprint72_automation_engine.py::test_67_event_payloads_
   are_metadata_only` - its allowlisted-keys set didn't yet include the
   new `code` field `automation.action_completed`/`action_failed` now
   carries (Section 6's own attribution requirement) - updated to
   include it (still a short code string, never a credential/frame,
   checked by the same test).
3. `tests/test_p0_7_vision_context.py::test_36_automation_rules_file_
   now_has_exactly_three_rules` - "three rules" was accurate as of P0.7;
   P0.8.0 legitimately added a fourth. Updated to `issubset()`, the same
   convention `test_p0_6_3_unified_vision_camera_automation.py::test_26`
   already established for the identical prior situation.

**Pre-existing failures found, confirmed unrelated, NOT fixed** (all
independently traced to files/behavior this sprint never touches - same
exact set already documented in P0.7's own regression baseline and, for
two of them, in `project_handover.md` §22 as far back as the Dashboard
Startup & Access Recovery sprint):

- `tests/test_llm_max_completion_tokens_compatibility.py` (7) + `tests/
  test_memory_session_summary_api_compatibility.py` (5) - this
  checkout's own `.env` sets `MAX_TOKENS_PARAM=max_tokens`, disagreeing
  with the tests' hardcoded default expectation.
- `tests/test_mic_device_index.py` (6) + `tests/test_real_adapters.py::
  test_real_whisper_source_*` (2) - missing `list_microphones.py` /
  a pre-existing `RealWhisperSource` test-construction bug.
- `tests/test_production_launcher.py::test_07` (1) - blocked OpenRouter/
  Fish Audio network health checks.
- `tests/test_sprint63_long_term_memory_recovery.py` (9-10) + `tests/
  test_sprint64_memory_corruption_forensics.py` (5) + `tests/test_
  sprint67_mutation_audit_trail.py` (0-1) + `tests/test_sprint68_
  mutation_audit_hardening.py` (2) - `config/backups/` file-count drift
  (51 files present against a hardcoded pristine-count expectation of
  12) from months of cumulative sprint activity in this same long-lived
  checkout - count varies slightly run to run depending on background
  scheduler/dashboard activity during the test process's own lifetime,
  never on anything this sprint touched (all four files import only
  `luno.memory`/`luno.config`, confirmed via source read).

## 10. Live hardware status - REAL HA ACTIONS WERE NOT PERFORMED

**Explicit statement, per the brief's own hard constraint:** no real
Home Assistant `turn_on`/`turn_off` call was made at any point during
this sprint. Every test in `tests/test_p0_8_0_camera_action_safety.py`
routes through `MockHomeAssistantHandler` (confirmed by `test_00`, and
by the fact that `register_real_tool_handlers()` - the ONE function in
this codebase that would swap in a real handler - is never called
anywhere in this file or by this sprint's own code). `register_camera_
action_ha_state_reader()` was also only ever exercised in these tests
against the mock backend (where it correctly no-ops) or via a hand-
injected fake reader - never a real `RealHomeAssistantClient`.

**Result classification: BLOCKED** (for the agent's own live-hardware
attempt) - same structural sandbox limitation as every prior sprint in
this line (no network route to a real Home Assistant instance or
camera). This is P0.8.0's own intended state, not a gap: this sprint is
explicitly the safety/preparation sprint; P0.8.1 is where live hardware
verification belongs (Section 14's own "Hard Constraint").

## 11. Files changed - diff safety (Section 12)

- `[NEW]` `luno/automation/camera_action_safety.py` - the safety gate
  itself. Necessary: this is the sprint's own core deliverable, an
  isolated module with no existing equivalent.
- `[MODIFIED]` `luno/automation/engine.py` - `_dispatch_action()`/
  `_run_actions()`/`_dispatch_home_assistant_action()` gained a leading
  `rule` parameter (+ trailing optional `event_data`, already computed
  by `_run_execution()` for condition evaluation, simply threaded one
  step further); new `_is_camera_triggered_rule()` helper; new `ha_
  state_reader` public attribute. Necessary: this is the ONE existing
  HA dispatch path (Section 3 audit finding) - the gate has to be called
  from inside it, which requires knowing which rule/event triggered the
  action being dispatched, information the pre-P0.8.0 signature didn't
  carry that far.
- `[MODIFIED]` `luno/bootstrap/adapters.py` - new `register_camera_
  action_ha_state_reader()` function (+ `Optional` added to the file's
  existing `typing` import). Necessary: same post-hoc "needs output from
  BOTH register_all_modules()/register_all_adapters()" wiring gap every
  prior sprint's own equivalent function (`register_vision_context_
  reader()` etc.) already had to solve the same way.
- `[MODIFIED]` `main.py` - one new call,
  `register_camera_action_ha_state_reader(modules, adapters)`. Necessary:
  the one call site every other post-hoc wiring function in this project
  already requires.
- `[MODIFIED]` `config/automation_rules.json` - one new rule,
  `camera_test_automation_safety_action` (log-equivalent test rule,
  targets the harmless `light.test_camera_automation`, mock-only in
  every test that exercises it). Necessary: Section 8's own explicit
  requirement for a dedicated test rule. Both/all three pre-existing
  rules are byte-for-byte unchanged.
- `[MODIFIED]` `tests/test_sprint72_automation_engine.py`,
  `tests/test_p0_7_vision_context.py` - 3 pre-existing tests updated
  for this sprint's own intentional additive changes (Section 9 above).
  Necessary: each assertion's own hardcoded assumption became stale by
  design, not by accident.
- `[NEW]` `tests/test_p0_8_0_camera_action_safety.py` - 48 tests.
- `[NEW]` `docs/change_impact/camera_automation_p0_8.md` - this file.

**Not touched:** `luno/adapters/vision.py`, `luno/vision.py`, `luno/
adapters/real_vision.py`, `luno/camera_automation/*.py` (the entire
package - CameraEvent/CameraAutomationModule/VisionCameraEventBridge/
VisionContext are all unmodified), `luno/adapters/home_assistant.py`,
`luno/adapters/real_home_assistant.py`, `luno/tool_manager/builtin/
home_assistant.py`, `luno/tool_manager/builtin/real_home_assistant.py`,
`luno/core/*` (Event Bus/Runtime/module_manager), `luno/automation/
models.py`, `luno/automation/conditions.py`, `config/camera_automation.
json`, `.env`. No lights/switches/PTZ/locks action type was added,
modified, or invoked by anything in this sprint.

## 12. Known limitations (honest, not glossed over)

- Live hardware verification remains BLOCKED for the agent - see Section
  10. This is intentional for P0.8.0, not a gap.
- The state-aware "already in the desired state -> skip" optimization
  (Section 5) is only ever exercised against a REAL Home Assistant
  client on the user's own machine with `HOME_ASSISTANT_BACKEND=real`
  and a successful connection - in every environment this sprint could
  actually run in (mock backend), it correctly stays unwired (`ha_state_
  reader is None`), which is a legitimate, tested "not available right
  now" state per the brief's own "if already available" wording, not a
  failure.
- The pre-existing `.env`/`MAX_TOKENS_PARAM` mismatch and `config/
  backups/` accumulation (Section 9) remain unfixed - both were already
  flagged as optional future pickups in `project_handover.md` §22 well
  before this sprint, and are entirely outside P0.8.0's own scope.
- No before/after performance numbers were measured - `validate_camera_
  ha_action()` is a handful of dict lookups/string comparisons with one
  optional function call; no real camera/HA load exists in this sandbox
  to measure against regardless.

## 13. Recommended P0.8.1 live test procedure

1. On the real machine, with `HOME_ASSISTANT_BACKEND=real` and a real
   Tapo C212, run `python main.py` and confirm the Vision Dashboard
   still shows real detections exactly as before this sprint (this
   sprint touched none of that code).
2. Confirm `register_camera_action_ha_state_reader()` actually wired a
   reader this time - check the startup log line: "Camera Action Safety
   Gate: HA state reader wired - ...". If it does NOT appear, the real
   HA backend didn't connect (`HOME_ASSISTANT_BACKEND=real` requested
   but fell back to mock) - fix that first, since P0.8.1's own live
   verification needs a real client.
3. In `config/automation_rules.json`, temporarily point `camera_test_
   automation_safety_action`'s target at a REAL, harmless, already-
   configured test light (or add a new dedicated one) instead of the
   placeholder `light.test_camera_automation` - the placeholder entity
   does not exist in any real Home Assistant instance and would
   correctly resolve to nothing.
4. Walk into camera view. Confirm in the AutomationEngine log: the rule
   matches, the safety gate logs no refusal, and the light actually
   turns on. Walk out of view and back in again quickly (within the
   rule's 30s cooldown) - confirm the SECOND entry does NOT re-trigger
   a Home Assistant call (cooldown, Section 7 above).
5. Turn the light on manually first, THEN walk into view - confirm the
   AutomationEngine log shows `code=already_in_desired_state` and NO
   second Home Assistant call was made (Section 5's own state-aware
   skip, this time against the real client).
6. Only after steps 1-5 are all confirmed should a future sprint (P0.8.1
   itself, or later) consider pointing a camera-triggered rule at a
   REAL production light for daily use.

**Nothing in this section was executed by the agent - it is a
procedure for the user (or a future sprint's live-hardware attempt) to
run, not a report of results.** No live HA/camera evidence is claimed
anywhere in this document.
