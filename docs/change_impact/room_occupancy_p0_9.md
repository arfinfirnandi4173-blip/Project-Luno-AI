# LUNO P0.9 — Room Occupancy State + Presence Duration

## 1. Purpose / architectural decision

Add a Room Occupancy State layer above the existing, already-hardened
Vision human-confirmation pipeline, so other Luno components (a future
automation rule, the dashboard, Proactive, voice status) can ask "is the
room occupied, by how many people, and for how long" without any of them
re-deriving that from raw detections themselves.

Most important architectural rule, preserved exactly as specified:

    YOLO detects. Vision confirms. Occupancy remembers.
    Automation decides. Home Assistant executes.

`RoomOccupancyModule` (`luno/vision_occupancy.py`) never controls Home
Assistant, WLED, or ToolManager, and never runs a second detection/
inference pipeline — it is a pure, in-memory, Event-Bus-driven state
machine layered on top of events the Vision pipeline already publishes.

## 2. Where occupancy state now lives

`luno/vision_occupancy.py::RoomOccupancyModule` — a new, standalone
`Module` (same `luno.core.module_manager.Module` interface every other
subsystem in this project implements), registered in
`luno/bootstrap/modules.py` as `"room_occupancy_module"`, always active
(no feature flag — it never controls a device, so there is no existing
behavior for it to change by simply existing, unlike
`camera_automation_module`, which stays opt-in because it gates a real
device action).

**Existing state-owning candidates inspected and rejected:**

- `luno/world_model.py::WorldModel` — inspected directly. Explicitly and
  exclusively "what is the world like RIGHT NOW" for **Home Assistant
  device state** (`light.bedroom = on`), fed only by
  `device_state_changed`/`ToolResult`/a one-time HA `get_states()` sync.
  It has zero knowledge of Vision/rooms/presence and is not the right
  owner for occupancy — reusing it would have meant bolting an unrelated
  concept onto a module whose own docstring explicitly scopes it to
  device state.
- `VisionAdapter` itself (`luno/adapters/vision.py`) — already owns
  `_person_present_debounced`/`_human_confirmed`, the INPUT this sprint
  consumes. Adding occupancy/duration bookkeeping directly onto it would
  have mixed two different responsibilities (raw detection confirmation
  vs. room-level memory) into one class, and would have required
  modifying an already-hardened, heavily-tested file for a purely
  additive feature — rejected in favor of a new, separate, read-only
  subscriber, per the brief's own "do not modify existing detection/
  confirmation" constraint.
- `CameraAutomationModule`/`AutomationEngine` — both already exist
  specifically to gate real device actions off classified camera events;
  neither is an appropriate "state memory" owner, and coupling Occupancy
  to `camera_automation`'s own enabled flag would have made occupancy
  tracking dependent on whether WLED automation happens to be turned on
  — rejected (see §3 for why the raw Vision events, not the classified
  `camera_automation.camera_event` stream, were chosen instead).

## 3. Event flow

```
Camera / YOLO
    v
Person Detection            (luno/vision.py, luno/adapters/vision.py - UNCHANGED)
    v
Existing Human Confirmation (VisionAdapter._update_confirmed_presence() - UNCHANGED)
    v
Room Occupancy State        (luno/vision_occupancy.py - NEW, this sprint)
    v
Automation / Luno Awareness (AutomationEngine, camera_automation - UNCHANGED)
```

`RoomOccupancyModule` subscribes directly to three EXISTING event types
`VisionAdapter` already publishes, unmodified:

- `HumanPresenceConfirmed` (`human_presence_confirmed`) — the SAME
  P0.8.6 confirmation gate the real WLED-ON rule already keys on
  (`event.kind == "human_confirmed"`) → occupancy becomes `occupied`.
- `CameraPersonLeft` (`camera_person_left`) — the SAME debounced
  room-absence signal the real WLED-OFF rule already keys on
  (`event.kind == "human_cleared"`, P0.8.2/P0.8.9) → occupancy becomes
  `vacant`. Deliberately **not** `HumanPresenceUnconfirmed` — that
  gate's own docstring explicitly warns it exists "to keep physical
  automation conservative, not to describe whether a person is still in
  the room" (a person stepping out of frame for one tracked cycle
  unconfirms the automation signal while Vision's own room presence,
  correctly, stays debounced-PRESENT).
- `VisionFrameProcessed` (`vision_frame_processed`) — fires once per
  tracked cycle (Sprint 8), already carries `data["human_count"]`
  (`len(current_humans)`, the SAME count `vision_context.build_
  vision_context()` already reduces to `person_count` for
  `AutomationEngine` conditions, P0.7). Read-only cache refresh, never
  used to derive STATE — only `person_count`.

No new import of Vision internals, YOLO, RTSP, or a second tracked-
object algorithm — see §11's architecture-guard tests.

## 4. State machine

Two states: `vacant` (default) and `occupied`.

- `human_confirmed` (vacant → occupied): creates a NEW `occupied_since`,
  resets `presence_duration_seconds` to 0, publishes `room_occupied`
  exactly once. If already occupied, this is a no-op for the state
  machine (only `last_seen` refreshes) — no duplicate `room_occupied`.
- `human_cleared` (occupied → vacant): freezes the FINAL
  `presence_duration_seconds`, records `vacant_since`, sets
  `person_count = 0`, publishes `room_vacant` exactly once. If already
  vacant, this is a complete no-op — no duplicate `room_vacant`
  (Section 5/9's "no repeated events while state is unchanged"
  requirement).
- `VisionFrameProcessed` never changes `state` — only refreshes the
  cached person count, surfaced only while `state == "occupied"`.

## 5. Event names

- `room_occupied` — published once per genuine vacant→occupied
  transition, payload = the full snapshot (`RoomOccupancySnapshot.to_dict()`).
- `room_vacant` — published once per genuine occupied→vacant transition,
  same payload shape.
- `occupancy_changed` — the brief's own "Optional" umbrella event,
  published alongside (never instead of) whichever specific one just
  fired, for a listener that only cares "something changed."

## 6. Snapshot schema

```json
{
    "state": "occupied",
    "person_count": 1,
    "occupied_since": "2026-08-23T13:54:02.123456+00:00",
    "vacant_since": null,
    "last_seen": "2026-08-23T14:10:17.654321+00:00",
    "presence_duration_seconds": 975.2
}
```

Exposed via `RoomOccupancyModule.get_snapshot() -> RoomOccupancySnapshot`
(frozen dataclass, `.to_dict()` for the JSON-shaped form) — safe to call
from any thread/component, lock-protected, no I/O. This is the one
canonical snapshot; §11's architecture-guard test statically confirms no
second file in `luno/` defines the same distinctive field set.

## 7. Timing implementation

Two clocks, never mixed:

- `time.monotonic()` — the ONLY clock used for `presence_duration_
  seconds`. Same convention `AutomationEngine`/`CameraAutomationModule`
  already established (P0.8.8's own dedicated tests prove that module's
  cooldown is immune to a wall-clock jump); this sprint's own
  `test_J1`/`test_J2` prove the same for Occupancy (AST-level proof that
  `time.monotonic()` is called and no naive `datetime` subtraction
  exists, plus a behavioral proof that jumping `utcnow()` by 365 days
  does not move `presence_duration_seconds`).
- `luno.core.utils.utcnow()` — the project's existing timezone-aware UTC
  helper, used ONLY for the three human-readable ISO-8601 fields
  (`occupied_since`, `vacant_since`, `last_seen`). Never used for
  duration arithmetic.

## 8. Multi-person semantics

`occupied_since` is set exactly once per occupied period, at the
vacant→occupied transition, and is NEVER touched by a person-count
change while already occupied (`test_H`, `test_G`) — count changes are
threaded through purely via the `VisionFrameProcessed` cache refresh
(§3), which never calls the state-transition code path.

Verified sequence (`test_G_full_multiperson_sequence`): 0→1 (occupied,
1 `room_occupied`), 1→2 (still occupied, same `room_occupied` count —
no new event), 2→1 (still occupied, zero `room_vacant`), 1→0 (vacant, 1
`room_vacant`), 0→2 (a genuine NEW occupied period, second
`room_occupied`), 2→0 (second `room_vacant`).

## 9. Restart behavior

No persistence of any kind (§K2's static test confirms zero `sqlite3`/
`json.load`/`json.dump`/`open(`/`atomic_write_json`/`pickle` references
in the module). A freshly-constructed `RoomOccupancyModule()` IS what a
process restart looks like for this module — always
`state="vacant", person_count=0, occupied_since=None`. Nothing is ever
fabricated; the module only transitions to `occupied` once a genuine,
fresh `HumanPresenceConfirmed` arrives from the also-freshly-restarted
Vision pipeline (`test_K1`). Deliberate choice, not an oversight — see
`luno/vision_occupancy.py`'s own "Restart behavior" docstring section
for the full reasoning (nothing comparable is persisted for
`VisionAdapter`'s own in-memory confirmation state either, so persisting
occupancy alone would create a false impression of durability the rest
of the pipeline doesn't actually have).

## 10. Observability

`[VISION OCCUPANCY] state=vacant -> occupied person_count=N` and
`[VISION OCCUPANCY] state=occupied -> vacant person_count=0
presence_duration=X.Xs` — logged ONLY on a genuine state transition,
never per-frame/per-cycle (the `VisionFrameProcessed` handler is
silent). `health()` reports current state/person_count for the module
manager's own status surface.

## 11. Files changed

- `luno/vision_occupancy.py` (NEW) — `RoomOccupancyModule`,
  `RoomOccupancySnapshot`, `ROOM_OCCUPIED_EVENT_TYPE`/`ROOM_VACANT_
  EVENT_TYPE`/`OCCUPANCY_CHANGED_EVENT_TYPE` constants.
- `luno/bootstrap/modules.py` — imports and registers
  `RoomOccupancyModule` (construction, `bind_event_bus`,
  `register_module`, returned in the `modules` dict as
  `"room_occupancy_module"`). No existing registration/wiring line
  touched or reordered.

**Nothing else was modified.** In particular, per the brief's explicit
list: `luno/vision.py`, `luno/adapters/vision.py`, YOLO/RTSP/confidence
threshold/confirmation-cycle config, `CameraAutomationModule.
_publish_if_not_suppressed()` (P0.8.8), `config/automation_rules.json`
(the WLED ON/OFF rules from P0.8.6/P0.8.9), and every HA-verification
code path are byte-for-byte unchanged.

## 12. Tests added

`tests/test_p0_9_room_occupancy.py` — 34 tests:

- **A (2)** initial state. **B (3)** first confirmed human. **C (2)**
  repeated detections → one transition only. **D (1)** duration
  increases correctly (monkeypatched `time.monotonic`). **E (4)** person
  leaves → vacant, final duration preserved, frozen. **F (3)** re-entry
  → new `occupied_since`, duration resets, second `room_occupied`.
  **G (1)** the full multi-person 0→1→2→1→0→2→0 sequence. **H (1)**
  `occupied_since` stability under person-count changes. **I (2)**
  repeated `human_cleared` → no duplicate `room_vacant`, before and
  after a genuine transition. **J (2)** clock correctness — AST +
  behavioral proof `time.monotonic()` gates duration, not wall clock.
  **K (2)** restart behavior — no fabricated `occupied_since`, no
  persistence imports. **L (3)** snapshot consistency — immutability,
  stability across repeated reads, `to_dict()` fidelity. **M (1)** a
  real, full-stack bootstrap (`register_all_modules`, camera_automation
  ENABLED, `MockHomeAssistantHandler` only) proving `RoomOccupancyModule`
  and the existing WLED-style automation both correctly react to the
  SAME real `HumanPresenceConfirmed` event with zero interference.
  **N1-N7 (7)** architecture guard — static source scans proving no
  Home Assistant/ToolManager/WLED/YOLO reference, subscription to
  exactly the three documented event types, state derived only from the
  confirmed-presence events (never a raw per-frame event), and exactly
  one file in `luno/` owns the canonical occupancy field set.

All 34 pass. Section M/N of the brief's own required-test list ("existing
WLED automation"/"existing P0.8.x tests remain green") are satisfied by
the full regression sweep below rather than duplicated wholesale inside
this new file, matching the same convention every prior P0.8.x sprint's
own change-impact doc already followed for "existing behavior remains
green" requirements.

## 13. Regression sweep results

**Focused suite** (new P0.9 suite + all Vision/`test_p0_*`/camera
patrol/automation-engine files, 29 files): 764 passed, 1 skipped, 4
failed — three are the SAME already-documented `.env`
`CAMERA_AUTOMATION_ENABLED=true` condition first observed and explained
during P0.8.9 (unrelated to this sprint's code, re-confirmed unrelated
here too since P0.9 touches neither `.env` nor that flag). The fourth,
`test_vision_sprint8.py::test_29_stress_many_cycles_varying_scene_no_
crash_no_leak`, is a `time.sleep(1.5)`-based real-thread stress test;
re-run in isolation three times, clean pass every time (`1 passed` each
run) — a full-suite-only timing flake under `-n 4` parallel CPU
contention, the same category already documented multiple times in
`regression_baseline.md` for other stress-style tests (`test_barge_in.py`,
`test_state_isolation.py`, `test_verification_dashboard.py`). Nothing in
`luno/vision.py`/`luno/adapters/vision.py` was touched by this sprint.

**Full repository sweep** (155 files, 8 parallel chunks, `-n 4
--timeout=90`): 4,356 passed, 1 skipped, 45 failed. Every failure traced
to an already-documented pre-existing category: LLM `max_completion_
tokens` `.env` override, `test_mic_device_index.py`/`list_microphones.py`
sandbox-has-no-audio-hardware, `test_real_adapters.py`/`test_production_
launcher.py::test_07` whisper/network-egress gaps, `config/backups/`-
accumulation forensic drift (`test_sprint63/64/67/68`), real
`config/lights.config.json` `light.main_light` drift
(`test_sprint60_area_schema.py`), the three `CAMERA_AUTOMATION_ENABLED`
tests (explained in P0.8.9, re-confirmed here), plus two additionally-
observed items this run — `test_sprint66_tool_boundary_hardening.py::
test_performance_validate_download_directory_is_fast` and `test_
sprint67_mutation_audit_trail.py::test_this_files_own_run_never_touches_
the_real_config_directory` — both re-run in isolation immediately after
and both passed cleanly (parallel-load timing flakes, not regressions),
and `test_sprint63_long_term_memory_recovery.py::test_N_production_
config_files_unchanged_by_this_test_run`, which failed once in
combination with two neighboring tests but passed cleanly standalone
both before and after — traced to real, live churn of
`config/vision_memory.sqlite3-wal`/`-shm` (a real SQLite WAL database
this checkout's own live Luno process actively uses), the same
`vision_memory.sqlite3-wal`/`-shm` drift family already referenced 5
times elsewhere in `regression_baseline.md`. **Zero failures touch
`luno/vision_occupancy.py`, `luno/vision.py`, `luno/adapters/vision.py`,
`luno/camera_automation/`, or `luno/automation/`** beyond the three
already-explained `CAMERA_AUTOMATION_ENABLED` ones.

## 14. Confirmation: existing WLED behavior was not altered

- `config/automation_rules.json` was not opened or modified by this
  sprint (verified: no diff).
- `luno/automation/engine.py`/`luno/automation/models.py` (P0.8.9's own
  delayed-action mechanism) were not opened or modified by this sprint.
- `luno/camera_automation/module.py` (`_publish_if_not_suppressed`,
  P0.8.8's fix) was not opened or modified by this sprint.
- `tests/test_p0_8_9_wled_off_debounce.py` (25 tests) and every other
  P0.8.x camera/WLED suite were re-run as part of this sprint's own
  regression sweep and remain 100% green.
- `test_M_occupancy_and_real_wled_automation_both_react_to_same_
  confirmation` additionally proves, directly, that a real, full-stack
  boot with BOTH `RoomOccupancyModule` and the existing WLED-style
  automation active react correctly and independently to the exact same
  real `HumanPresenceConfirmed` event.

## 15. Known limitations

- `RoomOccupancyModule` is purely observational this sprint, exactly as
  specified — no automation rule currently reads its snapshot or
  subscribes to `room_occupied`/`room_vacant`/`occupancy_changed`. A
  future sprint could wire an `AutomationEngine` condition or a
  dashboard panel to it without any further change to this module.
- Multi-camera/multi-room occupancy is out of scope — this sprint tracks
  ONE room (the single configured Vision camera), matching every prior
  P0.x sprint's own single-camera scope.
- As with every prior P0.8.x sprint, this module cannot and does not
  claim anything about physical reality beyond what Vision's own
  confirmed-presence pipeline already reports — it remembers and times
  what Vision already decided, nothing more.
