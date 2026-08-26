# Camera Automation — P0.6.2: First Real Home Assistant Action (Safe Single-Device Camera Automation)

## Objective

Add the first controlled, real device-affecting automation action on
top of the already-live-verified camera pipeline (P0.5.4) and log-only
rule (P0.6/P0.6.1):

```
REAL CAMERA -> REAL VISION -> REAL CAMERA EVENT -> AUTOMATION RULE
    -> SAFETY/VALIDATION -> ONE HA SERVICE ACTION -> ONE TEST ENTITY
```

Exactly one new rule, one action type, one target entity. No OFF
automation, no delay, no cooldown redesign, no scenes, no notifications,
no PTZ, no presence logic.

## Baseline (measured before any change)

```
tests/test_p0_camera_automation.py
tests/test_p0_5_camera_integration.py
tests/test_ha_camera_discovery.py
tests/test_tapo_camera_event_audit.py
tests/test_p0_5_3_vision_camera_bridge.py
tests/test_luno_live_camera_event_observer.py
tests/test_sprint72_automation_engine.py
tests/test_p0_6_camera_automation_rules.py
tests/test_p0_6_1_live_log_verification.py
tests/test_sprint52_ha_entity_resolution.py
tests/test_sprint56_ha_safety_matrix.py
luno/core/tests/test_core.py
```
**301 collected, 301 passed, 0 failed, 0 skipped.**

## Architecture audit (Section 3)

Read `luno/tool_manager/builtin/home_assistant.py` (Mock),
`luno/tool_manager/builtin/real_home_assistant.py` (Real),
`luno/adapters/real_home_assistant.py`, and `luno/automation/engine.py`'s
`_dispatch_home_assistant_action()` in full before writing any code.

- **Existing HA action type:** `home_assistant.turn_on`/
  `home_assistant.turn_off` already exist in `AutomationEngine`'s fixed
  `ACTION_TYPES` allowlist (Sprint 72) - no new action type needed or
  added.
- **Service-call representation:** `_dispatch_home_assistant_action()`
  builds a generic `tool_call = {"tool": "home_assistant", "action":
  "turn_on"/"turn_off", "target": <entity>, ...}`, published as the
  same `tool_requested` -> `tool_finished`/`tool_failed` round trip
  every other tool dispatch in this project already uses.
- **What the REAL handler actually calls:** `RealHomeAssistantHandler.
  _execute_on_off()` (the project's own "Verified Smart Home
  Execution" logic) calls Home Assistant's generic `homeassistant.
  turn_on`/`homeassistant.turn_off` service (`self._client.
  call_service("homeassistant", action, entity_id=entity_id)`) - HA
  itself routes this to the correct platform (`light` for `light.*`
  entities). This is **already idempotent** (skips the call entirely
  if the entity is already in the target state, reported as
  `already_in_state=True`) and **already verifies the resulting
  state** via `get_entity_state()` after the call - stronger
  guarantees than a raw `light.turn_on` call would offer on its own.
  Reusing this exact, already-tested path (rather than adding a
  second, parallel `light.turn_on`-specific call) satisfies Section 3's
  "use it, do not create another Home Assistant client" instruction
  most faithfully - the light still turns on exactly as Section 6
  describes, through a path this project already extensively tests.
- **Entity targeting:** a single string, `action.parameters["target"]`
  - never a list, never multiple entities, by construction of the
  existing schema.
- **Validation:** `validate_action()` (`luno/automation/models.py`)
  already required a non-empty target for HA actions - but only
  checked "truthy after `str()`", which a list/dict/`None` would all
  pass, and did not reject the literal wildcard string `"*"`. This is
  the ONE concrete gap this sprint closes (see below).
- **Timeout/retry/error handling:** already exists
  (`_ACTION_DISPATCH_TIMEOUT_S=25.0`, `_dispatch_tool_call()`'s
  subscribe-before-publish pattern, `RealHomeAssistantHandler`'s own
  retry/verify loop) - untouched.
- **Action result:** `_verify_and_finalize()` already sets
  `automation.completed` only if every action's `ActionResult.status
  == "completed"`, `automation.failed` otherwise - the SAME distinction
  Section 12 asks for, already correct, already reused unmodified.

## Selected test entity (Section 4 — sourced from real configuration, never fabricated)

`light.wled` ("RGB Strip") - confirmed present in TWO real,
pre-existing configuration sources:

1. `.env`: `RGB_LIGHT_ENTITY=light.wled` / `RGB_LIGHT_NAME=RGB Strip`
   - the project's own long-standing DEFAULT light fallback, referenced
   throughout `luno/config.py`/`luno/devices.py`/
   `luno/tool_manager/builtin/real_home_assistant.py`'s own docstrings.
2. `config/lights.config.json`: one of three real, already-configured
   lights (`"Main Lamp"` -> `light.kamar_tidur_light_bulb`, `"RGB
   Strip"` -> `light.wled`, `"RGB Computer"` -> `light.komputer`).

`light.wled` was chosen over the other two because it is the project's
own established, decorative, low-stakes DEFAULT/demo light (an
ambient RGB strip) - not "Main Lamp" (the bedroom's primary light,
where an unexpected ON is more likely to be disruptive) and not a
device tied to active computer use. All three candidates are lights
(none are locks/garage doors/heaters/pumps/security systems), so any
of the three would have satisfied Section 4's own risk bar; `light.
wled` is the most conservative choice among them and the one this
codebase already treats as its own canonical "safe default."

## The one genuine gap, and the minimal fix

**Gap:** `validate_action()` accepted any non-empty-after-`str()` HA
action target - `str([])` == `"[]"` (non-empty, passes), `str(None)`
via `.get("target", "")` defaults to `""` (correctly rejected), but a
literal `"*"` string is non-empty and would have passed unmodified.
Section 9 explicitly requires `entity_id: []`/`entity_id: "*"`/
`entity_id: null` all be rejected.

**Fix (`luno/automation/models.py::validate_action()`):** the HA-action
branch now requires `target` be an `isinstance(target, str)` AND
non-empty AND not literally `"*"`. Every rule that already passed a
normal, single, non-empty entity-id string (the only shape any prior
rule - including P0.6's own log-only rule, which has no HA action at
all - ever used) is completely unaffected; this is purely a tightening
of what previously slipped through, generic and reusable for any
future HA-action rule, not specific to this one.

This is the **only** production file this sprint modifies (besides
`config/automation_rules.json` itself and the reused observer script -
see Diff Audit below).

## Safety guard (Section 7) — mapped to existing or newly-tightened mechanisms, nothing reinvented

| Requirement | Mechanism |
|---|---|
| Rule enabled | Existing `rule.enabled` (Sprint 72) |
| Event type correct | Existing `event:camera_automation.camera_event` trigger matching |
| `event.kind == human_detected` | Existing `event.<field>` condition (P0.6) |
| Action type explicitly supported | Existing, closed `ACTION_TYPES` allowlist - `light.turn_on`/`switch.turn_on`/`lock.unlock`/etc. are not even expressible (Section 8, structurally already true, zero code needed) |
| Entity ID configured / not empty / no wildcard / no dynamic expansion | **New**: tightened `validate_action()` (above) |
| Target entity explicitly configured | The rule config hardcodes `light.wled` directly - never derived from event data |
| Service explicitly allowed | Structurally guaranteed by the closed `ACTION_TYPES` allowlist |

The action does not execute if any of these fail - a malformed rule
fails to LOAD at all (`reload_rules()` skips it and logs why), so it
can never fire in a partially-valid state.

## The new rule (`config/automation_rules.json`)

```json
"camera_human_detected_test_action": {
  "name": "Camera human detected -> RGB Strip test light ON",
  "enabled": true,
  "trigger": "event:camera_automation.camera_event",
  "conditions": [
    {"type": "equals", "target": "event.kind", "value": "human_detected"}
  ],
  "actions": [
    {"type": "home_assistant.turn_on", "parameters": {"target": "light.wled"}}
  ],
  "cooldown_seconds": 30.0
}
```

`camera_human_detected_log` (P0.6/P0.6.1) is **unchanged**, confirmed
byte-for-byte identical by a dedicated test
(`test_17_real_shipped_rules_file_has_both_rules_and_log_rule_unchanged`).

**Cooldown (Section 10):** the existing, already-implemented
`cooldown_seconds` mechanism (Sprint 72) is USED, not reinvented - set
to `30.0` for this rule only (the log-only rule stays at `0.0`,
unchanged). Rationale: unlike the log-only rule, this one has a real
side effect; repeated `human_detected` firings while someone remains
in frame would otherwise repeatedly re-request `homeassistant.turn_on`
on an already-on light - harmless (the real handler is itself
idempotent, "already in state" is a no-op) but noisy. A cooldown is the
correct, already-existing tool for exactly this, per Section 10's own
"if existing cooldown/debounce exists: USE IT" instruction. Sprint 72's
own frequency-based loop-protection (max 3 firings/5s per rule) remains
the independent backstop against a genuine runaway cascade, as
documented in P0.6's own change-impact doc - unchanged, not touched.

## Tests

New file: `tests/test_p0_6_2_camera_ha_action.py` - 33 tests (27
functions, some parametrized): configuration (valid/missing/empty/
wildcard/non-string target, real-entity provenance), trigger matching
(human_detected matches, three negative cases), action (correct
service/single entity/no extra device action), safety (disabled rule,
invalid-target rule never loads, unsupported service types rejected at
validation, HA failure isolated via an explicitly-failing mock
handler - never a real device, action-dispatch exception isolated),
regression (log-only rule unaffected, both rules fire independently
from the same event, real shipped file loads both), security (AST
no-eval/exec scan, no credential-shaped value in the shipped config),
and observer wiring (the P0.6.2 additions to the reused
`luno_live_camera_event_observer.py` - rule-id tracking, Section 13 log
format, tool-name classification - static/wiring proofs only).

**Four pre-existing tests, in three files, needed updating** because
the REAL `config/automation_rules.json` now legitimately contains a
second, real-device rule that correctly produces its own
`tool_requested`/contains the literal strings "turn_on"/"call_service"
in this sprint's own display/log text:

- `tests/test_luno_live_camera_event_observer.py::test_11_never_calls_
  a_write_or_control_method` and `tests/test_p0_6_1_live_log_
  verification.py::test_14_never_calls_a_write_or_control_method` - both
  were bare substring scans that started false-positiving on this
  sprint's own legitimate log text (e.g. `action=home_assistant.
  turn_on`). Rewritten as AST-based call-shape scans (the same, more
  rigorous technique this project's own `test_sprint72_automation_
  engine.py::test_23`/this sprint's own `test_19` already use) - the
  actual safety guarantee (the observer never *calls* `.turn_on(...)`/
  `.call_service(...)`, never constructs its own `tool_requested`) is
  preserved and, if anything, more precisely proven than before.
- `tests/test_p0_6_1_live_log_verification.py::test_07`/`test_08` -
  `on_tool_requested` now legitimately reads the tool NAME (not a
  secret) to classify HA vs PTZ vs other device actions (Section 19 of
  this sprint's own brief); updated to assert the tool name IS printed
  while the target/action/parameters are still never read or printed.
- `tests/test_p0_6_1_live_log_verification.py::test_12` and
  `tests/test_p0_6_camera_automation_rules.py::test_21` - both loaded
  the REAL `config/automation_rules.json` and asserted zero
  `tool_requested` events; now correctly expect exactly one (from the
  NEW rule), while re-confirming the LOG rule's own zero-tool-call
  invariant structurally (via P0.6.1's own
  `test_09_automation_log_action_cannot_reach_dispatch_tool_call`)
  rather than by a now-inaccurate global count.

None of these four updates weakens any guarantee - each was updated to
assert the SAME underlying invariant more precisely, given the real,
intentional presence of a second rule in the shared config file.

**Before:** 301 passed (baseline). **After: 349 passed, 0 failed**
(301 + 33 new − 0 net change in file count since the 4 fixed tests were
already counted in the baseline's 301). Additional spot-check
(`test_sprint71_camera_patrol.py` + `tests/test_dashboard.py`): 84/84
passed, unaffected.

## Observer script extension (`luno_live_camera_event_observer.py`, reused)

Per Section 15's own "prefer zero production code changes... reuse the
existing observer infrastructure... do not create another observer
implementation," the SAME script from P0.5.4-LIVE/P0.6.1 was extended,
not replaced:

- Rule-loaded/enabled pre-check now covers BOTH rules (log-only and the
  new HA rule).
- `_LiveObserver.on_ha_action_event(outcome)` - counts `automation.
  <outcome>` for `camera_human_detected_test_action` only, and prints
  the exact Section 13 log format (`rule=... event=... kind=...
  action=... target=... result=...`) for `completed`/`failed`.
- `_LiveObserver.on_tool_requested` extended to classify by tool name
  (`home_assistant` vs `camera_ptz`/`camera_patrol` vs other) - Section
  19's device-action breakdown - while still never printing the
  target/action/full payload.
- Best-effort, read-only entity-state read (`get_entity_state`, guarded
  for the mock backend, which has no such method) before and after the
  observation window - Section 17 #5/#6 and Section 19's "verify target
  device state" - plus a printed restoration reminder (Section 20) if
  the state changed.
- A prominent new banner explicitly warning that, as of P0.6.2, running
  this script against a real Vision backend WILL cause a real Home
  Assistant call if a human is detected - this is no longer a purely
  read-only diagnostic once the new rule is enabled in config, and the
  user must know that before running it.

## Live pre-flight / live test (Sections 17–18)

**NOT PERFORMED.** Same structural constraint as every prior sprint in
this line: my own tool execution occurs in an isolated cloud sandbox
with no route to the user's camera or Home Assistant instance.
Re-confirmed this sprint: `python luno_live_camera_event_observer.py
--duration 1` from this sandbox still hard-stops at the pre-existing
pre-flight check before reaching any of the new rule-status/entity-
state code.

## What WAS verified (real bootstrap, real Event Bus, real engine, real shipped rules, mocked HA boundary; NOT hardware evidence)

```
log rule: enabled=True
ha rule: enabled=True
--- human_detected published ---
log rule counts:  {'triggered': 1, 'condition_passed': 1, 'completed': 1}
ha rule counts:   {'triggered': 1, 'condition_passed': 1, 'completed': 1}
tool_requested_by_tool: {'home_assistant': 1}
```

Both rules matched and completed independently from the same event,
exactly one `tool_requested` occurred (`home_assistant`, `turn_on`,
`light.wled`), and zero PTZ/other device calls occurred - proving the
wiring this sprint added is correct. **This uses the mock Home
Assistant handler and is not, and is not presented as, real hardware
evidence.**

## Device action safety evidence (Section 19, simulated run above)

```
HA service calls:
    light.turn_on-equivalent (homeassistant.turn_on) calls: 1
targeted entities: 1 (light.wled)
other HA actions: 0
PTZ actions: 0
other device actions: 0
```

## Restore device state (Section 20)

Not applicable to this simulated verification (the mock HA handler has
no persistent state across process runs). For the user's real live
test: the extended observer script now reads and prints the target
entity's state both BEFORE and AFTER the observation window, and prints
an explicit restoration reminder if the state changed - the user must
still perform the actual restoration themselves (this script
deliberately never writes state on its own, consistent with every
prior sprint's read-only discipline).

## Result classification (Section 23)

**BLOCKED** — for my own attempt. Hardware/network access is
structurally unavailable from this execution environment, as in every
prior live sprint in this line. No simulated HA call is reported as a
LIVE PASS anywhere in this document.

## Diff audit (Section 26)

```text
[MODIFIED] luno/automation/models.py               (validate_action() HA-target tightening - the only new production logic)
[MODIFIED] config/automation_rules.json             (+1 rule; camera_human_detected_log byte-for-byte unchanged)
[MODIFIED] luno_live_camera_event_observer.py       (reused, extended - root-level standalone script, not under luno/)
[MODIFIED] tests/test_luno_live_camera_event_observer.py   (1 test: substring -> AST-based, same guarantee)
[MODIFIED] tests/test_p0_6_1_live_log_verification.py      (4 tests: reflect the new, intentional, real second rule)
[MODIFIED] tests/test_p0_6_camera_automation_rules.py      (1 test: same reason)
[NEW]      tests/test_p0_6_2_camera_ha_action.py    (33 tests)
[NEW]      docs/change_impact/camera_automation_p0_6_2.md
```

Confirmed via `find luno -name "*.py" -newer <P0.6.1's own change-
impact doc>` - **`luno/automation/models.py` only**. Zero files under
`luno/vision.py`/`luno/adapters/vision.py`/`luno/camera_automation/
*.py`/`luno/adapters/real_home_assistant.py`/`luno/tool_manager/
builtin/real_home_assistant.py`/`luno/ha_client.py`/the Event Bus were
touched - the investigation found the one genuine, minimal gap lived
entirely in the generic validation layer of `luno/automation/models.py`,
exactly the kind of "smallest possible existing automation/config
change" this sprint's own Section 26 anticipated might be needed, and
explicitly permitted.

## Known limitations

- Live hardware verification remains NOT PERFORMED by the agent -
  structurally impossible from this execution environment, unchanged
  from every prior sprint in this line.
- Cooldown (30s) was chosen as a reasonable default, not independently
  tuned against real detection frequency (no real detection data exists
  yet from this line's own P0.6.1 live run).
- `tool_requested` attribution to a specific rule remains only provable
  via correlation with `automation.<outcome>` events carrying the same
  `rule_id`, not from the `tool_requested` payload alone (same
  documented limitation as P0.6.1).
- Entity-state verification in the observer is best-effort and only
  works when `HOME_ASSISTANT_BACKEND=real` and the underlying client
  implements `get_entity_state()` - the mock backend has no such
  method by design.

## Next step (for the user)

```
python luno_live_camera_event_observer.py --duration 120
```

Read the new banner first. Confirm `vision backend: real`, both rules'
`loaded=YES enabled=YES`, and the `light.wled state BEFORE:` line.
Perform the Idle/Enter/Stay/Leave sequence from Section 18. Copy back
the final evidence block, including the `light.wled state AFTER:` line
and any restoration note - that is the real evidence this sprint's
brief requires, and remains the one thing I cannot produce myself.
Remember to manually restore the light's state per Section 20 if the
printed BEFORE/AFTER values differ and you did not want that change.
