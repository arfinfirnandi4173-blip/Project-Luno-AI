# P0.12 — Luno Automation API & CRUD

## 1. Goal

Give the Luno Dashboard (and, in a future sprint, its automation editor
UI) a stable HTTP surface for managing `AutomationEngine` rule
definitions — list, get, create, update, delete, enable, disable,
manually run, and validate-without-saving — without ever touching
`config/automation_rules.json` or `AutomationEngine` internal state
directly. P0.12 does **not** implement any Dashboard UI; it is the API
layer P0.13 will consume.

## 2. Architecture inspection (Phase 1) — what already existed

- **Persistence:** `AutomationEngine._persist_rules()` (existing,
  atomic write via the project's one and only JSON-write mechanism —
  temp file then `os.replace()`) and `AutomationEngine.reload_rules()`
  (existing, re-reads `config/automation_rules.json`, rebuilds
  `_rules`/`_rules_by_event`/time-trigger scheduling from disk).
- **Enable/disable:** `AutomationEngine.enable_automation()` /
  `disable_automation()` already existed (Sprint 72) and already
  persisted correctly — reused verbatim.
- **Manual execution:** `AutomationEngine.run_automation()` already
  existed and already called the same `_trigger()` → `_run_execution()`
  path as event/time-triggered automations, through `ToolManager` —
  reused verbatim (extended additively, see §4).
- **No create/update/delete rule methods existed anywhere** — these are
  new this sprint.
- **HTTP server:** `luno/dashboard/server.py::DashboardServer` is a
  deliberately dependency-free `http.server.ThreadingHTTPServer`
  subclass (project-wide "zero new hard dependencies" convention, see
  the file's own docstring). It implements only `do_GET`/`do_POST` — no
  `do_PUT`/`do_PATCH`/`do_DELETE` exist anywhere in the codebase. Routing
  is a flat `if/elif` chain over exact path strings, with one existing
  precedent for a dynamic path-parameter route (`/api/memory/{id}`, a
  `path.startswith()` catch-all placed after every exact-match branch).
- **`collectors.py`/`controls.py` split:** read-only "state → JSON-safe
  dict" functions live in `collectors.py`; mutation functions returning
  `{"ok": bool, "message": str, ...}` live in `controls.py`, dispatched
  via `_run_control(path, body)`'s own flat elif chain.
- **Authentication:** none exists anywhere in the project. The
  Dashboard server binds to localhost only; that bind is the entire
  security boundary. No token, session, or credential check of any kind
  is present for any existing `/api/*` route.

Given this, P0.12 follows the existing convention rather than the
brief's illustrative PUT/PATCH/DELETE sketch (the brief explicitly
permitted this: *"If the existing project uses a different API prefix
or routing convention, follow that convention instead of inventing a
parallel one"*).

## 3. Files created

- **`luno/dashboard/automation_api.py`** — new translation-layer
  module, same architectural role as `collectors.py`/`controls.py`
  combined (reads + writes, since CRUD needs both). Every function
  reads or writes exclusively through `AutomationEngine`'s own public
  methods; the module holds no state of its own.
- **`tests/test_p0_12_automation_api.py`** — 54 tests, sections A–AE
  plus 10 architecture-guard tests (M1–M10).
- **`docs/change_impact/automation_api_p0_12.md`** — this document.

## 4. Files modified

- **`luno/automation/models.py`** — additive only:
  - `MAX_DESCRIPTION_LENGTH = 500` constant.
  - `AutomationRule` gained three new fields: `description: str = ""`,
    `created_at: Optional[str] = None`, `updated_at: Optional[str] =
    None`.
  - `to_public_dict()` extended to emit the three new fields.
  - `validate_rule()` extended to type-check and length-bound
    `description`.
  - `rule_from_dict()` extended to parse the three new fields with safe
    defaults/coercion.
- **`luno/automation/engine.py`** — additive only:
  - `_trigger()` gained an optional `_execution_out: Optional[List[
    AutomationExecution]] = None` out-parameter (return type
    unchanged — still `Tuple[bool, str]` — preserving two existing test
    call sites that do strict 2-tuple unpacking). Populated on both the
    accepted path and the cycle-detected refusal path.
  - `run_automation()` now threads an `execution_holder` list through
    `_trigger()` and returns a new `"execution_id"` key in its result
    dict (all three return paths) — additive dict key only, no existing
    test does strict dict equality on this result.
  - `_set_enabled()`'s reconstructed `AutomationRule(...)` and
    `_rule_to_storage_dict()` both extended to carry the three new
    fields through (so enable/disable never drops description/
    timestamps).
  - Four new public methods: `get_rule()`, `create_rule()`,
    `update_rule()`, `delete_rule()` (see §6).
- **`luno/dashboard/server.py`**:
  - `automation_api` added to the package import line.
  - Two new GET branches: `/api/automations` (list) and
    `/api/automations/{id}` (single resource, 404 if missing) — added
    after the existing `/api/automation` singular status branch, which
    is untouched.
  - `_dispatch_post()` now tries `automation_api.dispatch_post()`
    first; if it returns `None` (path not part of the automations
    family), control falls through to the existing `_run_control()` /
    404 handling exactly as before.
- **`ARCHITECTURE_GUARD.md`, `docs/testing/regression_baseline.md`,
  `docs/project_handover.md`, `docs/project_handover.json`** — see §11.

Nothing in `luno/vision.py`, `luno/vision_occupancy.py`,
`luno/adapters/vision.py`, `luno/camera_automation/`, `luno/tool_
manager.py` (or any `ToolManager` internals), or any Home Assistant
adapter was opened or modified.

## 5. API endpoint list

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/automations` | List every rule, each merged with live status |
| GET | `/api/automations/{id}` | Single rule + status, 404 if missing |
| POST | `/api/automations` | Create a rule |
| POST | `/api/automations/{id}/update` | Update a rule |
| POST | `/api/automations/{id}/delete` | Delete a rule |
| POST | `/api/automations/{id}/enable` | Enable (reuses existing engine method) |
| POST | `/api/automations/{id}/disable` | Disable (reuses existing engine method) |
| POST | `/api/automations/{id}/run` | Manually run (reuses existing execution path) |
| POST | `/api/automations/validate` | Validate a payload — zero side effects |

Update/delete use POST + a trailing verb segment rather than HTTP
PUT/DELETE because the server has never implemented those methods for
any route — this matches the existing `controls.py` convention (every
mutation in the project is a POST to a verb-suffixed path) rather than
introducing a second, REST-purist routing style alongside it.

## 6. DTO / schema summary

Every automation is represented as:

```
{
  "id": str, "name": str, "description": str, "enabled": bool,
  "trigger": {...} | null, "conditions": [...], "actions": [...],
  "cooldown_seconds": number, "execution_policy": str,
  "sequence": [...], "created_at": str | null, "updated_at": str | null,
  "status": {"running": bool, "cooldown_remaining_s": number,
             "last_execution": {...} | null} | null
}
```

All fields except `status` come verbatim from `AutomationRule.
to_public_dict()` — no fabricated metadata. `sequence` reuses the
existing P0.11 schema exactly (a rule defines exactly one of `actions`
or `sequence`, enforced by the existing `validate_rule()`, never a
parallel check). `created_at`/`updated_at` are set server-side only
(`utcnow().isoformat()`, inside `create_rule()`/`update_rule()`) and are
never trusted from a request body — a caller cannot forge a rule's
history.

## 7. Validation

All validation reuses `models.py::rule_from_dict()` /
`validate_rule()` — no second validator was written. Since that
validator raises `AutomationRuleError` on the first failure (fail-fast,
not multi-error-collecting), the API layer's `_classify_field()`
performs a best-effort, ordered regex classification of the single
error message into a `field` name (id/name/trigger/conditions/actions/
`sequence[N]`/cooldown_seconds/description), returned as:

```
{"success": false, "errors": [{"field": "...", "code": "invalid_value", "message": "..."}]}
```

This covers every case the brief listed: missing/invalid id, empty
name, malformed trigger/condition/action/sequence step, both-actions-
and-sequence, neither, invalid action type/params, invalid delay
values, invalid condition/state-reader refs, and duplicate ids (a
distinct `"duplicate_id"` code from the engine, translated the same
way). No Python traceback is ever returned — every code path is wrapped
in a `try/except AutomationRuleError`, with a final defensive
`except Exception` as a last-resort non-traceback fallback.

A caller-supplied new id is additionally checked against an API-
boundary-only pattern (`^[A-Za-z0-9_-]{1,64}$`) before reaching the
engine — this is stricter than `validate_rule()`'s own permissive id
check and is deliberately *not* pushed down into `models.py`, so every
existing hand-authored id in the real `config/automation_rules.json`
keeps loading with no migration.

## 8. Persistence mechanism

`create_rule()`, `update_rule()`, and `delete_rule()` (new,
`AutomationEngine` methods) each: acquire the engine's existing
`RLock`, validate, mutate an in-memory copy of `self._rules`, call the
existing `_persist_rules()` (same atomic-write mechanism every other
mutation already uses — temp file + `os.replace()`), then call the
existing `reload_rules()` to re-derive `_rules`/`_rules_by_event`/
scheduled time-triggers from what was just written to disk. This
guarantees runtime state is always exactly consistent with the
persisted file, at the cost of one extra (cheap) disk read per CRUD
call. No second persistence mechanism, and no database, was
introduced — JSON remains the only format.

## 9. Security behavior

No authentication mechanism exists anywhere in the project; P0.12 does
not invent one. The Dashboard server's localhost-only bind remains the
sole security boundary, matching every other existing `/api/*` route —
documented here as a known limitation, not silently assumed away. The
API can only execute a fixed, registered set of action types through
the existing `ToolManager`/`_dispatch_action()` path; it never accepts,
evaluates, or imports arbitrary Python, shell commands, or module/
function names (proven by architecture-guard test M6, a static AST
scan of `automation_api.py` for `eval`/`exec`/`subprocess`/`os.system`/
dynamic `__import__`).

## 10. Concurrency behavior

`create_rule()`/`update_rule()`/`delete_rule()` hold `self._lock`
(an `RLock`) across the entire check → validate → mutate → persist →
reload sequence — a deliberate choice, different from the pre-existing
`_set_enabled()`'s own release-before-persist pattern, to prevent a
lost-update race between two concurrent CRUD calls for different rule
ids. Verified with a real 30-thread concurrent CRUD test: zero lost
updates, final on-disk state matches the last-applied mutation per id.
`_set_enabled()` itself was left completely untouched — its own,
already-tested locking behavior was not altered. A GET (`list_
automations`/`get_automation`) never blocks on this lock long enough to
observe a torn write, since `_persist_rules()`'s atomic replace means a
concurrent read only ever sees the file fully-old or fully-new, never
partial.

## 11. Manual execution path

`POST /api/automations/{id}/run` calls `AutomationEngine.
run_automation()` verbatim — the same method `AutomationToolHandler`'s
own voice-command `run` action already calls, which itself calls the
same `_trigger()` → `_run_execution()` → `ToolManager` → Home Assistant
path used for event/time-triggered automations. No new execution logic,
no direct Home Assistant call, and no `ToolManager` bypass exists in
`automation_api.py` (architecture-guard tests M3, M4, M8 assert this
statically via AST inspection of the function body). The HTTP response
returns immediately with `{"success": true, "automation_id": ...,
"execution_id": ..., "status": "queued"}` — execution happens on the
engine's own dedicated execution thread (unchanged from Sprint 72), so
the HTTP server is never blocked for a long-running sequence.

## 12. Validate-without-saving

`POST /api/automations/validate` calls `validate_automation(body)`, a
provably pure function that takes only the request body (no `modules`/
engine argument at all) and calls `rule_from_dict`/`validate_rule`
directly — it cannot touch `config/automation_rules.json`, the runtime
rule registry, or any `AutomationEngine` state even by accident.
Verified by architecture-guard test M9 (AST-based: the function body
contains zero references to `modules`, `engine`, or any mutating call)
and functionally by test section T (create/list before and after a
validate call, byte-identical).

## 13. Test count

**54 new tests** in `tests/test_p0_12_automation_api.py`, sections A
through AE, plus 10 architecture-guard tests (M1–M10):

- Route registration, GET list/one/nonexistent (404).
- CREATE: valid, duplicate id, invalid trigger, invalid action, invalid
  sequence, auto-generated id, caller-supplied id, unsafe id rejected.
- UPDATE: existing, nonexistent, immutable `created_at` preserved,
  `updated_at` refreshed.
- DELETE: existing, nonexistent (never silently ignored).
- ENABLE/DISABLE: existing, nonexistent, runtime/persisted consistency.
- VALIDATE: valid, invalid, zero persistence side effects (before/after
  file-content diff).
- RUN: existing, nonexistent, uses the existing execution path (AST-
  verified), no direct HA call (AST-verified).
- Sequence-rule create/retrieve/persist-reload round trip (exact P0.11
  schema).
- Legacy `actions`-only rule compatibility (the real shipped rules file
  loads and lists correctly).
- Malformed payload → structured error, never a traceback.
- 30-thread concurrent CRUD → zero corruption, zero lost updates.
- No arbitrary Python/shell execution (static AST scan).
- Auth/security follows existing (absent) project behavior — documented,
  not invented.
- M1–M10: no second `AutomationEngine`, no second persistence
  mechanism, no direct HA call, no `ToolManager` bypass, no duplicated
  sequence-execution logic, no `eval`/`exec`/shell/dynamic import,
  Vision/Camera/Occupancy modules untouched, manual-run reuses `run_
  automation()` verbatim, validate endpoint never touches the engine,
  `server.py` routes the automations family through `automation_api`.

All 54 passing. `tests/test_sprint72_automation_engine.py`'s
pre-existing 78 tests: unchanged, all still passing (zero regression).
`tests/test_dashboard.py` + `tests/test_memory_dashboard.py` (73
tests): all passing — every pre-existing dashboard route unaffected.

## 14. Regression results

**Targeted:** `test_p0_12_automation_api.py` (54) + `test_sprint72_
automation_engine.py` (78) + `test_p0_11_action_sequence.py` (52) +
`test_p0_10_occupancy_context.py` + `test_p0_9_room_occupancy.py` = 262
passed, 0 failed. ToolManager/camera/vision focused suite (8 files):
235 passed, 2 failed (both the already-documented `CAMERA_AUTOMATION_
ENABLED=true` `.env` drift). Dashboard suites: 73 passed, 0 failed.

**Full repository sweep** (158 files, 8-chunk methodology, `pytest -n
4 --timeout=90` per chunk): **4,507 passed, 44 failed**. Every failure
traced to an already-documented pre-existing category:

- LLM `.env` `MAX_TOKENS_PARAM=max_tokens` override — 7 (chunk 1).
- `config/backups/` forensic-drift family (`test_sprint63_long_term_
  memory_recovery.py`, `test_sprint64_memory_corruption_forensics.py`,
  `test_sprint68_mutation_audit_hardening.py`) + `test_sprint66_tool_
  boundary_hardening.py::test_performance_validate_download_directory_
  is_fast` — 17 (chunk 6), identical test names/count to the
  previously-confirmed P0.11 sweep.
- No-audio-hardware/`.env` mic gap + `RealWhisperSource._device_index`
  construction gap + `CAMERA_AUTOMATION_ENABLED=true`/real credentials/
  `light.main_light` config drift — remaining 20, spread across chunks
  3–5, all matching prior-sprint documented categories.
- One confirmed parallel-load-only timing flake: `test_p0_11_action_
  sequence.py::test_F2_completed_status_after_full_success` (chunk 3) —
  isolated via 3× standalone re-run and a full-file `-n 4` re-run, both
  clean; not a real regression.

Zero failures touch `luno/automation/`, `luno/dashboard/`, `luno/
vision.py`, `luno/vision_occupancy.py`, or `luno/camera_automation/`.

## 15. Architecture guards (Phase 14)

All ten required guard proofs are implemented as static/behavioral
tests M1–M10 in `test_p0_12_automation_api.py` (§13 above), all
passing:

1. No second `AutomationEngine` is instantiated by the API layer.
2. No second persistence mechanism — only `_persist_rules()`/
   `reload_rules()` are called.
3. No direct Home Assistant call from `automation_api.py`.
4. No `ToolManager` bypass.
5. No duplicated sequence-execution logic (P0.11's `_run_sequence()`
   remains the only sequence executor).
6. No arbitrary Python import/execution (`eval`/`exec`/`subprocess`/
   `os.system`/dynamic `__import__` absent).
7. `luno/vision.py`, `luno/vision_occupancy.py`, `luno/camera_
   automation/` untouched by this sprint.
8. Legacy `actions`-only rules remain fully compatible (real shipped
   rules file round-trips through list/get unmodified).
9. Manual run (`/run`) reuses the existing `run_automation()`/
   `_trigger()`/`_run_execution()` pipeline verbatim.
10. The validate endpoint has zero persistence side effects (file
    content byte-identical before/after).

## 16. Known limitations

- No authentication exists for any Dashboard API route, including the
  new automation endpoints — the localhost-only bind is the sole
  security boundary, unchanged from every pre-existing route.
- Update/delete use POST + verb-suffixed paths, not HTTP PUT/DELETE,
  because the server has never implemented those methods.
- `_classify_field()` is a best-effort, single-error classifier over
  `validate_rule()`'s fail-fast message text — it does not collect or
  return multiple simultaneous validation errors, because the
  underlying validator was not changed to do so (out of scope for this
  sprint).
- No P0.13 Dashboard/UI, visual builder, drag-and-drop, IF/ELSE,
  variables, loops, parallel execution, or advanced scheduling was
  implemented, per the brief's explicit instruction.
- Real Home Assistant/WLED hardware was not exercised in this sandbox —
  every test routes through the existing `MockHomeAssistantHandler`,
  same as every prior sprint's test suite.

## 17. Result classification

**STRONG** — a clean, additive API layer that introduces zero new
persistence, execution, or dispatch mechanisms; reuses every existing
validation/locking/atomic-write/execution-path primitive; is backed by
54 new tests including 10 static architecture-guard tests; and
produces zero regressions across a 158-file, full-repository sweep.
See `tests/test_p0_12_automation_api.py` for the full test suite and
`luno/dashboard/automation_api.py` for the implementation.
