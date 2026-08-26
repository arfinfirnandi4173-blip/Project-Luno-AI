# Camera Automation — P0.5.4-LIVE: Real Camera Proof-of-Life

## Environment

- **Luno machine:** `E:\Luno Evo` (per the brief). **Important
  clarification, established this sprint and central to this report:**
  my own code-execution shell is always an isolated cloud development
  sandbox — even when a folder like `E:\Luno Evo` is mounted so I can
  read/write files in it, *running* a command does not execute on the
  user's own Windows PC. Re-confirmed directly this sprint (not
  assumed): `hostname` reports `claude` on Linux kernel `6.8.0`, and a
  fresh TCP probe to `TAPO_HOST` fails with `OSError: [Errno 101]
  Network is unreachable` on ports 443/554/80. This is a permanent,
  structural property of my own tool access, not a fixable
  configuration issue, and not the same kind of blocker Section "HARD
  STOP" anticipates (a pipeline defect) — it means **I personally
  cannot run the live test**, full stop.
- **Camera:** Tapo C212 (per `.env`, values not printed).
- **RTSP source identity (no secrets):** unchanged from P0.5.4 —
  `rtsp://<user>:<pass>@<TAPO_HOST>:554/stream1`, auto-derived, never
  printed.
- **Vision configuration:** `CAMERA_VISION_ENABLED=true`,
  `VISION_BACKEND=real` already set in this checkout's `.env`.
- **Test date/time:** 2026-08-20.

## What this sprint actually delivers

Since I cannot execute against the real camera, this sprint's
deliverable is a ready-to-run, read-only, self-contained script the
**user** runs themselves on the machine that can actually reach the
camera — `luno_live_camera_event_observer.py` (repository root,
alongside the existing `ha_camera_discovery.py`/
`tapo_camera_event_audit.py` precedent). It was written, unit-tested
(13 new tests, all passing, covering everything that does NOT require
real hardware), and smoke-tested end-to-end against the real Event Bus
in this sandbox using a **simulated** `camera_person_entered` event
(never claimed to be a real camera detection) — proving the script's
own wiring (bootstrap → subscribe → print → clean shutdown) is correct
before handing it to the user.

### What the script does

1. **Pre-flight** (read-only): `TAPO_HOST`/`TAPO_USERNAME`/
   `TAPO_PASSWORD` configured (booleans only, never values); TCP
   reachability to `TAPO_HOST:443` and `:554`; `ultralytics`
   importable; `cv2` importable. Prints PASS/FAIL per check.
2. If any **critical** check fails, the script **hard-stops** and does
   not start the runtime at all — mirroring this sprint's own "HARD
   STOP: do not modify networking/configuration/dependencies" rule,
   applied to the script's own execution.
3. If pre-flight passes, boots the real, existing module stack via the
   exact same `register_all_modules()`/`register_all_adapters()` every
   test in this repo already uses — no new bootstrap path.
4. Sets `CAMERA_AUTOMATION_ENABLED=true` in the script's **own process
   only** (`os.environ`, never `.env`, never
   `config/camera_automation.json`) — the existing, documented way to
   opt into Camera Automation.
5. Subscribes a temporary, print-only observer to
   `camera_automation.camera_event` (safe to print in full — never
   contains a credential) and, for the full trace, to the four raw
   Vision events — for those four, **only the event type and a
   timestamp are ever printed, never `event.data`**, because
   `CameraDisconnected`/`CameraReconnected`'s own payload can contain
   the full, credentialed RTSP URL (`luno.vision.camera_status()
   ["source"]`). This is proven statically and behaviorally by this
   sprint's own test file, not merely asserted.
6. Runs for `--duration` seconds (default 120), then cleanly shuts down
   via the existing `ShutdownCoordinator`, unsubscribing itself and
   leaving no permanent debug subscriber.

### What it never does

Never modifies any file under `luno/`. Never touches
`config/camera_automation.json`. Never calls a Home Assistant service.
Never sends a PTZ command. Never prints a credential, token, or
RTSP URL.

## Pre-flight (run from this sandbox — informative, not the live test)

| Check | Result |
|---|---|
| TAPO_HOST | PASS (configured) |
| TAPO_USERNAME | PASS (configured) |
| TAPO_PASSWORD | PASS (configured) |
| Camera reachable (TCP 443) | FAIL — `Network is unreachable` |
| RTSP reachable (TCP 554) | FAIL — `Network is unreachable` |
| ultralytics (YOLO) | FAIL — not installed in this sandbox |
| cv2 (OpenCV) | PASS (5.0.0) |

Running the script from this sandbox correctly and immediately
hard-stops without attempting to boot the runtime — this is the
expected, correct behavior here, and directly demonstrates the
script's own safety logic works. **This table describes this sandbox,
not the user's real machine** — the user must run the script
themselves to get a real pre-flight table.

## Live tests

| Test | Result |
|---|---|
| Idle | NOT PERFORMED |
| Human enter | NOT PERFORMED |
| Human stay | NOT PERFORMED |
| Human exit | NOT PERFORMED |
| Re-entry | NOT PERFORMED |
| Camera disconnect | NOT PERFORMED |
| Camera reconnect | NOT PERFORMED |

**Reason for all seven:** I cannot execute code with access to the
user's real network from within my own tool environment — confirmed
structurally this sprint (see Environment section above), not a
pipeline defect and not something a code change can fix. Per this
sprint's own explicit rule ("Never turn NOT PERFORMED into PASS"), none
of these are fabricated.

## Most important evidence (what CAN be shown)

A simulated trace, run against the real Event Bus and real bootstrap in
this sandbox, using a `camera_person_entered` event constructed exactly
the way `VisionAdapter` itself would publish it (never claimed to be a
real hardware detection):

```
[Vision] camera_person_entered observed
[CAMERA EVENT]
    kind=human_detected
    camera_id=tapo_c212
    entity_id=vision:camera_person_entered
    confidence=None
    source=vision
    timestamp=1787220193.2903378
```

This proves the **transport** (Event Bus → bridge →
`camera_automation.camera_event`) is wired correctly and that the
observer script correctly prints and records it — it is explicitly
**not** a real-camera trace and is not presented as one.

## Code changes

```text
Production code modified:
    0
```

Two new, standalone files only:

```text
[NEW] luno_live_camera_event_observer.py
[NEW] tests/test_luno_live_camera_event_observer.py
[NEW] docs/change_impact/camera_automation_p0_5_4_live.md
```

Confirmed via `find luno -newer <P0.5.4's own change-impact doc>` —
zero results. No file under `luno/` was touched.

## Regression

**Baseline (recorded before any activity this sprint):** 333 passed, 0
failed (identical to P0.5.4's own final count — unchanged since P0.5.4
made no code changes either).

**After:** 333 + 13 new (`tests/
test_luno_live_camera_event_observer.py`) = **346 passed, 0 failed.**
`tests/test_sprint68_mutation_audit_hardening.py` spot-check: 65/67,
same 2 pre-existing unrelated environmental failures documented since
P0.5.1.

## Definition of done — honest accounting

- Real Tapo C212 tested: **NO** — I cannot reach it from my own
  execution environment; this is now confirmed to be a permanent
  constraint, not a transient sandbox limitation to work around.
- Human enter/exit produced the expected Vision event: **NOT
  VERIFIED** — no live event occurred.
- No event was fabricated: **YES**.
- No automation action executed: **YES** (nothing was run against real
  hardware at all).
- No PTZ movement: **YES**.
- No HA control service called: **YES**.
- Regression remains clean: **YES** (346/346).
- A ready-to-run, tested, safe script now exists for the user to
  perform Tests A–F themselves and report the results back.

## Next step (for the user, not a future sprint to implement)

Run, on the real Luno machine, in its own `.venv`:

```
python luno_live_camera_event_observer.py --duration 120
```

Walk in front of the Tapo C212 partway through the window, then leave
frame, then re-enter. Copy the printed `[CAMERA EVENT]` trace and the
final summary back — that output is the actual Test 1–5 evidence this
sprint's own brief asks for, and it is the one thing I am structurally
unable to produce myself.
