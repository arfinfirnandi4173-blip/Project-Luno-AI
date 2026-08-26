# LUNO P0.7 — Vision Context → Automation Context

## 1. Objective (restated)

Give `AutomationEngine` conditions a normalized, inspectable view of
"what does Vision currently see" (`human_present`/`person_count`/
`detected_objects`/`available`/`detection_error`) derived ONLY from
existing Vision data — no second inference pipeline, no new
high-frequency event, no disk persistence — and prove it with one new,
log-only example rule.

## 2. Baseline

Chunked full-suite run (10 files/chunk, plus the two isolated groups
this project's own convention already establishes — see Section 9
below): **3,978 passed** across the main chunks + `test_vision_
sprint8.py` (32, isolated) + the sprint69 pair (37, isolated), before
any P0.7-authored test file is added. All pre-existing failures found
during this run are documented in Section 9 and were independently
confirmed to be pre-existing (several already logged in `project_
handover.md` §22 as far back as the Dashboard Startup & Access Recovery
sprint).

## 3. Audit — `event.<field>` condition mechanism (Section 3 of the brief)

Read directly from `luno/automation/conditions.py`/`models.py`:

- `evaluate_condition()` already resolves any `condition.target` starting
  with the literal prefix `"event."` from the triggering event's own
  `.data` dict (P0.6) — no new mechanism needed to expose `VisionContext`
  fields to a rule; they only need to land in a `camera_automation.
  camera_event`'s `.data` dict, which `CameraEvent.to_dict()` already
  produces.
- `CONDITION_TYPES` already had `equals`/`not_equals`/`greater_than`/
  `less_than`/`contains`/`state_is` — enough to express `event.
  human_present == true`, `event.detected_objects contains "person"` with
  zero new code. Only `event.person_count >= 2` (Section 8's own worked
  example) had no existing operator — `greater_than`/`less_than` are both
  strict, and there is no negation/combinator mechanism to express "not
  less than." **The one new operator this sprint adds: `greater_equal`.**
  No `less_equal` — nothing in this sprint's brief or tests needs it
  (Section 8's own "implement only the smallest extension necessary").

## 4. Design — `VisionContext` (Section 4)

New, isolated, pure module: `luno/camera_automation/vision_context.py`.

- `VisionContext` — frozen dataclass: `camera_id`, `timestamp`,
  `available: Optional[bool]`, `human_present: bool`, `person_count:
  int`, `detected_objects: Tuple[str, ...]`, `detection_error:
  Optional[str]`. No raw frames, no images, no credentials, no RTSP
  URLs (Section 4's own explicit "avoid" list) — safe to embed directly
  in an Event Bus payload.
- `build_vision_context(camera_id, status, detection_error=None)` — a
  pure function. `status` is whatever `adapter_manager.status_all()
  .get("vision")` currently returns — the SAME public snapshot `luno/
  dashboard/collectors.py::collect_vision()` already reads. `None`/`{}`/
  malformed input degrades to an honest "unavailable, nothing known"
  default rather than raising, matching this project's own established
  convention (`cameras.load_camera_profiles`).
- `normalize_object_label(raw)` — case/whitespace normalization only
  (`"Person"`/`" PERSON "` → `"person"`), never re-maps one COCO class to
  another (that is `luno.vision._normalize_label()`'s own, separate,
  pre-existing job).

**Field provenance, not invented:** `human_present`/`person_count` come
from `status["human_count"]` — the Sprint 8 tracked-cycle loop's own
per-track `HumanState` count, the SAME data the dashboard's `human_count`
already shows. `detected_objects` comes from `status["objects"]`'s own
`label` fields, normalized only. `available` comes from `status
["camera_connected"]` unmodified. Nothing here re-derives a value the
architecture doesn't already honestly compute somewhere.

## 5. State preservation on detector failure (Section 5/12 — the core safety requirement)

`build_vision_context()` NEVER derives `human_present`/`person_count`
from a fresh "assume nobody" default when `detection_error` is set — it
simply passes through whatever `status["human_count"]`/`status
["objects"]` says, which is already the dashboard's own "last known
good" reading (the tracked-cycle loop's own state is not reset just
because a *different* loop — the presence-watch loop — happened to fail
this cycle; see Section 7 below for exactly which loop `detection_error`
can represent). Verified directly: `tests/test_p0_7_vision_context.py::
test_09_detection_error_does_not_zero_human_present_or_person_count` and
`test_15_system_error_tracked_then_threaded_into_next_ingest_then_
cleared` (end-to-end, through `VisionCameraEventBridge`).

## 6. Object labels — no invented classes (Section 6)

`normalize_object_label()` only lowercases/strips; it never adds a class
name the detector didn't produce. Verified: `test_04_build_vision_
context_normal_status` (dedup + sort), `test_05_build_vision_context_
ignores_malformed_object_entries` (non-dict/no-label entries silently
skipped, never fabricated).

## 7. `CameraEvent` extension (Section 7) — additive, not a new event type

`luno/camera_automation/cameras.py::CameraEvent` gained 5 new **optional**
fields (`human_present`, `person_count`, `detected_objects`, `available`,
`detection_error`) with safe defaults (`None`/`None`/`()`/`None`/`None`).
Every pre-P0.7 (HA-sourced) `CameraEvent` construction site never passes
these — confirmed unaffected: `test_18_camera_event_p0_7_fields_
default_to_none_or_empty_for_ha_sourced_path`. This is deliberately NOT a
new event type (Section 9's own "avoid high-frequency automation
events" warning) — `VisionContext` rides along ONLY on the four
already-existing, already-debounced events `VisionCameraEventBridge`
already publishes (`human_detected`/`human_cleared`/`camera_offline`/
`camera_online`), never a new polling-driven event.

## 8. Wiring — `VisionCameraEventBridge` (Section 5/11)

- New `vision_status_reader: Optional[Callable[[], Dict[str, Any]]]`
  public attribute (plain attribute, not a constructor parameter — same
  "wired post-construction" convention `planner_module.device_intent_
  client` already established). Left `None` by default; `_ingest()`
  degrades to `build_vision_context()`'s own honest default rather than
  raising when it's unwired (tests, or `main.py` hasn't reached
  `register_vision_context_reader()` yet).
- New subscription to the EXISTING, generic `system_error` Event Bus
  event, filtered to `data.get("adapter") == "vision" and data.get
  ("error_type") == "vision_detection_failed"` — the SAME signal
  P0.6.2-FIX/P0.6.3 already established for the tracked-cycle and
  presence-watch loops respectively. Tracked into `self._last_detection_
  error` until the next `_ingest()` reads it into a `VisionContext` and
  clears it. An unrelated `system_error` (different adapter, different
  `error_type`) is never tracked — `test_16_unrelated_system_error_
  type_never_tracked`.
- `_ingest()` now builds a `VisionContext` (via `vision_status_reader()`,
  or `None` if unwired) BEFORE constructing the `CameraEvent`, and
  attaches its 5 fields. No new debounce/timer logic added — `_ingest()`
  remains the same direct, unconditional translation P0.5.3 established
  (re-confirmed: `test_19_vision_bridge_never_imports_vision_yolo_
  rtsp_code`).

**Bootstrap wiring (Section 11):** `luno/bootstrap/adapters.py::
register_vision_context_reader(modules, adapters)` — same "needs output
from BOTH `register_all_modules()` and `register_all_adapters()`" post-hoc
pattern as `register_device_intent_classifier()`/`register_session_
summary_client()`, for the identical reason: `vision_camera_event_bridge`
is constructed inside `register_all_modules()`, but `adapter_manager`
(whose `status_all()` is the ONLY source `VisionContext` is built from)
does not exist until `register_all_adapters()` runs afterward. No-ops
harmlessly (`bridge is None or adapter_manager is None: return`) — no
failure mode that could crash startup. Called from `main.py`, right after
`register_intent_classifier(modules, adapters)`. Verified: `test_32_
register_vision_context_reader_noop_when_bridge_or_adapter_manager_
missing`.

## 9. New condition operator + example rule (Section 8/13)

- `luno/automation/models.py::CONDITION_TYPES` — added `"greater_equal"`
  (the ONLY new operator, see Section 3 above).
- `luno/automation/conditions.py::evaluate_condition()` — new branch,
  same `TypeError`-fails-closed contract every other comparison operator
  here already has (`current >= condition.value`, non-numeric `current`
  falls through to `CONDITION_INVALID`, never raises past this function).
- `config/automation_rules.json` — one new rule,
  `camera_multiple_people_log`: trigger `event:camera_automation.
  camera_event`, conditions `event.kind == "human_detected"` AND
  `event.person_count >= 2`, action `automation.log` only (no lights, no
  switches, no PTZ, no locks). The `kind == "human_detected"` condition
  is paired with the new `greater_equal` check (AND semantics, `engine.py`
  `_evaluate_conditions()`'s own pre-existing "ALL conditions must pass"
  contract) so the rule only fires on the actual enter transition, never
  on a `camera_offline`/`camera_online`/`human_cleared` event that might
  carry a stale `person_count >= 2` from a prior cycle — verified:
  `test_30_multiple_people_rule_does_not_fire_on_human_cleared_even_
  if_stale_count_high`.

**Deliberate scope decision:** no new "vision context changed" event
type was added. `VisionContext` rides along only on the four pre-existing
events — never a new polling-driven event (Section 9's own warning). This
means an object appearing/disappearing (or the person count changing)
WHILE a person is already continuously present will not, by itself,
retrigger any automation in P0.7 — only a fresh Enter/Leave/Disconnect/
Reconnect transition carries a refreshed `VisionContext`. This is an
intentional, documented limitation (Section 24's own "next sprint"
guidance applies here — see Section 12 below).

## 10. Tests

`tests/test_p0_7_vision_context.py` — 40 tests:

- **A. `VisionContext`/`build_vision_context`/`normalize_object_label`**
  (8) — pure unit coverage: `None`/`{}`/malformed status degrade safely;
  normal status normalizes/dedupes/sorts objects and clamps a
  negative/non-numeric `human_count` to 0; `to_dict()` shape; frozen
  immutability.
- **B. State preservation on detector failure** (3) — the Section 5/12
  core contract, both with and without a status snapshot present.
- **C. `VisionCameraEventBridge` wiring** (9) — reader defaults to
  `None`; ingest without a reader degrades honestly; ingest with a
  reader attaches all 5 fields correctly; `system_error` tracking/
  threading/clearing across ingests (using a third, distinct event kind
  to avoid confusing `CameraAutomationModule`'s own pre-existing `(camera_
  id, kind)` dedupe with a bridge bug — see the test's own comment);
  unrelated `system_error` never tracked; `CameraEvent.to_dict()`
  includes the new fields; the HA-sourced path's fields default to
  `None`/`()`; re-confirms the vision_bridge/vision_context modules still
  import zero cv2/ultralytics/YOLO/RTSP code; confirms `vision_context.py`
  has no Event Bus subscription of its own (not a second polling loop).
- **D. `greater_equal` operator** (6) — in `CONDITION_TYPES`; true on
  equal/greater; false on less; `CONDITION_INVALID` on a type mismatch or
  a missing `event.*` field.
- **E. `camera_multiple_people_log` rule** (5) — real bootstrap stack,
  end to end: fires when `person_count >= 2` on `human_detected`; does
  NOT fire when `person_count < 2`; does NOT fire on `human_cleared` even
  with a stale high count; is log-only (no device action type in its
  `actions`); `register_vision_context_reader()` no-ops safely when
  unwired.
- **F. Regression** (4) — the three pre-existing P0.6/P0.6.2/P0.6.3 rules/
  behaviors (`camera_human_detected_log`, `camera_human_detected_test_
  action`, `camera_offline`/`camera_online` delivery) remain unaffected;
  `config/automation_rules.json` now has exactly 3 keys, with both
  original rules byte-for-byte unchanged in shape.
- **G. Architecture guard** (3) — `vision_context.py` never opens a file/
  writes JSON/uses sqlite3/pickle (no persistence); no RTSP URL/password/
  raw-frame/cv2 reference anywhere in it; no `while True`/`threading.
  Thread`/scheduler introduced by either new/modified file (no new
  polling loop).

## 11. Regression (Section 21)

Full chunked sweep (10 roughly-equal file groups + the two isolated
groups, per this project's own established convention for fitting inside
the sandbox's per-tool-call wall-clock cap):

- **All 10 main chunks: 3,978 → 4,018 passed** (+40 new from `tests/
  test_p0_7_vision_context.py`), plus the two isolated groups unchanged
  (`test_vision_sprint8.py`: 32/32; sprint69 pair: 37/37).
- **Three genuine, EXPECTED test-staleness regressions found and fixed**
  (pre-existing tests whose hardcoded assumptions were correctly
  invalidated by this sprint's own intentional additive changes — same
  category of fix P0.6.2-FIX/P0.6.3 already established as acceptable
  when the underlying change is deliberate):
  1. `tests/test_p0_5_3_vision_camera_bridge.py::test_05_unknown_
     event_type_never_reaches_bridge_and_is_ignored` — asserted the
     bridge subscribes to exactly the original four event types; updated
     to include the new, intentional `system_error` subscription (a real,
     named event type, not a wildcard — the "does not subscribe to
     anything broader" guarantee this test checks still holds).
  2. `tests/test_p0_6_3_unified_vision_camera_automation.py::test_26_
     automation_rules_file_unchanged_this_sprint` — "this sprint" refers
     to P0.6.3 (which genuinely touched zero lines of that file); updated
     to assert the two P0.6/P0.6.2 rules remain a subset, present and
     byte-for-byte unchanged, rather than asserting no later sprint may
     ever add a new rule.
  3. `tests/test_sprint72_automation_engine.py::test_15_allowlists_are_
     exactly_the_documented_sets` — asserted `CONDITION_TYPES` was
     exactly the pre-P0.7 6-member set; updated to include the new
     `greater_equal` operator (Section 9 above).
- **Pre-existing failures found, confirmed unrelated to P0.7, NOT
  fixed** (all independently traced to files/behavior P0.7 never
  touches — `luno/camera_automation/*`, `luno/automation/models.py`,
  `luno/automation/conditions.py`, `luno/bootstrap/adapters.py`,
  `main.py`, `config/automation_rules.json`):
  - `tests/test_llm_max_completion_tokens_compatibility.py` (7) +
    `tests/test_memory_session_summary_api_compatibility.py` (5) — this
    checkout's own `.env` sets `MAX_TOKENS_PARAM=max_tokens`, which the
    code's own default (`max_completion_tokens`) the tests hardcode
    disagrees with. **Already documented as pre-existing in `project_
    handover.md` §22 as far back as the Dashboard Startup & Access
    Recovery sprint** — re-confirmed here via `python3 -c "import os;
    print(os.getenv('MAX_TOKENS_PARAM'))"` returning `max_tokens`, and
    via `.env` itself.
  - `tests/test_mic_device_index.py` (6) — `list_microphones.py` does not
    exist at the repo root in this checkout; `tests/test_real_adapters.
    py::test_real_whisper_source_*` (2) — a pre-existing `RealWhisperSource
    .__new__()`-bypasses-`__init__` test construction issue
    (`_device_index` never set) unrelated to any Vision/camera code.
  - `tests/test_production_launcher.py::test_07_health_checks_all_
    pass_in_default_mock_configuration` (1) — OpenRouter/Fish Audio
    external network health checks, blocked by this sandbox's own
    network policy (same class of failure P0.6.2-FIX/P0.6.3 already
    documented for other tests).
  - `tests/test_sprint63_long_term_memory_recovery.py` (10) + `tests/
    test_sprint64_memory_corruption_forensics.py` (5) + `tests/test_
    sprint67_mutation_audit_trail.py` (1) + `tests/test_sprint68_
    mutation_audit_hardening.py` (2) — forensic/byte-identical checks
    against `config/backups/` file counts and `luno/memory.py`-only
    state; this checkout has accumulated far more backup files than
    these tests' hardcoded pristine-count expectation, from months of
    real cumulative sprint activity in this same long-lived sandbox.
    **Also already documented as pre-existing in `project_handover.md`
    §22** (41 files noted at the Dashboard Startup & Access Recovery
    sprint; now higher). These tests only import `luno.memory`/`luno.
    config` — confirmed zero relation to anything P0.7 touched.

## 12. Live verification (Section 17)

**Not performed by the agent — same structural sandbox limitation as
every prior sprint in this line** (no `ultralytics`, no network route to
the camera). The user must run the real `main.py`, then either wait for
2+ people to appear in frame or simulate it, and confirm `camera_
multiple_people_log`'s log line appears in the AutomationEngine log
alongside the existing `camera_human_detected_log` line, while the
Vision Dashboard continues to show real detections exactly as before.

**Result classification: BLOCKED** (for the agent's own live-hardware
attempt) — per this project's own established definitions. Everything
provable from inside this sandbox (architecture audit, all 40 new tests,
full regression, the 3 test-staleness fixes) is a genuine, verified PASS
at the code level; only the live walk-test itself remains unverified by
the agent.

## 13. Diff audit — files touched this sprint

- `[NEW]` `luno/camera_automation/vision_context.py` — `VisionContext`,
  `build_vision_context()`, `normalize_object_label()`.
- `[MODIFIED]` `luno/camera_automation/cameras.py` — `CameraEvent` gained
  5 new optional fields + `to_dict()` entries (additive only).
- `[MODIFIED]` `luno/camera_automation/vision_bridge.py` —
  `vision_status_reader` public attribute, `_last_detection_error`
  tracking, new `system_error` subscription + `_on_system_error()`
  handler, `_ingest()` now builds/attaches a `VisionContext` (additive
  only — no existing subscription/dedupe/lifecycle behavior changed).
- `[MODIFIED]` `luno/automation/models.py` — `CONDITION_TYPES` gained
  `"greater_equal"` (additive only).
- `[MODIFIED]` `luno/automation/conditions.py` — one new branch in
  `evaluate_condition()` for `greater_equal` (additive only).
- `[MODIFIED]` `luno/bootstrap/adapters.py` — new `register_vision_
  context_reader()` function, same post-hoc wiring pattern as two
  existing functions in that file.
- `[MODIFIED]` `main.py` — one new call to `register_vision_context_
  reader(modules, adapters)`, right after the existing `register_
  intent_classifier()` call.
- `[MODIFIED]` `config/automation_rules.json` — one new rule,
  `camera_multiple_people_log` (log-only), appended; both pre-existing
  rules byte-for-byte unchanged.
- `[NEW]` `tests/test_p0_7_vision_context.py` — 40 tests.
- `[MODIFIED]` `tests/test_p0_5_3_vision_camera_bridge.py`, `tests/
  test_p0_6_3_unified_vision_camera_automation.py`, `tests/test_
  sprint72_automation_engine.py` — 3 pre-existing tests updated to
  reflect this sprint's own intentional, additive changes (Section 11).
- `[NEW]` `docs/change_impact/vision_context_p0_7.md` — this file.

**Not touched:** `luno/adapters/vision.py` (`VisionAdapter` itself),
`luno/vision.py`, `luno/adapters/real_vision.py`, YOLO model
configuration, RTSP configuration, the dashboard frontend/backend
collectors, the Home Assistant client, pytapo/PTZ code, `.env`. No
lights/switches/PTZ/locks action type was added or invoked by the new
rule.

## 14. Limitations (honest, not glossed over)

- Live hardware verification remains BLOCKED for the agent — same
  structural sandbox limitation as every prior sprint.
- `VisionContext` only refreshes on the four pre-existing discrete
  events (Enter/Leave/Disconnect/Reconnect) — an object appearing while a
  person is already present, or the person count changing mid-presence,
  does not itself retrigger any automation this sprint (Section 9 above).
  This is intentional (Section 9's own "avoid polling-driven events"
  constraint), not an oversight — see Section 15 for the recommended next
  step.
- No before/after performance numbers were measured — no real camera/GPU
  load exists in this sandbox to measure against (same limitation every
  prior sprint in this line has documented).
- The pre-existing `.env`/`MAX_TOKENS_PARAM` mismatch and the `config/
  backups/` accumulation (Section 11) remain unfixed — both were already
  flagged as optional future pickups in `project_handover.md` §22 well
  before this sprint, and are outside P0.7's own scope (Vision Context →
  Automation Context).

## 15. Recommended next step (smallest logical one — not implemented this sprint)

The most direct, smallest follow-on to P0.7 is closing the "does not
retrigger while a person stays present" gap documented in Section 14: add
a periodic, LOW-frequency (e.g. once every N seconds, not every tracked-
cycle frame — reusing the existing scheduler, never a new tight polling
loop) "VisionContext changed materially" check, gated behind its own
config flag, that only publishes when `person_count`/`detected_objects`
actually differ from the last published snapshot (a real state-change
check, not a raw polling-cycle-to-event mapping — preserving Section 9's
own constraint). This was deliberately NOT implemented in P0.7 per its
own explicit "do not implement the next sprint" instruction.
