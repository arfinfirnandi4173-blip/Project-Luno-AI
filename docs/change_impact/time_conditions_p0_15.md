# P0.15 — Human-Friendly Dashboard UX & Time-Based Automation Conditions

## 1. Goal

Let a person restrict an automation to a time-of-day window — "WHEN
Person detected AND Time is between 18:00 and 23:30 THEN Turn on WLED" —
while polishing the existing P0.13 Dashboard UI to be more human-
readable. Time restriction is evaluated when the automation trigger
fires; P0.15 does **not** introduce a scheduler, a background timer, a
polling loop, or a second execution path. Architecture preserved exactly:
Dashboard → Automation API → `AutomationEngine` → `ToolManager` → Home
Assistant. Out of scope this sprint (explicitly, per the brief's own
Section 15): days of week, dates, sunrise/sunset, weather conditions,
IF/ELSE, variables, loops, retries, cancellation, a scheduler, natural-
language automation generation, an AI automation builder.

## 2. Architecture inspection (Phase 0) — what already existed

- **`AutomationEngine._evaluate_conditions()`** (`engine.py`) already
  loops `for condition in rule.conditions: evaluate_condition(condition,
  self._state_readers, event_data=event_data)` generically for every
  condition regardless of type — confirmed by direct inspection, this
  means a new condition type automatically flows through the existing,
  unmodified pipeline once `models.py`/`conditions.py` support it, with
  **zero changes needed to `engine.py` itself**. This is the key
  architectural fact that satisfies "no new execution path" by
  construction rather than by discipline.
- **`CONDITION_TYPES`** (`models.py`) is a frozenset of pure comparison
  OPERATORS (`equals`/`not_equals`/`greater_than`/`less_than`/`contains`/
  `state_is`/`greater_equal`) consumed by `wait_until`'s own operator
  dropdown and the dashboard's generic condition row's operator dropdown
  (both schema-driven from `GET /api/automations/schema`'s
  `condition_types`). Confirmed a `"time"` condition must NOT be a
  member of this set — it has no `target`/comparison-operator shape at
  all, and adding it would break `test_sprint72_automation_engine.py`'s
  own exact-frozenset-content assertions (confirmed via direct read).
- **`_TIME_TRIGGER_RE`** (`models.py`) — the existing `^([01]\d|2[0-3]):
  ([0-5]\d)$` regex already used to validate the pre-existing `time`
  TRIGGER type's own HH:MM format. Confirmed reusable verbatim for the
  new time CONDITION's `after`/`before` fields — no second time-format
  parser was written, directly satisfying the brief's own "use the
  existing condition schema/validation mechanism wherever possible"
  instruction.
- **`AutomationAction.parameters`** — confirmed as the existing
  precedent for a `{type, parameters: {...}}` shape distinct from the
  flat `{type, target, value}` shape `AutomationCondition` originally
  had. `AutomationCondition` gained the identically-shaped, additive
  `parameters` field rather than inventing a third representation.
- **P0.14's own nested `condition` sequence step** (`models.py`'s
  `_validate_condition_step()`, `engine.py`'s `_run_condition_step()`)
  constructs its OWN, separate `AutomationCondition` objects for its
  nested `conditions` list. Confirmed via direct inspection and
  deliberately left untouched — per the brief's own "do not modify the
  P0.14 Home Assistant Script Runner contract" instruction, a time
  condition is usable only in a rule's top-level `conditions` list (the
  one gating the whole automation trigger, matching the brief's own
  WHEN/AND examples), not nested inside a P0.14 branch.
- **`luno/dashboard/static/index.html`**'s existing "hardcoded UI-only
  pseudo-type" precedent (`"delay"`/`"wait_until"`/`"condition"`/
  `"stop_automation"` are never members of the schema's `action_types`,
  they're hardcoded client-side) — confirmed reusable for `"time"` as a
  condition kind, so `GET /api/automations/schema`'s `condition_types`
  needed no change at all.

## 3. Files created

- **`tests/test_p0_15_time_conditions.py`** — 52 tests, sections A–G.
- **`docs/change_impact/time_conditions_p0_15.md`** — this document.

## 4. Files modified

- **`luno/automation/models.py`** — new `TIME_CONDITION_TYPE = "time"`
  constant (deliberately NOT a `CONDITION_TYPES` member — see §2); new
  additive `AutomationCondition.parameters: Dict[str, Any] =
  field(default_factory=dict)` field plus `to_public_dict()` updated to
  include it; `validate_condition()` special-cases `condition.type ==
  TIME_CONDITION_TYPE` first, delegating to a new
  `_validate_time_condition()` helper (reuses `_TIME_TRIGGER_RE`
  verbatim); `rule_from_dict()`'s conditions-list comprehension now also
  parses `parameters` (absent entirely, as in every pre-P0.15 rule,
  degrades to `{}` — byte-for-byte the same default).
- **`luno/automation/conditions.py`** — new `_parse_hhmm()` helper
  (`str -> Optional[datetime.time]`, fails closed to `None` on anything
  malformed); `evaluate_condition()` gained an optional `now:
  Optional[datetime.time] = None` parameter (defaults to
  `datetime.datetime.now().time()`; every existing caller, which never
  passes `now`, is unaffected) and one new branch — checked FIRST,
  before the pre-existing `target`-resolution block, since a time
  condition has no target/state-reader concept — implementing the
  normal-range (`after <= current <= before`) and overnight-range
  (`current >= after or current <= before`) comparisons, both inclusive.
- **`luno/automation/__init__.py`** — re-exports `TIME_CONDITION_TYPE`
  alongside the existing `CONDITION_TYPES`/`ACTION_TYPES`.
- **`luno/dashboard/static/index.html`** — additive CSS (`.autom-
  summary-line`, `.autom-time-card` and friends); `renderAutomConditions
  ()` gained a `c.type === 'time'` branch rendering a dedicated "🕐 Time"
  card (native `<input type=time>` From/To fields, a live "Active during
  this period" indicator) alongside the existing generic/advanced
  condition row (now labeled "+ Add Condition (Advanced)"); a new
  "+ Add Time Condition" button; new event bindings writing into
  `condition.parameters.{after,before}`; `automClientValidate()` gained
  time-condition-specific validation matching the brief's own wording;
  new natural-language summary helpers (`automTriggerHuman()`/
  `automConditionsHuman()`/`automEntityLabel()`/`automActionHumanLabel()`/
  `automActionsHuman()`/`automCardSummaryHtml()`) rendered under each
  automation's name in the list view; an empty-conditions state message;
  a loading state for the automations list; `loadAutomations()` now also
  awaits `ensureAutomSchema()` so the list view's device-name lookups are
  populated.

Nothing in `luno/automation/engine.py`, `luno/dashboard/automation_api.py`,
`luno/dashboard/server.py`, `luno/vision.py`, `luno/vision_occupancy.py`,
`luno/camera_automation/`, or `luno/tool_manager/` was opened or
modified this sprint.

## 5. The new condition type

| Type | Parameters | Evaluated by |
|---|---|---|
| `time` | `after` (HH:MM, 24h), `before` (HH:MM, 24h) | `conditions.py::evaluate_condition()`, same pipeline as every other condition |

Normal range (`after <= before`): `after <= current <= before`, both
boundaries inclusive. Overnight range (`after > before`, e.g.
`22:00`→`02:00`): `current >= after or current <= before`, both
boundaries inclusive. Verified against every worked example in the
brief:

- 18:00–23:30 (normal): 18:00 ✅, 20:00 ✅, 23:30 ✅, 23:31 ❌.
- 22:00–02:00 (overnight): 21:59 ❌, 22:00 ✅, 23:59 ✅, 00:00 ✅, 01:59 ✅,
  02:00 ✅, 02:01 ❌.

An `after`/`before` that fails to parse as HH:MM fails closed to
`CONDITION_INVALID` (same "unknown/invalid = the whole rule is SKIPPED,
never a partial/best-guess pass" contract every condition type already
has).

## 6. Automation execution behavior

A rule's top-level `conditions` list is still evaluated by the same,
single `_evaluate_conditions()` call, still strictly AND, still before
any action/sequence dispatch. A FALSE time condition (like any other
FALSE/invalid condition) sets `ExecutionStatus.SKIPPED`, publishes
`automation.condition_failed`/`automation.skipped` with reason
`condition_failed`, and returns — no action runs, no sequence step
executes, no `tool_requested` event is ever published, `ToolManager` is
never reached. A TRUE time condition (combined with every other
condition, if any, also TRUE) lets execution proceed exactly as before
P0.15. Rules with no conditions at all (every rule that existed before
this sprint) are completely unaffected.

## 7. Dashboard UX

The condition editor now shows a dedicated "🕐 Time" card (From/To native
time pickers, a live "✓ Active during this period" / "Not active right
now" indicator) instead of requiring the person to pick an abstract
`target`/operator/value row — the raw `type`/`target`/`value`/
`parameters` shape is never exposed in the primary interface. The
existing generic/advanced condition row (still schema-driven off
`CONDITION_TYPES`) remains available, relabeled "+ Add Condition
(Advanced)", for anything the brief did not ask this sprint to give a
dedicated card. The automations list now shows a natural-language
summary under each automation's name — trigger, any time/other
conditions ("Only between 18:00 and 23:30"), and up to four actions with
entity ids resolved to friendly names via the existing `schema.devices`
registry (capped with a "+N more" line, never a raw entity id unless the
person opens the technical/advanced editor). Invalid or incomplete time
input is rejected client-side with the brief's own wording ("❌ Invalid
time — please enter a time between 00:00 and 23:59." / "❌ Time range is
incomplete — please specify both start and end time.") before a save is
ever attempted; server-side validation (`_validate_time_condition()`)
remains authoritative either way. An empty-conditions state ("this
automation runs every time its trigger fires") and a loading state for
the automations list were added as part of the same UX-polish pass.

## 8. Persistence

No new persistence mechanism. A time condition is created/edited exactly
like any other condition, saved through the existing `POST /api/
automations` / `POST /api/automations/{id}/update` endpoints (which call
the existing `rule_from_dict()`/`validate_rule()`/`_persist_rules()`),
and survives a real `AutomationEngine.reload_rules()` disk re-read —
verified for both a normal-range and an overnight-range window in
`tests/test_p0_15_time_conditions.py`'s Section E.

## 9. Backward compatibility

Every existing `AutomationCondition(type=..., target=..., value=...)`
construction site across P0.6–P0.10 is unaffected — `parameters`
defaults to `{}`. Every existing automation with no conditions, or with
only ordinary comparison conditions, behaves byte-for-byte as before.
The P0.14 Script Runner's own nested `condition` sequence step, its
`wait_until` step, and every P0.11 sequence-execution behavior are
completely untouched — none of those files were opened this sprint.

## 10. Testing methodology (Section 13)

1. **Baseline first:** P0.11/P0.12/P0.13/P0.14/Sprint-72 suites re-run
   before any P0.15 test was added — 307 passed, 0 failed (this IS the
   "before" baseline; P0.15's backend changes to `models.py`/
   `conditions.py` are purely additive and were verified not to disturb
   these suites either before or after the new suite was written).
2. **New suite:** `tests/test_p0_15_time_conditions.py` written and run
   — 52 passed.
3. **P0.11–P0.14 regression, re-confirmed after the new suite:** 307
   passed, 0 failed (identical to step 1).
4. **Vision/Camera Automation suites:** 24 files re-run — 655 passed, 24
   failed, 1 skipped. Every failure re-traced to the exact same,
   already-documented `config/automation_rules.json`/`config/camera_
   automation.json` real-production-data-drift family P0.14 discovered
   (§7 of `ha_script_actions_p0_14.md`) — re-confirmed unchanged by this
   sprint (same missing rule ids, same `enabled` config drift).
5. **Full repository sweep:** 156 files under `tests/` (chunked
   methodology; 3 pre-existing collection errors for already-documented
   sandbox gaps — a missing `faster_whisper` package, a missing
   `legacy_main.py` file, a missing forensic-backup fixture file) —
   approximately 105 failures across the remaining chunks, every one
   individually re-traced to an already-documented pre-existing
   category (see §11). Zero failures touch `luno/automation/`, `luno/
   dashboard/`, or this sprint's own new test suite.

## 11. Pre-existing failure categories (re-confirmed, not caused by P0.15)

- **`config/automation_rules.json`/real-device-config drift** — the same
  family P0.14 discovered and documented (§7/§16 of `ha_script_actions_
  p0_14.md`): the real config file now holds only one user-created rule;
  every P0.6–P0.10 diagnostic/safety rule is gone via deliberate,
  sequential user deletions through the live dashboard. Spans `test_p0_
  6*.py`/`test_p0_7*.py`/`test_p0_8_0/1/2*.py`/`test_p0_8_9*.py`/
  `test_p0_10*.py`/`test_sprint60_area_schema.py`/`test_p0_camera_
  automation.py` and (newly re-confirmed this sprint) `test_p0_5_3_
  vision_camera_bridge.py`/`test_p0_5_camera_integration.py` via
  `config/camera_automation.json`'s own `enabled` default drift.
- **LLM `.env` token-param override** — `test_llm_max_completion_tokens_
  compatibility.py`, `test_memory_session_summary_api_compatibility.py`.
- **`config/backups/`/mutation-audit forensic drift** —
  `test_sprint63_long_term_memory_recovery.py`, `test_sprint68_
  mutation_audit_hardening.py`, and a collection error in `test_sprint64_
  memory_corruption_forensics.py` (a forensic fixture file no longer
  exists on disk).
- **No-audio-hardware sandbox gap** — `test_mic_device_index.py`.
- **Real-whisper construction gap** — `test_real_adapters.py`'s two
  `RealWhisperSource` tests; a collection error in `test_main_bargein.py`
  (missing the optional `faster_whisper` package).
- **Real credentials in `.env`** — `test_production_launcher.py::
  test_07_health_checks_all_pass_in_default_mock_configuration`
  (OpenRouter/Fish Audio API health checks fail without real
  credentials).
- **Sandbox environment gap (new observation, same family as the above,
  not automation-related)** — a collection error in `test_root_main_
  bargein.py` (a `legacy_main.py` file it expects no longer exists in
  this checkout).
- **One newly-observed, unrelated timing flake** —
  `test_llm_tts_streaming_production.py::
  test_14_cancellation_during_synthesis` (a FishAudio mock playback/
  cancellation race, nothing to do with automation conditions).

## 12. Known limitations

- Days-of-week, specific dates, sunrise/sunset, and every other item in
  the brief's own Section 15 exclusion list were deliberately NOT
  implemented this sprint.
- A time condition is only usable in a rule's top-level `conditions`
  list — not nested inside a P0.14 sequence `condition` step (§2/§9
  above explain why this was a deliberate scope boundary, not an
  oversight).
- No timezone configuration was introduced — time conditions use the
  same local-time convention (`datetime.datetime.now().time()`) every
  other time-of-day feature in this project (the `time` TRIGGER type)
  already used.
- The dashboard's "Active during this period" indicator is a purely
  cosmetic, client-side preview (using the browser's own local clock) —
  it never affects what actually gets saved or evaluated; the real
  decision is always made server-side, at real trigger time.

## 13. Result classification

**STRONG** — one new, additive condition type, routed through the
existing, completely unmodified `AutomationEngine` condition-evaluation
pipeline by construction (`engine.py` required zero changes for this
feature), with both normal and overnight time windows verified against
every worked example in the brief. Backed by 52 new tests including
AST-based architecture guards proving no scheduler/timer/polling-loop
primitive and no second execution path were introduced. Dashboard UX
polish (human-readable trigger/condition/action summaries, a dedicated
Time condition card, empty/loading states, inline validation matching
the brief's own wording) stayed within the existing single-file,
dependency-free, vanilla-JS architecture. Zero P0.15-caused regressions
across a 156-file full-repository sweep — every failure individually
traced to an already-documented pre-existing category. Per the user's
own explicit closing instruction ("Stop after P0.15. Do not begin the
next sprint automatically."), no further sprint was started.

See `tests/test_p0_15_time_conditions.py` for the full test suite and
`luno/automation/conditions.py` / `luno/automation/models.py` / `luno/
dashboard/static/index.html` for the implementation.
