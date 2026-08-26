# Camera Automation — P0.6.1: Live Camera → Automation Log-Only Verification

## Objective

Prove that the already-existing real Tapo C212 + Vision + YOLO pipeline
(verified in P0.5.4) can trigger the already-existing P0.6 log-only
automation rule (`camera_human_detected_log`), end to end, with zero
device actions. This sprint adds no new automation behavior — it is a
live-verification sprint, not a feature-expansion sprint.

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
luno/core/tests/test_core.py
```
**251 collected, 251 passed, 0 failed, 0 skipped** — identical to
P0.6's own final "after" count, confirming zero drift since that
sprint closed.

## Runtime inspection (Section 5)

Reconfirmed (not re-derived from scratch — this was already established
in P0.5.4-FIX and has not changed): `main.py` line 66 —
`launcher_config = LauncherConfig.load()` — remains the sole,
unmodified official entry point. `luno/bootstrap/adapters.py` line 166
still gates `RealVisionSource()` construction on
`launcher_config.vision_backend == BACKEND_REAL`, resolved only via
`.load()`. This sprint builds nothing new — it reuses this exact path,
via the same standalone observer script P0.5.4-LIVE/P0.5.4-FIX already
shipped (see below), never a second runtime.

## Why the observer needed extending (Section 15 — code changes were genuinely necessary)

The existing `luno_live_camera_event_observer.py` (from P0.5.4-LIVE,
fixed in P0.5.4-FIX) subscribed only to `camera_automation.camera_event`
and the four raw Vision events. It had **no visibility at all** into
whether `AutomationEngine` actually matched or executed a rule, and no
way to prove zero device actions occurred — exactly the evidence
Sections 7/9/10/12 of this sprint's own brief require. This is a
genuine capability gap, not a preference; per Section 15's own escape
hatch ("if code changes are genuinely necessary… reuse the existing
observer infrastructure… do not create another observer
implementation"), the SAME script was extended rather than building a
second one.

## What was added (all inside `luno_live_camera_event_observer.py` — the one standalone script; zero files under `luno/` touched)

1. **Rule-loaded/enabled pre-check (Section 7):** after `runtime.start()`,
   calls the engine's own pre-existing, public, read-only
   `AutomationEngine.get_automation_status("camera_human_detected_log")`
   accessor (Sprint 72's own API — not new) and prints
   `loaded=YES/NO` / `enabled=YES/NO`. If the rule is missing or
   disabled, prints a clear STOP message and skips the observation
   window entirely — never edits `config/automation_rules.json`
   automatically to force a pass.
2. **`_LiveObserver.on_automation_event(outcome)`:** subscribes to
   `automation.triggered`/`automation.condition_passed`/`automation.
   completed`/`automation.skipped`/`automation.failed`, counting each
   ONLY when `data["rule_id"] == "camera_human_detected_log"` — the one
   rule this sprint line ships; every other rule id (should one ever
   exist) is silently ignored.
3. **`_LiveObserver.on_tool_requested()`:** counts every `tool_requested`
   event during the window (Section 12's device-action safety proof).
   Honest limitation documented in the script's own docstring and
   below: `automation.log` is structurally incapable of publishing one
   (`_dispatch_internal_action()` never calls `_dispatch_tool_call()`),
   so this rule specifically can never be the source of one — but this
   script cannot attribute an observed `tool_requested` back to a
   specific rule execution from outside the engine without deeper
   instrumentation this sprint was not asked to add.
4. **Evidence block (Sections 9/10):** the final summary now prints
   three separate, never-conflated count groups — Vision raw event
   counts, `camera_automation.camera_event` kind counts, and
   AutomationEngine outcome counts for `camera_human_detected_log` —
   plus the `tool_requested` total, exactly matching this sprint's own
   worked evidence format.

Nothing else changed. `VisionAdapter`, `RealVisionSource`, YOLO, RTSP
handling, Tapo integration, `CameraAutomationModule`, `vision_bridge.py`,
Event Bus semantics, and the P0.6 `AutomationEngine`/`conditions.py`
change itself were all read but never modified this sprint — reused
exactly as they exist.

## Live test sequence (Section 8 — as specified)

Tests A–E (Idle / Enter / Stay / Leave / Re-enter) require walking in
front of the real Tapo C212 while the script runs on the real machine.
**I cannot perform this myself** — my own tool execution always occurs
in an isolated cloud sandbox with no network route to the camera,
confirmed again this sprint (unchanged since every prior sprint in this
line): running `python luno_live_camera_event_observer.py --duration 1`
from this sandbox still hard-stops at the pre-existing pre-flight check
(`Camera reachable (TCP 443): FAIL — Network is unreachable`,
`ultralytics (YOLO) importable: FAIL — not installed`) before ever
reaching the new rule-check/observation code. This is a structural
property of my execution environment, not a defect in this sprint's
work.

## What WAS verified (real bootstrap + real Event Bus + real engine + real shipped rule; simulated events — not hardware)

A real-bootstrap smoke test (`tests/test_p0_6_1_live_log_verification.py
::test_12`/`::test_13`, also run manually and reproduced below) publishes
simulated `camera_automation.camera_event`s through the real Event Bus
into the real, unmodified `AutomationEngine` loaded from the real
`config/automation_rules.json`:

```
rule status: enabled=True
--- (human_detected published) ---
automation_event_counts: {'triggered': 2, 'condition_passed': 1, 'completed': 1, 'skipped': 1}
tool_requested_count: 0
camera_events kinds: ['human_detected', 'human_cleared']
```

This proves the wiring this sprint added is correct and that the
distinguish-counts requirement (Section 10) behaves as expected:
`human_detected` -> `triggered=1, condition_passed=1, completed=1`;
`human_cleared` -> `triggered=1, skipped=1` (never `completed`) — the
negative test (Section 11) holds. `tool_requested_count: 0` in both
cases — the device-action safety proof. **None of this is, or is
presented as, real Tapo C212 hardware evidence.**

## Required evidence format (Section 9) — reproduced from the simulated run above

```
--- LIVE P0.6.1 RESULT ---
Vision (raw, count only):
    camera_person_entered: 0   (not exercised by this simulated smoke test)
    camera_person_left: 0
    camera_disconnected: 0
    camera_reconnected: 0
camera_automation.camera_event:
    human_detected: 1
    human_cleared: 1
    camera_online: 0
    camera_offline: 0
automation rule ('camera_human_detected_log'):
    loaded: YES
    enabled: YES
    triggered: 2
    condition_passed: 1
    completed: 1
    skipped: 1
    failed: 0
device actions:
    tool_requested (any tool, see attribution limitation above): 0
    Home Assistant / PTZ / other device actions: not independently observable from
    outside the engine beyond the tool_requested count above - automation.log is
    structurally incapable of reaching them (see module docstring).
```

The real, hardware-driven version of this exact block is what the user
must produce by running the script themselves (see "Next step" below).

## Tests

New file: `tests/test_p0_6_1_live_log_verification.py` — 15 tests.
Static/shape checks (tracked rule id matches the shipped rule, new
observer attributes exist, `on_tool_requested`/`on_automation_event`
never touch raw event data inappropriately, `_dispatch_internal_action`
structurally never calls `_dispatch_tool_call`), plus two real-bootstrap
simulated-event tests (`test_12`/`test_13`) reproducing the evidence
above end to end. No test claims real hardware verification.

**Before:** 251 passed (P0.6 baseline, reconfirmed this sprint).
**After: 266 passed, 0 failed (251 + 15).**

## Device-action safety proof (Section 12)

Structural: `automation.log` (the rule's only action) is in
`AutomationEngine._INTERNAL_ACTION_TYPES` and
`_dispatch_internal_action()` never calls `_dispatch_tool_call()` — the
only method in the engine that ever publishes `tool_requested`.
Re-confirmed this sprint by static source inspection
(`test_09_automation_log_action_cannot_reach_dispatch_tool_call`), not
merely re-asserted from memory. Behaviorally, the simulated-event tests
above additionally observed `tool_requested_count: 0` for every
`human_detected`/`human_cleared`/`camera_online`/`camera_offline` event
published during those tests.

**Honest limitation (documented, not glossed over):** the observer's
`tool_requested` count is a total, not attributable to a specific rule
execution from outside the engine — if a real live run ever shows a
nonzero count, it must be cross-referenced against the console log
timestamp to determine origin (it cannot originate from
`camera_human_detected_log` itself, per the structural proof above, but
could originate from something else entirely, e.g. a manual command
given during the same window).

## Result classification (Section 17)

**BLOCKED** — for my own attempt. Hardware/network access is
structurally unavailable from this execution environment (re-confirmed
this sprint, not assumed). Per this sprint's own explicit instruction,
this is never reported as PASS, and no simulated-event result is
reported as a LIVE PASS anywhere in this document.

The tool is now ready and tested for the user to obtain a real PASS/
PARTIAL/FAIL classification themselves — see "Next step."

## No config mutation (Section 14)

Confirmed via `find . -newer <P0.6's own change-impact doc>`:
`config/automation_rules.json`, `config/camera_automation.json`, and
`.env` do not appear in the changed-file list. The only config-adjacent
touches present are `config/relationship_state.json` and
`config/vision_memory.sqlite3*` — incidental side effects of running
this repository's own unrelated test suites during the regression gate
(the planner/vision-memory tests write their own runtime state; same
behavior observed and documented in P0.6's own diff audit), not
anything this sprint intentionally wrote to.

## Interruption safety (Section 13)

Not re-tested this sprint (unchanged from P0.5.4-LIVE — the script's
`finally:` block, which always calls `ShutdownCoordinator(...).shutdown()`
regardless of `KeyboardInterrupt`, was not touched by this sprint's
diff). No new risk introduced.

## Diff audit (Section 20)

```text
[MODIFIED] luno_live_camera_event_observer.py         (root-level standalone script - not under luno/)
[NEW]      tests/test_p0_6_1_live_log_verification.py
[NEW]      docs/change_impact/camera_automation_p0_6_1.md
```

Confirmed via `find luno -name "*.py" -newer <P0.6's own change-impact
doc>` — **zero results**. Production code (`luno/` package) changed: 0,
exactly as this sprint's own "ideal result" asked for. The one file
modified is the same reusable, standalone observer script this project
has extended once before (P0.5.4-FIX), explicitly anticipated and
permitted by this sprint's own Section 15.

## Known limitations

- Live hardware verification itself remains NOT PERFORMED by the agent
  — structurally impossible from this execution environment, as in
  every prior sprint in this line.
- `tool_requested` count is total, not per-rule-attributable from
  outside the engine (documented above).
- Camera identity (`camera_id`) is still not independently verified
  (unchanged from P0.5.1–P0.6 — out of scope for this sprint).

## Next step (for the user)

```
python luno_live_camera_event_observer.py --duration 120
```

Watch for, in order: `vision backend: real`, then
`automation rule 'camera_human_detected_log': loaded=YES enabled=YES`.
Then perform Tests A–E from Section 8 (idle, enter, stay, leave,
re-enter). Copy back the final `--- LIVE P0.6.1 RESULT ---` block —
that is the real evidence this sprint's brief asks for, and it is the
one thing I remain structurally unable to produce myself.
