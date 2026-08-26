# LUNO P0.8.2 — Camera Human Cleared → Safe Light OFF

## 1. Objective (restated)

Implement and verify the first real production-style camera automation
*pair*: `human_detected` → safely turn ON the configured test light
(P0.8.0/P0.8.1, unchanged), `human_cleared` → safely turn OFF the SAME
configured test light (new this sprint). Additive only — no second
Vision pipeline, Event Bus, HA client, automation engine, cooldown
system, or camera-event bridge was created.

## 2. Baseline

Targeted set (P0/P0.5.x/P0.6.x/P0.7/P0.8.0/P0.8.1/Sprint 72/`test_real_
adapters.py`), measured before any P0.8.2 file was touched: 450 passed,
1 skipped, 0 new failures (the 2 pre-existing `RealWhisperSource`
failures already excluded from this count by construction, per every
prior sprint's own convention). Full chunked sweep baseline (= P0.8.1's
own end state, per `docs/testing/regression_baseline.md`): 4,089
passed.

## 3. Architecture audit

Traced before writing anything, same discipline as every prior P0.x
sprint:

- `luno/automation/camera_action_safety.py::validate_camera_ha_action()`
  (P0.8.0) was already fully direction-agnostic: `CAMERA_HA_ACTION_
  TYPES = frozenset({"home_assistant.turn_on", "home_assistant.turn_
  off"})`, `_ACTION_TO_DESIRED_STATE` maps both directions, and the
  only `kind`-specific refusal (`camera_offline`) already applies
  regardless of action direction. **Conclusion: zero changes to the
  safety gate itself were needed for P0.8.2** — the single biggest
  architectural finding of this sprint.
- `AutomationEngine`'s per-rule cooldown (`_cooldown_until: Dict[str,
  float]`, keyed by `rule.id`) already gives an ON rule and an OFF
  rule, as two separate rule ids, independent cooldown windows by
  construction — no new cooldown mechanism needed.
- **A genuine, previously-latent bug was found and fixed** (not a new
  mechanism — see Section 6) in `AutomationEngine._run_execution()`'s
  `finally` block: cooldown was starting unconditionally on every
  triggered execution attempt, even one whose own conditions failed
  (`SKIPPED`). Since the ON and OFF rules share the same trigger event
  type (`camera_automation.camera_event`), the OFF rule was being
  triggered-and-evaluated (and its cooldown "wasted") every time the
  UNRELATED ON rule's matching event fired, and vice versa. Fixed by
  gating the cooldown-start on `execution.condition_result` being
  truthy — a field that is only ever set `True` after conditions
  genuinely passed.

**Conclusion: no second Vision/YOLO/RTSP pipeline, HA client,
AutomationEngine, Event Bus, cooldown system, or camera-event bridge
was needed or added.**

## 4. Rule design (Section 3 of the brief)

New rule, `camera_test_automation_safety_action_off`, added to `config/
automation_rules.json` as a fully separate rule from the existing ON
rule (not a second action on it — this is what makes Section 6's
cooldown-independence requirement meaningful):

```json
"camera_test_automation_safety_action_off": {
  "name": "Camera human cleared -> TEST light OFF (P0.8.2 safety pipeline, mock-only test entity)",
  "enabled": true,
  "trigger": "event:camera_automation.camera_event",
  "conditions": [
    {"type": "equals", "target": "event.kind", "value": "human_cleared"},
    {"type": "equals", "target": "event.available", "value": true},
    {"type": "equals", "target": "event.detection_error", "value": null}
  ],
  "actions": [
    {"type": "home_assistant.turn_off", "parameters": {"target": "light.test_camera_automation"}}
  ],
  "cooldown_seconds": 30.0
}
```

The existing ON rule (`camera_test_automation_safety_action`) is
byte-for-byte unchanged. Both rules target the same harmless,
non-real `light.test_camera_automation` placeholder by default.

## 5. Safety requirements (Section 4) — verified, not just designed

Because the safety gate is already direction-agnostic (Section 3), the
OFF action is refused for exactly the same conditions the ON action
already was: `camera_offline`, `detection_error`, malformed payload,
disabled camera automation, disabled rule, invalid entity id, HA state
read failure, unsupported HA action, missing event context. **Critical
invariant, directly tested:** a detector failure or `camera_offline`
signal is never interpreted as `human_cleared` and never itself causes
a device action — `test_20`-`test_29` in `tests/test_p0_8_2_human_
cleared_light_off.py` prove this both via a direct `validate_camera_ha_
action()` call and end-to-end through the real Event Bus/
AutomationEngine.

## 6. State-aware OFF behavior (Section 5)

Reuses the existing `AutomationEngine.ha_state_reader` (P0.8.0) as-is —
no second state reader. `human_cleared` while the light is already
reported OFF skips the HA call and reports the existing skip reason
(zero `tool_requested` events, `test_30`/`test_31`); `human_cleared`
while the light is ON results in exactly one `turn_off` call
(`test_32`).

## 7. Cooldown / duplicate protection (Section 6)

Reuses the existing `_cooldown_until` mechanism, keyed by rule id.
`test_40` publishes `human_cleared` three times within the 30s cooldown
window and confirms exactly one `turn_off` call. `test_41` is the test
that ORIGINALLY EXPOSED the cooldown bug described in Section 3 — it
publishes `human_cleared` then `human_detected` and confirms the ON
rule still fires (before the fix, the ON rule's own cooldown was
incorrectly "pre-consumed" by the OFF rule's failed-condition
evaluation on the same trigger event).

## 8. Automation rule semantics (Section 7)

`test_10`-`test_13` prove `human_detected` does not match the OFF
rule, `camera_online`/`camera_offline`/`vision_detection_failed`/
malformed events match neither rule, and a single `human_cleared` does
not trigger the ON rule (`test_50`/`test_51` — the latter also
originally exposed the cooldown bug, publishing `human_detected` then
`human_cleared` and asserting the action order is exactly
`["turn_on", "turn_off"]`).

## 9. Tests

`tests/test_p0_8_2_human_cleared_light_off.py` — 35 tests, all passing,
covering all 20 brief-mandated scenarios plus additional architecture/
credential-safety coverage, across 7 sections (A. fixture/rule-shape
sanity, B. OFF-rule condition matching, C. safety-gate blocking for
turn_off specifically, D. state-aware OFF, E. cooldown/independence, F.
ON-rule independence, G. architecture/mutation guard — no PTZ action,
no unrelated HA action, exactly one `RealHomeAssistantClient(`/
`AutomationEngine(` construction site, no second cooldown
implementation, no second Vision pipeline, no credentials in any
touched file). All HA interaction goes through `MockHomeAssistant
Handler` — `register_real_tool_handlers()` is never called by this
sprint's own tests.

Two supporting updates to prior sprints' own test files, both
documented staleness updates (not weakened safety guarantees):
`tests/test_p0_8_1_live_verification.py::test_11` — updated because the
test-light override now legitimately applies to BOTH the ON rule and
the new OFF rule; `tests/test_p0_8_0_camera_action_safety.py::test_33/
test_34` — `test_33`'s exact-4-rules assertion widened to `.issubset()`
(P0.8.2 legitimately added a 5th rule); `test_34`'s "no human_cleared
rule exists" assertion reframed to its actual underlying invariant (no
rule uses a delay/timer-based action type) — P0.8.0's own brief Section
8 constraint was specifically about DELAYED/timer OFF logic, and the
new OFF rule fires immediately, so this is a legitimate, intentional
addition, not a violation of the original constraint.

## 10. Live verification tool (Section 9)

`luno_live_p0_8_1_verification.py` (the SAME file P0.8.1 introduced —
no second, competing live observer was created) was extended with a
new `--sequence {p0_8_1,p0_8_2}` flag, defaulting to `p0_8_1` so the
original six-test sequence remains byte-for-byte the default behavior:

- `_LiveObserver.on_outcome_event()`/`on_action_event()` gained an
  optional `rule_ids` parameter (defaulting to `{_RULE_ID}` alone, so
  every existing call site is unaffected) and now also record evidence
  into new `outcome_counts_by_rule`/`action_events_by_rule` dicts,
  additive to the existing flat `outcome_counts`/`action_events` the
  p0_8_1 sequence already used.
- `_Snapshot` gained `delta_outcome_for_rule()`/`new_actions_for_rule()`
  for per-rule delta tracking, additive to its existing single-rule
  delta methods.
- A new `_run_p0_8_2_sequence()` function implements TEST A-F (light
  initially ON → human enter → expect ON, no duplicate; remain in view
  → no duplicate ON; leave → expect exactly one OFF; remain outside →
  no duplicate OFF; re-enter → expect ON again, waiting out the ON
  rule's own cooldown via a new read-only `_wait_for_rule_cooldown()`
  helper; re-exit → expect OFF again, same cooldown wait for the OFF
  rule) — same `_Snapshot`-delta discipline, same safe-evidence-only
  printing, same real bootstrap stack as TEST 1-6.
- A new `_print_final_result_p0_8_2()` function prints the mandated
  result block with independent ON-rule/OFF-rule evidence
  (`turn_on_requested`/`turn_on_completed`/`turn_off_requested`/
  `turn_off_completed`) — a separate PRINT function only, reading the
  same single `_LiveObserver` instance, never a second observer.
- TEST 4's own pre-existing comment (P0.8.1's "no OFF rule configured
  - light intentionally remains ON") was annotated, not rewritten, to
  note that a genuine OFF rule now exists but this particular
  (`p0_8_1`) sequence deliberately never subscribes to it — so the
  assertion remains accurate for what that sequence actually observes.

All 23 pre-existing `tests/test_p0_8_1_live_verification.py` tests pass
unchanged, confirming the p0_8_1 default sequence's behavior and
output are unaffected by this extension.

## 11. Live hardware result — REAL HA ACTIONS WERE NOT PERFORMED

Running `luno_live_p0_8_1_verification.py --sequence p0_8_2
--non-interactive` in this sandbox produces the identical structural
hard-stop as every prior "LIVE" sprint:

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

Same two categories of failure as P0.8.1's own sandbox attempt: pure
structural sandbox limitations (no network route to the real camera/HA
host, `ultralytics` not installed) and this checkout's own local
`CAMERA_AUTOMATION_ENABLED=False`. **Result classification for the
agent's own attempt: BLOCKED** — not FAIL, not PASS. No TEST A through
TEST F was executed; no physical light state was observed.

## 12. Regression result

Targeted set: 450 → **485 passed** (+35 new, `tests/test_p0_8_2_human_
cleared_light_off.py`), 1 skipped (unchanged), 0 new failures — the 2
pre-existing `RealWhisperSource` failures reconfirmed, unrelated.

Full repository sweep (chunked, per this project's own established
procedure): **4,052 passed, 37 failed, 1 skipped** across 140
collectible test files (142 total, minus the 2 pre-existing
uncollectible files, `test_main_bargein.py`/`test_root_main_bargein.py`
— "the same 2 pre-existing uncollectible files as every prior sprint"
per `project_handover.json`'s own `test_baseline` field). Every one of
the 37 failures was individually re-confirmed to match an already-
documented, pre-existing, unrelated category, with counts matching
(or, where `config/backups/` continues to accumulate, growing exactly
as expected) the categories `project_handover.md` §22 and `docs/
testing/regression_baseline.md` have flagged since well before this
sprint: the `.env`/`MAX_TOKENS_PARAM` mismatch (7 in `test_llm_max_
completion_tokens_compatibility.py` + 5 in `test_memory_session_
summary_api_compatibility.py`), missing `list_microphones.py` (6) +
the `RealWhisperSource` test-construction bug (2), the blocked
OpenRouter/Fish Audio network health check (1), and the long-term-
memory-corruption-forensics / `config/backups/` file-count drift
family (`test_sprint63_long_term_memory_recovery.py` 9 + `test_
sprint64_memory_corruption_forensics.py` 5 + `test_sprint68_mutation_
audit_hardening.py` 2 = 16, `config/backups/` now at 51 files). **Zero
new failures were found anywhere in the repository.** This sprint's own
production code change is confined to a single file (`luno_live_p0_8_
1_verification.py`, a standalone verification script) plus the
targeted `config/automation_rules.json`/`luno/bootstrap/adapters.py`/
`luno/automation/engine.py` changes described in Sections 3-4 above —
none of the 37 failing tests import or exercise any of those files.

## 13. Files changed — diff safety (Section 13)

- `[MODIFIED]` `config/automation_rules.json` — one new rule
  (`camera_test_automation_safety_action_off`). The 4 prior rules are
  byte-for-byte unchanged.
- `[MODIFIED]` `luno/bootstrap/adapters.py` —
  `apply_camera_automation_test_light_override()` generalized from a
  single hardcoded rule id to a frozenset of rule ids
  (`_LIVE_TEST_RULE_IDS`), looping to apply the same resolved entity to
  every TEST-ONLY rule's HA action. Still never persists to disk, still
  never touches any other rule or any other field of these rules.
- `[MODIFIED]` `luno/automation/engine.py` — one-line fix (gating the
  cooldown-start on `execution.condition_result`) in `_run_execution()`'s
  `finally` block. This is the only change to this file this sprint;
  verified safe via a 409-test combined regression run (Sprint 72 +
  every P0.x camera/automation test file) before proceeding, then
  reconfirmed via this sprint's own full repository sweep (Section 12).
- `[MODIFIED]` `tests/test_p0_8_1_live_verification.py` — 1 test
  updated (`test_11`, renamed and extended for the both-rules override
  scoping).
- `[MODIFIED]` `tests/test_p0_8_0_camera_action_safety.py` — 2 tests
  updated (`test_33`/`test_34`, both documented staleness updates —
  Section 9 above).
- `[NEW]` `tests/test_p0_8_2_human_cleared_light_off.py` — 35 tests.
- `[MODIFIED]` `luno_live_p0_8_1_verification.py` — extended in place
  with the P0.8.2 TEST A-F sequence (Section 10 above). No second live
  observer file was created.
- `[NEW]` `docs/change_impact/camera_automation_p0_8_2.md` — this file.

**Not touched:** `luno/camera_automation/*.py`, `luno/automation/
camera_action_safety.py`, `luno/automation/models.py`, `luno/
automation/conditions.py`, `luno/adapters/vision.py`, `luno/adapters/
real_vision.py`, `luno/adapters/home_assistant.py`, `luno/adapters/
real_home_assistant.py`, `luno/tool_manager/builtin/*.py`, `luno/
ha_client.py`, `luno/bootstrap/modules.py`, `main.py`. No PTZ action
type was added, modified, or invoked. No direct HA call was added
outside the existing `AutomationEngine` dispatch chain. No credential
value was written to any file this sprint (confirmed via targeted
grep across `luno_live_p0_8_1_verification.py` — only `bool(...)`/
configured-status flags are ever printed, same as P0.8.1).

## 14. Known limitations

- Live hardware verification (TEST A-F, a real light actually observed
  turning on and off in response to a real person entering/leaving
  frame) remains BLOCKED for the agent — see Section 11. A human with
  real camera/HA hardware must run `luno_live_p0_8_1_verification.py
  --sequence p0_8_2` themselves.
- TEST E/F's cooldown-wait logic (`_wait_for_rule_cooldown()`) is
  best-effort and read-only against `AutomationEngine._cooldown_until`
  — if that internal attribute is ever renamed, the wait silently
  becomes a no-op (harmless: the test itself would then correctly FAIL
  if a genuine cooldown-suppression occurred, rather than reporting a
  false PASS).
- This sandbox's own current `.env`/environment still has `CAMERA_
  AUTOMATION_ENABLED` resolving to `False` and `ultralytics` not
  installed — both already flagged in P0.8.1's own documentation as
  things to check on the real machine, unchanged this sprint.
- No before/after performance numbers were measured for the cooldown
  fix — it is a single boolean condition added to an existing `if`; no
  real camera/HA load exists in this sandbox to measure against.

## 15. Recommended next sprint

There is no code left to write for P0.8.2 itself — it is feature
complete, fully tested (mocked), and its own live procedure is fully
documented and ready to run. The concrete next step is for the user to
run it on their real machine:

```
CAMERA_AUTOMATION_TEST_LIGHT_ENTITY=light.<a_real_harmless_test_light> \
CAMERA_AUTOMATION_ENABLED=true \
python luno_live_p0_8_1_verification.py --sequence p0_8_2
```

and walk through TEST A-F. If every test passes and the `--- LIVE
P0.8.2 RESULT ---` block reports `Overall: PASS`, this closes the
"first real production-style camera automation behavior" milestone
this whole P0.8.x line has been building toward. A future sprint
("P0.8.3" or similar) could then consider pointing one (or both) of
these rules at a real, non-test production light — only after the
P0.8.2 live procedure has been run successfully at least once against
the harmless test light, per this whole line's own established,
incremental safety discipline.
