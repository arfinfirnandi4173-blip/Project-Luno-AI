# LUNO P0.8.1 — Live Camera → Home Assistant Light Verification

## 1. Objective (restated)

Prove, on the user's real machine, that the full chain REAL CAMERA →
REAL VISION → REAL CAMERA EVENT → REAL AUTOMATION ENGINE → REAL SAFETY
GATE → REAL HOME ASSISTANT → ONE TEST LIGHT actually works end to end -
the first sprint in this line permitted to perform a real Home
Assistant action, but ONLY against one explicitly configured test
light, and ONLY after a hard pre-flight gate. **No real Home Assistant
action was performed by the agent this sprint** - see Section 10.

## 2. Baseline

Targeted set (P0/P0.5.x/P0.6.x/P0.7/P0.8.0/Sprint 72/`test_real_
adapters.py`), measured before any P0.8.1 file was touched: 427
passed, 1 skipped, 0 failed (the 2 pre-existing `RealWhisperSource`
failures already excluded from this count by construction - see
Section 9). Full chunked sweep baseline (= P0.8.0's own end state):
4,066 passed.

## 3. Architecture audit (Section 1 of the brief)

Traced before writing anything, same discipline every prior P0.x
sprint in this line has followed:

- `AutomationEngine._dispatch_home_assistant_action()` (P0.8.0) is
  still the ONE place any camera-triggered `home_assistant.turn_on`/
  `turn_off` reaches the existing HA dispatcher - P0.8.1 adds no new
  dispatch path.
- `AutomationEngine.start()` calls `self.reload_rules()` - this is
  the ONE place `AutomationEngine._rules` (an in-memory dict) is
  actually populated from `config/automation_rules.json`. This
  matters directly for Section 4 below: any code that wants to modify
  an in-memory rule (this sprint's own test-light override) MUST run
  AFTER `runtime.start()`, never before - a real bug this sprint's own
  test suite (Section 8) caught and fixed before it ever reached
  `main.py` (see Section 12/"Errors and fixes" for the story).
- `luno.ha_client.HomeAssistantClient.connect()` already performs a
  full WebSocket-connect-then-auth handshake and returns a boolean -
  reused directly (via a throwaway, disposable instance - never the
  real running runtime's own connection) for the new "Home Assistant
  authentication succeeds" pre-flight check, rather than building a
  second, parallel auth mechanism.
- `RealHomeAssistantSource`/`RealHomeAssistantClient` (`luno/adapters/
  real_home_assistant.py`) remain the ONE real Home Assistant client
  implementation in this codebase - confirmed by construction-site
  grep (Section 12 below), unchanged this sprint.
- `AutomationAction.parameters` is a plain, mutable `dict` field on an
  otherwise `frozen` dataclass (`luno/automation/models.py`) - frozen
  only blocks reassigning the dataclass's own fields, not mutating a
  dict already held by one of those fields. This is the mechanism the
  new test-light override (Section 4) uses to redirect ONE rule's ONE
  action's target in memory, without needing to construct a new
  `AutomationRule`/`AutomationAction` or touch `AutomationEngine`'s
  own rule-loading code at all.

**Conclusion: no second Vision/YOLO/RTSP pipeline, HA client,
AutomationEngine, Event Bus, or camera polling loop was needed or
added.** This sprint is a pre-flight/verification script plus one
small, narrowly-scoped, opt-in bootstrap function.

## 4. Safety gate implementation - what's NEW this sprint

P0.8.1 does not modify `luno/automation/camera_action_safety.py` at
all (P0.8.0's safety gate is reused byte-for-byte). What's new:

- **`luno/bootstrap/adapters.py::apply_camera_automation_test_light_
  override(modules)`** - resolves `CAMERA_AUTOMATION_TEST_LIGHT_
  ENTITY` (brief Section 2: explicit config only, never guessed) and,
  ONLY when it is set, overrides the existing P0.8.0 TEST-ONLY rule's
  (`camera_test_automation_safety_action`) `home_assistant.turn_on`
  action's `target` parameter, in memory, for this process only.
  No-ops entirely (changes nothing) when the env var is unset - the
  default for every sandbox test and every prior sprint's own
  regression run. Never writes `config/automation_rules.json` on disk
  (no `_persist_rules()` call). Never touches any other rule or any
  other field of this one rule (id/name/enabled/trigger/conditions/
  cooldown all untouched).
- **`main.py`** - one new call, `apply_camera_automation_test_light_
  override(modules)`, placed AFTER `runtime.start()` (see the "Errors
  and fixes" note in Section 12 for why that ordering is load-bearing,
  not cosmetic).
- **`luno_live_p0_8_1_verification.py`** (new, root-level, standalone -
  same category of artifact as `ha_camera_discovery.py`/`tapo_camera_
  event_audit.py`/`luno_live_camera_event_observer.py`) - the
  interactive, six-test live verification script for the user to run
  on their real machine. Boots the real, existing bootstrap
  (`LauncherConfig.load()` → `register_all_modules()` →
  `register_all_adapters()`), applies the override, subscribes to the
  EXISTING event set (`camera_automation.camera_event`, the four raw
  Vision events, `automation.*` outcome/action events filtered to the
  one test rule, `tool_requested`, `system_error`), and prints the
  mandated Section 6 result block.

## 5. Pre-flight (Section 1) - implementation and actual sandbox result

All 13 of the brief's own Section 1 checks are implemented and treated
as CRITICAL (any FAIL is a hard stop - `_CRITICAL_PREFLIGHT_CHECKS` in
`luno_live_p0_8_1_verification.py`, unit-tested in `tests/test_p0_8_1_
live_verification.py::test_03`). **Actual result of running this
script in the agent's own sandbox** (captured verbatim, not
paraphrased):

```
Pre-flight:
    TAPO_HOST configured: PASS  (configured)
    TAPO_USERNAME configured: PASS  (configured)
    TAPO_PASSWORD configured: PASS  (configured)
    Camera reachable (TCP 443): FAIL  (OSError: [Errno 101] Network is unreachable)
    RTSP reachable (TCP 554): FAIL  (OSError: [Errno 101] Network is unreachable)
    Home Assistant reachable: FAIL  (lha.czdelta.biz.id:443 - gaierror: [Errno -3] Temporary failure in name resolution)
    Home Assistant authentication succeeds: FAIL  (skipped - HA not reachable)
    Vision backend = real: PASS  (real)
    ultralytics (YOLO) importable: FAIL  (ModuleNotFoundError: No module named 'ultralytics')
    cv2 (OpenCV) importable: PASS  (importable (5.0.0))
    CAMERA_VISION_ENABLED: PASS  (True)
    Camera automation enabled: FAIL  (False)
    P0.8 safety gate enabled: PASS  (camera_action_safety module + engine wiring present)

HARD STOP: one or more CRITICAL pre-flight checks failed (see above).
Not starting the runtime. No device action was attempted.
```

Two DIFFERENT kinds of failure are visible here, worth distinguishing
honestly:

1. **Structural sandbox limitations** (same as every prior "LIVE"
   sprint in this project's history) - no network route to the real
   Tapo C212 or the real Home Assistant host, and `ultralytics` is not
   installed in this sandbox. These are properties of WHERE this
   script ran, not of the code.
2. **A genuinely local, fixable configuration gap** - `Camera
   automation enabled: FAIL (False)` - this checkout's own current
   `.env`/environment simply has `CAMERA_AUTOMATION_ENABLED` unset or
   `false`. This is NOT a network limitation; on the user's real
   machine this is a one-line `.env` fix (`CAMERA_AUTOMATION_ENABLED=
   true`), already the documented, pre-existing way to opt in (P0
   sprint). Flagged explicitly here rather than lumped in with the
   network-based blockers, so the user knows to check it.

The runtime was never started, and no device action was ever
attempted - the pre-flight hard-stopped exactly as designed.

## 6. Test light (Section 2)

`CAMERA_AUTOMATION_TEST_LIGHT_ENTITY` was not set in this sandbox
either, so this sprint's own pre-flight hard-stopped before test-light
resolution was even reached (`_resolve_test_light()` is unit-tested
independently - Section 8 below). No entity id was guessed or assumed
anywhere.

## 7. State/cooldown behavior (Section 4/5)

Unchanged from P0.8.0 - the existing `_cooldown_until` mechanism and
the existing `AutomationEngine.ha_state_reader` (P0.8.0's own
state-aware skip) are both reused as-is. `tests/test_p0_8_1_live_
verification.py::test_40` proves, through a real bootstrap with a
mocked HA backend, that the override does not disturb either
mechanism - the overridden rule still fires, still respects cooldown,
and (when a state reader reports "already on") still skips the
redundant call, exactly as P0.8.0 already established.

## 8. Tests

`tests/test_p0_8_1_live_verification.py` - 23 tests, all passing:

- **A. Static/structural safety** (4) - the live script never prints a
  credential value, never calls a Home Assistant service directly,
  every Section 1 check is in the critical set, the script parses
  cleanly.
- **B. `apply_camera_automation_test_light_override()` scoping** (5) -
  true no-op when unset; applies ONLY to the one rule's target when
  set (every other rule's targets byte-identical before/after); never
  writes `config/automation_rules.json` on disk; returns `None`
  safely when the engine/rule is missing; strips whitespace and
  ignores a blank value.
- **C. `_resolve_test_light()`/pre-flight gating** (6) - returns `None`
  when unset, returns the configured value, never hardcodes a fallback
  entity id, `_print_preflight()` hard-stops on any critical failure,
  the HA-reachable/HA-auth checks never raise and fail closed against
  an unreachable host.
- **D. `_Snapshot` delta helper** (3) - pure logic, proves each test's
  own "since this test started" counting is correct.
- **E. Real-bootstrap end-to-end proof** (2) - the strongest evidence
  available without real hardware: publishing a real `camera_
  automation.camera_event` through the real Event Bus/AutomationEngine/
  safety gate, with the override applied, results in a mocked Home
  Assistant call targeting the OVERRIDDEN entity, never the shipped
  placeholder (`test_40`); without the override, the shipped
  placeholder is targeted, never a guessed real entity (`test_41`).
- **F. Regression safety** (2) - the P0.8.0 rule's shape on disk is
  unchanged; every HA-touching test in this file uses `MockHome
  AssistantHandler`, proven behaviorally (`isinstance` check), not by
  a fragile self-referential source scan.

## 9. Regression result

Targeted set: 427 → 450 passed (+23 new), 1 skipped (unchanged), 0
new failures - the 2 pre-existing `RealWhisperSource` failures
reconfirmed, unrelated.

Full chunked sweep (10 file-group chunks + the 2 isolated groups this
project's own convention requires): **4,066 → 4,089 passed** (+23
new). Isolated groups unchanged (`test_vision_sprint8.py` 32/32,
sprint69 pair 37/37). Zero pre-existing tests needed updating this
sprint (unlike P0.7/P0.8.0, P0.8.1 added no rule count changes, no
event schema changes, and no signature changes to any function another
test calls directly).

**One failure observed, investigated, and confirmed FLAKY, not a
regression:** `tests/test_streaming_e2e.py::test_D_barge_in_between_
llm_and_tts_chunk_never_plays` failed once during the combined chunk
run, then passed cleanly when re-run in isolation immediately after -
exactly the documented "re-run any TTS/streaming/voice-pipeline-timing
failure in isolation before classifying it" procedure `project_
handover.md` §21 already establishes (already observed in "4 of the
last 5 sprints" per that same section, before this sprint ever
started). This file is untouched by P0.8.1.

**Two pre-existing, already-baselined collection errors reconfirmed,
unrelated:** `tests/test_main_bargein.py` (imports `luno/main.py` →
`ModuleNotFoundError: No module named 'faster_whisper'`) and `tests/
test_root_main_bargein.py` (imports a `legacy_main.py` that does not
exist in this checkout). These are the exact "2 pre-existing
uncollectible files" `docs/project_handover.json`'s own `test_baseline`
field has referenced since the very first P0 sprint ("same 2
pre-existing uncollectible files as every prior sprint") - not new,
not touched by this sprint.

**Pre-existing failures reconfirmed, unrelated (same set as every
prior sprint in this line):** the `.env`/`MAX_TOKENS_PARAM` mismatch
(12), missing `list_microphones.py` (6), the `RealWhisperSource`
test-construction bug (2), the blocked OpenRouter/Fish Audio network
health check (1), and `config/backups/` file-count drift (16).

## 10. Live hardware status - REAL HA ACTIONS WERE NOT PERFORMED

**Explicit statement:** no real Home Assistant `turn_on`/`turn_off`
call was made by the agent at any point during this sprint, and no
real light was observed turning on or off. `luno_live_p0_8_1_
verification.py`'s own pre-flight hard-stopped (Section 5) before the
runtime was ever started - per the brief's own Section 1 instruction
("If any critical requirement fails: HARD STOP. Do not attempt a real
device action"), which this script honored exactly. **Result
classification for the agent's own attempt: BLOCKED** - not FAIL, not
PASS. No TEST 1 through TEST 6 in Section 4 of the brief was executed;
no physical light state was observed; nothing in this document should
be read as claiming otherwise.

## 11. Files changed - diff safety (Section 12/9)

- `[NEW]` `luno_live_p0_8_1_verification.py` - the live verification
  script itself.
- `[NEW]` `tests/test_p0_8_1_live_verification.py` - 23 tests.
- `[NEW]` `docs/change_impact/camera_automation_p0_8_1.md` - this file.
- `[MODIFIED]` `luno/bootstrap/adapters.py` - one new function,
  `apply_camera_automation_test_light_override()`. Necessary: the
  brief's own Section 2/3 requires redirecting the existing TEST-ONLY
  rule at a real, explicitly configured entity without touching
  `config/automation_rules.json` on disk or any other rule.
- `[MODIFIED]` `main.py` - one new import, one new call (placed AFTER
  `runtime.start()`). Necessary: this is the one place every other
  post-hoc bootstrap-wiring function in this project is already
  called from.

**Not touched:** `luno/camera_automation/*.py`, `luno/automation/
camera_action_safety.py`, `luno/automation/engine.py`, `luno/
automation/models.py`, `luno/automation/conditions.py`, `luno/
adapters/vision.py`, `luno/adapters/real_vision.py`, `luno/adapters/
home_assistant.py`, `luno/adapters/real_home_assistant.py`, `luno/
tool_manager/builtin/*.py`, `luno/ha_client.py`, `luno/bootstrap/
modules.py`, `config/automation_rules.json` (on disk - only ever
mutated in memory), `config/camera_automation.json`, `.env`. No
lights/switches/PTZ/locks action type was added, modified, or invoked
by anything in this sprint. No second rule was added (Section 3: "use
the existing P0.8.0 TEST-ONLY rule").

**Errors and fixes found and corrected during this sprint's own
development (self-caught by its own test suite, never shipped):** the
first draft of `apply_camera_automation_test_light_override()`'s call
site (in both `main.py` and the live script) was placed BEFORE
`runtime.start()`. Since `AutomationEngine._rules` is only populated
inside `AutomationEngine.start()` (called transitively by `runtime.
start()`), calling the override any earlier always found an empty rule
table and silently no-op'd - which would have made the ENTIRE feature
inert in real use while still reporting a clean pre-flight/tests. This
was caught by `tests/test_p0_8_1_live_verification.py::test_10/test_
11/test_14/test_40` failing with an explicit "rule ... is not loaded"
bootstrap log line during development, traced to the ordering bug, and
fixed by moving both call sites to run immediately after `runtime.
start()` (with an explanatory comment left at each site). No version
of this bug was ever run against real hardware or committed as final.

## 12. Known limitations

- Live hardware verification (TEST 1-6, a real light actually
  observed turning on) remains BLOCKED for the agent - see Section 10.
  This is the sprint's own defining constraint, not a gap: a human
  with real camera/HA hardware must run `luno_live_p0_8_1_verification.
  py` themselves.
- This sandbox's own current `.env`/environment has `CAMERA_
  AUTOMATION_ENABLED` resolving to `False` and `ultralytics` not
  installed - both flagged explicitly in Section 5 as things the user
  should check on their own machine before running the live script,
  separate from the pure network-reachability blockers.
- The six-test sequence in `luno_live_p0_8_1_verification.py` uses
  `input()` prompts to pace a human tester through physical actions
  (walking in/out of frame, manually toggling the light) - this is
  appropriate for an interactive terminal session but means the script
  cannot be fully unattended; a `--non-interactive` flag exists for a
  scripted/fixed-delay fallback, but the brief's own tests fundamentally
  require a human to perform the physical actions regardless.
- No before/after performance numbers were measured - the new override
  function is a handful of dict/list operations; no real camera/HA
  load exists in this sandbox to measure against.

## 13. Recommended P0.8.2 (or a repeat of this same P0.8.1 procedure)

There is no code left to write for P0.8.1 itself - it is feature
complete, fully tested (mocked), and its own live procedure is fully
documented and ready to run. The concrete next step is for the user to
run it on their real machine:

```
CAMERA_AUTOMATION_TEST_LIGHT_ENTITY=light.<a_real_harmless_test_light> \
CAMERA_AUTOMATION_ENABLED=true \
python luno_live_p0_8_1_verification.py
```

and walk through the six prompted tests. If every test passes and the
`--- LIVE P0.8.1 RESULT ---` block reports `Overall: PASS`, the
concrete next step for a future sprint (tentatively "P0.8.2") would be
deciding what, if anything, should happen when `human_cleared` fires
(currently intentionally a no-op - Section 4/TEST 4's own explicit
constraint) - e.g. a delayed, state-aware OFF rule - which the P0.8.0
change-impact doc already flagged as deliberately out of scope until
an existing delayed/state-aware mechanism can be reused without an
architectural change.
