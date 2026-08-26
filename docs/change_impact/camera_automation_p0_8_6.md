# LUNO P0.8.6 — End-to-End Human Detection → WLED Reliability Fix

## Summary

Two real-world problems were reported: (1) a single low-confidence YOLO
frame (e.g. `person_confidences=[0.506]`) could directly trigger
`home_assistant.turn_on` on `light.wled` via the existing
`CameraPersonEntered` → `human_detected` → WLED automation rule path,
with zero confidence floor and zero temporal confirmation; (2) Home
Assistant reported `light.wled=on` / "verification success" while the
physical light did not visibly turn on, alongside a log line
`"ignoring device_state_changed for unconfigured entity 'light.wled'"`
that needed investigation.

This sprint traced both problems end to end, fixed the false-positive
path with a new, additive confirmation layer, honestly re-scoped the
WLED verification wording to what it can actually prove, and confirmed
the "unconfigured entity" warning is a pre-existing, by-design, harmless
condition — not a defect.

## Root Cause

### Human Detection

`VisionAdapter._update_person_presence()` (the P0.8.5-era debounce
publishing `CameraPersonEntered`/`CameraPersonLeft`) rises on the FIRST
detection with no confidence floor and no consecutive-cycle
requirement — by design, for the room-level presence signal (a false
negative there is the less-safe failure direction). This signal feeds
`VisionCameraEventBridge` → `CameraEvent(kind="human_detected")`, which
`config/automation_rules.json::camera_human_detected_test_action` (the
real rule that calls `home_assistant.turn_on` on `light.wled`)
previously matched directly, with only `event.kind == "human_detected"`
as its sole condition. A single frame at `person=0.506` (well above the
detection-visibility threshold `CONFIDENCE_THRESHOLD=0.4`, but a
marginal, unconfirmed observation) was therefore sufficient to fire a
real device action.

25 real observed confidences (0.462–0.830) were reviewed. There is a
visible cluster below ~0.55 (single/marginal frames) and a cluster
above ~0.65 (sustained, high-confidence detections) — 0.60 was chosen
as `HUMAN_DETECTION_CONFIDENCE` as the cut between these clusters, not
copied blindly from the brief's own suggested "~0.60".

### WLED

Traced the full command/verification path
(`luno/tool_manager/builtin/real_home_assistant.py::RealHomeAssistantHandler._execute_on_off()`/`_verify_state()`/`_result_data()`):

- `_verify_state()`'s "success" already meant only that Home Assistant's
  own reported/cached entity state (`RealHomeAssistantClient.get_entity_state()`,
  fed by real HA `state_changed` broadcasts via
  `RealHomeAssistantSource._last_states`) matched the requested value —
  it never had, and structurally cannot have, an independent physical
  (e.g. optical) confirmation channel. This is an honest architectural
  limit, not a bug — Luno has no sensor that observes the light itself.
- The wording, however, said "I've turned on {friendly}" without ever
  distinguishing "HA accepted the state change" from "the physical
  device is lit". A user reading "verification success" could
  reasonably infer physical confirmation that was never actually
  possible.
- `_UNAVAILABLE_STATES = (None, "unavailable", "unknown")` already
  correctly fails verification when the entity never leaves an
  unavailable state — this was already correct, not a bug.
- Duplicate-turn_on was already prevented at two independent existing
  layers: `_execute_on_off()`'s own "already in expected state" skip
  (no service call at all), and `AutomationEngine`'s existing 30s rule
  cooldown. No new dedup mechanism was needed.

**"ignoring device_state_changed for unconfigured entity 'light.wled'"**
— traced to `CameraAutomationModule._handle()`, fired when an inbound
`device_state_changed` entity is in neither
`CameraAutomationConfig.entities` (the flat allowlist,
`config/camera_automation.json`, currently empty — all `tapo_c212`
roles are `null`) nor any configured camera role.
`CameraAutomationConfig.entities`'s own docstring states this allowlist
exists precisely so camera_automation does not accidentally react to
"an unrelated light/switch/script state change" — literally naming
"light" as an excluded category. `light.wled` is an OUTPUT device this
package acts ON (via `home_assistant.turn_on`), never something it
needs to listen to inbound FROM. This warning is confirmed **harmless
and by design**, not a defect — no code change was made for this
finding.

## Fix

A new, additive, parallel confirmation layer computed exclusively from
the tracked-cycle loop (`on_vision_cycle()`, the count-and-confidence-
aware loop, per requirement #3 — never the presence-watch loop, which
is structurally confidence-blind):

- `luno/config.py`: `HUMAN_DETECTION_CONFIDENCE` (default `0.60`,
  evidence-derived — see Root Cause above) and
  `HUMAN_DETECTION_CONFIRM_CYCLES` (default `3`, ~1.5s at the default
  `VISION_FPS=2.0`).
- `luno/adapters/events.py`: two new events, `HumanPresenceConfirmed`/
  `HumanPresenceUnconfirmed`.
- `luno/adapters/vision.py::VisionAdapter._update_confirmed_presence()`:
  a NEW state machine (`_human_confirm_streak`/`_human_confirmed`),
  called once per tracked cycle with that cycle's own real person
  confidences. Requires `HUMAN_DETECTION_CONFIRM_CYCLES` CONSECUTIVE
  cycles, each with at least one person at
  `>= HUMAN_DETECTION_CONFIDENCE`, before flipping `human_confirmed`
  True; resets to zero immediately on any single non-qualifying cycle
  (stricter/faster-to-fail than the raw presence debounce it sits on
  top of — intentional, since this gate exists specifically to keep
  physical automation off marginal evidence). Publishes on its own
  rising/falling transitions only.
- **`VisionAdapter._update_person_presence()` and its
  `CameraPersonEntered`/`CameraPersonLeft` contract are completely
  UNCHANGED** — `tests/test_camera_presence.py` (pre-existing, pinned)
  and P0.8.5's own fix/tests remain fully intact. This satisfies
  requirement #2 (reuse existing debounce mechanisms) and requirement
  #3 (never reintroduce the P0.8.5 split-brain — the tracked-cycle loop
  is the sole authoritative source for this new signal, exactly as it
  already is for `person_count`).
- `luno/camera_automation/vision_context.py`/`cameras.py`: additive
  `human_confirmed` field threaded through `VisionContext`/`CameraEvent`.
- `luno/camera_automation/vision_bridge.py`: subscribes to the two new
  events, maps them to `CameraEvent(kind="human_confirmed"/"human_unconfirmed")`
  — a **separate `kind`**, not a second publish of `human_detected`.
  This was required because `CameraAutomationModule._publish_if_not_suppressed()`'s
  dedupe key is `(camera_id, kind)`; re-publishing `kind="human_detected"`
  a second time (once confirmed) would compare equal to the first
  publish's dedupe state and be silently dropped, meaning the WLED rule
  would never see a confirmed event at all. `human_detected`/
  `human_cleared` and every rule listening to them
  (`camera_human_detected_log`, `camera_multiple_people_log`) are
  completely unaffected — full observability of every detection,
  including low-confidence ones, is preserved.
- `config/automation_rules.json::camera_human_detected_test_action`
  (the ONLY rule changed): condition changed from
  `event.kind == "human_detected"` to `event.kind == "human_confirmed"`,
  plus two new conditions `event.available == true` /
  `event.detection_error == null` added — bringing this real-device
  rule up to the same safety bar the P0.8.0 mock test rule
  (`camera_test_automation_safety_action`) already had (a genuine,
  evidence-based gap that is now closed). No other rule was changed.
- `luno/tool_manager/builtin/real_home_assistant.py`: `_verify_state()`'s
  success log line reworded to "HA reports state change accepted (not a
  physical device confirmation)"; `_result_data()` gained an additive
  `verification_scope: "ha_reported_state"` key. No new verification
  mechanism was invented (per explicit brief prohibition) — this is a
  wording/labeling fix on top of an already-correct verification
  implementation.

Class ID/name parsing was independently re-verified as already correct
(`detect_objects_tracked()` reads `model.names[int(cls_id)]` directly
from the model's own metadata; `_normalize_label()`/`_COCO_LABEL_ALIASES`
never rename "person") — no change needed.

## Detection Policy

- `CONFIDENCE_THRESHOLD` (0.4) — UNCHANGED. Still governs the
  tracked-cycle loop's own YOLO `conf=` kwarg (detection visibility).
- `HUMAN_DETECTION_CONFIDENCE` (0.60, new) — the AUTOMATION-safety
  floor. A person must be detected at or above this confidence in a
  qualifying cycle.
- `HUMAN_DETECTION_CONFIRM_CYCLES` (3, new) — a `human_confirmed`
  signal requires this many CONSECUTIVE qualifying cycles (~1.5s at
  default `VISION_FPS=2.0`). A single high-confidence frame alone can
  never confirm presence.
- The debug/diagnostic viewer (`tools/vision_debug_viewer.py`) is
  unaffected — it was never wired to `HUMAN_DETECTION_CONFIDENCE` and
  continues to show every detection, including below-floor ones
  (confirmed by inspection and by this sprint's own regression test
  `test_12_low_confidence_detections_remain_visible_in_status_output`).

## WLED Verification

`_verify_state()`'s "success" means, and has only ever meant, that Home
Assistant's own reported entity state (via `get_entity_state()`,
sourced from real `state_changed` broadcasts) matches the requested
value. This sprint made that scope explicit and honest in both the log
output and the structured result (`verification_scope: "ha_reported_state"`),
per the brief's explicit instruction not to invent a new physical-
verification mechanism. Luno has no independent, physical/optical
confirmation channel for any HA-controlled device — this is a real,
disclosed architectural limit, not a solvable bug in this sprint's
scope. Duplicate-turn_on prevention (already correct, two independent
existing layers) and unavailable-state handling (already correct) were
re-verified, not re-implemented.

`light.wled` ("RGB Strip") is confirmed correctly registered in
`config/lights.config.json` for outbound control. The "unconfigured
entity" warning is a confirmed non-defect (see Root Cause) — no fix was
needed or made.

## Tests

`tests/test_p0_8_6_end_to_end_human_wled_reliability.py` — 25 tests
covering the brief's 20 numbered scenarios (sub-threshold confidence
never confirms; single-cycle-at-floor is candidate-only; sustained
detection confirms exactly once with no duplicate events; confirmed
presence's stricter falling/rising edges vs. the raw P0.8.5 debounce;
multi-person cycles; false-positive-frame sequences; low-confidence
visibility in diagnostics; WLED already-ON/OFF end-to-end via the real
bootstrap + mock HA dispatcher; HA command failure and entity-
unavailable never produce false success; the new `verification_scope`
wording; `light.wled` configuration consistency across
`lights.config.json`/`automation_rules.json`/`camera_automation.json`;
source-level guards proving the P0.8.4 concurrency lock and the P0.8.5
shared-debounce call site were never touched). All 25 pass.

Five pre-existing tests required updates to reflect the intentional
rule/event redesign (the WLED rule's trigger condition changing from
`human_detected` to `human_confirmed`, plus new `available`/
`detection_error` conditions, plus the bridge's new subscribed-event
set): `tests/test_p0_5_3_vision_camera_bridge.py::test_05`,
`tests/test_p0_6_camera_automation_rules.py::test_21`,
`tests/test_p0_6_1_live_log_verification.py::test_12`,
`tests/test_p0_6_2_camera_ha_action.py::test_18`,
`tests/test_p0_6_3_unified_vision_camera_automation.py::test_24`,
`tests/test_p0_7_vision_context.py::test_34`,
`tests/test_p0_8_0_camera_action_safety.py::test_26`/`test_28`. Each
was updated with a docstring explaining exactly why (never silently
weakened) — the underlying invariant each test proves is unchanged;
only the event/kind each test uses to reach that invariant changed, to
match the new, intentional, safer behavior.

## Regression Results

- New suite: 25/25 pass.
- Targeted P0.x/Vision suite (`test_p0_8_6_...`, `test_camera_presence.py`,
  `test_p0_8_5_person_count_sync_fix.py`, `test_p0_8_4_yolo_concurrency_fix.py`,
  `test_p0_8_3_yolo_checkpoint_diagnostics.py`, `test_p0_8_2_human_cleared_light_off.py`,
  `test_p0_8_1*.py`, `test_p0_8_0_camera_action_safety.py`,
  `test_p0_7_vision_context.py`, `test_p0_6*.py`, `test_p0_5*.py`,
  `test_p0_camera_automation.py`, `test_vision_sprint8.py`,
  `test_vision_debug_viewer.py`): 487 passed, 1 pre-existing skip, 0
  failed.
- `luno/tool_manager/tests/test_real_home_assistant_verification.py`:
  39 passed (all "passing" — see Remaining Issues below for an honest
  caveat about this pre-existing file's own assertion style).
- `luno/` fast suite: 818 passed, 2 failed — both the same pre-existing,
  documented FLAKY-KNOWN `luno/barge_in/tests/test_barge_in.py` timing
  tests (unrelated to this sprint, unchanged from every prior sprint's
  baseline).
- Full repository sweep (144 files, chunked, excluding the two
  pre-existing uncollectable files): 4162 passed, 39 failed. Every
  failure maps to an already-documented, pre-existing baseline category:
  `.env`'s `MAX_TOKENS_PARAM=max_tokens` override (12:
  `test_llm_max_completion_tokens_compatibility.py` 7 +
  `test_memory_session_summary_api_compatibility.py` 5),
  `.env`/hardware `MIC_DEVICE_INDEX` gap (6:
  `test_mic_device_index.py`), real credentials configured (1:
  `test_production_launcher.py::test_07_...`), `RealWhisperSource`
  device-index/PortAudio gap (2: `test_real_adapters.py`), accumulated
  `config/backups/` drift on this live-synced folder (18:
  `test_sprint63_long_term_memory_recovery.py`/
  `test_sprint64_memory_corruption_forensics.py`/
  `test_sprint68_mutation_audit_hardening.py`). Zero new failures caused
  by this sprint's changes.

## Real Machine Result

- human detection verified: **NO** — no real camera/YOLO/RTSP execution
  exists in this sandbox. The confirmation-gate logic itself is proven
  correct via 25 passing unit/integration tests against real
  `VisionAdapter`/`TrackedDetection`/`HumanState` objects, not via a
  real detection run.
- false-positive prevention verified: **NO** (same reason — proven at
  the unit/integration level, not on real hardware).
- HA command verified: **NO** — no real Home Assistant instance is
  reachable from this sandbox (confirmed by the same network-isolation
  finding every prior P0.5.4/P0.8.1 sprint already established). The
  end-to-end rule → safety gate → mock HA dispatcher path is proven
  with a `MockHomeAssistantHandler`, never a real HA call.
- WLED physical ON verified: **NO** — never claimed, and cannot be
  claimed from this sandbox. This sprint's own fix is precisely about
  never overclaiming this in production either (see WLED Verification
  above).

`luno_live_p0_8_1_verification.py` was inspected directly (its actual
implementation, not assumed behavior). Its own "PASS" semantics were
already honest before this sprint: `_read_entity_state()` reads
`ha_client.get_entity_state()` — the same HA-reported-state-only scope
this sprint's `verification_scope` field now makes explicit elsewhere —
never a physical confirmation. Its TEST 1–6/TEST A–F sequences observe
a SEPARATE, dedicated P0.8.0/P0.8.2 TEST-ONLY rule/entity
(`camera_test_automation_safety_action`, target =
`CAMERA_AUTOMATION_TEST_LIGHT_ENTITY`, a user-configured test entity —
confirmed via `luno/bootstrap/adapters.py` and `main.py`, never
hardcoded to `light.wled`), not the real WLED-controlling rule
(`camera_human_detected_test_action`) this sprint changed. No code
change to this script was needed for this sprint's fix, and it was not
re-run against real hardware (same sandbox network-isolation limit as
every prior live-verification sprint).

## Files Changed

- `[MODIFIED] luno/config.py` — added `HUMAN_DETECTION_CONFIDENCE`,
  `HUMAN_DETECTION_CONFIRM_CYCLES`.
- `[MODIFIED] luno/adapters/events.py` — added `HumanPresenceConfirmed`,
  `HumanPresenceUnconfirmed`.
- `[MODIFIED] luno/adapters/vision.py` — added
  `_update_confirmed_presence()` + `_human_confirm_streak`/
  `_human_confirmed` state + one additive call in `on_vision_cycle()`;
  `_extra_status()` gained `human_confirmed`.
- `[MODIFIED] luno/camera_automation/vision_context.py` — additive
  `human_confirmed` field.
- `[MODIFIED] luno/camera_automation/cameras.py` — additive
  `human_confirmed` field on `CameraEvent`.
- `[MODIFIED] luno/camera_automation/vision_bridge.py` — two new
  subscriptions + handlers, mapped to `kind="human_confirmed"/"human_unconfirmed"`.
- `[MODIFIED] config/automation_rules.json` — `camera_human_detected_test_action`
  only: condition changed to `human_confirmed` + two new safety
  conditions added.
- `[MODIFIED] luno/tool_manager/builtin/real_home_assistant.py` —
  `_verify_state()` log wording + `_result_data()`'s additive
  `verification_scope` key.
- `[NEW] tests/test_p0_8_6_end_to_end_human_wled_reliability.py` — 25
  tests.
- `[MODIFIED] tests/test_p0_5_3_vision_camera_bridge.py`,
  `tests/test_p0_6_camera_automation_rules.py`,
  `tests/test_p0_6_1_live_log_verification.py`,
  `tests/test_p0_6_2_camera_ha_action.py`,
  `tests/test_p0_6_3_unified_vision_camera_automation.py`,
  `tests/test_p0_7_vision_context.py`,
  `tests/test_p0_8_0_camera_action_safety.py` — updated to reflect the
  intentional rule/event redesign (see Tests above).
- `[NEW] docs/change_impact/camera_automation_p0_8_6.md` (this file).

Zero changes to `luno/vision.py`, YOLO/RTSP/camera/torch code, any
`.pt` file, `AutomationEngine`, any other automation rule, or any
existing safety gate/debounce/cooldown mechanism.

## Remaining Issues

- Physical WLED confirmation cannot be verified from this sandbox and
  was never claimed to be — see Real Machine Result above. This is a
  disclosed architectural limit (no physical sensing channel), not a
  bug this sprint could fix.
- `luno/tool_manager/tests/test_real_home_assistant_verification.py`
  (pre-existing, not touched by this sprint) defines its test functions
  to `return (bool, str)` tuples rather than using `assert` — under
  pytest, a non-`None` return only emits a `PytestReturnNotNoneWarning`,
  it does not fail the test regardless of the returned boolean's value.
  All 39 "pass" in the sense that they ran without raising, but this
  file's own pass/fail signal is not enforced by pytest the way every
  other test file in this project is. This is a pre-existing condition,
  unrelated to and not introduced by this sprint; flagged here for
  honesty rather than silently relied upon as strong evidence.
- The narrow residual race window disclosed in P0.8.5's own
  documentation (the presence-watch loop can still occasionally win a
  transition very near cold start) is unrelated to and unchanged by
  this sprint.
- This sprint's own confirmation-gate values (`HUMAN_DETECTION_CONFIDENCE=0.60`,
  `HUMAN_DETECTION_CONFIRM_CYCLES=3`) are evidence-derived from the 25
  real confidences provided in the brief, not from a larger real-world
  dataset — a future sprint with real, prolonged on-device observation
  could refine these further if the real-machine behavior warrants it.
