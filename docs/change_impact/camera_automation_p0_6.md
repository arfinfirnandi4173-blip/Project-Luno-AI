# Camera Automation — P0.6: Camera Automation Rule Integration + Log-Only

## Objective

Connect the already-live-verified camera event pipeline

```
Tapo C212 -> RTSP/Vision/YOLO -> CameraPersonEntered/CameraPersonLeft
    -> Vision Bridge -> CameraAutomationModule -> camera_automation.camera_event
```

to the existing `luno.automation.AutomationEngine` (Sprint 72), and
implement the first rule: `human_detected` -> LOG ONLY. No Home
Assistant action, no PTZ, no device action of any kind. This sprint
proves `EVENT -> RULE -> MATCH -> LOG`, deliberately not
`EVENT -> RULE -> DEVICE ACTION` — that belongs to a future, separately
controlled sprint.

## Baseline (measured, not assumed)

Recorded before any change this sprint:

```
tests/test_p0_camera_automation.py
tests/test_p0_5_camera_integration.py
tests/test_ha_camera_discovery.py
tests/test_tapo_camera_event_audit.py
tests/test_p0_5_3_vision_camera_bridge.py
tests/test_luno_live_camera_event_observer.py
tests/test_sprint72_automation_engine.py
```
205 passed, 0 failed.

```
luno/core/tests/test_core.py   (Event Bus coverage)
```
19 passed, 0 failed.

**Combined baseline: 224 passed, 0 failed.**

## Architecture audit (Section 3 — performed before writing any code)

`luno/automation/engine.py` (`AutomationEngine`, Sprint 72) and
`luno/automation/models.py`/`conditions.py` were read in full.

- **Rule loading:** `config/automation_rules.json`, a dict keyed by
  rule id, read once at `start()`/`reload_rules()`. A malformed
  individual rule is skipped and logged, never crashes the whole load.
- **Triggers:** `event` / `time` / `manual`, a fixed allowlist. An
  `event` trigger matches on the literal `event.type` string via a
  `Dict[event_name, [rule_id, ...]]` index, populated from
  `reload_rules()`, consulted by `_on_bus_event()` (subscribed once,
  wildcard `"*"`, the same observability-tap idiom every other
  event-consuming module in this project already uses).
- **Conditions:** a pure function, `evaluate_condition(condition,
  state_readers)`. Before this sprint, `condition.target` was resolved
  ONLY through a caller-supplied `state_readers: Dict[str, Callable[[],
  Any]]` map (e.g. `"camera_patrol"` — "what is camera_patrol's current
  state right now?"). **Critical finding:** this map has no concept of
  "the event that just triggered this execution" — `_on_bus_event()`
  discarded the triggering event's own `.data` entirely before
  conditions were ever evaluated. There was no existing way to express
  "match only when `kind == 'human_detected'`" prior to this sprint.
- **Actions:** a fixed allowlist. `automation.log` already exists,
  already belongs to `_INTERNAL_ACTION_TYPES` (`luno/automation/
  models.py`), and `_dispatch_internal_action()` (`engine.py`) already
  does exactly what Section 7 of this sprint's brief asks for: emits a
  structured log line, returns a plain `ActionResult`, and — critically
  — **never calls `_dispatch_tool_call()` at all**, so it structurally
  cannot reach Home Assistant, PTZ, or any other device. Dry-run/log-
  only already existed; nothing new needed to be built for it.
- **Enable/disable:** `rule.enabled` (loaded field) plus
  `enable_automation()`/`disable_automation()` (explicit, user-
  initiated, persisted via the existing `atomic_write_json()`
  primitive). One mechanism only — not duplicated.
- **Error isolation:** `_run_execution()`'s top-level `try/except`
  already guarantees an action exception becomes `automation.failed`,
  never a crash; `_publish()` swallows its own publish failures.
  Verified behaviorally this sprint (Section D of the new test file),
  not just read about.
- **Duplicate events:** no dedicated dedupe/cooldown exists by default
  (`cooldown_seconds` defaults to `0.0`, opt-in per rule). The existing
  Sprint 72 loop-protection safety net (`_MAX_FIRINGS_IN_WINDOW=3`
  within `_CYCLE_WINDOW_S=5.0`) is a cycle/runaway-loop guard, not an
  intentional dedupe mechanism. Per the brief's own Section 13, this is
  documented, not silently redesigned — see Known Limitations below.

## The one genuine gap, and the minimal fix

The condition engine's `state_readers`-only design made "match a field
of the triggering event's own payload" structurally inexpressible.
Two options were considered:

1. **A side-channel state reader** that caches "the last camera
   event's kind," updated by a second subscriber. Rejected: this is a
   race-prone hack (correctness would depend on subscriber ordering and
   timing between two independent Event Bus deliveries) — exactly the
   kind of fragile, non-boring construction this sprint's own closing
   principle warns against.
2. **A small, additive extension to the real condition engine** letting
   a condition read a field of the event that actually triggered THIS
   execution. Chosen — the honest, minimal fix, entirely within the
   `automation` package itself (the natural, general-purpose home for
   this capability — useful to any future event-triggered rule, not
   just camera events).

### The change

`luno/automation/conditions.py`: `evaluate_condition()` gained one new
optional parameter, `event_data: Optional[Dict[str, Any]] = None`. A
condition `target` starting with the literal prefix `"event."` is now
resolved from `event_data[field]` (the field name is whatever follows
the prefix, e.g. `target="event.kind"`) instead of `state_readers`.
**Every target that does NOT start with `"event."` is completely
unaffected** — resolved via `state_readers` exactly as before,
byte-for-byte identical behavior. A missing/absent `event_data` (time/
manual triggers have no originating event) resolves to
`CONDITION_INVALID` — the same fail-closed semantics an unknown
`state_readers` target already had; no new failure mode, no relaxation
of "no partial execution."

`luno/automation/engine.py`: `event.data` is now threaded from
`_on_bus_event()` through `_trigger()` -> the spawned execution thread
-> `_run_execution()` -> `_evaluate_conditions()` -> `evaluate_
condition()`, as one additional, fully optional parameter at each hop
(default `None` everywhere). `_on_time_trigger()` and `run_automation()`
(manual trigger) never pass one — unchanged, pre-P0.6 behavior for
every trigger type that has no originating event.

**No other production file was touched.** `luno/vision.py`, `luno/
adapters/vision.py`, `luno/camera_automation/cameras.py`, `luno/
camera_automation/vision_bridge.py`, `luno/camera_automation/module.py`,
the Tapo/RTSP code, `luno/ha_client.py`/`HomeAssistantAdapter`, and the
Event Bus itself (`luno/core/events.py`) were never modified — the
investigation found the actual gap lived entirely inside the
`automation` package, which is exactly where the brief's own "smallest
possible AutomationEngine integration point" allowance anticipated a
change might be needed.

## Rule schema (Section 6 — used the existing schema, invented nothing)

`config/automation_rules.json` (previously `{}` — no rules existed
before this sprint):

```json
{
  "camera_human_detected_log": {
    "name": "Camera human detected (log only)",
    "enabled": true,
    "trigger": "event:camera_automation.camera_event",
    "conditions": [
      {"type": "equals", "target": "event.kind", "value": "human_detected"}
    ],
    "actions": [
      {"type": "automation.log", "parameters": {"message": "camera_automation.camera_event kind=human_detected matched (log-only, no device action)"}}
    ],
    "cooldown_seconds": 0.0
  }
}
```

Every field here is the pre-existing `AutomationRule`/`rule_from_dict()`
shape (`luno/automation/models.py`) — the compact `"event:<name>"`
trigger string, the existing `conditions`/`actions` array shapes, the
existing `automation.log` action type. The only new vocabulary is the
`event.kind` condition target, which is the P0.6 addition described
above. The rule is deliberately generic (no `camera_id` condition) —
Section 10 asked that camera identity not be hardcoded without
independent proof; `event.camera_id` matching is fully supported by the
same generic mechanism (proven by a dedicated test) but not used in the
shipped rule.

## Safety (Section 8/9 — why no real device action can occur)

Structural, not just conventional: `automation.log` is in `engine.py`'s
own `_INTERNAL_ACTION_TYPES` set, and `_dispatch_internal_action()`
returns directly from a `str(...)`/`log(...)` call — it never reaches
`_dispatch_tool_call()`, the one and only code path in this engine that
ever publishes `tool_requested` (the event every real Home Assistant/
PTZ action passes through). A test in the new suite (`test_16_matched_
execution_never_publishes_tool_requested`) subscribes to `tool_requested`
directly on the real Event Bus for the full duration of a matched
execution and asserts zero calls — proving this behaviorally, not just
by reading the source.

Event matching was verified in both directions: `kind=human_detected`
matches; `human_cleared`/`camera_online`/`camera_offline` do not
(parametrized test, three negative cases). No false positives.

## Tests

New file: `tests/test_p0_6_camera_automation_rules.py` — 23 test
functions (27 collected test cases once `@pytest.mark.parametrize` is
expanded: 3 kinds × 2 parametrized tests + 21 non-parametrized). Covers,
per the brief's own section numbering:

- Rule loading (valid loads, disabled loads, malformed rejected safely
  without crashing the whole file).
- Event matching (`human_detected` matches; `human_cleared`/
  `camera_online`/`camera_offline` do not; no event_data fails closed,
  not a false match; a missing field fails closed; non-`event.`
  targets are completely unaffected — the backward-compatibility
  proof; `event.camera_id` is matchable but not mandatory).
- Execution (matched rule executes the log action end to end through
  the real Event Bus; unmatched events produce `automation.skipped`,
  never the action; a disabled rule never triggers at all; re-enabling
  makes it fire again).
- Safety (zero `tool_requested` events during a matched execution;
  static confirmation `automation.log` is internal-only and the shipped
  rule uses no HA/camera action type; an injected action exception
  produces `automation.failed`, not a crash, and the engine/bus keep
  working for a subsequent unrelated event).
- Duplicate events (three rapid `human_detected` events each
  independently complete — no new cooldown was invented; documented,
  not silently redesigned, per Section 13).
- Real shipped-file integration: loads the ACTUAL `config/
  automation_rules.json` this sprint ships (not a copy) through the
  real engine, and a full real-bootstrap test publishes a
  `camera_automation.camera_event` through the real Event Bus and
  proves it reaches `camera_human_detected_log`'s log action with zero
  `tool_requested` calls.
- Security: an AST-based no-`eval`/`exec`/dynamic-import re-scan of the
  two touched files, and a static check that the shipped rule file
  contains no credential-shaped string.

**Before:** 224 passed (baseline). **After — targeted suite: 251
passed, 0 failed (224 + 27).**

Additional suites re-run for this sprint's own diff-relevant regression
(not part of the 224/251 combined count above, reported separately):
`tests/test_sprint71_camera_patrol.py` + `luno/adapters/tests/
test_adapters.py` + `tests/test_dashboard.py`: **99 passed, 0 failed**
(unaffected by this sprint's changes; `automation_engine`/Event Bus are
shared infrastructure these suites also exercise).

## Live verification

**NOT PERFORMED.** Same structural constraint documented in every prior
sprint in this line: my own tool execution always occurs in an isolated
cloud sandbox with no route to the user's Tapo C212/RTSP stream. This
sprint's own manual smoke test (below) proves the rule fires correctly
against a REAL bootstrap and REAL Event Bus using a simulated
`camera_automation.camera_event` — it is not, and is not presented as,
hardware verification. The real hardware pipeline itself was already
verified in P0.5.4/P0.5.4-LIVE/P0.5.4-FIX; this sprint only adds the
rule layer on top of it, which the user's real `main.py` will now also
load by default (`config/automation_rules.json` ships with the rule
`enabled: true`).

Manual sandbox smoke test performed this sprint (real bootstrap, real
Event Bus, simulated event, not hardware):

```
[Luno.automation_engine] automation.log [camera_human_detected_log/exec-...]:
    camera_automation.camera_event kind=human_detected matched (log-only, no device action)
```
followed by `automation.completed` for the matching event, and
`automation.skipped` (`reason=condition_failed`) for a `human_cleared`
event published immediately after — exactly the `EVENT -> RULE ->
MATCH -> LOG` chain this sprint set out to prove.

## Known limitations (Section 14/18 — documented, not silently fixed)

- **No dedicated cooldown/dedupe for this rule** (`cooldown_seconds:
  0.0`). Every matching event triggers and completes independently.
  The only existing backstop is Sprint 72's generic loop-protection
  (max 3 firings/5s per rule id, refuses further firings with
  `automation_cycle_detected` rather than crashing). If real-world
  human presence produces bursts of `human_detected` events faster
  than that window (unlikely for a room-level presence signal, but not
  independently measured this sprint), that is the visible behavior —
  a deliberate scope boundary for this sprint, not a bug. Cooldown
  tuning belongs to a future hardening sprint.
- **Camera identity is not verified.** The shipped rule does not
  condition on `camera_id` (per Section 10, since `same_physical_
  camera` remains `UNKNOWN` per P0.5.1–P0.5.4's own evidence chain).
  Any camera event with `kind=human_detected`, from any camera the
  system is ever wired to, would match.
- **No Home Assistant actions are enabled by this rule, at all**, by
  design — this sprint's entire point. A future, separately-controlled
  sprint is required before any device action can be added, per the
  brief's own closing architectural principle.

## Diff audit (Section 20)

```text
[MODIFIED] luno/automation/conditions.py     (event.<field> condition target — additive, backward compatible)
[MODIFIED] luno/automation/engine.py         (thread event.data through the trigger pipeline — additive, backward compatible)
[MODIFIED] config/automation_rules.json      ({} -> one new rule; no rules existed before this sprint)
[NEW]      tests/test_p0_6_camera_automation_rules.py
[NEW]      docs/change_impact/camera_automation_p0_6.md
```

Confirmed via `find luno config tests -name "*.py" -newer <P0.5.4-FIX's
own change-impact doc>` — exactly the two `luno/automation/*.py` files
above and the one new test file; zero other `.py` files under `luno/`
changed. `luno/vision.py`, `luno/adapters/vision.py`, `luno/
camera_automation/*.py`, Tapo/RTSP code were never touched — the
investigation confirmed the actual capability gap lived entirely inside
`luno/automation/`, so the brief's "if a production Vision file must
change, STOP and report" escape hatch was never triggered.

## Handover

See `ARCHITECTURE_GUARD.md` §85 and `docs/project_handover.md`
§19jj/20jj for the corresponding entries (added only — no historical
section rewritten).
