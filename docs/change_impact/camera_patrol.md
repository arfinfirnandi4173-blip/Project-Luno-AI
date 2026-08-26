# Sprint 71 — Camera Patrol

**Date:** 2026-08-18
**Status:** COMPLETE
**Scope:** new `luno/camera_patrol/` package, new `luno/tool_manager/builtin/camera_patrol.py`, new `config/camera_patrol_routes.json`, additive changes to `main_runtime_demo.py` (`ToolManagerBridgeModule`), `luno/bootstrap/modules.py`, `luno/planner/parser.py`, `luno/dashboard/collectors.py`, `luno/dashboard/server.py`, `luno/dashboard/static/index.html`.
**Explicitly NOT touched:** Tapo/C212 authentication, PTZ implementation (`real_camera_ptz.py`/`camera_ptz.py` — zero lines changed), pytapo/reconnect logic, Home Assistant, long-term memory, mutation audit, schema/config migration beyond the one new, deliberately-scoped `camera_patrol_routes.json` file.

## 1. Goal

Let Luno run a bounded, deterministic, stoppable-at-any-time patrol route across saved Tapo PTZ presets — "mulai patroli kamera" / "mulai patroli rumah" / "stop patroli kamera" / "status patroli kamera" — built entirely on top of the camera PTZ foundation Sprint 69/70 already shipped.

## 2. Architecture — no second PTZ implementation

Every actual camera movement a patrol issues goes through the **exact same** `tool_requested` → `ToolManagerBridgeModule` → `ToolManager` → `camera_ptz` handler round trip a manual voice command already uses (the same pattern `PlannerBridgeModule._tool_bridge_handler`/`RuntimeDemoConsole._execute_tool` already established — `CameraPatrolModule._dispatch_tool_call()` is a third caller of that same idiom, not a new mechanism). This gives patrol Sprint 69/70's error classification, per-call timeout enforcement, and single-worker FIFO serialization entirely for free, with zero duplicated PTZ logic. `luno/tool_manager/builtin/real_camera_ptz.py` and `camera_ptz.py` were not modified at all — confirmed by SHA-256 hash comparison before/after this sprint (see §8).

New pieces, by layer:

- **`luno/camera_patrol/route.py`** — `PatrolRoute` (name, presets, dwell_seconds, loop, max_cycles, max_duration_seconds, return_home) and `validate_route()`, the one place every schema/safety rule is enforced.
- **`luno/camera_patrol/state.py`** — `PatrolState` (idle/starting/moving/dwelling/stopped/completed/failed, plain string constants matching this project's own `TapoErrorClass`/`PTZConnectionState` convention).
- **`luno/camera_patrol/controller.py`** — `CameraPatrolModule`, a `Module` (Event-Bus-shaped, same interface `ToolManagerBridgeModule`/`ProactiveModule` already implement) that owns the patrol's own background thread, state machine, and Event Bus publishing.
- **`luno/tool_manager/builtin/camera_patrol.py`** — `CameraPatrolToolHandler`, a thin `ToolHandler` adapter (same role `camera_ptz.py`/`home_assistant.py` play relative to their own logic) registered as tool `"camera_patrol"` in the same `ToolManager.registry` every other tool lives in. `execute()` for `"start"` returns almost instantly — it kicks off the patrol's own background thread rather than blocking for the whole patrol's duration (which could be minutes); blocking here would starve `ToolManager`'s own handler-pool timeout and make a working patrol look like a failure.
- **`luno/planner/parser.py`** — `_classify_camera_patrol()`, anchored on the word "patrol"/"patroli" co-occurring with a start/stop/status verb, the same "every signal must co-occur" conservative approach `_classify_camera_ptz` already uses.

## 3. State machine

```
IDLE -> STARTING -> MOVING -> DWELLING -> MOVING -> ... -> COMPLETED
```

Terminal: `STOPPED`, `FAILED`. Every loop iteration checks the cancellation flag and the duration bound at each preset boundary *and* during the dwell wait itself — a stop request, a duration bound, or a failure can never be silently overridden by an in-flight completion (checked explicitly in `_finish_after_bound()`).

## 4. Safety invariant (Phase 1)

A route with `loop=true` must specify `max_cycles` or `max_duration_seconds`, or `validate_route()` raises `PatrolRouteError` and `start_patrol()` refuses the request (`refused_no_patrol_route`) before any camera movement happens. A non-looping route is inherently bounded (it runs the preset list exactly once) and needs no explicit bound — enforced exactly as narrowly as the brief's own reasoning states, not more broadly.

## 5. Ownership — manual PTZ always wins (Phase 5)

`ToolManagerBridgeModule` (in `main_runtime_demo.py`) gained a small, optional, empty-by-default hook list (`register_pre_dispatch_hook()`), invoked immediately before every tool call executes. `CameraPatrolModule.on_manual_ptz_dispatch()` is registered against it: for any `camera_ptz` call that is **not** tagged `_patrol_origin` (patrol's own outgoing calls carry that marker — see module docstring) while a patrol is active, it calls `stop_patrol()` **synchronously, with a bounded join** before returning, so by the time the manual command actually executes, patrol has genuinely relinquished the camera — not just been asked to. Verified live: `test_22_manual_ptz_command_stops_active_patrol` and `test_21_no_concurrent_ptz_ownership` (records real enter/exit timestamps on the handler and asserts zero overlap).

This hook is a no-op for every other tool (empty list by default) — zero behavior change for any tool call that existed before this sprint, confirmed by the full `luno/tool_manager/tests/` + `luno/planner/tests/` suite (270/270 unchanged) plus the full repository sweep (§7).

## 6. Emergency stop (Phase 6)

`stop_patrol()` sets a per-run `threading.Event`, then joins the patrol thread with a bounded timeout (25s — generous enough to cover `camera_ptz`'s own worst-case handler timeout of 20s, never unbounded). Every voluntary wait inside the patrol loop uses `Event.wait(...)` (the same idiom `luno/proactive/manager.py::_tick_loop` already established for this codebase), never `time.sleep()` — a stop request during a 5-second dwell was measured taking ~0.3s to actually stop in `test_11_stop_mid_run_reaches_stopped`, not "after 5 seconds."

## 7. Failure handling (Phase 7) — reuses Sprint 69/70's classifier, unchanged

A preset move that fails (camera offline, auth failure, unknown preset, timeout) immediately transitions the patrol to `FAILED` and stops — it never proceeds to the next preset. `classify_tapo_exception()` (Sprint 69/70, zero lines changed) is called exactly as before; patrol adds no new classification logic. Verified with a real `RealCameraPTZHandler` against a fake Tapo client raising an auth-failure-shaped exception (`test_19`), a disconnect mid-route (`test_18`), an unrecognized preset name (`test_20`), and a handler that never responds within its own timeout (`test_17`, using `ToolManager`'s own existing timeout enforcement — no new timeout mechanism was built).

## 8. Persistence (Phase 8)

Route **definitions** (name/presets/dwell/loop/bounds) live in `config/camera_patrol_routes.json` — the same kind of named-entity config file this repo already uses for `scripts.config.json`/`switches.config.json`, not a new database. Shipped empty (`{}`) — no fabricated example route naming presets the user hasn't confirmed saving (this checkout's own camera only recently had its `TAPO_HOST` corrected; no saved presets were confirmed to exist). To add a route, edit that file directly, e.g.:

```json
{
  "rumah": {
    "presets": ["pintu", "meja", "jendela"],
    "dwell_seconds": 10,
    "loop": true,
    "max_cycles": 3
  }
}
```

Runtime state (`current_preset`/`current_index`/`current_cycle`/`state`/`started_at`) lives **only** in `CameraPatrolModule`'s own Python attributes — never written to that file or anywhere else (verified directly: `test_25_runtime_state_is_ephemeral_not_written_to_the_routes_file` hashes the routes file before and after a full patrol run and asserts byte-identity). `PatrolRoute` cannot hold a credential, RTSP URL, or session token — there is no field for one.

## 9. Event Bus (Phase 9)

`camera_patrol_started` / `_moving` / `_dwell` / `_stopped` / `_completed` / `_failed`, each carrying only `{route, preset, index, cycle}` plus `reason` where relevant — metadata only, verified directly (`test_26_credentials_never_appear_in_event_payloads` asserts every payload key is in the allowed set and none of a forbidden-key list — password/username/credential/token/session/frame/image — ever appears).

## 10. Dashboard (Phase 10) — additive only

`collect_vision()` gained an **optional** `modules` parameter (default `None`) — every existing caller that doesn't pass it gets byte-for-byte the same response as before this sprint (`test_30`). When passed, it additively includes `patrol_state`/`patrol_route`/`patrol_preset`/`patrol_index`/`patrol_cycle`/`patrol_max_cycles`/`patrol_reason`, read directly from `CameraPatrolModule.get_status()` — not a second, independently-tracked patrol state. `index.html`'s `loadVision()` renders a `Patrol` card (and `Patrol Route`/`Patrol Preset`/`Patrol Cycle`/failure-reason cards when relevant) appended to the existing Vision grid — no existing card was reordered, removed, or restyled.

## 11. Tests

New file: `tests/test_sprint71_camera_patrol.py` — 37 tests covering route validation (valid/empty/duplicate/invalid-dwell/invalid-cycle/missing-safety-bound/unknown-preset-is-a-runtime-not-static-concern), full lifecycle (started→moving→dwelling→completed, stop mid-run, failure), safety (infinite-patrol rejected, max_cycles enforced, max_duration enforced, stop prevents next preset, timeout stops patrol, camera-disconnect/auth-failure stop patrol via the real Sprint 69/70 classifier, no concurrent PTZ ownership, manual override stops patrol, repeated start refused, stop-when-idle), persistence (no config mutation, runtime state never written to the routes file), security (no credentials in events/dashboard/route dict), dashboard integration (additive, backward compatible), parser classification (all 4 example commands plus a full Planner→ToolCall→handler round trip), and in-memory performance. 37/37 passing, reconfirmed clean across 4 consecutive full-file runs (148/148).

## 12. Regression (Phase 13)

Targeted: `test_sprint71_camera_patrol.py` (37/37) + `luno/tool_manager/tests/` + `luno/planner/tests/` + Sprint 69/70's own test files + `test_sprint71_dashboard_startup_recovery.py` = 322/322 passed. `test_dashboard.py`/`test_dashboard_turn_state_recovery.py`/`test_production_launcher.py` = 83 passed + 1 pre-existing known failure (`test_07_health_checks...`, environment-specific, unrelated).

Full repository sweep (whole-repo `--collect-only` clean — no new collection errors — plus the remaining ~147 project test files run in 8 chunks): every failure traced to a pre-existing, already-documented environment/checkout-state cause (`.env`'s `MAX_TOKENS_PARAM` override, `MIC_DEVICE_INDEX`/missing optional deps, this long-lived checkout's accumulated `config/backups` count and its pre-existing `logs/mutation_audit/` directory from real prior usage, `luno/barge_in/tests/test_barge_in.py`'s own documented parallel-execution flakiness class — confirmed absent when re-run in isolation) — **with one exception, fixed forward**: `tests/test_sprint68_mutation_audit_hardening.py::test_baseline_config_json_count_is_15` hardcoded a literal config-file count from Sprint 68's own time; this sprint's own, deliberate, sanctioned addition of `config/camera_patrol_routes.json` legitimately moved that count from 15 to 16. Renamed/updated to `test_baseline_config_json_count_is_16` with a comment explaining exactly why — not a workaround, a correct baseline update for a real, intentional, in-scope change. No other test file was modified.

## 13. Persistent state (Phase 14)

SHA-256 hashes of all `config/*.json` files and 11 critical source files taken immediately before this sprint's first edit and re-checked after the full regression sweep: exactly one new file appeared (`camera_patrol_routes.json`, expected), zero files disappeared, zero *existing* config file changed. Critical-file hashes confirm exactly the 5 files this sprint intentionally modified changed (`main_runtime_demo.py`, `luno/bootstrap/modules.py`, `luno/dashboard/collectors.py`, `luno/dashboard/static/index.html`, `luno/planner/parser.py`) and nothing else did — in particular `real_camera_ptz.py`, `camera_ptz.py`, `luno/tool_manager/manager.py`, `luno/tool_manager/builtin/__init__.py`, `luno/bootstrap/adapters.py`, and `luno/core/events.py` are all byte-identical. `config/backups/` file count unchanged (43 before and after); zero new backup files created by this sprint's own test/verification runs.

## 14. Performance (Phase 12)

Measured directly: `get_status()` and `stop_patrol()` (idle path) both average well under 5ms per call over 200 iterations (`test_34`) — pure in-memory operations, movement/dwell network timing deliberately excluded per the brief's own carve-out. No busy loops anywhere: the PTZ-call wait and the manual-override-check polling both use bounded `Event.wait(0.1s)` slices (same order of magnitude as `ToolManager`'s own established `_interruptible_sleep` step), and dwell waits use `Event.wait(dwell_seconds)` directly (a single OS-level blocking wait, not polling at all). No thread survives `CameraPatrolModule.stop()` (the Module-level shutdown hook) while a patrol was active — verified directly (`test_35`).

## 15. Security (Phase 11's own security tests)

Verified directly, not assumed: Event Bus payloads contain only `{route, preset, index, cycle, reason}` (`test_26`); `PatrolRoute.to_public_dict()` has no field capable of holding a credential (`test_27`); `get_status()` has no credential-shaped keys (`test_28`). `_redact_credentials()` (Sprint 69, unmodified) still applies to every failure message a patrol surfaces, since patrol only ever reads the SAME `ToolResult`/exception text `camera_ptz` already produces.

## 16. Known limitations

- Route names with spaces rely on the same slugify-based normalization the parser already applies to `goto_preset` targets elsewhere in this codebase (an existing, pre-Sprint-71 characteristic, not something this sprint introduced or fixed) — single-word route names (e.g. "rumah") are unaffected; a multi-word route should be authored in `camera_patrol_routes.json` using the underscore-joined form (e.g. `"ruang_tamu"`) to match voice input.
- "unknown preset" is deliberately a runtime, not a static-validation, concern — there is no way to synchronously ask the camera "does this preset exist" at route-load time without a live camera call (same honest limitation `real_camera_ptz.py`'s own docstring already documents for `goto_preset`).
- `config/camera_patrol_routes.json` ships empty; no voice command to *define* a new route was built (not requested by the brief — only `start`/`stop`/`status`) — routes are authored by editing the file directly, same as `scripts.config.json`.
- Live camera verification (an actual physical Tapo C212 executing a real patrol) was not performed — this sandbox has no route to the user's private LAN, the same structural limitation Sprint 69/70 already documented. Every test in this sprint uses `MockCameraPTZHandler` or a fake `pytapo`-shaped client; the dispatch path itself (real `tool_requested`/`ToolManagerBridgeModule`/`ToolManager` round trip) is exercised for real, only the final hardware hop is simulated.

## 17. Next recommended sprint

None required to close out Camera Patrol — it is feature-complete against the brief. If the user wants it live-verified, the concrete next step is: save at least 2-3 real presets on the actual camera (`"simpan posisi kamera sebagai <name>"` via voice, or the Tapo app), add one route to `config/camera_patrol_routes.json` referencing those exact names, then say "mulai patroli kamera" — matching Sprint 70's own precedent of handing the user a small, safe, concrete next step rather than a sandbox guess.
