# Sprint 52 — Robust Home Assistant Command & Entity Resolution

**Status: IMPLEMENTED, SYNTAX VERIFIED, UNIT TESTED, INTEGRATION TESTED (real pytest run, see below). NOT LIVE VERIFIED (no real Home Assistant server reachable from this session) and NOT FULL-REPOSITORY REGRESSION VERIFIED (only the files on this feature's real dependency path were staged/executed — see "What was and wasn't executed" below).**

Note on numbering: this is the *user's* "Sprint 52" (Robust HA Command &
Entity Resolution). `ARCHITECTURE_GUARD.md` already has a `## 52` entry
from a prior takeover session's Dashboard Turn-State Recovery fix
(Part 2 / TTS-path), a documentation-only coincidence. This work is
filed as `## 53` in that document, with its own heading naming the
user's "Sprint 52" explicitly, to keep the doc's own append-only
section numbering unambiguous.

## Problem

Vinn reported that Home Assistant commands only work with exact device
names — a small typo, missing/transposed character, or spacing
difference ("matikan rg strip" / "rbg strip" / "rgb strp" / "rgbstrip"
instead of "RGB Strip") causes the command to fail outright, even
though the intended device is obvious to a human.

## Root cause (confirmed by reading the actual current pipeline, not assumed)

`IntentParser.parse()` (`luno/planner/parser.py`) does raw-text-after-verb
extraction and slugification only — no entity awareness at all.
`RealHomeAssistantHandler._resolve_entity_id()`
(`luno/tool_manager/builtin/real_home_assistant.py`) — the actual,
authoritative resolution step in the production pipeline — only ever
does an **exact, normalized** name/alias lookup against
`luno.devices.LIGHTS/SWITCHES/SCRIPTS`. There is no fuzzy matching
anywhere in the execution path. A `difflib`-based fuzzy matcher
(`_suggest_similar_devices()`) already existed, but it was wired up
*only* to phrase a "did you mean X?" question on failure — it never
influenced execution, so even a near-perfect typo always failed and
asked the user to repeat themselves.

A second, narrower bug was found while building the alias tier
consistently: `_lookup_script()` never checked `cfg["aliases"]` at all
(unlike `_lookup_light()`, which always has) — `config/scripts.config.json`'s
own `"gaming mode": {"aliases": ["mode gaming"]}` entry was silently
unreachable by its alias. `_all_known_device_names()` (feeds the
pre-existing suggestion feature) had the identical gap. Both fixed as
part of this sprint (a 3-line pattern copied from `_lookup_light()`,
not a new capability).

## The fix

`RealHomeAssistantHandler._resolve_entity_tiered()` (new) wraps
`_resolve_entity_id()` — **untouched, called as-is, the single source
of truth for tiers 1–3** — and, only when it finds nothing, adds:

- **Tier 4 (fuzzy):** `_score_candidates()` computes a `difflib.
  SequenceMatcher` similarity score for every **distinct** known
  entity_id (its best-matching name/alias wins — two aliases of the
  same device can never look like two competing candidates). Auto-resolves
  **only** when exactly one distinct device clears both
  `fuzzy_min_confidence` (default 0.78) and `fuzzy_min_margin` (default
  0.15, i.e. beats the next-best distinct device by at least that
  much). Both are env-configurable (`FUZZY_ENTITY_MIN_CONFIDENCE`,
  `FUZZY_ENTITY_MIN_MARGIN`), reloaded fresh every call — same
  convention as the existing `VERIFY_DEVICE_STATE` etc.
- **Tier 5 (ambiguous/unknown):** two or more distinct devices "in
  contention" (within the margin of the top score) → refused
  (`executable=False`, `resolution_method="ambiguous"`), no guessing,
  ever. Nothing clears the confidence bar → `"unknown"`, same as
  before. **Both fall through to the pre-existing, unmodified
  `_unknown_device_result()`** — which already asks "which one did you
  mean?" when its own `difflib` suggestion returns 2+ matches — so no
  new user-facing message code was written for these paths.

`execute()`'s only change: the two lines that called
`self._resolve_entity_id(target)` directly now call
`self._resolve_entity_tiered(target)` and read `.executable`/
`.resolved_entity`. For every target that already resolved via tier
1–3 (the overwhelming majority of real traffic, and every pre-existing
test), the result is byte-identical to before — fuzzy scoring code
doesn't even run. Every other branch of `execute()` (on/off verify
loop, run_script, set_temperature, set_color, set_brightness,
`_unknown_device_result()`'s own messages) is unmodified.

### Structured resolution result

`EntityResolutionResult` (new, frozen dataclass): `raw_target`,
`normalized_target`, `resolved_entity`, `resolution_method` (`"exact"`
/ `"alias"` / `"entity_id_literal"` / `"fuzzy"` / `"ambiguous"` /
`"unknown"`), `confidence` (0.0–1.0), `candidate_count`, `ambiguity`
(bool), `executable` (bool) — matches the sprint brief's own schema.

### No forbidden techniques

`_score_candidates()` is traceable to `difflib.SequenceMatcher` alone
— stdlib only, in-process, over a ~6-device registry (this checkout's
actual `config/*.config.json`). No embeddings, no vector DB, no LLM
judge, no second ranking system, no network/HTTP call, no global
mutable device-state cache beyond the pre-existing `luno.devices`
registries. `test_score_candidates_uses_only_stdlib_difflib` asserts
this by source inspection.

### Ambiguity example (real devices)

The real registry has no two devices similar enough to naturally tie
from a typo — this checkout's 6 devices are all clearly distinct by
`difflib` distance (see "Threshold selection" below). A dedicated
direct-unit test (`test_T_ambiguity_gate_direct_unit_test`) exercises
the exact contention/margin logic with two of the project's real
device labels (`"RGB Strip"` / `"RGB Computer"`) engineered to score
within margin of each other, proving the safety gate refuses rather
than guesses when that *does* happen. A second test
(`test_observability_ambiguous_case_emits_resolution_event`) exercises
the same gate through the real `execute()` path end-to-end, with the
margin env var deliberately widened to force the branch (documented in
that test's own comment — an honest substitute for a real ambiguous
pair, which doesn't exist in this checkout's own registry today).

### Threshold selection (worked numbers, from a standalone `difflib` prototype against this checkout's real device names)

| Query | Best match | Score | Outcome |
|---|---|---|---|
| `rg strip` | RGB Strip | 0.94 | resolves |
| `rbg strip` | RGB Strip | 0.89 | resolves |
| `rgb strp` | RGB Strip | 0.94 | resolves |
| `rgbstrip` | RGB Strip | 0.94 | resolves |
| `batrai` | Baterai | 0.92 | resolves |
| `bateray` | Baterai | 0.86 | resolves |
| `aquascap`/`aqascape` | Aquascape | 0.94 | resolves |
| `gamin mode` | gaming mode | 0.95 | resolves |
| `rgb comp` | RGB Computer | 0.80 (margin 0.21 over RGB Strip) | resolves |
| `rgb` alone | RGB Strip | 0.50 | refuses (too little info) |
| `lamp` alone | Main Lamp | 0.62 | refuses (existing suggestion path still offers it) |
| `xyz nonsense device` | — | 0.27 | refuses |
| `desk_light` (pre-existing test, `office desk light` registry) | office desk light | **0.74** | refuses — stays on the pre-existing "did you mean" path, unchanged |
| `kitchen` (pre-existing test, 3-light registry) | kitchen_light/kitchen_strip | **0.70** | refuses — stays on the pre-existing "which one" path, unchanged |

The last two rows are not hypothetical — they are the *actual* targets
used by `test_similar_entity_single_suggestion` and
`test_multiple_similar_entities` in the pre-existing
`test_real_home_assistant_verification.py`. Their scores (0.74, 0.70)
were computed and checked against the chosen `fuzzy_min_confidence`
(0.78) **before** finalizing it, specifically so this sprint's new tier
does not intercept those two tests and change their pre-existing
behavior. Both tests were then re-run for real (see below) and still
pass.

## Observability (extends Sprint 50, doesn't duplicate)

`RealHomeAssistantHandler` already had an optional
`on_verification_event(stage, payload)` hook (Verified Smart Home
Execution sprint), wired to the real Event Bus in exactly one place
(`luno/bootstrap/adapters.py::_make_verification_event_publisher`).
This sprint reuses that same hook with one new stage, `"resolution"`
(`_emit_resolution()`), fired **only** for `"fuzzy"`/`"ambiguous"`
outcomes — the two this sprint actually introduces. Exact/alias/literal
matches and fully-unknown targets stay silent on purpose, matching the
module's existing "no event for nothing new to report" convention, and
— concretely — keeping the pre-existing
`test_events_unknown_device_emits_nothing` passing unchanged (this was
an actual regression found during design, not a hypothetical — see
"What was checked and fixed before it broke anything" below).

`luno/adapters/events.py` gets one new `Event` subclass,
`EntityResolutionDecision`, added to `ADAPTER_EVENT_TYPES` — same
pattern as `ActionVerificationStarted` et al. `luno/bootstrap/adapters.py`'s
`_VERIFICATION_STAGE_TO_EVENT_NAME` gets one new entry
(`"resolution": "EntityResolutionDecision"`). No new hook, no new
Event Bus wiring path, no new dashboard collector (the existing generic
Event Bus page already shows any new event type with zero dashboard
code change, same as Sprint 50 found for its own 5 new events).

## Real-world capture/replay (Sprint 50 infrastructure)

Not extended with new code in this sprint — `luno/test_capture.py`/
`luno/replay.py` operate on conversation-level `MemoryTurnTrace`
capture, a different layer than a single tool-call resolution decision.
The new `EntityResolutionDecision` event is visible through the
existing Event Bus / dashboard pages the same way every other event is,
which is the observability surface this sprint actually asked for.
Deeper replay-engine integration (replaying HA command resolution
specifically) is noted under **Next sprint / deferred** below.

## Conversational-context resolution

Checked, not extended: `Planner._apply_context_shortcuts()`
(`luno/planner/planner.py`) is keyed on an **exact** slugified
`step.target` string against `context.ha_state: Dict[str, bool]`, and
only ever short-circuits an already-satisfied turn_on/turn_off request
as a no-op — it is not an entity-name resolver of any kind and requires
no changes for the new resolver to coexist with it (this sprint's
resolver runs downstream, inside the Tool Manager, strictly after this
Planner-level shortcut has already decided the task needs to execute).
There is no existing "resolve a pronoun/reference to a previously
mentioned device" mechanism for HA entities specifically (Sprint 49's
provenance/disambiguation system lives in the conversational
memory/topic subsystem, a different concern) — per the brief's own
"only if already architecturally supported" instruction, none was
added. Noted under Next sprint / deferred.

## Files

**Modified:**
- `luno/tool_manager/builtin/real_home_assistant.py` — new "Sprint 52"
  docstring section; `_VerifyConfig` gets `fuzzy_min_confidence`/
  `fuzzy_min_margin`; new `EntityResolutionResult` dataclass; new
  `_resolve_entity_tiered()`/`_emit_resolution()` methods;
  `execute()`'s two-line resolution call site updated; `_lookup_script()`/
  `_all_known_device_names()` alias bugfix; new module-level
  `_classify_exact_match()`/`_all_known_device_entities()`/
  `_score_candidates()` helpers. `_resolve_entity_id()`,
  `_unknown_device_result()`, and every action branch of `execute()`
  are byte-for-byte unmodified.
- `luno/adapters/events.py` — new `EntityResolutionDecision` Event
  subclass, added to `ADAPTER_EVENT_TYPES` and the module docstring's
  catalogue.
- `luno/bootstrap/adapters.py` — one new entry in
  `_VERIFICATION_STAGE_TO_EVENT_NAME`.

**Created:**
- `tests/test_sprint52_ha_entity_resolution.py` — 29 tests (22 labeled
  A–V per the sprint brief, plus 7 additional: observability × 3,
  set_brightness typo-benefit, Mock-handler-untouched smoke test,
  performance, forbidden-dependency source-inspection guard). Uses this
  checkout's real discovered device names/aliases (`config/lights.config.json`
  / `switches.config.json` / `scripts.config.json`), not invented ones.
- `docs/change_impact/sprint52_ha_entity_resolution.md` — this file.

## What was and wasn't executed (read this before trusting any number above)

This sandboxed session has no access to the real Windows `.venv` or a
real Home Assistant server. What follows is the honest, complete
account — no step here was skipped or assumed:

1. **SYNTAX VERIFIED** — every modified/created `.py` file compiles
   cleanly (`python3 -m py_compile`), both immediately after editing
   and in the final assembled tree.
2. **ALGORITHM UNIT TESTED (isolated reimplementation)** — before
   touching production code, the exact tiering/scoring/safety-gate
   logic was prototyped and run standalone (pure `difflib` + a hand
   -copied fixture of this checkout's real device data) to choose
   `fuzzy_min_confidence`/`fuzzy_min_margin` and confirm the 8+
   required failure-case behaviors *before* writing them into
   production code.
3. **UNIT + INTEGRATION TESTED (real execution, this session)** — a
   minimal-but-real dependency chain was assembled in this sandbox
   (`luno/config.py`, `luno/devices.py`, `luno/tool_manager/{context,
   handler,models,result,utils}.py`, `luno/tool_manager/builtin/{home_assistant,
   real_home_assistant}.py` — every file actually on this feature's
   import path, staged unmodified from the real checkout except for
   this sprint's own edits) and **`pytest` was actually run** against
   it:
   - `tests/test_sprint52_ha_entity_resolution.py` — **29 passed, 0
     failed** (the new Sprint 52 suite).
   - `luno/tool_manager/tests/test_real_home_assistant_verification.py`
     (pre-existing, unmodified, 39 tests — the full Reliability/Verified
     Smart Home Execution sprint suite this change sits directly on top
     of) — **39 passed, 0 failed**, run against the Sprint-52-modified
     handler, proving no regression in this file for real (not just
     computed by hand — see the threshold table above for how the two
     closest-call pre-existing tests were specifically checked).
   - Combined: **68 passed, 0 failed** (`python3 -m pytest tests/test_sprint52_ha_entity_resolution.py luno/tool_manager/tests/test_real_home_assistant_verification.py -q` inside the assembled tree).
4. **NOT executed in this session:** the rest of the repository's test
   suite (~2900 tests across ~100 files per Sprint 50's own baseline) —
   the full `.venv`/dependency set (torch, the LLM stack, etc.) is not
   present in this sandbox and the device bridge's own Linux VM has no
   network access and lacks the project's dependencies (confirmed:
   missing `pytest`/`torch`, DNS resolution fails). `tests/
   test_verification_dashboard.py` (also touches `RealHomeAssistantHandler`,
   via a full `RuntimeDemoConsole`/dashboard server) was identified but
   not executed — it needs substantially more of the runtime staged than
   this focused feature required, and is a materially different kind of
   test (full dashboard E2E) than the resolver logic this sprint
   changes.
5. **NOT LIVE VERIFIED** — no real Home Assistant server was reached.
   Every test above uses `FakeHAClient`, a synthetic stand-in
   (`call_service()`/`get_entity_state()`), the same convention the
   pre-existing Reliability Sprint test file already uses. **Vinn must
   run the real dashboard/voice pipeline against a live device to
   confirm real-world behavior** — this is stated explicitly per the
   sprint brief's own instruction not to fabricate HA results.
6. **Regression scope, precisely stated:** confirmed, by real
   execution, that this change does not break the 39
   pre-existing tests in the ONE file most directly downstream of it.
   Cross-referenced (via `grep`, not executed) every other
   `.py` file in the checkout that references `RealHomeAssistantHandler`/
   `_resolve_entity_id`/`_unknown_device_result`/`_all_known_device_names`/
   `_lookup_script` for any other target/registry combination that could
   collide with the new thresholds — none found beyond the two already
   checked above and the literal-entity_id-shaped targets in
   `test_verification_dashboard.py` (`"light.demo"`/`"light.stuck"`,
   unaffected — they hit the untouched literal-passthrough path). The
   full 2900-test repository sweep this checkout's own
   `docs/testing/regression_baseline.md` methodology calls for was
   **not** run in this session — Vinn should run it before merging,
   using the same chunked `pytest -n 4` approach already documented
   there.

## Performance

`_resolve_entity_tiered()`'s fuzzy path measured for real in this
session (500-call loop, `time.time()`, cloud sandbox — not the real
Windows host, so treat as directional, not authoritative): **mean
0.20ms/call**, well under a 5ms target and consistent with "no network/
LLM call, pure in-process string comparison over ~6 devices." Tier 1–3
(the common case) costs nothing beyond what `_resolve_entity_id()`
already cost before this sprint — the fuzzy scoring code path is never
even entered.

## Persistent state

No `config/*.json` file is read differently or written to by this
sprint — `_score_candidates()`/`_all_known_device_entities()` only ever
*read* the same `luno.devices.LIGHTS/SWITCHES/SCRIPTS` dicts every
pre-existing lookup already reads. No new files, no new directories, no
new config keys. Not independently SHA256-verified in this session
(unlike Sprint 50's own persistent-state check) since the full
repository checkout with its config directory wasn't staged wholesale —
Vinn can confirm with a before/after hash of `config/*.json` when
running the real suite.

## Known limitations

1. `MockHomeAssistantHandler` (`luno/tool_manager/builtin/home_assistant.py`)
   was deliberately left unmodified — it never consulted `luno.devices`
   at all (it just echoes whatever target string it's given), so there
   is no resolver to extend there. Testability without live HA is
   already covered by `RealHomeAssistantHandler` + `FakeHAClient` (the
   pattern the pre-existing Reliability Sprint tests already use, and
   this sprint's own tests reuse).
2. Ambiguity gate could not be exercised with a *naturally* colliding
   real device pair (this checkout's real registry doesn't have one) —
   exercised instead via a direct gate-logic unit test and an
   env-var-widened end-to-end test, both using real device labels. See
   "Ambiguity example" above.
3. `entity_id`-literal passthrough (`"light.foo"` style targets) is
   untouched — still trusted unconditionally if it has the right shape,
   exactly as before this sprint. Validating it against the live HA
   registry before executing was considered and explicitly deferred
   (see below) to avoid changing execution semantics for a path this
   sprint's actual bug report never touched.
4. Full-repository regression (~2900 tests) and live-HA verification
   were not performed in this session — see item 4/5 above.

## Invariants preserved

- `_resolve_entity_id()` — the pre-sprint tier 1–3 lookup — is called
  as-is, never reimplemented; its return value is exactly what
  `_resolve_entity_tiered()` returns whenever it's non-`None`.
- Fuzzy resolution never overrides an exact/alias/literal match — it is
  only ever consulted after `_resolve_entity_id()` returns `None`.
- Ambiguous cases always refuse — `executable=False`, no entity chosen,
  no guessing, verified both by direct unit test and end-to-end.
- `_unknown_device_result()`'s messages, `ToolResult` schema, and every
  action branch's execution semantics (on/off verify loop, run_script,
  set_temperature, set_color, set_brightness) are unmodified.
- No second HA system, no embeddings, no vector search, no LLM judge,
  no network call anywhere in `_score_candidates()`.
- No global mutable device-state cache added — resolution reads
  `luno.devices.LIGHTS/SWITCHES/SCRIPTS` fresh every call, same as
  before.
- Sprint 49 (conversational entity provenance/disambiguation), TTS,
  voice lifecycle, vision, and the Dashboard Turn-State Recovery fix are
  untouched — none of their files were opened or modified this sprint.

## Next sprint / deferred (explicitly not implemented opportunistically)

- Validating `entity_id`-literal passthrough targets against the live
  HA entity registry before executing (currently trusted unconditionally
  if shape-valid, same as pre-sprint).
- A genuine contextual-reference resolver for HA entities ("turn it off"
  referring to a device mentioned 2 turns ago) — no such mechanism
  exists today for HA specifically; Sprint 49's provenance system is a
  conversational-memory concern, not wired to `luno.tool_manager` at
  all.
- Extending `luno/test_capture.py`/`luno/replay.py` to capture/replay
  individual HA resolution decisions (as opposed to whole conversation
  turns) — the sprint's observability requirement is already met via
  the Event Bus (`EntityResolutionDecision`); a deeper capture/replay
  integration is a larger, separate piece of work.
- Full live-HA verification and the full ~2900-test repository
  regression sweep, both blocked on this sandbox's lack of the real
  Windows `.venv` / network access — must be run by Vinn (or a future
  session with device access) before merging with full confidence.
