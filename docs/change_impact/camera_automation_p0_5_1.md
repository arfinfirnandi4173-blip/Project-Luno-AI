# Camera Automation — P0.5.1: Real Tapo C212 Entity Discovery

## Objective

Perform read-only discovery of the real Home Assistant entities exposed
by the user's Tapo C212, to obtain actual entity IDs for configuring the
already-implemented P0/P0.5 pipeline (`config/camera_automation.json`).
This sprint is discovery and verification only — no new automation
behavior, no architecture changes.

## Files touched

| File | Type | Justification |
|---|---|---|
| `ha_camera_discovery.py` | Rewritten (standalone script, not imported by any production module) | Fixed a real ordering bug (see below) and added registry-based classification per Sections 5–12 of the brief. |
| `tests/test_ha_camera_discovery.py` | New | Smallest-possible test file for the script's pure `_build_report()` classification logic, per Section 16. |
| `docs/change_impact/camera_automation_p0_5_1.md` | New | This document. |

**No file under `luno/` was modified.** Event Bus, `AutomationEngine`,
`HomeAssistantClient` (`luno/ha_client.py`), the camera automation
module/config/cameras package, and `config/camera_automation.json` are
all untouched. This satisfies the brief's stated preference of "ZERO
existing production-code modifications."

## Bug found and fixed (permitted under Section 19 — no architecture change required)

The P0.5-era version of `ha_camera_discovery.py` called
`client.get_states()` immediately after `client.connect()`, without
ever starting `client.listen_and_dispatch()` as a background task.
`luno/ha_listener.py` documents that `pending_responses` — the dict
`get_states()` polls for its response — is only ever populated by
`listen_and_dispatch()`'s own receive loop. Without that loop running
concurrently, `get_states()` (and this sprint's new registry calls)
would silently time out and return nothing, even on a fully successful
connection. Confirmed the correct pattern already in use in
`luno/adapters/real_home_assistant.py` (lines 100–127):
`listen_and_dispatch()` started as a background task *before* any
request/response call.

Fixed entirely inside the standalone script — `luno/ha_client.py` was
not touched. The fix has not been exercised against a live server
(connection remains blocked in this sandbox); this is stated honestly
as a known limitation below.

## Discovery strategy implemented

1. Connect via the existing `HomeAssistantClient` — no second client.
2. Call `get_states()` (existing method) for a snapshot.
3. Call the two standard, read-only HA websocket commands
   `config/entity_registry/list` and `config/device_registry/list` via
   a new local helper, `_send_and_wait()`, which reuses the *same*
   connected client's own `ws` / `msg_id` / `pending_responses` /
   `call_lock` attributes — the same ones `get_states()` uses
   internally — generalized to any read-only command type. If the
   connected HA account lacks admin rights, these calls fail
   gracefully and are reported as unavailable, never as "camera not
   found."
4. Classify entities by real relationship evidence — entity →
   device → manufacturer/model — rather than by name alone. A
   `camera.*` entity is only marked `confirmed_via_device_registry` if
   its linked device's manufacturer/model/name matches a Tapo/TP-Link
   hint. Motion/human/availability entities are first searched among
   entities on the *same device* as the confirmed camera, classified
   by HA's own `device_class` convention (`motion` / `occupancy`,
   `presence` / `connectivity`). Only if registry data is unavailable
   does the script fall back to keyword-only matching, and that
   fallback is explicitly labeled unconfirmed in both the human
   report and the JSON (`confirmed_via_device_registry: false`).
5. Human detection is never conflated with generic motion — a device
   with only a motion sensor reports `human.found: false`.
6. pytapo/HA same-physical-camera relationship: `same_physical_camera`
   is only ever `true` when the discovered camera device's HA
   `connections` list contains an entry matching the configured
   `TAPO_HOST`. It is `false` (not `true`) if a camera device was
   found but no such evidence exists, and stays unset (`null`) if no
   camera was found at all — never guessed.

## Read-only guarantee

`call_service()` is never invoked anywhere in the script. No PTZ
movement, no snapshot capture, no integration reload, no HA restart.
`HA_TOKEN` is never printed (only whether it is configured, as a
boolean). `config/camera_automation.json` is never opened for writing —
verified statically by `test_08_never_opens_any_file_for_writing`
(no `open(..., "w"/"a")` call anywhere in the script's source).

## Live verification attempt

Ran `python ha_camera_discovery.py` against the real, configured
`HA_URL` / `HA_TOKEN` in this sandbox:

```
[HA] Connecting to wss://lha.czdelta.biz.id/api/websocket...
[HA] ✗ Connection failed: proxy rejected connection: HTTP 403

HA DISCOVERY: BLOCKED

Reason:
    connect() returned False ...
No entity IDs were modified or invented.
{
  "ha_reachable": false,
  "reason": "connect() returned False ..."
}
```

This is the sandbox's own outbound-network policy (identical `HTTP 403`
seen for all outbound HTTPS/WSS in this environment across P0, P0.5,
and now P0.5.1) — not a Luno defect, and not evidence that the Tapo
C212 is absent from Home Assistant. Per Section 14, this is correctly
reported as `HA DISCOVERY: BLOCKED`, distinct from "HA reachable,
camera not found." **Tapo C212 presence/absence in Home Assistant
remains undetermined.** Running the script on the user's own machine
(where this sandbox's network restriction does not apply) is the
missing step to actually answer that question.

## Tests

`tests/test_ha_camera_discovery.py` — 8 new tests covering
`_build_report()`'s pure classification logic: full-registry
confirmed classification of all four roles; pytapo relationship only
confirmed with real connections-list evidence; unrelated entities
never misclassified; keyword-only fallback correctly marked
unconfirmed when registry is unavailable; human never conflated with
motion when absent; no false pytapo CONFIRMED claim without a camera
device; keyword/device-hint helper functions; static proof of no
file-write calls. All 8 pass.

Live websocket behavior (the `listen_and_dispatch` ordering fix
itself) is not unit-testable without a real or mocked websocket
server and was out of scope for "smallest possible test" (Section 16);
it is instead documented here and in the script's own docstring.

## Regression

- `tests/test_p0_camera_automation.py` (23 tests) — all pass.
- `tests/test_p0_5_camera_integration.py` (36 tests) — all pass.
- `tests/test_sprint68_mutation_audit_hardening.py` — 65/67 pass; 2
  pre-existing failures (`test_baseline_no_real_mutation_audit_dir_exists_before_any_real_write`,
  `test_backup_count_unchanged_by_this_entire_test_file`) are
  environmental drift from `config/backups`/`logs/mutation_audit`
  accumulating real files over prior days of work (oldest backup file
  dated Aug 11, newest Aug 18 — all predate this sprint). Confirmed
  unrelated: this sprint touched no file under `luno/`, wrote no
  backup, and ran no mutation-audited operation. Not fixed here per
  Section 17 ("Do not modify production code merely to make unrelated
  tests pass").

## Known limitations

- The `listen_and_dispatch()` ordering fix has never been exercised
  against a real, reachable HA server — only against the blocked-
  connection path and against synthetic `states`/`entity_registry`/
  `device_registry` fixtures in the new test file.
- Registry commands (`config/entity_registry/list`,
  `config/device_registry/list`) require admin rights on the HA
  account tied to `HA_TOKEN`; if that account lacks them, the script
  falls back to unconfirmed keyword matching. This was never verified
  against a real HA instance in this sandbox.
- Tapo C212 presence/absence in Home Assistant is still unknown.

## Next step

Run `python ha_camera_discovery.py` on the user's own machine. Review
its human-readable report and JSON output by hand, then — separately,
manually — copy any confirmed entity IDs into
`config/camera_automation.json`. Discovery and configuration remain
deliberately separate steps.
