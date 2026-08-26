# P0.8.8 - Fix the Confirmed Camera Automation Event Suppression Bug

## 1. Brief (summary)

Following P0.8.7's WLED verification-freshness fix, the user asked why the WLED still would
not turn on despite the person clearly being detected repeatedly ("di sini person sudah
terdeteksi tapi kenapa wled ngga nyala?"). Direct investigation (documented in the same
conversation, before this formal brief was issued) proved a real, reproducible bug in
`CameraAutomationModule._publish_if_not_suppressed()`: the suppression `state` being compared
was derived from / identical to part of the suppression `key` itself, making the dedupe check
a compile-time constant that permanently suppresses every occurrence after the first. The user's
formal brief demanded: full inspection of `_publish_if_not_suppressed()` and every caller;
identification of the intended semantics (key, state identity, cooldown, event identity); a fix
based on actual event/state identity, never a key-derived value; preservation of all existing
anti-spam behavior; reproduction of the bug BEFORE any code change, using the real production
function; a minimum set of regression tests (A-J); a production-call-path test (not a mocked
helper); duplicate-prevention and monotonic-clock verification; a full regression sweep with
baseline comparison; and a final report distinguishing stages A (person detection) through F
(physical WLED illumination), never claiming a stage without evidence.

## 2. Root cause

`_publish_if_not_suppressed(key, state, publish)`'s dedupe check was:

```python
if self._last_state.get(key) == state:
    return  # no-op re-fire
now = time.monotonic()
if now < self._cooldown_until.get(key, 0.0):
    return  # within cooldown window
```

evaluated unconditionally, with the state-equality check gating BEFORE the cooldown check. This
function is shared by two structurally different kinds of caller:

**The legacy raw-relay path** (`_handle()`'s `self._config.entities` branch) calls it with
`key=entity_id, state=new_state` - a genuinely independent, continuously-varying HA entity value.
Here the equality check correctly distinguishes "nothing actually changed" (a true no-op,
suppressed regardless of cooldown - this is intentional anti-spam design, not a bug) from "a real
transition happened" (proceeds to the cooldown-based rate limit). This path was already correct
and remains completely untouched by this sprint.

**Both classified-`CameraEvent` call sites** (`_handle()`'s `_entity_role_index` branch, sourced
from real HA `device_state_changed` events for a configured `CameraProfile` entity, and
`ingest_external_camera_event()`, the one `VisionCameraEventBridge` calls for every Vision-sourced
`camera_person_entered`/`camera_person_left`/`human_confirmed`/`human_unconfirmed`/
`camera_disconnected`/`camera_reconnected` event) call it with `key=(camera_id, kind), state=kind`.
`state` here is literally part of `key` - for a fixed key, it is a compile-time constant. After the
very first successful publish for a given `(camera_id, kind)` pair, `_last_state[key]` is
permanently set to that exact string. Every subsequent call with the same key has `state` equal to
that same string, so `self._last_state.get(key) == state` is trivially `True` forever - the
`_cooldown_until` check below it, the actual intended time-based anti-spam mechanism, becomes
unreachable dead code for every classified event, for the remaining lifetime of the module
instance.

## 3. Reproduction (before any code change)

Executed directly against the real, unmodified production `CameraAutomationModule.
ingest_external_camera_event()` (not a re-implementation, not a mock):

```
call #1: human_detected(camera=tapo_c212)              => published   (1 event)
wait past the configured cooldown (0.1s in the repro, cooldown_s=0.1)
call #2: human_detected(camera=tapo_c212)               => SUPPRESSED (still 1 event) - BUG
wait past another cooldown
call #3: human_detected(camera=tapo_c212)               => SUPPRESSED (still 1 event) - BUG
```

This exactly matches the brief's own specified "current buggy implementation is expected to
produce" sequence, and matches real production log evidence independently gathered from the same
`logs/runtime/2026-08-23.log` P0.8.7 already used: the raw Vision-level `camera_person_entered`
event fired 14 times across the ~2.5-hour session, but the classified `camera_automation.
camera_event (kind=human_detected, ...)` that `AutomationEngine`'s WLED-triggering rule
(`camera_human_detected_test_action`) actually listens to was published only twice total, both
within the session's first ~6 minutes - zero times for the remaining 2.5+ hours despite dozens
more real detections, and zero corresponding `automation.triggered` lines for the whole rest of the
log. The rule was never even being asked to fire.

**Why the disconnect/reconnect "workaround" appeared to work**: the two real publishes both
happened to bracket a `camera_offline` -> `camera_online` cycle (02:21:16 -> 02:22:03), and the
second `human_detected` publish landed immediately after. This is consistent with a reconnect
cycle coinciding with recreation of the in-memory Vision/camera pipeline state elsewhere in the
system (a fresh `_last_state` dict) - NOT because a reconnect is itself required to reset
suppression. This sprint's own Section G test directly disproves that a reconnect is required: a
single, never-disconnected, never-restarted module instance correctly un-suppresses purely by
cooldown elapsing.

## 4. Intended semantics identified

- **Suppression key**: `(camera_id, kind)` for a classified event, or the raw `entity_id` for the
  legacy relay - two disjoint key spaces (tuple vs. string), sharing one dict, by design (P0.5
  brief's own "one dedupe/cooldown implementation, not two").
- **State identity**: for the legacy relay, a genuinely independent value (`new_state`) that
  legitimately answers "did anything change". For a classified event, there is no such
  independent value - every occurrence of a given `kind` for a given `camera_id` is its own,
  independently meaningful occurrence (a new detection cycle), not a snapshot of a continuously-
  tracked value.
- **Cooldown**: a monotonic-time deadline (`_cooldown_until[key]`), intended as the actual rate
  limiter for both paths.
- **Last-published state**: recorded for observability (`health()`'s `n_tracked`) and, for the
  legacy relay path only, as the genuine "did this truly not change" signal.
- **Event identity**: for classified events, identity IS the `(camera_id, kind)` pair plus time -
  there is no finer-grained "value" to compare.

## 5. Fix

`_publish_if_not_suppressed()` gained one new, additive parameter: `dedupe_identical: bool = True`.

- **Default (`True`)** preserves the legacy relay path's exact prior behavior byte-for-byte - this
  call site was never modified. A truly-identical repeated `new_state` remains suppressed
  indefinitely (the correct, intentional "nothing changed" semantics), and a genuine transition
  arriving before cooldown expires remains rate-limited (also correct, intentional, and already
  tested by the pre-existing, still-pinned `tests/test_p0_camera_automation.py::test_09`/
  `test_10`).
- **`dedupe_identical=False`**, now passed explicitly by both classified-`CameraEvent` call sites
  (`_handle()`'s `_entity_role_index` branch and `ingest_external_camera_event()`): the state-
  equality gate is skipped entirely; suppression is purely `_cooldown_until`-based - a real,
  resettable, monotonic-time deadline. `_last_state[key]` is still recorded (harmless, kept for
  `health()`'s observability count) but is never consulted as a gate for these two call sites.

This is the smallest possible change that closes the bug: one new default-preserving parameter, and
two one-line call-site updates. No new dedupe/cooldown mechanism was invented; no existing call
site's signature or behavior changed except the two that needed it.

## 6. Files changed

- `[MODIFIED] luno/camera_automation/module.py` - `_publish_if_not_suppressed()` gained the
  additive `dedupe_identical` parameter with a full docstring explaining both modes and the exact
  bug this sprint closes; its two classified-path call sites (`_handle()`'s `_entity_role_index`
  branch, `ingest_external_camera_event()`) now pass `dedupe_identical=False`; the legacy relay
  path's own call site (`_handle()`'s `self._config.entities` branch) is untouched.
- `[NEW] tests/test_p0_8_8_camera_event_suppression_fix.py` - 16 tests, sections A-L (see Section
  8 below).
- `[MODIFIED] ARCHITECTURE_GUARD.md` - new §100 entry.
- `[NEW] docs/change_impact/camera_automation_p0_8_8.md` - this document.

Zero changes to `luno/vision.py`, `luno/adapters/vision.py`, `luno/camera_automation/
vision_bridge.py`, `luno/camera_automation/cameras.py`, `luno/automation/engine.py`, `luno/
tool_manager/builtin/real_home_assistant.py`, `luno/adapters/real_home_assistant.py`, `luno/
ha_client.py`, `config/automation_rules.json`, any HA credentials, or any `.pt` model - the
investigation traced each of these explicitly and found none of them responsible, matching the
brief's own scope constraint verbatim. YOLO, RTSP, Tapo, Home Assistant, WLED itself, and HA
verification (P0.8.7's own fix) were never redirected to, per the brief's explicit instruction, and
this write-up does not implicate any of them.

## 7. Preserved anti-spam behavior - explicit checklist (per the brief's own requirements)

- Identical repeated events during cooldown are suppressed: **yes** (Section B, J1) - unchanged
  for both paths.
- Identical events after cooldown are allowed again: **yes, now true for classified events too**
  (Section C, D, J2 - this is the fix). Already true for the legacy relay path when the value
  itself changes.
- A meaningful state transition is allowed immediately when appropriate: preserved exactly as
  before for the legacy relay path (a differing `new_state` still passes the equality gate
  immediately, then is independently subject to the cooldown gate - unchanged, `test_10`/I2 still
  pinned).
- Different cameras/entities/states do not suppress each other: **yes** (Section E, F) - disjoint
  dict keys, unaffected by this change.
- Suppression state does not become permanently locked: **yes, now true** (Section C, D, G - this
  is the fix).
- Module restart naturally resets in-memory suppression state: **yes, unaffected** - `_last_state`/
  `_cooldown_until` are plain in-memory dicts on the instance, always empty on construction; this
  was never broken and remains true, though this fix now also means a restart is no longer the
  ONLY way to reset suppression (Section G proves cooldown elapsing alone suffices).

## 8. New regression test suite - `tests/test_p0_8_8_camera_event_suppression_fix.py` (16 tests)

- **A-D**: the core reproduction and fix proof, run directly against the real, unmodified-call-
  shape `ingest_external_camera_event()` - first publish, suppressed during cooldown, publishes
  again after cooldown (the exact bug fix), and a third cycle proving it is not a one-time unstick.
- **E-F**: different `camera_id`/`kind` keys never interfere with each other.
- **G**: a single, never-disconnected, never-restarted module instance correctly un-suppresses
  purely via cooldown elapsing across 5 repeated detections - directly disproves the "reconnect is
  required" appearance from the production log.
- **H**: the real production call path - `VisionCameraEventBridge._on_person_entered()` (the exact
  method a real `CameraPersonEntered` Event Bus event triggers) -> real `CameraAutomationModule` ->
  real Event Bus -> real `AutomationEngine` (via `register_all_modules()`, `MockHomeAssistantHandler`
  only, never a real HA call) -> a real rule completing three separate times across cooldown-
  separated detections, with a real (mocked) `home_assistant.turn_on` dispatched each time.
  Directly evidences stages A (detection, via the real `CameraPersonEntered` event type)
  through D (HA turn_on command sent) of the brief's own A-F framework, three times over.
- **I1-I3**: the legacy relay path's existing anti-spam behavior re-locked inside this sprint's own
  suite - a truly-identical repeat stays suppressed indefinitely by design (I1, I3 - this is
  correct, NOT a bug, and is a genuinely different contract from the classified-path bug this
  sprint fixes), and a genuine transition within cooldown is still rate-limited (I2).
- **J1-J2**: the HA-sourced classified path (the OTHER call site with the same bug pattern,
  sourced from real HA `device_state_changed` events rather than Vision) - within-cooldown
  suppression re-locked exactly as the pre-existing `tests/test_p0_5_camera_integration.py::
  test_28` already established (J1), PLUS this sprint's own fix proven for this call site too
  (J2 - `test_28` never waited out the cooldown, so it never caught the after-cooldown bug here).
- **K**: no single `ingest_external_camera_event()` call ever produces more than one published
  event.
- **L1-L2**: `_publish_if_not_suppressed()`'s cooldown arithmetic is proven, both structurally (AST
  parse of the real function, confirming `time.monotonic()` is called and `time.time()` is not)
  and behaviorally (monkeypatching `time.time()` to jump 10,000 seconds forward has zero effect on
  suppression, since the real gating call is `time.monotonic()`, untouched by the monkeypatch) to
  be wall-clock-safe.

All 16 pass.

## 9. Regression sweep results

- New suite: 16/16 pass.
- Focused camera_automation/P0.x suite (16 files, including the new suite): 494 passed, 1
  pre-existing skip, 0 failed. Baseline immediately before this sprint's change (same 15 files,
  without the new suite) was 478 passed, 1 skipped - exactly `478 + 16 = 494`, a clean, exact
  match confirming zero regressions in this focused suite.
- Full ~153-file repository sweep (chunked, `pytest -n 4 --timeout=90` per chunk, following this
  project's established methodology): every failure traced to an already-documented pre-existing
  baseline category - LLM `max_tokens`/`max_completion_tokens` provider-compat gap
  (`test_llm_max_completion_tokens_compatibility.py`, `test_memory_session_summary_api_
  compatibility.py`), `MIC_DEVICE_INDEX`/`RealWhisperSource` device-index gap
  (`test_mic_device_index.py`, `test_real_adapters.py`'s two whisper tests),
  `test_production_launcher.py::test_07` (documented environment-specific: this checkout's real
  `.env` has live credentials configured), `test_sprint60_area_schema.py`/`test_sprint63_long_
  term_memory_recovery.py`/`test_sprint64_memory_corruption_forensics.py`/`test_sprint68_
  mutation_audit_hardening.py` (real config-migration/backup-accumulation drift on this live-synced
  folder). Two additional failures - `test_state_isolation.py::test_verified_facts_does_not_leak_
  between_tests_part_b` and `test_verification_dashboard.py::test_api_verification_reports_a_
  successful_verified_action_end_to_end` - occurred only under `-n 4` parallel xdist execution and
  reproduced as a clean pass (22/22 and 6/6 respectively) when re-run in isolation; both are
  already-documented pre-existing parallel-execution-order flake categories in `docs/testing/
  regression_baseline.md` (69 and 21 prior references respectively), not new regressions. Zero
  failures anywhere in the sweep trace back to `luno/camera_automation/module.py` or any file this
  sprint touched.

## 10. Staged evidence report (per the brief's explicit A-F framework)

The brief requires every claim to be backed by actual evidence, staged separately, never
conflating "the event was emitted" with "the physical device changed."

- **A - Person detection**: evidenced. `VisionCameraEventBridge._on_person_entered()` is triggered
  by the real `CameraPersonEntered.EVENT_TYPE` Event Bus event (Section H's test uses the exact
  method a real detection publishes to) - this is Vision's own, unmodified detection signal,
  unrelated to and unaffected by this sprint.
- **B - Camera event published**: evidenced, and this is the layer this sprint's fix directly
  restores. Before the fix, `camera_automation.camera_event` was published once, then silently
  suppressed forever for that `(camera_id, kind)` pair. After the fix, Section C/D/H's tests prove
  it publishes again on every subsequent detection separated by more than the configured cooldown.
- **C - AutomationEngine received event**: evidenced directly, not inferred - Section H subscribes
  to the real `automation.completed` event and asserts `rule_id="p0_8_8_wled_test_rule"` appears
  three separate times, once per detection, each separated by cooldown.
- **D - HA turn_on command sent**: evidenced directly - Section H subscribes to the real
  `tool_requested` event and asserts at least 3 real (mocked) `home_assistant.turn_on` dispatches
  targeting the test light, one per detection cycle.
- **E - HA fresh state verification**: NOT re-tested by this sprint (P0.8.7's own `state_query_
  freshness="fresh"` fix, from the immediately prior sprint, already covers this stage end to end
  with its own dedicated 18-test suite) - `MockHomeAssistantHandler` is used throughout this
  sprint's own end-to-end test (Section H), which is a simpler mock than `RealHomeAssistantHandler`
  and does not exercise P0.8.7's verification-freshness logic; that logic's own regression coverage
  is unchanged and still green (confirmed in Section 9's sweep, `test_p0_8_7_wled_verification_fix.
  py` still passes in full).
- **F - Physical WLED illumination**: **NOT evidenced, and not claimed.** Nothing in this sprint,
  P0.8.7, or any prior sprint gives Luno an optical, electrical, or other independent sensing
  channel for any Home-Assistant-controlled device. This fix restores the PATH by which a real
  detection can reach a real (verified, per P0.8.7) `home_assistant.turn_on` dispatch, repeatedly,
  not just once - it does not and cannot prove the physical strip lights up. If the user still does
  not see the WLED turn on after this fix, with logs now showing `automation.completed` for
  `camera_human_detected_test_action` firing repeatedly across separate detections (not just once),
  the remaining explanation space is entirely downstream of Luno's own code, in the same territory
  P0.8.7's own §99 entry already lays out: Home Assistant's own WLED integration reporting an
  optimistic/stale state, the WLED device's firmware/network dropping the command after
  acknowledging it to HA, or a physical wiring/segment/power issue on the strip itself.

## 11. Result classification

**STRONG** - root cause identified and fixed with a complete, source-evidenced mechanism (a
compile-time-constant dedupe comparison made the intended time-based cooldown unreachable dead
code for every classified camera/Vision event), full backward compatibility (the legacy relay
path's own pre-existing pinned tests remain byte-for-byte green, proven, not assumed), and full
regression coverage (16 new focused tests, including a genuine end-to-end production-call-path
proof through the real bridge/module/engine, plus a clean, baseline-matched full-repository
sweep). This closes the specific "person detected repeatedly but WLED only turns on (at most) once"
symptom the user reported. Physical WLED illumination was never claimed and remains, as disclosed
in P0.8.7, outside this codebase's ability to independently confirm.
