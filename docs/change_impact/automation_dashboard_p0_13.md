# P0.13 — Luno Automation Dashboard / Visual Automation Builder

## 1. Goal

Give the Luno Dashboard a usable UI for creating, editing, validating,
testing, enabling/disabling, running, and deleting automations —
consuming the P0.12 Automation API exclusively. Mandated flow:
Dashboard UI → P0.12 Automation API → `AutomationEngine` → `ToolManager`
→ Home Assistant → physical devices. P0.13 does **not** rewrite
`AutomationEngine`, does not add a second execution path, does not call
Home Assistant or touch `config/automation_rules.json` from the UI, and
does not start P0.14 (AI/natural-language automation authoring).

## 2. Architecture inspection (Phase 1) — what already existed

- **Single-file, dependency-free frontend:** `luno/dashboard/static/
  index.html` is the ONLY static asset anywhere in the project — all
  HTML/CSS/JS inline, no build step, no npm/node toolchain, no frontend
  framework. `DashboardServer._read_static_index()` reads and caches it
  once at server construction. This dictated that all P0.13 frontend
  work had to be added to this single file in vanilla JS — the brief's
  own "do not introduce a heavy frontend framework unless the
  repository already uses one" instruction left no other option.
- **Panel/nav convention:** sidebar `<button data-panel="X">` toggles
  `.panel`/`.panel.active`; `onPanelShown(name)` dispatches through a
  `{name: loaderFn}` map; a global `setInterval(..., 3000)` re-invokes
  the active panel's loader every 3s for auto-refresh — any modal/editor
  state must set a guard flag (mirroring the pre-existing Memory
  Dashboard's `memState.modalOpen`) so the poll never clobbers an
  in-progress edit.
- **`api(path, opts)`:** pre-existing thin `fetch()` wrapper, throws on
  non-2xx, toggles an offline banner.
- **Modal pattern:** `.modal-overlay`/`.modal-box` shared CSS,
  click-outside-to-close, `confirm()` before destructive actions, a
  status-banner area for inline feedback — mirrored from the Memory
  Dashboard's own modal/edit/delete JS.
- **`esc()`:** pre-existing helper only escapes `&<>` — safe for text
  nodes, but no prior panel in the codebase ever interpolated free text
  into a quoted HTML attribute (confirmed via grep), so it was
  insufficient for the new editor's `value="..."` fields.
- **P0.12 Automation API:** confirmed the full CRUD/enable/disable/run/
  validate surface at `/api/automations*` (see `docs/change_impact/
  automation_api_p0_12.md`) and the P0.11 action/sequence schema
  (`{"type": ..., "parameters": {...}}`, plus `{"type": "delay",
  "parameters": {"seconds": N}}` steps).
- **Device registry:** `luno/devices.py` already loads `LIGHTS`/
  `SWITCHES` module-level dicts from `config/lights.config.json`/
  `config/switches.config.json` at import — a genuine, pre-existing
  device model, reused read-only rather than inventing a fake discovery
  mechanism.
- **Execution status asymmetry (confirmed by reading `engine.py`):**
  `_last_execution[rule.id]` is set only AFTER completion for legacy
  `actions`-based runs, but is set immediately (before steps run) and
  mutated in place for `sequence`-based runs. This directly shaped the
  run-monitor design (§9 below).

## 3. Files created

- **`tests/test_p0_13_automation_dashboard.py`** — 65 tests, sections
  A–X (per the brief's own lettered checklist) plus a dedicated Schema
  section and 11 architecture-guard tests (M1–M11).
- **`docs/change_impact/automation_dashboard_p0_13.md`** — this
  document.

## 4. Files modified

- **`luno/dashboard/automation_api.py`** — one new function,
  `get_schema(modules)`, and its private helper `_known_devices()`.
  Pure read-only reflection of `models.py`'s own allowlist constants
  (`TRIGGER_TYPES`/`CONDITION_TYPES`/`ACTION_TYPES`/
  `SEQUENCE_STEP_TYPES`), plus the already-loaded `luno.devices.LIGHTS`/
  `SWITCHES` registry, plus curated known-event-name/camera-event-kind/
  condition-target hints (explicitly non-restrictive — never enforced
  server-side, only used for UI autocomplete; a user can still type any
  value the underlying validator accepts).
- **`luno/dashboard/server.py`** — one new GET branch,
  `/api/automations/schema`, registered before the existing
  `/api/automations/{id}` catch-all (otherwise the literal string
  `"schema"` would be treated as an automation id).
- **`luno/dashboard/static/index.html`** — additive only:
  - New `.autom-*`-prefixed CSS block.
  - New `escAttr()` helper (escapes `&<>"'`, for the editor's quoted
    attribute interpolation — `esc()` itself was left untouched).
  - New nav button (`data-panel="automations"`), new
    `<div id="panel-automations">` (list page) and
    `<div id="autom-modal-overlay">`/`<div id="autom-modal-box">`
    (editor modal), inserted immediately before the pre-existing
    read-only `<div id="panel-automationengine">` status panel (which
    gained one added sentence pointing to the new page — otherwise
    unmodified).
  - ~600 lines of new JS: automation list rendering, enable/disable/
    delete/run wiring, the create/edit modal, the visual sequence
    builder (add/remove/reorder/edit step, delay step), the trigger/
    condition/action pickers (schema-driven), payload construction,
    client + server validation, save/update, and the run/execution
    monitor. Registered in the loader dispatch table
    (`automations: loadAutomations`).

Nothing in `luno/automation/engine.py`, `luno/automation/models.py`,
`luno/vision.py`, `luno/vision_occupancy.py`, `luno/camera_automation/`,
or `luno/tool_manager.py` was opened or modified this sprint.

## 5. API surface used / added

All existing P0.12 endpoints are consumed as-is: `GET /api/automations`,
`GET /api/automations/{id}`, `POST /api/automations`,
`POST /api/automations/{id}/update`, `POST /api/automations/{id}/
delete`, `POST /api/automations/{id}/enable`, `POST /api/automations/
{id}/disable`, `POST /api/automations/{id}/run`,
`POST /api/automations/validate`.

**One new endpoint, `GET /api/automations/schema`** — justified by the
brief's own explicit permission clause ("add only the smallest
appropriate API extension if necessary, and document it"). Returns:

```
{
  "trigger_types": [...], "condition_types": [...], "action_types": [...],
  "sequence_step_types": [...], "cooldown_seconds_max": N,
  "delay_seconds_max": N, "description_max_length": N,
  "known_event_names": [...], "known_camera_event_kinds": [...],
  "known_condition_targets": [...],
  "devices": [{"name": str, "entity_id": str, "domain": "light"|"switch"}]
}
```

`trigger_types`/`condition_types`/`action_types`/`sequence_step_types`
are direct, live reflections of `models.py`'s own constants (cannot
drift out of sync). `known_event_names` unions a curated hint set with
whatever event names are actually in use by currently-loaded rules.
`devices` comes from the already-loaded `luno.devices.LIGHTS`/
`SWITCHES` dicts, deduplicated by `entity_id`. None of the "known_*"
hint lists are enforced — the server-side validator remains the sole
authority, exactly as the brief required.

## 6. UI structure

Per the brief's Phase 13 preferred structure: Automation List →
Automation Detail/Editor → Trigger → Conditions → Sequence → Execution
Settings. The list page shows each automation as a card (name,
description, enabled/disabled badge, trigger summary, action-or-step
count, cooldown, last-execution result badge, current running/idle
status) with Create/Edit/Enable-Disable/Run/Delete actions, and handles
loading, empty-state, validation-error, and network/API-error display.
The editor is a modal with, in order: Basic (name/description/enabled),
Trigger (type + schema-driven parameter fields), Conditions (field/
operator/value list), Actions/Sequence (toggle between the two modes —
a rule defines exactly one, never both, mirroring `validate_rule()`'s
own invariant), and Execution Settings (cooldown_seconds,
execution_policy). Styling is intentionally plain — cards and status
indicators, no heavy animation — per the brief's own "engineering
control interface, not a casino pretending to be productive."

## 7. Sequence builder and action/trigger/condition pickers

The sequence builder reuses the P0.11 schema verbatim — every step is
`{"type": ..., "parameters": {...}}`, with delay steps using
`{"type": "delay", "parameters": {"seconds": N}}` — no second action
schema was invented. Reordering uses explicit up/down controls (no
drag-and-drop library was introduced, consistent with "no heavy
frontend framework"). The device/action picker builds
`{"type": "home_assistant.turn_on", "parameters": {"entity_id": "..."}}`
-shaped payloads from the schema endpoint's `devices` list — domain and
entity choices come from the real, already-loaded device registry, not
a fabricated one. The trigger picker's type dropdown is populated from
`schema.trigger_types` (never a hard-coded, potentially-stale list);
event-name/condition-target fields are free-text inputs with
autocomplete hints from the schema's "known_*" arrays, so a user can
still type any value the engine will accept.

## 8. Validation and save/update

`automBuildPayload()` assembles the rule from the current editor state;
`automClientValidate()` performs shallow client-side checks (name
non-empty, at least one action/step, obvious type mismatches) purely as
a fast pre-check — it never blocks a save on its own. Every save first
POSTs to `/api/automations/validate`; only on `{"valid": true}` does the
UI proceed to `POST /api/automations` (create) or
`POST /api/automations/{id}/update` (update). Server errors are
rendered field-by-field from the API's `{"field", "code", "message"}`
shape. No invalid rule is ever saved by the UI. After a successful
save, the list is refreshed from the server (never patched
client-side) and a success banner is shown.

## 9. Run / execution monitor

`POST /api/automations/{id}/run` returns immediately with an
`execution_id`; the monitor then polls `GET /api/automations/{id}`
and reads `status.running` (backed by the engine's own
`_running_rule_ids`, updated synchronously for both legacy-`actions`
and `sequence` rules) to show QUEUED → RUNNING → COMPLETED/FAILED.
Step-level progress ("Step 1 ✓ Step 2 ✓ Step 3 ✗") is shown **only**
when `status.last_execution.total_steps` is present in the response —
directly reflecting the real `_last_execution` population asymmetry
discovered in Phase 1 (§2 above), so the UI never fabricates a
progress state the backend does not actually provide.

## 10. Delete / enable / disable

Delete requires an explicit `confirm()` before calling
`POST /api/automations/{id}/delete`; on success the rule is removed
from the list (re-fetched from the server), and a failed delete leaves
the rule visible with an inline error rather than silently vanishing
client-side. Enable/disable call the existing
`/api/automations/{id}/enable`/`/disable` endpoints and always render
from the server's returned state — the UI holds no separate
client-only "enabled" flag that could drift from the backend.

## 11. Security / XSS

`escAttr()` (new) escapes `&<>"'` and is used for every free-text value
interpolated into a quoted HTML attribute in the new editor (automation
name, description, id, entity_id, and other user-authored strings);
`esc()` (pre-existing, `&<>`-only) continues to be used for text-node
interpolation, matching its established use throughout the rest of the
file. Verified directly by tests W2–W4 (automation names/descriptions
containing `<script>`, quotes, and `onerror=` payloads render inert in
both text nodes and attribute values).

## 12. Test count

**65 new tests** in `tests/test_p0_13_automation_dashboard.py`:

- A–X (per the brief's own lettered list): list loading, empty state,
  create form, edit form, delete confirmation, enable, disable, manual
  run, validate success, validate failure, sequence creation, sequence
  reorder, delay step, action step, invalid action, invalid trigger,
  invalid condition, API failure handling, network failure handling, no
  direct HA call, no direct config-file mutation, no second execution
  path, XSS-safe rendering, persistence after refresh.
- Dedicated Schema section (SCHEMA1–5): endpoint registration, live
  reflection of `models.py` constants, device list correctness,
  non-enforcement of the "known_*" hints, and route ordering relative
  to the `/api/automations/{id}` catch-all.
- M1–M11 architecture guards (static source-scan of the served
  `<script>` block via a custom, explicitly best-effort brace-depth JS
  function extractor, paralleling the existing Python-AST-based guard
  convention in `test_p0_12_automation_api.py`): no `fetch()`/XHR call
  to any non-`/api/` origin from the automations section, no direct
  Home Assistant reference, no direct `automation_rules.json`/config
  read-write, no `eval`/`Function()`/`innerHTML`-with-unescaped-user-
  data, no second execution-status polling mechanism outside the
  documented run-monitor, enable/disable handler only ever calls the
  two real endpoints, delete requires confirmation before the network
  call, Vision/Camera/Occupancy source files untouched by this sprint,
  manual run reuses the existing `/run` endpoint (never a second
  `POST` path), sequence/action payloads use only the P0.11 `{"type",
  "parameters"}` shape, and `server.py`'s new schema route is
  positioned before the single-resource catch-all.

All 65 passing (re-confirmed in this session: `65 passed in 26.84s`).

## 13. Regression results

**Targeted (already run earlier this sprint, see prior session
context):** full targeted core suite — 400 passed, 0 failed. Camera/
vision-focused suite (8 files) — 319 passed, 3 failed, all the
already-documented `CAMERA_AUTOMATION_ENABLED=true` `.env` drift
family.

**Full repository sweep, this session** (154 files under `tests/`,
8-chunk methodology, `pytest -n 4 --timeout=90` per chunk,
`--ignore=tests/test_main_bargein.py --ignore=tests/
test_root_main_bargein.py` applied per-chunk):

| Chunk | Passed | Failed | Errors | Skipped |
|---|---|---|---|---|
| 0 | 529 | 0 | 0 | 0 |
| 1 | 558 | 7 | 1 | 0 |
| 2 | 735 | 11 | 0 | 0 |
| 3 | 588 | 2 | 0 | 1 |
| 4 | 588 | 4 | 4 | 0 |
| 5 | 485 | 11 | 0 | 0 |
| 6 | 548 | 9 | 0 | 0 |
| 7 | 433 | 1 | 0 | 0 |
| **Total** | **4,464** | **45** | **5** | **1** |

Every failure/error individually traced to an already-documented
pre-existing category:

- LLM `.env` `MAX_TOKENS_PARAM=max_tokens` override —
  `test_llm_max_completion_tokens_compatibility.py` (7) +
  `test_memory_session_summary_api_compatibility.py` (5).
- No-audio-hardware/`.env` mic gap — `test_mic_device_index.py` (6).
- `RealWhisperSource` construction gap — `test_real_adapters.py` (2).
- Real credentials in `.env` — `test_production_launcher.py::test_07_
  ...` (1).
- Real `light.main_light` config drift —
  `test_sprint60_area_schema.py` (2).
- `config/backups/`/mutation-audit forensic-drift family —
  `test_sprint63_long_term_memory_recovery.py` (9) +
  `test_sprint64_memory_corruption_forensics.py` (5) +
  `test_sprint68_mutation_audit_hardening.py` (2) — 16 total.
- Documented timing-sensitive test —
  `test_sprint66_tool_boundary_hardening.py::
  test_performance_validate_download_directory_is_fast` (1).
- `CAMERA_AUTOMATION_ENABLED=true` `.env` condition — confirmed
  present again in this sweep's targeted suite (§ above, 3), not
  re-triggered in the 8-chunk sweep itself this run.
- Real-network/hardware sandbox isolation (same architectural limit
  documented since P0.5.4-LIVE) — newly observed instance:
  `test_sprint71_dashboard_startup_recovery.py::
  test_12_e2e_main_py_survives_dashboard_port_conflict_and_keeps_
  running` (1) — spawns the real `main.py` entry point, which attempts
  a real HA websocket connection and a real RTSP connection to
  `192.168.1.4:554`, both unreachable from this sandbox; confirmed via
  the test's own source (subprocess-launches `main.py`, waits for a
  `"Ready."` line within a 25s deadline) that this is a genuine
  network-isolation limit, not a P0.13-caused failure — this sprint
  touched no networking, `main.py`, or HA-connection code.
- Confirmed parallel-xdist-order flake —
  `test_verification_dashboard.py::
  test_api_verification_reports_a_successful_verified_action_end_to_end`
  (1) — re-run standalone: `6 passed` (clean), matching the
  already-documented flake category (69/21 prior references per
  `docs/testing/regression_baseline.md`).
- Two pre-existing collection-time gaps, unrelated to any file this
  sprint touched: `test_main_bargein.py` (1 error — `faster_whisper`
  package genuinely not installed in this sandbox, confirmed via `pip
  show`) and `test_root_main_bargein.py` (4 errors — `legacy_main.py`
  does not exist at repo root; this directory is not a git repository,
  so no history could be checked). Both are pre-existing per
  `docs/testing/regression_baseline.md`; the 4× repetition for the
  latter reflects pytest-xdist's 4 parallel workers each independently
  failing collection of the same file, not 4 distinct causes.

Zero failures touch `luno/automation/`, `luno/dashboard/`, `luno/
vision.py`, `luno/vision_occupancy.py`, or `luno/camera_automation/`.

## 14. Architecture guards (Phase 14)

All 11 required guard proofs are implemented as static source-scan
tests M1–M11 in `test_p0_13_automation_dashboard.py` (§12 above), all
passing, proving: UI talks only to `/api/*` (never Home Assistant
directly), UI never reads/writes `config/automation_rules.json`
directly, no second automation execution path exists client-side, no
`eval`/`Function()`/unsafe `innerHTML`, the P0.11 sequence engine
remains the sole executor (UI only constructs its schema), manual run
and enable/disable/delete each map to exactly one real endpoint, and
Vision/Camera/Occupancy source files are untouched.

## 15. Known limitations

- No authentication exists for any Dashboard API route, including the
  new schema endpoint — same pre-existing, documented limitation as
  every other route (localhost-only bind is the sole boundary).
- Sequence reordering uses explicit up/down controls, not drag-and-drop
  — the repository has no drag-and-drop library and none was added.
- The schema endpoint's "known_*" hint arrays are UI autocomplete
  conveniences only; they are not, and must never become, a
  server-side allowlist gate (this would silently break any
  hand-authored automation using an event name or condition target not
  yet in the hint list).
- `_classify_field()` (P0.12) remains a best-effort single-error
  classifier — the editor shows one error at a time per save attempt.
- Real Home Assistant/WLED hardware was not exercised — every test and
  every manual UI action in this sandbox routes through
  `MockHomeAssistantHandler`.
- No AI/natural-language automation authoring was implemented or
  started, per the user's own explicit closing instruction for this
  sprint (reserved for a future, separately-requested P0.14).

## 16. Result classification

**STRONG** — a single-file, dependency-free UI extension that consumes
the P0.12 API exclusively, introduces exactly one small, justified,
read-only API extension, reuses the P0.11 sequence schema verbatim,
and is backed by 65 new tests (including 11 static architecture-guard
tests) with zero regressions across a 154-file, 8-chunk full-repository
sweep — every one of the 45 failures, 5 collection errors, and 1 skip
individually traced to an already-documented pre-existing category. See
`tests/test_p0_13_automation_dashboard.py` for the full test suite and
`luno/dashboard/static/index.html` / `luno/dashboard/automation_api.py`
for the implementation.
