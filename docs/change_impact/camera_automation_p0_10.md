# P0.10 - Occupancy-Aware Automation Intelligence

**Filename note:** this document is intentionally named
`camera_automation_p0_10.md` (the `camera_automation` prefix this
project's change-impact docs have used since P0.6), per the user's own
explicit sprint brief, even though this sprint's actual subject is the
Room Occupancy layer (`luno/vision_occupancy.py`) and `AutomationEngine`
context wiring, not `luno/camera_automation/` itself (which is untouched
by this sprint).

## 1. Context

P0.9 built `RoomOccupancyModule` - a thin, additive, purely
OBSERVATIONAL state layer deriving canonical `vacant`/`occupied` room
state, `person_count`, and presence-duration timestamps strictly from
the EXISTING confirmed human-presence pipeline (`HumanPresenceConfirmed`
/ `CameraPersonLeft` / `VisionFrameProcessed` - all P0.8.x/Sprint-8,
unmodified). It never controls a device and never runs a second
detection pipeline.

P0.10 makes that state layer *usable* by the automation system, without
letting automation decisions leak into the occupancy layer itself:

    YOLO detects -> Vision confirms -> Occupancy remembers -> Automation decides -> HA executes

Nothing about YOLO, RTSP, human-confirmation thresholds,
`_publish_if_not_suppressed`, the P0.8.8 dedupe fix, or the existing
WLED ON/OFF rules is touched by this sprint.

## 2. What changed

### 2.1 `luno/vision_occupancy.py` (Phase 2 + Phase 4)

`RoomOccupancySnapshot` (frozen dataclass) gained two new READ-ONLY
fields, additive to the six P0.9 already shipped:

- `occupancy_age_seconds: float` - time in the CURRENT state, either
  direction. While occupied this is identical to
  `presence_duration_seconds` (both measure from the same
  `occupied_since` instant). While vacant it is genuinely DIFFERENT: it
  measures time since `vacant_since`, and keeps moving - unlike
  `presence_duration_seconds`, which freezes at the final visit length
  the moment the room becomes vacant. This is the "how long has the
  room been empty" metric `presence_duration_seconds` cannot express.
- `last_transition: Optional[str]` - `"occupied"` / `"vacant"` / `None`
  (never fabricated for a fresh instance - `None` until the first
  genuine transition).

`RoomOccupancyModule` gained two new private instance attributes
(`_last_transition_monotonic`, `_last_transition`), set only on a
GENUINE state transition inside `_on_human_confirmed()`/
`_on_human_cleared()` - never on a person-count-only
`VisionFrameProcessed` update (Phase 2's own "person-count changes must
never reset `occupied_since`" requirement, extended identically to the
new `occupancy_age_seconds`/`last_transition` fields).

`_publish_transition()` gained a `previous_state: str` parameter
(Phase 4), included in both the `room_occupied`/`room_vacant` and the
`occupancy_changed` event payloads as `payload["previous_state"]`.
Example `room_occupied` payload now includes both `state="occupied"`
and `previous_state="vacant"`; a `room_vacant` payload includes both
`state="vacant"` and `previous_state="occupied"`.

Everything else in this file - the state machine itself, the three
subscribed event types, the two-clock discipline (`time.monotonic()`
for durations, `utcnow()` for human-readable timestamps only), the
"never controls a device" boundary - is byte-for-byte unchanged from
P0.9.

### 2.2 `luno/bootstrap/modules.py` (Phase 3)

`room_occupancy_module = RoomOccupancyModule()` was moved earlier in
the function (now constructed immediately before `automation_engine`,
where P0.9 originally placed it further down) so that
`AutomationEngine`'s `state_readers` dict can close over the REAL
instance. This is still the exact same single instance - registered
into `runtime`, bound to the event bus, and returned in the modules
dict exactly as P0.9 left it; only its line position moved.

`automation_engine = AutomationEngine(state_readers={...})` gained five
new entries, each a zero-argument lambda reading
`room_occupancy_module.get_snapshot()` - the SAME defensive/immutable
snapshot API P0.9 built, reused rather than duplicated:

```python
automation_engine = AutomationEngine(state_readers={
    "camera_patrol": lambda: camera_patrol_module.get_status().get("state"),
    "occupancy.state": lambda: room_occupancy_module.get_snapshot().state,
    "occupancy.person_count": lambda: room_occupancy_module.get_snapshot().person_count,
    "occupancy.presence_duration_seconds": lambda: room_occupancy_module.get_snapshot().presence_duration_seconds,
    "occupancy.occupancy_age_seconds": lambda: room_occupancy_module.get_snapshot().occupancy_age_seconds,
    "occupancy.last_transition": lambda: room_occupancy_module.get_snapshot().last_transition,
})
```

This is `AutomationEngine`'s EXISTING, already-established context
mechanism (`evaluate_condition()`'s `state_readers` lookup, see
`luno/automation/conditions.py`) - the identical mechanism
`"camera_patrol"` already used before this sprint. No second occupancy
state machine was created; no direct device control was added to
`RoomOccupancyModule`.

### 2.3 `config/automation_rules.json` (Phase 5)

Two new rules, both `automation.log`-only (no `home_assistant.*`
action), appended after the existing six:

- `occupancy_test_log` - trigger `event:room_occupied`, condition
  `occupancy.state == "occupied"`.
- `occupancy_long_presence_test` - trigger `event:occupancy_changed`,
  conditions `occupancy.state == "occupied"` AND
  `occupancy.presence_duration_seconds >= 30`.

Neither rule controls a real device. The five pre-existing rules
(`camera_human_detected_log`, `camera_human_detected_test_action`,
`camera_multiple_people_log`, `camera_test_automation_safety_action`,
`camera_test_automation_safety_action_off`) and the one P0.8.9 rule
(`camera_wled_human_cleared_off`) are byte-for-byte unchanged.

### 2.4 `tests/test_p0_8_9_wled_off_debounce.py` (pre-existing test updated)

`test_B7_real_rules_file_has_exactly_six_rules` hardcoded the shipped
rules file's key set. Since P0.10 (Phase 5) intentionally, additively
ships two new rules, this test's assertion was updated to the new,
correct eight-rule set. This is the ONLY pre-existing test file this
sprint modified, and the change is a deliberate reflection of an
intentional, brief-mandated addition - not a behavior fix. The test's
own name and docstring were left in place (documented instead of
silently renamed) with a P0.10 note explaining the discrepancy.

### 2.5 `tests/test_p0_10_occupancy_context.py` (new, 44 tests)

Sections A-U (see file's own module docstring for the full list):
snapshot schema, defensive/immutable snapshot, vacant/occupied context,
duration calculation (including the `occupancy_age_seconds` vs.
`presence_duration_seconds` divergence while vacant), monotonic clock
discipline (structural + behavioral), transition direction tracking
(`last_transition`), multi-person transition stability, event payload
`previous_state` (both `room_occupied`/`room_vacant` and
`occupancy_changed`), duplicate-transition prevention,
`AutomationEngine` context access (both a direct closure test and a
static bootstrap-ordering guard), `occupancy.state`/
`occupancy.person_count`/duration conditions via
`evaluate_condition()`, the two shipped diagnostic rules end-to-end
against the REAL `config/automation_rules.json`, WLED ON/OFF regression
(real bootstrap, real Event Bus, mocked HA backend, occupancy events
coexisting), restart semantics, and architecture guards (no device
control, no `AutomationEngine` import, single canonical occupancy
owner, no second YOLO inference).

## 3. What did NOT change

- YOLO detection, confidence thresholds, RTSP/camera acquisition -
  untouched.
- `VisionAdapter`'s confirmation/debounce logic
  (`HumanPresenceConfirmed`/`CameraPersonLeft`/`HumanPresenceUnconfirmed`)
  - untouched.
- `_publish_if_not_suppressed()` and the P0.8.8 dedupe fix - untouched.
- The existing WLED ON rule (`camera_human_detected_test_action`) and
  OFF rule (`camera_wled_human_cleared_off`, P0.8.9's real 10s-delay
  debounce/cancellation mechanism) - untouched, and directly
  re-verified in this sprint's own Section S tests to still fire
  exactly once each, with occupancy events coexisting on the same real
  Event Bus.
- `RoomOccupancyModule`'s own state machine, event subscriptions, and
  "never controls a device" boundary - unchanged from P0.9 (Section U's
  architecture guards re-verify this).
- `luno/camera_automation/` - not opened.

## 4. Root cause / design notes

This sprint discovered no defect - it is a pure feature addition on top
of P0.9's already-correct state layer. One honest architectural
observation surfaced during test design (Section R3, documented here
rather than silently worked around): `occupancy_long_presence_test`'s
condition shape (`occupancy_changed` trigger + `presence_duration_seconds
>= 30`) has a narrow-to-nonexistent NATURAL firing window under P0.9/
P0.10's current event semantics, because `occupancy_changed` only ever
fires AT a transition instant - duration is ~0 the moment the room
becomes occupied, and `occupancy.state` is no longer `"occupied"` the
moment it becomes vacant. The rule was still shipped exactly as
specified (Phase 5 mandated this exact shape as a diagnostic-mechanism
proof, not a claim that it fires routinely), and the mechanism itself
(condition evaluation against live `occupancy.*` state_readers) is
proven correct by a dedicated test. See Known Limitations below.

## 5. Tests

`tests/test_p0_10_occupancy_context.py` - 44 tests, all passing.
`tests/test_p0_9_room_occupancy.py` - 34 tests, all still passing
unmodified (P0.10's additive dataclass fields are fully backward
compatible). `tests/test_sprint72_automation_engine.py` - 78 tests,
all still passing unmodified.

## 6. Regression

Full repository sweep (156 files, chunked 8-way, `-n 4`): 4,402 passed,
1 skipped, 43 failed. Every failure traced to an already-documented
pre-existing category:

- `.env` `max_completion_tokens`/`MAX_TOKENS_PARAM` override
  (`test_llm_max_completion_tokens_compatibility.py`,
  `test_memory_session_summary_api_compatibility.py`).
- No-audio-hardware sandbox gap (`test_mic_device_index.py`).
- Whisper `_device_index` construction gap / blocked network egress
  (`test_real_adapters.py`, `test_production_launcher.py::test_07`).
- The `.env` `CAMERA_AUTOMATION_ENABLED=true` condition first documented
  in P0.8.9 (`test_p0_camera_automation.py::test_15`,
  `test_p0_5_camera_integration.py::test_35`,
  `test_p0_5_3_vision_camera_bridge.py::test_22`).
- Real `config/lights.config.json` `light.main_light` drift vs. old
  Sprint 60 fixture expectations (`test_sprint60_area_schema.py`).
- `config/backups/`/`vision_memory.sqlite3-wal`/`-shm` forensic drift
  (`test_sprint63_long_term_memory_recovery.py`,
  `test_sprint64_memory_corruption_forensics.py`,
  `test_sprint68_mutation_audit_hardening.py`).
- One parallel-load-only timing flake
  (`test_streaming_e2e.py::test_D_barge_in_between_llm_and_tts_chunk_never_plays`),
  confirmed clean 3/3 in isolation.

Zero failures touch `luno/vision_occupancy.py`,
`luno/automation/engine.py`, `luno/automation/conditions.py`,
`luno/bootstrap/modules.py`, `config/automation_rules.json`, or any
file this sprint modified/created, aside from the one intentional,
documented `test_B7` update above (which now passes).

## 7. Documentation updated

`ARCHITECTURE_GUARD.md` (new numbered section),
`docs/testing/regression_baseline.md` (new `## LUNO P0.10` section),
`docs/project_handover.md`, `docs/project_handover.json`
(`architecture_version`, `completed_sprints`, `modified_files`,
`created_files`).

## 8. Known limitations

- `occupancy_long_presence_test`'s exact condition combination
  (`occupancy_changed` + `presence_duration_seconds >= 30` while still
  `occupied`) has a narrow-to-nonexistent natural firing window under
  the current event semantics, as explained in Section 4 above. The
  mechanism is correct and tested (Section R3); the specific example
  rule shape, as literally specified in the brief, will rarely if ever
  fire on its own in production without a future sprint adding a
  periodic/duration-triggered re-evaluation (explicitly out of scope
  here - Rule 10 forbids introducing new scheduling/persistence this
  sprint).
- Real Home Assistant hardware and physical WLED behavior were NOT
  exercised this sprint (same structural sandbox limitation as every
  prior sprint in this line - no network route to a real HA instance).
  Every test in this sprint routes through `MockHomeAssistantHandler`.
  Section S's WLED regression tests prove the ON/OFF rules still
  dispatch the correct MOCK tool call exactly once each; they do not
  and cannot prove a physical light actually changed state.
- No before/after performance numbers were measured - the five new
  `state_readers` lambdas are simple attribute reads on an already
  in-memory snapshot; no real camera/HA load exists in this sandbox to
  measure against.
